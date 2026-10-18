"""Review, approval, publishing, and job-control tools."""

from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import MCPServer

from ...workflow.orchestrator import CreatorService
from ..schemas import ToolEnvelope, guard

TOOL_GET_REVIEW_PACKAGE = "creator_get_review_package"
TOOL_APPROVE_PUBLISH = "creator_approve_publish"
TOOL_PUBLISH_VIDEO = "creator_publish_video"
TOOL_GET_PUBLISH_STATUS = "creator_get_publish_status"
TOOL_CANCEL_JOB = "creator_cancel_job"


def register_review_tools(server: MCPServer, service: CreatorService) -> None:
    @server.tool(
        name=TOOL_GET_REVIEW_PACKAGE,
        description=(
            "Assemble the review bundle for the rendered video: preview path, exact publish "
            "payload (caption/hashtags/title for a target Page), media and payload SHA-256 hashes, "
            "license/attribution manifest, and warnings. Local write (review files). Present its "
            "contents to the USER for review; only the user's explicit decision may lead to "
            "creator_approve_publish."
        ),
    )
    async def creator_get_review_package(
        project_id: str, page_id: Optional[str] = None
    ) -> ToolEnvelope:
        return await guard(
            TOOL_GET_REVIEW_PACKAGE, service.get_review_package(project_id, page_id=page_id)
        )

    @server.tool(
        name=TOOL_APPROVE_PUBLISH,
        description=(
            "Record the USER's explicit approval for the exact reviewed payload, bound to the "
            f"target Page, media SHA-256, payload SHA-256, and an expiry (default 60 min, single "
            "use). Security-sensitive local write. Never call this because a model 'believes' the "
            "user approved: the user must have explicitly approved THIS payload for THIS Page; the "
            "publisher independently revalidates the approval before any upload."
        ),
    )
    async def creator_approve_publish(
        project_id: str, page_id: Optional[str] = None, ttl_minutes: int = 60
    ) -> ToolEnvelope:
        return await guard(
            TOOL_APPROVE_PUBLISH,
            service.approve_publish(project_id, page_id=page_id, ttl_minutes=ttl_minutes),
        )

    @server.tool(
        name=TOOL_PUBLISH_VIDEO,
        description=(
            "Publish the approved video to the target Facebook Page. The approval guard is "
            "verified with fresh hashes and single-use semantics; the attempt is journaled before "
            "any network call; UNKNOWN outcomes are never retried blindly. With dry_run=true "
            "(default when live publishing is disabled) nothing is uploaded and the simulated "
            "request is journaled instead."
        ),
    )
    async def creator_publish_video(
        project_id: str,
        page_id: Optional[str] = None,
        dry_run: Optional[bool] = None,
        note: Optional[str] = None,
    ) -> ToolEnvelope:
        return await guard(
            TOOL_PUBLISH_VIDEO,
            service.publish_video(project_id, page_id=page_id, dry_run=dry_run, note=note),
        )

    @server.tool(
        name=TOOL_GET_PUBLISH_STATUS,
        description=(
            "Report the journaled publish attempt for a project (latest, or a specific "
            "attempt_id) and reconcile UNKNOWN outcomes through the Graph API when a remote id is "
            "known. Network request only when reconciling. Use it after any failed/interrupted "
            "publish BEFORE considering a retry; it never uploads anything."
        ),
    )
    async def creator_get_publish_status(
        project_id: str, attempt_id: Optional[str] = None
    ) -> ToolEnvelope:
        return await guard(
            TOOL_GET_PUBLISH_STATUS,
            service.get_publish_status(project_id, attempt_id=attempt_id),
        )

    @server.tool(
        name=TOOL_CANCEL_JOB,
        description=(
            "Cancel a running job (e.g. an ffmpeg render started by creator_render_preview) by its "
            "job_id. Terminates the underlying subprocess; partial artifacts are preserved and the "
            "project stays resumable. Side effect: stops work in progress only. Job ids are "
            "ephemeral per server session."
        ),
    )
    async def creator_cancel_job(job_id: str) -> ToolEnvelope:
        return await guard(TOOL_CANCEL_JOB, service.cancel_job(job_id))
