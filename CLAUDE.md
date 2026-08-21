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

State under `~/.meshapi/`: `config.json` (settings, never the API key — `save_config` strips it), `credentials` (the API key, single line; created 0600 via `os.open` so there's no readable window), `history` (input history, scrubbed + 0600), `servers.json` (backgrounded server records for crash-recovery), `update_check.json` (last known PyPI version + `declined_version` so a declined release never re-nags), `toolcall_failures.jsonl` (+`.jsonl.1` rotation — raw args of every doomed/repaired tool call, for model-vs-gateway attribution). All written 0600.

**Key resolution order:** `MESHAPI_API_KEY` env > `MESH_API_KEY` env > `~/.meshapi/credentials` > legacy hand-edited `config.json` (auto-migrated to `credentials` on load). **First run with no key anywhere:** if stdin is a tty, `commands.prompt_for_api_key` walks the user through it — hidden input, best-effort live verify against `GET /models` (only an explicit 401/403 rejects; network trouble saves with a warning so onboarding works offline), persisted to `credentials`. Non-tty (CI/pipes) keeps the hard error + exit 1. `/login` re-runs the same flow mid-session.

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
  askui.py        # arrow-key option picker (prompt_toolkit Application) for ask_user
  pricing.py      # client-side cost: usage × catalog rates (gateway sends no cost)
  router.py       # smart routing: local cohort classify + table-driven pick (fail-open)
  styles.py       # output styles (prose contract only) → appended to system prompt
  loopguard.py    # stall detection (identical-batch cycles), retry policy, batch sealing
  compact.py      # token estimation + deterministic 2-phase history compaction
  memory.py       # repo memory: warm-start map, remember notes, read-dedupe
  completer.py    # fuzzy tab-completion: slash commands + model IDs
  update.py       # PyPI version check (daemon thread) + y/n upgrade offer
  render.py       # rich Console singleton, render_stream, fmt_usd
  __main__.py     # python -m meshapi
```

## Agentic tool-calling loop

`handle_tool_calls` (cli.py) appends the assistant `tool_calls` message + one `tool` result message per call, then the turn loops: re-stream, run any new tool calls, repeat until the model stops calling tools. **There is no arbitrary hop cap** (the old `MAX_HOPS_NO_PLAN`=8/`MAX_HOPS_WITH_PLAN`=60 wall died in 0.5.9): turns run to completion, bounded instead by real checks — `loopguard.StallDetector` (period-1/2 identical-batch cycles → nudge at 3, renudge at 6, stop at 9 per `cfg["stall_policy"]`; `/stall keep-going` never stops), an optional user checkpoint (`/hops <n>`, `cfg["max_hops"]`, 0 = unlimited), and auto-compaction (`compact.py`, `cfg["auto_compact"]`) that keeps history under the model's context limit (70% threshold; truncate old tool results → fold whole assistant/tool runs into structural system summaries; never bisects a batch; remaps/drops `session_reads` entries). **Compaction is recoverable, not destructive**: before folding, history is appended to `~/.meshapi/transcripts/<session_id>.jsonl` (0600, delta-only via `state["_transcript_upto"]`) and the summary names that path so the model can read back exact detail — the same escape hatch Claude Code's compact summary uses. `state["_compact_exhausted"]` latches when compaction can no longer get under the limit (no per-hop thrash; re-armed each user turn). Every deliberate pause appends `_pause_breadcrumb` so "continue" resumes without redoing work. Per-hop streaming rides `_stream_hop_with_retry`: exponential backoff+jitter on network errors and retryable statuses (408/429/5xx/529), **`Retry-After` honored when present** (refused above `RETRY_AFTER_MAX`=90s rather than freezing), transient in-band errors retried, in-band CONTEXT errors compact-aggressively-and-retry-once, empty responses retried twice, and a **`max_tokens` overflow 400 parsed and repaired** (`loopguard.parse_max_tokens_overflow`/`adjusted_max_tokens` — shrink the output ask, keep ALL history; below `FLOOR_OUTPUT_TOKENS`=1024 it becomes a context problem instead). Last resort: **one non-streaming attempt** (`client.complete_chat` via `_try_non_streaming`) — Mesh's server-side retry *and provider fallback* cover non-streaming requests only, so a streaming-only client otherwise gets zero gateway resilience. A caller-forced `cfg["max_tokens"]` is applied last to every attempt in `client.stream_chat` so optimize's own default can't re-trigger the same 400. Interrupts and errors keep completed work: `_finalize_interrupted_turn` seals a half-answered batch with stub tool results (never pops the assistant message — side effects already happened; dangling tool_use 400s on strict gateways) and appends a resume breadcrumb; only a zero-progress turn rolls back clean-slate. `session_cost` commits in a `finally` (spend is tracked even on abort).

Tools (`tools.py` `TOOLS`): `write_file`, `read_file`, `run_bash`, `start_server`, `web_search` (POST `{base_url}/web/search` through the gateway; needs cfg, so `execute()` takes an optional third `cfg` param — filesystem/shell tools ignore it), and the two **plan** tools `create_plan` / `update_step` (`PLAN_TOOLS` — pure bookkeeping, no side effects, never gated). `read_file` refuses image files and tells the model to ask the user to attach them (the CLI auto-attaches — see below), and caps output at `READ_LIMIT` (80k chars) with a prescriptive truncation notice — an uncapped read inside the compactor's keep-recent window would make context errors unrecoverable. `run_bash` accepts an optional `timeout` arg (default 120s, clamp 5–600) for known-slow builds.

**Doomed-call defense (three layers).** Providers occasionally stream broken `tool_calls` deltas, and models (especially cheap ones) emit malformed argument JSON; every layer below exists because its failure shape was seen live.

1. **`client.ToolCallAccumulator`** repairs delta-level damage — deltas with no `index` (would merge parallel calls into concatenated-JSON garbage), argument fragments arriving under a different index than the call's name (two-pass merge in `finalize()`: donors merge into the nearest-by-index named call whose args don't already parse, trying append then prepend, acceptance-gated — a lower-index donor used to be silently dropped), missing `id`s (synthesized `call_<n>`), nameless leftovers (dropped, counted in `meta["accum_dropped"]`). Never corrupt a call whose args already parse.
2. **`cli._prepare_call`** classifies every call before anything runs: strict parse → `strict=False` (normalizes raw control chars — literal newlines in `content`) → `tools.repair_tool_args` (narrow missing-comma-only repair; truncation checked FIRST and **never** repaired — no fabricated closures, half-written files are worse than failures). Repaired/normalized calls proceed through the FULL approval+safety path with a visible ⚠/dim note. **The assistant history message is built from sanitized `history_args`** — raw only when valid JSON, canonical `json.dumps` for repaired/normalized, `"{}"` for doomed — because replaying the model's own malformed JSON few-shot-primes it into repeating the mistake verbatim (observed: 4 identical parse errors in a row), and strict Anthropic-translating gateways 400 on unparseable `tool_use.input`.
3. **Prescriptive skip feedback** (`cli._doom_feedback`): doomed kinds (invalid/truncated/unparseable) skip *before* the approval prompt; feedback carries the ±40-char raw window around the parse error (`tools.parse_error_context`), the expected schema (`tools.schema_hint`, derived from TOOLS), and keys-present for missing-field cases. A per-turn `state["doom_streak"]` counter escalates: the 2nd consecutive write_file failure adds escape hatches (single-line JSON / run_bash heredoc with unique delimiter / split files). Streak resets on successful execution and each user turn. The wall for a truly stuck model is `StallDetector`'s doom counter: `DOOM_STOP_HOPS`(=8) consecutive all-doomed hops → pause with history kept (the old hop-cap wall, rebuilt as a real check).

**Forensics:** every doomed/repaired/normalized call is appended to `~/.meshapi/toolcall_failures.jsonl` (raw args preserved even though history is sanitized; 0600; >1 MB rotates to `.jsonl.1`). `stream_chat` also counts SSE data lines that fail to json-parse (`meta["dropped_chunks"]` + first-sample) — nonzero implicates the gateway relay (routersvc), not the model. This is how to attribute corruption when debugging.

**Known asymmetry (pre-existing):** `run_bash` can write anywhere via shell redirection without `is_path_safe_for_auto_write`'s path checks — the heredoc escape hatch grants nothing new. Optional follow-up: redirect-target check in `safety.is_command_safe_for_auto`.

## Repo memory (`memory.py`)

Per-repo persistent context under `~/.meshapi/context/<sha256(normcase(resolved cwd))[:16]>/` — `repomap.json` (structural map, 0600) + `memory.md` (model-authored notes via the ungated `remember` tool). **Capture is zero-token**: hooks in handle_tool_calls' write/read paths extract symbols (per-language regex, `extract_symbols`) from content already in hand. `build_system_prompt` appends a token-capped (~1.5k) REPO MEMORY block — notes first, then files by recency, `~changed` marks from an mtime+size stat pass (dead files lazily compacted), explicitly framed as DATA not instructions (the store is a self-persisting prompt-injection channel — control chars stripped, lengths clipped, `/memory clear` is the recovery path). `/memory [notes|clear|on|off]`; `cfg["repo_memory"]` gates injection+capture.

**Read-dedupe invariant (LOAD-BEARING):** `dedupe_read` answers a repeat read with a stub ("content already in your context") ONLY when provably true: write-sourced content rides in assistant `tool_calls` messages which optimize NEVER prunes → safe at any dial; read-sourced content is gated by `optimize.survives_pruning(chars, dial)` — that helper lives next to the pruning constants and a drift-guard unit test asserts it against `prepare()`'s real output. **Changing `_TRUNCATE_TO_CHARS`/`_KEEP_RECENT_MESSAGES`/the role filter in optimize.py requires revisiting it.** Other guards: sha256 re-check against disk at dedupe time (ground truth), `stubbed_last` anti-loop (an immediate re-ask returns the body), `msg_index` invalidation in `_finalize_interrupted_turn` (was `_drop_in_flight_turn`) + explicit drop/remap in `compact.compact_history` (any fixup exception clears `session_reads` entirely) + `session_reads` reset in `/clear` `/system`, 300-char minimum. Every condition fails toward a normal read — a wrong "already in context" gaslights the model.

Known gap (accepted, documented): `run_bash` heredoc writes are invisible to capture — same asymmetry as the quality guard; the dedupe hash re-check still protects correctness.

## Quality guard (stub detection + fix-it hop)

Cheap models declare victory on skeletons ("// Add game logic here" → blank page → "Server's up!"). The guard makes that impossible to miss:

- **Detection** (`tools.find_stub_markers`, pure, never raises): only `STUB_SCAN_EXTS` code files. Tier 1 = narrative phrases, IGNORECASE ("add … logic here", "goes here", "coming soon", "rest of the code", NotImplementedError…). Tier 2 = bare `TODO|FIXME|TBD` **case-sensitive + comment-context-only, URLs scrubbed first** — this is what keeps a *todo app* (`<h1>TODO List</h1>`, `todo` variables), `<input placeholder=…>`, `::placeholder`, and `https://…/todos` from false-flagging. No empty-body heuristic (no-op callbacks/`pass` are idiomatic). Evidence capped at 3/file, deduped by pattern.
- **Tracking**: scan after each successful write in `handle_tool_calls`, keyed by `resolve()`d path (a clean rewrite via `./script.js` clears the `script.js` entry). All guard state resets per user turn.
- **Fix-it hop**: when the model ends its turn with flagged files — ONE extra hop per turn, never past the user's /hops limit (if set), suppressed when the user asked for scaffolding (`tools.stub_guard_suppressed`: scaffold/skeleton/boilerplate/"with TODOs"/"don't implement" — bare "todo"/"placeholder" do NOT suppress). The instruction rides as a **transient consume-once system message** (same mechanism as the plan reminder; a persistent copy would go stale the moment the rewrite lands), placed LAST in `_extras` (recency wins on cheap models), **tool-name-free** (the XML-mode trap applies to injected messages too), and explicitly overrides start_server's end-the-turn instruction while forbidding a server restart.
- **Final warning + breadcrumb**: post-loop (covers both break paths incl. hop-cap; exception paths skip it) — per-file evidence, tips (`/model anthropic/claude-sonnet-4.5`, `/route auto` when off, "reply 'implement the full logic, no placeholders'"), plus a persistent breadcrumb so a follow-up "implement fully" hands the model concrete file+marker targets.
- Known gaps (accepted): heredoc writes bypass detection (same run_bash asymmetry as above); files not written this turn are never judged; a turn like "add skeleton enemies to my game" suppresses the guard for that turn.

## Always-visible input, queueing, ESC (keywatcher + render footer)

The keywatcher is now a full type-ahead capturer (`_InputParser` — pure byte-level state machine, unit-testable): shift+tab cycles the mode, printable text accumulates in `watcher.typeahead`, **Enter queues the buffer** into `state["input_queue"]` (each drains as its own full turn before the next interactive prompt; slash commands route normally; drained messages print the same highlighted `› text` frame as typed ones), **bare ESC aborts the turn** (checked between deltas / hops / tool calls via `state["esc_interrupt"]` — a blocked read still needs ctrl+c). Un-submitted type-ahead prefills the next prompt via `session.prompt(default=…)`. Ctrl+c discards the queue (with a printed count) but keeps the type-ahead.

Hard-won specifics: Enter-vs-paste heuristic (newline ending a chunk pends ~30ms; more bytes = paste = literal `\n` — otherwise a multi-line paste becomes N API calls); CR/CRLF normalized BEFORE the state machine (cross-chunk pairs); 8-bit CSI `0x9b` is deliberately NOT recognized (valid UTF-8 continuation byte — pasted `›` would corrupt); runaway CSI sequences are poisoned and swallowed through their final byte so param junk never leaks into type-ahead; `take_typeahead()` only under `paused()` (single-writer, no locks); the self-pipe/CPR logic is load-bearing — do not restructure it.

**Live footer** (`_StreamView._footer`, streaming only, never in transcripts): dim rule → mode row (read per frame — a mid-stream shift+tab shows within one 12fps refresh; hint `shift+tab to cycle · esc to interrupt` is now truthful) → `› typeahead█ (N queued)` row. **Tail-crop**: rich.Live's default overflow crops the BOTTOM, so over-tall streams used to hide the spinner; `_StreamView` now renders only the newest body lines with spinner+footer pinned (try/except → plain-Markdown fallback). `render_stream(events, header, state)` sets `state["live_active"]` (watcher's one-shot queue-ack print is suppressed while a Live owns the screen) and marks `done` in a finally (ctrl+c no longer leaves a stale frame).

