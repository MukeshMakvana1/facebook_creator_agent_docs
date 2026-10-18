"""TTS adapter contract (FR-040..FR-042).

Separate from the LLM adapter by design: an OpenAI-compatible chat endpoint
does not imply TTS (PRD §7.2).  Adapters expose capability metadata and produce
an audio file at a requested output path; audio validation (decodable, plausible
duration) happens in the media pipeline before rendering.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional


class TTSAdapter(ABC):
    provider_id: str = "tts"

    @property
    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    async def generate(
        self,
        text: str,
        out_path: Path,
        *,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ) -> dict[str, Any]:
        """Synthesize ``text`` into ``out_path``. Returns generation metadata."""

    @abstractmethod
    def capabilities(self) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "configured": self.configured,
            "capabilities": self.capabilities(),
        }
