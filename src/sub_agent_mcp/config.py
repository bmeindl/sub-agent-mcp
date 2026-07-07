"""Load tier mapping and environment-passthrough config from user TOML.

The MCP server ships no provider-specific defaults: tier→model mapping and
the env-var allowlist for child processes both come from the user's local
config file. This keeps provider preferences (paid IU UE, free opencode tier,
local Ollama, etc.) out of the source tree.

Resolution order for the config file:
  1. `SUBAGENT_TIERS_FILE` env var (absolute path)
  2. `~/.config/sub-agent-mcp/tiers.toml`
  3. None — tier-based calls then raise a clear setup error.

TOML schema:
    [tiers]
    default = "provider/model-id"
    fast    = "provider/model-id"
    deep    = "provider/model-id"

    extra_approved_models = ["provider/model-id", ...]
    passthrough_env       = ["MY_API_KEY", "MY_API_BASE_URL", ...]

Env vars in `passthrough_env` are forwarded to the opencode subprocess in
addition to a small base set (PATH/HOME/locale/TMPDIR/SHELL). The runtime
env var `SUBAGENT_PASSTHROUGH_ENV` (comma-separated) is unioned in too,
for ad-hoc additions without editing the file.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sub-agent-mcp" / "tiers.toml"

# Universally needed by opencode (shell, paths, locale). Provider-specific
# secrets must be added via `passthrough_env` in tiers.toml.
_BASE_ENV_KEEP: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "SHELL",
})


class ConfigError(RuntimeError):
    """Raised when the config file is missing or malformed AND a tier was
    requested. Setup-time errors are surfaced with actionable messages
    pointing the user at the config path."""


def config_path() -> Path:
    raw = os.environ.get("SUBAGENT_TIERS_FILE", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH


def load_config() -> dict:
    """Read the TOML config. Missing file → empty config (callers may raise
    ConfigError later if a tier is actually requested)."""
    p = config_path()
    if not p.is_file():
        return {"tiers": {}, "extra_approved_models": [], "passthrough_env": []}
    try:
        with p.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"failed to read {p}: {e}") from e

    tiers = raw.get("tiers", {})
    if not isinstance(tiers, dict):
        raise ConfigError(f"{p}: [tiers] must be a table, got {type(tiers).__name__}")
    for k, v in tiers.items():
        if not isinstance(v, str):
            raise ConfigError(f"{p}: tiers.{k} must be a string, got {type(v).__name__}")

    extra = raw.get("extra_approved_models", [])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise ConfigError(f"{p}: extra_approved_models must be a list of strings")

    passthrough = raw.get("passthrough_env", [])
    if not isinstance(passthrough, list) or not all(isinstance(x, str) for x in passthrough):
        raise ConfigError(f"{p}: passthrough_env must be a list of strings")

    return {
        "tiers": dict(tiers),
        "extra_approved_models": list(extra),
        "passthrough_env": list(passthrough),
    }


def get_tiers() -> dict[str, str]:
    return load_config()["tiers"]


def get_approved_models() -> set[str]:
    cfg = load_config()
    return set(cfg["tiers"].values()) | set(cfg["extra_approved_models"])


def get_env_keep() -> frozenset[str]:
    """Base env-vars + passthrough from TOML + SUBAGENT_PASSTHROUGH_ENV."""
    cfg = load_config()
    env_extra_raw = os.environ.get("SUBAGENT_PASSTHROUGH_ENV", "").strip()
    env_extra = [s.strip() for s in env_extra_raw.split(",") if s.strip()]
    return _BASE_ENV_KEEP | set(cfg["passthrough_env"]) | set(env_extra)


def setup_hint() -> str:
    """One-liner pointing the user at config. Used in error messages."""
    p = config_path()
    return (
        f"No tier configuration found. Create {p} (see "
        f"sub-agent-mcp README → Setup) or set SUBAGENT_TIERS_FILE."
    )
