"""Integration tests: REAL ffmpeg/ffprobe render of a 9:16 video.

No mocks here — lavfi-generated source clips are rendered through the real
``render_project_video`` pipeline (segments -> concat -> narration mix) and the
result is validated with the real ffprobe + full decode test.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from facebook_creator_agent.core.errors import CreatorError

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    not FFMPEG or not FFPROBE, reason="ffmpeg and ffprobe are required"
)


def _timeline():
    from facebook_creator_agent.core.ids import utcnow
    from facebook_creator_agent.core.models import Timeline, TimelineScene

    return Timeline(
        project_id="intproj",
        version=1,
        created_at=utcnow(),
        aspect_ratio="9:16",
        width=360,
        height=640,
        fps=30,
        total_duration_seconds=2.0,
        scenes=[
            TimelineScene(
                index=0,
                scene_id="s01",
                source="assets/clip.mp4",
                kind="video",
                duration_seconds=2.0,
                asset_id="asset_0001",
                caption_text="Hello integration",
            )
        ],
    )


async def test_real_render_and_validate_9x16(tmp_path, media_library):
    from facebook_creator_agent.core.jobs import JobRegistry
    from facebook_creator_agent.media_pipeline.ffmpeg import FFmpegRunner, render_project_video
    from facebook_creator_agent.media_pipeline.validation import validate_render

    project_dir = tmp_path / "intproj"
    (project_dir / "assets").mkdir(parents=True)
    (project_dir / "narration").mkdir(parents=True)
    shutil.copyfile(media_library["clip"], project_dir / "assets" / "clip.mp4")
    shutil.copyfile(media_library["wav"], project_dir / "narration" / "narration.wav")

    runner = FFmpegRunner(ffmpeg_path=FFMPEG, crf=28, preset="ultrafast", timeout_seconds=180)
    job = JobRegistry().create("render", "intproj")

    result = await render_project_video(
        project_id="intproj",
        project_dir=project_dir,
        timeline=_timeline(),
        runner=runner,
        burn_captions=False,
        narration_path=project_dir / "narration" / "narration.wav",
        job=job,
    )

    final = project_dir / result["final_stable"]
    assert final.is_file() and final.stat().st_size > 0

    validation = await validate_render(
        final,
        ffprobe_path=FFPROBE,
        ffmpeg_path=FFMPEG,
        expected_width=360,
        expected_height=640,
        expected_min_duration=2.0,
        expected_max_duration=2.0,
        require_audio=True,
    )
    assert validation["ok"] is True
    probe = validation["probe"]
    assert probe["width"] == 360
    assert probe["height"] == 640
    assert 1.0 <= (probe["duration_seconds"] or 0.0) <= 3.5
    assert validation["decode"]["decode_ok"] is True
    assert "max_volume_db" in validation["audio_levels"]


async def test_validate_render_rejects_wrong_dimensions(tmp_path, media_library):
    from facebook_creator_agent.media_pipeline.validation import validate_render

    wrong = tmp_path / "wrong.mp4"
    shutil.copyfile(media_library["clip"], wrong)

    with pytest.raises(CreatorError) as excinfo:
        await validate_render(
            wrong,
            ffprobe_path=FFPROBE,
            ffmpeg_path=FFMPEG,
            expected_width=1080,
            expected_height=1920,
            expected_min_duration=2.0,
            expected_max_duration=2.0,
            require_audio=False,
        )
    assert excinfo.value.code == "RENDER_VALIDATION_FAILED"
    assert "dimensions" in excinfo.value.safe_explanation
