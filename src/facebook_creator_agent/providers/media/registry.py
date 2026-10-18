"""Media provider registry.

Only configured providers are queried. Key-required providers exist as
declared, capability-gated adapters; without a key they are listed by
creator_list_providers but never called (no key-required network calls unless
configured — task constraint and FR-030).
"""

from __future__ import annotations

from typing import Any, Optional

from ...config.settings import Settings
from .base import AssetCandidate, MediaProvider
from .fixture import FixtureMediaProvider


class PexelsMediaProvider(MediaProvider):
    """Pexels stock media adapter (EXPERIMENTAL, unverified against live API).

    Endpoint shapes follow the public Pexels API documentation (v1
    /videos/search and /photos/search, ``Authorization`` header).  This code
    path is capability-gated: it is only called when ``FCA_PEXELS_API_KEY`` is
    configured, and it has NOT been validated against the live service in this
    repository's test environment (no credentials available).  Results are
    normalized to AssetCandidate with the Pexels License recorded as evidence
    (Pexels does not require attribution, but the photographer/creator name is
    preserved anyway).
    """

    provider_id = "pexels"
    BASE = "https://api.pexels.com"

    def __init__(self, api_key: Optional[str], *, client: Any = None) -> None:
        self._api_key = api_key
        self._client = client

    @property
    def requires_key(self) -> bool:
        return True

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, kind: str, max_results: int) -> list[AssetCandidate]:
        if not self.configured:
            return []
        import httpx

        results: list[AssetCandidate] = []
        async with httpx.AsyncClient(timeout=30) as client:
            if kind in ("video", "any"):
                results.extend(
                    await self._search(client, query, max_results, "videos")
                )
            if kind in ("image", "any") and len(results) < max_results:
                results.extend(
                    await self._search(client, query, max_results - len(results), "photos")
                )
        return results[:max_results]

    async def _search(
        self, client: "httpx.AsyncClient", query: str, limit: int, endpoint: str
    ) -> list[AssetCandidate]:
        import httpx

        url = f"{self.BASE}/v1/{endpoint}/search"
        try:
            response = await client.get(
                url,
                params={"query": query, "per_page": max(1, min(limit, 15))},
                headers={"Authorization": self._api_key or ""},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError:
            # Degrade to empty rather than failing the whole search; the tool
            # layer records a warning.
            return []
        out: list[AssetCandidate] = []
        for item in payload.get("videos" if endpoint == "videos" else "photos", [])[:limit]:
            if endpoint == "videos":
                files = item.get("video_files") or []
                best = next((f for f in files if f.get("quality") == "sd"), files[0] if files else None)
                out.append(
                    AssetCandidate(
                        provider=self.provider_id,
                        provider_asset_id=str(item.get("id")),
                        title=str(item.get("url") or ""),
                        kind="video",
                        source_page_url=item.get("url"),
                        download_url=(best or {}).get("link"),
                        license_name="PEXELS-LICENSE",
                        license_url="https://www.pexels.com/license/",
                        attribution=(item.get("user") or {}).get("name"),
                        width=best.get("width") if best else None,
                        height=best.get("height") if best else None,
                        duration_seconds=(item.get("duration") or None),
                    )
                )
            else:
                src = item.get("src") or {}
                out.append(
                    AssetCandidate(
                        provider=self.provider_id,
                        provider_asset_id=str(item.get("id")),
                        title=item.get("alt"),
                        kind="image",
                        source_page_url=item.get("url"),
                        download_url=src.get("large2x") or src.get("large") or src.get("original"),
                        license_name="PEXELS-LICENSE",
                        license_url="https://www.pexels.com/license/",
                        attribution=(item.get("photographer") or None),
                        width=item.get("width"),
                        height=item.get("height"),
                    )
                )
        return out


class MediaProviderRegistry:
    def __init__(self, providers: list[MediaProvider]) -> None:
        self.providers: dict[str, MediaProvider] = {p.provider_id: p for p in providers}

    @classmethod
    def from_settings(cls, settings: Settings) -> "MediaProviderRegistry":
        providers: list[MediaProvider] = [
            FixtureMediaProvider(settings.fixture_dir),
        ]
        if settings.media.pexels_api_key is not None:
            providers.append(PexelsMediaProvider(settings.media.pexels_api_key.get_secret_value()))
        else:
            providers.append(PexelsMediaProvider(None))
        return cls(providers)

    def get(self, provider_id: str) -> Optional[MediaProvider]:
        return self.providers.get(provider_id)

    def status_list(self, allowed_providers: set[str]) -> list[dict[str, Any]]:
        out = []
        for provider in self.providers.values():
            entry = provider.status()
            entry["allowed_by_policy"] = provider.provider_id.lower() in {
                p.lower() for p in allowed_providers
            }
            out.append(entry)
        return out

    async def search(
        self,
        query: str,
        kind: str,
        max_results: int,
        *,
        allowed_providers: set[str],
    ) -> tuple[list[AssetCandidate], list[str]]:
        """Query configured+allowed providers; returns (candidates, warnings)."""
        candidates: list[AssetCandidate] = []
        warnings: list[str] = []
        allowed_lower = {p.lower() for p in allowed_providers}
        for provider in self.providers.values():
            if provider.provider_id.lower() not in allowed_lower:
                continue
            if not provider.configured:
                if provider.requires_key:
                    warnings.append(
                        f"provider {provider.provider_id!r} is allowed but not configured; skipped"
                    )
                continue
            try:
                found = await provider.search(query, kind, max_results)
            except Exception as exc:  # provider failure must not kill the search
                warnings.append(
                    f"provider {provider.provider_id!r} search failed ({type(exc).__name__})"
                )
                continue
            candidates.extend(found)
        return candidates[:max_results], warnings
