"""Configuration: environment + optional local JSON config + .env, validated.

Precedence (ARCHITECTURE.md §8): tool arguments > environment variables >
``.env`` file (dotenv subset, no interpolation) > ``config.local.json`` >
built-in defaults.

Secrets live only in environment variables (or the ignored ``.env`` file),
never in project files or manifests.  The JSON config file is for
non-secret settings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from pydantic import BaseModel, Field, SecretStr, ValidationError

from ..core.errors import CreatorError, ErrorCode

CONFIG_FILENAME = "config.local.json"
ENV_FILE_NAME = ".env"


class LLMSettings(BaseModel):
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[SecretStr] = None
    timeout_seconds: int = 90
    max_retries: int = 2
    temperature: float = 0.4


class TTSSettings(BaseModel):
    command_json: Optional[str] = None  # JSON array of argv tokens (no shell)
    output_extension: str = "wav"
    timeout_seconds: int = 120


class MediaSettings(BaseModel):
    allowed_providers: list[str] = Field(default_factory=lambda: ["fixture", "user"])
    allowed_licenses: list[str] = Field(default_factory=list)  # empty -> built-in allowlist
    max_download_bytes: int = 50 * 1024 * 1024
    max_redirects: int = 3
    download_timeout_seconds: int = 60
    max_assets_per_scene: int = 1
    pexels_api_key: Optional[SecretStr] = None


class FacebookSettings(BaseModel):
    access_token: Optional[SecretStr] = None
    page_id: Optional[str] = None
    graph_version: str = "v23.0"
    live_publish_enabled: bool = False
    graph_base_url: str = "https://graph.facebook.com"
    graph_video_base_url: str = "https://graph-video.facebook.com"
    upload_timeout_seconds: int = 300


class RenderSettings(BaseModel):
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    default_fps: int = 30
    max_duration_seconds: int = 180
    max_scenes: int = 60
    timeout_seconds: int = 600
    crf: int = 20
    preset: str = "veryfast"


class ResearchSettings(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[SecretStr] = None


class WorkspaceSettings(BaseModel):
    root: str = "workspace"


class Settings(BaseModel):
    schema_version: int = 1
    base_dir: Path
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    facebook: FacebookSettings = Field(default_factory=FacebookSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    fixture_dir: Optional[str] = None
    log_level: str = "INFO"

    @property
    def workspace_root(self) -> Path:
        root = Path(self.workspace.root)
        if not root.is_absolute():
            root = Path(self.base_dir) / root
        return root.resolve()

    def redacted_dump(self) -> dict[str, Any]:
        """Secret-free view for tools/logs."""
        data = self.model_dump(mode="json")
        for section in ("llm", "facebook", "research", "media"):
            block = data.get(section)
            if isinstance(block, dict):
                for key in list(block.keys()):
                    if "key" in key or "token" in key or "secret" in key:
                        block[key] = "SET" if block[key] else None
        data["base_dir"] = str(self.base_dir)
        data["workspace_root"] = str(self.workspace_root)
        return data


def parse_dotenv(path: Path) -> dict[str, str]:
    """Minimal dotenv subset: KEY=VALUE lines, # comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def env_overrides(env: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Map FCA_* environment variables onto nested settings fields."""
    out: dict[str, dict[str, Any]] = {}

    def put(section: str, field: str, value: Any) -> None:
        out.setdefault(section, {})[field] = value

    table: tuple[tuple[str, str, str, Any], ...] = (
        ("FCA_WORKSPACE_ROOT", "workspace", "root", str),
        ("FCA_LLM_BASE_URL", "llm", "base_url", str),
        ("FCA_LLM_MODEL", "llm", "model", str),
        ("FCA_LLM_API_KEY", "llm", "api_key", str),
        ("FCA_LLM_TIMEOUT_SECONDS", "llm", "timeout_seconds", int),
        ("FCA_LLM_MAX_RETRIES", "llm", "max_retries", int),
        ("FCA_TTS_COMMAND_JSON", "tts", "command_json", str),
        ("FCA_TTS_OUTPUT_EXTENSION", "tts", "output_extension", str),
        ("FCA_MEDIA_MAX_DOWNLOAD_MB", "media", "max_download_bytes", "mb"),
        ("FCA_MEDIA_MAX_REDIRECTS", "media", "max_redirects", int),
        ("FCA_PEXELS_API_KEY", "media", "pexels_api_key", str),
        ("FCA_FACEBOOK_ACCESS_TOKEN", "facebook", "access_token", str),
        ("FCA_FACEBOOK_PAGE_ID", "facebook", "page_id", str),
        ("FCA_FACEBOOK_GRAPH_VERSION", "facebook", "graph_version", str),
        ("FCA_ENABLE_LIVE_PUBLISH", "facebook", "live_publish_enabled", "bool"),
        ("FCA_FACEBOOK_GRAPH_BASE_URL", "facebook", "graph_base_url", str),
        ("FCA_FACEBOOK_GRAPH_VIDEO_BASE_URL", "facebook", "graph_video_base_url", str),
        ("FCA_FFMPEG_PATH", "render", "ffmpeg_path", str),
        ("FCA_FFPROBE_PATH", "render", "ffprobe_path", str),
        ("FCA_RENDER_FPS", "render", "default_fps", int),
        ("FCA_RENDER_MAX_DURATION_SECONDS", "render", "max_duration_seconds", int),
        ("FCA_RENDER_TIMEOUT_SECONDS", "render", "timeout_seconds", int),
        ("FCA_RESEARCH_BASE_URL", "research", "base_url", str),
        ("FCA_RESEARCH_API_KEY", "research", "api_key", str),
        ("FCA_LOG_LEVEL", "log_level", "", str),
        ("FCA_FIXTURE_MEDIA_DIR", "fixture_dir", "", str),
        ("FCA_MEDIA_ALLOWED_PROVIDERS", "media", "allowed_providers", "csv"),
        ("FCA_MEDIA_ALLOWED_LICENSES", "media", "allowed_licenses", "csv"),
    )
    for env_name, section, field, kind in table:
        if env_name not in env:
            continue
        raw = env[env_name]
        if kind is int:
            value: Any = int(raw)
        elif kind == "mb":
            value = int(float(raw) * 1024 * 1024)
        elif kind == "bool":
            value = _as_bool(raw)
        elif kind == "csv":
            value = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            value = raw
        if section == "log_level" or section == "fixture_dir":
            out.setdefault("_top", {})[section] = value
        else:
            put(section, field, value)
    return out


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    *,
    base_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    use_dotenv: bool = True,
) -> Settings:
    """Load and validate settings. Raises CONFIG_INVALID on hard errors."""
    effective_env: Mapping[str, str] = env if env is not None else os.environ
    base = Path(base_dir or effective_env.get("FCA_BASE_DIR") or Path.cwd()).resolve()
    if not base.exists():
        base = Path.cwd().resolve()

    file_data: dict[str, Any] = {}
    cfg_path = Path(config_path) if config_path else base / CONFIG_FILENAME
    if cfg_path.is_file():
        try:
            file_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(file_data, dict):
                raise ValueError("config file must contain a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            raise CreatorError(
                ErrorCode.CONFIG_INVALID,
                f"{cfg_path.name} is not valid JSON: {exc}",
                stage="setup",
                next_action="Fix or delete config.local.json and run creator_doctor again.",
            )

    overlay: dict[str, str] = {}
    if use_dotenv and env is None:
        overlay = parse_dotenv(base / ENV_FILE_NAME)
    merged_env: dict[str, str] = {**overlay, **{k: v for k, v in effective_env.items()}}

    try:
        overrides = env_overrides(merged_env)
    except ValueError as exc:
        raise CreatorError(
            ErrorCode.CONFIG_INVALID,
            f"Invalid environment value: {exc}",
            stage="setup",
            next_action="Check FCA_* variables (numbers must be numeric, flags 1/0).",
        )

    top: dict[str, Any] = dict(file_data)
    top["base_dir"] = base
    for section, fields in overrides.items():
        if section == "_top":
            top.update(fields)
            continue
        block = dict(top.get(section) or {})
        block.update(fields)
        top[section] = block

    try:
        settings = Settings.model_validate(top)
    except ValidationError as exc:
        raise CreatorError(
            ErrorCode.CONFIG_INVALID,
            f"Configuration failed validation: {exc.errors()[:3]}",
            stage="setup",
            next_action="Fix the listed fields in config.local.json or environment variables.",
        )

    # Register secrets for log redaction immediately.
    from ..observability.logging import register_secret

    for secret in (
        settings.llm.api_key,
        settings.facebook.access_token,
        settings.research.api_key,
        settings.media.pexels_api_key,
    ):
        if secret is not None:
            register_secret(secret.get_secret_value())

    return settings


