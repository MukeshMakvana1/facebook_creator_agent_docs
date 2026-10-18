"""Core data contracts (ARCHITECTURE.md §5, PRD §10)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .errors import ErrorInfo
from .state_machine import ProjectStatus

RightsStatus = Literal["APPROVED", "REVIEW_REQUIRED", "REJECTED"]
MediaKind = Literal["video", "image", "audio"]


class Scene(BaseModel):
    index: int
    scene_id: str
    narration: str
    on_screen_text: Optional[str] = None
    visual_description: Optional[str] = None
    media_query: str = ""
    target_seconds: float = 5.0
    selected_asset_ids: list[str] = Field(default_factory=list)


class StageRecord(BaseModel):
    stage: str
    status: Literal["started", "ok", "error", "cancelled", "cached"] 
    started_at: datetime
    finished_at: Optional[datetime] = None
    job_id: Optional[str] = None
    error: Optional[ErrorInfo] = None


class AssetRecord(BaseModel):
    """Provenance record for one asset (ARCHITECTURE.md §5, FR-031)."""

    asset_id: str
    provider: str
    provider_asset_id: Optional[str] = None
    title: Optional[str] = None
    kind: MediaKind = "video"
    source_url: Optional[str] = None
    download_url: Optional[str] = None
    license_name: Optional[str] = None
    license_url: Optional[str] = None
    attribution: Optional[str] = None
    retrieved_at: datetime
    intended_use: str = "social_media"
    scene_id: Optional[str] = None
    local_path: Optional[str] = None  # relative to project dir (posix)
    sha256: Optional[str] = None
    bytes: Optional[int] = None
    mime: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[float] = None
    rights_status: RightsStatus = "REVIEW_REQUIRED"
    rights_reasons: list[str] = Field(default_factory=list)
    rights_confirmed_by_user: bool = False
    user_confirmation_text: Optional[str] = None


class ScriptDraft(BaseModel):
    """Strict schema the LLM output must satisfy (FR-021, T0304)."""

    title: str
    locale: str = "en"
    hook: Optional[str] = None
    scenes: list[dict[str, Any]]
    cta: Optional[str] = None
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)

    @field_validator("scenes")
    @classmethod
    def _scenes_non_empty(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not value:
            raise ValueError("scenes must contain at least one scene")
        return value


class Storyboard(BaseModel):
    project_id: str
    script_version: int
    title: str
    locale: str
    hook: Optional[str] = None
    cta: Optional[str] = None
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    total_target_seconds: float
    scenes: list[Scene]
    research_status: Literal["available", "unavailable"] = "unavailable"
    unverified_claims: list[str] = Field(default_factory=list)
    generated_at: datetime


class PublishApproval(BaseModel):
    """Approval bound to exact payload (ARCHITECTURE.md §5, FR-065/066)."""

    approval_id: str
    project_id: str
    page_id: str
    action: Literal["PUBLISH_VIDEO"] = "PUBLISH_VIDEO"
    media_sha256: str
    payload_sha256: str
    created_at: datetime
    expires_at: datetime
    nonce: str
    consumed_at: Optional[datetime] = None
    confirmation_mode: Literal["elicitation", "caller", "cli"] = "caller"
    approved_payload_path: str = "review/publish_payload.json"


class PublishAttempt(BaseModel):
    attempt_id: str
    idempotency_key: str
    project_id: str
    page_id: str
    action: str = "PUBLISH_VIDEO"
    status: Literal["STARTED", "SUCCEEDED", "FAILED", "UNKNOWN", "DRY_RUN"]
    started_at: datetime
    finished_at: Optional[datetime] = None
    request_summary: dict[str, Any] = Field(default_factory=dict)
    response: Optional[dict[str, Any]] = None
    remote_id: Optional[str] = None
    reconciled: bool = False
    error: Optional[ErrorInfo] = None


class PublishState(BaseModel):
    last_outcome: Optional[str] = None
    attempts_file: str = "review/publish_attempts.json"


class Project(BaseModel):
    schema_version: int = 1
    project_id: str
    topic: str
    language: str = "en"
    tone: Optional[str] = None
    audience: Optional[str] = None
    target_duration_seconds: int = 45
    aspect_ratio: Literal["9:16", "16:9"] = "9:16"
    brand_instructions: Optional[str] = None
    status: ProjectStatus = ProjectStatus.NEW
    created_at: datetime
    updated_at: datetime
    script_version: int = 0
    timeline_version: int = 0
    render_version: int = 0
    current_job_id: Optional[str] = None
    last_error: Optional[ErrorInfo] = None
    stage_history: list[StageRecord] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)  # logical name -> posix relpath
    warnings: list[str] = Field(default_factory=list)


class TimelineScene(BaseModel):
    index: int
    scene_id: str
    source: str  # posix relpath under project dir
    kind: MediaKind
    in_seconds: float = 0.0
    out_seconds: Optional[float] = None
    duration_seconds: float
    asset_id: str
    caption_text: Optional[str] = None
    on_screen_text: Optional[str] = None


class Timeline(BaseModel):
    schema_version: int = 1
    project_id: str
    version: int
    created_at: datetime
    aspect_ratio: Literal["9:16", "16:9"]
    width: int
    height: int
    fps: int
    total_duration_seconds: float
    scenes: list[TimelineScene]
    narration: Optional[dict[str, Any]] = None  # {path, duration_seconds}
    music: Optional[dict[str, Any]] = None  # {path, gain}
    captions: Optional[dict[str, Any]] = None  # {srt_path, burn_in: bool}
    render: dict[str, Any] = Field(default_factory=dict)
