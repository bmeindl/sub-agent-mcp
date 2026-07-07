"""Task-id generation, results directory resolution, result extraction."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RESULTS_DIR = Path.home() / "Documents" / "ai-workspace" / "sub-results"


def results_root() -> Path:
    """Where task-id folders live. Server-managed metadata, not agent-accessible."""
    env = os.environ.get("SUBAGENT_RESULTS_DIR", "").strip()
    root = Path(env) if env else DEFAULT_RESULTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_task_id() -> str:
    """Timestamp + 6-hex random. Sortable, collision-resistant for parallel spawns."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{secrets.token_hex(3)}"


def task_dir(task_id: str) -> Path:
    """Get (and create) the per-task metadata directory."""
    d = results_root() / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def extract_text(result_json_path: Path) -> str:
    """Concat all assistant text events from opencode's NDJSON output."""
    if not result_json_path.exists():
        return ""
    chunks: list[str] = []
    for line in result_json_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "text":
            text = event.get("part", {}).get("text", "")
            if text:
                chunks.append(text)
    return "".join(chunks)


def extract_error(result_json_path: Path) -> str:
    """Return the first error message found in opencode's NDJSON, or empty string."""
    if not result_json_path.exists():
        return ""
    for line in result_json_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "error":
            err = event.get("error", {})
            data = err.get("data", {}) if isinstance(err, dict) else {}
            msg = data.get("message") or err.get("name") or "unknown error"
            return str(msg)
    return ""


def extract_cost(result_json_path: Path) -> float:
    """Sum cost from any step_finish events in opencode's NDJSON output."""
    if not result_json_path.exists():
        return 0.0
    total = 0.0
    for line in result_json_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "step_finish":
            cost = event.get("part", {}).get("cost", 0)
            if isinstance(cost, (int, float)):
                total += float(cost)
    return total


def list_files_written(write_dir: Path | None, since: float | None = None) -> list[str]:
    """List files in write_dir whose mtime is >= `since` (sub-agent's started_at).

    The earlier implementation listed every pre-existing file under write_dir
    (often thousands). With `since` set, we report only files actually touched
    during the sub-agent's run — including files merely modified, since opencode
    edits and creates are both meaningful "writes" from the orchestrator's view.

    Without `since`, falls back to listing everything (kept for callers that
    don't have started_at, e.g. ad-hoc inspection scripts).
    """
    if not write_dir or not write_dir.exists():
        return []
    if since is None:
        return sorted(str(p) for p in write_dir.rglob("*") if p.is_file())
    out: list[str] = []
    for p in write_dir.rglob("*"):
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime >= since:
                out.append(str(p))
        except OSError:
            # File vanished between rglob and stat — skip silently.
            continue
    return sorted(out)
