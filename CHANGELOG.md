# Changelog

All notable changes to `meshapi-code`. Upgrade with `pipx upgrade meshapi-code`.

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
