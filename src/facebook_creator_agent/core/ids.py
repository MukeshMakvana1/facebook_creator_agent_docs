"""ID generation, canonical JSON, and hashing helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def new_id(prefix: str, length: int = 12) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:length]}"


def new_nonce() -> str:
    import secrets

    return secrets.token_hex(16)


def slugify(text: str, max_len: int = 40) -> str:
    """ASCII-ish slug safe for Windows/Linux filenames. Deterministic."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:max_len].strip("-")


def canonical_json(obj: Any) -> str:
    """Canonical JSON used for every hash: sorted keys, compact, UTF-8."""

    def _default(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, Path):
            return str(value)
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return str(value)

    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(*parts: Any, length: int = 8) -> str:
    return sha256_text(canonical_json(list(parts)))[:length]


def unique_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