## Live activity UI

- **Framed input**: the box's top edge is the cwd rule (`──── meshapi-test · main`, right-aligned title, branch via `cli._git_branch()`, 5s cache); the bottom edge is `bottom_toolbar` row 1 — a full-width `─` border re-evaluated on every repaint (tracks resizes live). Under it: the mode row, an optional `● serving localhost:5174 · …` row when `state["servers"]` is non-empty (`print_line` mirrors it), and a trailing blank. No sticky DEC regions — that fight was lost once already (see statusbar.py docstring).
- **Streaming header**: `render_stream(events, header="model · hop N")` shows a `✦ model · hop 3` line above the live stream (cli passes "auto" when auto-routing; hop only when >1). Live-only — `_StreamView.done` drops it so transcripts stay clean.
- `render._StreamView` is phase-aware via `{"stream_progress": {tool, chars}}` events yielded by `client.stream_chat` during tool-call delta accumulation (popped by `render_stream`, never merged into meta): "meshing around… 3.1s" → "still meshing · ↓ ~1.2k tok · 8.4s" → "preparing write_file (↓ 3.2k chars) · 12.4s". Before this, an 8KB write_file argument streamed in dead silence.

`start_server` runs a long-lived process in the background, waits for readiness, and prints the URL. Server records persist to `servers.json`; `_shutdown_servers` (atexit + SIGTERM/SIGHUP handlers) kills them on exit, and `_adopt_orphaned_servers` offers to clean up survivors of a hard kill on next launch.

