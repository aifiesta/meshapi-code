# meshapi-code

[![PyPI](https://img.shields.io/pypi/v/meshapi-code)](https://pypi.org/project/meshapi-code/)
[![Python](https://img.shields.io/pypi/pyversions/meshapi-code)](https://pypi.org/project/meshapi-code/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/aifiesta/meshapi-code/blob/main/LICENSE)

Agentic terminal CLI for [Mesh API](https://meshapi.ai) — one OpenAI-compatible key, 300+ models. Plans, writes files, runs commands, starts dev servers, searches the web — with streaming markdown, live cost, and permission modes. Modeled on Claude Code.

📚 **Docs:** [Install guide (Windows & macOS)](https://github.com/aifiesta/meshapi-code/blob/main/INSTALL.md) · [Upgrading](https://github.com/aifiesta/meshapi-code/blob/main/UPGRADE.md) · [Changelog](https://github.com/aifiesta/meshapi-code/blob/main/CHANGELOG.md) · [Release notes](https://github.com/aifiesta/meshapi-code/releases)

```
$ meshapi
███╗   ███╗███████╗███████╗██╗  ██╗
████╗ ████║██╔════╝██╔════╝██║  ██║
██╔████╔██║█████╗  ███████╗███████║   ✦  meshapi 0.5.2
██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║   cwd:   ~/code/myproj
██║ ╚═╝ ██║███████╗███████║██║  ██║   model: anthropic/claude-sonnet-4.5
╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   route: off

type /help for commands, /exit to quit

──────────────────────────────────────────────────── myproj · main
› add a healthcheck endpoint to server.py and run the tests
──────────────────────────────────────────────────────────────────

✦ anthropic/claude-sonnet-4.5 · hop 2
⚙ write_file: server.py (+14 −2)      ✓ OK
⚙ run_bash: pytest -q                 ✓ exit 0
   anthropic/claude-sonnet-4.5  •  942→318 tok  •  $0.001234  •  session $0.001234  •  6.1s
```

## Install

**macOS / Linux (Terminal):**

```bash
brew install pipx && pipx ensurepath   # if you don't have pipx yet
pipx install meshapi-code
meshapi
```

**Windows (PowerShell):**

```powershell
py -m pip install --user pipx
py -m pipx ensurepath                  # then open a NEW PowerShell window
pipx install meshapi-code
meshapi
```

Alternatives on any OS: `uv tool install meshapi-code` or `pip install meshapi-code`.
PyPI package is `meshapi-code`; the command on your `$PATH` is `meshapi` (same split Claude Code uses: `@anthropic-ai/claude-code` → `claude`).

**First run asks for your API key** (get one at [app.meshapi.ai](https://app.meshapi.ai)) — hidden input, verified live, saved to `~/.meshapi/credentials`. No environment variable needed. To set one anyway (CI, scripts):

```bash
# macOS / Linux
export MESHAPI_API_KEY=rsk_your_key_here
```

```powershell
# Windows (PowerShell) — current session:
$env:MESHAPI_API_KEY = "rsk_your_key_here"
# persistent:
setx MESHAPI_API_KEY "rsk_your_key_here"
```

## Upgrade

Use whichever tool you installed with — same commands on macOS, Linux, and Windows:

| Installed with | Command |
|---|---|
| **pipx** | `pipx upgrade meshapi-code` |
| **uv** | `uv tool upgrade meshapi-code` |
| **pip** | `pip install --upgrade meshapi-code` |

From 0.5.1 onward you rarely need these: **the CLI checks PyPI in the background and offers a one-key upgrade** when a new version ships (`/update` checks on demand; declining a version won't re-nag).

Verify with `meshapi --version`. If it still shows an old version, a second older copy is shadowing the new one on your PATH — find every copy with `which -a meshapi` (macOS/Linux) or `where.exe meshapi` (Windows), remove the stray (often an old `pip install --user`: `python3 -m pip uninstall meshapi-code`), then `hash -r` or open a new terminal. Full troubleshooting: [upgrade guide](https://github.com/aifiesta/meshapi-code/blob/main/UPGRADE.md).

## What it does

- **Agentic tool calling** — the model plans multi-step work, reads/writes files, runs shell commands, starts dev servers in the background (with port auto-detection), and searches the web. Every step gated by permission modes.
- **Repo memory** — the agent remembers your project across sessions: files it touches are structurally mapped (zero extra tokens) into `~/.meshapi/context/` (never your repo), durable decisions persist via a `remember` tool, and re-reads of unchanged files are deduped. Next session starts warm. `/memory` inspects, `/memory clear` deletes, `/memory off` disables.
- **Quality guard** — stub code (`// Add game logic here`) is caught before the model declares victory: one automatic fix-it pass, then an honest warning naming the files and suggesting a stronger model. No more "Server's up!" over a blank page.
- **Self-healing tool calls** — malformed arguments are repaired client-side; the model never re-reads its own broken JSON. Ends the retry doom-loop, biggest win on cheaper models.
- **Type while it works** — the input stays live during streaming; Enter stacks messages that auto-run in order; ESC aborts a turn; unfinished text prefills the next prompt. *(macOS/Linux; on Windows input is available between turns.)*
- **Fuzzy model picker** — `/model qw` pops a menu of every qwen model; `gpt4m` finds `openai/gpt-4o-mini`. `/models` browses the catalog with context sizes and $/1M pricing.
- **Auto-routing** — `/route auto` lets Mesh's gateway pick the best model per prompt; the resolved model shows in the status line. `/fallback m1 m2` sets an ordered failover list.
- **Real cost per turn** — Mesh returns `cost` in the SSE tail; surfaced after every reply and accumulated per session.
- **Streaming with live status** — markdown rendering, phase-aware spinner (`preparing write_file (↓ 3.2k chars)`), always-visible permission mode, background servers listed under the prompt.

## Tool calling & permission modes

| Tool | What it does |
|---|---|
| `read_file` | Read a file (image files are auto-attached instead). |
| `write_file` | Create or overwrite a file; parent dirs created; scanned by the quality guard. |
| `run_bash` | Shell command in the working directory. 120s timeout, output capped. |
| `start_server` | Long-running dev server in the background — detects the port in your command, adopts what it actually binds, shows progress, killed on exit. |
| `web_search` | Search the web through the Mesh gateway. |
| `create_plan` / `update_step` | The model's visible step-by-step plan. |
| `remember` | Persist a durable project note for future sessions (repo memory). |

Permission modes, cycled live with **Shift+Tab** (works mid-run on macOS/Linux):

- **default** — ask for every tool call
- **accept edits** — auto-approve file writes inside the working directory
- **auto** — plus shell commands and web searches
- **bypass** — auto-approve everything (still asks before `rm -rf`, `sudo`, writes to `~/.ssh`, …)

At any approval prompt, answer **`a`** to allow that tool for the rest of the session.

```bash
meshapi --mode bypass     # start in bypass (macOS/Linux/Windows alike)
```

## Slash commands

| Command | What it does |
|---|---|
| `/model <name>` | Switch model — **fuzzy tab-completion** from the live catalog |
| `/models [free\|query]` | Browse the catalog: context, capabilities, $/1M pricing |
| `/route auto\|off\|preview` | Gateway auto-routing; `preview` shows the pick without running |
| `/fallback <m1> <m2>\|off` | Ordered fallback models if the primary fails |
| `/reasoning <level>` | `high`/`medium`/`low`/`none`/`off` reasoning effort |
| `/mode <perm>` | `default`, `accept-edits`, `auto`, `bypass` (Shift+Tab cycles) |
| `/file <path>` | Inject a text file into the conversation |
| `/image <path\|url>` | Attach an image (drag-dropped paths auto-attach too) |
| `/clear-attach` | Drop queued image attachments |
| `/system <text>` | Replace system prompt and reset chat |
| `/optimize <dial>` | Token-savings dial (beta), see below |
| `/memory [notes\|clear\|on\|off]` | Repo memory: map + notes from past sessions |
| `/login` | Set or replace your API key |
| `/update` | Check PyPI and upgrade |
| `/cost` `/clear` `/help` `/exit` | The usual |

## Keyboard & live controls

| Key | When | What it does |
|---|---|---|
| **Shift+Tab** | anytime¹ | Cycle permission mode — applies to the *next* tool call, visible live |
| **type + Enter** | while the model works¹ | Stack a message; it auto-runs when the turn ends (`(N queued)` shows live) |
| **ESC** | while the model works¹ | Abort the turn (between deltas/hops/tool calls) |
| **Ctrl+C** | anytime | Abort the turn and discard stacked messages |
| **`a`** | at any approval prompt | Approve + auto-approve that tool for the rest of the session |
| **Tab / arrows** | at the prompt | Fuzzy completion menu for commands and model IDs |
| **↑** | at the prompt | Prompt history (persists across sessions, secrets scrubbed) |

¹ macOS/Linux; on Windows these work at the prompt between turns.

## Mesh Optimize (beta)

> **Beta feature.** Off by default. `/optimize off` bypasses everything.

One dial that cuts token spend on every request. `/optimize 0.3` enables it:

| dial | levers | quality impact |
|---|---|---|
| 0 | off, byte-identical passthrough | none |
| 0 to 0.2 | prompt-cache breakpoints on stable prefixes, max_tokens defaults | none |
| 0.2 to 0.95 | plus pruning of tool results the model already consumed | minimal |

Every turn re-sends the whole conversation — a 5000-line test log from ten turns ago is billed again on every request after it. The pruning lever truncates consumed outputs (last 4 messages untouched); the cache lever marks the stable prefix for the provider's ~90% cache discount. Savings are only claimed when measurable; if the gateway rejects an optimized request, the raw request is retried automatically. Reference implementation: [mesh-optimize on GitHub](https://github.com/raushan-aifiesta/mesh-optimize).

## Config & state

`~/.meshapi/` (all files 0600):

| File | What |
|---|---|
| `credentials` | Your API key (set on first run, `/login` replaces) |
| `config.json` | Settings — model, auto_route, fallback_models, reasoning_effort, optimize (never the key) |
| `history` | Prompt history (secrets scrubbed) |
| `servers.json` | Background-server records for crash recovery |
| `update_check.json` | Update-checker cache |
| `toolcall_failures.jsonl` | Forensics for malformed tool calls |

Env overrides: `MESHAPI_API_KEY`, `MESHAPI_BASE_URL`.

## Platform notes

- **macOS / Linux** — everything above.
- **Windows** — fully supported for chat, tools, servers, completion, and the update *check*; three POSIX-only niceties degrade gracefully: mid-run typing/queueing/ESC (input is available between turns), mid-run Shift+Tab (works at the prompt), and in-place self-upgrade (the CLI prints the exact command to run instead — the running `.exe` is file-locked).

## About Mesh API

[Mesh API](https://meshapi.ai) is a unified LLM gateway: one API key, 300+ models from OpenAI, Anthropic, Google, Meta, Mistral, DeepSeek, Alibaba, and more. OpenAI-compatible — change the model name, leave everything else alone.

- **Zero platform fees for 12 months.** You only pay for tokens.
- **Auto-routing.** Send `model: "auto"` and the gateway picks the best model per prompt.
- **Automatic failover.** Provider down? Your request routes to another.
- **Highest rate limits.** Capacity pooled across providers.
- **Zero data retention.** Prompts and completions pass through; not stored.
- **Full observability.** Every request, token, cost tracked in real time; per-key limits.

Built by the founders of [TagMango](https://tagmango.com) (YC W20) and [AI Fiesta](https://aifiesta.ai) (1M+ users across India).

## Why this CLI exists

Any generic OpenAI-compatible CLI talks to Mesh. `meshapi` adds what a generic one can't: the gateway-only `cost` field after every turn, `/route auto` + `/models` driving Mesh's gateway-side selection, an agentic loop hardened for cheap models (argument repair, quality guard), and 300+ models behind one fuzzy picker.

## Roadmap

- ✅ 0.5.1 — first-run key setup, update checker, auto-routing, fuzzy model picker, web search, quality guard, self-healing tool calls, always-visible input, ESC abort
- ✅ 0.5.2 — repo memory: zero-token context capture, warm-start repo maps, `remember` notes, read-dedupe
- 0.6 — something special 👀 (+ optional [graphify](https://github.com/Graphify-Labs/graphify) backend for the memory layer)
- later — `npm i -g meshapi-code` (Node port), Homebrew tap

## License

[Apache 2.0](https://github.com/aifiesta/meshapi-code/blob/main/LICENSE)
