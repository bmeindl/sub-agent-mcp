"""Build the OPENCODE_CONFIG_CONTENT JSON for a sub-agent task.

The sub-agent runs with default-deny filesystem and bash. Web tools always allowed.
read/edit/glob/grep enabled only when read_dir/write_dir/context_files are set.
"""

from __future__ import annotations

import json
from pathlib import Path

# Hardcoded deny list — protects credential dirs even if user accidentally puts
# them inside an allowed root. Sub-agent never reads these regardless of config.
# Extend this list (via PR or local fork) for app-specific secret zones.
SECRET_DENY_GLOBS = [
    "/Users/*/.ssh/**",
    "/Users/*/.aws/**",
    "/Users/*/.gnupg/**",
    "/Users/*/.config/opencode/**",
]


def build_config(
    *,
    read_dir: Path | None,
    write_dir: Path | None,
    context_files: list[Path],
) -> str:
    """Return the JSON string suitable for OPENCODE_CONFIG_CONTENT."""
    has_read = bool(read_dir) or bool(context_files)
    has_write = bool(write_dir)

    # opencode permission semantics (per https://opencode.ai/docs/permissions/
    # and 2026-04-30 empirical testing on opencode 1.14.30):
    # - LAST-MATCH-WINS. Rules ordered broad→specific: catch-all deny FIRST,
    #   specific allows AFTER (so they override).
    # - Catch-all uses `*` (single star) not `**`.
    # - opencode's glob matcher is finicky with multi-segment absolute paths.
    #   Patterns like `**/private/tmp/agent-out/**` DO NOT match
    #   `/private/tmp/agent-out/file` (probably the leading `/` + multiple
    #   `/`-segments after `**/` confuse the matcher). The single-segment
    #   pattern `**/<basename>/**` reliably matches.
    # - This means our opencode-level glob is intentionally LOOSER than the
    #   absolute path. The actual security boundary is `SUBAGENT_ALLOWED_ROOTS`
    #   validated by validators.py BEFORE opencode is spawned. opencode's
    #   permission engine is defense-in-depth, not the primary gate.
    def _glob(p: str) -> str:
        """Pattern that opencode's glob actually matches. Falls back to basename."""
        basename = p.rstrip("/").rsplit("/", 1)[-1]
        return f"**/{basename}/**"

    edit_perm: dict[str, str] = {}
    read_perm: dict[str, str] = {}

    edit_perm["*"] = "deny"
    if write_dir:
        edit_perm[_glob(str(write_dir))] = "allow"

    read_perm["*"] = "deny"
    if read_dir:
        read_perm[_glob(str(read_dir))] = "allow"
    for f in context_files:
        path_str = str(f)
        basename = path_str.rsplit("/", 1)[-1]
        read_perm[f"**/{basename}"] = "allow"
    # Secret-deny globs use the same `**/<segment>/**` pattern.
    for glob in SECRET_DENY_GLOBS:
        # SECRET_DENY_GLOBS are paths like `/Users/*/.ssh/**`. Extract the
        # meaningful directory segment (e.g. `.ssh`) — the LAST non-glob segment,
        # not the first. Picking the first gave us `Users`, which on macOS
        # matches every absolute path and silently nuked all read-allow rules.
        parts = glob.strip("/").split("/")
        meaningful = next(
            (p for p in reversed(parts) if p not in ("*", "**")),
            parts[-1],
        )
        read_perm[f"**/{meaningful}/**"] = "deny"

    # external_directory: opencode's "are you reaching outside cwd?" gate. Our cwd
    # is the per-task result dir, so the agent's legitimate read_dir/write_dir
    # ARE always external. Switch this to "allow" — the per-tool read/edit/write
    # globs are the real boundary. Validators already enforced read_dir/write_dir
    # are inside SUBAGENT_ALLOWED_ROOTS at spawn time.
    ext_dir = "allow" if (has_read or has_write) else "deny"

    config = {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "edit":  edit_perm if has_write else "deny",
            "write": edit_perm if has_write else "deny",
            "read":  read_perm if has_read else "deny",
            "bash":  "deny",
            "external_directory": ext_dir,
        },
        "tools": {
            "bash": False,
            "edit": has_write,
            "write": has_write,
            "read": has_read,
            "glob": bool(read_dir),
            "grep": bool(read_dir),
            "list": bool(read_dir),
            "patch": False,
            "task": False,
            "todoread": False,
            "todowrite": False,
            # opencode 1.14.30 tool-name keys (one word, NOT web_search/web_fetch).
            # Unknown keys are silently ignored by opencode, so the previous
            # snake_case spellings were no-ops — webfetch worked only because
            # opencode enables it by default; websearch stayed off.
            # websearch additionally requires OPENCODE_ENABLE_EXA=1 + EXA_API_KEY
            # in the subprocess env (handled in runner.py).
            "websearch": True,
            "webfetch": True,
        },
    }
    return json.dumps(config)
