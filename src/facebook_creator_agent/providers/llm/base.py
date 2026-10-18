"""LLM adapter interface (FR-010..FR-016)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel


class LLMCapabilities(BaseModel):
    """What this endpoint is used for. OpenAI-compatible does NOT imply
    TTS/vision/web-search/video generation (PRD §7.2, FR-016)."""

    text_generation: bool = True
    json_mode_hint: bool = False
    vision: bool = False
    tts: bool = False
    web_search: bool = False


class LLMAdapter(ABC):
    provider_id: str = "llm"

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_model: type[BaseModel],
        *,
        max_attempts: int = 2,
    ) -> BaseModel:
        """Generate JSON and validate strictly against ``schema_model``.

        Raises CreatorError(MODEL_OUTPUT_INVALID) when output stays invalid.
        """

    @abstractmethod
    async def test(self) -> dict[str, Any]:
        """Minimal endpoint test; returns latency/model info or raises."""

    def status(self) -> dict[str, Any]:
        return {"provider": self.provider_id, "configured": self.configured}


class UnconfiguredLLM(LLMAdapter):
    provider_id = "unconfigured"

    def __init__(self, reason: str = "no LLM endpoint configured") -> None:
        self.reason = reason

    @property
    def configured(self) -> bool:
        return False

    async def generate_json(self, *args: Any, **kwargs: Any) -> BaseModel:  # pragma: no cover
        from ...core.errors import CreatorError, ErrorCode

        raise CreatorError(
            ErrorCode.CONFIG_INVALID,
            "No LLM endpoint is configured (set FCA_LLM_BASE_URL and FCA_LLM_MODEL).",
            stage="script",
            next_action="Configure an OpenAI-compatible endpoint, then run creator_test_llm.",
        )

    async def test(self) -> dict[str, Any]:  # pragma: no cover
        return {"ok": False, "error": self.reason}
