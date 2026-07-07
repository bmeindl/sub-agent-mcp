"""Permission JSON construction tests."""

from __future__ import annotations

import json
from pathlib import Path

from sub_agent_mcp import permissions


def test_no_read_no_write_web_only():
    cfg = json.loads(permissions.build_config(read_dir=None, write_dir=None, context_files=[]))
    assert cfg["tools"]["bash"] is False
    assert cfg["tools"]["edit"] is False
    assert cfg["tools"]["read"] is False
    assert cfg["tools"]["websearch"] is True
    assert cfg["tools"]["webfetch"] is True
    assert cfg["permission"]["bash"] == "deny"


def test_with_read_dir_grants_read_tools():
    cfg = json.loads(
        permissions.build_config(read_dir=Path("/tmp/foo"), write_dir=None, context_files=[])
    )
    assert cfg["tools"]["read"] is True
    assert cfg["tools"]["glob"] is True
    assert cfg["tools"]["grep"] is True
    # opencode glob matcher only reliably handles single-segment patterns;
    # build_config emits `**/<basename>/**` rather than the absolute path.
    assert cfg["permission"]["read"]["**/foo/**"] == "allow"
    assert cfg["permission"]["read"]["*"] == "deny"


def test_with_write_dir_grants_write_tools():
    cfg = json.loads(
        permissions.build_config(
            read_dir=None, write_dir=Path("/tmp/out"), context_files=[]
        )
    )
    assert cfg["tools"]["write"] is True
    assert cfg["tools"]["edit"] is True
    edit = cfg["permission"]["edit"]
    assert edit["**/out/**"] == "allow"
    assert edit["*"] == "deny"


def test_secret_paths_always_denied():
    cfg = json.loads(
        permissions.build_config(read_dir=Path("/tmp/foo"), write_dir=None, context_files=[])
    )
    read = cfg["permission"]["read"]
    # Each SECRET_DENY_GLOBS entry must produce a deny on its OWN dotfile
    # segment, not on `Users` (the first non-glob segment in `/Users/*/.ssh/**`).
    # Regression: previously all six collapsed to `**/Users/**`, denying
    # every absolute path on macOS and silently breaking read_dir.
    assert read["**/.ssh/**"] == "deny"
    assert read["**/.aws/**"] == "deny"
    assert read["**/.gnupg/**"] == "deny"
    assert read["**/opencode/**"] == "deny"
    assert "**/Users/**" not in read


def test_context_files_added_to_read():
    cfg = json.loads(
        permissions.build_config(
            read_dir=None,
            write_dir=None,
            context_files=[Path("/tmp/foo/ctx1.md"), Path("/tmp/foo/ctx2.md")],
        )
    )
    read = cfg["permission"]["read"]
    assert read["**/ctx1.md"] == "allow"
    assert read["**/ctx2.md"] == "allow"
    assert cfg["tools"]["read"] is True
