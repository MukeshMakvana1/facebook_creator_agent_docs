"""Shared fixtures for the Facebook Creator Agent test suite.

Everything here is hermetic: settings are built from explicit environment
mappings against ``tmp_path`` workspaces, external providers are replaced by
fakes, and the only real subprocess work is ffmpeg/ffprobe on tiny lavfi
fixtures. The approval guard, the state machine, and the publisher are never
mocked — only the Facebook network adapter and the LLM are replaced.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = str(_VENV_PYTHON if _VENV_PYTHON.exists() else __import__("sys").executable)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

PAGE_ID = "1234567890"
FAKE_TOKEN = "fca-fake-token-not-a-real-secret"


def base_env(**overrides) -> dict:
    """Baseline hermetic environment for ``load_settings``."""
    env = {
        "FCA_LOG_LEVEL": "ERROR",
        "FCA_MEDIA_ALLOWED_PROVIDERS": "fixture",
        "FCA_FACEBOOK_PAGE_ID": PAGE_ID,
        "FCA_FACEBOOK_ACCESS_TOKEN": FAKE_TOKEN,
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return env


@pytest.fixture()
def settings_factory(tmp_path):
    """Return a factory building hermetic ``Settings`` rooted at ``tmp_path``."""
    from facebook_creator_agent.config.settings import load_settings

    def _make(**overrides):
        env = base_env(**overrides.pop("env", {}))
        return load_settings(base_dir=tmp_path, env=env)

    return _make


class FakeLLM:
    """Deterministic LLM adapter returning a valid ``ScriptDraft`` for any prompt."""

    provider_id = "fake-llm"

    def __init__(self, scenes=None, caption="A short caption for the test video"):
        self.scenes = scenes or [
            {
                "scene_id": "s01",
                "narration": "First scene: MCP servers give agents safe, typed tools.",
                "on_screen_text": "MCP servers",
                "visual_description": "abstract blue technology background",
                "media_query": "clip",
                "target_seconds": 4,
            },
            {
                "scene_id": "s02",
                "narration": "Second scene: every tool returns structured evidence and errors.",
                "on_screen_text": "Structured results",
                "visual_description": "abstract network nodes",
                "media_query": "clip",
                "target_seconds": 4,
            },
        ]
        self.caption = caption
        self.calls: list[dict] = []

    # ----- LLMAdapter interface
    @property
    def configured(self) -> bool:
        return True

    async def generate_json(self, system_prompt, user_prompt, schema_model, *, max_attempts=2):
        self.calls.append({"system": system_prompt, "user": user_prompt})
        return schema_model.model_validate(
            {
                "title": "Test Video About MCP",
                "locale": "en",
                "hook": "A deterministic test hook",
                "scenes": [dict(scene) for scene in self.scenes],
                "cta": "Follow for more",
                "caption": self.caption,
                "hashtags": ["testtag", "mcp"],
            }
        )

    async def test(self):
        return {"ok": True, "model": "fake-llm", "latency_ms": 1}

    def status(self):
        return {"provider": self.provider_id, "configured": True}


class FakeFacebookAdapter:
    """Facebook adapter that records uploads and never touches the network."""

    provider_id = "fake-facebook"

    def __init__(self, *, has_credentials: bool = True, remote_id: str = "vid_fake_0001"):
        self._has_credentials = has_credentials
        self.remote_id = remote_id
        self.upload_calls: list[dict] = []
        self.status_calls: list[str] = []

    @property
    def has_credentials(self) -> bool:
        return self._has_credentials

    async def check_token(self):
        from facebook_creator_agent.providers.facebook.base import TokenInfo

        return TokenInfo(id="1", name="Fake User")

    async def list_pages(self):
        from facebook_creator_agent.providers.facebook.base import PageInfo

        return [PageInfo(id=PAGE_ID, name="Test Page", tasks=["CREATE_CONTENT"], can_publish=True)]

    async def get_page(self, page_id):
        from facebook_creator_agent.providers.facebook.base import PageInfo

        return PageInfo(id=str(page_id), name="Test Page", tasks=["CREATE_CONTENT"], can_publish=True)

    async def upload_video(self, page_id, video_path, payload, *, timeout_seconds):
        from facebook_creator_agent.providers.facebook.base import UploadResult

        self.upload_calls.append(
            {
                "page_id": str(page_id),
                "video_path": str(video_path),
                "caption": payload.get("caption"),
                "timeout_seconds": timeout_seconds,
            }
        )
        return UploadResult(remote_id=self.remote_id, response={"id": self.remote_id})

    async def get_video_status(self, video_id):
        self.status_calls.append(str(video_id))
        return {"exists": True, "status": "ready"}

    def status(self):
        return {"provider": self.provider_id, "credentials_present": self.has_credentials}


@pytest.fixture()
def fake_llm():
    return FakeLLM()


@pytest.fixture()
def fake_facebook():
    return FakeFacebookAdapter()


@pytest.fixture()
def service_factory(settings_factory, fake_llm, fake_facebook):
    """CreatorService wired to the fake LLM/Facebook adapters.

    The approval guard, publish journal, publisher, state machine, and media
    pipeline remain the real implementations.
    """
    from facebook_creator_agent.workflow.orchestrator import CreatorService

    def _make(**overrides):
        settings = overrides.pop("settings", None) or settings_factory()
        return CreatorService(
            settings,
            llm=overrides.pop("llm", fake_llm),
            facebook=overrides.pop("facebook", fake_facebook),
        )

    return _make


@pytest.fixture(scope="session")
def media_library(tmp_path_factory):
    """Tiny real media generated once with ffmpeg lavfi (video, audio, image)."""
    if not FFMPEG:
        pytest.skip("ffmpeg is required for media fixtures")
    root = tmp_path_factory.mktemp("fca-media")

    clip = root / "clip.mp4"
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            str(clip),
        ],
        check=True,
    )

    wav = root / "narration.wav"
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=330:duration=4",
            "-c:a", "pcm_s16le",
            str(wav),
        ],
        check=True,
    )

    image = root / "still.png"
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=steelblue:size=360x640:rate=30:duration=1",
            "-frames:v", "1",
            str(image),
        ],
        check=True,
    )

    return {"root": root, "clip": clip, "wav": wav, "image": image}