**Port resolution (hard-won):** precedence = explicit port in the COMMAND (`_extract_command_port`: `--port N`/`-p N` flags incl. docker `-p host:cont`, `host:N` tokens, bare 1024-65535 digit token — NOT ports inside URLs) > `port` arg > auto-pick. A bare `python -m http.server` gets the port appended (`_maybe_append_port`) because http.server ignores the PORT env — the live failure was 7×30s timeouts waiting on an auto-picked port while the server sat on the command's port. Safety net: the wait loop scans every 2s what the process group ACTUALLY listens on (`_discover_listen_ports`: one `lsof -nP -g <pgid>` call, ~25ms, exit 1 = none; `ss`+`ps` fallback; POSIX-only, `[]` elsewhere) and adopts a reachable mismatched port with a NOTE to the model. Waits show a 5s ticker (+ newest server-output line); ctrl+c during the wait kills the unrecorded process group (was a leak); exit-0 daemonizers get a 5s grace scan instead of "server exited". A busy expected port that belongs to a server in `state["servers"]` returns "that's YOUR server, don't restart it" — kills restart loops.

## Permission modes & shift+tab

`permissions.Mode`: `DEFAULT` (ask every tool) → `ACCEPT_EDITS` (auto write_file) → `AUTO` (+ run_bash, web_search) → `BYPASS` (+ read_file, start_server). `AUTO_APPROVE[mode]` is the set of tool names that skip the y/n confirm. shift+tab cycles via `next_mode`. web_search rides with run_bash: its only risk is leaking the query off-machine, and run_bash can already `curl` anything; the DEFAULT-mode confirm shows the query verbatim (🔎 line).

