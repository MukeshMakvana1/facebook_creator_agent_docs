"""Resume/consistency checks (NFR-002, T1103 support).

Each pipeline stage writes its artifacts and the project manifest records the
status.  Before resuming at any stage the orchestrator (and ``creator_doctor``)
can verify that the artifacts implied by the recorded status actually exist —
so a resume never silently continues from a half-written state.
"""

from __future__ import annotations

from ..core.models import Project
from ..core.state_machine import ProjectStatus
from ..workspace.project_store import ProjectStore

#: status -> artifact names that must exist for that status to be trustworthy
REQUIRED_ARTIFACTS: dict[ProjectStatus, tuple[str, ...]] = {
    ProjectStatus.SCRIPT_READY: ("script", "storyboard"),
    ProjectStatus.ASSETS_READY: ("script", "storyboard", "assets_manifest"),
    ProjectStatus.NARRATION_READY: ("script", "storyboard", "assets_manifest", "narration"),
    ProjectStatus.RENDERED: ("script", "storyboard", "assets_manifest", "narration", "timeline", "render"),
    ProjectStatus.REVIEW_REQUIRED: (
        "script",
        "storyboard",
        "assets_manifest",
        "narration",
        "timeline",
        "render",
        "publish_payload",
    ),
    ProjectStatus.APPROVED: (
        "script",
        "storyboard",
        "assets_manifest",
        "narration",
        "timeline",
        "render",
        "publish_payload",
    ),
}

ORDER = [
    ProjectStatus.NEW,
    ProjectStatus.SCRIPT_READY,
    ProjectStatus.ASSETS_READY,
    ProjectStatus.NARRATION_READY,
    ProjectStatus.RENDERED,
    ProjectStatus.REVIEW_REQUIRED,
    ProjectStatus.APPROVED,
]


def verify_stage_artifacts(store: ProjectStore, project: Project) -> list[str]:
    """Return human-readable problems with the recorded status vs disk state."""
    status = project.status
    if status not in REQUIRED_ARTIFACTS:
        return []
    issues: list[str] = []
    for name in REQUIRED_ARTIFACTS[status]:
        relpath = project.artifacts.get(name)
        if not relpath:
            issues.append(f"status {status.value} but artifact {name!r} is not registered")
            continue
        if not store.exists(project.project_id, relpath):
            issues.append(f"status {status.value} but artifact {name!r} ({relpath}) is missing on disk")
    return issues
