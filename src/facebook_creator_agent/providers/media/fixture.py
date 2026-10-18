"""Fixture media provider — local, key-free, for tests and local dry runs.

Searches ``FCA_FIXTURE_MEDIA_DIR`` (config ``fixture_dir``) for ``*.mp4``,
``*.png``, ``*.jpg``/``*.jpeg`` files.  Optional per-file sidecar metadata
``<name>.meta.json`` supplies provenance:

    {"license_name": "CC0-1.0", "license_url": "...", "attribution": "...",
     "source_page_url": "...", "title": "..."}

Files without a sidecar are treated as operator-owned test fixtures licensed
``CC0-1.0`` (they only exist because the operator put them there); this keeps
the default-deny policy meaningful while remaining usable offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .base import AssetCandidate, MediaProvider

_VIDEO_EXT = {".mp4", ".mov"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


class FixtureMediaProvider(MediaProvider):
    provider_id = "fixture"

    def __init__(self, directory: Optional[str | Path]) -> None:
        self.directory = Path(directory) if directory else None

    @property
    def configured(self) -> bool:
        return bool(self.directory and self.directory.is_dir())

    @property
    def requires_key(self) -> bool:
        return False

    def status(self) -> dict:
        base = super().status()
        base["directory"] = str(self.directory) if self.directory else None
        return base

    async def search(self, query: str, kind: str, max_results: int) -> list[AssetCandidate]:
        if not self.configured:
            return []
        tokens = [t for t in (query or "").lower().split() if t]
        scored: list[tuple[int, Path]] = []
        for path in sorted((self.directory or Path()).iterdir()):
            if not path.is_file() or path.name.endswith(".meta.json") or path.name.endswith(".part"):
                continue
            ext = path.suffix.lower()
            file_kind = "video" if ext in _VIDEO_EXT else "image" if ext in _IMAGE_EXT else None
            if file_kind is None:
                continue
            if kind in ("video", "image") and file_kind != kind:
                continue
            name = path.stem.lower()
            score = sum(1 for token in tokens if token in name)
            scored.append((score, path))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        results: list[AssetCandidate] = []
        for _, path in scored[: max(1, max_results)]:
            results.append(self._candidate_for(path))
        return results

    def _candidate_for(self, path: Path) -> AssetCandidate:
        sidecar = path.with_name(path.name + ".meta.json")
        meta: dict = {}
        if sidecar.is_file():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        kind = "video" if path.suffix.lower() in _VIDEO_EXT else "image"
        return AssetCandidate(
            provider=self.provider_id,
            provider_asset_id=path.name,
            title=meta.get("title") or path.stem,
            kind=kind,
            source_page_url=meta.get("source_page_url") or path.as_uri(),
            download_url=None,  # local file — copied in by the intake path, never downloaded
            license_name=meta.get("license_name", "CC0-1.0"),
            license_url=meta.get("license_url"),
            attribution=meta.get("attribution", "Operator-owned test fixture"),
            local_source_path=str(path),
            extra={"sidecar": sidecar.name if sidecar.is_file() else None},
        )
