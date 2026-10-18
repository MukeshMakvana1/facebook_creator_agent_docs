"""FFmpeg render engine: deterministic EDL-to-MP4 pipeline with argument
arrays only (ARCHITECTURE.md §4.7, PRD FR-050..FR-057, NFR-006).

Pipeline (two encode passes + stream copies, which keeps it deterministic and
idempotent per timeline version):

1. per-scene segments: trim, scale/crop (cover) or scale/pad (contain) to the
   aspect preset, constant fps, H.264 — cached per timeline version;
2. concat of segments with the concat demuxer (stream copy);
3. audio stage: narration (optional +music mix with gain controls) mixed to
   AAC and padded to the exact target duration;
4. optional subtitle burn-in (final encode) or sidecar SRT;
5. stable-path stream copies (``renders/final.mp4``, ``renders/preview.mp4``)
   and an optional cover frame.

Every subprocess: argv list (never a shell), timeout kill, cancellation via
the owning job, progress reporting through ``-progress pipe:2``, and
sanitized (redacted, bounded) log tails.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from ..core.errors import CreatorError, ErrorCode
from ..core.jobs import Job
from ..core.models import Timeline, TimelineScene
from ..observability.logging import get_logger, redact_text

logger = get_logger("media_pipeline.ffmpeg")

ASPECT_PRESETS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}

_OUT_TIME_RE = re.compile(r"out_time_ms=(\d+)")


class FFmpegRunner:
    """Thin, safe wrapper around one ffmpeg binary."""

    def __init__(
        self,
        *,
        ffmpeg_path: str = "ffmpeg",
        crf: int = 20,
        preset: str = "veryfast",
        timeout_seconds: float = 600.0,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.crf = crf
        self.preset = preset
        self.timeout_seconds = timeout_seconds

    async def run(
        self,
        args: list[str],
        *,
        job: Optional[Job] = None,
        timeout: Optional[float] = None,
        cwd: Optional[Path] = None,
        total_seconds_hint: Optional[float] = None,
        label: str = "ffmpeg",
    ) -> None:
        """Run ffmpeg with progress parsing, timeout and cancellation."""
        full_args = [self.ffmpeg_path, "-hide_banner", "-nostdin", *args]
        timeout = timeout if timeout is not None else self.timeout_seconds
        logger.debug("%s: starting (%d args)", label, len(full_args))

        try:
            proc = await asyncio.create_subprocess_exec(
                *full_args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
            )
        except FileNotFoundError:
            raise CreatorError(
                ErrorCode.DEPENDENCY_MISSING,
                f"ffmpeg executable not found: {self.ffmpeg_path!r}.",
                stage="render",
                next_action="Install FFmpeg or set FCA_FFMPEG_PATH to the ffmpeg executable.",
            )
        if job is not None:
            job.process = proc
            job.progress = f"{label}: started"

        tail: list[str] = []

        async def _consume_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                match = _OUT_TIME_RE.search(text)
                if match and job is not None:
                    done_s = int(match.group(1)) / 1_000_000
                    if total_seconds_hint and total_seconds_hint > 0:
                        pct = min(99, int(done_s / total_seconds_hint * 100))
                        job.progress = f"{label}: {pct}%"
                    else:
                        job.progress = f"{label}: {done_s:.1f}s"
                else:
                    tail.append(redact_text(text))
                    if len(tail) > 40:
                        tail.pop(0)

        reader = asyncio.create_task(_consume_stderr())
        timed_out = False
        try:
            try:
                return_code = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                await proc.wait()
                return_code = proc.returncode or -1
        finally:
            try:
                await asyncio.wait_for(reader, timeout=10)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - reader cleanup
                reader.cancel()
            if job is not None and job.process is proc:
                job.process = None

        if timed_out:
            raise CreatorError(
                ErrorCode.FFMPEG_FAILED,
                f"{label} timed out after {timeout:.0f}s and was terminated.",
                stage="render",
                retryable=True,
                next_action="Retry; if the render is genuinely slow, raise FCA_RENDER_TIMEOUT_SECONDS.",
            )
        if job is not None and job.cancelled():
            raise CreatorError(
                ErrorCode.CANCELLED, f"{label} was cancelled.", stage="render"
            )
        if return_code != 0:
            stderr_tail = redact_text("\n".join(tail))[-600:]
            raise CreatorError(
                ErrorCode.FFMPEG_FAILED,
                f"{label} failed with exit code {return_code}. {stderr_tail}".strip(),
                stage="render",
                next_action="Inspect workspace logs; verify inputs with creator_validate_render.",
            )
        if job is not None:
            job.progress = f"{label}: done"


# --------------------------------------------------------------------------- #
# Command builders (pure functions — unit tested without ffmpeg)
# --------------------------------------------------------------------------- #

def scene_filter(width: int, height: int, fps: int, fit: str) -> str:
    """Scale/crop (cover) or scale/pad (contain) into the target frame."""
    if fit == "contain":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps}"
    )


def segment_args(
    scene: TimelineScene,
    source: Path,
    out: Path,
    *,
    width: int,
    height: int,
    fps: int,
    crf: int,
    preset: str,
) -> list[str]:
    duration = max(0.5, float(scene.duration_seconds))
    fit = "contain" if scene.kind == "image" else "cover"
    vf = scene_filter(width, height, fps, fit)
    base = ["-y", "-v", "error", "-progress", "pipe:2", "-nostats"]
    if scene.kind == "image":
        return [
            *base,
            "-loop", "1",
            "-i", str(source),
            "-t", f"{duration:.3f}",
            "-vf", vf,
            "-an",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            str(out),
        ]
    args = [
        *base,
        "-i", str(source),
    ]
    if scene.in_seconds and scene.in_seconds > 0:
        args += ["-ss", f"{float(scene.in_seconds):.3f}"]
    args += [
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-an",
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    return args


def concat_list_file(segments: list[Path]) -> str:
    lines = []
    for seg in segments:
        posix = str(seg.resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{posix}'")
    return "\n".join(lines) + "\n"


def audio_mix_args(
    video_only: Path,
    out: Path,
    *,
    total_seconds: float,
    narration: Optional[Path],
    narration_gain_db: float,
    music: Optional[Path],
    music_gain_db: float,
) -> Optional[list[str]]:
    """Build the audio stage command; returns None when there is no audio."""
    if narration is None and music is None:
        return None
    args = ["-y", "-v", "error", "-progress", "pipe:2", "-nostats", "-i", str(video_only)]
    if narration is not None and music is not None:
        args += ["-i", str(narration)]
        args += ["-stream_loop", "-1", "-i", str(music)]
        filter_complex = (
            f"[1:a]volume={narration_gain_db}dB[n];"
            f"[2:a]volume={music_gain_db}dB[m];"
            f"[n][m]amix=inputs=2:duration=first:dropout_transition=2:normalize=0,"
            f"aresample=48000,apad[a]"
        )
    elif narration is not None:
        args += ["-i", str(narration)]
        filter_complex = f"[1:a]volume={narration_gain_db}dB,aresample=48000,apad[a]"
    else:
        args += ["-stream_loop", "-1", "-i", str(music)]
        filter_complex = f"[1:a]volume={music_gain_db}dB,aresample=48000,apad[a]"
    args += [
        "-filter_complex", filter_complex,
        "-map", "0:v:0",
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-t", f"{total_seconds:.3f}",
        "-movflags", "+faststart",
        str(out),
    ]
    return args


def subtitle_args(
    source: Path,
    srt_relpath: str,
    out: Path,
    *,
    crf: int,
    preset: str,
) -> list[str]:
    """Burn-in command; ``srt_relpath`` must be relative to the cwd used."""
    return [
        "-y", "-v", "error", "-progress", "pipe:2", "-nostats",
        "-i", str(source),
        "-vf", f"subtitles={srt_relpath}",
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out),
    ]


def _place(source: Path, dest: Path) -> None:
    """Hardlink when possible (same volume), else copy. Never leaves partials."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        import os

        os.link(source, dest)
    except OSError:
        shutil.copyfile(source, dest)


