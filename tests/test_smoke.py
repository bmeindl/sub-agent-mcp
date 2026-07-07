"""End-to-end smoke test. Requires opencode + a configured provider.

Skipped if opencode binary missing or no provider credentials configured.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from sub_agent_mcp import results, runner


def _has_opencode_provider() -> bool:
    if not shutil.which("opencode"):
        return False
    try:
        out = subprocess.run(
            ["opencode", "providers", "list"], capture_output=True, text=True, timeout=10
        )
    except subprocess.SubprocessError:
        return False
    return "0 credentials" not in out.stdout


pytestmark = pytest.mark.skipif(
    not _has_opencode_provider(), reason="no opencode provider configured"
)


def test_smoke_spawn_and_extract(tmp_path, monkeypatch):
    """Spawn a trivial task, wait, assert result contains '56'."""
    # Use isolated results dir for this test
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    monkeypatch.setenv("SUBAGENT_RESULTS_DIR", str(results_dir))
    # No FS access needed — task is web-only friendly
    monkeypatch.delenv("SUBAGENT_ALLOWED_ROOTS", raising=False)

    # Use the tier mechanism — passing arbitrary opencode-known slugs via `model`
    # is now rejected by the allowlist, which is the whole point of the allowlist.
    out = runner.spawn(
        task="Was ist 7 mal 8? Antworte nur mit der Zahl.",
        tier=os.environ.get("SUBAGENT_TEST_TIER", "fast"),
        read_dir=None,
        write_dir=None,
        context_files=None,
    )
    assert "task_id" in out
    task_id = out["task_id"]
    tdir = results_dir / task_id
    assert tdir.is_dir()

    # Wait up to 60s for completion
    for _ in range(60):
        meta = runner.read_meta(tdir)
        pid = int(meta.get("pid", 0))
        if pid and not runner.is_running(pid):
            break
        time.sleep(1)

    text = results.extract_text(tdir / "result.json")
    assert "56" in text, f"expected '56' in result; got: {text!r}"
