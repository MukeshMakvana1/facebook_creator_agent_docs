"""Deterministic timeline/EDL builder (FR-050, FR-053, T0702).

The timeline is the single source of truth the renderer consumes: per-scene
source file, trim window, final duration, aspect preset dimensions, narration
and caption metadata.  Scene durations are derived from the narration length
when narration exists (proportional to word counts), so audio and picture stay
in sync; otherwise the storyboard targets are used unchanged.
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.errors import CreatorError, ErrorCode
from ..core.ids import utcnow
from ..core.models import AssetRecord, Project, Scene, Storyboard, Timeline, TimelineScene
from .ffmpeg import ASPECT_PRESETS


def allocate_scene_seconds(scenes: list[Scene], total_seconds: float) -> list[float]:
    """Split ``total_seconds`` across scenes, proportional to word counts."""
    weights = [max(1, len((scene.narration or "").split())) for scene in scenes]
    total_weight = sum(weights) or 1
    durations = [total_seconds * w / total_weight for w in weights]
    # never below half a second per scene (caption sanity)
    return [max(0.5, d) for d in durations]


def scene_windows(timeline: Timeline) -> list[dict[str, Any]]:
    """Cumulative windows per scene, for the caption timing fallback."""
    windows: list[dict[str, Any]] = []
    cursor = 0.0
    for scene in timeline.scenes:
        start = cursor
        end = cursor + float(scene.duration_seconds)
        windows.append(
            {
                "scene_id": scene.scene_id,
                "caption_text": scene.caption_text or scene.on_screen_text or "",
                "start": start,
                "end": end,
            }
        )
        cursor = end
    return windows


def build_timeline(
    *,
    project: Project,
    storyboard: Storyboard,
    assets_by_scene: dict[str, AssetRecord],
    narration_duration: Optional[float],
    srt_relpath: Optional[str],
    burn_in: bool,
    timing_mode: str,
    fps: int,
    version: int,
    max_scenes: int = 60,
    max_duration_seconds: int = 180,
) -> Timeline:
    """Build and validate the timeline for the project's storyboard."""
    if len(storyboard.scenes) > max_scenes:
        raise CreatorError(
            ErrorCode.INVALID_INPUT,
            f"Storyboard has {len(storyboard.scenes)} scenes; the configured limit is {max_scenes}.",
            stage="timeline",
            next_action="Regenerate the script with fewer scenes or raise FCA_RENDER limits.",
        )

    missing = [s.scene_id for s in storyboard.scenes if s.scene_id not in assets_by_scene]
    if missing:
        raise CreatorError(
            ErrorCode.INVALID_STATE,
            f"Scenes without an approved asset: {', '.join(missing[:5])}.",
            stage="timeline",
            next_action="Run creator_download_assets for every scene before building the timeline.",
        )

    if narration_duration and narration_duration > 0:
        durations = allocate_scene_seconds(storyboard.scenes, float(narration_duration))
    else:
        durations = [max(0.5, float(s.target_seconds)) for s in storyboard.scenes]

    total = sum(durations)
    if total > max_duration_seconds:
        raise CreatorError(
            ErrorCode.INVALID_INPUT,
            f"Timeline would run {total:.1f}s, above the configured {max_duration_seconds}s limit.",
            stage="timeline",
            next_action="Shorten the script/narration or raise FCA_RENDER_MAX_DURATION_SECONDS.",
        )

    width, height = ASPECT_PRESETS[project.aspect_ratio]
    timeline_scenes: list[TimelineScene] = []
    for scene, duration in zip(storyboard.scenes, durations):
        asset = assets_by_scene[scene.scene_id]
        if not asset.local_path:
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                f"Asset {asset.asset_id!r} for scene {scene.scene_id!r} has no local file.",
                stage="timeline",
                next_action="Re-run creator_download_assets for this scene.",
            )
        timeline_scenes.append(
            TimelineScene(
                index=scene.index,
                scene_id=scene.scene_id,
                source=asset.local_path,
                kind=asset.kind,
                in_seconds=0.0,
                out_seconds=None,
                duration_seconds=round(duration, 3),
                asset_id=asset.asset_id,
                caption_text=(scene.narration or "").strip() or None,
                on_screen_text=scene.on_screen_text,
            )
        )

    captions: Optional[dict[str, Any]] = None
    if srt_relpath:
        captions = {"srt_path": srt_relpath, "burn_in": burn_in, "timing_mode": timing_mode}

    narration_info: Optional[dict[str, Any]] = None
    if narration_duration:
        narration_info = {"duration_seconds": round(float(narration_duration), 3)}

    return Timeline(
        project_id=project.project_id,
        version=version,
        created_at=utcnow(),
        aspect_ratio=project.aspect_ratio,
        width=width,
        height=height,
        fps=fps,
        total_duration_seconds=round(total, 3),
        scenes=timeline_scenes,
        narration=narration_info,
        music=None,
        captions=captions,
    )


def edl_dict(timeline: Timeline) -> dict[str, Any]:
    """JSON-serializable EDL document."""
    return timeline.model_dump(mode="json")
