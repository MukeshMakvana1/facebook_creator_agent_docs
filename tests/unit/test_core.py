"""Unit tests: configuration, path jail, ids, error contract, state machine, redaction."""

from __future__ import annotations

import json

import pytest

from facebook_creator_agent.core.errors import CreatorError, ErrorCode, error_info
from facebook_creator_agent.core.ids import canonical_json, sha256_json, slugify, stable_hash
from facebook_creator_agent.core.paths import PathJail, safe_filename, validate_project_id
from facebook_creator_agent.core.state_machine import ProjectStatus, StateMachine
from facebook_creator_agent.observability.logging import (
    REDACTED,
    clear_registered_secrets,
    redact_text,
    register_secret,
)

# --------------------------------------------------------------------- config


def test_settings_defaults_and_env_override(settings_factory):
    settings = settings_factory()
    assert "fixture" in settings.media.allowed_providers
    assert settings.render.default_fps > 0

    with_live = settings_factory(env={"FCA_ENABLE_LIVE_PUBLISH": "true"})
    assert with_live.facebook.live_publish_enabled is True

    without_live = settings_factory(env={"FCA_ENABLE_LIVE_PUBLISH": "0"})
    assert without_live.facebook.live_publish_enabled is False


def test_settings_invalid_value_raises_config_invalid(settings_factory):
    with pytest.raises(CreatorError) as excinfo:
        settings_factory(env={"FCA_RENDER_MAX_DURATION_SECONDS": "not-a-number"})
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID.value


def test_settings_redact_secrets(settings_factory):
    token = "EAAG" + "Z" * 30
    settings = settings_factory(env={"FCA_FACEBOOK_ACCESS_TOKEN": token})
    dumped = json.dumps(settings.redacted_dump())
    assert token not in dumped


def test_dotenv_file_is_loaded_from_base_dir(tmp_path, monkeypatch):
    from facebook_creator_agent.config.settings import load_settings

    # `.env` participates only in the real-process path (env=None); an explicit
    # env mapping is treated as fully hermetic. This mirrors server startup.
    monkeypatch.delenv("FCA_LLM_MODEL", raising=False)
    (tmp_path / ".env").write_text("FCA_LLM_MODEL=dotenv-model\n", encoding="utf-8")
    settings = load_settings(base_dir=tmp_path)
    assert settings.llm.model == "dotenv-model"


# ------------------------------------------------------------------ path jail


def test_validate_project_id_accepts_simple_ids():
    assert validate_project_id("my-video-01") == "my-video-01"


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "..", ""])
def test_validate_project_id_rejects_separators_and_traversal(bad):
    with pytest.raises(CreatorError):
        validate_project_id(bad)


def test_path_jail_blocks_escapes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    jail = PathJail(root)

    inside = jail.join("a", "b.txt")
    assert jail.contains(inside.resolve())

    with pytest.raises(CreatorError):
        jail.check_inside((root.parent / "outside.txt").resolve())


def test_safe_filename_strips_path_parts():
    name = safe_filename("../../etc/passwd")
    assert name and "/" not in name and "\\" not in name
    assert safe_filename("") == "file"


# ------------------------------------------------------------------------- ids


def test_stable_hash_is_deterministic_and_order_sensitive():
    assert stable_hash("a", "b") == stable_hash("a", "b")
    assert stable_hash("a", "b") != stable_hash("b", "a")
    assert len(stable_hash("a", "b")) == 8


def test_canonical_json_ignores_key_order():
    assert canonical_json({"b": 1, "a": [2, 3]}) == canonical_json({"a": [2, 3], "b": 1})
    assert sha256_json({"b": 1, "a": 2}) == sha256_json({"a": 2, "b": 1})


def test_slugify_produces_safe_slug():
    slug = slugify("Hello, World! — em dash")
    assert slug.startswith("hello")
    assert all(ch.isalnum() or ch == "-" for ch in slug)


# --------------------------------------------------------------- error contract


def test_error_five_field_contract():
    error = CreatorError(
        ErrorCode.INVALID_INPUT, "bad topic", stage="setup", next_action="Provide a topic."
    )
    info = error.to_info()
    assert set(info.model_dump()) == {"code", "safe_explanation", "retryable", "stage", "next_action"}
    assert info.code == "INVALID_INPUT"
    assert info.retryable is False
    assert info.stage == "setup"


def test_error_unknown_stage_falls_back_to_general():
    error = CreatorError("INVALID_INPUT", "x", stage="not-a-stage")
    assert error.stage == "general"


def test_error_info_helper_shapes():
    info = error_info(ErrorCode.NOT_FOUND, "missing", stage="media", retryable=True)
    assert info.code == "NOT_FOUND" and info.retryable is True


# ---------------------------------------------------------------- state machine


def test_state_machine_valid_transitions():
    StateMachine.ensure(ProjectStatus.NEW, ProjectStatus.SCRIPT_READY)
    StateMachine.ensure(ProjectStatus.NARRATION_READY, ProjectStatus.RENDERED)
    StateMachine.ensure("APPROVED", "PUBLISHING")
    # documented additions
    StateMachine.ensure(ProjectStatus.RENDERED, ProjectStatus.RENDERED)
    StateMachine.ensure(ProjectStatus.APPROVED, ProjectStatus.RENDERED)
    StateMachine.ensure(ProjectStatus.FAILED, ProjectStatus.PUBLISHED)
    StateMachine.ensure(ProjectStatus.FAILED, ProjectStatus.SCRIPT_READY)


@pytest.mark.parametrize(
    "src,dst",
    [
        (ProjectStatus.NEW, ProjectStatus.PUBLISHED),
        (ProjectStatus.NEW, ProjectStatus.RENDERED),
        (ProjectStatus.PUBLISHED, ProjectStatus.PUBLISHING),
        (ProjectStatus.CANCELLED, ProjectStatus.NEW),
        (ProjectStatus.SCRIPT_READY, ProjectStatus.PUBLISHING),
    ],
)
def test_state_machine_invalid_transitions(src, dst):
    with pytest.raises(CreatorError) as excinfo:
        StateMachine.ensure(src, dst)
    assert excinfo.value.code == ErrorCode.INVALID_STATE.value


def test_cancellable_states():
    assert StateMachine.is_cancellable(ProjectStatus.NEW) is True
    assert StateMachine.is_cancellable(ProjectStatus.PUBLISHED) is False


# ------------------------------------------------------------------- redaction


def test_redact_text_masks_tokens_and_keys():
    assert REDACTED in redact_text("access_token=abc1234def")
    assert REDACTED in redact_text("Authorization: Bearer abcdef12345678xyz")
    assert REDACTED in redact_text("token EAA" + "A" * 25)
    assert REDACTED in redact_text("key sk-" + "a" * 24)
    assert redact_text("nothing secret here") == "nothing secret here"


def test_registered_secret_exact_match():
    register_secret("supersecretvalue")
    try:
        assert "supersecretvalue" not in redact_text("around supersecretvalue end")
    finally:
        clear_registered_secrets()