**Live mode reads + session allowlist:** `handle_tool_calls` reads `state["mode"]` PER CALL (not a frozen batch param) — a shift+tab during a long tool run applies to the very next call, and a mid-batch change prints the mode line so the switch is visible. The approval prompt accepts `a` ("always for this tool this session") → adds the tool to `state["session_allow"]`, checked alongside `AUTO_APPROVE`. ⚠ Session-allowed approvals are safety-checked at **AUTO strictness** (`safety_mode = Mode.AUTO`) — the guards deliberately no-op in DEFAULT (they assume the caller confirms), so passing the raw mode would disarm them entirely for allowlisted tools; this exact hole was caught by a test that ran `sudo rm -rf /`. The "esc to interrupt" hint is only true at the prompt — during execution only ctrl+c interrupts (keywatcher handles solely shift+tab / CSI Z).

- **At the prompt:** the `@kb.add("s-tab")` binding cycles the mode and calls `event.app.invalidate()`.
- **During streaming / tool execution:** `keywatcher.KeyWatcher` reads stdin in cbreak mode and fires the same cycle. It `paused()`s around `session.prompt(...)` so prompt_toolkit owns the termios state cleanly.

The mode indicator is a prompt_toolkit **`bottom_toolbar`** (`statusbar.bottom_toolbar`), NOT a scrollback line — that's what makes it update **live** on shift+tab (the toolbar is re-evaluated on every `invalidate()`). It's right-aligned, degrades on narrow terminals (drops the esc hint, then the cycle hint), has a trailing pad line, and uses `noreverse` to kill prompt_toolkit's default inverted bar. `statusbar.print_line` still prints a one-shot scrollback line once per tool batch (when no prompt/toolbar is active). Don't move the indicator back to a pre-prompt scrollback print — it can't repaint on keypress and the toggle appears frozen.

