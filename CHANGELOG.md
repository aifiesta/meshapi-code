# Changelog

All notable changes to `meshapi-code`. Upgrade with `pipx upgrade meshapi-code`.

## 0.5.9 — 2026-08-19 · "the immortal loop"
- **The model can ask YOU, mid-task.** New interactive picker: at a real fork (which stack, which design, how big a scope) the model presents 2-4 concrete options with descriptions and you choose with the arrow keys — Enter to select, Tab/←→ between questions, Esc to cancel, and a "Type something." free-text escape so you're never boxed in. Multiple related questions render as a tab strip that ticks off as you answer. Dismissing it never blocks: the model is told to pick sensibly and say why. Headless/non-tty runs skip it automatically.
- **Cost after every turn — the gateway's own number.** Chat completions carry no `cost` field (streaming or not), but Mesh bills every request and exposes the figure on the usage API keyed by the `x-request-id` it returns. The CLI now captures that id per hop and reconciles the turn against `POST /v1/usage/events` — so the line shows the **authoritative** `$0.000195`, available immediately (no billing lag, streaming included). If the lookup can't run (offline, endpoint down), it falls back to a computed estimate from token usage × the catalog's per-1M rates — fresh vs cached prompt tokens at their separate rates, `discount_pct`, and under `/route auto` the router's classifier tokens, which are billed and are *not* small — shown with a `~` to mark it as estimated. `/cost` also reports your live account balance.
- **Steer mid-run.** `/model`, `/reasoning`, `/route`, `/mode`, `/fallback`, `/hops`, `/stall`, `/optimize` and `/compact` typed *while a turn is running* now take effect on the very next hop instead of queuing behind it — switch to a stronger model the moment you see a long run going sideways, without losing the work. Ordinary messages still queue as full turns.
- **`/reasoning` can no longer break a request — and no longer lies about which models support thinking.** Sending `reasoning_effort` to a model that won't accept it makes the provider reject the whole call, which used to fail every turn until you noticed. The CLI now sends it, and if a provider actually rejects it, drops the field, retries, and remembers that model for the session — your setting is preserved and still applies everywhere it works. Deliberately *not* driven by the catalog's `supports_thinking` flag, which reports `False` for gpt-5.4, sonnet-4.6, opus-4.8 and haiku-4.5 even though they accept the field: trusting it would have silently disabled reasoning on exactly the models you'd want it for. Worth knowing: gpt-5.x rejects `reasoning_effort` whenever tools are in the request ("use /v1/responses or set reasoning_effort to 'none'"), and an agentic CLI always sends tools — so on those models you'll see one "retrying without it" line and a normal answer. The gateway accepts `high|medium|low|none` only, so `xhigh`/`max`/`minimal` map to the nearest real level instead of erroring.
- **No more "Stopped after 8 tool hops."** The arbitrary hop cap is gone — long agentic tasks run to completion, whether they take a minute or a day. What replaces it is real checks instead of a blind wall:
  - **Stall detection**: if the model repeats the exact same action (including A/B alternation), it gets a corrective nudge at 3 repeats, a stronger one at 6, and at 9 the turn pauses at the prompt with all work saved — say "continue" to resume, or `/stall keep-going` to never pause (unattended runs). Malformed-tool-call loops hit the same wall after 8 straight broken rounds.
  - **`/hops <n>`** (optional): pause each turn after n hops as a checkpoint. Default is unlimited.
