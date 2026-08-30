"""Tests for config.py — TOML loader and resolution rules."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sub_agent_mcp import config


@pytest.fixture
def tiers_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a tiers.toml under tmp_path and point SUBAGENT_TIERS_FILE at it."""
    p = tmp_path / "tiers.toml"
    monkeypatch.setenv("SUBAGENT_TIERS_FILE", str(p))
    return p


def test_load_config_missing_file_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("SUBAGENT_TIERS_FILE", str(tmp_path / "nonexistent.toml"))
    cfg = config.load_config()
    assert cfg == {"tiers": {}, "extra_approved_models": [], "passthrough_env": []}


def test_load_config_full(tiers_file: Path):
    tiers_file.write_text(textwrap.dedent("""
        extra_approved_models = ["p/d"]
        passthrough_env       = ["MY_KEY", "MY_URL"]

        [tiers]
        default = "p/a"
        fable   = "p/b"
        kimi    = "p/c"
    """))
    cfg = config.load_config()
    assert cfg["tiers"] == {"default": "p/a", "fable": "p/b", "kimi": "p/c"}
    assert cfg["extra_approved_models"] == ["p/d"]
    assert cfg["passthrough_env"] == ["MY_KEY", "MY_URL"]


def test_get_approved_models_unions_tiers_and_extras(tiers_file: Path):
    tiers_file.write_text(textwrap.dedent("""
        extra_approved_models = ["p/c", "p/a"]

        [tiers]
        default = "p/a"
        fable   = "p/b"
    """))
    assert config.get_approved_models() == {"p/a", "p/b", "p/c"}


def test_get_env_keep_includes_base_plus_passthrough(tiers_file: Path):
    tiers_file.write_text('passthrough_env = ["MY_KEY"]\n')
    keep = config.get_env_keep()
    assert "PATH" in keep
    assert "HOME" in keep
    assert "MY_KEY" in keep


def test_get_env_keep_unions_runtime_env_extras(tiers_file: Path, monkeypatch: pytest.MonkeyPatch):
    tiers_file.write_text("passthrough_env = []\n")
    monkeypatch.setenv("SUBAGENT_PASSTHROUGH_ENV", "RUNTIME_KEY,ANOTHER_KEY")
    keep = config.get_env_keep()
    assert "RUNTIME_KEY" in keep
    assert "ANOTHER_KEY" in keep
    assert "PATH" in keep  # base preserved


def test_load_config_rejects_bad_tiers_table(tiers_file: Path):
    tiers_file.write_text('tiers = "not a table"\n')
    with pytest.raises(config.ConfigError, match="must be a table"):
        config.load_config()


def test_load_config_rejects_non_string_tier_value(tiers_file: Path):
    tiers_file.write_text(textwrap.dedent("""
        [tiers]
        default = 42
    """))
    with pytest.raises(config.ConfigError, match="must be a string"):
        config.load_config()


def test_load_config_rejects_bad_extra_approved_models(tiers_file: Path):
    tiers_file.write_text("extra_approved_models = [1, 2, 3]\n")
    with pytest.raises(config.ConfigError, match="extra_approved_models"):
        config.load_config()


def test_load_config_rejects_bad_passthrough_env(tiers_file: Path):
    tiers_file.write_text('passthrough_env = "MY_KEY"\n')
    with pytest.raises(config.ConfigError, match="passthrough_env"):
        config.load_config()


def test_load_config_rejects_malformed_toml(tiers_file: Path):
    tiers_file.write_text("not = valid = toml\n")
    with pytest.raises(config.ConfigError, match="failed to read"):
        config.load_config()


def test_setup_hint_includes_path(tiers_file: Path):
    msg = config.setup_hint()
    assert "tiers.toml" in msg
    assert "SUBAGENT_TIERS_FILE" in msg


def test_config_path_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SUBAGENT_TIERS_FILE", "/tmp/custom-tiers.toml")
    assert config.config_path() == Path("/tmp/custom-tiers.toml")


def test_config_path_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SUBAGENT_TIERS_FILE", raising=False)
    p = config.config_path()
    assert p.name == "tiers.toml"
    assert "sub-agent-mcp" in str(p)
