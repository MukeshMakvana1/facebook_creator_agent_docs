"""Workflow orchestrator: the single application service behind the MCP tools.

Coordinates state transitions and dependencies (ARCHITECTURE.md §4.2):
writes artifacts after each stage, records stage history, supports resume
from the last validated artifact, and enforces the order script -> media ->
narration -> timeline -> render -> review -> approval -> publish.

The MCP layer contains no business logic; every tool call lands here.
"""

from __future__ import annotations

import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ..config.settings import Settings, validate_settings
from ..core.errors import CreatorError, ErrorCode, error_info
from ..core.ids import iso, new_id, sha256_file, sha256_json, sha256_text, utcnow
from ..core.jobs import Job, JobRegistry
from ..core.models import AssetRecord, Project, ScriptDraft, Storyboard, Timeline
from ..core.paths import safe_filename
from ..core.state_machine import ProjectStatus, StateMachine
from ..media_pipeline import captions as captions_module
from ..media_pipeline import validation as media_validation
from ..media_pipeline.ffmpeg import FFmpegRunner, render_project_video
from ..media_pipeline.timeline import build_timeline, edl_dict, scene_windows
from ..observability.logging import get_logger, redact_text
from ..providers.facebook.base import FacebookAdapter
from ..providers.facebook.meta_graph import build_facebook_adapter
from ..providers.llm.base import LLMAdapter
from ..providers.llm.openai_compatible import build_llm
from ..providers.media.base import AssetCandidate
from ..providers.media.policy import LicensePolicy
from ..providers.media.registry import MediaProviderRegistry
from ..providers.research.base import ResearchAdapter, build_research
from ..providers.tts.base import TTSAdapter
from ..providers.tts.command import CommandTTSAdapter, parse_command
from ..providers.tts.manual import ingest_manual_narration
from ..publishing.approval import ApprovalGuard
from ..publishing.journal import PublishJournal
from ..publishing.publisher import PublishService
from ..security.downloader import SafeDownloader
from ..workspace.project_store import ProjectStore
from . import stages
from .resume import verify_stage_artifacts

logger = get_logger("workflow.orchestrator")

PROVIDER_HOSTS: dict[str, set[str]] = {
    "pexels": {"images.pexels.com", "videos.pexels.com"},
}

_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _guess_kind(ext: str) -> Optional[str]:
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _IMAGE_EXT:
        return "image"
    return None


