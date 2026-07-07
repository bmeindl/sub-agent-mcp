"""Shared pytest setup. Trigger env-file loading so slow tests that hit
real opencode subprocesses get provider auth tokens from ~/.config/opencode/*.env
without having to be run from an interactive shell that sourced them."""

from __future__ import annotations

# Importing server has the side effect of running _load_env_files() at module
# import time. Tests don't otherwise need server.py — runner is the unit under
# test — so this is the cheapest way to share env-file loading across all tests.
from sub_agent_mcp import server  # noqa: F401
