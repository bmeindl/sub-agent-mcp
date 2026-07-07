"""Validation tests — no opencode subprocess, fast."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from sub_agent_mcp import validators


@pytest.fixture
def tmp_root(monkeypatch):
    """Set SUBAGENT_ALLOWED_ROOTS (legacy fallback) to a freshly-created tmpdir.

    Clears the explicit READ/WRITE root vars so the fallback is exercised.
    """
    monkeypatch.delenv("SUBAGENT_READ_ROOTS", raising=False)
    monkeypatch.delenv("SUBAGENT_WRITE_ROOTS", raising=False)
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("SUBAGENT_ALLOWED_ROOTS", d)
        yield Path(d)


def test_path_under_allowed_root_ok(tmp_root):
    sub = tmp_root / "sub"
    sub.mkdir()
    resolved = validators.validate_path(str(sub), must_be_dir=True)
    # Compare against fully-resolved path (handles macOS /var → /private/var symlink)
    assert resolved == sub.resolve()


def test_path_outside_allowed_roots_rejected(tmp_root):
    with pytest.raises(validators.ValidationError, match="not under any SUBAGENT_READ_ROOTS"):
        validators.validate_path("/etc", must_be_dir=True)


def test_path_with_traversal_rejected(tmp_root):
    sub = tmp_root / "sub"
    sub.mkdir()
    with pytest.raises(validators.ValidationError, match="\\.\\."):
        validators.validate_path(f"{sub}/../escape", must_be_dir=True)


def test_path_with_special_chars_rejected(tmp_root):
    with pytest.raises(validators.ValidationError, match="must match"):
        validators.validate_path("/tmp/with space", must_be_dir=True)


def test_path_when_no_roots_set(monkeypatch):
    monkeypatch.delenv("SUBAGENT_ALLOWED_ROOTS", raising=False)
    monkeypatch.delenv("SUBAGENT_READ_ROOTS", raising=False)
    monkeypatch.delenv("SUBAGENT_WRITE_ROOTS", raising=False)
    with pytest.raises(validators.ValidationError, match="SUBAGENT_READ_ROOTS .* is unset"):
        validators.validate_path("/tmp", must_be_dir=True)


def test_unknown_model_rejected_at_spawn(monkeypatch, tmp_path):
    """Typos in model slugs (e.g. wrong provider prefix) must fail synchronously
    instead of silently hanging the opencode subprocess."""
    from sub_agent_mcp import runner, validators
    monkeypatch.delenv("SUBAGENT_ALLOWED_ROOTS", raising=False)
    monkeypatch.setenv("SUBAGENT_READ_ROOTS", str(tmp_path))
    monkeypatch.setenv("SUBAGENT_WRITE_ROOTS", str(tmp_path))
    monkeypatch.delenv("SUBAGENT_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("SUBAGENT_DEFAULT_READ_DIR", raising=False)
    monkeypatch.delenv("SUBAGENT_DEFAULT_WRITE_DIR", raising=False)

    with pytest.raises(validators.ValidationError, match="not on the approved list"):
        runner.spawn(task="hi", model="bogusprovider/some-model")
    with pytest.raises(validators.ValidationError, match="not on the approved list"):
        runner.spawn(task="hi", model="provider/typo-in-name-zzzzz")


def test_approved_models_includes_all_configured_tiers():
    """get_approved_models() must at minimum cover everything the tier dict
    resolves to, otherwise tier='X' would silently fail post-resolution."""
    from sub_agent_mcp import config
    tiers = config.get_tiers()
    if not tiers:
        pytest.skip("no tiers configured locally — nothing to validate")
    assert set(tiers.values()).issubset(config.get_approved_models())


def test_split_read_write_roots(monkeypatch, tmp_path):
    """When READ and WRITE roots differ, write outside WRITE_ROOTS is rejected."""
    read_only = tmp_path / "ro"
    write_ok = tmp_path / "rw"
    read_only.mkdir()
    write_ok.mkdir()
    monkeypatch.delenv("SUBAGENT_ALLOWED_ROOTS", raising=False)
    monkeypatch.setenv("SUBAGENT_READ_ROOTS", f"{read_only}:{write_ok}")
    monkeypatch.setenv("SUBAGENT_WRITE_ROOTS", str(write_ok))

    # Reading from read_only is allowed.
    assert validators.validate_path(str(read_only), must_be_dir=True, kind="read")
    # Writing to read_only is rejected.
    with pytest.raises(validators.ValidationError, match="SUBAGENT_WRITE_ROOTS"):
        validators.validate_path(str(read_only), must_be_dir=True, kind="write")
    # Writing to write_ok is allowed.
    assert validators.validate_path(str(write_ok), must_be_dir=True, kind="write")


def test_model_valid():
    # Tests the slug-format regex only — these need to pass shape validation
    # even though most aren't on the approved-list (that's a separate check at spawn time).
    assert validators.validate_model("provider/some-model") == "provider/some-model"
    assert validators.validate_model("anthropic/claude-sonnet-4-6")
    assert validators.validate_model("openrouter/deepseek-v3-0324")


def test_model_invalid_no_slash():
    with pytest.raises(validators.ValidationError):
        validators.validate_model("gpt-4o-mini")


def test_model_invalid_special_chars():
    with pytest.raises(validators.ValidationError):
        validators.validate_model("provider/model with space")


def test_task_empty_rejected():
    with pytest.raises(validators.ValidationError):
        validators.validate_task("")
    with pytest.raises(validators.ValidationError):
        validators.validate_task("   ")


def test_task_too_large_rejected():
    huge = "x" * (65 * 1024)
    with pytest.raises(validators.ValidationError, match="exceeds"):
        validators.validate_task(huge)


def test_context_files_validate(tmp_root):
    f = tmp_root / "ctx.md"
    f.write_text("hello")
    out = validators.validate_context_files([str(f)])
    assert out == [f.resolve()]


def test_context_files_outside_roots(tmp_root):
    # /etc/hosts exists but is outside allowed roots
    with pytest.raises(validators.ValidationError):
        validators.validate_context_files(["/etc/hosts"])


def test_context_file_must_be_regular_file(tmp_root):
    sub = tmp_root / "sub"
    sub.mkdir()
    with pytest.raises(validators.ValidationError, match="not a regular file"):
        validators.validate_context_files([str(sub)])
