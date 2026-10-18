"""OpenAI-compatible chat/completions adapter.

* configurable base_url / model / api_key / timeout;
* bounded retries for transient failures (connect errors, timeouts, 429, 5xx);
* normalized errors mapped to the ARCHITECTURE.md §9 taxonomy;
* strict schema validation of model output — malformed JSON or schema drift is
  retried once with a corrective instruction, then rejected;
* secrets never enter prompts or logs (Authorization header is built in one
  place; URLs are sanitized before logging).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, ValidationError

from ...core.errors import CreatorError, ErrorCode
from ...observability.logging import get_logger, sanitize_url
from .base import LLMAdapter, LLMCapabilities, UnconfiguredLLM

logger = get_logger("providers.llm")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE_RE.sub("", cleaned).strip()
    return cleaned


def _extract_json_object(text: str) -> str:
    """Best-effort extraction of the outermost JSON object from model text."""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def normalize_llm_error(exc: Exception, *, endpoint_label: str) -> CreatorError:
    if isinstance(exc, httpx.TimeoutException):
        return CreatorError(
            ErrorCode.PROVIDER_UNREACHABLE,
            f"LLM endpoint {endpoint_label} timed out.",
            stage="script",
            retryable=True,
            next_action="Retry, raise FCA_LLM_TIMEOUT_SECONDS, or check the endpoint load.",
        )
    if isinstance(exc, httpx.ConnectError):
        return CreatorError(
            ErrorCode.PROVIDER_UNREACHABLE,
            f"LLM endpoint {endpoint_label} is not reachable.",
            stage="script",
            retryable=True,
            next_action="Start the endpoint (e.g. local server) and run creator_test_llm.",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return CreatorError(
                ErrorCode.PROVIDER_AUTH_FAILED,
                f"LLM endpoint rejected the credentials (HTTP {status}).",
                stage="script",
                next_action="Check FCA_LLM_API_KEY for the configured endpoint.",
            )
        if status == 429:
            return CreatorError(
                ErrorCode.RATE_LIMITED,
                "LLM endpoint rate limit reached (HTTP 429).",
                stage="script",
                retryable=True,
                next_action="Wait and retry; reduce request frequency.",
            )
        return CreatorError(
            ErrorCode.PROVIDER_UNREACHABLE,
            f"LLM endpoint returned HTTP {status}.",
            stage="script",
            retryable=status >= 500,
            next_action="Check the endpoint configuration and retry.",
        )
    return CreatorError(
        ErrorCode.PROVIDER_UNREACHABLE,
        f"LLM request failed ({type(exc).__name__}).",
        stage="script",
        retryable=True,
    )


class OpenAICompatibleLLM(LLMAdapter):
    provider_id = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: Optional[str],
        model: Optional[str],
        api_key: Optional[str] = None,
        timeout_seconds: int = 90,
        max_retries: int = 2,
        temperature: Optional[float] = 0.4,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/") or None
        self.model = model
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, min(max_retries, 5))
        self.temperature = temperature
        self._transport = transport
        self.capabilities = LLMCapabilities()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def _endpoint(self) -> str:
        assert self.base_url
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            transport=self._transport,
            follow_redirects=False,
        )

    async def _chat(self, messages: list[dict[str, str]], *, temperature: Optional[float] = None) -> str:
        if not self.configured:
            raise CreatorError(
                ErrorCode.CONFIG_INVALID,
                "LLM endpoint is not configured.",
                stage="script",
                next_action="Set FCA_LLM_BASE_URL and FCA_LLM_MODEL, then run creator_test_llm.",
            )
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        temp = self.temperature if temperature is None else temperature
        if temp is not None:
            payload["temperature"] = temp
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self._client() as client:
                    response = await client.post(self._endpoint(), json=payload, headers=headers)
                if response.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise CreatorError(
                        ErrorCode.MODEL_OUTPUT_INVALID,
                        "LLM response contained no choices.",
                        stage="script",
                        next_action="Verify the endpoint implements /chat/completions.",
                    )
                message = choices[0].get("message") or {}
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise CreatorError(
                        ErrorCode.MODEL_OUTPUT_INVALID,
                        "LLM response contained no text content.",
                        stage="script",
                        next_action="Try a different model; check endpoint compatibility.",
                    )
                return content
            except CreatorError:
                raise
            except httpx.HTTPStatusError as exc:  # retry only transient classes
                if exc.response.status_code in (429,) or exc.response.status_code >= 500:
                    last_error = exc
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(2.0 * (attempt + 1), 6.0))
                        continue
                raise normalize_llm_error(exc, endpoint_label=sanitize_url(self.base_url or ""))
            except (httpx.TransportError,) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(min(2.0 * (attempt + 1), 6.0))
                    continue
                raise normalize_llm_error(exc, endpoint_label=sanitize_url(self.base_url or ""))
        # exhausted retries on a transient condition
        raise normalize_llm_error(
            last_error or RuntimeError("unknown"), endpoint_label=sanitize_url(self.base_url or "")
        )

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_model: type[BaseModel],
        *,
        max_attempts: int = 2,
    ) -> BaseModel:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        schema_json = json.dumps(schema_model.model_json_schema(), ensure_ascii=False)
        messages[0]["content"] = (
            system_prompt + "\n\nRespond with a single JSON object matching this JSON schema:\n" + schema_json
        )
        last_error: Optional[str] = None
        for attempt in range(max(1, max_attempts)):
            content = await self._chat(messages)
            candidate = _extract_json_object(_strip_fences(content))
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                last_error = f"invalid JSON ({exc.msg} at position {exc.pos})"
            else:
                try:
                    return schema_model.model_validate(parsed)
                except ValidationError as exc:
                    fields = ", ".join(".".join(str(p) for p in err["loc"]) or "?" for err in exc.errors()[:5])
                    last_error = f"schema violations: {fields}"
            if attempt < max_attempts - 1:
                messages = messages[:1] + [
                    {"role": "user", "content": user_prompt},
                    {
                        "role": "assistant",
                        "content": content[:2000],
                    },
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was invalid against the required JSON schema "
                            f"({last_error}). Reply again with ONLY the corrected JSON object."
                        ),
                    },
                ]
                logger.info("retrying LLM generation after invalid output (%s)", last_error)
        raise CreatorError(
            ErrorCode.MODEL_OUTPUT_INVALID,
            f"Model output failed schema validation after {max_attempts} attempt(s): {last_error}.",
            stage="script",
            next_action="Adjust the prompt/revision_instructions or use a model with stronger JSON output.",
        )

    async def test(self) -> dict[str, Any]:
        started = time.perf_counter()
        content = await self._chat(
            [
                {
                    "role": "system",
                    "content": "You are a connectivity probe. Answer with exactly one word.",
                },
                {"role": "user", "content": "Reply with the single word: OK"},
            ],
            temperature=0.0,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "model": self.model,
            "endpoint": sanitize_url(self.base_url or ""),
            "latency_ms": latency_ms,
            "reply_preview": content.strip()[:60],
            "capabilities": self.capabilities.model_dump(),
        }

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": self.configured,
            "base_url": sanitize_url(self.base_url or ""),
            "model": self.model,
            "api_key_set": bool(self._api_key),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "capabilities": self.capabilities.model_dump(),
            "note": "OpenAI-compatible text generation only; TTS/vision/web-search are separate adapters.",
        }


def build_llm(settings: Any) -> LLMAdapter:
    from ...config.settings import Settings

    assert isinstance(settings, Settings)
    llm = settings.llm
    if not (llm.base_url and llm.model):
        return UnconfiguredLLM()
    return OpenAICompatibleLLM(
        base_url=llm.base_url,
        model=llm.model,
        api_key=llm.api_key.get_secret_value() if llm.api_key else None,
        timeout_seconds=llm.timeout_seconds,
        max_retries=llm.max_retries,
        temperature=llm.temperature,
    )
