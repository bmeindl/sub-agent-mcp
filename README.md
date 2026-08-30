# sub-agent-mcp

> Spawn off-context sub-agents from your main agent. Whatever model you want, parallel-safe, sandboxed filesystem.

An MCP server that hands work to background [opencode](https://opencode.ai) processes and returns only the final answer to your main session. The grunt work — tool calls, file reads, intermediate reasoning — never re-enters the orchestrating agent's context window.

Built for Claude Code as the main agent, but works with any MCP-capable client. The sub-agents themselves can be Claude, Gemini, GPT-5.x, Kimi, local Ollama models, or anything else opencode supports.

## Why

The main agent's context window is the scarce resource. Native sub-agent calls (e.g. Claude Code's `Task` tool) stream all output back into the main session's tokens — even when the actual answer you need is one paragraph. This MCP server runs the sub-agent as a fully external process: only the final result text re-enters the main agent's context, not the intermediate tool calls, file reads, web fetches, or scratch reasoning.

### When this beats native sub-agents

For ~90% of delegatable tasks, model identity doesn't matter — what matters is that the work happens off-context. Specifically:

- **Bulk research tasks** where you only need the summary: any capable model works, output stays out of your tokens until you fetch it.
- **Filesystem + web-search jobs** with default-deny isolation: explicit `read_dir` / `write_dir` allow-list is auditable per call.
- **Architecture review / brainstorm rounds** where you want **model diversity** as a validation mechanism: spawn the same prompt against Claude *and* Kimi *and* Gemini *and* compare. A single-vendor sub-agent loop converges to one bias.
- **Long-running parallel work** that should genuinely run detached, not block your conversation flow.

### When native `Task` is the right call instead

