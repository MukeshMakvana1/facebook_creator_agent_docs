"""Production pipeline tools: project -> script -> media -> narration -> timeline -> render."""

from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import MCPServer

from ...workflow.orchestrator import CreatorService
from ..schemas import AssetSelection, ToolEnvelope, UserMediaInput, guard

TOOL_CREATE_PROJECT = "creator_create_project"
TOOL_GENERATE_SCRIPT = "creator_generate_script"
TOOL_SEARCH_MEDIA = "creator_search_media"
TOOL_DOWNLOAD_ASSETS = "creator_download_assets"
TOOL_GENERATE_NARRATION = "creator_generate_narration"
TOOL_BUILD_TIMELINE = "creator_build_timeline"
TOOL_RENDER_PREVIEW = "creator_render_preview"
TOOL_VALIDATE_RENDER = "creator_validate_render"


def register_production_tools(server: MCPServer, service: CreatorService) -> None:
    @server.tool(
        name=TOOL_CREATE_PROJECT,
        description=(
            "Create (or idempotently return) an isolated project workspace for one video from a "
            "topic/prompt, with language, target duration, aspect ratio, tone, audience, and "
            "optional brand instructions. Local write only; a repeated identical call returns the "
            "existing project instead of duplicating it. This is always the first pipeline step."
        ),
    )
    async def creator_create_project(
        topic: str,
        language: str = "en",
        target_duration_seconds: int = 45,
        aspect_ratio: str = "9:16",
        tone: Optional[str] = None,
        audience: Optional[str] = None,
        brand_instructions: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> ToolEnvelope:
        return await guard(
            TOOL_CREATE_PROJECT,
            service.create_project(
                topic=topic,
                language=language,
                target_duration_seconds=target_duration_seconds,
                aspect_ratio=aspect_ratio,
                tone=tone,
                audience=audience,
                brand_instructions=brand_instructions,
                project_id=project_id,
            ),
        )

    @server.tool(
        name=TOOL_GENERATE_SCRIPT,
        description=(
            "Generate the research brief, script, scene plan, and storyboard for a project using "
            "the configured LLM endpoint, then store them as versioned artifacts (model request + "
            "local write). Include revision_instructions to revise the script from NEW/SCRIPT_READY. "
            "Use it once per script version; it refuses to run after asset selection has moved on."
        ),
    )
    async def creator_generate_script(
        project_id: str, revision_instructions: Optional[str] = None
    ) -> ToolEnvelope:
        return await guard(
            TOOL_GENERATE_SCRIPT,
            service.generate_script(project_id, revision_instructions=revision_instructions),
        )

    @server.tool(
        name=TOOL_SEARCH_MEDIA,
        description=(
            "Search the configured media providers for candidates for every storyboard scene (or a "
            "subset via scene_ids), evaluating each candidate against the license policy. Network "
            "requests only to providers the operator allowlisted; results are stored with "
            "provenance. Use it after creator_generate_script; it never downloads files (that is "
            "creator_download_assets)."
        ),
    )
    async def creator_search_media(
        project_id: str,
        scene_ids: Optional[list[str]] = None,
        kind: str = "any",
        max_results: int = 5,
        query: Optional[str] = None,
    ) -> ToolEnvelope:
        return await guard(
            TOOL_SEARCH_MEDIA,
            service.search_media(
                project_id, scene_ids=scene_ids, kind=kind, max_results=max_results, query=query
            ),
        )

    @server.tool(
        name=TOOL_DOWNLOAD_ASSETS,
        description=(
            "Download the chosen APPROVED media candidates into the project (provider-host "
            "allowlists, size/content-type checks, hashes) and/or intake user-supplied files with "
            "explicit rights confirmation (confirm_rights must be true), then write the asset "
            "manifest. Local/network write. Non-approved candidates are refused; unknown-rights "
            "material is never silently accepted."
        ),
    )
    async def creator_download_assets(
        project_id: str,
        selections: Optional[list[AssetSelection]] = None,
        user_assets: Optional[list[UserMediaInput]] = None,
    ) -> ToolEnvelope:
        return await guard(
            TOOL_DOWNLOAD_ASSETS,
            service.download_assets(
                project_id,
                selections=[s.model_dump() for s in selections] if selections else [],
                user_assets=[u.model_dump() for u in user_assets] if user_assets else [],
            ),
        )

    @server.tool(
        name=TOOL_GENERATE_NARRATION,
        description=(
            "Produce the project's narration: either run the configured TTS command on the script "
            "text, or ingest a user-supplied audio file via narration_path (manual fallback). The "
            "audio is probed with ffprobe, hashed, and recorded with provider metadata. Requires "
            "ASSETS_READY; run it once before building the timeline."
        ),
    )
    async def creator_generate_narration(
        project_id: str,
        narration_path: Optional[str] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ) -> ToolEnvelope:
        return await guard(
            TOOL_GENERATE_NARRATION,
            service.generate_narration(
                project_id, narration_path=narration_path, voice=voice, language=language
            ),
        )

    @server.tool(
        name=TOOL_BUILD_TIMELINE,
        description=(
            "Build the deterministic timeline/edit-decision-list and the SRT captions for the "
            "project (scene durations derived from the narration, aspect preset, subtitle timing "
            "mode recorded). Local write; raises the timeline version. Use it after narration "
            "exists; re-running after revisions bumps the version and requires a re-render."
        ),
    )
    async def creator_build_timeline(project_id: str, burn_captions: bool = True) -> ToolEnvelope:
        return await guard(
            TOOL_BUILD_TIMELINE, service.build_timeline(project_id, burn_captions=burn_captions)
        )

    @server.tool(
        name=TOOL_RENDER_PREVIEW,
        description=(
            "Render the video with ffmpeg: per-scene segments, concat, narration mix (and optional "
            "music), optional subtitle burn-in, H.264/AAC output, then ffprobe + full decode "
            "validation. CPU/disk intensive, cancellable via creator_cancel_job. Use it after "
            "creator_build_timeline; re-rendering identical inputs is cached and idempotent."
        ),
    )
    async def creator_render_preview(
        project_id: str, burn_captions: Optional[bool] = None, force: bool = False
    ) -> ToolEnvelope:
        return await guard(
            TOOL_RENDER_PREVIEW,
            service.render_preview(project_id, burn_captions=burn_captions, force=force),
        )

    @server.tool(
        name=TOOL_VALIDATE_RENDER,
        description=(
            "Re-run the ffprobe checks and full decode test on the project's rendered video and "
            "report dimensions, duration, audio levels, and warnings. Read-only/CPU; makes no "
            "changes and no network calls. Use it to double-check a render or diagnose playback "
            "problems before review."
        ),
    )
    async def creator_validate_render(project_id: str) -> ToolEnvelope:
        return await guard(TOOL_VALIDATE_RENDER, service.validate_render(project_id))
