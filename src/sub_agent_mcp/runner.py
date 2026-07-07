"""Spawn opencode as detached background subprocess; manage per-task state."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import config, permissions, results, validators

# v0: hardcoded. Future version may make this per-call.
DEFAULT_KILL_AFTER_SECONDS = 1800

# Tier mapping and approved-model allowlist live in the user's TOML config
# (see config.py). The MCP ships no provider-specific defaults — paid vs
# free, IU UE vs OpenRouter, etc. is the user's choice.
#
# To extend the allowlist: prefer adding a new tier in tiers.toml. Use
# `extra_approved_models` only for one-off slugs that don't deserve a tier
# alias.


def _opencode_binary() -> str:
    path = shutil.which("opencode")
    if not path:
        raise RuntimeError("opencode binary not found in PATH")
    return path


def spawn(
    *,
    task: str,
    tier: str | None = None,
    model: str | None = None,
    read_dir: str | None = None,
    write_dir: str | None = None,
    context_files: list[str] | None = None,
) -> dict:
    """Validate, build config, spawn opencode detached. Returns task_id + meta.

    Model resolution:
      1. If `model` is set: use it verbatim (escape hatch, discouraged).
      2. Else if `tier` is set: map via TIERS dict.
      3. Else: SUBAGENT_DEFAULT_MODEL env, then DEFAULT_MODEL_FALLBACK.
    """
    validators.validate_task(task)

    tiers = config.get_tiers()
    approved = config.get_approved_models()

    tier_warning = ""
    if model:
        if tier:
            tier_warning = f"[runner] both `model` ({model}) and `tier` ({tier}) given; `model` wins.\n"
    elif tier:
        if not tiers:
            raise validators.ValidationError(config.setup_hint())
        if tier not in tiers:
            raise validators.ValidationError(
                f"unknown tier {tier!r}; configured tiers are {sorted(tiers)} "
                f"(see {config.config_path()})"
            )
        model = tiers[tier]

    # Resolve env defaults if per-call args not given. Falls back to the
    # `default` tier if configured, else None (validate_model will reject).
    model = (
        model
        or os.environ.get("SUBAGENT_DEFAULT_MODEL", "").strip()
        or tiers.get("default", "")
    )
    read_dir = read_dir or os.environ.get("SUBAGENT_DEFAULT_READ_DIR", "").strip() or None
    write_dir = write_dir or os.environ.get("SUBAGENT_DEFAULT_WRITE_DIR", "").strip() or None
    context_files = context_files or []

    if not model:
        raise validators.ValidationError(config.setup_hint())

    validators.validate_model(model)
    if model not in approved:
        raise validators.ValidationError(
            f"model {model!r} is not on the approved list. Use `tier` instead — "
            f"configured tiers are {sorted(tiers)} mapping to {sorted(set(tiers.values()))}. "
            f"To allow this slug, add it under [tiers] or to extra_approved_models in "
            f"{config.config_path()} after manually verifying it works through this MCP."
        )
    resolved_read = (
        validators.validate_path(read_dir, must_be_dir=True, kind="read")
        if read_dir
        else None
    )
    resolved_write = (
        validators.validate_path(write_dir, must_be_dir=True, must_exist=False, kind="write")
        if write_dir
        else None
    )
    resolved_context = validators.validate_context_files(context_files)

    # Create write_dir if it doesn't exist (within allowed roots).
    if resolved_write and not resolved_write.exists():
        resolved_write.mkdir(parents=True, exist_ok=True)

    task_id = results.new_task_id()
    tdir = results.task_dir(task_id)

    # Write brief.
    (tdir / "brief.txt").write_text(task)

    # Build per-job opencode config.
    config_json = permissions.build_config(
        read_dir=resolved_read,
        write_dir=resolved_write,
        context_files=resolved_context,
    )
    (tdir / "opencode-config.json").write_text(config_json)

    # opencode 1.14.30: positional message arg makes opencode hang waiting for
    # stdin even if message is non-empty. Fix: --model flag + pipe message via stdin.
    argv: list[str] = [_opencode_binary(), "run", "--model", model, "--format", "json", "--dangerously-skip-permissions"]
    for cf in resolved_context:
        argv += ["-f", str(cf)]

    # Build subprocess env: WHITELIST approach. Inheriting full os.environ would
    # leak ANTHROPIC_API_KEY, AWS_*, GIT_*, etc. into the sub-agent's environment
    # where a curious or compromised model could read them via env-printing tools.
    # Only pass the base set (PATH, HOME, locale, ...) plus provider-specific
    # secrets the user listed under [passthrough_env] in tiers.toml.
    env_keep = config.get_env_keep()
    env = {k: v for k, v in os.environ.items() if k in env_keep}
    env["OPENCODE_CONFIG_CONTENT"] = config_json

    # opencode's built-in websearch needs OPENCODE_ENABLE_EXA=1 alongside
    # EXA_API_KEY. We only flip the flag when the key was actually forwarded
    # — otherwise the sub-agent would call websearch and hit an auth error
    # at tool-time. Users opt in by adding EXA_API_KEY to passthrough_env in
    # tiers.toml.
    if "EXA_API_KEY" in env:
        env["OPENCODE_ENABLE_EXA"] = "1"

    # Open stdout/stderr files.
    result_fp = open(tdir / "result.json", "w", buffering=1)
    log_fp = open(tdir / "log.txt", "w", buffering=1)
    if tier_warning:
        log_fp.write(tier_warning)

    started_at = time.time()
    kill_at = started_at + DEFAULT_KILL_AFTER_SECONDS

    # Pipe task as stdin; opencode 1.14.30 ignores positional message
    proc = subprocess.Popen(
        argv,
        stdout=result_fp,
        stderr=log_fp,
        stdin=subprocess.PIPE,
        env=env,
        cwd=str(tdir),
        start_new_session=True,  # POSIX: equivalent to setsid; child becomes process group leader
        close_fds=True,
    )
    if proc.stdin:
        proc.stdin.write(task.encode("utf-8"))
        proc.stdin.close()

    meta = {
        "task_id": task_id,
        "pid": proc.pid,
        "tier": tier or "",
        "model": model,
        "read_dir": str(resolved_read) if resolved_read else "",
        "write_dir": str(resolved_write) if resolved_write else "",
        "context_files": [str(p) for p in resolved_context],
        "started_at": started_at,
        "kill_at": kill_at,
    }
    _write_meta(tdir, meta)

    return {"task_id": task_id, "result_dir": str(tdir)}


def _write_meta(tdir: Path, meta: dict) -> None:
    """Write meta.yaml in a minimal pseudo-yaml (no PyYAML dep)."""
    lines = []
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{k}: {v}")
    (tdir / "meta.yaml").write_text("\n".join(lines) + "\n")


def read_meta(tdir: Path) -> dict:
    """Parse meta.yaml back. Lightweight, only handles what _write_meta produces."""
    meta: dict = {}
    if not (tdir / "meta.yaml").exists():
        return meta
    current_list_key: str | None = None
    for line in (tdir / "meta.yaml").read_text().splitlines():
        if line.startswith("  - "):
            if current_list_key:
                meta.setdefault(current_list_key, []).append(line[4:].strip())
            continue
        current_list_key = None
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            current_list_key = key
            meta[key] = []
        elif value.replace(".", "", 1).isdigit():
            meta[key] = float(value) if "." in value else int(value)
        else:
            meta[key] = value
    return meta


def is_running(pid: int) -> bool:
    """Check if a pid is still alive AND not a zombie (POSIX).

    The MCP server is the parent of opencode subprocesses (subprocess.Popen).
    When opencode exits, it becomes a zombie until the parent calls waitpid.
    Without reaping, `os.kill(pid, 0)` returns True forever (zombies still have
    a pid entry). So we reap first via waitpid(WNOHANG), then check.
    """
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False  # we just reaped a zombie
    except ChildProcessError:
        pass  # not our child or already reaped — fall through to kill check
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_process_group(pid: int) -> None:
    """SIGKILL the entire process group of pid. Used on deadline expiry."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
