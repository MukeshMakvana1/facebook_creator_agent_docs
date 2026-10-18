"""Meta Graph API adapter (official endpoints only).

IMPORTANT — verification status: this adapter is exercised in tests against a
local fake Graph endpoint (base URLs configurable) and with mocked transports.
It has NOT been validated against the live Meta Graph API in this repository
because no credentials/developer app are available (TASK.md T0901/T0909 are
BLOCKED on that external prerequisite).  Review current official Meta docs and
pin a supported Graph version before enabling live publishing.

Endpoints used (classic, long-stable Graph shapes):
* GET  {graph}/{version}/me?fields=id,name            — token identity check
* GET  {graph}/{version}/me/accounts                    — authorized Pages + tasks
* GET  {graph}/{version}/{page-id}?fields=id,name      — Page identity recheck
* POST {graph-video}/{version}/{page-id}/videos         — video upload (multipart,
  field ``source``), with description/title/access_token
* GET  {graph}/{version}/{video-id}?fields=id,status   — reconciliation

Outcome classification:
* connection errors            -> DEFINITE failure (never sent)
* HTTP 4xx                     -> DEFINITE failure (rejected)
* HTTP 429                     -> RATE_LIMITED (retryable after backoff)
* HTTP 5xx / read timeouts /
  protocol errors after send   -> UNKNOWN (must reconcile, never blind-retry)
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Optional

import httpx

from ...core.errors import CreatorError, ErrorCode
from ...observability.logging import get_logger, redact_text, sanitize_url
from .base import (
    FacebookAdapter,
    FacebookAdapterError,
    FacebookUnknownOutcome,
    PageInfo,
    TokenInfo,
    UploadResult,
)

logger = get_logger("providers.facebook")

AUTH_DOCS = "https://developers.facebook.com/docs/facebook-login/"
PAGES_DOCS = "https://developers.facebook.com/docs/pages/access-tokens"
PUBLISH_DOCS = "https://developers.facebook.com/docs/video-api/guides/publishing"


def _map_api_error(status_code: int, payload: dict[str, Any] | None) -> FacebookAdapterError:
    message = ""
    if payload:
        err = payload.get("error") or {}
        message = redact_text(str(err.get("message") or ""))[:200]
    if status_code in (401, 403):
        return FacebookAdapterError(
            ErrorCode.FACEBOOK_AUTH_REQUIRED,
            f"Meta rejected the token/permission (HTTP {status_code}). {message}",
            retryable=False,
        )
    if status_code == 429:
        return FacebookAdapterError(
            ErrorCode.RATE_LIMITED,
            f"Meta rate limit reached (HTTP 429). {message}",
            retryable=True,
        )
    if status_code == 400 and "permission" in message.lower():
        return FacebookAdapterError(
            ErrorCode.FACEBOOK_PERMISSION_MISSING,
            f"Meta reports a missing permission: {message}",
            retryable=False,
        )
    return FacebookAdapterError(
        ErrorCode.FACEBOOK_OPERATION_UNSUPPORTED,
        f"Meta rejected the operation (HTTP {status_code}). {message}",
        retryable=False,
    )


class MetaGraphAdapter(FacebookAdapter):
    provider_id = "meta_graph"

    def __init__(
        self,
        *,
        access_token: Optional[str],
        page_id: Optional[str] = None,
        graph_version: str = "v23.0",
        graph_base_url: str = "https://graph.facebook.com",
        graph_video_base_url: str = "https://graph-video.facebook.com",
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._token = access_token
        self.page_id = page_id
        self.graph_version = graph_version
        self.graph_base = graph_base_url.rstrip("/")
        self.graph_video_base = graph_video_base_url.rstrip("/")
        self._transport = transport
        self.timeout_seconds = timeout_seconds

    @property
    def has_credentials(self) -> bool:
        return bool(self._token)

    # ------------------------------------------------------------------ utils
    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            transport=self._transport,
            follow_redirects=False,
        )

    def _url(self, path: str, *, video: bool = False) -> str:
        base = self.graph_video_base if video else self.graph_base
        return f"{base}/{self.graph_version}/{path.lstrip('/')}"

    def _require_token(self) -> str:
        if not self._token:
            raise CreatorError(
                ErrorCode.FACEBOOK_AUTH_REQUIRED,
                "No Facebook access token is configured.",
                stage="publish",
                next_action=(
                    "Complete the official Meta authorization flow, then set "
                    "FCA_FACEBOOK_ACCESS_TOKEN (never commit it)."
                ),
            )
        return self._token

    async def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "access_token": self._require_token()}
        try:
            async with self._client(self.timeout_seconds) as client:
                response = await client.get(url, params=params)
        except httpx.ConnectError as exc:
            raise CreatorError(
                ErrorCode.PROVIDER_UNREACHABLE,
                "Meta Graph API is not reachable (connection failed).",
                stage="publish",
                retryable=True,
                next_action="Check network connectivity and retry.",
            ) from exc
        except httpx.TimeoutException as exc:
            raise CreatorError(
                ErrorCode.PROVIDER_UNREACHABLE,
                "Meta Graph API request timed out.",
                stage="publish",
                retryable=True,
                next_action="Retry; if persistent, lower request scope.",
            ) from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:
                payload = None
            raise _map_api_error(response.status_code, payload)
        return response.json()

    # ------------------------------------------------------------------- API
    async def check_token(self) -> TokenInfo:
        data = await self._get_json(self._url("me"), {"fields": "id,name"})
        return TokenInfo(id=str(data.get("id")), name=data.get("name"))

    async def list_pages(self) -> list[PageInfo]:
        data = await self._get_json(self._url("me/accounts"), {"fields": "id,name,tasks"})
        pages: list[PageInfo] = []
        for item in data.get("data", []):
            tasks = [str(t) for t in item.get("tasks", [])]
            pages.append(
                PageInfo(
                    id=str(item.get("id")),
                    name=str(item.get("name") or ""),
                    tasks=tasks,
                    can_publish="CREATE_CONTENT" in tasks or not tasks,
                )
            )
        return pages

    async def get_page(self, page_id: str) -> PageInfo:
        data = await self._get_json(self._url(page_id), {"fields": "id,name"})
        return PageInfo(id=str(data.get("id")), name=str(data.get("name") or ""), can_publish=True)

    async def upload_video(
        self,
        page_id: str,
        video_path: Path,
        payload: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> UploadResult:
        token = self._require_token()
        url = self._url(f"{page_id}/videos", video=True)
        data: dict[str, Any] = {"access_token": token}
        if payload.get("caption"):
            data["description"] = str(payload["caption"])[:5000]
        if payload.get("title"):
            data["title"] = str(payload["title"])[:255]
        fileobj = open(video_path, "rb")
        try:
            files = {"source": (video_path.name, fileobj, "video/mp4")}
            async with self._client(float(timeout_seconds)) as client:
                response = await self._post_locked(client, url, data=data, files=files)
        finally:
            fileobj.close()

        if response.status_code >= 400:
            try:
                payload_json = response.json()
            except Exception:
                payload_json = None
            raise _map_api_error(response.status_code, payload_json)
        body = response.json()
        remote_id = str(body.get("id") or body.get("video_id") or "")
        if not remote_id:
            raise FacebookUnknownOutcome(
                "Meta returned HTTP 200 but no video id; outcome cannot be confirmed."
            )
        return UploadResult(remote_id=remote_id, response={"id": remote_id})

    async def _post_locked(self, client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        """POST with UNKNOWN-outcome classification for post-send failures."""
        try:
            return await client.post(url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Connection was never established: the upload definitely did not happen.
            raise FacebookAdapterError(
                ErrorCode.PROVIDER_UNREACHABLE,
                "Could not connect to Meta for upload; the video was NOT sent.",
                retryable=True,
            ) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError, httpx.PoolTimeout) as exc:
            raise FacebookUnknownOutcome(
                "Upload request outcome is UNKNOWN (network failure after the request was sent). "
                "Do not retry blindly; reconcile status first."
            ) from exc

    async def get_video_status(self, video_id: str) -> dict[str, Any]:
        try:
            data = await self._get_json(self._url(video_id), {"fields": "id,status"})
        except FacebookAdapterError as exc:
            if exc.code == ErrorCode.FACEBOOK_OPERATION_UNSUPPORTED and "404" in str(exc):
                return {"exists": False, "status": None}
            raise
        status = None
        raw_status = data.get("status")
        if isinstance(raw_status, dict):
            status = raw_status.get("video_status")
        return {"exists": True, "status": status}

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "credentials_present": self.has_credentials,
            "page_id": self.page_id,
            "graph_version": self.graph_version,
            "graph_base_url": sanitize_url(self.graph_base),
            "graph_video_base_url": sanitize_url(self.graph_video_base),
            "note": "Adapter paths are test-exercised against a local fake Graph endpoint; live Meta validation is a documented blocked task (T0909).",
        }


def build_facebook_adapter(settings) -> FacebookAdapter:  # noqa: ANN001
    from ...config.settings import Settings

    assert isinstance(settings, Settings)
    fb = settings.facebook
    return MetaGraphAdapter(
        access_token=fb.access_token.get_secret_value() if fb.access_token else None,
        page_id=fb.page_id,
        graph_version=fb.graph_version,
        graph_base_url=fb.graph_base_url,
        graph_video_base_url=fb.graph_video_base_url,
        timeout_seconds=float(fb.upload_timeout_seconds),
    )
