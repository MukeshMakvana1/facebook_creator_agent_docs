"""Structured logging with mandatory secret redaction (FR-008, NFR-004).

Rules enforced here:
* stderr + optional project log file only — stdout belongs to the MCP protocol.
* every record passes through :class:`RedactionFilter`, which removes
  registered secret values (exact match) and token-like patterns (regex),
  including ``access_token=...`` query parameters and Authorization values.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

REDACTED = "***REDACTED***"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # access_token / api_key / token / secret / password in URLs or k=v pairs
    re.compile(
        r"(?i)\b(access_token|api[_-]?key|apikey|token|secret|password|client_secret)"
        r"(\s*[=:]\s*)([^&\s\"',]{4,})"
    ),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{8,}"),
    # Facebook-style long-lived tokens (EAA...) and OpenAI-style keys (sk-...)
    re.compile(r"\bEAA[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
)

_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register an exact secret value to redact from all log output."""
    if value and isinstance(value, str) and len(value) >= 6:
        _registered_secrets.add(value)


def clear_registered_secrets() -> None:
    _registered_secrets.clear()


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for secret in _registered_secrets:
        if secret in out:
            out = out.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        out = pattern.sub(lambda m: _sub(m), out)
    return out


def _sub(match: re.Match[str]) -> str:
    groups = match.groups()
    if len(groups) == 3 and groups[0] and groups[2]:
        return f"{groups[0]}{groups[1] or '='}{REDACTED}"
    if len(groups) >= 1 and groups[0] and len(groups) == 2:
        return f"{groups[0]} {REDACTED}"
    return REDACTED


def sanitize_url(url: str) -> str:
    """Strip credential query parameters from a URL for logs/errors."""
    return redact_text(url)


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def configure_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
    *,
    force: bool = False,
) -> None:
    """Configure process logging: human-readable stderr + JSON file (optional)."""
    global _configured
    root = logging.getLogger()
    if _configured and not force:
        if log_file is not None:
            _attach_file_handler(root, log_file)
        return
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    stream.addFilter(RedactionFilter())
    root.addHandler(stream)
    if log_file is not None:
        _attach_file_handler(root, log_file)
    _configured = True


def _attach_file_handler(root: logging.Logger, log_file: Path) -> None:
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    marker = str(log_file)
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == str(
            log_file.resolve()
        ):
            return
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(RedactionFilter())
    file_handler._fca_marker = marker  # type: ignore[attr-defined]
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"facebook_creator_agent.{name}")


def log_event(logger: logging.Logger, message: str, **fields: object) -> None:
    """Structured event: extra fields end up in the JSON file log."""
    logger.info(message, extra={"extra_fields": {k: _jsonable(v) for k, v in fields.items()}})


def _jsonable(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return redact_text(str(value))
