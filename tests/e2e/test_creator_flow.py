"""End-to-end tests: the full creator workflow over the real MCP tool surface.

The MCP server is built in-process around a ``CreatorService`` whose LLM and
Facebook *network* adapters are fakes. Everything else is real: state machine,
project store, fixture media intake, manual narration, timeline, ffmpeg
render, validation, review package, the approval guard, and the publisher
(dry-run and live-gated paths). No test bypasses or weakens the guard.
"""

from __future__ import annotations

import shutil

import pytest

from facebook_creator_agent.core.errors import CreatorError  # noqa: F401  (imported for parity)

FFMPEG = shutil.which("ffmpeg")


def _structured(result):
    return getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )


async def call(server, name, **arguments):
    result = await server.call_tool(name, arguments)
    envelope = _structured(result)
    assert isinstance(envelope, dict), f"{name}: no structured content"
    return envelope


async def _run_pipeline_to_review(server, media_library, *, topic: str) -> str:
    """create -> script -> media -> assets -> narration -> timeline -> render -> review."""
    created = await call(
        server, "creator_create_project", topic=topic, target_duration_seconds=8
    )
    assert created["status"] == "ok"
    project_id = created["project_id"]
    assert created["data"]["project_status"] == "NEW"

    script = await call(server, "creator_generate_script", project_id=project_id)
    assert script["status"] == "ok"
    assert script["data"]["project_status"] == "SCRIPT_READY"

    search = await call(server, "creator_search_media", project_id=project_id)
    assert search["status"] == "ok"
    scenes = search["data"]["scenes"]
    assert scenes
    candidate_keys = [
        candidate["candidate_key"]
        for scene in scenes
        for candidate in scene["candidates"]
        if candidate["rights_status"] == "APPROVED"
    ]
    assert candidate_keys, "no APPROVED candidate from the fixture provider"
    candidate_key = candidate_keys[0]

    downloaded = await call(
        server,
        "creator_download_assets",
        project_id=project_id,
        selections=[
            {"scene_id": scene["scene_id"], "candidate_key": candidate_key}
            for scene in scenes
        ],
    )
    assert downloaded["status"] == "ok"
    assert downloaded["data"]["project_status"] == "ASSETS_READY"
    assert len(downloaded["data"]["scene_assets"]) == len(scenes)

    narration = await call(
        server,
        "creator_generate_narration",
        project_id=project_id,
        narration_path=str(media_library["wav"]),
    )
    assert narration["status"] == "ok"
    assert narration["data"]["narration"]["provider"] == "manual"
    assert narration["data"]["project_status"] == "NARRATION_READY"

    timeline = await call(server, "creator_build_timeline", project_id=project_id)
    assert timeline["status"] == "ok"
    assert timeline["artifacts"]["timeline"].endswith("edit_decision_list.json")

    rendered = await call(server, "creator_render_preview", project_id=project_id)
    assert rendered["status"] == "ok"
    assert rendered["data"]["project_status"] == "RENDERED"
    assert rendered["data"]["validation"]["ok"] is True

    validated = await call(server, "creator_validate_render", project_id=project_id)
    assert validated["status"] == "ok"
    assert validated["data"]["ok"] is True

    review = await call(server, "creator_get_review_package", project_id=project_id)
    assert review["status"] == "ok"
    assert review["data"]["project_status"] == "REVIEW_REQUIRED"
    return project_id


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is required")
async def test_full_flow_with_mocked_publish(settings_factory, service_factory, fake_facebook, media_library):
    from facebook_creator_agent.mcp_server.server import build_server

    settings = settings_factory(
        env={
            "FCA_FIXTURE_MEDIA_DIR": str(media_library["root"]),
            "FCA_ENABLE_LIVE_PUBLISH": "1",
        }
    )
    service = service_factory(settings=settings)
    server = build_server(service=service)

    project_id = await _run_pipeline_to_review(server, media_library, topic="MCP servers explained quickly")

    approved = await call(server, "creator_approve_publish", project_id=project_id)
    assert approved["status"] == "ok"
    assert approved["data"]["project_status"] == "APPROVED"
    assert approved["data"]["page_id"] == "1234567890"

    # Dry run first: verified but never consumed, nothing uploaded.
    dry = await call(server, "creator_publish_video", project_id=project_id, dry_run=True)
    assert dry["status"] == "ok"
    assert dry["data"]["mode"] == "dry_run"
    assert dry["data"]["network_call_performed"] is False
    assert fake_facebook.upload_calls == []

    # Live path against the fake adapter (approval guard is the real one).
    live = await call(server, "creator_publish_video", project_id=project_id, dry_run=False)
    assert live["status"] == "ok"
    assert live["data"]["mode"] == "live"
    assert live["data"]["project_status"] == "PUBLISHED"
    assert live["data"]["remote_video_id"] == "vid_fake_0001"
    assert len(fake_facebook.upload_calls) == 1

    # Same exact payload again: idempotent replay, no second upload.
    replay = await call(server, "creator_publish_video", project_id=project_id, dry_run=False)
    assert replay["status"] == "ok"
    assert replay["data"]["mode"] == "idempotent_replay"
    assert len(fake_facebook.upload_calls) == 1

    status = await call(server, "creator_get_publish_status", project_id=project_id)
    assert status["status"] == "ok"
    assert status["data"]["outcome"] == "SUCCEEDED"

    # The approval was consumed exactly once by the live publish.
    approval_doc = service.store.read_json(project_id, "review/approval.json")
    assert approval_doc["consumed_at"]
    project = service.store.load(project_id)
    assert project.status.value == "PUBLISHED"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is required")
