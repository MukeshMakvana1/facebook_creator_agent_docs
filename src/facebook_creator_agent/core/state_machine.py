"""Project workflow state machine (ARCHITECTURE.md §6).

Implements the transition graph from the architecture diagram, plus two
documented implementation additions required by the same section:

* ``RENDERED -> RENDERED`` (idempotent re-render) and ``APPROVED -> RENDERED``
  (a revision after approval invalidates the approval).
* ``FAILED -> <stage state>`` resume edges so an interrupted or failed project
  can resume from the last validated artifact instead of restarting.

Transient (retryable) stage errors do NOT move a project to FAILED; the status
is kept and ``last_error`` records the retryable error.
"""

from __future__ import annotations

from enum import Enum

from .errors import CreatorError, ErrorCode


class ProjectStatus(str, Enum):
    NEW = "NEW"
    SCRIPT_READY = "SCRIPT_READY"
    ASSETS_READY = "ASSETS_READY"
    NARRATION_READY = "NARRATION_READY"
    RENDERED = "RENDERED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TRANSITIONS: dict[ProjectStatus, set[ProjectStatus]] = {
    ProjectStatus.NEW: {ProjectStatus.SCRIPT_READY, ProjectStatus.FAILED, ProjectStatus.CANCELLED},
    ProjectStatus.SCRIPT_READY: {ProjectStatus.ASSETS_READY, ProjectStatus.FAILED, ProjectStatus.CANCELLED},
    ProjectStatus.ASSETS_READY: {ProjectStatus.NARRATION_READY, ProjectStatus.FAILED, ProjectStatus.CANCELLED},
    ProjectStatus.NARRATION_READY: {ProjectStatus.RENDERED, ProjectStatus.FAILED, ProjectStatus.CANCELLED},
    ProjectStatus.RENDERED: {
        ProjectStatus.REVIEW_REQUIRED,
        ProjectStatus.RENDERED,  # idempotent re-render
        ProjectStatus.FAILED,
        ProjectStatus.CANCELLED,
    },
    ProjectStatus.REVIEW_REQUIRED: {ProjectStatus.RENDERED, ProjectStatus.APPROVED, ProjectStatus.FAILED},
    ProjectStatus.APPROVED: {ProjectStatus.PUBLISHING, ProjectStatus.RENDERED},
    ProjectStatus.PUBLISHING: {
        ProjectStatus.PUBLISHED,
        ProjectStatus.FAILED,
        ProjectStatus.REVIEW_REQUIRED,  # payload changed / approval invalid
    },
    ProjectStatus.PUBLISHED: set(),
    ProjectStatus.FAILED: {  # resume edges (documented addition)
        ProjectStatus.SCRIPT_READY,
        ProjectStatus.ASSETS_READY,
        ProjectStatus.NARRATION_READY,
        ProjectStatus.RENDERED,
        ProjectStatus.REVIEW_REQUIRED,
        ProjectStatus.PUBLISHED,  # reconciliation confirmed a previously-UNKNOWN upload succeeded
    },
    ProjectStatus.CANCELLED: set(),
}

#: States the user may cancel directly (ARCHITECTURE.md §6 cancelled edges).
CANCELLABLE_STATES = frozenset(
    {
        ProjectStatus.NEW,
        ProjectStatus.SCRIPT_READY,
        ProjectStatus.ASSETS_READY,
        ProjectStatus.NARRATION_READY,
        ProjectStatus.RENDERED,
    }
)


class StateMachine:
    """Pure transition logic; project mutation happens in the store/service."""

    @staticmethod
    def can_transition(src: ProjectStatus | str, dst: ProjectStatus | str) -> bool:
        src_s = ProjectStatus(src)
        dst_s = ProjectStatus(dst)
        if src_s == dst_s:
            return True  # idempotent no-op
        return dst_s in TRANSITIONS.get(src_s, set())

    @staticmethod
    def ensure(src: ProjectStatus | str, dst: ProjectStatus | str) -> None:
        if not StateMachine.can_transition(src, dst):
            raise CreatorError(
                ErrorCode.INVALID_STATE,
                f"Transition {src} -> {dst} is not part of the workflow state machine.",
                stage="general",
                next_action="Run the workflow stages in order; check the project status first.",
            )

    @staticmethod
    def is_cancellable(status: ProjectStatus | str) -> bool:
        return ProjectStatus(status) in CANCELLABLE_STATES
