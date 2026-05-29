# meshapi-code — Claude Context

Terminal chat REPL for [Mesh API](https://meshapi.ai), the OpenAI-compatible LLM gateway. Modeled on Claude Code and Aider. It is now an **agentic** CLI: it does tool calling (file read/write, shell, background servers, plans), image attachments, and permission modes — not just chat.

PyPI package = `meshapi-code`. Command on `$PATH` = `meshapi` (same split Claude Code uses: package `@anthropic-ai/claude-code`, command `claude`).

## Commands

```bash
pipx install -e .       # local dev install (or: uv tool install -e .)
meshapi                 # launch REPL
meshapi --version
python -m build         # build wheel + sdist for PyPI
twine check dist/*      # validate before upload
```

To run the working tree without reinstalling: `PYTHONPATH=src python -m meshapi`.

## Env Vars

| Var | Purpose |
|---|---|
| `MESHAPI_API_KEY` | Mesh API data-plane key (`rsk_…`). Falls back to `MESH_API_KEY` for one release. |
| `MESHAPI_BASE_URL` | Override gateway URL. Default `https://api.meshapi.ai/v1`. |

State under `~/.meshapi/`: `config.json` (settings, never the API key), `history` (input history, scrubbed + 0600), `servers.json` (backgrounded server records for crash-recovery). All written 0600.

## Architecture

Single-process REPL → stream `/v1/chat/completions` (SSE, OpenAI-compatible) → `rich.live.Live` markdown render → if the model returned `tool_calls`, run the agentic loop → loop back to the prompt.

```
src/meshapi/
  cli.py          # argparse + REPL loop, agentic tool-call loop, cost line, server lifecycle
  client.py       # stream_chat — yields content deltas + tool_calls + final {usage, cost} dict
  commands.py     # slash command handlers (/model, /route, /file, /image, /mode, /cost, ...)
  config.py       # ~/.meshapi/ load/save (config, history, servers.json), env override, 0600
  tools.py        # TOOLS schema, build_system_prompt, execute(), summarize_call, PLAN_TOOLS
  permissions.py  # Mode enum, AUTO_APPROVE sets, ORDER, next_mode, LABELS, SHOW_ESC_HINT
  safety.py       # auto-approval guardrails (path denylist, cwd-scope, bad commands, SSRF)
  attachments.py  # image load → base64 data URL; quote-aware auto-detect of image paths/URLs
  statusbar.py    # mode indicator: bottom_toolbar (live) + print_line (scrollback)
  keywatcher.py   # daemon thread: shift+tab (CSI Z) while prompt_toolkit isn't reading stdin
  plan.py         # plan state model for create_plan / update_step
  render.py       # rich Console singleton, render_stream, fmt_usd
  __main__.py     # python -m meshapi
```

## Agentic tool-calling loop

`handle_tool_calls` (cli.py) appends the assistant `tool_calls` message + one `tool` result message per call, then the turn loops: re-stream, run any new tool calls, repeat until the model stops calling tools or we hit the hop cap (`MAX_HOPS_NO_PLAN`, raised to `MAX_HOPS_WITH_PLAN` once a plan exists).

Tools (`tools.py` `TOOLS`): `write_file`, `read_file`, `run_bash`, `start_server`, and the two **plan** tools `create_plan` / `update_step` (`PLAN_TOOLS` — pure bookkeeping, no side effects, never gated). `read_file` refuses image files and tells the model to ask the user to attach them (the CLI auto-attaches — see below).

`start_server` runs a long-lived process in the background, waits for readiness, and prints the URL. Server records persist to `servers.json`; `_shutdown_servers` (atexit + SIGTERM/SIGHUP handlers) kills them on exit, and `_adopt_orphaned_servers` offers to clean up survivors of a hard kill on next launch.

## Permission modes & shift+tab

`permissions.Mode`: `DEFAULT` (ask every tool) → `ACCEPT_EDITS` (auto write_file) → `AUTO` (+ run_bash) → `BYPASS` (+ read_file, start_server). `AUTO_APPROVE[mode]` is the set of tool names that skip the y/n confirm. shift+tab cycles via `next_mode`.

- **At the prompt:** the `@kb.add("s-tab")` binding cycles the mode and calls `event.app.invalidate()`.
- **During streaming / tool execution:** `keywatcher.KeyWatcher` reads stdin in cbreak mode and fires the same cycle. It `paused()`s around `session.prompt(...)` so prompt_toolkit owns the termios state cleanly.

The mode indicator is a prompt_toolkit **`bottom_toolbar`** (`statusbar.bottom_toolbar`), NOT a scrollback line — that's what makes it update **live** on shift+tab (the toolbar is re-evaluated on every `invalidate()`). It's right-aligned, degrades on narrow terminals (drops the esc hint, then the cycle hint), has a trailing pad line, and uses `noreverse` to kill prompt_toolkit's default inverted bar. `statusbar.print_line` still prints a one-shot scrollback line once per tool batch (when no prompt/toolbar is active). Don't move the indicator back to a pre-prompt scrollback print — it can't repaint on keypress and the toggle appears frozen.

## Safety guardrails (`safety.py`)

Auto-approval is gated by safety checks; a failing check **never hard-denies** — it downgrades to the y/n confirm (the user is the source of truth) and prints `⚠ auto-approval blocked: <reason>`.

- `is_path_safe_for_auto_write` — denylist (`~/.ssh`, `~/.aws`, `~/.meshapi`, `/etc`, `*.pem`, … — blocks **even under BYPASS**) + cwd-scope for `AUTO`/`ACCEPT_EDITS`. Resolves symlinks first.
- `is_path_safe_for_auto_read` — same denylist, no cwd-scope (reading outside cwd is usually legit; denylist still bites so secrets don't leak to the provider).
- `is_command_safe_for_auto` — blocks destructive/exfil shapes for `AUTO`/`BYPASS` (`rm -rf`, `sudo`, `curl|sh`, fork bomb, `dd`, raw-device writes, reading `/etc/passwd`, …).
- `is_url_safe_for_fetch` — SSRF guard for `/image` URL fetch; re-resolves DNS and rejects loopback/private/link-local/reserved/multicast.
- `SESSION_IMAGE_BYTE_CAP` (100 MB) — cumulative attachment budget per session; per-image hard limit is `attachments.HARD_LIMIT_BYTES` (20 MB).

## Image attachments (`attachments.py`)

`load_image` always base64-encodes into a `data:image/...;base64,...` URL (Mesh docs warn some providers reject public URLs). Surfaced explicitly via `/image`, and **auto-detected** in any prompt: `find_image_tokens` scans for paths/URLs ending in a known image extension and attaches them, rewriting the token to `[Image #N]`.

The tokenizer is **quote-aware** (`_TOKEN_RE = '...' | "..." | \S+`) — it must keep quoted spans whole so drag-dropped paths **with spaces** (e.g. `'/Users/me/snake game/img.png'`) aren't shredded by whitespace splitting. A leading backtick (`` `foo.png` ``) is an explicit "treat as text" escape. Don't regress this back to `text.split()`.

## Mesh-specific conventions

- **Base URL:** `https://api.meshapi.ai/v1` (production).
- **Auth:** `Authorization: Bearer rsk_…` — `rsk_` is the data-plane key prefix.
- **Model format:** `provider/model-name` (e.g. `anthropic/claude-opus-4.8`, `openai/gpt-4o-mini`). See `meshapi-docs/fern/`.
- **Cost in stream:** the final SSE chunk includes a `cost` field (string USD) alongside `usage`. `client.stream_chat` captures it as the generator's last yield (a dict, not a string), which `render.render_stream` separates from content.
- **Routing:** request body accepts a `route` key (`cheapest`, `fastest`, `balanced`). Surfaced via `/route` — Mesh's wedge over generic OpenAI-compat CLIs.

## Reusable utilities

- `render.fmt_usd(value)` — port of `fmtUsd` from `../routersvc-client/src/lib/utils.ts`. **Always 6 decimals** with K/M abbreviations. Use this for every USD amount; never raw `f"{n:.2f}"`. Keeps CLI cost display identical to the dashboard.

## Slash commands

`/model` `/route` `/file` `/image` `/system` `/mode` `/cost` `/clear` `/help` `/exit` (`/quit`, `/q`).

## Distribution & release

- **Version lives in TWO places** — bump both: `pyproject.toml` `version` and `src/meshapi/__init__.py` `__version__`. Verify with `python -m meshapi --version`.
- **PyPI** (`meshapi-code`): `.github/workflows/publish.yml` builds + uploads on a **`v*` tag push** via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (no token). A plain push to `main` does NOT publish. Trusted Publisher = `aifiesta/meshapi-code` repo + `publish.yml`.
- **Release flow:** commit to `main` → (only on explicit ship-it) `git tag -a vX.Y.Z -m "…" && git push origin vX.Y.Z` → watch with `gh run watch <id> --exit-status` → confirm at `https://pypi.org/pypi/meshapi-code/<version>/json` (the `/json` "latest" field is CDN-cached and lags a few minutes; the version-specific endpoint is authoritative).
- ⚠️ **Never auto-publish.** Stop at "ready to test" and wait for an explicit ship-it before tagging/pushing a `v*` tag. PyPI uploads of a given version are immutable — you can't re-upload `0.4.3`.
- **Install paths users use:** `pipx install meshapi-code`, `uv tool install meshapi-code`, `pip install meshapi-code`. Upgrade: `pipx upgrade meshapi-code`.
- **npm port** (`meshapi-code`): planned. Node rewrite using `ink` + `chalk`, same UX.

## Gotchas / hard-won learnings

- **`pipx` vs editable shadowing:** an activated `.build-venv` (`pip install -e .`) prepends its `bin/` to `$PATH`, so `meshapi` runs the editable working-tree copy and shadows the pipx-installed one. `pipx upgrade` still updates the pipx copy; it just won't be what `meshapi` resolves to until that venv is off PATH. Editable installs report the working-tree version live.
- **Testing prompt_toolkit in a pty:** it needs a terminal size or it can't render (toolbar/CPR). Set `TIOCSWINSZ` via `fcntl.ioctl` AND answer the `\x1b[6n` cursor-position query with `\x1b[<row>;<col>R`, or you'll see "terminal doesn't support CPR" and no toolbar. shift+tab to send is `\x1b[Z` (CSI Z).
- **No test suite** — verify changes by importing every module (`PYTHONPATH=src python -c "import meshapi.<mod>"`), unit-calling the pure functions (safety guards, `find_image_tokens`, `bottom_toolbar`), and a pty harness for the interactive bits.

## Testing the REPL end-to-end

```bash
MESHAPI_API_KEY=rsk_… meshapi
> hello                                  # streamed markdown reply, then cost line
> /model openai/gpt-4o-mini              # switch model mid-session
> /route cheapest                        # ask gateway to pick cheapest route
> /file ./pyproject.toml                 # inject file into context
> write a hello.py and run it            # tool calling: write_file + run_bash
> [shift+tab]                            # cycle permission mode (toolbar updates live)
> describe '/path/with spaces/img.png'   # auto-attaches the image (quote-aware)
> /cost                                  # cumulative session spend
> /exit
```
