"""User-configured command TTS adapter (FR-041).

The command is a JSON argv array from ``FCA_TTS_COMMAND_JSON``, e.g.::

    FCA_TTS_COMMAND_JSON=["python","tools/piper_tts.py","--voice","en_US-amy"]

Placeholders substituted per token: ``{text_file}`` (UTF-8 text written to a
temp file), ``{output}`` (target audio path), ``{voice}``, ``{language}``.
If no token contains ``{output}``/``{text_file}``, ``<text_file> <output>`` are
appended as the last two arguments.

Executed with ``asyncio.create_subprocess_exec`` — never a shell, so user text
can never be interpreted as a command (ARCHITECTURE.md §7).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Optional

from ...core.errors import CreatorError, ErrorCode
from ...observability.logging import get_logger, redact_text
from .base import TTSAdapter

logger = get_logger("providers.tts")


def parse_command(command_json: Optional[str]) -> Optional[list[str]]:
    if not command_json:
        return None
    try:
        parsed = json.loads(command_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not parsed or not all(isinstance(t, str) and t for t in parsed):
        return None
    return parsed


class CommandTTSAdapter(TTSAdapter):
    provider_id = "command"

    def __init__(self, command: Optional[list[str]], *, output_extension: str = "wav", timeout_seconds: int = 120) -> None:
        self.command = command
        self.output_extension = output_extension.lstrip(".")
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.command)

    def capabilities(self) -> dict[str, Any]:
        return {
            "engine": "command",
            "kind": "local_or_configured",
            "alignment_timings": False,
            "output_extension": self.output_extension,
            "note": "Capability metadata is declarative; verify the configured engine manually (T0602).",
        }

    async def generate(
        self,
        text: str,
        out_path: Path,
        *,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ) -> dict[str, Any]:
        if not self.command:
            raise CreatorError(
                ErrorCode.TTS_UNAVAILABLE,
                "No TTS command is configured.",
                stage="narration",
                next_action="Set FCA_TTS_COMMAND_JSON (argv JSON array) or pass narration_path with a manual audio file.",
            )
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8", dir=str(out_path.parent)
        ) as fh:
            fh.write(text)
            text_file = Path(fh.name)

        args: list[str] = []
        used_output = False
        used_text = False
        for token in self.command:
            substituted = (
                token.replace("{text_file}", str(text_file))
                .replace("{output}", str(out_path))
                .replace("{voice}", voice or "")
                .replace("{language}", language or "")
            )
            if "{output}" in token:
                used_output = True
            if "{text_file}" in token:
                used_text = True
            args.append(substituted)
        if not used_text and not used_output:
            args.extend([str(text_file), str(out_path)])
        elif not used_output:
            args.append(str(out_path))
        elif not used_text:
            args.append(str(text_file))

        logger.info("running TTS command: %s ... (%d args)", args[0] if args else "?", len(args))
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise CreatorError(
                    ErrorCode.TTS_UNAVAILABLE,
                    f"TTS command timed out after {self.timeout_seconds}s.",
                    stage="narration",
                    retryable=True,
                    next_action="Check the configured engine; raise FCA_TTS timeout if it is slow.",
                )
        except FileNotFoundError:
            raise CreatorError(
                ErrorCode.TTS_UNAVAILABLE,
                f"TTS command executable not found: {args[0]!r}.",
                stage="narration",
                next_action="Fix FCA_TTS_COMMAND_JSON (first token must be an existing executable).",
            )
        finally:
            try:
                text_file.unlink()
            except OSError:
                pass

        if proc.returncode != 0:
            tail = redact_text((stderr or b"").decode("utf-8", "replace"))[-400:]
            raise CreatorError(
                ErrorCode.TTS_UNAVAILABLE,
                f"TTS command failed with exit code {proc.returncode}. stderr tail: {tail}",
                stage="narration",
                next_action="Run the configured TTS command manually to debug it.",
            )
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise CreatorError(
                ErrorCode.TTS_UNAVAILABLE,
                "TTS command exited 0 but produced no audio file.",
                stage="narration",
                next_action="Verify the command writes to the {output} path.",
            )
        return {
            "provider": self.provider_id,
            "command_head": args[0] if args else None,
            "voice": voice,
            "language": language,
        }
