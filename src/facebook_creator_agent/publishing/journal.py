"""Publish attempt journal (FR-067/FR-068, T0906).

The attempt is written **before** the external side effect starts, so an
interrupted server never loses the fact that an upload may have been sent.
Statuses follow :class:`PublishAttempt`:

* ``STARTED``   — journaled; the request is about to be sent (or was, if the
  process died mid-flight — treated as possibly-sent, i.e. like ``UNKNOWN``);
* ``SUCCEEDED`` — Meta confirmed an id;
* ``FAILED``    — definite failure (never sent / rejected);
* ``UNKNOWN``   — sent, outcome unconfirmed (timeout/5xx after send);
* ``DRY_RUN``   — simulated; nothing was sent.

The idempotency key is a stable hash of (action, project, page, media sha,
payload sha).  A new attempt with a key whose previous attempt is
``SUCCEEDED``/``UNKNOWN``/``STARTED`` must not blindly re-upload — the
publisher enforces that policy, the journal only records.
"""

from __future__ import annotations

from typing import Optional

from ..core.errors import CreatorError, ErrorCode, ErrorInfo
from ..core.ids import new_id, utcnow
from ..core.models import PublishAttempt
from ..workspace.project_store import ProjectStore

ATTEMPTS_PATH = "review/publish_attempts.json"


class PublishJournal:
    def __init__(self, store: ProjectStore) -> None:
        self.store = store

    # ------------------------------------------------------------------ files
    def _load(self, project_id: str) -> list[PublishAttempt]:
        if not self.store.exists(project_id, ATTEMPTS_PATH):
            return []
        raw = self.store.read_json(project_id, ATTEMPTS_PATH)
        return [PublishAttempt.model_validate(item) for item in raw.get("attempts", [])]

    def _save(self, project_id: str, attempts: list[PublishAttempt]) -> None:
        self.store.write_json(
            project_id,
            ATTEMPTS_PATH,
            {"schema_version": 1, "attempts": [a.model_dump(mode="json") for a in attempts]},
        )

    # ------------------------------------------------------------------- api
    def attempts(self, project_id: str) -> list[PublishAttempt]:
        return self._load(project_id)

    def find(self, project_id: str, attempt_id: str) -> Optional[PublishAttempt]:
        for attempt in self._load(project_id):
            if attempt.attempt_id == attempt_id:
                return attempt
        return None

    def latest(self, project_id: str) -> Optional[PublishAttempt]:
        attempts = self._load(project_id)
        return attempts[-1] if attempts else None

    def last_for_key(self, project_id: str, idempotency_key: str) -> Optional[PublishAttempt]:
        for attempt in reversed(self._load(project_id)):
            if attempt.idempotency_key == idempotency_key:
                return attempt
        return None

    def start_attempt(
        self,
        *,
        project_id: str,
        page_id: str,
        idempotency_key: str,
        request_summary: dict,
        status: str = "STARTED",
    ) -> PublishAttempt:
        """Journal an attempt BEFORE the side effect. Returns the record."""
        attempts = self._load(project_id)
        attempt = PublishAttempt(
            attempt_id=new_id("pat"),
            idempotency_key=idempotency_key,
            project_id=project_id,
            page_id=str(page_id),
            status=status,  # type: ignore[arg-type]
            started_at=utcnow(),
            request_summary=request_summary,
        )
        attempts.append(attempt)
        self._save(project_id, attempts)
        return attempt

    def finish_attempt(
        self,
        *,
        project_id: str,
        attempt_id: str,
        status: str,
        response: Optional[dict] = None,
        remote_id: Optional[str] = None,
        error: Optional[ErrorInfo] = None,
        reconciled: bool = False,
    ) -> PublishAttempt:
        attempts = self._load(project_id)
        for attempt in attempts:
            if attempt.attempt_id == attempt_id:
                attempt.status = status  # type: ignore[assignment]
                attempt.finished_at = utcnow()
                if response is not None:
                    attempt.response = response
                if remote_id is not None:
                    attempt.remote_id = remote_id
                if error is not None:
                    attempt.error = error
                attempt.reconciled = reconciled
                self._save(project_id, attempts)
                return attempt
        raise CreatorError(
            ErrorCode.NOT_FOUND,
            f"Publish attempt {attempt_id!r} is not in the journal.",
            stage="publish",
            next_action="Read creator_get_publish_status to list known attempts.",
        )
