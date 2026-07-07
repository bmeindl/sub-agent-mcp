"""FastMCP entry point. Wires MCP tools to runner/results modules."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from . import results, runner, validators

Tier = Literal["default", "fast", "deep"]


def _load_env_files() -> None:
    """Load secrets from chmod-600 env files into os.environ at server start.

    Avoids putting API keys in .mcp.json (which can leak via screenshots/git).
    Default location: ~/.config/opencode/*.env. Override via SUBAGENT_ENV_FILES.

    Format: shell-style `export KEY='value'` lines.
    """
    raw = os.environ.get("SUBAGENT_ENV_FILES", "").strip()
    if raw:
        paths = [Path(p).expanduser() for p in raw.split(":") if p.strip()]
    else:
        default_dir = Path.home() / ".config" / "opencode"
        paths = sorted(default_dir.glob("*.env")) if default_dir.is_dir() else []

    pattern = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$")
    for p in paths:
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = pattern.match(line)
            if not m:
                continue
            key, value = m.group(1), m.group(2).strip()
            # Strip surrounding quotes if present
            if (value.startswith("'") and value.endswith("'")) or (
                value.startswith('"') and value.endswith('"')
            ):
                value = value[1:-1]
            # Don't overwrite values already set by the parent env (allows .mcp.json override)
            os.environ.setdefault(key, value)


_load_env_files()

# Two preset timeouts for run_subagent. Default for typical tasks; "long" for
# bigger research/multi-step. The detached sub-agent is still bounded by the
# hardcoded kill_at deadline (1800s / 30 min) regardless — see runner.py.
RUN_SUBAGENT_TIMEOUT_DEFAULT = 120  # 2 min — fits most web-search / single-file tasks
RUN_SUBAGENT_TIMEOUT_LONG = 600  # 10 min — for bigger research

mcp = FastMCP("sub-agent")


def _error(message: str, **extra) -> dict:
    """Structured error return — preferred over raising, for MCP clarity."""
    return {"error": message, **extra}


@mcp.tool()
def spawn_subagent(
    task: str,
    tier: Tier = "default",
    read_dir: str | None = None,
    write_dir: str | None = None,
    context_files: list[str] | None = None,
    model: str | None = None,
) -> dict:
    """Spawn an isolated opencode sub-agent in the background.

    Default-deny filesystem access. Set read_dir/write_dir/context_files
    (must be under SUBAGENT_READ_ROOTS / SUBAGENT_WRITE_ROOTS) to opt in.
    Without those, the sub-agent has webfetch only by default. websearch
    additionally requires EXA_API_KEY in your passthrough_env (tiers.toml);
    when present, OPENCODE_ENABLE_EXA=1 is set automatically.

    MODEL SELECTION — only ever use `tier`. Pick "default" or "fast":
      - tier="default" → strong general-purpose, near-Opus quality.
            ~12-22s with tool use. Use this for most work.
      - tier="fast"    → cheap, snappy, normal quality.
            ~14-19s. Use when cost matters more than quality.
      - tier="deep"    → thinking model, SLOW (5-15 min even on trivial inputs).
            ONLY for genuinely hard reasoning. Auto-enables `long=True`.

    Do NOT set `model`. The exact slugs are an implementation detail and
    typos in provider prefixes (silently accepted by opencode's regex check)
    can hang the subprocess in provider-auth handshake. Any `model` value
    not on the configured allowlist is rejected at spawn with a clear
    ValidationError. `list_models()` is diagnostics — its output is NOT
    directly usable as a model id.

    Args:
        task: The prompt for the sub-agent (required).
        tier: "default" | "fast" | "deep". Defaults to "default".
        read_dir: Optional. Directory the sub-agent may read.
        write_dir: Optional. Directory the sub-agent may write.
        context_files: Optional. Specific files to attach + grant read access.
        model: Don't use. Reserved for adding new tested slugs to the
            allowlist; rejected for unknown slugs.

    Returns:
        On success: {"task_id": str, "result_dir": str}
        On error:   {"error": str, ...}
    """
    try:
        return runner.spawn(
            task=task,
            tier=tier,
            model=model,
            read_dir=read_dir,
            write_dir=write_dir,
            context_files=context_files,
        )
    except validators.ValidationError as e:
        return _error(f"validation: {e}")
    except RuntimeError as e:
        return _error(str(e))
    except Exception as e:  # noqa: BLE001 — last-resort catchall
        return _error(f"internal: {type(e).__name__}: {e}")


@mcp.tool()
def check_subagent(task_id: str) -> dict:
    """Check the status of a sub-agent task and retrieve its result if done.

    Also runs a deadline sweep: any task whose kill_at has passed and is still
    running gets SIGKILL'd here.

    Args:
        task_id: Returned from spawn_subagent.

    Returns:
        {status: "running"|"done"|"failed",
         result: str,           # extracted assistant text, "" if running
         cost_usd: float,
         files_written: list[str],
         exit_code: int|None,
         meta: dict}
        On error: {"error": str}
    """
    try:
        _sweep_deadlines()

        tdir = results.results_root() / task_id
        if not tdir.is_dir():
            return _error(f"task_id not found: {task_id}")

        meta = runner.read_meta(tdir)
        result_json = tdir / "result.json"
        done_flag = tdir / "done.flag"
        failed_flag = tdir / "failed.flag"

        # Determine completion. If subprocess no longer running but no flag set,
        # set done.flag now (opencode finished but our wrapper script didn't run).
        pid = int(meta.get("pid", 0))
        still_running = pid and runner.is_running(pid)

        if not still_running and not done_flag.exists() and not failed_flag.exists():
            # Synthesize completion: opencode died on its own, we missed it.
            done_flag.touch()

        if failed_flag.exists():
            status = "failed"
        elif done_flag.exists():
            status = "done"
        else:
            status = "running"

        # Detect opencode-side errors (e.g. no-payment-method, model unavailable)
        # even when the process exited 0 — these end up in the NDJSON as type=error events.
        error_msg = ""
        if status == "done":
            error_msg = results.extract_error(result_json)
            if error_msg:
                status = "failed"
                # Persist the failed marker so subsequent checks return failed too
                failed_flag.touch()

        write_dir = Path(meta["write_dir"]) if meta.get("write_dir") else None

        result_text = ""
        if status == "done":
            result_text = results.extract_text(result_json)
        elif status == "failed":
            result_text = error_msg or results.extract_text(result_json)

        return {
            "status": status,
            "result": result_text,
            "cost_usd": results.extract_cost(result_json) if status != "running" else 0.0,
            "files_written": (
                results.list_files_written(write_dir, since=meta.get("started_at"))
                if status == "done"
                else []
            ),
            "exit_code": None,  # opencode --format json doesn't surface this cleanly; future work
            "meta": meta,
        }
    except Exception as e:  # noqa: BLE001
        return _error(f"internal: {type(e).__name__}: {e}")


def _sweep_deadlines() -> None:
    """Kill any task whose kill_at has passed and that's still running."""
    now = time.time()
    root = results.results_root()
    if not root.is_dir():
        return
    for tdir in root.iterdir():
        if not tdir.is_dir():
            continue
        meta = runner.read_meta(tdir)
        kill_at = meta.get("kill_at")
        pid = meta.get("pid")
        if not kill_at or not pid:
            continue
        try:
            kill_at = float(kill_at)
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if now > kill_at and runner.is_running(pid):
            runner.kill_process_group(pid)
            (tdir / "failed.flag").touch()
            (tdir / "log.txt").open("a").write(
                f"\n[deadline-sweep] killed pid {pid} at {now} (kill_at={kill_at})\n"
            )


@mcp.tool()
def run_subagent(
    task: str,
    tier: Tier = "default",
    read_dir: str | None = None,
    write_dir: str | None = None,
    context_files: list[str] | None = None,
    model: str | None = None,
    long: bool = False,
) -> dict:
    """Spawn a sub-agent and BLOCK until it finishes (or timeout). Sync variant.

    Use this for the typical request→response flow (matches Confluence/Jira MCP
    pattern). For very long jobs or true fire-and-forget, use spawn_subagent
    + check_subagent instead.

    MODEL SELECTION — use `tier`, not `model`. See spawn_subagent docstring for
    the three tested tiers ("default" / "fast" / "deep") and why you should
    almost never override via `model`.

    Args (same as spawn_subagent, plus):
        long: if False (default), wait up to 120s.
              if True, wait up to 600s (10 min). Auto-enabled when tier="deep"
              (the thinking model needs the headroom).
              On timeout the sub-agent keeps running in the background — you
              can always call check_subagent(task_id) later to collect the result.

    Returns:
        On completion: status="done"|"failed", result, cost_usd, files_written, meta.
        On timeout:    status="running", task_id, message — sub-agent is NOT killed,
                       it keeps running detached; poll with check_subagent.
    """
    # tier="deep" implies long=True — thinking/reasoning models routinely
    # take 5-15 min even on trivial inputs, so the short timeout would
    # always fire.
    if tier == "deep":
        long = True
    timeout = RUN_SUBAGENT_TIMEOUT_LONG if long else RUN_SUBAGENT_TIMEOUT_DEFAULT
    spawned = spawn_subagent(
        task=task,
        tier=tier,
        read_dir=read_dir,
        write_dir=write_dir,
        context_files=context_files,
        model=model,
    )
    if "error" in spawned:
        return spawned
    task_id = spawned["task_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check_subagent(task_id)
        if result.get("status") in ("done", "failed"):
            return result
        time.sleep(2)

    # Timeout reached — sub-agent is detached and keeps running. We do NOT kill it.
    # Caller can call check_subagent(task_id) any time later to collect the result,
    # until kill_at is reached (30 min from spawn) at which point the deadline-sweep
    # finally terminates it. This makes timeout safe by design.
    return {
        "status": "running",
        "task_id": task_id,
        "result": "",
        "message": (
            f"Sub-agent did not finish within {timeout}s but is still running in "
            f"the background (will be auto-killed after 30 min total). "
            f"Call check_subagent('{task_id}') to collect the result when ready — "
            f"no work is lost."
        ),
        "waited_seconds": timeout,
    }


@mcp.tool()
def list_models() -> list[dict]:
    """DIAGNOSTICS ONLY. List opencode models the local install knows about.

    Do NOT use this to pick a model for spawn_subagent / run_subagent — most
    listed models are untested through this MCP, broken via adapter quirks, or
    have flaky availability per provider. Use the `tier` parameter instead
    ("default" | "fast" | "deep"). This tool exists for debugging provider
    config and verifying that opencode sees the expected providers.

    Returns each as {provider, model, free}. `free` is heuristic: True if the
    model id contains 'free' or the provider is 'opencode'.
    """
    binary = shutil.which("opencode")
    if not binary:
        return [{"error": "opencode binary not found in PATH"}]
    try:
        out = subprocess.run(
            [binary, "models"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return [{"error": "opencode models timed out"}]
    if out.returncode != 0 or not out.stdout.strip():
        return [{"error": "opencode returned no models", "stderr": out.stderr[:200]}]

    models: list[dict] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or "/" not in line or " " in line:
            continue
        provider, _, model = line.partition("/")
        if not provider or not model:
            continue
        # Conservative heuristic: only flag explicit "-free" suffix.
        # Anything else (incl. opencode/gpt-5.4 etc.) may require payment;
        # let opencode itself surface errors at spawn time.
        free = "-free" in model.lower() or model.lower().endswith("free")
        models.append({"provider": provider, "model": model, "free": free})

    models.sort(key=lambda m: (not m["free"], m["provider"], m["model"]))
    return models


def main() -> None:
    """Entry point referenced by pyproject.toml `[project.scripts]`."""
    mcp.run()


if __name__ == "__main__":
    main()
