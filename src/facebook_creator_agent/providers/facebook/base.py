"""Facebook adapter interface (ARCHITECTURE.md §4.9, FR-060..FR-068).

Contract highlights:
* official Graph API flows only; no password collection, ever;
* every method normalizes failures into the §9 taxonomy;
* ``upload_video`` distinguishes DEFINITE failure (never sent / rejected by
  the API), UNKNOWN outcome (timeout or 5xx after the request may have been
  processed), and success;
* tokens are SecretStr-like: never logged, never echoed in errors.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel


class PageInfo(BaseModel):
    id: str
    name: str
    tasks: list[str] = []
    can_publish: bool = False  # CREATE_CONTENT present


class TokenInfo(BaseModel):
    id: str
    name: Optional[str] = None


@dataclass
class UploadResult:
    remote_id: str
    response: dict[str, Any] = field(default_factory=dict)


class FacebookAdapterError(Exception):
    """Definite failure: request rejected or never sent."""

    def __init__(self, code: str, explanation: str, *, retryable: bool = False) -> None:
        super().__init__(explanation)
        self.code = code
        self.explanation = explanation
        self.retryable = retryable


class FacebookUnknownOutcome(Exception):
    """The request may have reached Meta; the outcome is UNKNOWN."""

    def __init__(self, explanation: str) -> None:
        super().__init__(explanation)
        self.explanation = explanation


class FacebookAdapter(ABC):
    provider_id: str = "facebook"

    @property
    @abstractmethod
    def has_credentials(self) -> bool: ...

    @abstractmethod
    async def check_token(self) -> TokenInfo: ...

    @abstractmethod
    async def list_pages(self) -> list[PageInfo]: ...

    @abstractmethod
    async def get_page(self, page_id: str) -> PageInfo: ...

    @abstractmethod
    async def upload_video(
        self,
        page_id: str,
        video_path: Path,
        payload: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> UploadResult: ...

    @abstractmethod
    async def get_video_status(self, video_id: str) -> dict[str, Any]:
        """{exists: bool, status: str|None} — used for reconciliation."""

    def status(self) -> dict[str, Any]:
        return {"provider": self.provider_id, "credentials_present": self.has_credentials}