async def test_publish_guard_negatives(settings_factory, service_factory, fake_facebook, media_library):
    from facebook_creator_agent.mcp_server.server import build_server

    settings = settings_factory(
        env={
            "FCA_FIXTURE_MEDIA_DIR": str(media_library["root"]),
            "FCA_ENABLE_LIVE_PUBLISH": "1",
        }
    )
    service = service_factory(settings=settings)
    server = build_server(service=service)

    project_id = await _run_pipeline_to_review(server, media_library, topic="Guard negative paths")

    # 1) No approval exists yet -> PUBLISH_APPROVAL_REQUIRED, nothing uploaded.
    refused = await call(server, "creator_publish_video", project_id=project_id)
    assert refused["status"] == "error"
    assert refused["error"]["code"] == "PUBLISH_APPROVAL_REQUIRED"
    assert fake_facebook.upload_calls == []

    # 2) Approval created, then removed -> the guard itself refuses.
    approved = await call(server, "creator_approve_publish", project_id=project_id)
    assert approved["data"]["project_status"] == "APPROVED"
    service.store.abs_path(project_id, "review/approval.json").unlink()

    refused_guard = await call(server, "creator_publish_video", project_id=project_id)
    assert refused_guard["status"] == "error"
    assert refused_guard["error"]["code"] == "PUBLISH_APPROVAL_REQUIRED"
    assert fake_facebook.upload_calls == []

    # 3) Fresh review + approval, then the caption changes -> STALE.
    #    (Re-render from APPROVED is the documented approval-invalidating edge;
    #    identical inputs are cached, so this reuses the existing render.)
    await call(server, "creator_render_preview", project_id=project_id)
    await call(server, "creator_get_review_package", project_id=project_id)
    re_approved = await call(server, "creator_approve_publish", project_id=project_id)
    assert re_approved["data"]["project_status"] == "APPROVED"

    payload = service.store.read_json(project_id, "review/publish_payload.json")
    payload["caption"] = payload["caption"] + " (changed after approval)"
    service.store.write_json(project_id, "review/publish_payload.json", payload)

    refused_stale = await call(server, "creator_publish_video", project_id=project_id)
    assert refused_stale["status"] == "error"
    assert refused_stale["error"]["code"] == "PUBLISH_APPROVAL_STALE"
    assert fake_facebook.upload_calls == []


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is required")
async def test_user_supplied_media_intake(settings_factory, service_factory, media_library):
    """User media intake: confirm_rights gate -> real intake (copy + hash) -> real render."""
    from facebook_creator_agent.mcp_server.server import build_server

    settings = settings_factory()
    service = service_factory(settings=settings)
    server = build_server(service=service)

    async def new_project(topic: str) -> tuple[str, list[str]]:
        created = await call(
            server, "creator_create_project", topic=topic, target_duration_seconds=8
        )
        assert created["status"] == "ok"
        project_id = created["project_id"]
        script = await call(server, "creator_generate_script", project_id=project_id)
        assert script["status"] == "ok"
        storyboard = service.store.read_json(project_id, "storyboard.json")
        return project_id, [scene["scene_id"] for scene in storyboard["scenes"]]

    # 1) No rights confirmation -> refused before anything is written.
    refusal_project, refusal_scenes = await new_project("User media refusal path")
    refused = await call(
        server,
        "creator_download_assets",
        project_id=refusal_project,
        user_assets=[
            {
                "scene_id": refusal_scenes[0],
                "path": str(media_library["clip"]),
                "confirm_rights": False,
            }
        ],
    )
    assert refused["status"] == "error"
    assert refused["error"]["code"] == "MEDIA_LICENSE_REJECTED"
    assert "confirm_rights" in refused["error"]["safe_explanation"]
    assert not service.store.exists(refusal_project, "assets/manifest.json")

    # 2) Explicit confirmation -> the user's file is copied in, hashed, recorded.
    project_id, scene_ids = await new_project("User supplied media intake")
    assert len(scene_ids) >= 2
    downloaded = await call(
        server,
        "creator_download_assets",
        project_id=project_id,
        user_assets=[
            {
                "scene_id": scene_id,
                "path": str(media_library["clip"]),
                "confirm_rights": True,
            }
            for scene_id in scene_ids
        ],
    )
    assert downloaded["status"] == "ok"
    assert downloaded["data"]["project_status"] == "ASSETS_READY"
    assert len(downloaded["data"]["scene_assets"]) == len(scene_ids)

    records = service.store.read_json(project_id, "assets/manifest.json")["assets"]
    assert len(records) == len(scene_ids)
    for record in records:
        assert record["provider"] == "user"
        assert record["license_name"] == "USER-SUPPLIED-CONFIRMED"
        assert record["rights_confirmed_by_user"] is True
        assert record["sha256"]
        assert record["local_path"].startswith("assets/clips/")

    # 3) The user's own file flows through the real pipeline to a validated render.
    narration = await call(
        server,
        "creator_generate_narration",
        project_id=project_id,
        narration_path=str(media_library["wav"]),
    )
    assert narration["status"] == "ok"

    timeline = await call(server, "creator_build_timeline", project_id=project_id)
    assert timeline["status"] == "ok"

    rendered = await call(server, "creator_render_preview", project_id=project_id)
    assert rendered["status"] == "ok"
    assert rendered["data"]["validation"]["ok"] is True

    validated = await call(server, "creator_validate_render", project_id=project_id)
    assert validated["status"] == "ok"
    assert validated["data"]["ok"] is True