- Tasks that need access to your other MCP servers (Confluence, Jira, Amplitude, …) — sub-agents here only see opencode's built-in tools.
- Quick code-exploration where streaming output back into your context is *desirable* (you want to follow the agent's reasoning).
- Anything where the sub-agent's output is small enough that token-budget isn't a concern.

Both have valid places — pick by whether you want isolation (here) or context inheritance (`Task`).

## How it works

```
   main agent (Claude Code)
       │
       │  mcp__sub-agent__run_subagent(task, tier="fable", ...)
       ▼
   sub-agent-mcp (this repo)
       │  • validate paths against SUBAGENT_{READ,WRITE}_ROOTS
       │  • resolve tier → model via your tiers.toml
       │  • build per-task opencode config (default-deny FS, web tools on)
       │  • spawn `opencode run` as detached subprocess (start_new_session)
       ▼
   opencode subprocess
       │  • talks to your provider (paid/free/local — your config)
       │  • writes JSON event stream + meta to ~/.../sub-results/<task-id>/
       ▼
   final result text returned to main agent
```

Each spawn is independent (parallel-safe). Long jobs can detach: `run_subagent` returns `status: "running"` after a timeout but the job keeps writing; you collect later via `check_subagent(task_id)`.

**Timeouts** (`run_subagent`, seit 2026-07-28): normal **600 s**, mit `long=True` **1200 s**; der detachte Sub wird unabhängig davon nach **1800 s** hart gekillt (`runner.py: DEFAULT_KILL_AFTER_SECONDS`). Der Timeout ist als *Fehler*grenze gedacht, nicht als Normallaufzeit — deshalb großzügig. ⚠️ Es gibt **keinen Callback**: wer seinen Turn beendet, während ein Sub noch läuft, verliert das Ergebnis faktisch (es liegt in `sub-results/`, aber niemand liest es). Auspollen oder die `task_id` explizit weiterreichen.

## Quick start (60 seconds)

```bash
# 1. Install dependencies
brew install uv opencode
opencode providers login   # pick the free "opencode" provider for first test

# 2. Minimal tier config
mkdir -p ~/.config/sub-agent-mcp
cat > ~/.config/sub-agent-mcp/tiers.toml <<'EOF'
[tiers]
default = "opencode/minimax-m2.5-free"
fast    = "opencode/nemotron-3-super-free"
deep    = "opencode/hy3-preview-free"

passthrough_env = []
EOF

# 3. Add to your MCP client's .mcp.json (see Install below for full env block)
#    then restart the client.

# 4. Try it
#    mcp__sub-agent__run_subagent(task="What's 2+2? One word answer.", tier="kimi")
#    → {"result": "4", "status": "done", ...}
```

That's the whole flow. Replace the free-tier slugs with paid/local models when you're ready.

## Install

```bash
brew install uv opencode
opencode providers login   # pick any provider; free tier ("opencode") works for testing
```

In your `.mcp.json`:
```json
{
  "mcpServers": {
    "sub-agent": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/bmeindl/sub-agent-mcp",
        "sub-agent-mcp"
      ],
      "env": {
        "SUBAGENT_READ_ROOTS":   "/Users/me/work:/Users/me/scratch",
        "SUBAGENT_WRITE_ROOTS":  "/Users/me/scratch",
        "SUBAGENT_DEFAULT_READ_DIR":  "/Users/me/work",
        "SUBAGENT_DEFAULT_WRITE_DIR": "/Users/me/scratch",
        "SUBAGENT_RESULTS_DIR":  "/Users/me/scratch/sub-results"
      }
    }
  }
}
```

Then create a tier config (see Setup below) and restart your MCP client.

## Setup — `tiers.toml`

The MCP ships **no provider-specific defaults**. You decide which models back the three tiers (`default` / `fast` / `deep`) and which provider env-vars to forward to the opencode subprocess. This keeps the source provider-agnostic and lets you mix paid/free/local providers freely.

Create `~/.config/sub-agent-mcp/tiers.toml`:

```toml
# Map the three tiers to the model slugs you want.
# Tiers are conceptual: default = strong general-purpose, fast = cheap & snappy,
# deep = thinking/reasoning model (slow, only for genuinely hard problems).
[tiers]
default = "opencode/claude-sonnet-4-6"
fast    = "opencode/minimax-m2.5-free"
deep    = "opencode/gpt-5.5-pro"

# Optional: extra slugs allowed via the `model=` escape hatch.
# Leave empty unless you've verified a slug works through this MCP.
extra_approved_models = []

# Optional: env vars to forward to the opencode subprocess. The MCP only
# passes PATH/HOME/locale/TMPDIR/SHELL by default (no provider tokens),
# so each provider's auth vars must be listed here.
passthrough_env = [
  "OPENROUTER_API_KEY",
  "ANTHROPIC_API_KEY",
]
```

Override the path with `SUBAGENT_TIERS_FILE=/some/other/tiers.toml` if you keep configs in a dotfile repo.

### Provider examples

**OpenCode free tier** (no API key needed for testing):
```toml
[tiers]
default = "opencode/minimax-m2.5-free"
fast    = "opencode/nemotron-3-super-free"
deep    = "opencode/hy3-preview-free"

passthrough_env = []  # opencode free tier needs no extra env vars
```

**OpenRouter (any model, one key):**
```toml
[tiers]
default = "openrouter/anthropic/claude-sonnet-4-6"
fast    = "openrouter/qwen/qwen-2.5-72b-instruct"
deep    = "openrouter/openai/gpt-5.5-pro"

passthrough_env = ["OPENROUTER_API_KEY"]
```

**Local Ollama (no payment, fully offline):**
```toml
[tiers]
default = "ollama/llama3.3:70b"
fast    = "ollama/llama3.2:3b"
deep    = "ollama/qwq:32b"

passthrough_env = ["OLLAMA_HOST"]
```

**Custom enterprise endpoint:**
```toml
[tiers]
default = "myco-google/gemini-3.1-pro-preview"
fast    = "myco-openai/gpt-5.4-mini"
deep    = "myco-openai/gpt-5.5-pro"

passthrough_env = [
  "MYCO_UNIFIED_API_KEY",
  "MYCO_UNIFIED_BASE_URL",
]
```

## Configuration — env vars

| Env var | Purpose | Required for |
|---|---|---|
| `SUBAGENT_READ_ROOTS` | Colon-separated absolute paths the sub-agent may read from. `read_dir` and `context_files` are validated against this list. | Any read access. |
| `SUBAGENT_WRITE_ROOTS` | Colon-separated absolute paths the sub-agent may write to. `write_dir` is validated against this list. Typical policy: read everything, write only into a scratch dir. | Any write access. |
| `SUBAGENT_ALLOWED_ROOTS` | Deprecated single-list fallback applied to both read and write when the split vars are unset. | Backcompat only. |
| `SUBAGENT_RESULTS_DIR` | Where task metadata is stored (server-managed). | Optional, defaults to `~/Documents/ai-workspace/sub-results` |
| `SUBAGENT_DEFAULT_READ_DIR` | Default `read_dir` when not given per-call | Optional |
| `SUBAGENT_DEFAULT_WRITE_DIR` | Default `write_dir` when not given per-call | Optional |
| `SUBAGENT_TIERS_FILE` | Override path of `tiers.toml`. | Optional |
| `SUBAGENT_DEFAULT_MODEL` | Override the `tier="default"` resolved model. Discouraged — prefer editing `tiers.toml`. | Optional |
| `SUBAGENT_PASSTHROUGH_ENV` | Comma-separated extra env vars to forward (unioned with `passthrough_env` from TOML). | Optional |

### Model selection — use `tier`, not `model`

Three tiers, picked to cover **the only model selections you should normally need**:

| `tier` | Intent | When |
|---|---|---|
| `"default"` | The primary workhorse | Most work; also what a call without `tier` gets. |
| `"sol"` | OpenAI's flagship | Strong agentic/tool work. |
| `"fable"` | A Claude model | Cross-checking a run that is NOT Claude (e.g. a Codex/Sol run asking for a review). |
| `"kimi"` | A third voice | Neither Anthropic nor OpenAI — a genuinely independent read. |

Tiers are named after the model they resolve to, not after a price class: the reason to
spawn an external sub is *whose* second opinion you get. What each name maps to is your
own `tiers.toml` — the names above are this repo's suggested shape, and the tier keys are
yours to define. Pick a tier for a different model, not for a cheaper one.

**Stick to these three.** Other models exposed via `list_models()` are untested through this MCP, may be broken via adapter quirks, or have flaky availability per provider. `list_models()` is **diagnostics only** — don't pick from it.

**`model` parameter** is reserved for adding new tested slugs. Anything not on the allowlist (the three tier values + `extra_approved_models`) is rejected at spawn with a `ValidationError`. Don't try to guess slugs from `list_models()` output — opencode's slug regex is permissive and unknown providers can hang the subprocess in auth handshake. Use `tier` and let the MCP pick.

## Tools

### `spawn_subagent(task, tier?, read_dir?, write_dir?, context_files?, model?)`

Spawn a sub-agent and return immediately with a `task_id`. Sub-agent runs detached in the background.

- `task` (required): the prompt
- `tier`: `"default"` | `"sol"` | `"fable"` | `"kimi"`. Defaults to `"default"`.
- `read_dir`: absolute path the sub-agent may read recursively (under `SUBAGENT_READ_ROOTS`)
- `write_dir`: absolute path the sub-agent may write to (under `SUBAGENT_WRITE_ROOTS`)
- `context_files`: list of absolute file paths attached as context (each under allowed roots)
- `model`: ESCAPE HATCH (discouraged). Explicit opencode model id. Overrides `tier` if both set. Must be on the allowlist.

Returns `{task_id, result_dir}` or `{error}`.

### `run_subagent(task, tier?, read_dir?, write_dir?, context_files?, model?, long?)`

Same as `spawn_subagent` but blocks until the sub-agent finishes (or the timeout: 600 s, mit `long=True` 1200 s). Blockiert den *aufrufenden* Agenten — wer nebenher arbeiten will, nimmt `spawn_subagent` + `check_subagent`. On timeout the sub-agent keeps running detached — poll with `check_subagent(task_id)`. Set `long=True` yourself for a genuinely long job.

### `check_subagent(task_id)`

Poll for task status and result. Also runs a deadline sweep (30 min hardcoded) — kills any task whose deadline has passed.

Returns `{status, result, cost_usd, files_written, exit_code, meta}` or `{error}`.

`files_written` reports only files actually touched during this run (mtime ≥ `started_at`), not pre-existing files in `write_dir`.

### `list_models()`

List models available via the configured opencode providers.

Returns `[{provider, model, free}]` sorted free-first. Diagnostics only — see warning above.

## Examples

**Web-only research (no FS access), cheap tier:**
```json
{"task": "Use web search: latest stable Python version. Return only the version.",
 "tier": "kimi"}
```

**With reference document attached, default tier:**
```json
{"task": "Summarize the attached PRD into 3 bullet points",
 "context_files": ["/Users/me/work/projects/foo/PRD.md"]}
```

**With read+write dirs, default tier:**
```json
{"task": "Audit all README.md files in this repo for outdated commands. Write findings to audit.md",
 "read_dir": "/Users/me/work",
 "write_dir": "/Users/me/scratch/sub-results/readme-audit"}
```

**Deep reasoning (slow):**
```json
{"task": "Multi-step regulatory analysis: trace AI Act Art. 50 enforcement implications for synthetic-voice products in DE 2026-2028.",
 "tier": "fable"}
```

## Permission model

Sub-agents run with default-deny filesystem and shell:

- `bash`, `task`, `patch`, `todoread`, `todowrite` — always disabled
- `web_search`, `web_fetch` — always enabled
- `read`, `glob`, `grep`, `list` — enabled iff `read_dir` or `context_files` set
- `edit`, `write` — enabled iff `write_dir` set, and only inside it (`<write_dir>/**` allow, `**` deny)
- Hardcoded deny for `~/.ssh`, `~/.aws`, `~/.gnupg` and a small list of secret zones, regardless of allowed roots

The MCP server itself runs as your user — no special privileges needed. The boundary is enforced inside opencode via per-task `OPENCODE_CONFIG_CONTENT`.

The subprocess env is whitelisted: `PATH/HOME/locale/TMPDIR/SHELL` plus whatever you list under `passthrough_env` in `tiers.toml`. ANTHROPIC_API_KEY, AWS_*, GIT_*, etc. are NOT forwarded by default.

### Defense-in-depth: don't trust opencode's globbing alone

The MCP validates every `read_dir` / `write_dir` / `context_files` path against `SUBAGENT_{READ,WRITE}_ROOTS` **before** spawning opencode (`src/sub_agent_mcp/validators.py`). The opencode `permission` block is a second line of defense, not the primary gate. Reason: opencode's permission-glob matcher [doesn't handle absolute paths consistently across permission keys](https://github.com/anomalyco/opencode/issues/20045) — `read` matches against absolute paths, `edit`/`write` against paths relative to `Instance.worktree`. Patterns like `/some/abs/path/**` in `edit`/`write` therefore never match. Workaround used here: `<write_dir>/**` allow + `**` deny in opencode config, and authoritative scope-checking in our own validators.

## Tests

```bash
# Fast (validator-only, no API calls, instant):
uv run pytest tests/test_validators.py tests/test_permissions.py tests/test_results.py tests/test_config.py

# Slow (real opencode subprocesses, costs API tokens, ~75s total):
uv run pytest -m slow

# Or just the security boundary suite (~50s):
uv run pytest tests/test_sandbox_security.py -m slow
```

The security suite verifies the sandbox boundary holds: read/write inside scope works, secret zones blocked, env-var leak blocked, bash unavailable, write outside scope blocked. The multi-model sanity suite verifies each tier model can read a file and webfetch a URL in one combined task.

Provider config lives in `~/.config/opencode/opencode.json`. Tokens in `~/.config/opencode/*.env` (chmod 600, single-line `export VAR=value` format), loaded by the MCP server at startup.

## Development

```bash
git clone https://github.com/bmeindl/sub-agent-mcp
cd sub-agent-mcp
uv sync --group dev
uv run pytest                          # ~46 unit tests, fast
uv run pytest tests/test_smoke.py      # end-to-end, requires opencode + provider + tiers.toml
```

## License

MIT — see [LICENSE](LICENSE)
