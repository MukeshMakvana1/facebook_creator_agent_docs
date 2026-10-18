"""ffprobe-based media validation and the render accept/reject checks.

All external processes are invoked as argument arrays (never a shell) with
timeouts.  Functions raise :class:`CreatorError` with the ARCHITECTURE.md §9
taxonomy:

* ``DEPENDENCY_MISSING``  — ffprobe/ffmpeg binary not found
* ``FFMPEG_FAILED``       — ffprobe failed to read the media
* ``MEDIA_INVALID``       — file is not usable media (probe says no streams)
* ``RENDER_VALIDATION_FAILED`` — a rendered video failed the output checks
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

from ..core.errors import CreatorError, ErrorCode
from ..observability.logging import get_logger, redact_text

logger = get_logger("media_pipeline.validation")

_TIME_RE = re.compile(r"^\s*Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")
_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")


async def _run_capture(args: list[str], *, timeout: float = 60.0) -> tuple[int, bytes, bytes]:
    """Run a subprocess with a timeout, capturing stdout/stderr."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise CreatorError(
            ErrorCode.DEPENDENCY_MISSING,
            f"Executable not found: {args[0]!r}. FFmpeg/ffprobe must be installed and on PATH "
            "(or set FCA_FFMPEG_PATH / FCA_FFPROBE_PATH).",
            stage="setup",
            next_action="Install FFmpeg (which includes ffprobe) and restart the server.",
        )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise CreatorError(
            ErrorCode.FFMPEG_FAILED,
            f"{Path(args[0]).name} timed out after {timeout:.0f}s.",
            stage="render",
            retryable=True,
            next_action="Retry; if persistent, reduce the render scope or raise the timeout.",
        )
    return proc.returncode or 0, stdout, stderr


def _sanitized_tail(stderr: bytes, limit: int = 400) -> str:
    return redact_text(stderr.decode("utf-8", "replace"))[-limit:].strip()


async def ffprobe_json(path: Path, *, ffprobe_path: str = "ffprobe", timeout: float = 60.0) -> dict[str, Any]:
    """Return the raw ``ffprobe -print_format json`` document for ``path``."""
    path = Path(path)
    if not path.is_file():
        raise CreatorError(
            ErrorCode.NOT_FOUND,
            f"Media file does not exist: {path.name!r}.",
            stage="general",
            next_action="Check the artifact path; run the earlier stage that produces it.",
        )
    args = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_error",
        str(path),
    ]
    code, stdout, stderr = await _run_capture(args, timeout=timeout)
    if code != 0:
        raise CreatorError(
            ErrorCode.FFMPEG_FAILED,
            f"ffprobe could not read {path.name!r}: {_sanitized_tail(stderr)}",
            stage="general",
            next_action="Verify the file is a valid media file (re-download or re-render it).",
        )
    try:
        return json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        raise CreatorError(
            ErrorCode.FFMPEG_FAILED,
            "ffprobe returned unparseable JSON output.",
            stage="general",
            retryable=True,
        )


