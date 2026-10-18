"""Facebook publisher service (FR-063..FR-068, T0905..T0908).

Safety properties enforced here (never delegated to the caller):

* live publishing requires BOTH ``FCA_ENABLE_LIVE_PUBLISH=1`` and a configured
  access token; otherwise the default is dry-run, and an explicit
  ``dry_run=false`` is refused with ``CONFIG_INVALID``;
* the approval guard is verified with **fresh** media/payload hashes and the
  approval is consumed single-use before any network call;
* the attempt is journaled before the upload starts;
* ``UNKNOWN`` outcomes are never retried blindly — they must be reconciled
  through ``creator_get_publish_status``;
* a new upload with the same idempotency key is refused unless the earlier
  attempt definitively failed (or was a dry run).

Verified against a local fake Graph endpoint and mocked transports in tests;
live Meta validation is a documented blocked task (T0909) because no
credentials are available in this repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..config.settings import Settings
from ..core.errors import CreatorError, ErrorCode, error_info
from ..core.ids import iso, sha256_file, sha256_json, stable_hash, utcnow
from ..core.models import Project
from ..core.state_machine import ProjectStatus, StateMachine
from ..observability.logging import get_logger, redact_text
from ..providers.facebook.base import (
    FacebookAdapter,
    FacebookAdapterError,
    FacebookUnknownOutcome,
)
from ..workspace.project_store import ProjectStore
from .approval import ApprovalGuard
from .journal import PublishJournal

logger = get_logger("publishing.publisher")

PAYLOAD_PATH = "review/publish_payload.json"

RETRY_ACTIONS = {
    ErrorCode.RATE_LIMITED: "Wait for the rate limit to clear, then create a new approval and retry.",
    ErrorCode.PROVIDER_UNREACHABLE: "Check connectivity, then create a new approval and retry.",
    ErrorCode.FACEBOOK_AUTH_REQUIRED: (
        "Complete the official Meta authorization flow and set FCA_FACEBOOK_ACCESS_TOKEN "
        "(never commit it), then create a new approval."
    ),
    ErrorCode.FACEBOOK_PERMISSION_MISSING: (
        "Grant the missing Page permission to the app/token, then create a new approval."
    ),
}


class PublishService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ProjectStore,
        guard: ApprovalGuard,
        journal: PublishJournal,
        adapter: FacebookAdapter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.guard = guard
        self.journal = journal
        self.adapter = adapter

    # ----------------------------------------------------------------- helpers
    def _live_ready(self) -> bool:
        fb = self.settings.facebook
        return bool(fb.live_publish_enabled and self.adapter.has_credentials)

    def _media_path(self, project: Project) -> Path:
        rel = project.artifacts.get("render")
        if not rel:
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                "No rendered video is registered for this project.",
                stage="publish",
                next_action="Run creator_render_preview and creator_get_review_package first.",
            )
        path = self.store.abs_path(project.project_id, rel)
        if not path.is_file():
            raise CreatorError(
                ErrorCode.NOT_FOUND,
                f"Rendered video {rel!r} is missing from disk.",
                stage="publish",
                next_action="Re-render with creator_render_preview.",
            )
        return path

    def _payload(self, project: Project) -> dict[str, Any]:
        if not self.store.exists(project.project_id, PAYLOAD_PATH):
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_REQUIRED,
                "No reviewed publish payload exists for this project.",
                stage="publish",
                next_action=(
                    "Run creator_get_review_package, have the user review it, then "
                    "creator_approve_publish before publishing."
                ),
            )
        return self.store.read_json(project.project_id, PAYLOAD_PATH)

    @staticmethod
    def _idempotency_key(
        project_id: str, page_id: str, media_sha256: str, payload_sha256: str
    ) -> str:
        return stable_hash(
            "PUBLISH_VIDEO", project_id, page_id, media_sha256, payload_sha256, length=32
        )

    # ---------------------------------------------------------------- publish
    async def publish_video(
        self,
        project_id: str,
        *,
        page_id: Optional[str] = None,
        dry_run: Optional[bool] = None,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        project = self.store.load(project_id)
        fb = self.settings.facebook
        live_ready = self._live_ready()
        if dry_run is None:
            dry_run = not live_ready
        if dry_run is False and not live_ready:
            raise CreatorError(
                ErrorCode.CONFIG_INVALID,
                "Live publishing is disabled or no access token is configured; refusing a real upload.",
                stage="publish",
                next_action=(
                    "Set FCA_ENABLE_LIVE_PUBLISH=1 and FCA_FACEBOOK_ACCESS_TOKEN, or call "
                    "creator_publish_video with dry_run=true."
                ),
            )

        payload = self._payload(project)
        page = str(page_id or payload.get("page_id") or fb.page_id or "")
        if not page:
            raise CreatorError(
                ErrorCode.INVALID_INPUT,
                "No target Page id is available.",
                stage="publish",
                next_action="Pass page_id (see creator_list_pages) or set FCA_FACEBOOK_PAGE_ID.",
            )
        if str(payload.get("page_id") or "") != page:
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_STALE,
                "The reviewed payload was built for a different Page than the requested one.",
                stage="publish",
                next_action="Re-run creator_get_review_package with the intended page_id, then re-approve.",
            )
        media_path = self._media_path(project)
        media_sha = sha256_file(media_path)
        payload_sha = sha256_json(payload)

        # Gate order is deliberate (each gate is independent of the others):
        # 1. idempotency: an identical publication that already SUCCEEDED is
        #    answered with a replay; an unresolved attempt (STARTED/UNKNOWN)
        #    always refuses, regardless of approvals or state;
        # 2. approval guard with FRESH media/payload hashes, so a missing or
        #    stale approval is always reported as PUBLISH_APPROVAL_REQUIRED /
        #    PUBLISH_APPROVAL_STALE even when the project is not yet APPROVED;
        # 3. the APPROVED state gate, and only then any network work.
        key = self._idempotency_key(project_id, page, media_sha, payload_sha)
        prior = self.journal.last_for_key(project_id, key)
        if prior is not None:
            if prior.status == "SUCCEEDED":
                return self._already_published(project, prior)
            if prior.status in ("STARTED", "UNKNOWN"):
                raise CreatorError(
                    ErrorCode.PUBLISH_OUTCOME_UNKNOWN,
                    f"A previous attempt ({prior.attempt_id}) may have reached Meta and is not reconciled; "
                    "refusing to upload again.",
                    stage="publish",
                    retryable=False,
                    next_action="Call creator_get_publish_status to reconcile the previous attempt.",
                    details={"attempt_id": prior.attempt_id, "attempt_status": prior.status},
                )
            # FAILED (definite) or DRY_RUN: a fresh attempt is safe.
            logger.info("previous attempt %s was %s; allowing a new attempt", prior.attempt_id, prior.status)

        approval = self.guard.verify(
            project_id=project_id,
            page_id=page,
            media_sha256=media_sha,
            payload_sha256=payload_sha,
        )

        if project.status != ProjectStatus.APPROVED:
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                f"Project status is {project.status.value}; publishing requires APPROVED.",
                stage="publish",
                next_action="Run creator_get_review_package and creator_approve_publish first.",
            )

        request_summary = {
            "action": "PUBLISH_VIDEO",
            "page_id": page,
            "media": project.artifacts.get("render"),
            "media_sha256": media_sha,
            "media_bytes": media_path.stat().st_size,
            "payload_sha256": payload_sha,
            "caption_preview": str(payload.get("caption") or "")[:120],
            "mode": "dry_run" if dry_run else "live",
        }
        if note:
            request_summary["note"] = redact_text(str(note))[:200]

        if dry_run:
            attempt = self.journal.start_attempt(
                project_id=project_id,
                page_id=page,
                idempotency_key=key,
                request_summary=request_summary,
                status="DRY_RUN",
            )
            finished = self.journal.finish_attempt(
                project_id=project_id,
                attempt_id=attempt.attempt_id,
                status="DRY_RUN",
                response={"simulated": True, "would_post_to": f"{'graph-video'}/{fb.graph_version}/{page}/videos"},
            )
            logger.info("dry-run publish journaled as %s", finished.attempt_id)
            return {
                "project_id": project_id,
                "project_status": project.status.value,
                "data": {
                    "mode": "dry_run",
                    "attempt_id": finished.attempt_id,
                    "idempotency_key": key,
                    "approval_id": approval.approval_id,
                    "request_summary": request_summary,
                    "network_call_performed": False,
                },
                "warnings": [
                    "Dry run only: nothing was uploaded. The approval was verified but not consumed.",
                    "Meta live validation is not configured in this environment.",
                ],
                "next_action": (
                    "Review the summarized request; to publish for real, enable live publishing "
                    "(FCA_ENABLE_LIVE_PUBLISH=1 + token) and call creator_publish_video with dry_run=false."
                ),
            }

        # ---- live path -------------------------------------------------------
        StateMachine.ensure(project.status, ProjectStatus.PUBLISHING)
        project.status = ProjectStatus.PUBLISHING
        self.store.save(project)

        # consumed single-use BEFORE the network call; journal directly after
        self.guard.consume(approval)
        attempt = self.journal.start_attempt(
            project_id=project_id,
            page_id=page,
            idempotency_key=key,
            request_summary=request_summary,
        )

        try:
            result = await self.adapter.upload_video(
                page,
                media_path,
                payload,
                timeout_seconds=int(fb.upload_timeout_seconds),
            )
        except FacebookUnknownOutcome as exc:
            info = error_info(
                ErrorCode.PUBLISH_OUTCOME_UNKNOWN,
                redact_text(str(exc))[:300],
                stage="publish",
                retryable=False,
                next_action="Call creator_get_publish_status to reconcile; never retry blindly.",
            )
            self.journal.finish_attempt(
                project_id=project_id, attempt_id=attempt.attempt_id, status="UNKNOWN", error=info
            )
            StateMachine.ensure(project.status, ProjectStatus.FAILED)
            project.status = ProjectStatus.FAILED
            self.store.record_error(project, info)
            raise CreatorError(
                ErrorCode.PUBLISH_OUTCOME_UNKNOWN,
                info.safe_explanation,
                stage="publish",
                next_action=info.next_action,
                details={"attempt_id": attempt.attempt_id},
            ) from exc
        except (FacebookAdapterError, CreatorError) as exc:
            code = exc.code
            retryable = exc.retryable if isinstance(exc, FacebookAdapterError) else exc.retryable
            explanation = (
                exc.explanation if isinstance(exc, FacebookAdapterError) else exc.safe_explanation
            )
            next_action = RETRY_ACTIONS.get(ErrorCode(str(code)), "Resolve the error, then create a new approval and retry.")
            info = error_info(code, explanation, stage="publish", retryable=retryable, next_action=next_action)
            self.journal.finish_attempt(
                project_id=project_id, attempt_id=attempt.attempt_id, status="FAILED", error=info
            )
            target = ProjectStatus.REVIEW_REQUIRED if retryable else ProjectStatus.FAILED
            StateMachine.ensure(project.status, target)
            project.status = target
            self.store.record_error(project, info)
            raise CreatorError(
                code,
                explanation,
                stage="publish",
                retryable=retryable,
                next_action=next_action,
                details={"attempt_id": attempt.attempt_id},
            ) from exc

        finished = self.journal.finish_attempt(
            project_id=project_id,
            attempt_id=attempt.attempt_id,
            status="SUCCEEDED",
            response=result.response,
            remote_id=result.remote_id,
            reconciled=True,
        )
        StateMachine.ensure(project.status, ProjectStatus.PUBLISHED)
        project.status = ProjectStatus.PUBLISHED
        project.last_error = None
        project.artifacts["published_video_id"] = result.remote_id
        project.artifacts["published_page_id"] = page
        project.artifacts["published_at"] = iso(utcnow()) or ""
        self.store.save(project)
        logger.info("published video %s to page %s (attempt %s)", result.remote_id, page, finished.attempt_id)
        return {
            "project_id": project_id,
            "project_status": project.status.value,
            "artifacts": {"published_video_id": result.remote_id},
            "data": {
                "mode": "live",
                "attempt_id": finished.attempt_id,
                "remote_video_id": result.remote_id,
                "page_id": page,
                "response": {"id": result.remote_id},
            },
            "warnings": [],
            "next_action": "Use creator_get_publish_status to confirm Meta-side processing state.",
        }

    @staticmethod
    def _already_published(project: Project, prior: Any) -> dict[str, Any]:
        if project.status != ProjectStatus.PUBLISHED:
            try:
                StateMachine.ensure(project.status, ProjectStatus.PUBLISHING)
                StateMachine.ensure(ProjectStatus.PUBLISHING, ProjectStatus.PUBLISHED)
                project.status = ProjectStatus.PUBLISHED
            except CreatorError:
                pass  # keep whatever state is recorded; the attempt proves success
        return {
            "project_id": project.project_id,
            "project_status": project.status.value,
            "artifacts": {"published_video_id": prior.remote_id or ""},
            "data": {
                "mode": "idempotent_replay",
                "attempt_id": prior.attempt_id,
                "remote_video_id": prior.remote_id,
                "idempotency_key": prior.idempotency_key,
                "note": "An identical publication already succeeded; no new upload was performed.",
            },
            "warnings": [],
            "next_action": "No action needed; use creator_get_publish_status to re-check status.",
        }

    # -------------------------------------------------------------- reconcile
    async def get_publish_status(
        self, project_id: str, *, attempt_id: Optional[str] = None
    ) -> dict[str, Any]:
        project = self.store.load(project_id)
        attempts = self.journal.attempts(project_id)
        if not attempts:
            raise CreatorError(
                ErrorCode.NOT_FOUND,
                "No publish attempts have been journaled for this project.",
                stage="publish",
                next_action="Call creator_publish_video when the project is APPROVED.",
            )
        attempt = (
            self.journal.find(project_id, attempt_id) if attempt_id else attempts[-1]
        )
        if attempt is None:
            raise CreatorError(
                ErrorCode.NOT_FOUND,
                f"Publish attempt {attempt_id!r} is not in the journal.",
                stage="publish",
                next_action="Omit attempt_id to inspect the latest attempt.",
            )

        if attempt.status == "SUCCEEDED":
            self._sync_published_state(project, attempt.remote_id, attempt.page_id)
            return self._status_response(project, attempt, "SUCCEEDED", reconciled=True)

        if attempt.status in ("STARTED", "UNKNOWN"):
            if attempt.remote_id:
                info = await self.adapter.get_video_status(attempt.remote_id)
                if info.get("exists"):
                    finished = self.journal.finish_attempt(
                        project_id=project_id,
                        attempt_id=attempt.attempt_id,
                        status="SUCCEEDED",
                        response=info,
                        reconciled=True,
                    )
                    self._sync_published_state(project, attempt.remote_id, attempt.page_id)
                    return self._status_response(project, finished, "SUCCEEDED", reconciled=True)
                finished = self.journal.finish_attempt(
                    project_id=project_id,
                    attempt_id=attempt.attempt_id,
                    status="FAILED",
                    error=error_info(
                        ErrorCode.PUBLISH_OUTCOME_UNKNOWN,
                        "Reconciliation confirmed the video does NOT exist on the Page; the upload never completed.",
                        stage="publish",
                        retryable=True,
                        next_action="Create a new approval and publish again.",
                    ),
                    reconciled=True,
                )
                return self._status_response(
                    project, finished, "NOT_PUBLISHED", reconciled=True
                )
            return self._status_response(project, attempt, "UNKNOWN", reconciled=False)

        if attempt.status == "FAILED":
            return self._status_response(project, attempt, "FAILED", reconciled=True)
        return self._status_response(project, attempt, "DRY_RUN", reconciled=True)

    def _sync_published_state(self, project: Project, remote_id: Optional[str], page_id: str) -> None:
        changed = False
        if project.status != ProjectStatus.PUBLISHED:
            for step in (ProjectStatus.PUBLISHING, ProjectStatus.PUBLISHED):
                try:
                    StateMachine.ensure(project.status, step)
                    project.status = step
                except CreatorError:
                    continue
            changed = True
        if remote_id and project.artifacts.get("published_video_id") != remote_id:
            project.artifacts["published_video_id"] = remote_id
            changed = True
        if page_id and project.artifacts.get("published_page_id") != page_id:
            project.artifacts["published_page_id"] = page_id
            changed = True
        if changed:
            self.store.save(project)

    def _status_response(
        self, project: Project, attempt: Any, outcome: str, *, reconciled: bool
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "outcome": outcome,
            "attempt_id": attempt.attempt_id,
            "attempt_status": attempt.status,
            "reconciled": reconciled,
            "remote_video_id": attempt.remote_id,
            "page_id": attempt.page_id,
            "started_at": iso(attempt.started_at),
            "finished_at": iso(attempt.finished_at),
            "response": attempt.response,
        }
        warnings: list[str] = []
        next_action: Optional[str] = None
        if attempt.status == "UNKNOWN" and not reconciled:
            data["manual_check_required"] = True
            warnings.append(
                "The upload outcome is UNKNOWN and no remote identifier was captured; automatic "
                "reconciliation is impossible. Check the Page's video list (Meta Business Suite / "
                "Graph API explorer) before retrying."
            )
            next_action = (
                "Verify the Page has no video for this attempt; if it does not, re-approve and "
                "retry. Never upload twice."
            )
        elif outcome == "SUCCEEDED":
            next_action = "Publication confirmed."
        elif outcome == "NOT_PUBLISHED":
            next_action = "Safe to re-approve (creator_approve_publish) and publish again."
        elif outcome == "FAILED":
            if attempt.error:
                next_action = attempt.error.next_action
            else:
                next_action = "Review the error, create a new approval, and retry."
        elif outcome == "DRY_RUN":
            next_action = "Dry run only; publish for real after enabling live publishing."
        return {
            "project_id": project.project_id,
            "project_status": project.status.value,
            "artifacts": (
                {"published_video_id": attempt.remote_id} if attempt.remote_id else {}
            ),
            "data": data,
            "warnings": warnings,
            "next_action": next_action,
        }