class CreatorService:
    """Application service used by every ``creator_*`` tool."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: Optional[ProjectStore] = None,
        registry: Optional[MediaProviderRegistry] = None,
        llm: Optional[LLMAdapter] = None,
        research: Optional[ResearchAdapter] = None,
        tts: Optional[TTSAdapter] = None,
        facebook: Optional[FacebookAdapter] = None,
        downloader: Optional[SafeDownloader] = None,
        jobs: Optional[JobRegistry] = None,
        guard: Optional[ApprovalGuard] = None,
        journal: Optional[PublishJournal] = None,
        publisher: Optional[PublishService] = None,
    ) -> None:
        self.settings = settings
        self.store = store or ProjectStore(settings)
        self.registry = registry or MediaProviderRegistry.from_settings(settings)
        self.llm = llm or build_llm(settings)
        self.research = research or build_research(settings)
        self.tts = tts or CommandTTSAdapter(
            parse_command(settings.tts.command_json),
            output_extension=settings.tts.output_extension,
            timeout_seconds=settings.tts.timeout_seconds,
        )
        self.facebook = facebook or build_facebook_adapter(settings)
        self.downloader = downloader or SafeDownloader(
            allowed_hosts=PROVIDER_HOSTS,
            max_bytes=settings.media.max_download_bytes,
            max_redirects=settings.media.max_redirects,
        )
        self.jobs = jobs or JobRegistry()
        self.guard = guard or ApprovalGuard(self.store)
        self.journal = journal or PublishJournal(self.store)
        self.publisher = publisher or PublishService(
            settings=settings,
            store=self.store,
            guard=self.guard,
            journal=self.journal,
            adapter=self.facebook,
        )

    # ------------------------------------------------------------- utilities
    @asynccontextmanager
    async def _stage(self, project: Project, stage: str, job: Optional[Job] = None) -> AsyncIterator[Any]:
        record = self.store.stage_started(project, stage, job_id=job.job_id if job else None)
        try:
            yield record
        except CreatorError as exc:
            status = "cancelled" if exc.code == ErrorCode.CANCELLED.value else "error"
            self.store.stage_finished(project, record, status=status, error=exc.to_info())
            raise
        except Exception as exc:  # unexpected: record and normalize
            info = error_info(
                ErrorCode.INTERNAL_ERROR,
                f"Unexpected {type(exc).__name__} during stage {stage!r}.",
                stage=stage,
                next_action="Check the server logs for the traceback; report if it repeats.",
            )
            self.store.stage_finished(project, record, status="error", error=info)
            logger.exception("unexpected error in stage %s", stage)
            raise CreatorError(
                ErrorCode.INTERNAL_ERROR, info.safe_explanation, stage=stage, next_action=info.next_action
            ) from exc
        else:
            self.store.stage_finished(project, record)

    def _transition(self, project: Project, target: ProjectStatus) -> None:
        StateMachine.ensure(project.status, target)
        project.status = target
        self.store.save(project)

    def _load_storyboard(self, project: Project) -> Storyboard:
        if not self.store.exists(project.project_id, "storyboard.json"):
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                "No storyboard exists for this project.",
                stage="script",
                next_action="Run creator_generate_script first.",
            )
        return Storyboard.model_validate(
            self.store.read_json(project.project_id, "storyboard.json")
        )

    def _load_timeline(self, project: Project) -> Timeline:
        rel = project.artifacts.get("timeline")
        if not rel or not self.store.exists(project.project_id, rel):
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                "No timeline exists for this project.",
                stage="timeline",
                next_action="Run creator_build_timeline first.",
            )
        return Timeline.model_validate(self.store.read_json(project.project_id, rel))

    def _load_assets_manifest(self, project: Project) -> dict[str, Any]:
        rel = project.artifacts.get("assets_manifest")
        if not rel or not self.store.exists(project.project_id, rel):
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                "No asset manifest exists for this project.",
                stage="media",
                next_action="Run creator_download_assets first.",
            )
        return self.store.read_json(project.project_id, rel)

    def _require_status(self, project: Project, allowed: set[ProjectStatus], stage: str, next_action: str) -> None:
        if project.status not in allowed:
            expected = ", ".join(sorted(s.value for s in allowed))
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                f"Project status is {project.status.value}; this step expects one of: {expected}.",
                stage=stage,
                next_action=next_action,
            )

    @staticmethod
    def project_summary(project: Project) -> dict[str, Any]:
        return {
            "project_id": project.project_id,
            "topic": project.topic,
            "status": project.status.value,
            "aspect_ratio": project.aspect_ratio,
            "target_duration_seconds": project.target_duration_seconds,
            "script_version": project.script_version,
            "timeline_version": project.timeline_version,
            "render_version": project.render_version,
            "artifacts": dict(project.artifacts),
            "warnings": list(project.warnings)[-10:],
            "last_error": project.last_error.model_dump() if project.last_error else None,
            "updated_at": iso(project.updated_at),
        }

    # ------------------------------------------------------------------ setup
    async def doctor(self, *, probe_network: bool = False) -> dict[str, Any]:
        components: list[dict[str, Any]] = []

        py_ok = sys.version_info >= (3, 10)
        components.append(
            {
                "name": "python",
                "status": "ok" if py_ok else "error",
                "details": f"{sys.version.split()[0]} (>=3.10 required)",
            }
        )

        binaries = await media_validation.detect_binaries(
            self.settings.render.ffmpeg_path, self.settings.render.ffprobe_path
        )
        components.append(
            {
                "name": "ffmpeg",
                "status": "ok" if binaries["available"] else "error",
                "details": binaries,
            }
        )

        writable, detail = self.store.writable_check()
        components.append(
            {"name": "workspace", "status": "ok" if writable else "error", "details": detail}
        )

        llm_state = "ok" if self.llm.configured else "warn"
        components.append(
            {
                "name": "llm",
                "status": llm_state,
                "details": self.llm.status() if hasattr(self.llm, "status") else {"configured": self.llm.configured},
            }
        )
        if probe_network and self.llm.configured:
            try:
                result = await self.llm.test()
                components.append({"name": "llm_network", "status": "ok", "details": result})
            except CreatorError as exc:
                components.append(
                    {"name": "llm_network", "status": "error", "details": exc.to_info().model_dump()}
                )

        components.append(
            {
                "name": "tts",
                "status": "ok" if getattr(self.tts, "configured", False) else "warn",
                "details": self.tts.capabilities() if hasattr(self.tts, "capabilities") else {},
            }
        )

        fb = self.settings.facebook
        fb_status = "warn"
        if fb.access_token and fb.page_id:
            fb_status = "ok"
        components.append(
            {
                "name": "facebook",
                "status": fb_status,
                "details": {
                    "token_present": bool(fb.access_token),
                    "page_id_present": bool(fb.page_id),
                    "live_publish_enabled": fb.live_publish_enabled,
                    "graph_version": fb.graph_version,
                    "note": "Live Meta validation is a documented blocked task (T0909).",
                },
            }
        )

        components.append(
            {
                "name": "media_providers",
                "status": "ok" if self.settings.media.allowed_providers else "warn",
                "details": self.registry.status_list(set(self.settings.media.allowed_providers)),
            }
        )
        components.append(
            {
                "name": "research",
                "status": "ok" if self.research.configured else "warn",
                "details": self.research.status(),
            }
        )

        # recent project consistency (resume safety)
        recent: list[dict[str, Any]] = []
        projects_dir = self.store.projects_dir
        if projects_dir.is_dir():
            entries = sorted(
                (p for p in projects_dir.iterdir() if (p / "project.json").is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:5]
            for entry in entries:
                try:
                    project = self.store.load(entry.name)
                    issues = verify_stage_artifacts(self.store, project)
                    recent.append(
                        {
                            "project_id": project.project_id,
                            "status": project.status.value,
                            "resume_issues": issues,
                        }
                    )
                except CreatorError as exc:
                    recent.append({"project_id": entry.name, "error": exc.safe_explanation})
        components.append({"name": "recent_projects", "status": "ok", "details": recent})

        errors = [c["name"] for c in components if c["status"] == "error"]
        warnings_list = [c["name"] for c in components if c["status"] == "warn"]
        overall = "blocked" if errors else ("partial" if warnings_list else "ready")
        config_issues = validate_settings(self.settings)
        hard_config = [i for i in config_issues if i["severity"] == "error"]

        next_action = None
        if errors:
            next_action = f"Fix the failing components: {', '.join(errors)}."
        elif hard_config:
            next_action = "Fix the configuration issues listed in data.config_issues."
        elif warnings_list:
            next_action = "Optional components are not configured: " + ", ".join(warnings_list) + "."
        return {
            "data": {
                "overall": overall,
                "components": components,
                "config_issues": config_issues,
                "settings": self.settings.redacted_dump(),
            },
            "warnings": [f"{c['name']}: {c['details']}" for c in components if c["status"] == "warn"],
            "next_action": next_action,
        }

    async def list_providers(self) -> dict[str, Any]:
        fb = self.facebook.status()
        return {
            "data": {
                "llm": self.llm.status(),
                "research": self.research.status(),
                "tts": (
                    self.tts.capabilities()
                    if hasattr(self.tts, "capabilities")
                    else {"configured": getattr(self.tts, "configured", False)}
                ),
                "manual_narration": {
                    "available": True,
                    "note": "Pass narration_path to creator_generate_narration to use a user-supplied audio file.",
                },
                "media": self.registry.status_list(set(self.settings.media.allowed_providers)),
                "facebook": fb,
                "render": {
                    "ffmpeg_path": self.settings.render.ffmpeg_path,
                    "ffprobe_path": self.settings.render.ffprobe_path,
                    "fps": self.settings.render.default_fps,
                    "crf": self.settings.render.crf,
                    "preset": self.settings.render.preset,
                },
            }
        }

    async def test_llm(self) -> dict[str, Any]:
        if not self.llm.configured:
            raise CreatorError(
                ErrorCode.CONFIG_INVALID,
                "No LLM endpoint is configured.",
                stage="script",
                next_action="Set FCA_LLM_BASE_URL and FCA_LLM_MODEL, then run creator_test_llm again.",
            )
        started = utcnow()
        result = await self.llm.test()
        return {
            "data": {"test": result, "started_at": iso(started)},
            "warnings": [],
            "next_action": "Configured endpoints are text-generation only; TTS/media/Meta are separate adapters.",
        }

    # --------------------------------------------------------------- facebook
    def _authorization_guide(self) -> dict[str, Any]:
        fb = self.settings.facebook
        return {
            "principle": "Official Meta flows only. The agent never asks for or stores Facebook passwords.",
            "steps": [
                f"Create/select an app at https://developers.facebook.com/apps (Graph API version {fb.graph_version}).",
                "Add the Page-management permissions your app is approved for "
                "(typically pages_show_list, pages_read_engagement, pages_manage_posts).",
                "Generate a user token, then exchange it for a Page access token for the target Page.",
                "Store the token ONLY as FCA_FACEBOOK_ACCESS_TOKEN (environment or local .env; never committed).",
                "Set FCA_FACEBOOK_PAGE_ID to the target Page id.",
                "Optional: set FCA_ENABLE_LIVE_PUBLISH=1 to arm real uploads (default is dry-run).",
                "Verify with creator_list_pages; keep an eye on app review requirements for production use.",
            ],
            "env_vars": [
                "FCA_FACEBOOK_ACCESS_TOKEN",
                "FCA_FACEBOOK_PAGE_ID",
                "FCA_ENABLE_LIVE_PUBLISH",
                "FCA_FACEBOOK_GRAPH_VERSION",
            ],
            "docs": [
                "https://developers.facebook.com/docs/pages/access-tokens",
                "https://developers.facebook.com/docs/video-api/guides/publishing",
                "https://developers.facebook.com/docs/facebook-login/",
            ],
            "unsupported": [
                "Publishing to personal profiles",
                "Password collection of any kind",
                "Guaranteed Reels/scheduling availability (feature-gated, verify against current official docs)",
            ],
            "live_validation_note": "Live Meta publishing was NOT validated in this repository (no credentials; T0909 blocked).",
        }

    async def connect_facebook(self) -> dict[str, Any]:
        guide = self._authorization_guide()
        identity: dict[str, Any] = {"checked": False}
        warnings: list[str] = []
        if self.facebook.has_credentials:
            try:
                token = await self.facebook.check_token()
                identity = {"checked": True, "ok": True, "id": token.id, "name": token.name}
            except CreatorError as exc:
                identity = {"checked": True, "ok": False, "code": exc.code, "explanation": exc.safe_explanation}
            except Exception as exc:  # adapter-level errors
                identity = {"checked": True, "ok": False, "explanation": redact_text(str(exc))[:200]}
        else:
            warnings.append("No access token is configured yet; complete the guide and set the env vars.")
        return {
            "data": {"status": self.facebook.status(), "identity_check": identity, "authorization_guide": guide},
            "warnings": warnings,
            "next_action": "Set FCA_FACEBOOK_ACCESS_TOKEN + FCA_FACEBOOK_PAGE_ID, then run creator_list_pages.",
        }

    async def list_pages(self) -> dict[str, Any]:
        if not self.facebook.has_credentials:
            raise CreatorError(
                ErrorCode.FACEBOOK_AUTH_REQUIRED,
                "No Facebook access token is configured.",
                stage="publish",
                next_action="Run creator_connect_facebook for the official authorization flow, then set FCA_FACEBOOK_ACCESS_TOKEN.",
            )
        identity = await self.facebook.check_token()
        pages = await self.facebook.list_pages()
        return {
            "data": {
                "identity": identity.model_dump(),
                "pages": [p.model_dump() for p in pages],
                "publishable": [p.id for p in pages if p.can_publish],
            },
            "warnings": [] if pages else ["The token has no authorized Pages."],
            "next_action": "Pass the chosen page_id to creator_get_review_package and creator_approve_publish.",
        }

    # ---------------------------------------------------------------- project
    async def create_project(
        self,
        *,
        topic: str,
        language: str = "en",
        target_duration_seconds: int = 45,
        aspect_ratio: str = "9:16",
        tone: Optional[str] = None,
        audience: Optional[str] = None,
        brand_instructions: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        topic = (topic or "").strip()
        if not topic:
            raise CreatorError(
                ErrorCode.INVALID_INPUT,
                "topic must be a non-empty string.",
                stage="setup",
                next_action="Provide the video topic/prompt.",
            )
        if target_duration_seconds < 5 or target_duration_seconds > self.settings.render.max_duration_seconds:
            raise CreatorError(
                ErrorCode.INVALID_INPUT,
                f"target_duration_seconds must be between 5 and {self.settings.render.max_duration_seconds}.",
                stage="setup",
                next_action="Choose a duration within the configured render limits.",
            )
        if aspect_ratio not in ("9:16", "16:9"):
            raise CreatorError(
                ErrorCode.INVALID_INPUT,
                "aspect_ratio must be '9:16' or '16:9'.",
                stage="setup",
            )
        project, created = self.store.create(
            topic=topic,
            language=language,
            target_duration_seconds=target_duration_seconds,
            aspect_ratio=aspect_ratio,
            tone=tone,
            audience=audience,
            brand_instructions=brand_instructions,
            project_id=project_id,
        )
        return {
            "project_id": project.project_id,
            "project_status": project.status.value,
            "artifacts": {"project": "project.json"},
            "data": {"created": created, "project": self.project_summary(project)},
            "warnings": [] if created else ["An identical project already existed; returning it unchanged (idempotent)."],
            "next_action": "Run creator_generate_script to produce the brief/script/storyboard.",
        }

    # ----------------------------------------------------------------- script
    async def generate_script(
        self, project_id: str, *, revision_instructions: Optional[str] = None
    ) -> dict[str, Any]:
        project = self.store.load(project_id)
        self._require_status(
            project,
            {ProjectStatus.NEW, ProjectStatus.SCRIPT_READY},
            "script",
            "Script generation/revision is allowed from NEW or SCRIPT_READY.",
        )
        if not self.llm.configured:
            raise CreatorError(
                ErrorCode.CONFIG_INVALID,
                "No LLM endpoint is configured; script generation needs one.",
                stage="script",
                next_action="Set FCA_LLM_BASE_URL + FCA_LLM_MODEL, verify with creator_test_llm, then retry.",
            )

        async with self._stage(project, "script"):
            research = await self.research.research(project.topic)
            research_doc = research.model_dump(mode="json")
            research_doc["retrieved_at"] = iso(utcnow())
            self.store.write_json(project.project_id, "research.json", research_doc)

            previous_script = None
            if revision_instructions and self.store.exists(project.project_id, "script.md"):
                previous_script = self.store.read_text(project.project_id, "script.md")

            system_prompt, user_prompt = stages.build_script_prompts(
                project,
                max_scenes=self.settings.render.max_scenes,
                revision_instructions=revision_instructions,
                research_note=(
                    research.note if research.status != "available" else None
                ),
                previous_script=previous_script,
            )
            draft = await self.llm.generate_json(system_prompt, user_prompt, ScriptDraft, max_attempts=2)
            if not isinstance(draft, ScriptDraft):
                draft = ScriptDraft.model_validate(draft)

            storyboard = stages.draft_to_storyboard(
                project,
                draft,
                research_status=research.status,
                max_scenes=self.settings.render.max_scenes,
            )
            project.script_version = storyboard.script_version
            self.store.write_text(
                project.project_id,
                "script.md",
                stages.script_markdown(storyboard, sources=research.sources),
            )
            self.store.write_json(
                project.project_id, "storyboard.json", storyboard.model_dump(mode="json")
            )
            self.store.register_artifact(project, "script", "script.md")
            self.store.register_artifact(project, "storyboard", "storyboard.json")

            warnings: list[str] = []
            if research.status != "available":
                warnings.append(
                    "Research is unavailable: factual claims are unverified and no citations were added."
                )
                project.warnings.append("research unavailable during script generation")
            if revision_instructions:
                project.warnings.append(f"script revised: {revision_instructions[:120]}")
            self._transition(project, ProjectStatus.SCRIPT_READY)
            self.store.save(project)

        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {"script": "script.md", "storyboard": "storyboard.json", "research": "research.json"},
            "data": {
                "script_version": storyboard.script_version,
                "scene_count": len(storyboard.scenes),
                "research_status": storyboard.research_status,
                "scenes": [
                    {
                        "scene_id": s.scene_id,
                        "narration_preview": s.narration[:120],
                        "media_query": s.media_query,
                        "target_seconds": s.target_seconds,
                    }
                    for s in storyboard.scenes
                ],
            },
            "warnings": warnings,
            "next_action": "Review the script with the user; then run creator_search_media for candidate footage.",
        }

    # ------------------------------------------------------------------ media
    async def search_media(
        self,
        project_id: str,
        *,
        scene_ids: Optional[list[str]] = None,
        kind: str = "any",
        max_results: int = 5,
        query: Optional[str] = None,
    ) -> dict[str, Any]:
        project = self.store.load(project_id)
        storyboard = self._load_storyboard(project)
        if kind not in ("any", "video", "image"):
            raise CreatorError(
                ErrorCode.INVALID_INPUT, "kind must be one of: any, video, image.", stage="media"
            )
        max_results = max(1, min(int(max_results), 15))

        by_id = {s.scene_id: s for s in storyboard.scenes}
        selected_scenes = storyboard.scenes
        if scene_ids:
            unknown = [sid for sid in scene_ids if sid not in by_id]
            if unknown:
                raise CreatorError(
                    ErrorCode.INVALID_INPUT,
                    f"Unknown scene ids: {', '.join(unknown[:5])}.",
                    stage="media",
                    next_action=f"Valid scene ids: {', '.join(list(by_id)[:10])}… (see storyboard.json).",
                )
            selected_scenes = [by_id[sid] for sid in scene_ids]

        policy = LicensePolicy(
            allowed_providers=set(self.settings.media.allowed_providers),
            allowed_licenses=set(self.settings.media.allowed_licenses) or None,
        )

        existing: dict[str, Any] = {}
        if self.store.exists(project_id, "assets/candidates.json"):
            existing = self.store.read_json(project_id, "assets/candidates.json").get("scenes", {})

        warnings: list[str] = []
        scenes_out: list[dict[str, Any]] = []
        async with self._stage(project, "media"):
            for scene in selected_scenes:
                scene_query = (query or scene.media_query or project.topic).strip()
                candidates, provider_warnings = await self.registry.search(
                    scene_query,
                    kind,
                    max_results,
                    allowed_providers={p.lower() for p in self.settings.media.allowed_providers},
                )
                warnings.extend(provider_warnings)
                evaluated: list[dict[str, Any]] = []
                for index, candidate in enumerate(candidates):
                    result = policy.evaluate(candidate)
                    key = f"{candidate.provider}:{candidate.provider_asset_id or candidate.local_source_path or index}"
                    evaluated.append(
                        {
                            "candidate_key": key,
                            "rights_status": result.rights_status,
                            "rights_reasons": result.reasons,
                            "candidate": candidate.model_dump(mode="json"),
                        }
                    )
                approachable = [c for c in evaluated if c["rights_status"] == "APPROVED"]
                if not approachable:
                    warnings.append(
                        f"scene {scene.scene_id}: no automatically-usable candidate; "
                        "provide user media or review candidates manually."
                    )
                existing[scene.scene_id] = {
                    "query": scene_query,
                    "kind": kind,
                    "candidates": evaluated,
                }
                scenes_out.append(
                    {
                        "scene_id": scene.scene_id,
                        "query": scene_query,
                        "candidates": [
                            {
                                "candidate_key": c["candidate_key"],
                                "provider": c["candidate"]["provider"],
                                "title": c["candidate"].get("title"),
                                "kind": c["candidate"].get("kind"),
                                "license_name": c["candidate"].get("license_name"),
                                "rights_status": c["rights_status"],
                                "rights_reasons": c["rights_reasons"],
                                "downloadable": bool(
                                    c["candidate"].get("download_url")
                                    or c["candidate"].get("local_source_path")
                                ),
                            }
                            for c in evaluated
                        ],
                    }
                )
            self.store.write_json(
                project_id,
                "assets/candidates.json",
                {"schema_version": 1, "generated_at": iso(utcnow()), "scenes": existing},
            )

        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {"candidates": "assets/candidates.json"},
            "data": {"scenes": scenes_out},
            "warnings": warnings,
            "next_action": (
                "Pick approved candidate_keys and run creator_download_assets "
                "(or supply user media with confirm_rights=true)."
            ),
        }

    async def download_assets(
        self,
        project_id: str,
        *,
        selections: Optional[list[dict[str, Any]]] = None,
        user_assets: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        selections = selections or []
        user_assets = user_assets or []
        if not selections and not user_assets:
            raise CreatorError(
                ErrorCode.INVALID_INPUT,
                "Nothing to download: provide selections and/or user_assets.",
                stage="media",
                next_action="Run creator_search_media, then pass candidate_keys or user file paths.",
            )
        project = self.store.load(project_id)
        storyboard = self._load_storyboard(project)
        self._require_status(
            project,
            {ProjectStatus.SCRIPT_READY, ProjectStatus.ASSETS_READY},
            "media",
            "Asset intake is allowed from SCRIPT_READY or ASSETS_READY (re-runs).",
        )

        candidate_index: dict[str, dict[str, Any]] = {}
        if selections:
            if not self.store.exists(project_id, "assets/candidates.json"):
                raise CreatorError(
                    ErrorCode.NOT_FOUND,
                    "No candidate list exists; run creator_search_media first.",
                    stage="media",
                    next_action="Run creator_search_media, then retry creator_download_assets.",
                )
            doc = self.store.read_json(project_id, "assets/candidates.json")
            for scene_block in doc.get("scenes", {}).values():
                for candidate in scene_block.get("candidates", []):
                    candidate_index[candidate["candidate_key"]] = candidate

        # existing asset records (append-only)
        records: list[AssetRecord] = []
        if self.store.exists(project_id, "assets/assets.json"):
            raw = self.store.read_json(project_id, "assets/assets.json")
            records = [AssetRecord.model_validate(item) for item in raw.get("assets", [])]
        scene_asset: dict[str, str] = {}
        if self.store.exists(project_id, "assets/manifest.json"):
            scene_asset = dict(self.store.read_json(project_id, "assets/manifest.json").get("scene_assets", {}))

        new_assets: list[AssetRecord] = []
        warnings: list[str] = []
        policy = LicensePolicy(
            allowed_providers=set(self.settings.media.allowed_providers),
            allowed_licenses=set(self.settings.media.allowed_licenses) or None,
        )
        valid_scene_ids = {s.scene_id for s in storyboard.scenes}

        async with self._stage(project, "media"):
            for selection in selections:
                scene_id = str(selection.get("scene_id") or "")
                key = str(selection.get("candidate_key") or "")
                if scene_id not in valid_scene_ids:
                    raise CreatorError(
                        ErrorCode.INVALID_INPUT,
                        f"Unknown scene id {scene_id!r}.",
                        stage="media",
                        next_action="Use scene ids from storyboard.json.",
                    )
                entry = candidate_index.get(key)
                if entry is None:
                    raise CreatorError(
                        ErrorCode.NOT_FOUND,
                        f"Candidate {key!r} is not in the stored candidate list.",
                        stage="media",
                        next_action="Re-run creator_search_media and use a fresh candidate_key.",
                    )
                candidate = AssetCandidate.model_validate(entry["candidate"])
                result = policy.evaluate(candidate)
                if result.rights_status != "APPROVED":
                    raise CreatorError(
                        ErrorCode.MEDIA_LICENSE_REJECTED,
                        f"Candidate {key!r} is {result.rights_status}: {'; '.join(result.reasons)}.",
                        stage="media",
                        next_action=(
                            "Choose an APPROVED candidate, or supply the file as user media with "
                            "confirm_rights=true after checking its license."
                        ),
                    )
                record = await self._intake_candidate(project, candidate, scene_id, result.reasons)
                records.append(record)
                new_assets.append(record)

            for user_input in user_assets:
                scene_id = str(user_input.get("scene_id") or "")
                if scene_id not in valid_scene_ids:
                    raise CreatorError(
                        ErrorCode.INVALID_INPUT,
                        f"Unknown scene id {scene_id!r} for user asset.",
                        stage="media",
                        next_action="Use scene ids from storyboard.json.",
                    )
                if not user_input.get("confirm_rights"):
                    raise CreatorError(
                        ErrorCode.MEDIA_LICENSE_REJECTED,
                        "User-supplied media requires explicit rights confirmation (confirm_rights=true).",
                        stage="media",
                        next_action=(
                            "Confirm you own or have licensed the media for social media use, then set "
                            "confirm_rights=true."
                        ),
                    )
                record = self._intake_user_asset(project, user_input, scene_id)
                records.append(record)
                new_assets.append(record)

            for record in new_assets:
                if record.scene_id:
                    scene_asset[record.scene_id] = record.asset_id

            missing = [sid for sid in valid_scene_ids if sid not in scene_asset]
            manifest = {
                "schema_version": 1,
                "generated_at": iso(utcnow()),
                "scene_assets": scene_asset,
                "assets": [r.model_dump(mode="json") for r in records],
                "attribution_block": stages.attribution_block(records),
                "license_summary": _license_summary(records),
            }
            self.store.write_json(project_id, "assets/assets.json", {"schema_version": 1, "assets": [r.model_dump(mode="json") for r in records]})
            self.store.write_json(project_id, "assets/manifest.json", manifest)
            self.store.register_artifact(project, "assets_manifest", "assets/manifest.json")

            if not missing:
                self._transition(project, ProjectStatus.ASSETS_READY)
            else:
                warnings.append(
                    "Scenes still without an approved asset: " + ", ".join(missing[:10])
                )
            self.store.save(project)

        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {"assets_manifest": "assets/manifest.json"},
            "data": {
                "added": [
                    {
                        "asset_id": r.asset_id,
                        "scene_id": r.scene_id,
                        "provider": r.provider,
                        "kind": r.kind,
                        "local_path": r.local_path,
                        "license_name": r.license_name,
                        "sha256": r.sha256,
                    }
                    for r in new_assets
                ],
                "scene_assets": scene_asset,
                "attribution_block": manifest["attribution_block"],
                "license_summary": manifest["license_summary"],
            },
            "warnings": warnings,
            "next_action": (
                "Run creator_generate_narration (TTS command or narration_path) once every scene has media."
                if not missing
                else "Add media for the remaining scenes, then continue."
            ),
        }

    async def _intake_candidate(
        self, project: Project, candidate: AssetCandidate, scene_id: str, reasons: list[str]
    ) -> AssetRecord:
        kind = "image" if candidate.kind == "image" else "video"
        subdir = "assets/images" if kind == "image" else "assets/clips"
        dest_dir = self.store.abs_path(project.project_id, subdir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        local_source = candidate.local_source_path
        if local_source:
            source = Path(local_source)
            if not source.is_file():
                raise CreatorError(
                    ErrorCode.MEDIA_DOWNLOAD_FAILED,
                    f"Provider-local asset is missing on disk: {source.name!r}.",
                    stage="media",
                    next_action="Re-run creator_search_media against the current fixture directory.",
                )
            ext = source.suffix.lower()
            sha = sha256_file(source)
            dest = dest_dir / f"{sha[:16]}{ext}"
            if not dest.exists():
                shutil.copyfile(source, dest)
            bytes_ = dest.stat().st_size
            mime = _MIME_BY_EXT.get(ext)
            probe: dict[str, Any] = {}
            try:
                probe = await media_validation.probe_media(dest, ffprobe_path=self.settings.render.ffprobe_path)
            except CreatorError:
                dest.unlink(missing_ok=True)
                raise
            if kind == "video" and not probe.get("has_video"):
                dest.unlink(missing_ok=True)
                raise CreatorError(
                    ErrorCode.MEDIA_INVALID,
                    f"Fixture {source.name!r} contains no video stream.",
                    stage="media",
                    next_action="Choose another candidate.",
                )
        else:
            if not candidate.download_url:
                raise CreatorError(
                    ErrorCode.MEDIA_DOWNLOAD_FAILED,
                    "Candidate has no download URL or local source.",
                    stage="media",
                    next_action="Choose a different candidate.",
                )
            downloaded = await self.downloader.download(
                candidate.download_url, dest_dir, provider=candidate.provider, kind=kind
            )
            dest = downloaded.path
            sha = downloaded.sha256
            bytes_ = downloaded.bytes
            mime = downloaded.content_type
            probe = await media_validation.probe_media(dest, ffprobe_path=self.settings.render.ffprobe_path)

        rel = dest.resolve().relative_to(self.store.project_dir(project.project_id)).as_posix()
        return AssetRecord(
            asset_id=new_id("asset"),
            provider=candidate.provider,
            provider_asset_id=candidate.provider_asset_id,
            title=candidate.title,
            kind=kind,  # type: ignore[arg-type]
            source_url=candidate.source_page_url,
            download_url=candidate.download_url,
            license_name=candidate.license_name,
            license_url=candidate.license_url,
            attribution=candidate.attribution,
            retrieved_at=utcnow(),
            intended_use="social_media",
            scene_id=scene_id,
            local_path=rel,
            sha256=sha,
            bytes=bytes_,
            mime=mime,
            width=probe.get("width") or candidate.width,
            height=probe.get("height") or candidate.height,
            duration_seconds=probe.get("duration_seconds") or candidate.duration_seconds,
            rights_status="APPROVED",
            rights_reasons=list(reasons),
        )

    def _intake_user_asset(self, project: Project, user_input: dict[str, Any], scene_id: str) -> AssetRecord:
        raw_path = str(user_input.get("path") or "").strip()
        if not raw_path:
            raise CreatorError(
                ErrorCode.INVALID_INPUT,
                "user_assets entries need a 'path'.",
                stage="media",
                next_action="Pass the absolute path of your media file.",
            )
        source = Path(raw_path).expanduser()
        if not source.is_absolute():
            source = (self.settings.workspace_root / source).resolve()
        if not source.is_file():
            raise CreatorError(
                ErrorCode.NOT_FOUND,
                f"User media file does not exist: {source.name!r}.",
                stage="media",
                next_action="Check the path (absolute or workspace-relative).",
            )
        ext = source.suffix.lower()
        kind = _guess_kind(ext)
        if kind is None:
            raise CreatorError(
                ErrorCode.MEDIA_INVALID,
                f"Unsupported user media extension {ext or '(none)'!r}.",
                stage="media",
                next_action="Use a video (mp4/mov) or image (png/jpg/webp) file.",
            )
        if source.stat().st_size > self.settings.media.max_download_bytes:
            raise CreatorError(
                ErrorCode.MEDIA_INVALID,
                "User media exceeds the configured size limit.",
                stage="media",
                next_action="Raise FCA_MEDIA_MAX_DOWNLOAD_MB or use a smaller file.",
            )
        subdir = "assets/images" if kind == "image" else "assets/clips"
        dest_dir = self.store.abs_path(project.project_id, subdir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        sha = sha256_file(source)
        dest = dest_dir / f"{sha[:16]}{ext}"
        if not dest.exists():
            shutil.copyfile(source, dest)
        rel = dest.resolve().relative_to(self.store.project_dir(project.project_id)).as_posix()
        return AssetRecord(
            asset_id=new_id("asset"),
            provider="user",
            provider_asset_id=safe_filename(source.name),
            title=safe_filename(source.name),
            kind=kind,  # type: ignore[arg-type]
            source_url=None,
            download_url=None,
            license_name="USER-SUPPLIED-CONFIRMED",
            license_url=None,
            attribution="User-supplied media (rights confirmed by user)",
            retrieved_at=utcnow(),
            intended_use="social_media",
            scene_id=scene_id,
            local_path=rel,
            sha256=sha,
            bytes=dest.stat().st_size,
            mime=_MIME_BY_EXT.get(ext),
            rights_status="APPROVED",
            rights_reasons=["user confirmed rights for this file"],
            rights_confirmed_by_user=True,
            user_confirmation_text=str(user_input.get("license_note") or "user confirmed rights")[:300],
        )

    # -------------------------------------------------------------- narration
    async def generate_narration(
        self,
        project_id: str,
        *,
        narration_path: Optional[str] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ) -> dict[str, Any]:
        project = self.store.load(project_id)
        storyboard = self._load_storyboard(project)
        self._require_status(
            project,
            {ProjectStatus.ASSETS_READY, ProjectStatus.NARRATION_READY},
            "narration",
            "Narration generation is allowed from ASSETS_READY (or NARRATION_READY to replace it).",
        )
        narration_text = "\n\n".join(s.narration for s in storyboard.scenes).strip()
        narration_dir = self.store.abs_path(project_id, "narration")

        async with self._stage(project, "narration"):
            meta: dict[str, Any]
            if narration_path:
                source = Path(str(narration_path)).expanduser()
                if not source.is_absolute():
                    source = (self.settings.workspace_root / source).resolve()
                ingested = await ingest_manual_narration(
                    source,
                    narration_dir,
                    ffprobe_path=self.settings.render.ffprobe_path,
                    max_seconds=float(self.settings.render.max_duration_seconds * 4),
                )
                audio_path = Path(ingested["path"])
                meta = {
                    "provider": "manual",
                    "voice": None,
                    "language": language or project.language,
                    "duration_seconds": ingested["duration_seconds"],
                    "sha256": ingested["sha256"],
                    "codec": ingested.get("codec"),
                    "source_name": ingested.get("source_name"),
                }
            else:
                if not getattr(self.tts, "configured", False):
                    raise CreatorError(
                        ErrorCode.TTS_UNAVAILABLE,
                        "No narration source is available: no TTS command is configured and no narration_path was supplied.",
                        stage="narration",
                        next_action=(
                            "Either set FCA_TTS_COMMAND_JSON (argv JSON array) or call "
                            "creator_generate_narration with narration_path pointing to your audio file."
                        ),
                    )
                ext = (self.settings.tts.output_extension or "wav").lstrip(".") or "wav"
                out_path = narration_dir / f"narration.{ext}"
                engine_meta = await self.tts.generate(
                    narration_text, out_path, voice=voice, language=language or project.language
                )
                probe = await media_validation.probe_audio(
                    out_path, ffprobe_path=self.settings.render.ffprobe_path
                )
                meta = {
                    "provider": engine_meta.get("provider", "command"),
                    "voice": voice,
                    "language": language or project.language,
                    "duration_seconds": probe["duration_seconds"],
                    "sha256": sha256_file(out_path),
                    "codec": probe.get("codec"),
                    "engine": {k: v for k, v in engine_meta.items() if k != "provider"},
                }
                audio_path = out_path

            rel = audio_path.resolve().relative_to(self.store.project_dir(project_id)).as_posix()
            narration_doc = {
                "schema_version": 1,
                "path": rel,
                "text_sha256": sha256_text(narration_text),
                "text_source": "storyboard",
                "generated_at": iso(utcnow()),
                **meta,
            }
            self.store.write_json(project_id, "narration/narration.json", narration_doc)
            self.store.register_artifact(project, "narration", rel)
            self.store.register_artifact(project, "narration_meta", "narration/narration.json")

            warnings: list[str] = []
            duration = float(meta.get("duration_seconds") or 0)
            if duration > self.settings.render.max_duration_seconds:
                warnings.append(
                    f"Narration is {duration:.0f}s, longer than the configured render limit "
                    f"({self.settings.render.max_duration_seconds}s); rendering will refuse."
                )
            self._transition(project, ProjectStatus.NARRATION_READY)
            self.store.save(project)

        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {"narration": rel},
            "data": {"narration": narration_doc},
            "warnings": warnings,
            "next_action": "Run creator_build_timeline to create the edit decision list and captions.",
        }

    # --------------------------------------------------------------- timeline
    async def build_timeline(self, project_id: str, *, burn_captions: bool = True) -> dict[str, Any]:
        project = self.store.load(project_id)
        storyboard = self._load_storyboard(project)
        self._require_status(
            project,
            {
                ProjectStatus.NARRATION_READY,
                ProjectStatus.RENDERED,
                ProjectStatus.REVIEW_REQUIRED,
                ProjectStatus.APPROVED,
            },
            "timeline",
            "Build the timeline after narration exists (NARRATION_READY or later).",
        )
        manifest = self._load_assets_manifest(project)
        scene_assets = manifest.get("scene_assets", {})
        records_by_id = {a["asset_id"]: a for a in manifest.get("assets", [])}
        assets_by_scene: dict[str, AssetRecord] = {}
        for scene_id, asset_id in scene_assets.items():
            if asset_id in records_by_id:
                assets_by_scene[scene_id] = AssetRecord.model_validate(records_by_id[asset_id])

        narration_meta: dict[str, Any] = {}
        if self.store.exists(project_id, "narration/narration.json"):
            narration_meta = self.store.read_json(project_id, "narration/narration.json")
        narration_duration = narration_meta.get("duration_seconds")

        async with self._stage(project, "timeline"):
            version = project.timeline_version + 1
            timeline = build_timeline(
                project=project,
                storyboard=storyboard,
                assets_by_scene=assets_by_scene,
                narration_duration=narration_duration,
                srt_relpath=None,
                burn_in=burn_captions,
                timing_mode="approximate",
                fps=self.settings.render.default_fps,
                version=version,
                max_scenes=self.settings.render.max_scenes,
                max_duration_seconds=self.settings.render.max_duration_seconds,
            )
            windows = scene_windows(timeline)
            segments, timing_mode = captions_module.scenes_to_segments(windows)
            srt_text = captions_module.build_srt(segments)
            srt_path = self.store.abs_path(project_id, "captions/subtitles.srt")
            captions_module.write_srt(srt_text, srt_path)
            timeline.captions = {
                "srt_path": "captions/subtitles.srt",
                "burn_in": bool(burn_captions),
                "timing_mode": timing_mode,
            }

            edl = edl_dict(timeline)
            self.store.write_json(project_id, "timeline/edit_decision_list.json", edl)
            self.store.write_json(project_id, f"timeline/timeline_v{version}.json", edl)
            project.timeline_version = version
            self.store.register_artifact(project, "timeline", "timeline/edit_decision_list.json")
            self.store.register_artifact(project, "captions", "captions/subtitles.srt")
            self.store.save(project)

        warnings: list[str] = []
        if not narration_duration:
            warnings.append("No narration metadata found: scene durations use the storyboard targets.")
        if timing_mode == "approximate":
            warnings.append(
                "Captions use the documented approximate-timing fallback (no TTS alignment data); "
                "verify them in the preview before approval."
            )
        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {
                "timeline": "timeline/edit_decision_list.json",
                "captions": "captions/subtitles.srt",
            },
            "data": {
                "timeline_version": version,
                "total_duration_seconds": timeline.total_duration_seconds,
                "scene_count": len(timeline.scenes),
                "caption_timing_mode": timing_mode,
                "scenes": [
                    {"scene_id": s.scene_id, "duration_seconds": s.duration_seconds, "source": s.source}
                    for s in timeline.scenes
                ],
            },
            "warnings": warnings,
            "next_action": "Run creator_render_preview to render the video with ffmpeg.",
        }

    # ----------------------------------------------------------------- render
    async def render_preview(
        self, project_id: str, *, burn_captions: Optional[bool] = None, force: bool = False
    ) -> dict[str, Any]:
        project = self.store.load(project_id)
        self._require_status(
            project,
            {
                ProjectStatus.NARRATION_READY,
                ProjectStatus.RENDERED,
                ProjectStatus.REVIEW_REQUIRED,
                ProjectStatus.APPROVED,
            },
            "render",
            "Run creator_build_timeline first; rendering is allowed from NARRATION_READY or later.",
        )
        timeline = self._load_timeline(project)
        burn = (
            bool(timeline.captions.get("burn_in", True))
            if burn_captions is None and timeline.captions
            else (True if burn_captions is None else burn_captions)
        )
        narration_path = self.store.artifact_path(project, "narration")
        runner = FFmpegRunner(
            ffmpeg_path=self.settings.render.ffmpeg_path,
            crf=self.settings.render.crf,
            preset=self.settings.render.preset,
            timeout_seconds=float(self.settings.render.timeout_seconds),
        )
        job = self.jobs.create("render", project_id)
        try:
            async with self._stage(project, "render", job):
                result = await render_project_video(
                    project_id=project_id,
                    project_dir=self.store.project_dir(project_id),
                    timeline=timeline,
                    runner=runner,
                    burn_captions=burn,
                    narration_path=narration_path,
                    job=job,
                    force=force,
                )
                final_abs = self.store.abs_path(project_id, result["final_stable"])
                validation = await media_validation.validate_render(
                    final_abs,
                    ffprobe_path=self.settings.render.ffprobe_path,
                    ffmpeg_path=self.settings.render.ffmpeg_path,
                    expected_width=timeline.width,
                    expected_height=timeline.height,
                    expected_min_duration=timeline.total_duration_seconds,
                    expected_max_duration=timeline.total_duration_seconds,
                    require_audio=True,
                )
                self.store.write_json(
                    project_id, f"renders/v{result['render_version']}/validation.json", validation
                )
                project.render_version = int(result["render_version"])
                self.store.register_artifact(project, "render", result["final_stable"])
                self.store.register_artifact(project, "preview", result["preview_stable"])
                if result.get("cover"):
                    self.store.register_artifact(project, "cover", result["cover"])
                self._transition(project, ProjectStatus.RENDERED)
                self.store.save(project)
        except CreatorError as exc:
            self.jobs.finish(job, "CANCELLED" if exc.code == ErrorCode.CANCELLED.value else "FAILED", exc.code)
            raise
        except Exception:
            self.jobs.finish(job, "FAILED", ErrorCode.INTERNAL_ERROR.value)
            raise
        self.jobs.finish(job, "DONE")
        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {
                "render": result["final_stable"],
                "preview": result["preview_stable"],
                **({"cover": result["cover"]} if result.get("cover") else {}),
            },
            "data": {
                "job_id": job.job_id,
                "render_version": result["render_version"],
                "duration_seconds": validation["probe"]["duration_seconds"],
                "dimensions": f"{timeline.width}x{timeline.height}",
                "subtitle_mode": result["subtitle_mode"],
                "validation": {
                    "ok": validation["ok"],
                    "audio_levels": validation["audio_levels"],
                },
            },
            "warnings": validation.get("warnings", []),
            "next_action": "Run creator_get_review_package to assemble the review bundle and payload.",
        }

    async def validate_render(self, project_id: str) -> dict[str, Any]:
        project = self.store.load(project_id)
        rel = project.artifacts.get("render")
        if not rel or not self.store.exists(project_id, rel):
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                "No rendered video is registered for this project.",
                stage="render",
                next_action="Run creator_render_preview first.",
            )
        timeline = self.load_timeline_safe(project)
        path = self.store.abs_path(project_id, rel)
        report = await media_validation.validate_render(
            path,
            ffprobe_path=self.settings.render.ffprobe_path,
            ffmpeg_path=self.settings.render.ffmpeg_path,
            expected_width=timeline.width if timeline else None,
            expected_height=timeline.height if timeline else None,
            expected_min_duration=timeline.total_duration_seconds if timeline else None,
            expected_max_duration=timeline.total_duration_seconds if timeline else None,
            require_audio=bool(project.artifacts.get("narration")),
        )
        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {"render": rel},
            "data": report,
            "warnings": report.get("warnings", []),
            "next_action": "Proceed to creator_get_review_package.",
        }

    def load_timeline_safe(self, project: Project) -> Optional[Timeline]:
        try:
            return self._load_timeline(project)
        except CreatorError:
            return None

    # ----------------------------------------------------------------- review
    async def get_review_package(self, project_id: str, *, page_id: Optional[str] = None) -> dict[str, Any]:
        project = self.store.load(project_id)
        self._require_status(
            project,
            {ProjectStatus.RENDERED, ProjectStatus.REVIEW_REQUIRED},
            "review",
            "Render the video first (creator_render_preview).",
        )
        storyboard = self._load_storyboard(project)
        rel = project.artifacts.get("render")
        if not rel or not self.store.exists(project_id, rel):
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                "No rendered video found for review.",
                stage="review",
                next_action="Run creator_render_preview first.",
            )
        page = str(page_id or self.settings.facebook.page_id or "")
        if not page:
            raise CreatorError(
                ErrorCode.INVALID_INPUT,
                "No target Page is available for the review package.",
                stage="review",
                next_action="Pass page_id (see creator_list_pages) or set FCA_FACEBOOK_PAGE_ID.",
            )
        media_sha = sha256_file(self.store.abs_path(project_id, rel))
        manifest = self._load_assets_manifest(project)
        attribution = stages.attribution_block(
            [AssetRecord.model_validate(a) for a in manifest.get("assets", [])]
        )
        payload = stages.build_publish_payload(
            project=project,
            storyboard=storyboard,
            page_id=page,
            media_rel=rel,
            media_sha256=media_sha,
            attribution=attribution,
        )
        payload_sha = sha256_json(payload)

        warnings = list(project.warnings[-10:])
        if self.store.exists(project_id, f"renders/v{project.render_version}/validation.json"):
            validation = self.store.read_json(project_id, f"renders/v{project.render_version}/validation.json")
            warnings.extend(validation.get("warnings", []))

        package = {
            "schema_version": 1,
            "generated_at": iso(utcnow()),
            "project_id": project_id,
            "page_id": page,
            "media": {"path": rel, "sha256": media_sha, "bytes": self.store.abs_path(project_id, rel).stat().st_size},
            "preview": project.artifacts.get("preview"),
            "script": project.artifacts.get("script"),
            "captions": project.artifacts.get("captions"),
            "payload_sha256": payload_sha,
            "payload": payload,
            "asset_manifest": {
                "asset_count": len(manifest.get("assets", [])),
                "license_summary": manifest.get("license_summary", {}),
                "attribution_block": attribution,
            },
            "warnings": warnings,
        }
        self.store.write_json(project_id, "review/publish_payload.json", payload)
        self.store.write_json(project_id, "review/review_package.json", package)
        self.store.register_artifact(project, "publish_payload", "review/publish_payload.json")
        self.store.register_artifact(project, "review_package", "review/review_package.json")
        self._transition(project, ProjectStatus.REVIEW_REQUIRED)

        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {
                "review_package": "review/review_package.json",
                "publish_payload": "review/publish_payload.json",
                **({"preview": project.artifacts["preview"]} if project.artifacts.get("preview") else {}),
            },
            "data": {
                "page_id": page,
                "media_sha256": media_sha,
                "payload_sha256": payload_sha,
                "caption": payload["caption"],
                "hashtags": payload["hashtags"],
                "attribution": attribution,
                "warnings": warnings,
            },
            "warnings": warnings,
            "next_action": (
                "Present this exact payload to the user. Only after the user explicitly approves THIS "
                "payload for THIS Page, call creator_approve_publish."
            ),
        }

    async def approve_publish(
        self,
        project_id: str,
        *,
        page_id: Optional[str] = None,
        ttl_minutes: int = 60,
    ) -> dict[str, Any]:
        project = self.store.load(project_id)
        self._require_status(
            project,
            {ProjectStatus.REVIEW_REQUIRED},
            "review",
            "Approve only after creator_get_review_package produced a fresh review bundle.",
        )
        if not self.store.exists(project_id, "review/publish_payload.json"):
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_REQUIRED,
                "No reviewed payload exists to approve.",
                stage="publish",
                next_action="Run creator_get_review_package first.",
            )
        payload = self.store.read_json(project_id, "review/publish_payload.json")
        payload_page = str(payload.get("page_id") or "")
        if page_id and str(page_id) != payload_page:
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_STALE,
                f"The reviewed payload targets Page {payload_page!r}, not {page_id!r}.",
                stage="publish",
                next_action="Re-run creator_get_review_package for the intended Page, then approve.",
            )
        rel = project.artifacts.get("render")
        if not rel or not self.store.exists(project_id, rel):
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                "Rendered media is missing; nothing to approve.",
                stage="publish",
                next_action="Re-render with creator_render_preview.",
            )
        media_sha = sha256_file(self.store.abs_path(project_id, rel))
        payload_sha = sha256_json(payload)

        approval = self.guard.create(
            project_id=project_id,
            page_id=payload_page,
            media_sha256=media_sha,
            payload_sha256=payload_sha,
            payload_snapshot=payload,
            ttl_minutes=ttl_minutes,
        )
        self._transition(project, ProjectStatus.APPROVED)
        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {"approval": "review/approval.json"},
            "data": {
                "approval_id": approval.approval_id,
                "action": approval.action,
                "page_id": approval.page_id,
                "media_sha256": media_sha,
                "payload_sha256": payload_sha,
                "expires_at": iso(approval.expires_at),
                "confirmation_mode": approval.confirmation_mode,
            },
            "warnings": [
                "This approval is single-use and bound to the exact Page, media hash, and payload hash.",
                "Any change to the media or caption invalidates it automatically.",
            ],
            "next_action": (
                "Call creator_publish_video. It runs a dry-run by default; pass dry_run=false for the "
                "real upload (requires FCA_ENABLE_LIVE_PUBLISH=1 + token)."
            ),
        }

    # ---------------------------------------------------------------- publish
    async def publish_video(
        self,
        project_id: str,
        *,
        page_id: Optional[str] = None,
        dry_run: Optional[bool] = None,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        return await self.publisher.publish_video(
            project_id, page_id=page_id, dry_run=dry_run, note=note
        )

    async def get_publish_status(self, project_id: str, *, attempt_id: Optional[str] = None) -> dict[str, Any]:
        return await self.publisher.get_publish_status(project_id, attempt_id=attempt_id)

    # ------------------------------------------------------------------- jobs
    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        cancelled = await self.jobs.cancel_job_id(job_id)
        data: dict[str, Any] = {"job_id": job_id, "cancelled": cancelled}
        if not cancelled:
            job = self.jobs.get(job_id)
            data["note"] = (
                f"Job already finished with status {job.status}." if job else "Job is not running."
            )
        return {
            "data": data,
            "warnings": [] if cancelled else ["The job was not in a cancellable RUNNING state."],
            "next_action": None if cancelled else "Check creator_doctor or re-run the producing tool.",
        }


def _license_summary(records: list[AssetRecord]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for record in records:
        key = record.license_name or "unknown"
        summary[key] = summary.get(key, 0) + 1
    return summary


def build_default_service(settings: Settings) -> CreatorService:
    return CreatorService(settings)