def _check_cancel(job: Optional[Job]) -> None:
    if job is not None and job.cancelled():
        raise CreatorError(ErrorCode.CANCELLED, "Render cancelled.", stage="render")


async def render_project_video(
    *,
    project_id: str,
    project_dir: Path,
    timeline: Timeline,
    runner: FFmpegRunner,
    ffmpeg_enabled: bool = True,
    burn_captions: bool = True,
    narration_path: Optional[Path] = None,
    narration_gain_db: float = 0.0,
    music_path: Optional[Path] = None,
    music_gain_db: float = -18.0,
    job: Optional[Job] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Render the timeline into ``renders/v{N}/final.mp4`` + stable copies.

    Returns paths relative to the project dir (posix) plus render metadata.
    """
    if not ffmpeg_enabled:  # pragma: no cover - defensive
        raise CreatorError(ErrorCode.DEPENDENCY_MISSING, "ffmpeg is disabled.", stage="render")

    version = max(1, int(timeline.version))
    renders = project_dir / "renders"
    vdir = renders / f"v{version}"
    seg_dir = vdir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    total_seconds = float(timeline.total_duration_seconds)
    width, height = int(timeline.width), int(timeline.height)
    fps = int(timeline.fps)

    def rel(p: Path) -> str:
        return p.resolve().relative_to(project_dir.resolve()).as_posix()

    # 1. segments (cached per version unless forced)
    segment_paths: list[Path] = []
    for index, scene in enumerate(timeline.scenes):
        _check_cancel(job)
        source = (project_dir / scene.source).resolve()
        if not source.is_file():
            raise CreatorError(
                ErrorCode.NOT_FOUND,
                f"Timeline source missing for scene {scene.scene_id!r}: {scene.source}",
                stage="render",
                next_action="Re-run creator_download_assets, then creator_build_timeline.",
            )
        seg_path = seg_dir / f"seg_{index:03d}.mp4"
        if seg_path.is_file() and seg_path.stat().st_size > 0 and not force:
            segment_paths.append(seg_path)
            continue
        args = segment_args(
            scene, source, seg_path,
            width=width, height=height, fps=fps, crf=runner.crf, preset=runner.preset,
        )
        if job is not None:
            job.progress = f"segment {index + 1}/{len(timeline.scenes)}"
        await runner.run(
            args,
            job=job,
            total_seconds_hint=scene.duration_seconds,
            label=f"segment {index + 1}/{len(timeline.scenes)}",
        )
        if not seg_path.is_file() or seg_path.stat().st_size == 0:
            raise CreatorError(
                ErrorCode.FFMPEG_FAILED,
                f"Segment encode produced no output for scene {scene.scene_id!r}.",
                stage="render",
                retryable=True,
            )
        segment_paths.append(seg_path)

    # 2. concat (stream copy)
    concat_file = project_dir / "timeline" / f"segments_v{version}.txt"
    concat_file.parent.mkdir(parents=True, exist_ok=True)
    concat_file.write_text(concat_list_file(segment_paths), encoding="utf-8")
    video_only = vdir / "video_only.mp4"
    if not (video_only.is_file() and video_only.stat().st_size > 0 and not force):
        await runner.run(
            [
                "-y", "-v", "error", "-progress", "pipe:2", "-nostats",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                "-movflags", "+faststart",
                str(video_only),
            ],
            job=job,
            total_seconds_hint=total_seconds,
            label="concat",
        )

    # 3. audio stage
    audio_stage: Path = video_only
    mix_args = audio_mix_args(
        video_only,
        vdir / "mux_input.mp4",
        total_seconds=total_seconds,
        narration=narration_path,
        narration_gain_db=narration_gain_db,
        music=music_path,
        music_gain_db=music_gain_db,
    )
    if mix_args is not None:
        if not ((vdir / "mux_input.mp4").is_file() and not force):
            await runner.run(
                mix_args, job=job, total_seconds_hint=total_seconds, label="audio mix"
            )
        audio_stage = vdir / "mux_input.mp4"

    # 4. subtitle burn-in (optional) into the versioned final
    versioned_final = vdir / "final.mp4"
    srt = None
    if timeline.captions and timeline.captions.get("srt_path"):
        candidate = (project_dir / str(timeline.captions["srt_path"])).resolve()
        if candidate.is_file():
            srt = candidate
    became = False
    if burn_captions and srt is not None:
        srt_rel = rel(srt)
        await runner.run(
            subtitle_args(audio_stage, srt_rel, versioned_final, crf=runner.crf, preset=runner.preset),
            job=job,
            timeout=max(runner.timeout_seconds, 300.0),
            total_seconds_hint=total_seconds,
            cwd=project_dir,
            label="subtitle burn-in",
        )
        became = True
    else:
        _place(audio_stage, versioned_final)
        became = False
    if not versioned_final.is_file() or versioned_final.stat().st_size == 0:
        raise CreatorError(
            ErrorCode.FFMPEG_FAILED, "Final render produced no output file.", stage="render"
        )

    # 5. stable-path copies + cover
    final_stable = renders / "final.mp4"
    preview_stable = renders / "preview.mp4"
    _place(versioned_final, final_stable)
    _place(versioned_final, preview_stable)

    cover_rel: Optional[str] = None
    try:
        cover = renders / "cover.jpg"
        await runner.run(
            [
                "-y", "-v", "error",
                "-ss", "0.3",
                "-i", str(versioned_final),
                "-frames:v", "1",
                "-q:v", "3",
                str(cover),
            ],
            job=None,
            timeout=60.0,
            label="cover frame",
        )
        if cover.is_file() and cover.stat().st_size > 0:
            cover_rel = rel(cover)
    except CreatorError as exc:
        logger.info("cover extraction skipped: %s", exc.safe_explanation)

    return {
        "render_version": version,
        "segment_count": len(segment_paths),
        "video_only": rel(video_only),
        "final": rel(versioned_final),
        "final_stable": rel(final_stable),
        "preview_stable": rel(preview_stable),
        "cover": cover_rel,
        "subtitles_burned_in": became,
        "subtitle_mode": "burn-in" if became else ("sidecar" if srt is not None else "none"),
    }