- **Auto-compaction — long turns no longer die on "prompt is too long."** When history nears the model's context limit (~70%), old tool outputs are trimmed and completed work is folded into compact summaries — deterministic, no extra spend, files on disk unaffected. If the gateway still rejects on context, the CLI compacts aggressively and retries once instead of ending the turn. `/compact` shows context usage; `/compact now` compacts on demand; `/compact auto off` disables.
- **Network resilience per hop.** A dropped connection, timeout, 429, or 5xx mid-task now retries with exponential backoff (visible "retrying in Ns" line, esc aborts) instead of killing the turn — up to 5 attempts. Transient gateway errors and empty responses retry too. (The gateway's own retry/fallback only covers non-streaming requests; the CLI streams everything.)
- **Interruptions no longer throw away completed work.** Esc/ctrl+c, an API error, or a network failure used to discard the ENTIRE turn from the conversation — hours of context gone. Now completed tool actions stay in history (a half-finished batch is sealed safely), a breadcrumb marks where things stopped, and "continue" resumes without redoing work. Session spend is now tracked even when a turn aborts.
- **Long-task quality of life**: `run_bash` accepts an optional `timeout` (up to 600s) for slow builds; huge `read_file` results are capped at 80k chars with guidance to read ranges; a heartbeat line every 10 hops shows hops / context size / elapsed / spend; the streaming header shows running turn cost.
- Fixed: `/clear` now also clears the plan (a stale plan used to keep injecting steps from the wiped conversation, and silently granted extended hops forever).
- Also ships: self-upgrade prints the one-line reinstall command when uv/pipx can't be found at all (never dead-ends on "command not found").
- **Resilience patterns adopted from Claude Code** (researched against the shipping 2.1.236 binary):
  - **`Retry-After` is honored** — when the gateway states its own wait, that wins over our exponential guess; an absurd wait (>90s) surfaces as an error instead of freezing the CLI.
  - **Non-streaming fallback** — after streaming attempts are exhausted, one blocking request is tried. This matters on Mesh specifically: the gateway's server-side retry *and provider fallback* apply to non-streaming requests only, so a streaming-only client got none of it.
  - **`max_tokens` overflow is repaired, not fatal** — a `input length and max_tokens exceed context limit` 400 now shrinks the output budget and retries with **all history intact**, instead of compacting or failing.
  - **Compaction is recoverable, not destructive** — before folding, the full history is written to `~/.meshapi/transcripts/<session>.jsonl` (0600) and the summary left in context names that path, so the model can read back exact code or error text it no longer holds.
  - **Compaction thrash guard** — if a compaction can't get under the limit, the CLI stops retrying it every hop.
- +92 tests (560 total): stall detector, retry/backoff policy, Retry-After parsing (seconds + HTTP-date + refusal), max_tokens overflow math, non-streaming fallback, compaction validity (no orphaned tool calls, dedupe-contract coherence, transcript pointer), interrupted-turn sealing, read cap, bash timeout.

**Verified before release** — 609 automated tests (no network), plus a full manual pass in a real terminal: an 11-hop sequential build with no cap, mid-run `/model` and `/mode` switching, ESC-then-`continue` preserving completed work, the `/hops` checkpoint, compaction and context reporting, per-turn cost reconciled against the gateway's usage API, the arrow-key picker driven end to end, a 130s shell command surviving the old 120s ceiling, and an 80k read cap. Two design corrections came out of that testing and are described above.

## 0.5.8 — 2026-08-12
- **Fixed self-upgrade `[Errno 2] No such file or directory: 'uv'`.** A uv-installed meshapi can run with a `PATH` that omits uv's bin directory, so `meshapi upgrade` (and the in-app "upgrade now?" prompt) couldn't find `uv` even though it works fine in your shell. The upgrade now resolves `uv`/`pipx` to their absolute path (searching the usual install locations) before running it.

## 0.5.7 — 2026-08-11 · "reliability & hardening"
- **Fixed the long-session "hang."** After a transient empty response the CLI used to append an empty assistant message, which the backend then rejected on *every* later turn (HTTP 200 + an in-band error that was silently dropped) — the session looked frozen with `?→? tok`. Empty assistant messages are now stripped before sending, in-band errors are surfaced instead of dropped, and an empty/errored turn ends cleanly with a clear message instead of poisoning the conversation.
- **Live progress during tool calls.** A long-running `run_bash` (or web search) now shows a spinner + elapsed timer (`⠹ running · 12s`) plus the same mode / type-ahead / queued-message footer that streaming shows — no more staring at a static line wondering if it's stuck.
- **Security & robustness hardening** (from a full pre-release audit): a corrupt `config.json` degrades to defaults instead of bricking launch (and saves are now atomic); the destructive-command guard is case-insensitive and now catches `rm --recursive --force`, `find -delete`, `git clean -f`, `shred`, and redirects to protected paths like `>> ~/.ssh/authorized_keys`; `/image` URL fetches re-validate every redirect hop (SSRF); `base_url` accepts only genuinely-local `http://` hosts.
- `/model auto` now enables auto-routing instead of erroring "Unknown model: auto"; `/optimize`'s token cap no longer clips long answers; installer failure paths report the real cause; installer PATH-shadow detection is more robust.
- **First automated test suite** — a 463-test `pytest` suite (`tests/`, `python -m pytest`) with CI on Ubuntu/macOS × Python 3.10/3.13 and a regression test for every fix above.

## 0.5.6 — 2026-08-06 · "one-line install"
- **One-command install, any OS, nothing preinstalled.** New `install.sh` (macOS/Linux) and `install.ps1` (Windows) bootstrap [uv](https://astral.sh), which brings its own Python — no manual Python or pipx. `curl -fsSL https://cli.meshapi.ai/install.sh | sh` installs uv (if missing), `uv tool install`s meshapi, fixes PATH, and drops you into the first-run key prompt. Idempotent: re-running upgrades an existing install; a returning user with a key set skips straight to the REPL.
- **`meshapi upgrade`** — upgrade from the shell without entering the chat. Reuses the same pipx/uv/pip resolution as the in-REPL `/update` (and the same "exit first" guidance on Windows, where the `.exe` is file-locked).
- **`/model` no longer misleads under auto-routing.** Setting a model (or running `/model` with no argument) while `/route auto` is on now warns that the pin is *inactive* until `/route off` — the gateway still picks per prompt, so a pinned model was silently ignored before. Display-only; routing behavior unchanged.
- Docs lead with the one-liner; the manual pipx/uv/pip walkthrough stays as a fallback for air-gapped/locked-down environments.

## 0.5.5 — 2026-07-06
- `/model <invalid>` is now rejected before persisting (was silently saved to config, breaking every future launch) — unknown ids get top-3 fuzzy "did you mean" suggestions; offline still sets with a warning.
- `/fallback <invalid>` now rejected when the catalog is reachable (was warn-but-keep — a bogus fallback breaks failover exactly when needed); offline keeps with warning.
- `--route preview` accepted at launch for parity with /route (explains that preview needs a conversation).

## 0.5.4 — 2026-07-06
- **Fixed cross-platform crash**: `/file` with no argument (or a directory / binary / >2MB file) killed the whole CLI — PermissionError on Windows, IsADirectoryError on Linux/macOS (external user report). Now prints a friendly message.
- **Never again**: all slash commands now run inside exception isolation — a command bug can print an error but can no longer exit the session.

## 0.5.3 — 2026-07-06
- Docs: model count corrected to 1000+ (manually verified against the live catalog).

## 0.5.2 — 2026-07-06 · "repo memory"

- **The agent remembers your repo.** Every file it writes or reads is
  structurally captured (symbols, sizes — zero extra tokens, the content is
  already in hand) into `~/.meshapi/context/` — never inside your repo. The
  next session in the same directory starts warm: a token-capped repo map +
  notes ride the system prompt, so the model knows the project without
  re-reading everything.
- **`remember` tool**: the model persists durable decisions ("uses pnpm",
  "tests run with pytest -q") across sessions. `/memory` inspects,
  `/memory notes` prints them, `/memory clear` deletes this repo's store,
  `/memory off` disables the feature.
- **Read-dedupe**: re-reading an unchanged file returns a short "already in
  your context" pointer instead of the full body — provably safe (sha256
  re-check against disk, correct against the /optimize pruning lever at any
  dial, anti-loop: an immediate re-ask returns the body).
- web_search results now include the actual result text (prod sends
  `content`, not `snippet` — verified live; was silently dropped).
- Verified live in prod: `/route preview` (`/v1/router/select`) and the
  `web_search` tool (`/v1/web/search`) both work against the gateway.

## 0.5.1 — 2026-07-06 · "the agentic release"

**Getting started**
- First-run key setup: hidden input, live verification against the gateway, saved to `~/.meshapi/credentials` (0600). `/login` replaces it. Keys hand-edited into `config.json` are auto-migrated (they used to be silently wiped on the next settings save).
- Built-in update checker: background PyPI check + one-key upgrade offer; `/update` on demand; a declined version never re-nags. Windows prints the upgrade command instead of running it (the live `.exe` is file-locked).

**Models & routing**
- Fuzzy model completion: `/model qw` pops a live menu of every qwen model; `gpt4m` matches `openai/gpt-4o-mini`. Command names and `/route`/`/mode`/`/reasoning`/`/fallback` args complete too.
- `/models [free|query]` catalog browser: context window, capabilities, $/1M pricing.
- Real auto-routing: `/route auto` (gateway picks per prompt; resolved model shown in the status line), `/route preview`. The old `route: cheapest|fastest|balanced` never existed gateway-side and was removed.
- `/fallback m1 m2` ordered failover list; `/reasoning high|medium|low|none`.
- `web_search` agent tool (permission-gated).

**Agentic reliability**
- Self-healing tool calls: malformed streamed arguments (missing commas, raw control chars, fragments under the wrong stream index) are repaired client-side; sanitized history means the model never re-reads its own broken JSON (this ended a live doom-loop of 6 identical failures). Raw failures logged to `~/.meshapi/toolcall_failures.jsonl` with SSE dropped-chunk counters for gateway-vs-model attribution.
- Quality guard: placeholder code (`// Add game logic here`, comment-context `TODO`s, "rest of the code remains the same" elisions) triggers one automatic fix-it pass with per-file evidence, then an honest warning + stronger-model suggestion if stubs survive. Suppressed when scaffolding is requested explicitly.
- `start_server` intelligence: detects the port inside your command, adopts whatever port the server actually binds (via process-group inspection), progress ticker while waiting, "that's YOUR server, don't restart it" guidance, no orphaned processes on ctrl+c, exit-0 daemonizer grace.
- Plan bookkeeping allowed after server start (the "END THE TURN" instruction no longer strands plan steps).

**Terminal experience**
- Always-visible input: type while the model streams — the footer shows your text live; Enter stacks messages that auto-run in order; unfinished text prefills the next prompt; ctrl+c discards the stack. (macOS/Linux; Windows: between turns.)
- ESC aborts a running turn (between deltas/hops/tool calls).
- Permission mode always visible, shift+tab applies mid-run; `a` at any approval allows that tool for the session (still safety-checked at AUTO strictness).
- Framed input with `repo · git-branch` title; streaming header `✦ model · hop N`; phase-aware spinner (`preparing write_file (↓ 3.2k chars)`); live ~token estimates; background servers listed under the mode line; long streams tail-scroll instead of freezing behind an ellipsis.
- Cost segments hidden on turns where the gateway returned no cost (no dangling "—").

## 0.4.6 — 2026-07-05
- Tool calls with empty/malformed arguments are skipped with precise feedback to the model instead of prompting the user to approve a doomed call.

## 0.4.5 — 2026-07-05
- Fixed fatal Windows startup crash (`signal.SIGHUP` is POSIX-only) and two related Windows-only process-kill crashes.

## 0.4.4 — 2026-05-29
- Mesh Optimize dial (beta): `/optimize 0–0.95` — prompt-cache breakpoints, max_tokens defaults, consumed-tool-result pruning.
- Cached-token reads from OpenAI-style `prompt_tokens_details`.

## 0.4.3
- Live permission-mode toolbar (bottom bar repaints on shift+tab).
- Safety guardrails: sensitive-path denylist, cwd scoping, destructive-command shapes, SSRF guard for URL fetches.
- Drag-dropped image paths with spaces auto-attach correctly.

## 0.4.1 – 0.4.2
- Image input: base64 attachments via `/image` + auto-detection of image paths/URLs in prompts; `read_file` guards against binary image files.

## 0.4.0
- Plan tools (`create_plan`/`update_step`) with visible progress; `start_server` for background dev servers; visibility overhaul.

## 0.3.0 – 0.3.4
- Tool calling (read/write/bash) with ask/bypass permission modes; cwd-aware system prompt; security hardening (0600 config, https-only, scrubbed history, resolved-path approvals); relicensed Apache 2.0.

## 0.2.x
- Brand theme, MESH logo banner, spinner, per-turn stats.

## 0.1.0
- Initial release: streaming chat REPL with live markdown and per-turn cost.