def _fraction_to_float(value: str | None) -> Optional[float]:
    if not value or value in ("0/0", "N/A"):
        return None
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else None
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def summarize_probe(document: dict[str, Any]) -> dict[str, Any]:
    """Normalize an ffprobe document into the fields the pipeline needs."""
    streams = document.get("streams") or []
    fmt = document.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration: Optional[float] = None
    for candidate in (fmt.get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
        try:
            if candidate not in (None, "N/A"):
                duration = float(candidate)
                break
        except (TypeError, ValueError):
            continue
    return {
        "duration_seconds": duration,
        "format_name": fmt.get("format_name"),
        "size_bytes": int(fmt.get("size") or 0) or None,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "video_codec": (video or {}).get("codec_name"),
        "audio_codec": (audio or {}).get("codec_name"),
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "fps": _fraction_to_float((video or {}).get("avg_frame_rate")),
        "sample_rate": int((audio or {}).get("sample_rate") or 0) or None,
        "channels": (audio or {}).get("channels"),
    }


async def probe_media(path: Path, *, ffprobe_path: str = "ffprobe") -> dict[str, Any]:
    """Full probe summary for any media file (video/image/audio)."""
    return summarize_probe(await ffprobe_json(path, ffprobe_path=ffprobe_path))


async def probe_audio(path: Path, *, ffprobe_path: str = "ffprobe") -> dict[str, Any]:
    """Audio-focused probe used by narration intake (FR-044).

    Returns ``has_audio``, ``duration_seconds``, ``codec``, ``sample_rate``,
    and ``channels``.  Raises ``AUDIO_INVALID`` when the file has no decodable
    audio stream at all.
    """
    summary = summarize_probe(await ffprobe_json(path, ffprobe_path=ffprobe_path))
    if not summary["has_audio"]:
        raise CreatorError(
            ErrorCode.AUDIO_INVALID,
            f"{Path(path).name!r} contains no decodable audio stream.",
            stage="narration",
            next_action="Provide a valid audio file (wav/mp3/m4a/aac/ogg/flac).",
        )
    return {
        "has_audio": True,
        "duration_seconds": summary["duration_seconds"],
        "codec": summary["audio_codec"],
        "sample_rate": summary["sample_rate"],
        "channels": summary["channels"],
    }


async def measure_audio_levels(
    path: Path, *, ffmpeg_path: str = "ffmpeg", timeout: float = 120.0
) -> dict[str, Any]:
    """Peak/mean volume measurement used for clipping checks (FR-054).

    Audio-only decode; fast even for long files.  Returns ``{}`` when the
    measurement cannot be parsed (never fails the workflow on its own).
    """
    args = [
        ffmpeg_path,
        "-hide_banner", "-nostdin",
        "-i", str(path),
        "-vn",
        "-af", "volumedetect",
        "-f", "null",
        "-",
    ]
    code, _stdout, stderr = await _run_capture(args, timeout=timeout)
    text = stderr.decode("utf-8", "replace")
    if code != 0 and "max_volume" not in text:
        return {}
    max_match = _MAX_VOLUME_RE.search(text)
    mean_match = _MEAN_VOLUME_RE.search(text)
    return {
        "max_volume_db": float(max_match.group(1)) if max_match else None,
        "mean_volume_db": float(mean_match.group(1)) if mean_match else None,
    }


async def decode_test(path: Path, *, ffmpeg_path: str = "ffmpeg", timeout: float = 300.0) -> dict[str, Any]:
    """Full-file decode test: decode every stream to null (FR-056)."""
    args = [
        ffmpeg_path,
        "-hide_banner", "-nostdin",
        "-v", "error",
        "-i", str(path),
        "-f", "null",
        "-",
    ]
    code, _stdout, stderr = await _run_capture(args, timeout=timeout)
    if code != 0:
        raise CreatorError(
            ErrorCode.RENDER_VALIDATION_FAILED,
            f"Decode test failed for {Path(path).name!r}: {_sanitized_tail(stderr)}",
            stage="render",
            next_action="Re-render the video; if the failure repeats, inspect the ffmpeg log.",
        )
    return {"decode_ok": True, "decoded_bytes_ok": True}


async def validate_render(
    path: Path,
    *,
    ffprobe_path: str = "ffprobe",
    ffmpeg_path: str = "ffmpeg",
    expected_width: Optional[int] = None,
    expected_height: Optional[int] = None,
    expected_min_duration: Optional[float] = None,
    expected_max_duration: Optional[float] = None,
    require_audio: bool = True,
    duration_grace_seconds: float = 1.5,
) -> dict[str, Any]:
    """Validate a rendered video (FR-056, T0708).

    Checks performed:
    1. ffprobe stream presence: video stream (and audio when required);
    2. dimensions match the requested preset;
    3. duration within the expected window (grace applied);
    4. audio peak level (clipping warning);
    5. full decode test of the entire file.

    Raises ``RENDER_VALIDATION_FAILED`` on hard failures; returns a report
    with warnings otherwise.
    """
    probe = await probe_media(path, ffprobe_path=ffprobe_path)
    issues: list[str] = []
    warnings: list[str] = []

    if not probe["has_video"]:
        issues.append("no video stream found")
    if require_audio and not probe["has_audio"]:
        issues.append("no audio stream found")

    if expected_width and expected_height:
        if probe["width"] != expected_width or probe["height"] != expected_height:
            issues.append(
                f"dimensions {probe['width']}x{probe['height']} do not match the "
                f"requested {expected_width}x{expected_height}"
            )

    duration = probe["duration_seconds"] or 0.0
    if expected_min_duration is not None and duration < expected_min_duration - duration_grace_seconds:
        issues.append(
            f"duration {duration:.2f}s is shorter than the expected {expected_min_duration:.2f}s"
        )
    if expected_max_duration is not None and duration > expected_max_duration + duration_grace_seconds:
        issues.append(
            f"duration {duration:.2f}s exceeds the expected {expected_max_duration:.2f}s"
        )
    if duration <= 0.0:
        issues.append("duration could not be determined")

    levels: dict[str, Any] = {}
    if probe["has_audio"]:
        levels = await measure_audio_levels(path, ffmpeg_path=ffmpeg_path)
        max_db = levels.get("max_volume_db")
        if max_db is not None and max_db > -1.0:
            warnings.append(f"audio peak {max_db:.1f} dB is close to clipping")
        if max_db is not None and max_db < -35.0:
            warnings.append(f"audio peak {max_db:.1f} dB is very low; check narration gain")

    if issues:
        raise CreatorError(
            ErrorCode.RENDER_VALIDATION_FAILED,
            "Render validation failed: " + "; ".join(issues) + ".",
            stage="render",
            next_action="Re-render (creator_render_preview) or inspect the timeline inputs.",
            details={"issues": issues, "probe": probe},
        )

    decode = await decode_test(path, ffmpeg_path=ffmpeg_path)
    return {
        "path": Path(path).name,
        "probe": probe,
        "audio_levels": levels,
        "decode": decode,
        "warnings": warnings,
        "ok": True,
    }


async def detect_binaries(ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> dict[str, Any]:
    """Version diagnostics for doctor (T0701)."""
    out: dict[str, Any] = {"ffmpeg": None, "ffprobe": None}
    for key, exe in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
        try:
            code, stdout, stderr = await _run_capture([exe, "-version"], timeout=15.0)
            text = (stdout or stderr).decode("utf-8", "replace")
            first_line = text.splitlines()[0] if text else ""
            out[key] = {"available": code == 0, "version": first_line.strip()}
        except CreatorError as exc:
            out[key] = {"available": False, "version": None, "error": exc.safe_explanation}
    out["available"] = bool(out["ffmpeg"] and out["ffmpeg"].get("available") and out["ffprobe"] and out["ffprobe"].get("available"))
    return out
