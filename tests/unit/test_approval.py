"""Unit tests for the publish approval guard (FR-065/066).

These tests exercise the REAL guard against a real project store. No mock
shortcuts: expiry, hash binding, page binding, single-use consumption, and the
audit log are all enforced by the implementation under test.
"""

from __future__ import annotations

import pytest

from facebook_creator_agent.core.errors import CreatorError
from facebook_creator_agent.publishing.approval import (
    APPROVAL_LOG_PATH,
    APPROVAL_PATH,
    ApprovalGuard,
)
from facebook_creator_agent.workspace.project_store import ProjectStore

MEDIA_SHA = "a" * 64
PAYLOAD_SHA = "b" * 64


@pytest.fixture()
def ctx(settings_factory):
    settings = settings_factory()
    store = ProjectStore(settings)
    project, _created = store.create(
        topic="Approval guard test project",
        language="en",
        target_duration_seconds=30,
        aspect_ratio="9:16",
        tone=None,
        audience=None,
        brand_instructions=None,
        project_id=None,
    )
    return store, ApprovalGuard(store), project


def _create(guard: ApprovalGuard, project):
    return guard.create(
        project_id=project.project_id,
        page_id="P1",
        media_sha256=MEDIA_SHA,
        payload_sha256=PAYLOAD_SHA,
        payload_snapshot={"caption": "hello"},
    )


def _verify(guard: ApprovalGuard, project, **overrides):
    kwargs = {
        "project_id": project.project_id,
        "page_id": "P1",
        "media_sha256": MEDIA_SHA,
        "payload_sha256": PAYLOAD_SHA,
    }
    kwargs.update(overrides)
    return guard.verify(**kwargs)


def test_create_verify_consume_roundtrip_with_audit_log(ctx):
    store, guard, project = ctx
    approval = _create(guard, project)

    verified = _verify(guard, project)
    assert verified.approval_id == approval.approval_id
    assert store.exists(project.project_id, APPROVAL_PATH)
    assert store.exists(project.project_id, APPROVAL_LOG_PATH)

    consumed = guard.consume(verified)
    assert consumed.consumed_at is not None

    log = store.read_text(project.project_id, APPROVAL_LOG_PATH)
    assert '"event": "created"' in log
    assert '"event": "consumed"' in log


def test_verify_without_approval_requires_approval(ctx):
    _store, guard, project = ctx
    with pytest.raises(CreatorError) as excinfo:
        _verify(guard, project)
    assert excinfo.value.code == "PUBLISH_APPROVAL_REQUIRED"
    assert excinfo.value.stage == "publish"


def test_payload_change_invalidates_approval(ctx):
    _store, guard, project = ctx
    _create(guard, project)
    with pytest.raises(CreatorError) as excinfo:
        _verify(guard, project, payload_sha256="c" * 64)
    assert excinfo.value.code == "PUBLISH_APPROVAL_STALE"
    assert "payload" in excinfo.value.safe_explanation.lower()


def test_media_change_invalidates_approval(ctx):
    _store, guard, project = ctx
    _create(guard, project)
    with pytest.raises(CreatorError) as excinfo:
        _verify(guard, project, media_sha256="d" * 64)
    assert excinfo.value.code == "PUBLISH_APPROVAL_STALE"
    assert "media" in excinfo.value.safe_explanation.lower()


def test_page_mismatch_invalidates_approval(ctx):
    _store, guard, project = ctx
    _create(guard, project)
    with pytest.raises(CreatorError) as excinfo:
        _verify(guard, project, page_id="OTHER_PAGE")
    assert excinfo.value.code == "PUBLISH_APPROVAL_STALE"


def test_expired_approval_is_rejected(ctx):
    store, guard, project = ctx
    _create(guard, project)
    document = store.read_json(project.project_id, APPROVAL_PATH)
    document["expires_at"] = "2020-01-01T00:00:00+00:00"
    store.write_json(project.project_id, APPROVAL_PATH, document)

    with pytest.raises(CreatorError) as excinfo:
        _verify(guard, project)
    assert excinfo.value.code == "PUBLISH_APPROVAL_STALE"
    assert "expired" in excinfo.value.safe_explanation.lower()


def test_consumed_approval_cannot_be_replayed(ctx):
    _store, guard, project = ctx
    approval = _create(guard, project)
    guard.consume(approval)

    with pytest.raises(CreatorError) as excinfo:
        _verify(guard, project)
    assert excinfo.value.code == "PUBLISH_APPROVAL_STALE"
    assert "consumed" in excinfo.value.safe_explanation.lower()

    with pytest.raises(CreatorError):
        guard.consume(approval)
