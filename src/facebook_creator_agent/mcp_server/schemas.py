"""Tool envelope and shared input models for the MCP surface.

Every tool returns :class:`ToolEnvelope`.  The envelope's ``error`` field is
the ARCHITECTURE.md §9 five-field contract, so clients get the same shape for
success and failure.  ``ToolError`` (SDK argument validation) stays the SDK's
own error path; everything the application can classify comes back in-band.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from ..core.errors import ErrorInfo
from ..observability.logging import get_logger, redact_text

logger = get_logger("mcp_server.schemas")


class ToolEnvelope(BaseModel):
    """Uniform tool response (ARCHITECTURE.md §4.1)."""

    status: str = Field(description="'ok' or 'error'.")
    tool: str = Field(description="Name of the tool that produced this result.")
    project_id: Optional[str] = Field(default=None, description="Project this result belongs to, if any.")
    artifacts: dict[str, str] = Field(
        default_factory=dict, description="Logical name -> project-relative artifact path."
    )
    warnings: list[str] = Field(default_factory=list)
    next_action: Optional[str] = Field(default=None, description="Suggested next call or user action.")
    error: Optional[ErrorInfo] = Field(default=None)
    data: Optional[dict[str, Any]] = Field(default=None)


class AssetSelection(BaseModel):
    """One approved candidate to download for one scene."""

    scene_id: str = Field(description="Scene id from the storyboard (e.g. 's01').")
    candidate_key: str = Field(
        description="candidate_key from creator_search_media (e.g. 'fixture:clip-1.mp4')."
    )


class UserMediaInput(BaseModel):
    """A user-supplied media file used for one scene (rights confirmed)."""

    scene_id: str = Field(description="Scene id from the storyboard.")
    path: str = Field(description="Absolute (or workspace-relative) path to your video/image file.")
    confirm_rights: bool = Field(
        default=False,
        description="Must be true: you confirm you own or have licensed this file for social media use.",
    )
    license_note: Optional[str] = Field(
        default=None, description="Optional note describing your license/rights basis."
    )


def ok_envelope(tool: str, result: dict[str, Any]) -> ToolEnvelope:
    """Build a success envelope from a service result dict."""
    data = dict(result.get("data") or {})
    if result.get("project_status"):
        data.setdefault("project_status", result["project_status"])
    return ToolEnvelope(
        status="ok",
        tool=tool,
        project_id=result.get("project_id"),
        artifacts=result.get("artifacts") or {},
        warnings=[str(w) for w in (result.get("warnings") or [])],
        next_action=result.get("next_action"),
        data=data or None,
    )


def error_envelope(
    tool: str, error: ErrorInfo, *, details: Optional[dict[str, Any]] = None
) -> ToolEnvelope:
    project_id = None
    if details and isinstance(details.get("project_id"), str):
        project_id = details["project_id"]
    return ToolEnvelope(
        status="error",
        tool=tool,
        project_id=project_id,
        warnings=[],
        next_action=error.next_action,
        error=error,
        data=details or None,
    )


async def guard(tool: str, call) -> ToolEnvelope:  # noqa: ANN001 - async callable
    """Execute a service call, converting errors to structured envelopes.

    ``CreatorError`` maps to its five-field contract; any other exception is
    logged (server-side only) and returned as a redacted ``INTERNAL_ERROR``.
    """
    from ..core.errors import CreatorError, ErrorCode, error_info

    try:
        result = await call
    except CreatorError as exc:
        logger.info("tool %s failed: %s", tool, exc.code)
        details = dict(exc.details or {})
        return error_envelope(tool, exc.to_info(), details=details)
    except Exception as exc:  # noqa: BLE001 - normalized below
        logger.exception("tool %s raised an unexpected exception", tool)
        info = error_info(
            ErrorCode.INTERNAL_ERROR,
            f"Unexpected {type(exc).__name__}: {redact_text(str(exc))[:200]}",
            stage="general",
            next_action="Check the server stderr log; report this failure if it repeats.",
        )
        return error_envelope(tool, info)
    return ok_envelope(tool, result)
