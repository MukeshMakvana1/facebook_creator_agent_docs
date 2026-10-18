"""In-process job registry: bounded concurrency, cancellation, subprocess
termination (NFR-006, FR-082, creator_cancel_job)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .errors import CreatorError, ErrorCode
from .ids import new_id, utcnow


@dataclass
class Job:
    job_id: str
    kind: str
    project_id: Optional[str]
    status: str = "RUNNING"  # RUNNING | DONE | FAILED | CANCELLED
    created_at: datetime = field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    process: Optional[Any] = None  # asyncio.subprocess.Process while running
    progress: Optional[str] = None
    error_code: Optional[str] = None

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class JobRegistry:
    """Per-service job table. One event loop (the MCP server's) owns it."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self, kind: str, project_id: Optional[str], job_id: Optional[str] = None) -> Job:
        job = Job(job_id=job_id or new_id("job"), kind=kind, project_id=project_id)
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def running_for_project(self, project_id: str) -> list[Job]:
        return [j for j in self._jobs.values() if j.project_id == project_id and j.status == "RUNNING"]

    def finish(self, job: Job, status: str, error_code: Optional[str] = None) -> None:
        job.status = status
        job.error_code = error_code
        job.finished_at = utcnow()

    async def cancel(self, job: Job) -> bool:
        """Signal cancellation and terminate any running subprocess."""
        if job.status != "RUNNING":
            return False
        job.cancel_event.set()
        proc = job.process
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
            except ProcessLookupError:
                pass
        return True

    async def cancel_job_id(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            raise CreatorError(
                ErrorCode.NOT_FOUND,
                f"No job with id {job_id!r} is known to this server instance.",
                stage="general",
                next_action="Call creator_render_preview first; job ids are ephemeral per server session.",
            )
        return await self.cancel(job)
