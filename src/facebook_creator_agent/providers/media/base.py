"""Media provider interface and normalized candidate schema (FR-030, T0503)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel, Field


class AssetCandidate(BaseModel):
    provider: str
    provider_asset_id: Optional[str] = None
    title: Optional[str] = None
    kind: str = "video"  # video | image
    source_page_url: Optional[str] = None  # human-readable page (provenance)
    download_url: Optional[str] = None  # direct file URL when the provider allows it
    license_name: Optional[str] = None
    license_url: Optional[str] = None
    attribution: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[float] = None
    local_source_path: Optional[str] = None  # for the fixture / user providers
    extra: dict[str, Any] = Field(default_factory=dict)


class MediaProvider(ABC):
    provider_id: str = ""

    @property
    @abstractmethod
    def configured(self) -> bool:
        """True when this provider can actually be queried (keys/dirs present)."""

    @property
    def requires_key(self) -> bool:
        return False

    @abstractmethod
    async def search(self, query: str, kind: str, max_results: int) -> list[AssetCandidate]:
        """Return candidates; never raises for 'not configured' — the registry
        only calls configured providers."""

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": self.configured,
            "requires_key": self.requires_key,
        }
