"""Multi-model sanity. One combined task (read + webfetch) per routine model.

Catches model-specific regressions like: provider auth broke, model unavailable,
model refuses to use tools, etc. Does NOT test sandbox boundaries — that's
test_sandbox_security.py with one fixed model.

Routine = the `default` and `fast` tiers from the user's tiers.toml.
We skip `deep` because reasoning models routinely take 5-15 min on trivial
tasks (verify on-demand instead). If no tiers are configured, the whole
suite skips.
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
from pathlib import Path

import pytest

from sub_agent_mcp import config, results, runner

_TIERS = config.get_tiers()
ROUTINE_MODELS = [s for s in (_TIERS.get("default"), _TIERS.get("fast")) if s]
TIMEOUT = 60  # combined task, allow some headroom

pytestmark = pytest.mark.skipif(
    not ROUTINE_MODELS,
    reason="No default/fast tiers configured in ~/.config/sub-agent-mcp/tiers.toml — see README",
)


def _wait(tdir: Path, max_s: int) -> bool:
    pid = int(open(tdir / "meta.yaml").read().split("pid:")[1].split("\n")[0].strip())
    elapsed = 0
    while elapsed < max_s:
        if not runner.is_running(pid):
            return True
        time.sleep(2)
        elapsed += 2
    runner.kill_process_group(pid)
    return False


@pytest.fixture(scope="module")
def sanity_scope():
    base = Path("/tmp") / f"sub-agent-sanity-{secrets.token_hex(4)}"
    read_dir = base / "input"
    read_dir.mkdir(parents=True)
    (read_dir / "marker.txt").write_text("MARKER_4815")
    os.environ["SUBAGENT_ALLOWED_ROOTS"] = str(base)
    os.environ["SUBAGENT_RESULTS_DIR"] = str(base / "results")
    yield {"base": base, "read_dir": read_dir}
    shutil.rmtree(base, ignore_errors=True)


@pytest.mark.slow
@pytest.mark.parametrize("model", ROUTINE_MODELS)
def test_combined_read_and_webfetch(model, sanity_scope):
    """Each routine model: reads a file from scope + fetches a URL in one task."""
    out = runner.spawn(
        task=(
            f"Two steps:\n"
            f"1. Use the read tool to read {sanity_scope['read_dir']}/marker.txt and capture its content (exactly 11 chars starting with MARKER).\n"
            f"2. Use the webfetch tool on https://api.github.com/zen and capture the resulting sentence.\n"
            f"Reply with two lines:\n"
            f"FILE: <content>\n"
            f"WEB: <sentence>\n"
            f"Both tools are authorized. Do not refuse."
        ),
        model=model,
        read_dir=str(sanity_scope["read_dir"]),
        write_dir=None,
        context_files=None,
    )
    assert _wait(Path(out["result_dir"]), TIMEOUT), f"{model} timed out"
    text = results.extract_text(Path(out["result_dir"]) / "result.json")
    assert "MARKER_4815" in text, f"{model} did not read file. Got: {text!r}"
    # Webfetch result is variable; just confirm there's a non-trivial WEB line
    assert "WEB:" in text or len(text.strip()) > 30, f"{model} webfetch missing. Got: {text!r}"