## Update check (`update.py`)

Every launch fires a daemon thread (`meshapi-update-check`) against `https://pypi.org/pypi/meshapi-code/json`. **Poll, never push**: the thread only writes `update_state["latest"]` + refreshes `update_check.json`; it never prints or prompts. `maybe_offer` consumes the result at exactly two safe points — after the banner (before `watcher.start()`, stdin still canonical) and at the top of each prompt-loop turn — so the y/n offer can never collide with prompt_toolkit or streaming. Declining a version persists `declined_version` (no re-nag for that release; `/update` asks explicitly and ignores it). **Windows never upgrades in-process** — the running `meshapi.exe` shim is file-locked (WinError 5), so it prints the command and tells the user to exit first; POSIX runs the auto-detected command (`sys.prefix` contains `pipx/venvs` → pipx, `uv/tools` → uv tool, else `python -m pip`) with inherited stdio. Nothing in `update.py` may be POSIX-only (the 0.4.5 SIGHUP lesson).

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

Public docs: **https://developers.meshapi.ai**. Don't scrape the HTML — it publishes `llms.txt` (page index), `llms-full.txt` (~581 KB, whole docs) and `/api/openapi.json` (OpenAPI 3.1, 53 paths). The models-list page is client-rendered and empty; the catalog only exists at `GET /v1/models`.

