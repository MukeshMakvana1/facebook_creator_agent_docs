"""Caption generation (FR-043, T0605/T0606).

Alignment support is capability-based: when a TTS adapter returns word/segment
timings they are used directly; otherwise the **documented approximate-timing
fallback** distributes each scene's narration over its final rendered window
(the same windows the renderer uses, so captions stay in sync with the video).

SRT output only — the most portable subtitle format; the review package states
which timing mode was used.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..core.errors import CreatorError, ErrorCode

MAX_LINE_CHARS = 42
MAX_LINES = 2


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    millis = int(round(seconds * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


_TIME_LINE_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$"
)


def wrap_text(text: str, *, max_chars: int = MAX_LINE_CHARS, max_lines: int = MAX_LINES) -> str:
    """Greedy wrap to at most ``max_lines`` lines of ~``max_chars`` chars."""
    words = (text or "").split()
    if not words:
        return ""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines:
        joined = " ".join(lines)
        if len(joined) < len(" ".join(words)):
            # mark truncation rather than silently dropping words
            if len(lines[-1]) + 3 <= max_chars:
                lines[-1] = lines[-1] + "..." 
            else:
                lines[-1] = lines[-1][:-3] + "..."
    return "\n".join(lines)


def build_srt(segments: list[dict[str, Any]]) -> str:
    """Build SRT text from ``[{start, end, text}]`` segments."""
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        text = wrap_text(str(segment.get("text") or ""))
        if not text:
            continue
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or 0.0)
        if end <= start:
            end = start + 0.5
        blocks.append(
            f"{index}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n"
        )
    return "\n".join(blocks)


def scenes_to_segments(
    scenes: list[dict[str, Any]], *, alignment: Optional[list[dict[str, Any]]] = None
) -> tuple[list[dict[str, Any]], str]:
    """Produce caption segments and the timing mode used.

    ``scenes`` items: ``{scene_id, caption_text, start, end}`` where start/end
    are the final scene windows in the render.  When ``alignment`` segments
    (``{start, end, text}``) are supplied they are used verbatim and the mode
    is ``"alignment"``; otherwise the mode is ``"approximate"``.
    """
    if alignment:
        return (
            [
                {"start": float(s["start"]), "end": float(s["end"]), "text": str(s["text"] or "")}
                for s in alignment
                if str(s.get("text") or "").strip()
            ],
            "alignment",
        )
    segments = []
    for scene in scenes:
        text = str(scene.get("caption_text") or "").strip()
        if not text:
            continue
        start = float(scene.get("start") or 0.0)
        end = float(scene.get("end") or 0.0)
        if end <= start:
            continue
        segments.append({"start": start, "end": end, "text": text})
    return segments, "approximate"


def validate_srt(text: str) -> list[str]:
    """Return structural problems; empty list means valid."""
    issues: list[str] = []
    if not text.strip():
        return ["subtitle file is empty"]
    blocks = [b for b in text.strip().split("\n\n") if b.strip()]
    expected_index = 1
    previous_start = -1.0
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            issues.append(f"block {expected_index}: fewer than 3 lines")
            continue
        if not lines[0].strip().isdigit() or int(lines[0]) != expected_index:
            issues.append(f"block {expected_index}: bad index line")
        if not _TIME_LINE_RE.match(lines[1].strip()):
            issues.append(f"block {expected_index}: malformed time line")
        else:
            start_str = lines[1].split("-->")[0].strip()
            start = _parse_srt_time(start_str)
            if start < previous_start - 0.001:
                issues.append(f"block {expected_index}: start time goes backwards")
            previous_start = start
        if not "".join(lines[2:]).strip():
            issues.append(f"block {expected_index}: empty text")
        expected_index += 1
    return issues


def _parse_srt_time(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def write_srt(text: str, dest) -> str:
    issues = validate_srt(text)
    if issues:
        raise CreatorError(
            ErrorCode.INTERNAL_ERROR,
            "Generated captions failed SRT validation: " + "; ".join(issues[:3]),
            stage="narration",
            next_action="Re-run creator_build_timeline; report this if it repeats.",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return text
