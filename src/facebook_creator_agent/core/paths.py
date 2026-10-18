"""Path jail: every filesystem path used by the agent resolves inside the
workspace root, with traversal, drive-letter, reserved-name and control-char
inputs rejected (ARCHITECTURE.md §7, NFR-001)."""

from __future__ import annotations

import re
from pathlib import Path

from .errors import CreatorError, ErrorCode

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


def validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id or ""):
        raise CreatorError(
            ErrorCode.INVALID_INPUT,
            "project_id must match ^[a-z0-9][a-z0-9-]{1,48}$ (lowercase letters, digits, hyphens).",
            stage="general",
            next_action="Use the project_id returned by creator_create_project.",
        )
    return project_id


def validate_part(part: str) -> str:
    """Validate one path component (no separators, no traversal, no reserved)."""
    if not isinstance(part, str) or part in ("", ".", ".."):
        raise CreatorError(
            ErrorCode.FORBIDDEN,
            "Path segment is empty or a traversal segment; refused.",
            stage="general",
        )
    if "/" in part or "\\" in part or ":" in part:
        raise CreatorError(
            ErrorCode.FORBIDDEN,
            "Path segment contains a separator or drive designator; refused.",
            stage="general",
        )
    if _CTRL_RE.search(part):
        raise CreatorError(
            ErrorCode.FORBIDDEN, "Path segment contains control characters; refused.", stage="general"
        )
    if part.endswith((" ", ".")):
        raise CreatorError(
            ErrorCode.FORBIDDEN,
            "Path segment ends with a space or dot (unsafe on Windows); refused.",
            stage="general",
        )
    if part.split(".")[0].lower() in _WINDOWS_RESERVED:
        raise CreatorError(
            ErrorCode.FORBIDDEN, f"Path segment '{part}' is a reserved Windows name; refused.", stage="general"
        )
    if len(part) > 120:
        raise CreatorError(
            ErrorCode.FORBIDDEN, "Path segment is too long (limit 120 characters); refused.", stage="general"
        )
    return part


def safe_filename(name: str, fallback: str = "file") -> str:
    """Normalize an untrusted filename into a safe basename (no directories)."""
    base = (name or "").replace("\\", "/").split("/")[-1]
    base = _CTRL_RE.sub("", base).strip().strip(". ")
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)
    base = base.strip(". ")
    if not base:
        base = fallback
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    if stem.split(".")[0].lower() in _WINDOWS_RESERVED or stem.lower() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    stem = stem[:100].strip(". ") or fallback
    return f"{stem}.{ext[:12]}" if ext else stem[:100]


class PathJail:
    """Resolves untrusted relative paths strictly below ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def join(self, *parts: str) -> Path:
        """Join validated components under root and verify containment."""
        current = self.root
        for part in parts:
            current = current / validate_part(part)
        resolved = current.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise CreatorError(
                ErrorCode.FORBIDDEN,
                "Resolved path escapes the workspace root; refused.",
                stage="general",
            )
        return resolved

    def contains(self, path: Path) -> bool:
        resolved = Path(path).resolve()
        return resolved == self.root or self.root in resolved.parents

    def check_inside(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        if not self.contains(resolved):
            raise CreatorError(
                ErrorCode.FORBIDDEN,
                "Path is outside the workspace root; refused.",
                stage="general",
            )
        return resolved


def try_resolve_under(root: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and require it to live under ``root``."""
    resolved = Path(candidate).resolve()
    root_resolved = Path(root).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise CreatorError(
            ErrorCode.FORBIDDEN,
            "Path is outside the workspace root; refused.",
            stage="general",
        )
    return resolved
