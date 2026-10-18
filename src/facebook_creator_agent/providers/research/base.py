"""Research adapter interface (ARCHITECTURE.md §4.4).

Optional search/retrieval provider.  If not configured, script generation must
label research as unavailable and never fabricate citations (FR-022/FR-024).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ResearchResult(BaseModel):
    question_or_topic: str
    status: str  # "available" | "unavailable"
    sources: list[dict]  # {url, title, retrieved_at, snippet}
    note: Optional[str] = None


class ResearchAdapter(ABC):
    provider_id: str = "research"

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    async def research(self, topic: str) -> ResearchResult: ...

    def status(self) -> dict:
        return {"provider": self.provider_id, "configured": self.configured}


class UnavailableResearch(ResearchAdapter):
    """Default: research is simply not configured; this is not an error."""

    provider_id = "research_unavailable"

    def __init__(self, reason: str = "no research provider configured") -> None:
        self._reason = reason

    @property
    def configured(self) -> bool:
        return False

    async def research(self, topic: str) -> ResearchResult:
        return ResearchResult(
            question_or_topic=topic,
            status="unavailable",
            sources=[],
            note=(
                "No research provider is configured. Script generation must treat all "
                "factual claims as unverified and must not include citations."
            ),
        )


def build_research(settings) -> ResearchAdapter:  # noqa: ANN001
    # MVP: no first-party research provider is implemented (T0401/T0402 keep the
    # interface; wiring a concrete provider is a documented next step).
    return UnavailableResearch()
