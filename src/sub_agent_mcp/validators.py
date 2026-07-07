"""Input validation. Enforces SUBAGENT_{READ,WRITE}_ROOTS as the outer FS boundary.

Read and write boundaries are split so a policy like "may read everything under
my work tree, may only write into a scratch dir" can be expressed.
SUBAGENT_ALLOWED_ROOTS is kept as a deprecated fallback applied to both read
and write."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal


class ValidationError(Exception):
    """Raised when an input fails validation. Becomes a structured MCP error."""


_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
_MODEL_RE = re.compile(r"^[a-z0-9_-]+/[a-z0-9._-]+$", re.IGNORECASE)

PathKind = Literal["read", "write"]


def _parse_roots(env_value: str) -> list[Path]:
    return [
        Path(p.strip()).expanduser().resolve()
        for p in env_value.split(":")
        if p.strip()
    ]


def _legacy_roots() -> list[Path]:
    return _parse_roots(os.environ.get("SUBAGENT_ALLOWED_ROOTS", "").strip())


def read_roots() -> list[Path]:
    """Roots a sub-agent may read from. Falls back to SUBAGENT_ALLOWED_ROOTS."""
    explicit = _parse_roots(os.environ.get("SUBAGENT_READ_ROOTS", "").strip())
    return explicit or _legacy_roots()


def write_roots() -> list[Path]:
    """Roots a sub-agent may write to. Falls back to SUBAGENT_ALLOWED_ROOTS."""
    explicit = _parse_roots(os.environ.get("SUBAGENT_WRITE_ROOTS", "").strip())
    return explicit or _legacy_roots()


def allowed_roots() -> list[Path]:
    """Backcompat: union of read+write roots. Prefer read_roots() / write_roots()."""
    seen: set[Path] = set()
    result: list[Path] = []
    for r in read_roots() + write_roots():
        if r not in seen:
            seen.add(r)
            result.append(r)
    return result


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_path(
    raw: str,
    *,
    must_be_dir: bool = False,
    must_exist: bool = True,
    kind: PathKind = "read",
) -> Path:
    """Validate an absolute path: regex, no traversal, real-path-resolved, under allowed roots.

    `kind` selects which root list applies:
      - "read"  → SUBAGENT_READ_ROOTS  (or ALLOWED_ROOTS fallback)
      - "write" → SUBAGENT_WRITE_ROOTS (or ALLOWED_ROOTS fallback)
    """
    if not raw:
        raise ValidationError("empty path")
    if not _PATH_RE.match(raw):
        raise ValidationError(
            f"path must match {_PATH_RE.pattern!r}; got {raw!r}. "
            "Special characters not allowed."
        )
    if ".." in raw.split("/"):
        raise ValidationError(f"path may not contain '..' segments: {raw}")

    resolved = Path(raw).expanduser().resolve()

    if must_exist and not resolved.exists():
        raise ValidationError(f"path does not exist: {resolved}")
    if must_be_dir and resolved.exists() and not resolved.is_dir():
        raise ValidationError(f"path is not a directory: {resolved}")

    roots = read_roots() if kind == "read" else write_roots()
    env_name = "SUBAGENT_READ_ROOTS" if kind == "read" else "SUBAGENT_WRITE_ROOTS"
    if not roots:
        raise ValidationError(
            f"{env_name} (and SUBAGENT_ALLOWED_ROOTS fallback) is unset; "
            f"no {kind} path arguments allowed. Configure roots in your MCP server env, "
            "or call without read_dir/write_dir/context_files."
        )
    if not any(_is_under(resolved, r) for r in roots):
        raise ValidationError(
            f"{kind} path {resolved} is not under any {env_name} entry "
            f"({[str(r) for r in roots]})"
        )
    return resolved


def validate_model(raw: str) -> str:
    """Validate provider/model identifier."""
    if not _MODEL_RE.match(raw):
        raise ValidationError(
            f"model must match {_MODEL_RE.pattern!r}; got {raw!r}. "
            "Expected format: 'provider/model-name'."
        )
    return raw


def validate_context_files(paths: list[str]) -> list[Path]:
    """Each must be an existing regular file under SUBAGENT_READ_ROOTS."""
    out: list[Path] = []
    for p in paths:
        resolved = validate_path(p, must_be_dir=False, must_exist=True, kind="read")
        if not resolved.is_file():
            raise ValidationError(f"context_file is not a regular file: {resolved}")
        out.append(resolved)
    return out


def validate_task(task: str, *, max_bytes: int = 64 * 1024) -> str:
    """Task prompt: non-empty, length-capped."""
    if not task or not task.strip():
        raise ValidationError("task is required and may not be empty")
    if len(task.encode("utf-8")) > max_bytes:
        raise ValidationError(f"task exceeds {max_bytes} bytes")
    return task
