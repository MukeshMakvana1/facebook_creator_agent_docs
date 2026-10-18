"""Publish approval guard (FR-065/FR-066, T0802..T0805).

An approval is bound to the exact publication: project, target Page, rendered
media SHA-256, canonical payload SHA-256, action, expiry, and a one-time
nonce.  The publisher verifies it independently — no caller (including an AI
model) can create a valid approval by asserting that the user approved.

Rules enforced here:

* verification re-computes nothing on trust — the caller passes fresh
  observations (media hash, payload hash, page) and mismatch is fatal;
* approvals are single-use: :meth:`ApprovalGuard.consume` stamps
  ``consumed_at``; a consumed approval fails verification as a replay;
* expiry is enforced at verification time;
* every create/consume is appended to an audit log
  (``review/approvals_log.jsonl``).

Files (inside the project workspace, never secrets):

* ``review/approval.json`` — the latest approval record;
* ``review/approvals_log.jsonl`` — append-only audit trail.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from ..core.errors import CreatorError, ErrorCode
from ..core.ids import iso, new_id, new_nonce, utcnow
from ..core.models import PublishApproval
from ..observability.logging import get_logger
from ..workspace.project_store import ProjectStore

logger = get_logger("publishing.approval")

APPROVAL_PATH = "review/approval.json"
APPROVAL_LOG_PATH = "review/approvals_log.jsonl"
APPROVED_PAYLOAD_PATH = "review/approved_payload.json"


class ApprovalGuard:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    # ------------------------------------------------------------------ files
    def load(self, project_id: str) -> Optional[PublishApproval]:
        if not self.store.exists(project_id, APPROVAL_PATH):
            return None
        try:
            return PublishApproval.model_validate(self.store.read_json(project_id, APPROVAL_PATH))
        except CreatorError:
            return None

    def _save(self, approval: PublishApproval) -> None:
        self.store.write_json(approval.project_id, APPROVAL_PATH, approval.model_dump(mode="json"))
        self._append_log(
            {
                "event": "created",
                "approval_id": approval.approval_id,
                "project_id": approval.project_id,
                "page_id": approval.page_id,
                "media_sha256": approval.media_sha256,
                "payload_sha256": approval.payload_sha256,
                "expires_at": iso(approval.expires_at),
                "at": iso(utcnow()),
            }
        )

    def _append_log(self, entry: dict) -> None:
        import json as _json

        path = self.store.abs_path(entry["project_id"], APPROVAL_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")

    # ----------------------------------------------------------------- create
    def create(
        self,
        *,
        project_id: str,
        page_id: str,
        media_sha256: str,
        payload_sha256: str,
        payload_snapshot: dict,
        ttl_minutes: int = 60,
        confirmation_mode: str = "caller",
    ) -> PublishApproval:
        """Record an explicit user approval for one exact payload."""
        now = utcnow()
        approval = PublishApproval(
            approval_id=new_id("apr"),
            project_id=project_id,
            page_id=str(page_id),
            action="PUBLISH_VIDEO",
            media_sha256=media_sha256,
            payload_sha256=payload_sha256,
            created_at=now,
            expires_at=now + timedelta(minutes=max(5, min(int(ttl_minutes), 24 * 60))),
            nonce=new_nonce(),
            confirmation_mode=confirmation_mode,  # type: ignore[arg-type]
            approved_payload_path=APPROVED_PAYLOAD_PATH,
        )
        self.store.write_json(project_id, APPROVED_PAYLOAD_PATH, payload_snapshot)
        self._save(approval)
        logger.info(
            "approval created: %s (project=%s, expires=%s)",
            approval.approval_id,
            project_id,
            iso(approval.expires_at),
        )
        return approval

    # ----------------------------------------------------------------- verify
    def verify(
        self,
        *,
        project_id: str,
        page_id: str,
        media_sha256: str,
        payload_sha256: str,
        action: str = "PUBLISH_VIDEO",
        approval: Optional[PublishApproval] = None,
    ) -> PublishApproval:
        """Verify an approval against fresh observations. Raises on any mismatch."""
        approval = approval or self.load(project_id)
        if approval is None:
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_REQUIRED,
                "No publish approval exists for this project.",
                stage="publish",
                next_action=(
                    "Run creator_get_review_package, let the user review it, then call "
                    "creator_approve_publish for the exact Page and payload."
                ),
            )
        if approval.consumed_at is not None:
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_STALE,
                "This approval was already consumed; approvals are single-use (replay rejected).",
                stage="publish",
                next_action="Create a new approval with creator_approve_publish before publishing again.",
            )
        if utcnow() >= approval.expires_at:
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_STALE,
                f"The approval expired at {iso(approval.expires_at)}.",
                stage="publish",
                next_action="Create a new approval with creator_approve_publish.",
            )
        if approval.action != action:
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_STALE,
                f"The approval is bound to action {approval.action!r}, not {action!r}.",
                stage="publish",
                next_action="Create a new approval for the requested action.",
            )
        if str(approval.page_id) != str(page_id):
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_STALE,
                f"The approval is bound to Page {approval.page_id!r}, not {page_id!r}.",
                stage="publish",
                next_action="Create a new approval for the intended Page.",
            )
        if approval.media_sha256 != media_sha256:
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_STALE,
                "The rendered media changed after approval (SHA-256 mismatch); approval is invalid.",
                stage="publish",
                next_action="Re-review the new render, then create a new approval.",
            )
        if approval.payload_sha256 != payload_sha256:
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_STALE,
                "The caption/metadata payload changed after approval (SHA-256 mismatch); approval is invalid.",
                stage="publish",
                next_action="Re-review the current payload, then create a new approval.",
            )
        return approval

    # ---------------------------------------------------------------- consume
    def consume(self, approval: PublishApproval) -> PublishApproval:
        """Mark the approval as used (single-use). Idempotent-safe: raises on reuse."""
        if approval.consumed_at is not None:
            raise CreatorError(
                ErrorCode.PUBLISH_APPROVAL_STALE,
                "Approval already consumed; refusing to consume twice.",
                stage="publish",
            )
        approval.consumed_at = utcnow()
        self.store.write_json(
            approval.project_id, APPROVAL_PATH, approval.model_dump(mode="json")
        )
        self._append_log(
            {
                "event": "consumed",
                "approval_id": approval.approval_id,
                "project_id": approval.project_id,
                "at": iso(approval.consumed_at),
            }
        )
        return approval
