"""Manual narration fallback (FR-041, T0603).

A user-supplied audio file is validated (exists, decodable, plausible
duration) with ffprobe before it is accepted, then copied into the project's
``narration/`` folder with a sha256 and provenance metadata.  No ffmpeg
re-encode is needed; the render pipeline mixes the file as-is.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ...core.errors import CreatorError, ErrorCode
from ...core.ids import sha256_file
from ...core.paths import safe_filename
from ...observability.logging import get_logger

logger = get_logger("providers.tts.manual")

_ALLOWED_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac"}


async def ingest_manual_narration(
    source: Path,
    dest_dir: Path,
    *,
    ffprobe_path: str = "ffprobe",
    expected_min_seconds: float = 0.5,
    max_seconds: float = 3600.0,
) -> dict[str, Any]:
    source = Path(source)
    if not source.is_file():
        raise CreatorError(
            ErrorCode.NOT_FOUND,
            f"Narration file does not exist: {source.name!r}.",
            stage="narration",
            next_action="Pass a valid path to an audio file (wav/mp3/m4a/aac/ogg/flac).",
        )
    ext = source.suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise CreatorError(
            ErrorCode.AUDIO_INVALID,
            f"Narration file extension {ext or '(none)'!r} is not supported.",
            stage="narration",
            next_action=f"Convert to one of: {', '.join(sorted(_ALLOWED_EXT))}.",
        )

    from ...media_pipeline.validation import probe_audio

    probe = await probe_audio(source, ffprobe_path=ffprobe_path)
    if not probe["has_audio"]:
        raise CreatorError(
            ErrorCode.AUDIO_INVALID,
            "Narration file has no decodable audio stream.",
            stage="narration",
            next_action="Provide an audio file with a valid audio stream.",
        )
    duration = probe["duration_seconds"] or 0.0
    if duration < expected_min_seconds:
        raise CreatorError(
            ErrorCode.AUDIO_INVALID,
            f"Narration duration {duration:.2f}s is shorter than the {expected_min_seconds}s minimum.",
            stage="narration",
            next_action="Provide a longer narration file.",
        )
    if duration > max_seconds:
        raise CreatorError(
            ErrorCode.AUDIO_INVALID,
            f"Narration duration {duration:.2f}s exceeds the {max_seconds}s limit.",
            stage="narration",
            next_action="Shorten the narration or split the project.",
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(source.name, fallback=f"narration{ext}")
    dest = dest_dir / filename
    if dest.resolve() != source.resolve():
        shutil.copyfile(source, dest)
    sha = sha256_file(dest)
    logger.info("manual narration ingested: %s (%.2fs)", filename, duration)
    return {
        "path": dest,
        "sha256": sha,
        "duration_seconds": duration,
        "source_name": source.name,
        "provider": "manual",
        "codec": probe.get("codec"),
        "sample_rate": probe.get("sample_rate"),
        "channels": probe.get("channels"),
    }
