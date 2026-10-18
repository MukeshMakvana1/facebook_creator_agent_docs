"""Error taxonomy and structured error contract.

Every error surfaced by a tool is a :class:`CreatorError` carrying the five
fields required by ARCHITECTURE.md §9: ``code``, ``safe_explanation``,
``retryable``, ``stage``, ``next_action``.

The first block of :class:`ErrorCode` values is the canonical taxonomy from
ARCHITECTURE.md §9.  The values after the ``-- extensions`` marker are small,
documented additions needed to express input/state/not-found failures while
keeping the same five-field shape.  They never replace a canonical code where
one exists.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class ErrorCode(str, Enum):
    """Canonical error taxonomy (ARCHITECTURE.md §9) plus documented extensions."""

    # --- canonical taxonomy (ARCHITECTURE.md §9) ---
    CONFIG_INVALID = "CONFIG_INVALID"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    RESEARCH_UNAVAILABLE = "RESEARCH_UNAVAILABLE"
    MEDIA_LICENSE_REJECTED = "MEDIA_LICENSE_REJECTED"
    MEDIA_DOWNLOAD_FAILED = "MEDIA_DOWNLOAD_FAILED"
    MEDIA_INVALID = "MEDIA_INVALID"
    TTS_UNAVAILABLE = "TTS_UNAVAILABLE"
    AUDIO_INVALID = "AUDIO_INVALID"
    FFMPEG_FAILED = "FFMPEG_FAILED"
    RENDER_VALIDATION_FAILED = "RENDER_VALIDATION_FAILED"
    FACEBOOK_AUTH_REQUIRED = "FACEBOOK_AUTH_REQUIRED"
    FACEBOOK_PERMISSION_MISSING = "FACEBOOK_PERMISSION_MISSING"
    FACEBOOK_OPERATION_UNSUPPORTED = "FACEBOOK_OPERATION_UNSUPPORTED"
    PUBLISH_APPROVAL_REQUIRED = "PUBLISH_APPROVAL_REQUIRED"
    PUBLISH_APPROVAL_STALE = "PUBLISH_APPROVAL_STALE"
    PUBLISH_OUTCOME_UNKNOWN = "PUBLISH_OUTCOME_UNKNOWN"
    RATE_LIMITED = "RATE_LIMITED"
    CANCELLED = "CANCELLED"

    # --- extensions (documented; same five-field shape) ---
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_STATE = "INVALID_STATE"
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Stages used in error reports (aligned with the workflow phases).
STAGES = (
    "setup",
    "script",
    "media",
    "narration",
    "timeline",
    "render",
    "review",
    "publish",
    "general",
)


class ErrorInfo(BaseModel):
    """Structured error payload carried in every tool envelope."""

    code: str
    safe_explanation: str
    retryable: bool
    stage: str
    next_action: Optional[str] = None


class CreatorError(Exception):
    """Application error with the ARCHITECTURE.md §9 five-field contract.

    ``safe_explanation`` must never contain secrets, tokens, or raw provider
    payloads — only redacted, user-readable text.
    """

    def __init__(
        self,
        code: ErrorCode | str,
        safe_explanation: str,
        *,
        stage: str = "general",
        retryable: bool = False,
        next_action: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        if isinstance(code, ErrorCode):
            code = code.value
        if stage not in STAGES:
            stage = "general"
        super().__init__(f"{code}: {safe_explanation}")
        self.code = code
        self.safe_explanation = safe_explanation
        self.retryable = retryable
        self.stage = stage
        self.next_action = next_action
        self.details = details or {}

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code,
            safe_explanation=self.safe_explanation,
            retryable=self.retryable,
            stage=self.stage,
            next_action=self.next_action,
        )


def error_info(
    code: ErrorCode | str,
    safe_explanation: str,
    *,
    stage: str = "general",
    retryable: bool = False,
    next_action: Optional[str] = None,
) -> ErrorInfo:
    return CreatorError(
        code,
        safe_explanation,
        stage=stage,
        retryable=retryable,
        next_action=next_action,
    ).to_info()