- **Base URL:** `https://api.meshapi.ai/v1` (production). `/v1` is a **stable namespace, not a version** — `/v2` is not planned.
- **Auth:** `Authorization: Bearer rsk_…` — `rsk_` is the data-plane key prefix.
- **Model format:** `provider/model-name` (e.g. `anthropic/claude-opus-4.8`, `openai/gpt-4o-mini`). **Dots, not hyphens** — `claude-sonnet-4-6` 404s. A few catalog rows carry no prefix at all (`gpt-4o-transcribe`), so `/model` validation must not assume a `/`.
- **Versioning is a dated header:** `X-Mesh-Version: 2026-08`. Sending nothing gets the **oldest** supported version (the baseline), deliberately not the newest, so unpinned traffic never moves. A bad value is a hard `400 invalid_api_version`; an *empty* header value is malformed, so omit it entirely rather than sending it blank. Every response echoes `x-mesh-version`. **The CLI does not pin today** — it rides the baseline.
- **Cost: the gateway sends none on chat, but bills every request.** No `cost` field on streaming or non-streaming responses (re-verified 2026-08-19). The authoritative figure is `cost_usd` on `POST /v1/usage/events`, keyed by the `x-request-id` response header — available **immediately**, streaming included (0s lag, measured). `client` captures `meta["request_ids"]` per hop; after the turn `pricing.fetch_actual_costs(cfg, ids, since)` fetches and sums only those ids (guards against concurrent traffic on the same key). ⚠️ Its `org_id` is marked required by the schema but is **ignored for API-key callers** — send `""` (a `null` fails validation). Fallback when the lookup fails: `pricing.estimate_cost` (usage × catalog per-1M rates: fresh vs `cached_tokens` split, `discount_pct`, auto-route `classifier_*_tokens`), returning **None never 0.0** when unpriceable. UI shows `~` only for estimates.
- **Response headers worth capturing:** `x-request-id` (`req_…`, the dashboard log key — worth surfacing for support), `x-cache: HIT` (**present only on a hit; there is no `MISS` value**), `x-provider-latency-ms`, `x-mesh-routing-attempts`, `x-mesh-routing-fallback`, `x-ratelimit-*`, `x-auto-routed`, `x-resolved-model-id`.
- **Gateway response cache** (free, on by default): key = sha256 of `model` + resolved provider + `messages` + `max_tokens` + `temperature` + `stop` + `response_format`. `stream` is **not** in the key, so a streaming call can be served from a stored non-streaming one — replayed as SSE with id `chatcmpl-cached-…`, the entire body in **one chunk**. Requires `temperature` 0-or-omitted and **no `tools`** — so agentic turns are never cached, but plain chat turns are. Opt out per request with `X-Mesh-Cache: no-store` or `"cache": false`.
- **Routing:** there is **no** `route: cheapest/fastest/balanced` request key — it never existed gateway-side (verified against the routersvc source; unknown body fields are silently dropped). Real routing is **auto-routing**: send `model: "auto"` and the gateway's Auto Router picks a concrete model per prompt; the resolved model comes back in the SSE chunks' `model` field and the `X-Resolved-Model-Id` response header. `POST /v1/router/select` previews the pick without inference. Surfaced via `/route auto|off|preview` (`cfg["auto_route"]`). **The classifier is billed to you** — auto-routed `usage` carries `classifier_prompt_tokens`/`classifier_completion_tokens`/`classifier_tokens`, and they dominate short prompts (~1.4k classifier prompt tokens on a 3-word ask).
- **Other request extensions** (27 body fields; only `messages` is required): `models: [...]` = ordered fallback list (`/fallback`); `reasoning_effort` (`/reasoning`); `template` + `variables` (server-side prompt templates); `session_id`; `cache`; `timeout`; `response_format`; `transforms`; `modality`/`modalities`/`image`/`audio`; `async_mode`. `POST /v1/web/search` backs the `web_search` tool — **flat $0.005 per successful search**, not token-priced (the CLI doesn't surface this), and a key `allowed_models` allow-list that omits the search model silently disables it.
- **Retry/fallback is server-side** (same provider → same model on another provider → different model), but **streaming responses are never retried** at either layer — the CLI is streaming-only, so it sees none of that resilience.
- **Unused surface** the CLI could reach: `/v1/messages` (Anthropic-compatible, and *not* restricted to Anthropic models), `/v1/responses` (reasoning effort, provider-hosted tools, background jobs), `/v1/chat/compare`, batch, RAG (`/v1/files`), server-side memory (`x-mem-id`), templates, `/v1/balance`, `/v1/usage/*`, and an MCP server.

## Reusable utilities

- `render.fmt_usd(value)` — port of `fmtUsd` from `../routersvc-client/src/lib/utils.ts`. **Always 6 decimals** with K/M abbreviations. Use this for every USD amount; never raw `f"{n:.2f}"`. Keeps CLI cost display identical to the dashboard.

## Slash commands

`/model` (fuzzy tab-completion from the catalog — `/model qw` pops every qwen model; `completer.SlashCompleter`, ThreadedCompleter so the lazy silent catalog fetch never blocks a keystroke) `/models` (also `free|thinking|tools` capability filters) `/route` (auto|smart|off|preview|weights|why) `/fallback` (also completes model ids) `/reasoning` `/file` `/image` `/clear-attach` `/system` `/mode` `/cost` (session spend + live account balance) `/hops` (n|off — per-turn hop checkpoint, 0/off = unlimited) `/compact` (context usage | now | auto on/off) `/stall` (pause|keep-going) `/optimize` `/style` (concise|default|explanatory|learning — interactive slider; rewrites the live system message so it applies mid-session; prose-only, never widens or narrows the actual work) `/login` `/update` `/clear` (also resets the plan) `/help` `/exit` (`/quit`, `/q`).

**Smart routing** (`router.py`, `/route smart`): local per-prompt model picking. `classify()` = deterministic signal regexes (image > long-context > code > extraction > math > classify > research > writing; `has_tools` tips code→agentic and the chat default→agentic — this CLI always sends tools, so `needs_tools=True` on every pick). `pick()` scores a cohort's candidate list from `src/meshapi/routing_table.json` (bundled data: opaque per-model×cohort scores + verified capability booleans — **compiled elsewhere; the methodology is not in this repo**) with normalized user weights over cost (live catalog prices, log-compressed)/capability/speed, top-3 stickiness via `incumbent`, and a strict **fail-open contract**: any miss returns None and the pinned model rides. CLI seam: `_smart_route_turn` (turn start, once per user turn) stashes `state["_smart_pick"]`; `_effective_cfg` swaps it in as `{model: pick, auto_route: False}`. `route_mode`/`route_weights` in config; `/route smart|weights|why`; header shows `smart → model`. Never bill a classifier call or add a network hop for routing — that's the whole point.

**Mid-run steering:** `cli.LIVE_CONTROL_COMMANDS` / `_drain_live_controls` — `/model`, `/reasoning`, `/route`, `/fallback`, `/mode`, `/hops`, `/stall`, `/optimize`, `/compact` typed *during* a turn are applied at the top of the next hop (cfg is re-read per hop, so the switch lands on the next request) instead of queuing as a new turn. `/clear` and `/exit` are deliberately NOT live-applicable. Ordinary text still queues as a full turn.

⚠️ **`reasoning_effort` is a minefield — the CLI is deliberately evidence-based about it.** Three verified facts: (1) sending it to a model that can't take it does NOT get ignored — the request is rejected outright (`gpt-4o-mini`: "Unrecognized request argument supplied"); (2) **streaming reports that as HTTP 200 + an in-band error chunk**, not a 400, so both shapes must be handled — missing the in-band one made the guard useless in a live test; (3) the catalog's `supports_thinking` is **False for gpt-5.4 / sonnet-4.6 / opus-4.8 / haiku-4.5**, which is wrong enough to be unusable — gpt-5.4 accepts the field on a plain call. So: never gate on the catalog flag (it would silently disable reasoning where it works). `cli._effective_cfg` sends the field until a provider actually rejects it, then `_maybe_drop_reasoning` (checked in BOTH the `HTTPStatusError` and in-band branches of `_stream_hop_with_retry`) strips it, retries, and records the model in `state["_reasoning_rejected"]` so later turns skip it without paying a retry. `commands.warn_reasoning_unsupported` only speaks about models observed rejecting it. **Agentic reality:** gpt-5.x rejects `reasoning_effort` *whenever function tools are present* ("use /v1/responses or set reasoning_effort to 'none'"), and this CLI always sends tools — so effort is largely unavailable on gpt-5.x here regardless. The gateway enum is `high|medium|low|none` ONLY; `xhigh`/`max`/`minimal` are 422 `literal_error`, so `_REASONING_ALIASES` maps them to the nearest real level.

**`ask_user`** (`tools.INTERACTIVE_TOOLS`, ungated like plan tools — the user answering IS the approval): `askui.ask` runs a prompt_toolkit Application under `watcher.paused()`; a tab strip for multi-question calls, arrow-key selection, a always-appended "Type something." free-text escape, and Esc to cancel. Every failure path (non-tty, cancel, broken TTY) returns a prose instruction telling the model to decide and move on — a model must never wedge on a UI that can't render.

## Distribution & release

- **Version lives in TWO places** — bump both: `pyproject.toml` `version` and `src/meshapi/__init__.py` `__version__`. Verify with `python -m meshapi --version`.
- **PyPI** (`meshapi-code`): `.github/workflows/publish.yml` builds + uploads on a **`v*` tag push** via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (no token). A plain push to `main` does NOT publish. Trusted Publisher = `aifiesta/meshapi-code` repo + `publish.yml`.
- **Release flow:** commit to `main` → (only on explicit ship-it) `git tag -a vX.Y.Z -m "…" && git push origin vX.Y.Z` → watch with `gh run watch <id> --exit-status` → confirm at `https://pypi.org/pypi/meshapi-code/<version>/json` (the `/json` "latest" field is CDN-cached and lags a few minutes; the version-specific endpoint is authoritative).
- ⚠️ **Never auto-publish.** Stop at "ready to test" and wait for an explicit ship-it before tagging/pushing a `v*` tag. PyPI uploads of a given version are immutable — you can't re-upload `0.4.3`.
- **Install paths users use:** `pipx install meshapi-code`, `uv tool install meshapi-code`, `pip install meshapi-code`. Upgrade: `pipx upgrade meshapi-code`.
- **npm port** (`meshapi-code`): planned. Node rewrite using `ink` + `chalk`, same UX.

## Gotchas / hard-won learnings

- **`pipx` vs editable shadowing:** an activated `.build-venv` (`pip install -e .`) prepends its `bin/` to `$PATH`, so `meshapi` runs the editable working-tree copy and shadows the pipx-installed one. `pipx upgrade` still updates the pipx copy; it just won't be what `meshapi` resolves to until that venv is off PATH. Editable installs report the working-tree version live.
- **Catalog fetches are 2.8 MB and block the prompt.** `/model <name>` validates against `GET /v1/models`, which can take seconds on first use in a session; the REPL looks frozen meanwhile. Anything that needs the catalog (`_known_model_ids`, `warn_reasoning_unsupported`) inherits that pause — cache it, fetch it once, never per hop.
- **`~/.meshapi/config.json` is shared state.** `/model`, `/reasoning`, `/mode` etc. persist immediately, so any live/pty test silently rewrites the user's real settings. Snapshot and restore around automated runs.
- **Two wire shapes for one rejection.** A request the provider won't accept comes back as HTTP 400 when non-streaming but **HTTP 200 + an in-band `{"error":…}` chunk when streaming**. Any guard that only handles one shape is dead code in practice — this exact gap shipped once and was caught by the first manual test.
- See **Verification** below for the pty recipe and the full test workflow.

## Verification (how to know a change is good)

**Run the working tree, not the installed copy.** The repo ships a venv with an editable install; activating it is the whole setup:

```bash
source .build-venv/bin/activate     # now `python`, `pytest`, `meshapi` all exist
meshapi --version                   # -> the working tree's version
pytest                              # -> 609 tests, no network
```

⚠️ There is **no bare `python`** on a stock macOS shell and Homebrew's `python3` does **not** have this project's deps — outside the venv every command fails confusingly. Inside it, plain `meshapi` *is* `src/` (editable), so there's no `PYTHONPATH` to remember. `deactivate` flips `meshapi` back to the published PyPI build, which makes a handy A/B baseline in a second terminal.

**Automated** — `tests/` (609, zero network: httpx MockTransport + monkeypatching; CI on Ubuntu/macOS × py3.10/3.13). Pure logic lives in modules that are unit-testable without the REPL (`loopguard`, `compact`, `pricing`, `askui`'s render, `safety`, `tools`) — keep it that way and add a test with every fix.

**Manual / pty** — the prompt_toolkit surface (picker, toolbar, type-ahead, ESC) can only be proven in a real terminal. Drive it with `pty.fork()`; the recipe and its traps:

- Set `TIOCSWINSZ` via `fcntl.ioctl` **and** answer the `\x1b[6n` CPR query with `\x1b[<row>;<col>R`, or prompt_toolkit renders nothing ("terminal doesn't support CPR"). shift+tab is `\x1b[Z`; bare ESC is `\x1b`.
- **Never match a wait-marker against the terminal's echo of what you just typed** — it fires instantly. Snapshot the buffer length before writing and search only what came after.
- **Wait for a command to actually finish before sending the next line.** If the CLI is mid-command (e.g. `/model` blocking on the 2.8 MB catalog) the keystrokes concatenate into one buffer and you get nonsense like `/model openai/gpt-5.4What is 17*23?` → "Unknown model". This looks exactly like a product bug and isn't.
- **Live tests mutate the real `~/.meshapi/config.json`** (`/model`, `/reasoning` persist). Snapshot and restore it, or you'll hand the user a session with your test settings — this actually happened and caused a false bug report.

**Scenarios worth re-running by hand after loop changes** (all verified on 0.5.9): unlimited hops, mid-run `/model`, ESC-then-continue, `/hops` checkpoint, compaction, cost line, reasoning fallback, the `ask_user` picker. Two traps when writing them:

- **Hop count ≠ step count.** Models batch parallel tool calls — 12 files often lands in 3 hops. To genuinely exercise many hops, force sequential work (`sleep 3 && echo ... > s$i.txt`, one call per step).
- **A fast turn finishes before you can type**, so mid-run and ESC tests need a deliberately slow turn or they silently test nothing.

## The 0.5.9 journey (why the loop looks like this)

Worth reading before changing `cli.py`'s turn loop — every guard below replaced something that failed in front of a user:

1. **The report**: long tasks "stop and give no response", or die with *"Stopped after 8 tool hops — model wasn't converging."* The cap was arbitrary and there was no convergence check behind the message.
2. **Four independent killers** were found, not one: the hop cap; unbounded context growth (the gateway eventually rejects the prompt); zero network retry (the gateway's own retry/fallback covers non-streaming only, and this CLI streams); and `_drop_in_flight_turn`, which discarded an *entire* turn — hours of billed work — on any exception.
3. **Cross-checked against Claude Code 2.1.236** (strings from the shipped binary). It agreed on the big calls (no default turn cap, interrupts keep history) and taught five things we lacked: `Retry-After` honoring, non-streaming fallback, `max_tokens`-overflow repair, **transcript-backed (recoverable) compaction**, and a compaction thrash guard.
4. **Then live testing corrected the design twice.** A guard that only handled HTTP 400 was useless because streaming reports rejections as *200 + in-band error*. And gating `reasoning_effort` on the catalog's `supports_thinking` would have silently disabled reasoning on gpt-5.4/sonnet-4.6/opus-4.8 — the flag is simply wrong. Both are now evidence-based.

The through-line: **prefer evidence over metadata, handle both wire shapes, and never throw away work the user already paid for.**