def validate_settings(settings: Settings) -> list[dict[str, str]]:
    """Non-fatal validation report for doctor/config tools."""
    issues: list[dict[str, str]] = []

    def issue(field: str, severity: str, message: str) -> None:
        issues.append({"field": field, "severity": severity, "message": message})

    llm = settings.llm
    if llm.base_url and not (llm.base_url.startswith("http://") or llm.base_url.startswith("https://")):
        issue("llm.base_url", "error", "base_url must start with http:// or https://")
    if llm.base_url and not llm.model:
        issue("llm.model", "error", "model is required when llm.base_url is configured")
    if llm.model and not llm.base_url:
        issue("llm.base_url", "error", "base_url is required when llm.model is configured")
    if llm.timeout_seconds <= 0:
        issue("llm.timeout_seconds", "error", "timeout must be positive")

    if settings.tts.command_json:
        try:
            parsed = json.loads(settings.tts.command_json)
            if not isinstance(parsed, list) or not parsed or not all(isinstance(t, str) for t in parsed):
                issue("tts.command_json", "error", "must be a JSON array of strings (argv tokens)")
        except json.JSONDecodeError:
            issue("tts.command_json", "error", "must be valid JSON (argv token array)")

    if settings.facebook.access_token and not settings.facebook.page_id:
        issue("facebook.page_id", "warning", "access token set but no page_id; set FCA_FACEBOOK_PAGE_ID or choose a page at publish time")
    if settings.facebook.live_publish_enabled and not settings.facebook.access_token:
        issue(
            "facebook.access_token",
            "error",
            "FCA_ENABLE_LIVE_PUBLISH=1 requires FCA_FACEBOOK_ACCESS_TOKEN",
        )
    if not settings.facebook.graph_version.startswith("v"):
        issue("facebook.graph_version", "warning", "graph_version normally looks like 'v23.0'")

    if settings.render.default_fps <= 0 or settings.render.default_fps > 120:
        issue("render.default_fps", "error", "fps must be between 1 and 120")
    if settings.media.max_download_bytes <= 0:
        issue("media.max_download_bytes", "error", "max download size must be positive")
    if not settings.media.allowed_providers:
        issue("media.allowed_providers", "warning", "no providers allowed; media search will return nothing")

    return issues
