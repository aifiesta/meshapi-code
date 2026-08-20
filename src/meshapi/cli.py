"""meshapi — terminal chat REPL for Mesh API."""
import argparse
import collections
import contextlib
import difflib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import ThreadedCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.markdown import Markdown
from rich.markup import escape as _rich_escape
from rich.text import Text

from . import __version__, askui, compact, loopguard, memory, pricing, router, statusbar
from .attachments import AttachmentError, find_image_tokens, load_image
from .client import complete_chat, stream_chat
from .commands import fetch_models_quiet, handle_command, prompt_for_api_key
from .config import (
    CREDENTIALS_FILE, HISTORY_FILE, clear_servers_file, load_config,
    load_servers, load_update_check, log_toolcall_failure, save_servers,
    secure_file,
)
from .keywatcher import KeyWatcher
from .permissions import AUTO_APPROVE, Mode, from_str, next_mode
from .plan import Plan
from . import safety
from .render import (
    BRAND, BRAND_BG, BRAND_BG_FG, BRAND_DIM, CODE, console, fmt_usd, pretty_cwd, render_stream,
    run_with_ticker,
)
from .tools import (
    INTERACTIVE_TOOLS, PLAN_TOOLS, TOOLS, build_system_prompt, execute as exec_tool,
    find_stub_markers, parse_error_context, repair_tool_args, schema_hint,
    stub_guard_suppressed, summarize_call, validate_call,
)
from .update import maybe_offer, run_upgrade, start_background_check

# ANSI Shadow figlet font
MESH_LOGO_LINES = [
    "███╗   ███╗███████╗███████╗██╗  ██╗",
    "████╗ ████║██╔════╝██╔════╝██║  ██║",
    "██╔████╔██║█████╗  ███████╗███████║",
    "██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║",
    "██║ ╚═╝ ██║███████╗███████║██║  ██║",
    "╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝",
]
LOGO_WIDTH = 35  # chars per line
LOGO_GUTTER = 3  # spaces between logo and info column

# Mesh data-plane keys are `rsk_` followed by an opaque token. Prevent these
# strings from being persisted to the prompt-toolkit history file in case a
# user pastes one at the prompt by accident.
_API_KEY_RE = re.compile(r"\brsk_[A-Za-z0-9_-]{8,}\b")


class ScrubbedFileHistory(FileHistory):
    """FileHistory that drops entries containing API-key-shaped strings
    and tightens file perms to 0600 after every write."""

    def store_string(self, string: str) -> None:
        if _API_KEY_RE.search(string):
            return
        super().store_string(string)
        secure_file(Path(self.filename))


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="meshapi", description="Terminal chat for Mesh API")
    p.add_argument("--version", action="version", version=f"meshapi {__version__}")
    p.add_argument("--model", help="Override model for this session (e.g. openai/gpt-4o-mini)")
    p.add_argument(
        "--route", choices=["auto", "off", "preview"],
        help="Auto-routing: 'auto' lets the gateway pick a model per prompt; "
             "'preview' is meaningful in-session (/route preview) once a "
             "conversation exists",
    )
    p.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default="default",
        help="Tool permission mode (default: ask each tool). Cycle in-session with shift+tab.",
    )
    # Subcommands are optional — a bare `meshapi` (with or without the flags
    # above) still launches the REPL (command == None). `meshapi upgrade`
    # upgrades in place without entering the REPL.
    sub = p.add_subparsers(dest="command")
    sub.add_parser(
        "upgrade",
        help="Upgrade meshapi to the latest release (pipx/uv/pip, matching how "
             "it was installed) without entering the chat.",
    )
    return p.parse_args(argv)


def render_banner(cfg: dict) -> None:
    info_per_line: list = [
        None,
        None,
        Text.from_markup(f"[bold {BRAND}]✦  meshapi {__version__}[/bold {BRAND}]"),
        Text.from_markup(f"cwd:   [{BRAND}]{pretty_cwd()}[/{BRAND}]"),
        Text.from_markup(f"model: [bold {BRAND}]{cfg['model']}[/bold {BRAND}]"),
        Text.from_markup(
            f"route: [{BRAND}]"
            f"{'smart' if cfg.get('route_mode') == 'smart' else ('auto' if cfg.get('auto_route') else 'off')}"
            f"[/{BRAND}]"),
    ]
    console.print()
    for i, logo_line in enumerate(MESH_LOGO_LINES):
        line = Text()
        line.append(logo_line, style=BRAND)
        info = info_per_line[i] if i < len(info_per_line) else None
        if info is not None:
            pad = max(0, LOGO_WIDTH - len(logo_line))
            line.append(" " * (pad + LOGO_GUTTER))
            line.append(info)
        console.print(line)
    console.print()
    console.print("type /help for commands, /exit to quit", style=BRAND_DIM)
    console.print()


def _resolved_path_line(raw: str) -> str:
    """Render `→ /abs/path` and flag if the path escapes the launch cwd."""
    try:
        resolved = Path(raw).expanduser().resolve()
    except Exception:
        return f"[dim]→ {raw}[/dim]"
    cwd = Path.cwd().resolve()
    try:
        outside = not resolved.is_relative_to(cwd)
    except AttributeError:  # is_relative_to is 3.9+, but pyproject pins 3.10+
        outside = not str(resolved).startswith(str(cwd))
    if outside:
        return f"[dim]→[/dim] [bold yellow]{resolved}[/bold yellow]  [bold yellow](outside cwd)[/bold yellow]"
    return f"[dim]→ {resolved}[/dim]"


# How much of a write_file body or bash output to show inline. Long enough to
# eyeball the model's work; short enough to keep scrollback usable.
_MAX_BODY_LINES = 24
_MAX_LINE_LEN = 240


def _print_code_body(content: str) -> None:
    """Render file content as left-bar quoted lines in CODE color (no diff)."""
    if not isinstance(content, str) or not content:
        return
    lines = content.split("\n")
    for line in lines[:_MAX_BODY_LINES]:
        if len(line) > _MAX_LINE_LEN:
            line = line[:_MAX_LINE_LEN] + "…"
        console.print(f"  [{CODE}]│[/{CODE}] [{CODE}]{_rich_escape(line)}[/{CODE}]")
    if len(lines) > _MAX_BODY_LINES:
        more = len(lines) - _MAX_BODY_LINES
        console.print(f"  [{CODE}]│[/{CODE}] [dim]… {more} more line{'s' if more != 1 else ''}[/dim]")


def _print_added_lines(content: str) -> None:
    """Render every line of a new file with line numbers and a green + marker."""
    if not content:
        console.print("  [dim](empty file)[/dim]")
        return
    lines = content.split("\n")
    for i, line in enumerate(lines[:_MAX_BODY_LINES], 1):
        if len(line) > _MAX_LINE_LEN:
            line = line[:_MAX_LINE_LEN] + "…"
        console.print(f"  [dim]{i:>4}[/dim] [green]+ {_rich_escape(line)}[/green]")
    if len(lines) > _MAX_BODY_LINES:
        more = len(lines) - _MAX_BODY_LINES
        console.print(f"  [dim]    [/dim] [dim]… {more} more line{'s' if more != 1 else ''}[/dim]")


def _print_unified_diff(old: str, new: str) -> None:
    """Render a unified diff with line numbers, ± markers, and 3-line context.

    Hunk headers are dimmed; added lines are green with +, removed are red
    with -, context is dimmed with no marker. Line numbers are absolute
    against the new file for + and context lines, against the old file for -
    lines (matches what git/Claude Code show).
    """
    raw_lines = list(difflib.unified_diff(
        old.split("\n"),
        new.split("\n"),
        lineterm="",
        n=3,
    ))
    if not raw_lines:
        console.print("  [dim](no changes)[/dim]")
        return

    old_ln = new_ln = 0
    shown = 0
    cap = _MAX_BODY_LINES * 3  # diffs naturally have more lines than raw content
    for raw in raw_lines:
        if raw.startswith("---") or raw.startswith("+++"):
            continue  # difflib's file headers — uninformative for us
        if raw.startswith("@@"):
            # Parse '@@ -A,B +C,D @@'  →  old_ln = A, new_ln = C
            try:
                parts = raw.split(" ")
                old_ln = int(parts[1].split(",")[0].lstrip("-"))
                new_ln = int(parts[2].split(",")[0].lstrip("+"))
            except (IndexError, ValueError):
                pass
            console.print(f"  [dim]{_rich_escape(raw)}[/dim]")
            continue
        if shown >= cap:
            console.print("  [dim]    … (diff truncated)[/dim]")
            break
        shown += 1
        text = raw[1:]
        if len(text) > _MAX_LINE_LEN:
            text = text[:_MAX_LINE_LEN] + "…"
        if raw.startswith("+"):
            console.print(f"  [dim]{new_ln:>4}[/dim] [green]+ {_rich_escape(text)}[/green]")
            new_ln += 1
        elif raw.startswith("-"):
            console.print(f"  [dim]{old_ln:>4}[/dim] [red]- {_rich_escape(text)}[/red]")
            old_ln += 1
        else:
            console.print(f"  [dim]{new_ln:>4}[/dim] [dim]  {_rich_escape(text)}[/dim]")
            old_ln += 1
            new_ln += 1


def _print_file_diff(path: str, new_content: str) -> None:
    """Show write_file as a git-style diff: new files render fully as +added,
    existing files render a unified diff against the current on-disk content."""
    if not isinstance(path, str) or not path:
        _print_code_body(new_content)
        return
    try:
        p = Path(path).expanduser()
    except Exception:
        _print_code_body(new_content)
        return
    if not p.exists():
        # Brand-new file — every line is added.
        added = (new_content or "").count("\n") + (1 if new_content else 0)
        console.print(f"  [dim]new file • {added} line{'s' if added != 1 else ''}[/dim]")
        _print_added_lines(new_content or "")
        return
    try:
        old = p.read_text()
    except Exception:
        # Binary or unreadable — fall back to plain content view.
        _print_code_body(new_content)
        return
    if old == (new_content or ""):
        console.print("  [dim](no changes)[/dim]")
        return
    # Summarize the diff up front so the user has a count even if it's huge.
    diff_added = sum(1 for ln in difflib.ndiff(old.split("\n"), (new_content or "").split("\n")) if ln.startswith("+ "))
    diff_removed = sum(1 for ln in difflib.ndiff(old.split("\n"), (new_content or "").split("\n")) if ln.startswith("- "))
    console.print(f"  [dim]+{diff_added} −{diff_removed}[/dim]")
    _print_unified_diff(old, new_content or "")


def _print_shell_command(cmd: str) -> None:
    """Render the shell command in CODE color with a $ prefix."""
    if not isinstance(cmd, str) or not cmd:
        return
    # Wrap-friendly: if the command is very long, truncate. Most commands fit.
    display = cmd if len(cmd) <= _MAX_LINE_LEN * 2 else cmd[: _MAX_LINE_LEN * 2] + "…"
    console.print(f"  [{CODE}]$[/{CODE}] [{CODE}]{_rich_escape(display)}[/{CODE}]")


def _print_shell_output(body: str) -> None:
    """Render captured stdout/stderr lines dimly (it's tool output, not chat)."""
    if not body or not body.strip():
        return
    lines = body.rstrip("\n").split("\n")
    for line in lines[:_MAX_BODY_LINES]:
        if len(line) > _MAX_LINE_LEN:
            line = line[:_MAX_LINE_LEN] + "…"
        console.print(f"    [dim]{_rich_escape(line)}[/dim]")
    if len(lines) > _MAX_BODY_LINES:
        more = len(lines) - _MAX_BODY_LINES
        console.print(f"    [dim]… {more} more line{'s' if more != 1 else ''}[/dim]")


def _render_tool_result(name: str, args: dict, result: str) -> None:
    """Render the outcome line(s) for a non-plan tool's execution."""
    is_error = result.startswith("Error:")
    if is_error:
        console.print(f"  [red]✗ {_rich_escape(result)}[/red]")
        return

    if name == "run_bash":
        # tools.run_bash returns "<output>\n[exit N]" — split the exit code off.
        body, exit_code = result, None
        marker = result.rfind("\n[exit ")
        if marker >= 0:
            body = result[:marker]
            try:
                exit_code = int(result[marker + 7 :].rstrip("]").strip())
            except ValueError:
                exit_code = None
        _print_shell_output(body)
        if exit_code is None:
            console.print(f"  [green]✓[/green] [dim]done[/dim]")
        elif exit_code == 0:
            console.print(f"  [green]✓ exit 0[/green]")
        else:
            console.print(f"  [red]✗ exit {exit_code}[/red]")
        return

    if name == "write_file":
        console.print(f"  [green]✓[/green] [dim]{_rich_escape(result)}[/dim]")
        return

    if name == "read_file":
        nchars = len(result)
        nlines = result.count("\n") + (1 if result and not result.endswith("\n") else 0)
        console.print(f"  [green]→[/green] [dim]read {nchars} chars ({nlines} line{'s' if nlines != 1 else ''})[/dim]")
        return

    if name == "web_search":
        console.print(f"  [green]→[/green] [dim]web results ({len(result)} chars)[/dim]")
        return

    # Unknown tool — show a one-line preview.
    preview = result[:200].replace("\n", " ")
    tail = "…" if len(result) > 200 else ""
    console.print(f"  [dim]→ {_rich_escape(preview)}{tail}[/dim]")


def _finalize_interrupted_turn(state: dict, reason: str = "abort") -> int:
    """Keep completed work in history after an interrupt or error.

    An hours-long turn used to be rolled back WHOLE on any exception — every
    completed hop's tokens wasted. Now: seal a half-answered trailing tool
    batch with stub results (strict Anthropic-translating gateways 400 on a
    tool_use id with no tool_result, and popping the assistant message would
    erase actions whose side effects already happened), then keep everything
    with a resume breadcrumb. Only when NOTHING happened (no tool results, no
    assistant text since the user message) does the old clean-slate rollback
    run — an unanswered user message would otherwise read as ignored.
    Returns the number of completed tool actions kept (0 = rolled back).
    """
    msgs = state["messages"]
    n = loopguard.completed_actions_since_user(msgs)
    has_assistant = False
    for m in reversed(msgs):
        if m.get("role") == "user":
            break
        if m.get("role") == "assistant":
            has_assistant = True
            break
    if n == 0 and not has_assistant:
        # Nothing was accomplished — clean slate, exactly the old behavior.
        while msgs and msgs[-1]["role"] != "user":
            msgs.pop()
        if msgs and msgs[-1]["role"] == "user":
            msgs.pop()
        memory.invalidate_dropped(state)
        return 0
    loopguard.seal_partial_batch(msgs)
    cause = (
        "The user interrupted this turn" if reason == "abort"
        else "This turn was cut short by a connection or gateway error"
    )
    msgs.append({"role": "system", "content": (
        f"[{cause} after {n} completed tool action(s). The results above are "
        "real — files written and commands run did happen. Wait for the "
        "user's next instruction; when asked to continue, resume from where "
        "you left off — do not redo completed work.]"
    )})
    memory.invalidate_dropped(state)
    return n


def _pause_breadcrumb(state: dict, hopped: int) -> None:
    """Record a pause in history so a "continue" turn resumes correctly.

    Appended on every deliberate pause (hop limit, stall stop) — without it
    the model reconstructs (or hallucinates) progress from buried tool
    history and tends to redo work or falsely claim completion.
    """
    _plan = state.get("plan")
    plan_part = ""
    if _plan is not None and not _plan.is_complete():
        plan_part = (
            f" The plan is incomplete {_plan.summary()}. "
            f"Remaining steps:\n{_plan.reminder_text()}\n"
        )
    state["messages"].append({"role": "system", "content": (
        f"[Execution was paused after {hopped} tool hops.{plan_part} "
        "Progress so far is recorded above. When the user asks to continue, "
        "resume from where you left off — do not redo completed work and do "
        "not claim the task is finished until it is.]"
    )})


# Stall nudges: short, prescriptive, tool-name-free (the XML-mode trap applies
# to injected messages too). Injected transiently — consume-once, LAST in
# _extras, same mechanism as the quality fix-it message.
_STALL_NUDGE = (
    "[You have repeated the same action with identical inputs several times; "
    "it is not making progress. Do something different this time: change the "
    "input, try another method, or re-read the last result carefully. If you "
    "are genuinely blocked, stop and tell the user exactly what is blocking "
    "you.]"
)
_STALL_RENUDGE = (
    "[You are still repeating the same action with identical inputs. This is "
    "the second reminder: change your approach now — different input, "
    "different method, or explain to the user exactly what is blocking you. "
    "If the next attempts are identical again, execution will pause.]"
)


def _retry_wait(state: dict, attempt: int, reason: str,
                delay: "float | None" = None) -> None:
    """Visible backoff between stream retries; ESC aborts the wait.

    `delay` overrides the exponential guess — used for a server-sent
    Retry-After, which knows the real capacity window better than we do.
    """
    if delay is None:
        delay = loopguard.backoff_delay(attempt)
    console.print(
        f"[yellow]⚠ {reason} — retrying in {delay:.1f}s "
        f"(attempt {attempt + 1}/{loopguard.MAX_STREAM_ATTEMPTS})[/yellow]"
    )
    end = time.monotonic() + delay
    while time.monotonic() < end:
        if state["esc_interrupt"].is_set():
            raise KeyboardInterrupt
        time.sleep(0.2)


# Slash commands that are safe and useful to apply MID-RUN, between hops,
# instead of queuing as a whole new turn. These only mutate session config
# that the next hop re-reads (model, routing, effort, budget, permissions),
# so applying them immediately is the behavior the user expects when they
# type "/model X" while watching a long run go sideways.
LIVE_CONTROL_COMMANDS = (
    "/model", "/reasoning", "/route", "/fallback", "/mode", "/hops",
    "/stall", "/optimize", "/compact", "/effort",
)


def is_live_control(text: str) -> bool:
    """True if `text` is a slash command applicable mid-run.

    Abbreviations resolve the same way handle_command resolves them, so a
    mid-run "/eff max" steers immediately instead of queuing as a turn.
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return False
    name = t.split()[0].lower()
    if name in LIVE_CONTROL_COMMANDS:
        return True
    from .commands import resolve_command
    resolved, _ = resolve_command(name)
    return resolved in LIVE_CONTROL_COMMANDS


def _drain_live_controls(state: dict) -> bool:
    """Apply any queued mid-run control commands. True if something changed.

    Called at the top of each hop: the model/effort/mode for the NEXT hop is
    read fresh from cfg, so a switch takes effect on the very next request
    without ending the turn. Non-control messages stay queued as full turns.
    """
    queue = state.get("input_queue")
    if not queue:
        return False
    remaining = collections.deque()
    applied = False
    while queue:
        item = queue.popleft()
        if is_live_control(item):
            console.print(f"[{BRAND_DIM}]⚙ applying mid-run: {item.strip()}[/{BRAND_DIM}]")
            try:
                handle_command(item.strip(), state)
            except Exception as e:  # never let a command kill the turn
                console.print(f"[red]  command failed: {e}[/red]")
            applied = True
        else:
            remaining.append(item)
    queue.extend(remaining)
    return applied


def _handle_ask_user(args: dict, state: dict) -> str:
    """Run the interactive picker and return the user's choices as a result.

    Ungated (no y/n): the "approval" IS the user answering. The watcher is
    paused so prompt_toolkit owns termios cleanly, exactly like the main
    prompt. Cancelling or a headless terminal returns a plain instruction to
    ask in prose instead — the model must never be stuck waiting on a UI
    that cannot render.
    """
    questions = args.get("questions") or []
    watcher = state.get("watcher")
    ctx = watcher.paused() if watcher is not None else contextlib.nullcontext()
    try:
        with ctx:
            answers = askui.ask(questions)
    except Exception as e:  # a broken TTY must not kill the turn
        return (f"Error: couldn't show the interactive picker ({e}). Ask the "
                "user your question in plain text instead.")
    if answers is None:
        console.print(f"[{BRAND_DIM}]✕ question picker dismissed — the model "
                      f"will decide and continue[/{BRAND_DIM}]")
        return ("The user dismissed the question picker without answering "
                "(or the terminal can't display it). Do NOT call ask_user "
                "again for this — ask in plain text, or make a reasonable "
                "choice yourself and say which you made and why.")
    # The picker erased itself on exit — print the transcript-worthy record
    # in its place: what was asked, what was chosen.
    n = len(answers)
    console.print(
        f"[green]✔[/green] [bold]answered {n} question{'s' if n != 1 else ''}[/bold]"
    )
    lines = []
    for q, a in zip(questions, answers):
        shown = ", ".join(a) if isinstance(a, list) else str(a)
        lines.append(f"Q: {q.get('question')}\nA: {shown}")
        label = q.get("header") or (q.get("question") or "answer")[:30]
        console.print(
            f"    [{BRAND_DIM}]{_rich_escape(label)}[/{BRAND_DIM}] → "
            f"[bold]{_rich_escape(shown)}[/bold]"
        )
    return ("The user answered:\n" + "\n".join(lines)
            + "\n\nProceed on these answers without re-asking.")


def _maybe_drop_reasoning(state: dict, message: str) -> bool:
    """True if `message` is the upstream rejecting reasoning_effort.

    Sets the per-turn latch so the retry omits the field. Must be checked on
    BOTH failure shapes: a non-streaming call surfaces this as HTTP 400, but
    a STREAMING call returns HTTP 200 and an in-band {"error": …} chunk —
    the in-band shape is what real turns hit, and missing it made the guard
    useless in practice.
    """
    if state.get("_drop_reasoning") or not state["cfg"].get("reasoning_effort"):
        return False
    if "reasoning_effort" not in (message or ""):
        return False
    state["_drop_reasoning"] = True
    # Remember it: later turns on this model skip the field (and the retry)
    # entirely, without ever guessing from an unreliable catalog flag.
    bad = state["cfg"].get("model")
    state.setdefault("_reasoning_rejected", set()).add(bad)
    # Persist (capped) so the NEXT session skips the doomed call too.
    try:
        from .config import save_config
        stored = list(state["cfg"].get("reasoning_rejected_models") or [])
        if bad and bad not in stored:
            state["cfg"]["reasoning_rejected_models"] = (stored + [bad])[-50:]
            save_config(state["cfg"])
    except Exception:
        pass
    console.print(
        "[yellow]⚠ this model rejected the reasoning-effort setting — "
        "retrying without it (your setting is kept for models that support "
        "it)[/yellow]"
    )
    return True


def _smart_route_abandon(state: dict, reason: str) -> bool:
    """The smart pick failed LIVE (empty replies / fatal error) — blacklist
    it for the session and fall back to the pinned model, keeping the turn
    alive. True if there was a pick to abandon.

    This is outcome-level fail-open: a routing table can say a model is
    good, but the model answering NOW beats the table's opinion of it.
    """
    bad = state.get("_smart_pick")
    if not bad:
        return False
    state.setdefault("_smart_bad", set()).add(bad)
    state["_smart_pick"] = None
    state["_smart_last"] = None
    console.print(
        f"[yellow]⚠ smart pick {bad} {reason} — falling back to "
        f"{state['cfg']['model']} (that model is skipped for the rest of "
        "this session)[/yellow]"
    )
    return True


def _smart_route_turn(state: dict, user_input: str) -> None:
    """Compute this turn's smart pick (route_mode == "smart"). Fail-open:
    any miss leaves state["_smart_pick"] unset and the pinned model rides.

    Runs once per user turn — the pick then holds for every hop (mid-turn
    model flapping would waste prompt cache and confuse the transcript).
    needs_tools is always True here: this CLI sends its tool schema on every
    request, so the picked model must be able to hold tools regardless of
    which cohort the prompt itself lands in.
    """
    state["_smart_pick"] = None
    cfg = state["cfg"]
    if cfg.get("route_mode") != "smart":
        return
    try:
        table = router.load_table()
        if table is None:
            return
        if not state.get("models_cache"):
            fetch_models_quiet(state)
        catalog = state.get("models_cache")
        if not catalog:
            return
        history_chars = sum(
            len(m.get("content") or "") if isinstance(m.get("content"), str) else 0
            for m in state.get("messages") or [])
        cohort, _conf = router.classify(
            user_input,
            has_image=bool(state.get("pending_attachments")),
            has_tools=True,
            history_chars=history_chars,
        )
        # Short follow-ups ("2", "yes", "continue") are answers WITHIN the
        # ongoing task, not new tasks — inherit the conversation's cohort
        # instead of reclassifying three characters of text.
        forced = cfg.get("route_effort", "auto")
        if forced != "auto":
            difficulty = forced                      # user pinned the effort
        elif len(user_input.strip()) < 25 and state.get("_smart_cohort"):
            difficulty = state.get("_smart_difficulty") or "mid"
        else:
            difficulty = router.estimate_difficulty(user_input)
        if len(user_input.strip()) < 25 and state.get("_smart_cohort"):
            cohort = state["_smart_cohort"]
        state["_smart_cohort"] = cohort
        state["_smart_difficulty"] = difficulty
        needs_ctx = int(compact.est_history_tokens(state.get("messages") or []) * 1.3) + 4096
        weights = router.effective_weights(cfg.get("route_weights"), difficulty)
        info = router.pick(cohort, weights, table, catalog,
                           needs_tools=True, needs_ctx=needs_ctx,
                           incumbent=state.get("_smart_last"),
                           exclude=state.get("_smart_bad"))
        if not info or not info.get("model"):
            return
        info["difficulty"] = difficulty
        state["_smart_pick"] = info["model"]
        state["_smart_last"] = info["model"]
        state["_smart_pick_info"] = info
        _dtag = "" if difficulty in ("mid", "medium") else f"/{difficulty}"
        console.print(
            f"[{BRAND_DIM}]⚙ smart route: {info['cohort']}{_dtag} → {info['model']}"
            f"{' (sticky)' if info.get('sticky') else ''}  (/route why)[/{BRAND_DIM}]"
        )
    except Exception:
        state["_smart_pick"] = None  # never let routing break a turn


def _effective_cfg(state: dict) -> dict:
    """cfg for this hop, minus settings this model has proven it can't take.

    We deliberately do NOT gate `reasoning_effort` on the catalog's
    `supports_thinking` flag: it is False for gpt-5.4, sonnet-4.6, opus-4.8
    and haiku-4.5, yet the gateway accepts the field on them (verified live,
    HTTP 200). Trusting the flag would silently disable reasoning on exactly
    the models people want it for.

    So the rule is evidence-based: send it, and if a provider actually
    rejects it, remember that model for the session and stop sending it.
    Costs one retry per model, once — and never disables a feature that
    works.
    """
    cfg = state["cfg"]
    if state.get("_smart_pick") and cfg.get("route_mode") == "smart":
        # Smart routing: this turn's locally-picked model replaces the pin;
        # auto_route is forced off so build_payload sends the concrete id.
        cfg = {**cfg, "model": state["_smart_pick"], "auto_route": False}
    if not cfg.get("reasoning_effort"):
        return cfg
    rejected = state.get("_reasoning_rejected") or set()
    model_id = cfg.get("model")
    if state.get("_drop_reasoning") or (model_id in rejected and not cfg.get("auto_route")):
        return {**cfg, "reasoning_effort": None}
    return cfg


def _bare_command(text: str) -> "str | None":
    """"route" -> "/route": a lone word that exactly names a command is a
    mistyped command, not a prompt. One such slip cost a user an 18k-token
    agentic turn ($0.06) while the model explored the repo to guess what
    "route" meant. Single word only, exact name only (never prefixes —
    prose like "continue" must keep going to the model); add more words to
    genuinely send a command-name as a prompt.
    """
    t = (text or "").strip().lower()
    if not t or " " in t or t.startswith("/") or len(t) < 2:
        return None
    from .completer import COMMANDS
    known = {c.lstrip("/") for c in COMMANDS} | {"quit", "effort"}
    return "/" + t if t in known else None


def _utc_now_iso() -> str:
    """UTC timestamp for the usage-API `since` filter."""
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=5)).isoformat()


def _compact_and_report(state: dict, why: str) -> bool:
    """Aggressively compact history mid-hop; True if it actually freed room.

    Returning False (nothing left to compact) is what stops a compact→retry
    →compact thrash loop: the caller then surfaces the error instead of
    spinning. Claude Code guards the same way, via its
    `willRetriggerNextTurn` / "made no progress" checks.
    """
    rep = compact.compact_history(
        state, limit=state.get("_ctx_limit"), aggressive=True
    )
    if not rep:
        return False
    console.print(
        f"[dim]⚙ {why} — compacted history "
        f"~{rep['before_tok'] // 1000}k → ~{rep['after_tok'] // 1000}k tok "
        "(est), retrying[/dim]"
    )
    return True


def _try_non_streaming(state: dict, turn_messages: list):
    """Last-resort blocking request when streaming keeps failing.

    Mesh's server-side retry and provider fallback are documented to apply
    to non-streaming chat completions ONLY — a streaming-only client gets
    none of it. One blocking attempt therefore has a genuinely different
    failure profile than a sixth stream, and it is the same escape hatch
    Claude Code uses (`didFallBackToNonStreaming`).

    Returns (reply, meta) on success, or None to let the caller re-raise.
    """
    console.print(
        "[yellow]⚠ streaming keeps failing — retrying once without "
        "streaming (this also enables the gateway's own retry/fallback)"
        "[/yellow]"
    )
    _cfg = _effective_cfg(state)
    try:
        reply, meta = complete_chat(
            turn_messages, _cfg, tools=TOOLS,
            max_tokens=state.get("_max_tokens_shrunk"),
        )
    except KeyboardInterrupt:
        raise
    except Exception as e:
        console.print(f"[dim]  non-streaming attempt also failed: {e}[/dim]")
        return None
    if reply:
        console.print(Markdown(reply))
    return reply, meta


def _stream_hop_with_retry(state: dict, extras: list, hdr: str):
    """One model round-trip with bounded retry — the per-hop resilience the
    gateway can't provide (its retry/fallback applies only to non-streaming
    requests, and this CLI is streaming-only).

    Retries with exponential backoff + jitter on network errors, retryable
    HTTP statuses (429/5xx), transient in-band errors, and (twice) on an
    empty response. An in-band CONTEXT error compacts history aggressively
    and retries once — the recovery that makes day-long turns survivable.
    Fatal errors are returned via meta for the caller's error path; hard
    failures re-raise only after MAX_STREAM_ATTEMPTS. A mid-stream retry
    re-renders from scratch; the partial text above the retry line is
    cosmetic scrollback — it never entered history.
    """
    compacted_for_context = False
    empty_retries = 0
    tried_non_streaming = False
    attempt = 0
    while True:
        attempt += 1
        # Rebuilt each attempt so a compaction between attempts is picked up.
        turn_messages = state["messages"] + extras if extras else state["messages"]
        # A shrunk output budget (from a max_tokens/context 400) must ride the
        # RETRY too, not just the non-streaming fallback — otherwise the same
        # request is re-sent and rejected identically.
        _cfg = _effective_cfg(state)
        if state.get("_max_tokens_shrunk"):
            _cfg = {**_cfg, "max_tokens": state["_max_tokens_shrunk"]}
        try:
            reply, meta = render_stream(
                stream_chat(turn_messages, _cfg, tools=TOOLS),
                header=hdr,
                state=state,
            )
        except KeyboardInterrupt:
            raise
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            # A max_tokens/context arithmetic 400 is recoverable without
            # dropping any history — ask for fewer output tokens instead.
            _body = _safe_response_text(e.response)
            # Reactive safety net for the case the catalog couldn't predict
            # (auto-routing, a stale/offline catalog): the upstream names
            # reasoning_effort as unacceptable — drop it and retry so the
            # turn proceeds instead of dying on a preference.
            if _maybe_drop_reasoning(state, _body):
                continue
            _ovf = loopguard.parse_max_tokens_overflow(_body)
            if _ovf is not None and not state.get("_max_tokens_shrunk"):
                _new = loopguard.adjusted_max_tokens(_ovf)
                if _new:
                    state["_max_tokens_shrunk"] = _new
                    console.print(
                        "[dim]⚙ output budget exceeded the context window — "
                        f"retrying with max_tokens={_new}[/dim]"
                    )
                    continue
                # No room left for output: this is really a context problem.
                if not compacted_for_context and state["cfg"].get("auto_compact", True):
                    compacted_for_context = True
                    if _compact_and_report(state, "context limit reached"):
                        continue
            if attempt < loopguard.MAX_STREAM_ATTEMPTS and loopguard.is_retryable_status(code):
                # The server's own Retry-After beats our exponential guess.
                _ra = loopguard.retry_after_seconds(e.response)
                _reason = f"gateway returned {code}"
                if _ra is None and e.response.headers.get("retry-after"):
                    console.print(
                        "[yellow]⚠ gateway asked for a wait longer than "
                        f"{loopguard.RETRY_AFTER_MAX:.0f}s — not blocking on "
                        "it[/yellow]"
                    )
                    raise
                _retry_wait(state, attempt, _reason, delay=_ra)
                continue
            # Streaming keeps failing — try ONE non-streaming request. Mesh's
            # server-side retry + provider fallback only covers non-streaming
            # requests, so this is the CLI's last real chance to recover.
            if not tried_non_streaming and loopguard.is_retryable_status(code):
                tried_non_streaming = True
                _r = _try_non_streaming(state, turn_messages)
                if _r is not None:
                    return _r
            raise
        except httpx.RequestError as e:
            if attempt < loopguard.MAX_STREAM_ATTEMPTS:
                _retry_wait(state, attempt, f"network error ({type(e).__name__})")
                continue
            if not tried_non_streaming:
                tried_non_streaming = True
                _r = _try_non_streaming(state, turn_messages)
                if _r is not None:
                    return _r
            raise
        err = meta.get("error")
        if err:
            # Streaming reports an unacceptable request as HTTP 200 + an
            # in-band error, so this is where a rejected reasoning_effort
            # actually lands.
            if _maybe_drop_reasoning(state, str(err)):
                continue
            kind = loopguard.classify_inband_error(str(err))
            if kind == "transient" and attempt < loopguard.MAX_STREAM_ATTEMPTS:
                _retry_wait(state, attempt, "gateway is rate-limiting or overloaded")
                continue
            if (kind == "context" and not compacted_for_context
                    and state["cfg"].get("auto_compact", True)):
                compacted_for_context = True
                if _compact_and_report(state, "context limit hit"):
                    continue
            if kind == "fatal" and _smart_route_abandon(state, "hit a fatal error"):
                continue  # pinned model takes over this hop
            return reply, meta  # fatal — the caller's error path handles it
        if (not meta.get("tool_calls") and not (reply or "").strip()
                and attempt < loopguard.MAX_STREAM_ATTEMPTS):
            if empty_retries < 1:
                empty_retries += 1
                _retry_wait(state, attempt, "empty response from the model")
                continue
            # One retry on the same model is enough evidence — if it was a
            # smart pick, abandon it and let the pinned model take the turn.
            if _smart_route_abandon(state, "keeps returning empty responses"):
                empty_retries = 0
                continue
            if empty_retries < 2:
                empty_retries += 1
                _retry_wait(state, attempt, "empty response from the model")
                continue
        return reply, meta


def _safe_response_text(resp) -> str:
    """Return response.text, falling back to a placeholder if the body
    can't be read (e.g. streamed response not yet consumed)."""
    try:
        return resp.text
    except Exception:
        try:
            return resp.read().decode("utf-8", errors="replace")
        except Exception:
            return "<response body unavailable>"


def confirm_tool_call(name: str, args: dict, watcher=None, session_allow=None) -> bool:
    """ASK-mode prompt for a single tool call. Returns True if approved.

    `watcher` is the KeyWatcher: paused around `console.input` so the
    terminal is in canonical line-edit mode while reading the y/n answer.
    `session_allow` is the session allowlist set: answering `a` approves AND
    adds the tool so it never asks again this session (Claude Code's
    "don't ask again"). Safety guards still apply to allowlisted tools.
    """
    summary = summarize_call(name, args)
    console.print(f"[bold {BRAND}]⚙ approve tool call?[/bold {BRAND}]  [dim]{summary}[/dim]")
    if name in ("read_file", "write_file"):
        console.print(_resolved_path_line(args.get("path") or ""))
    if name == "write_file":
        preview = (args.get("content") or "")[:300]
        console.print(f"[dim]──[/dim]\n{preview}{'…' if len(args.get('content') or '') > 300 else ''}\n[dim]──[/dim]")
    elif name == "run_bash":
        console.print(f"[dim]$ {args.get('command')}[/dim]")
    elif name == "start_server":
        port = args.get("port") or "auto"
        cwd = args.get("cwd") or str(Path.cwd())
        console.print(f"[dim]$ {args.get('command')}[/dim]  [dim](port {port}, cwd {cwd})[/dim]")
    elif name == "web_search":
        # Show the exact query verbatim — approving sends it off-machine.
        console.print(f"[dim]🔎 {_rich_escape(args.get('query') or '')}[/dim]")
    # Pause the keywatcher so console.input gets canonical-mode stdin.
    paused_ctx = watcher.paused() if watcher is not None else _noop_ctx()
    allow_hint = (
        f" / [bold]a[/bold] (always for {name} this session)"
        if session_allow is not None else ""
    )
    try:
        with paused_ctx:
            ans = console.input(
                f"[bold]y[/bold] (yes){allow_hint} / [bold]n[/bold] (no)  › "
            ).strip().lower()
    except KeyboardInterrupt:
        # Bubble up so the outer turn handler can abort cleanly.
        raise
    except EOFError:
        return False
    except Exception:
        # If the input prompt itself blows up (corrupted terminal state, etc.),
        # treat it as a deny and keep the session alive.
        return False
    if ans in ("a", "always") and session_allow is not None:
        session_allow.add(name)
        console.print(f"[dim]  ✓ auto-approving {name} for the rest of this session[/dim]")
        return True
    return ans in ("y", "yes")


@contextlib.contextmanager
def _noop_ctx():
    yield


def _cwd_rule() -> None:
    """The input frame's top edge: cwd · git-branch, right-aligned."""
    title = Path.cwd().name
    branch = _git_branch()
    if branch:
        title += f" · {branch}"
    console.rule(
        title=f"[{BRAND_DIM}]{title}[/{BRAND_DIM}]",
        align="right",
        style=BRAND_DIM,
        characters="─",
    )


def _print_input_frame(text: str) -> None:
    """Transcript block for a queue-drained message — visually identical to
    a typed one (top rule, highlighted `› text` line, closing rule) so the
    conversation reads uniformly whether the user typed at the prompt or
    stacked messages mid-run."""
    _cwd_rule()
    line = Text()
    line.append("› ", style=f"bold {BRAND} on {BRAND_BG}")
    line.append(text, style=f"{BRAND_BG_FG} on {BRAND_BG}")
    line.append("  (queued)", style="dim")
    console.print(line)
    console.rule(style=BRAND_DIM, characters="─")
    console.print()


_BRANCH_CACHE = {"t": 0.0, "cwd": None, "branch": None}


def _git_branch() -> "str | None":
    """Current git branch for the prompt rule, or None outside a repo /
    detached HEAD. Best-effort, cached 5s so the per-prompt cost is one
    subprocess at most every few turns."""
    now = time.monotonic()
    cwd = str(Path.cwd())
    if _BRANCH_CACHE["cwd"] == cwd and now - _BRANCH_CACHE["t"] < 5.0:
        return _BRANCH_CACHE["branch"]
    branch = None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=1, cwd=cwd,
        )
        if out.returncode == 0:
            b = out.stdout.strip()
            branch = b if b and b != "HEAD" else None
    except Exception:
        branch = None
    _BRANCH_CACHE.update(t=now, cwd=cwd, branch=branch)
    return branch


_PORT_RANGE = (5173, 5273)  # vite's default + 100 fallback ports


def _find_free_port(start: int = _PORT_RANGE[0], end: int = _PORT_RANGE[1]) -> int:
    """Pick a port in [start, end) that we can currently bind. Races are
    possible (port could be grabbed between probe and child bind) but the
    window is milliseconds — acceptable for dev workflows."""
    for p in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"no free port in {start}..{end}")


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Cheap readiness check: can we connect to the port?"""
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except (OSError, socket.timeout):
        return False


# Explicit port in a server command. Rule 1: port flags (`--port 3000`,
# `-p 8080:80` docker-style keeps the HOST side). Rule 2: host:port token
# (`php -S localhost:8000`, `gunicorn -b :8000`) — anchored at token start so
# ports inside URLs (`--open http://localhost:3000`) deliberately DON'T match
# (the adoption net catches those). Rule 3 (in code): bare pure-digit token
# 1024..65535 (`python3 -m http.server 8080`) — the floor kills `2>&1`, `-j8`,
# `sleep 30`, `--max-old-space-size=4096` and friends.
_FLAG_PORT_RE = re.compile(
    r"(?:^|\s)(?:--(?:port|server-port|listen-port)|-p)[=\s]+(\d{1,5})(?::\d{1,5})?(?=\s|$)"
)
_COLON_PORT_RE = re.compile(r"(?:^|[\s=])[\w.\-*\[\]]*:(\d{1,5})(?=[\s/]|$)")


def _extract_command_port(cmd: str) -> "int | None":
    """Explicit port named in the command itself, or None.

    Biased against false positives — a miss just costs one adoption-scan
    cycle (~2s), a false positive means prechecking/waiting on the wrong
    port. Last match wins within each rule; flag > colon > bare token.
    """
    for rx in (_FLAG_PORT_RE, _COLON_PORT_RE):
        hits = [int(m.group(1)) for m in rx.finditer(cmd)]
        hits = [h for h in hits if 1 <= h <= 65535]
        if hits:
            return hits[-1]
    bare = [int(t) for t in cmd.split() if t.isdigit() and 1024 <= int(t) <= 65535]
    return bare[-1] if bare else None


_HTTP_SERVER_RE = re.compile(
    r"^\s*\S*python[\d.]*(?:\s+-[a-zA-Z]+)*\s+-m\s+http\.server(?:\s+--?\S+(?:\s+\S+)?)*\s*$"
)


def _maybe_append_port(cmd: str, port: int) -> tuple:
    """python's http.server ignores the PORT env var (binds 8000 by default),
    so a bare `python3 -m http.server` can never open the port we wait on.
    Append the chosen port for exactly that shape. Returns (cmd, appended)."""
    if _HTTP_SERVER_RE.match(cmd):
        return f"{cmd.rstrip()} {port}", True
    return cmd, False


def _discover_listen_ports(pgid: int) -> list:
    """TCP ports the spawned process GROUP is listening on. POSIX best-effort
    — [] on any failure, never raises (the wait loop must not crash the REPL).

    One `lsof -g <pgid>` call (macOS + most Linux): ~25ms, exit 1 just means
    "no matches". `-Fn` machine format: parse `n<addr>:<port>` lines.
    """
    if os.name != "posix":
        return []
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-g", str(pgid), "-a", "-iTCP", "-sTCP:LISTEN", "-Fn"],
            capture_output=True, text=True, timeout=2,  # lsof can hang on dead NFS
        ).stdout
    except FileNotFoundError:
        return _discover_via_ss(pgid)
    except Exception:
        return []
    ports = []
    for line in out.splitlines():
        if line.startswith("n") and ":" in line:
            tail = line.rsplit(":", 1)[1]
            if tail.isdigit() and int(tail) not in ports:
                ports.append(int(tail))
    return sorted(ports)


def _discover_via_ss(pgid: int) -> list:
    """Fallback for lsof-less Linux (minimal containers): ss -tlnp filtered
    to the group's pids. We spawn with start_new_session=True, so sid ==
    pgid == the child's pid — `ps -g` selects by group on macOS and by
    session on Linux procps, and both resolve to the same tree here."""
    try:
        pids = set(subprocess.run(
            ["ps", "-o", "pid=", "-g", str(pgid)],
            capture_output=True, text=True, timeout=2,
        ).stdout.split())
        if not pids:
            return []
        out = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return []
    ports = []
    for line in out.splitlines():
        if not any(f"pid={p}" in line for p in pids):
            continue
        fields = line.split()
        if len(fields) >= 4 and ":" in fields[3]:
            tail = fields[3].rsplit(":", 1)[1]
            if tail.isdigit() and int(tail) not in ports:
                ports.append(int(tail))
    return sorted(ports)


def _kill_server(pid: int) -> None:
    """SIGTERM the entire process group of a tracked server (best-effort)."""
    try:
        # os.killpg/os.getpgid are POSIX-only. On Windows there are no process
        # groups (start_new_session is a no-op), so kill the single pid.
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)  # Windows: TerminateProcess, single pid
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _persist_servers(state: dict) -> None:
    """Write current live servers to ~/.meshapi/servers.json. Best-effort —
    a corrupt or missing file should never block the REPL."""
    try:
        save_servers(state.get("servers", []))
    except Exception:
        pass


def _shutdown_servers(state: dict) -> None:
    """Kill every server we launched. Called on meshapi exit (clean or
    via SIGTERM/SIGHUP). Also wipes the persisted servers file so the
    next launch doesn't offer to kill ghosts."""
    for srv in state.get("servers", []):
        _kill_server(srv["pid"])
    state["servers"] = []
    clear_servers_file()


def _adopt_orphaned_servers(state: dict) -> None:
    """At startup, look for processes recorded by a previous (crashed)
    meshapi and offer to terminate them. A hard kill of meshapi (SIGKILL,
    laptop sleep + battery, segfault) skips atexit/SIGTERM, so this is
    the safety net that catches leaked servers."""
    rec = load_servers()
    if not rec:
        return
    live = []
    for s in rec:
        pid = s.get("pid") if isinstance(s, dict) else None
        if not isinstance(pid, int):
            continue
        try:
            os.kill(pid, 0)  # signal 0 = existence check, no actual signal
        except (ProcessLookupError, PermissionError):
            continue
        except OSError:
            continue
        live.append(s)
    if not live:
        clear_servers_file()
        return
    console.print(
        f"[yellow]Found {len(live)} background server(s) left running from a "
        "previous session:[/yellow]"
    )
    for s in live:
        console.print(
            f"  [dim]pid {s.get('pid')}, port {s.get('port')}, "
            f"{s.get('cmd', '')}[/dim]"
        )
    try:
        ans = console.input(
            "Kill them now? [bold]y[/bold] (yes) / [bold]n[/bold] (no)  › "
        ).strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if ans in ("y", "yes"):
        for s in live:
            _kill_server(s.get("pid", 0))
        clear_servers_file()
        console.print(f"[dim]Killed {len(live)} server(s).[/dim]")
    else:
        clear_servers_file()  # don't keep asking on every launch
        console.print("[dim]Leaving them running.[/dim]")


def _check_image_cap(state: dict, additional_bytes: int) -> tuple[bool, str]:
    """Per-session image-bytes budget. Counts both already-sent and queued
    attachments — clearing the queue (/clear-attach) releases them again."""
    sent = state.get("session_image_bytes", 0)
    queued = sum(int(a.get("size_bytes", 0))
                 for a in (state.get("pending_attachments") or []))
    total = sent + queued + additional_bytes
    if total > safety.SESSION_IMAGE_BYTE_CAP:
        cap_mb = safety.SESSION_IMAGE_BYTE_CAP // (1024 * 1024)
        used_mb = max(1, (sent + queued) // (1024 * 1024))
        return False, (
            f"would exceed session image budget ({cap_mb} MB total, "
            f"{used_mb} MB used)"
        )
    return True, ""


def _handle_start_server(args: dict, state: dict) -> str:
    """Spawn a long-running server detached, wait for its port, return URL.

    The server keeps running after this function returns. We track its
    pid + port in state["servers"] so the CLI can clean it up on exit.
    """
    cmd = args.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return "Error: start_server requires a `command` argument."

    # Port resolution precedence: explicit port in the COMMAND > `port` arg
    # > auto-pick. The command wins because it's what the server will
    # actually bind — the live failure mode was waiting on an auto-picked
    # port while `python3 -m http.server 8080` listened on 8080.
    arg_port = args.get("port")
    if arg_port is not None and (
        not isinstance(arg_port, int) or arg_port < 1 or arg_port > 65535
    ):
        return f"Error: invalid port {arg_port!r}; must be an integer in 1..65535."
    cmd_port = _extract_command_port(cmd)
    if cmd_port is not None:
        if arg_port is not None and arg_port != cmd_port:
            console.print(
                f"  [dim]command specifies port {cmd_port}; "
                f"ignoring port arg {arg_port}[/dim]"
            )
        port, port_source = cmd_port, "command"
    elif arg_port is not None:
        port, port_source = arg_port, "arg"
    else:
        try:
            port = _find_free_port()
        except RuntimeError as e:
            return f"Error: {e}"
        port_source = "auto"

    if port_source != "auto" and _port_open(port):
        # Is it OUR server from earlier this session? Then the fix is to not
        # restart it — this exact loop (restart → port busy → retry) burned
        # a live session.
        for srv in state.get("servers", []):
            if srv.get("port") == port:
                return (
                    f"Error: port {port} is YOUR OWN server started earlier "
                    f"this session — it is already running at {srv['url']} "
                    f"(pid {srv['pid']}). Do NOT start it again; just tell "
                    "the user the URL."
                )
        if port_source == "command":
            return (
                f"Error: your command specifies port {port}, which is "
                "already in use. Change the port in the command, or stop "
                f"whatever is listening on {port}."
            )
        return (
            f"Error: port {port} is already in use. Pick a different port or "
            "omit `port` to auto-pick a free one."
        )

    # python's http.server ignores PORT env — append the port for that shape.
    appended = False
    if cmd_port is None:
        cmd, appended = _maybe_append_port(cmd, port)

    wait_seconds = args.get("wait_seconds")
    if not isinstance(wait_seconds, int) or wait_seconds < 1 or wait_seconds > 300:
        wait_seconds = 30

    cwd = args.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = str(Path.cwd())
    try:
        cwd = str(Path(cwd).expanduser().resolve())
    except Exception:
        return f"Error: invalid cwd {cwd!r}"

    env = os.environ.copy()
    env["PORT"] = str(port)
    env["BROWSER"] = "none"  # stop CRA / others from auto-opening a browser

    console.print(f"  [{CODE}]$[/{CODE}] [{CODE}]{_rich_escape(cmd)}[/{CODE}]")
    detail = f"port {port} (from {port_source}), cwd {cwd}"
    if appended:
        detail += "  — appended port: http.server ignores PORT env"
    console.print(f"  [dim]{detail}[/dim]")

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            start_new_session=True,  # own pgid so we can kill the whole tree
        )
    except Exception as e:
        return f"Error: failed to spawn server: {e}"

    # Drain output in a thread so the pipe buffer never fills up (long-lived
    # servers can produce gigabytes of logs). Keep the last 1000 lines in
    # memory in case we want to surface them.
    output_lines: list = []
    output_lock = threading.Lock()

    def _drain() -> None:
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                with output_lock:
                    output_lines.append(line)
                    if len(output_lines) > 1000:
                        del output_lines[: len(output_lines) - 1000]
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    drainer = threading.Thread(target=_drain, daemon=True, name=f"server-{proc.pid}-drain")
    drainer.start()

    start_t = time.monotonic()

    def _success(final_port: int) -> str:
        """Record + announce the server on `final_port` (== expected `port`,
        or a discovered/adopted one when the command ignored PORT env)."""
        elapsed = time.monotonic() - start_t
        # Give the server a beat to log its banner ("ready in X ms" etc.)
        time.sleep(0.4)
        with output_lock:
            preview = "\n".join(output_lines[:20])
        url = f"http://localhost:{final_port}"
        state.setdefault("servers", []).append({
            "pid": proc.pid, "port": final_port, "cmd": cmd, "url": url,
        })
        _persist_servers(state)  # survive a hard kill / crash

        # Make the URL big, plain, on its own line — most terminals
        # auto-detect bare URLs as cmd-clickable, which is more reliable
        # than rich's OSC-8 `[link=...]` markup that some terminals
        # (xterm.js, older Terminal.app) strip silently.
        from rich.panel import Panel
        console.print(f"  [green]✓ ready in {elapsed:.1f}s[/green]")
        if final_port != port:
            console.print(
                f"  [yellow]note: command ignored PORT={port} and bound "
                f"{final_port} — using {url}[/yellow]"
            )
        console.print()
        console.print(Panel.fit(
            f"[bold green]{url}[/bold green]\n"
            f"[dim]server running in the background  ·  pid {proc.pid}  ·  "
            "⌘-click or paste the URL in your browser[/dim]",
            title="🌐 ready",
            border_style="green",
            padding=(0, 2),
        ))
        console.print()
        if preview.strip():
            console.print("  [dim]── server output ──[/dim]")
            for line in preview.split("\n")[:_MAX_BODY_LINES]:
                if len(line) > _MAX_LINE_LEN:
                    line = line[:_MAX_LINE_LEN] + "…"
                console.print(f"    [dim]{_rich_escape(line)}[/dim]")

        note = ""
        if final_port != port:
            note = (
                f"\nNOTE: your command bound port {final_port}, not the "
                f"expected {port} — it ignores the PORT env var. Next time "
                f"put the port in the command or pass port: {final_port}."
            )
        return (
            f"Server up at {url} (pid {proc.pid}, ready in {elapsed:.1f}s).\n"
            "The user can already see the URL in their terminal — it was "
            "printed by the CLI. If a plan is active, FIRST call update_step "
            "to mark every finished step completed (plan bookkeeping is "
            "still allowed — it runs nothing). Then respond with a SINGLE "
            "short text line (e.g. 'Server's up at " + url + " — open it in "
            "your browser') and END THE TURN. Do NOT call any OTHER tools — "
            "no curl, no read_file, no more servers. The server keeps "
            "running in the background until meshapi exits; the user will "
            "interact with it through the browser, not through you." + note
        )

    # Poll for readiness: the expected port, plus a periodic discovery scan
    # of what the process group ACTUALLY listens on (adopts mismatches in
    # ~2s instead of timing out), plus a ticker so the wait is never silent.
    deadline = start_t + wait_seconds
    next_discovery = start_t + 2.0
    next_tick = start_t + 5.0
    exit0_grace = None
    last_echoed = ""
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            rc = proc.poll()
            if rc is not None and rc != 0:
                with output_lock:
                    tail = "\n".join(output_lines[-30:])
                return (
                    f"Error: server exited with code {rc} before "
                    f"opening port {port}.\nOutput:\n{tail or '(no output)'}"
                )
            if rc == 0 and exit0_grace is None:
                # Shell exited cleanly — it may have backgrounded a daemon
                # that inherited the pgid. Grace window + immediate scan
                # instead of misreporting "server exited".
                exit0_grace = min(deadline, now + 5.0)
                next_discovery = now
            if _port_open(port):
                return _success(port)
            if now >= next_discovery:
                next_discovery = now + 2.0
                # pgid == proc.pid thanks to start_new_session=True.
                for p in _discover_listen_ports(proc.pid):
                    if p != port and _port_open(p):
                        return _success(p)  # adopt what it actually bound
            if rc == 0 and exit0_grace is not None and now >= exit0_grace:
                with output_lock:
                    tail = "\n".join(output_lines[-30:])
                return (
                    "Error: the command exited 0 without leaving a listening "
                    "server behind. If it daemonizes, keep it in the "
                    f"foreground instead.\nOutput:\n{tail or '(no output)'}"
                )
            if now >= next_tick:
                next_tick = now + 5.0
                waited = int(now - start_t)
                console.print(
                    f"  [dim]… waiting for port {port} "
                    f"({waited}s/{wait_seconds}s) — ctrl+c to abort[/dim]"
                )
                with output_lock:
                    newest = output_lines[-1] if output_lines else ""
                if newest and newest != last_echoed:
                    last_echoed = newest
                    line = newest
                    if len(line) > _MAX_LINE_LEN:
                        line = line[:_MAX_LINE_LEN] + "…"
                    console.print(f"  [dim]│ {_rich_escape(line)}[/dim]")
            time.sleep(0.2)
    except KeyboardInterrupt:
        # Don't orphan the half-started server: it isn't in state["servers"]
        # yet, so nothing else would ever kill it.
        _kill_server(proc.pid)
        raise

    # Timeout — see what it IS listening on (for the error), then kill.
    leftover = _discover_listen_ports(proc.pid)
    _kill_server(proc.pid)
    with output_lock:
        tail = "\n".join(output_lines[-30:])
    if leftover:
        ports_s = ", ".join(str(p) for p in leftover)
        return (
            f"Error: timed out after {wait_seconds}s. The server IS listening "
            f"on port(s) {ports_s}, but not reachable at "
            f"http://localhost:{leftover[0]} — it may be bound to a specific "
            "interface or running inside a container. Killed it. Bind to "
            f"127.0.0.1 or 0.0.0.0 and retry.\nOutput so far:\n{tail or '(no output)'}"
        )
    return (
        f"Error: timed out after {wait_seconds}s — the process never opened "
        "a TCP port. Killed it. If the command takes a fixed port, put the "
        "port in the command (it is auto-detected: '--port 3000', "
        "'localhost:8000', or a trailing number like 'http.server 8080'). "
        "Note: python -m http.server ignores the PORT env var.\n"
        f"Output so far:\n{tail or '(no output)'}"
    )


def _handle_plan_tool(name: str, args: dict, state: dict) -> str:
    """Execute a plan tool (auto-approved, mutates state['plan'], renders).

    Returns the string result that gets sent back to the model.
    """
    if name == "create_plan":
        steps = args.get("steps")
        if not isinstance(steps, list) or not steps:
            return "Error: create_plan requires a non-empty `steps` list."
        state["plan"] = Plan(steps)
        if not state["plan"].steps:
            state["plan"] = None
            return "Error: all steps were empty after trimming whitespace."
        console.print(f"[{BRAND_DIM}]⚙ {summarize_call(name, args)}[/{BRAND_DIM}]")
        state["plan"].render()
        return f"Plan created with {len(state['plan'].steps)} step(s). Now call update_step(1, 'in_progress') and start work."

    if name == "update_step":
        if state.get("plan") is None:
            return "Error: no active plan. Call create_plan first."
        err = state["plan"].update(args.get("index"), args.get("status"))
        if err:
            return f"Error: {err}"
        console.print(f"[{BRAND_DIM}]⚙ {summarize_call(name, args)}[/{BRAND_DIM}]")
        state["plan"].render()
        return f"Step {args['index']} → {args['status']}. {state['plan'].summary()}"

    return f"Error: unknown plan tool `{name}`"


def _prepare_call(tc: dict) -> dict:
    """Classify one accumulated tool call — parse, normalize, repair. No I/O.

    Returns {id, name, raw, args, history_args, kind, error, pos} with kind:
      ok          strict-valid dict, required fields present → execute
      normalized  parsed only with strict=False (raw control chars) → execute
      repaired    missing-comma repair succeeded → execute
      invalid     valid JSON but wrong shape / missing field → skip + feedback
      truncated   args cut off mid-stream → skip; NEVER fabricate closures
      unparseable everything else → skip + feedback with error window

    `history_args` is what gets replayed in the assistant message: raw only
    when it's valid JSON, canonical json.dumps for normalized/repaired, and
    "{}" for the doomed kinds — the model must never re-read its own
    malformed JSON (it few-shot-primes itself into repeating the mistake),
    and strict gateways translating to Anthropic tool_use must always
    receive parseable input.
    """
    raw = tc.get("arguments") or ""
    p = {"id": tc["id"], "name": tc["name"], "raw": raw, "args": {},
         "history_args": "{}", "kind": "invalid", "error": "", "pos": None}
    stripped = raw.strip()
    if not stripped:
        p["error"] = validate_call(p["name"], {}) or (
            f"Error: {p['name']} received empty arguments."
        )
        return p
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as e:
        p["pos"], p["error"] = e.pos, str(e)
        try:
            lenient = json.loads(stripped, strict=False)
        except json.JSONDecodeError:
            lenient = None
        if isinstance(lenient, dict):
            p["args"] = lenient
            p["history_args"] = json.dumps(lenient, ensure_ascii=False)
            err = validate_call(p["name"], lenient)
            if err:
                p["kind"], p["error"] = "invalid", err
            else:
                p["kind"], p["error"] = "normalized", ""
            return p
        repaired, reason = repair_tool_args(stripped)
        if repaired is not None:
            fixed = json.loads(repaired, strict=False)
            p["args"] = fixed
            p["history_args"] = json.dumps(fixed, ensure_ascii=False)
            err = validate_call(p["name"], fixed)
            if err:
                p["kind"], p["error"] = "invalid", err
            else:
                p["kind"], p["error"] = "repaired", ""
            return p
        p["kind"] = "truncated" if reason == "truncated" else "unparseable"
        return p
    if not isinstance(obj, dict):
        p["error"] = (
            f"Error: {p['name']} arguments must be a single JSON object, "
            f"got {type(obj).__name__}."
        )
        return p
    p["args"] = obj
    err = validate_call(p["name"], obj)
    if err:
        # Valid JSON, wrong contents — truthful verbatim replay is safe here.
        p["history_args"], p["error"] = raw, err
        return p
    p["kind"], p["history_args"] = "ok", raw
    return p


def _doom_feedback(p: dict, streak: int) -> str:
    """Prescriptive tool-result message for a skipped call — tells the model
    exactly what was wrong and how to fix it, so retries converge fast
    (cheap models especially need the raw-window and schema reminder)."""
    name = p["name"]
    hint = schema_hint(name)
    if p["kind"] == "truncated":
        msg = (
            f"Error: the arguments for `{name}` were cut off mid-stream after "
            f"{len(p['raw'])} characters. The call was NOT executed — no file "
            f"was written, nothing ran. Re-issue the COMPLETE call. {hint}"
        )
    elif p["kind"] == "unparseable":
        window = parse_error_context(p["raw"], p["pos"] or 0)
        msg = (
            f"Error: could not parse the arguments for `{name}` as JSON "
            f"({p['error']}). The problem is here: {window} — {hint}. Your "
            "malformed arguments were not preserved in the conversation; do "
            "not repeat them, emit fresh valid JSON."
        )
    else:  # invalid — valid JSON, wrong shape or missing field
        keys = sorted(p["args"].keys())
        msg = f"{p['error']} Keys present: {keys}. {hint}."
    if streak >= 2 and name == "write_file":
        msg += (
            f"\n\n(Consecutive failure #{streak} for write_file. Alternatives: "
            '1) emit the arguments as ONE single-line strict JSON object — a '
            'comma between "path" and "content", newlines in the content '
            "escaped as \\n; 2) write the file via run_bash with a quoted "
            "heredoc: cat > FILE <<'MESH_EOF_x7' … MESH_EOF_x7 — pick a "
            "delimiter string that does not appear in the content; 3) split "
            "the content into several smaller files.)"
        )
    return msg


def _stub_display(path: str) -> str:
    """Short path for quality-guard messages: relative to cwd, falling back
    to the basename (Windows raises on cross-drive relpath)."""
    try:
        return os.path.relpath(path)
    except (ValueError, OSError):
        return os.path.basename(path) or path


def _stub_fix_message(stub_files: dict) -> str:
    """Transient system message for the fix-it hop. Tool-name-free (naming
    tools in injected prose flips some models into XML tool-use mode),
    overrides start_server's end-the-turn instruction, and carries the
    intentional-placeholder escape."""
    listing = "\n".join(
        f"  {_stub_display(p)} — {ev[0]}" for p, ev in stub_files.items()
    )
    return (
        "[Quality check — do not end the turn yet. Files written this turn "
        "still contain placeholder markers instead of working code:\n"
        f"{listing}\n"
        "Replace every placeholder with the complete working implementation "
        "now — real logic, no TODO comments, no empty function bodies. "
        "Rewrite each file listed in full. It is fine to use tools again "
        "even if you were told the turn was over after starting a server — "
        "but do NOT restart the server; it is still running and will serve "
        "the updated files. If the user explicitly asked for placeholders "
        "or scaffolding, keep them, tell the user they are intentional, and "
        "end the turn.]"
    )


def _log_call_failure(state: dict, p: dict, repaired: bool) -> None:
    """Forensics record for a doomed/repaired call (best-effort). Raw args
    are preserved here even though history gets the sanitized form."""
    log_toolcall_failure({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": state.get("last_model") or state["cfg"]["model"],
        "tool": p["name"],
        "kind": p["kind"],
        "error": p["error"],
        "repaired": repaired,
        "raw_args": p["raw"],
    })


def handle_tool_calls(tool_calls: list, state: dict) -> dict:
    """Append assistant tool_calls message + tool result messages to state.

    Returns {"total": n, "doomed": k} so the caller's stall detector can
    tell an all-doomed hop (malformed args — counts toward the doom wall)
    from a productive one.

    The permission mode is read from state["mode"] PER CALL, not frozen for
    the batch — the keywatcher mutates it from its thread on shift+tab, so a
    cycle during a long tool run applies to the very next call. When it
    changes mid-batch we print the mode line so the switch is visible.

    Calls are classified by _prepare_call first; the assistant history
    message is built from SANITIZED arguments (see _prepare_call docstring)
    — this severs the doom loop where the model re-reads and repeats its own
    malformed JSON. Doomed kinds are skipped BEFORE the approval prompt with
    prescriptive feedback; repaired/normalized kinds go through the FULL
    approval + safety path. Invariants: the assistant message precedes all
    results; every tool_call id gets exactly one tool result.

    Every tool execution is exception-isolated: if a single tool call blows up
    (unexpected exception, terminal disconnect during approval, etc.), we log
    a clear error, feed it back to the model as the tool result, and move on
    to the next call. The session never crashes from a single bad call.
    """
    prepared = [_prepare_call(tc) for tc in tool_calls]
    state["messages"].append({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": p["id"],
                "type": "function",
                "function": {"name": p["name"], "arguments": p["history_args"]},
            }
            for p in prepared
        ],
    })
    # Index of the assistant tool_calls message just appended — write_file
    # content rides in it (never pruned by optimize), so it's the
    # content-bearing message for write-sourced dedupe entries.
    state["_batch_assistant_idx"] = len(state["messages"]) - 1
    shown_mode = state.get("mode")
    doomed_n = 0
    for p in prepared:
        _esc = state.get("esc_interrupt")
        if _esc is not None and _esc.is_set():
            # ESC pressed — abort between tool calls. The interrupt handler
            # seals the half-batch with stub results (one result per id) and
            # keeps completed work, same as ctrl+c.
            raise KeyboardInterrupt
        name, args = p["name"], p["args"]
        mode = state.get("mode", Mode.DEFAULT)  # live read — see docstring
        if mode is not shown_mode:
            statusbar.print_line(state)  # make the mid-batch switch visible
            shown_mode = mode

        # Doomed calls: skip before the approval prompt, feed the precise
        # reason back, and track a per-turn streak so repeated failures
        # escalate to escape-hatch guidance. Plan tools normalize their own
        # args in _handle_plan_tool, so they never take this branch.
        if p["kind"] in ("invalid", "truncated", "unparseable") and name not in PLAN_TOOLS:
            doomed_n += 1
            streaks = state.setdefault("doom_streak", {})
            streaks[name] = streaks.get(name, 0) + 1
            result = _doom_feedback(p, streaks[name])
            first_line = result.splitlines()[0]
            console.print(f"[yellow]  → skipped {name}: {_rich_escape(first_line)}[/yellow]")
            _log_call_failure(state, p, repaired=False)
            state["messages"].append({
                "role": "tool",
                "tool_call_id": p["id"],
                "content": result,
            })
            continue

        if p["kind"] == "repaired":
            console.print("[yellow]⚠ repaired malformed tool arguments (missing comma)[/yellow]")
            _log_call_failure(state, p, repaired=True)
        elif p["kind"] == "normalized":
            console.print("[dim]  normalized control characters in tool arguments[/dim]")
            _log_call_failure(state, p, repaired=True)

        try:
            if name in PLAN_TOOLS:
                # Plan tools are bookkeeping — no filesystem or shell side
                # effects, so we don't gate them on the approval prompt.
                result = _handle_plan_tool(name, args, state)
            elif name in INTERACTIVE_TOOLS:
                # The user answers it in person — an approval prompt on top
                # of a question prompt would be nonsense.
                result = _handle_ask_user(args, state)
            elif name == "remember":
                # Memory bookkeeping — writes only to ~/.meshapi/context/,
                # never the user's repo; ungated like plan tools. Doomed
                # (empty-note) calls were already skipped pre-approval.
                result = memory.append_note(
                    state["memory_root"], args.get("note") or ""
                )
                console.print(f"[{BRAND_DIM}]⚙ {summarize_call(name, args)}[/{BRAND_DIM}]")
            else:
                state.setdefault("doom_streak", {}).pop(name, None)  # reached execution — streak broken
                # Per-mode auto-approval: each Mode declares which tool names
                # bypass the y/n prompt. Anything not in the set, OR anything
                # that fails a safety check, falls back to confirmation —
                # even in BYPASS we ask before truly dangerous shapes
                # (sensitive paths, `rm -rf /`, sudo, curl | sh, ...).
                # Auto-approve via the mode's set OR the session allowlist
                # (tools the user answered "a — always this session" for).
                # Safety guards below still run on both paths and can
                # downgrade back to the y/n prompt. CRITICAL: the guards
                # no-op in DEFAULT (they assume the caller confirms), so a
                # session-allowed tool must be safety-checked at AUTO
                # strictness or `a` in DEFAULT would disarm them entirely.
                auto_approved = name in AUTO_APPROVE.get(mode, set())
                safety_mode = mode
                if not auto_approved and name in state.get("session_allow", set()):
                    auto_approved = True
                    safety_mode = Mode.AUTO
                safety_reason: str = ""
                if auto_approved and name == "write_file":
                    ok, reason = safety.is_path_safe_for_auto_write(
                        args.get("path"), safety_mode
                    )
                    if not ok:
                        auto_approved = False
                        safety_reason = reason or "path safety check failed"
                elif auto_approved and name == "read_file":
                    # BYPASS auto-approves reads; we still block sensitive
                    # paths so the model can't silently leak ~/.ssh/...
                    # to the upstream provider.
                    ok, reason = safety.is_path_safe_for_auto_read(
                        args.get("path"), safety_mode
                    )
                    if not ok:
                        auto_approved = False
                        safety_reason = reason or "path safety check failed"
                elif auto_approved and name in ("run_bash", "start_server"):
                    ok, reason = safety.is_command_safe_for_auto(
                        args.get("command"), safety_mode
                    )
                    if not ok:
                        auto_approved = False
                        safety_reason = reason or "command safety check failed"
                if not auto_approved and safety_reason:
                    console.print(
                        f"[yellow]⚠ auto-approval blocked: {safety_reason}[/yellow]"
                    )
                approved = auto_approved or confirm_tool_call(
                    name, args,
                    watcher=state.get("watcher"),
                    session_allow=state.setdefault("session_allow", set()),
                )
                if approved:
                    # 1) Action header — purple, "the AI is doing this"
                    console.print(f"[{BRAND_DIM}]⚙ {summarize_call(name, args)}[/{BRAND_DIM}]")
                    # 2) Body BEFORE execution — cyan, "this is the actual
                    #    code/command being run". For write_file we have the
                    #    full content up front; for run_bash we have the
                    #    command (output prints after exec).
                    if name == "start_server":
                        # Header is enough; _handle_start_server prints command,
                        # readiness, URL, and a short output preview itself.
                        result = _handle_start_server(args, state)
                        if result.startswith("Error:"):
                            console.print(f"  [red]✗ {_rich_escape(result)}[/red]")
                    elif name == "write_file":
                        _print_file_diff(args.get("path") or "", args.get("content") or "")
                        result = exec_tool(name, args, state["cfg"])
                        _render_tool_result(name, args, result)
                        # Quality guard: scan the freshly written content for
                        # stub markers. Re-scan on every write so a clean
                        # rewrite CLEARS its entry. Best-effort — a guard bug
                        # must never break a write.
                        if not result.startswith("Error:"):
                            try:
                                key = str(Path(args.get("path") or "").expanduser().resolve())
                                ev = find_stub_markers(key, args.get("content") or "")
                                stubs = state.setdefault("stub_files", {})
                                if ev:
                                    stubs[key] = ev
                                else:
                                    stubs.pop(key, None)
                            except Exception:
                                pass
                            # Repo memory: capture structure (zero extra
                            # tokens — content in hand) + record for
                            # read-after-write dedupe. Best-effort.
                            try:
                                memory.capture(
                                    state["memory_root"],
                                    args.get("path") or "",
                                    args.get("content") or "",
                                )
                                memory.record_write(
                                    state, args.get("path") or "",
                                    args.get("content") or "",
                                    msg_index=state.get("_batch_assistant_idx", 0),
                                )
                            except Exception:
                                pass
                    elif name == "run_bash":
                        _print_shell_command(args.get("command") or "")
                        result = run_with_ticker(
                            "running", lambda: exec_tool(name, args, state["cfg"]), state
                        )
                        _render_tool_result(name, args, result)
                    elif name == "read_file":
                        # Dedupe AFTER the approval gate (DEFAULT still
                        # confirms; only the result differs): if this exact
                        # content is provably in context, answer with a
                        # short stub instead of re-sending the body.
                        stub = None
                        try:
                            stub = memory.dedupe_read(
                                state, args.get("path") or "",
                                float(state["cfg"].get("optimize") or 0),
                            )
                        except Exception:
                            stub = None
                        if stub is not None:
                            result = stub
                            console.print(
                                "  [green]→[/green] [dim]unchanged — content "
                                "already in context (skipped re-send)[/dim]"
                            )
                        else:
                            result = exec_tool(name, args, state["cfg"])
                            _render_tool_result(name, args, result)
                            if not result.startswith("Error:"):
                                try:
                                    # len(messages) == the index this tool
                                    # result will occupy when appended below.
                                    memory.record_read(
                                        state, args.get("path") or "", result,
                                        msg_index=len(state["messages"]),
                                    )
                                    memory.capture(
                                        state["memory_root"],
                                        args.get("path") or "", result,
                                    )
                                except Exception:
                                    pass
                    else:
                        result = run_with_ticker(
                            name, lambda: exec_tool(name, args, state["cfg"]), state
                        )
                        _render_tool_result(name, args, result)
                else:
                    result = "User denied this tool call."
                    console.print("[dim]  → denied by user[/dim]")
        except KeyboardInterrupt:
            # Bubble up so the outer loop can abort the whole turn.
            raise
        except Exception as e:  # pragma: no cover — safety net for unknown bugs
            result = f"Error: tool execution raised {type(e).__name__}: {e}"
            console.print(f"[red]  → {result}[/red]")

        state["messages"].append({
            "role": "tool",
            "tool_call_id": p["id"],
            "content": result,
        })
    # Once per batch — keep the mode visible between hops in multi-step turns.
    statusbar.print_line(state)
    return {"total": len(prepared), "doomed": doomed_n}


def _turn_status_line(model: str, auto_routed: bool, prompt_t, completion_t,
                      agg_cost: float, session_cost: float, elapsed: float,
                      estimated: bool = False) -> str:
    """The dim per-turn summary. Cost segments are omitted when neither the
    gateway nor the catalog could price the turn — no dangling '—'. A "~"
    marks a client-computed cost (usage × catalog rates), since the gateway
    returns no cost of its own. When auto-routed, show the picked model."""
    display_model = f"auto → {model}" if auto_routed else model
    segments = [display_model, f"{prompt_t}→{completion_t} tok"]
    tilde = "~" if estimated else ""
    if agg_cost:
        segments.append(f"{tilde}{fmt_usd(agg_cost)}")
    if session_cost:
        segments.append(f"session {tilde}{fmt_usd(session_cost)}")
    segments.append(f"{elapsed:.1f}s")
    return "  •  ".join(segments)


def main() -> None:
    args = parse_args()

    # `meshapi upgrade`: upgrade in place and exit, no key or REPL needed.
    # run_upgrade() reuses detect_upgrade_command() so this resolves to the
    # same pipx/uv/pip command the in-REPL /update uses, and prints the
    # "exit first" guidance on Windows (the .exe is file-locked).
    if getattr(args, "command", None) == "upgrade":
        ok = run_upgrade()
        if ok:
            console.print(
                "[green]✓ meshapi upgraded — relaunch to use the new "
                "version.[/green]"
            )
        sys.exit(0 if ok else 1)

    cfg = load_config()

    # Kick off the PyPI version check immediately (daemon thread, never
    # blocks, needs no API key). The thread only writes into update_state;
    # maybe_offer() consumes it at safe points — after the banner and at the
    # top of each prompt-loop turn — so the y/n offer can never collide with
    # prompt_toolkit or a streaming response.
    _update_cache = load_update_check()
    update_state = {
        "latest": _update_cache.get("latest"),
        "declined": _update_cache.get("declined_version"),
        "done": threading.Event(),
        "prompted": False,
    }
    start_background_check(update_state)

    if args.model:
        cfg["model"] = args.model
    if args.route in ("auto", "off"):
        cfg["auto_route"] = args.route == "auto"
    elif args.route == "preview":
        # Flag/command parity: /route preview needs a conversation to
        # preview, which doesn't exist at launch — explain instead of
        # rejecting the flag (external tester report).
        console.print(
            "[dim]--route preview: nothing to preview at launch — send a "
            "message first, then use /route preview in-session.[/dim]"
        )

    if not cfg["api_key"]:
        # First run (or key removed): walk the user through connecting a key
        # instead of bouncing them to docs. Non-interactive stdin (CI, pipes)
        # can't prompt, so keep the hard error there.
        if sys.stdin.isatty():
            if not prompt_for_api_key(cfg):
                sys.exit(1)
        else:
            console.print(
                "[red]No API key found. Set the MESHAPI_API_KEY env var, or "
                "run meshapi in a terminal to be prompted (the key is saved "
                f"to {CREDENTIALS_FILE}).[/red]"
            )
            sys.exit(1)

    state = {
        "cfg": cfg,
        "messages": [{"role": "system", "content": build_system_prompt(cfg)}],
        "session_cost": 0.0,
        "mode": from_str(args.mode),
        "plan": None,    # populated by the model via create_plan
        "servers": [],   # background processes spawned via start_server
        "pending_attachments": [],  # list of {"part","size_bytes","name"}
        # Cumulative bytes of attachments already sent to the model.
        # Enforces safety.SESSION_IMAGE_BYTE_CAP across the whole session.
        "session_image_bytes": 0,
        "update": update_state,  # background PyPI check (see maybe_offer)
        "doom_streak": {},       # per-turn consecutive doomed-call counter
        "last_model": cfg["model"],  # resolved model of the last stream (forensics)
        "session_allow": set(),  # tools approved with "a — always this session"
        # Quality guard (all reset per user turn): flagged writes, one-hop
        # bound, transient fix-it message, per-turn suppression.
        "stub_files": {},
        "quality_hop_fired": False,
        "quality_fix_msg": None,
        "stub_guard_off": False,
        # Always-visible input: messages stacked mid-run (FIFO, one full
        # turn each), esc-abort signal, and whether a rich.Live owns the
        # screen (watcher thread must not print then).
        "input_queue": collections.deque(),
        "esc_interrupt": threading.Event(),
        "live_active": False,
        # Repo memory: per-session read/write tracking for dedupe, and the
        # repo root frozen at startup (nothing chdirs in-process).
        "session_reads": {},
        "memory_root": Path.cwd().resolve(),
        # Session id: names the transcript that compaction points the model
        # at, so compacted-away detail stays recoverable rather than lost.
        "session_id": time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}",
        # Models observed rejecting reasoning_effort — seeded from config so
        # a new session doesn't re-pay the rejected call every launch.
        "_reasoning_rejected": set(cfg.get("reasoning_rejected_models") or []),
    }

    # Mode cycle — used by both the prompt-toolkit keybinding (while at the
    # prompt) and the keywatcher (while the model is streaming or executing).
    # The change is silent; user sees the new mode on the next `print_line`
    # (above the next prompt or after the next tool batch).
    def _cycle_mode() -> None:
        state["mode"] = next_mode(state["mode"])

    kb = KeyBindings()

    @kb.add("s-tab")  # Shift+Tab while at the prompt
    def _(event):
        _cycle_mode()
        event.app.invalidate()

    # Prompt is just the "› " marker. The mode indicator is rendered by
    # statusbar.print_line ABOVE the cwd separator each turn (matches the
    # user's mockup — no extra indicator on the input line). Trade-off:
    # shift+tab during typing still cycles the mode internally, but the
    # repainted line is only visible at the next prompt or after the next
    # tool batch (handle_tool_calls also fires statusbar.print_line).
    def prompt_message():
        return FormattedText([("class:prompt", "› ")])

    def _queue_input(text: str) -> None:
        """Watcher-thread callback: Enter was pressed mid-run. Queue the
        message; when no Live owns the screen (tool exec), acknowledge with
        a one-shot dim line (rich Console holds an RLock — safe here)."""
        state["input_queue"].append(text)
        if not state.get("live_active"):
            try:
                ack = Text()
                ack.append("  › ", style=BRAND_DIM)
                ack.append(text if len(text) <= 60 else text[:60] + "…")
                ack.append(
                    "  (applies next hop)" if is_live_control(text) else "  (queued)",
                    style="dim",
                )
                console.print(ack)
            except Exception:
                pass

    def _request_interrupt() -> None:
        """Watcher-thread callback: bare ESC. Signal the main thread; it
        aborts between deltas/hops/tool calls (never mid-syscall)."""
        state["esc_interrupt"].set()

    # Out-of-prompt key watcher: shift+tab cycles the mode, typed text
    # accumulates as type-ahead (rendered by the live footer), Enter queues,
    # ESC requests an abort. Paused while prompt_toolkit owns stdin.
    watcher = KeyWatcher(
        on_shift_tab=_cycle_mode,
        on_submit=_queue_input,
        on_esc=_request_interrupt,
    )
    state["watcher"] = watcher  # so confirm_tool_call can pause around y/n input

    # Touch the history file with 0600 up front so prompt_toolkit doesn't
    # create it world-readable on first write.
    HISTORY_FILE.touch(mode=0o600, exist_ok=True)
    secure_file(HISTORY_FILE)
    from .completer import SlashCompleter
    session = PromptSession(
        history=ScrubbedFileHistory(str(HISTORY_FILE)),
        key_bindings=kb,
        # Fuzzy completion for slash commands + their args ("/model qw" →
        # every qwen model). Threaded so the one-time catalog fetch inside
        # the completer never blocks a keystroke; non-slash text yields
        # nothing, so normal prompts never see a menu.
        completer=ThreadedCompleter(SlashCompleter(state)),
        complete_while_typing=True,
    )

    render_banner(cfg)
    _adopt_orphaned_servers(state)
    # Update offer, consume point 1: stdin is still canonical here (no
    # watcher, no prompt_toolkit), so a plain y/n input is safe. Fires when
    # the cache already knew a newer version or the check landed fast.
    maybe_offer(update_state, watcher=None)
    watcher.start()  # captures shift+tab whenever prompt_toolkit isn't reading

    # Make sure backgrounded servers die with us — even if Python exits via
    # an unhandled exception, SIGTERM, or hangup. atexit covers normal paths;
    # the signal handlers cover the rest.
    import atexit as _atexit
    _atexit.register(_shutdown_servers, state)

    def _signal_shutdown(signum, frame):  # noqa: ARG001
        _shutdown_servers(state)
        # Re-raise the default signal behavior to actually exit.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    # SIGHUP is POSIX-only — referencing signal.SIGHUP on Windows raises
    # AttributeError, so build the list conditionally instead of unconditionally.
    _signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        _signals.append(signal.SIGHUP)
    for _sig in _signals:
        try:
            signal.signal(_sig, _signal_shutdown)
        except (ValueError, OSError):
            pass

    while True:
        try:
            # Prune server records whose process died out-of-band (e.g. the
            # model pkill'd it) so the toolbar never advertises dead URLs.
            # Once per turn, POSIX signal-0 probe; anything but "no such
            # process" keeps the entry (permission errors mean it's alive).
            _live = []
            for _srv in state.get("servers", []):
                try:
                    os.kill(_srv["pid"], 0)
                    _live.append(_srv)
                except ProcessLookupError:
                    pass
                except Exception:
                    _live.append(_srv)
            if len(_live) != len(state.get("servers", [])):
                state["servers"] = _live
                _persist_servers(state)
            # Queue drain: messages the user stacked mid-run submit in order,
            # each as its own full turn, BEFORE the interactive prompt shows.
            # New type-ahead during drained turns keeps queueing (FIFO).
            queued = state["input_queue"].popleft() if state["input_queue"] else None
            if queued is not None:
                _print_input_frame(queued)
                try:
                    session.history.append_string(queued)  # up-arrow recall parity
                except Exception:
                    pass
                user_input = queued
            else:
                # Update offer, consume point 2: if the background check landed
                # after startup, surface it between turns — never mid-prompt.
                maybe_offer(update_state, watcher=watcher)
                # cwd separator above the input box. The mode indicator is no
                # longer printed here — it lives in the bottom_toolbar below the
                # prompt, which prompt_toolkit repaints live on shift+tab:
                #   ──────────────────────────────────────── cli_for_meshapi_v1
                #   › ...
                #   ⏵⏵ bypass permissions on  (shift+tab to cycle) · esc to interrupt
                _cwd_rule()
                with watcher.paused():
                    # Hand stdin off to prompt_toolkit (canonical-mode termios).
                    # The prompt itself is just "› "; the mode indicator is the
                    # bottom_toolbar, repainted live by the s-tab binding's
                    # event.app.invalidate(). "noreverse" kills prompt_toolkit's
                    # default inverted bar so the toggle reads as plain text.
                    # Un-submitted type-ahead from the last run prefills the
                    # buffer (take_typeahead is race-free under paused()).
                    user_input = session.prompt(
                        prompt_message,
                        default=watcher.take_typeahead(),
                        bottom_toolbar=lambda: statusbar.bottom_toolbar(state),
                        style=Style.from_dict({
                            "prompt": f"bold fg:{BRAND} bg:{BRAND_BG}",
                            "": f"fg:{BRAND_BG_FG} bg:{BRAND_BG}",
                            "bottom-toolbar": "noreverse bg:default",
                        }),
                    )
                console.rule(style=BRAND_DIM, characters="─")
                console.print()  # bottom padding under the input box per the mockup
        except (KeyboardInterrupt, EOFError):
            _shutdown_servers(state)
            watcher.stop()
            console.print("\n[dim]bye[/dim]")
            break

        # Strip outer whitespace BEFORE slash detection — a pasted
        # " /memory clear" must be a command, not a chat message (seen live).
        user_input = user_input.strip()
        if not user_input:
            continue
        _bare = _bare_command(user_input)
        if _bare:
            console.print(
                f"[dim]→ {_bare}  (a lone command name runs the command; "
                "add more words to send it as a prompt)[/dim]"
            )
            user_input = _bare
        if user_input.startswith("/"):
            # Exception isolation for the command path — the tool loop has
            # had it forever; commands didn't, so ONE handler bug could
            # exit the whole REPL (seen in the wild: `/file` with no arg).
            try:
                if not handle_command(user_input, state):
                    break
            except (KeyboardInterrupt, EOFError):
                raise  # ctrl+c at an inner prompt (/login) keeps its meaning
            except Exception as e:
                console.print(
                    f"[red]Command error: {type(e).__name__}: {e} — "
                    "the session is still alive.[/red]"
                )
            continue

        # Auto-detect image paths/URLs in the prompt and attach them. The
        # detector is liberal — drag-dropped paths (often quoted), bare
        # filenames that exist in cwd, and URLs all work. Each match comes
        # back as (raw_token, normalized): we replace `raw_token` in the
        # original text (so wrapping quotes go too) with `[Image #N]`.
        auto_text = user_input
        auto_attachments: list = []  # list of {"part","size_bytes","name"}
        queued = state.get("pending_attachments") or []
        n_offset = len(queued)
        for raw_token, source in find_image_tokens(user_input):
            if raw_token not in auto_text:
                continue  # already replaced (duplicate mention in same prompt)
            try:
                part, info = load_image(source)
            except AttachmentError as e:
                console.print(f"[yellow]Couldn't auto-attach {source}: {e}[/yellow]")
                continue
            # Session-cap check: refuse attachments that would push us past
            # the cumulative budget. Already-sent + queued + this one.
            ok, reason = _check_image_cap(
                state,
                info["size_bytes"]
                + sum(int(a.get("size_bytes", 0)) for a in auto_attachments),
            )
            if not ok:
                console.print(
                    f"[red]Skipping {info['name']}: {reason}[/red]"
                )
                continue
            n = n_offset + len(auto_attachments) + 1
            auto_text = auto_text.replace(raw_token, f"[Image #{n}]")
            auto_attachments.append({
                "part": part,
                "size_bytes": info["size_bytes"],
                "name": info["name"],
            })
            size_kb = max(1, info["size_bytes"] // 1024)
            console.print(
                f"[{CODE}]📎 attached {info['name']} ({size_kb} KB, {info['mime']})[/{CODE}]"
            )

        all_attachments = queued + auto_attachments
        if all_attachments:
            console.print(
                f"[dim]→ sending {len(all_attachments)} image(s) with this prompt[/dim]"
            )
            parts = [{"type": "text", "text": auto_text}] + [
                a["part"] for a in all_attachments
            ]
            state["messages"].append({"role": "user", "content": parts})
            # Move the queued + auto bytes from "pending" to "sent" and clear
            # the queue. session_image_bytes is what's enforced going forward.
            state["session_image_bytes"] = state.get("session_image_bytes", 0) + sum(
                int(a.get("size_bytes", 0)) for a in all_attachments
            )
            state["pending_attachments"] = []
        else:
            state["messages"].append({"role": "user", "content": user_input})
        _smart_route_turn(state, user_input)
        console.print()

        # Tool-calling loop: keep streaming until the model returns text
        # without tool_calls, the user's optional /hops limit fires, or the
        # stall detector calls a genuinely stuck loop. There is no arbitrary
        # hop cap — long tasks run to completion (esc interrupts).
        state["doom_streak"] = {}  # fresh user turn — failure streaks reset
        state["stall"] = loopguard.StallDetector()
        state["stall_nudge_msg"] = None
        state["_max_tokens_shrunk"] = None  # per-turn output-budget override
        turn_request_ids: list = []          # for the authoritative cost lookup
        turn_started_iso = _utc_now_iso()
        state["_compact_exhausted"] = False  # re-arm compaction each user turn
        state["_drop_reasoning"] = False      # re-test reasoning support each turn
        # Quality guard resets: new turn, new deliverables. Suppressed for
        # the whole turn when the user explicitly asked for scaffolding.
        state["stub_files"] = {}
        state["quality_hop_fired"] = False
        state["quality_fix_msg"] = None
        state["stub_guard_off"] = stub_guard_suppressed(user_input)
        state["esc_interrupt"].clear()  # stale abort must not kill this turn
        agg_cost = 0.0
        last_model = state["cfg"]["model"]
        last_usage: dict = {}
        last_optimize_plan = {}
        last_elapsed = 0.0
        turn_failed = False  # empty/errored response — skip the cosmetic cost line
        try:
            hopped = 0
            _hop_limit = int(state["cfg"].get("max_hops") or 0)  # 0 = unlimited
            _turn_started = time.monotonic()
            while True:
                if state["esc_interrupt"].is_set():
                    raise KeyboardInterrupt  # ESC pressed — abort between hops
                # Mid-run steering: /model, /reasoning, /mode … typed while
                # the turn is running apply to the NEXT hop instead of
                # waiting for the turn to end.
                _drain_live_controls(state)
                if _hop_limit and hopped >= _hop_limit:
                    # User-configured checkpoint (/hops) — a pause, not a
                    # failure: history is kept and breadcrumbed for resume.
                    console.print(
                        f"[yellow]Paused after {hopped} tool hops (your /hops "
                        "limit). Work so far is saved — say 'continue' to "
                        "resume, or /hops off for unlimited.[/yellow]"
                    )
                    _pause_breadcrumb(state, hopped)
                    break
                hopped += 1

                # Auto-compaction: keep history under the model's context
                # limit so a long turn never dies on "prompt is too long".
                # Hop top sits between complete tool batches — the only safe
                # place to mutate history.
                if state["cfg"].get("auto_compact", True):
                    _est = compact.est_history_tokens(state["messages"])
                    if _est > 48_000 and not state.get("models_cache"):
                        # Bounded: fetch_models_quiet does NOT cache failures,
                        # and a 10s catalog stall per hop would be worse than
                        # falling back to the 128k default limit.
                        if state.get("_models_fetch_attempts", 0) < 3:
                            state["_models_fetch_attempts"] = (
                                state.get("_models_fetch_attempts", 0) + 1
                            )
                            fetch_models_quiet(state)  # silent, session-cached
                    _mfl = (
                        (state.get("last_model") or state["cfg"]["model"])
                        if state["cfg"].get("auto_route") else state["cfg"]["model"]
                    )
                    state["_ctx_limit"] = compact.context_limit(
                        _mfl, state.get("models_cache")
                    )
                    if (compact.should_compact(state["messages"], state["_ctx_limit"])
                            and not state.get("_compact_exhausted")):
                        _rep = compact.compact_history(
                            state, limit=state["_ctx_limit"]
                        )
                        if _rep:
                            console.print(
                                "[dim]⚙ compacted context: "
                                f"~{_rep['before_tok'] // 1000}k → "
                                f"~{_rep['after_tok'] // 1000}k tok (est)"
                                f"{' · transcript kept' if state.get('_transcript_upto') else ''}"
                                "[/dim]"
                            )
                            # Still over after compacting means there is
                            # nothing left to squeeze — stop trying every
                            # hop (compaction thrash burns time and can
                            # loop). The gateway's own error path takes
                            # over if it genuinely doesn't fit.
                            if compact.should_compact(
                                state["messages"], state["_ctx_limit"]
                            ):
                                state["_compact_exhausted"] = True
                        else:
                            state["_compact_exhausted"] = True

                # Periodic heartbeat for long turns — hops, context size,
                # elapsed, spend (when the gateway reports cost).
                if hopped % 10 == 0:
                    _est = compact.est_history_tokens(state["messages"])
                    _mins, _secs = divmod(int(time.monotonic() - _turn_started), 60)
                    _lim = state.get("_ctx_limit")
                    _ctx_seg = (
                        f"~{_est // 1000}k/{_lim // 1000}k tok (est)"
                        if _lim else f"~{_est // 1000}k tok (est)"
                    )
                    _cost_seg = f" · {fmt_usd(agg_cost)}" if agg_cost else ""
                    console.print(
                        f"[dim]— hop {hopped} · {_ctx_seg} · "
                        f"{_mins}m{_secs:02d}s{_cost_seg} —[/dim]"
                    )

                # Re-ground the model in the current plan state on every hop.
                # The plan lives client-side; without this the model has to
                # reconstruct "what's left" from buried tool history and tends
                # to stop early or falsely claim completion. Injected
                # transiently (not persisted) so it always reflects live state
                # and history stays clean.
                _extras = []
                _plan = state.get("plan")
                if _plan is not None and not _plan.is_complete():
                    _extras.append({
                        "role": "system",
                        "content": (
                            f"[Active plan {_plan.summary()}. Steps still "
                            f"remaining:\n{_plan.reminder_text()}\n"
                            "Keep working through these now. Do NOT tell the "
                            "user the task is complete, and do not treat "
                            "starting a server as the final step, until every "
                            "step above is done. If a step is genuinely "
                            "impossible, mark it blocked and say why.]"
                        ),
                    })
                # Quality-guard fix-it message: transient, consume-once, and
                # LAST (recency dominates for cheap models). A persistent
                # copy would go stale in history the moment the rewrite
                # lands — mirror of the plan-reminder pattern above.
                _fix = state.pop("quality_fix_msg", None)
                if _fix:
                    _extras.append({"role": "system", "content": _fix})
                # Stall nudge: consume-once and LAST — recency dominates for
                # cheap models, and a repeated loop needs the freshest voice.
                _nudge = state.pop("stall_nudge_msg", None)
                if _nudge:
                    _extras.append({"role": "system", "content": _nudge})
                if state.get("_smart_pick") and state["cfg"].get("route_mode") == "smart":
                    _hdr = f"smart → {state['_smart_pick']}"
                elif state["cfg"].get("auto_route"):
                    _hdr = "auto"
                else:
                    _hdr = state["cfg"]["model"]
                if hopped > 1:
                    _hdr += f" · hop {hopped}"
                if agg_cost:
                    _hdr += f" · {fmt_usd(agg_cost)}"
                reply, meta = _stream_hop_with_retry(state, _extras, _hdr)
                cost = meta.get("cost")
                if cost is not None:
                    try:
                        agg_cost += float(cost)
                    except (TypeError, ValueError):
                        cost = None
                if cost is None:
                    # The gateway does not return `cost` (verified live) —
                    # compute it from usage × the catalog's own per-1M rates
                    # so the spend line isn't permanently blank.
                    _u = meta.get("usage")
                    if _u:
                        if not state.get("models_cache"):
                            fetch_models_quiet(state)
                        _est = pricing.estimate_cost(
                            meta.get("model") or last_model,
                            _u, state.get("models_cache"),
                        )
                        if _est is not None:
                            agg_cost += _est
                            state["cost_estimated"] = True
                last_model = meta.get("model") or last_model
                state["last_model"] = last_model
                # SSE lines the client couldn't parse were dropped — if that
                # coincides with broken tool args, the gateway relay (not the
                # model) is the culprit. Surface + log for attribution.
                if meta.get("dropped_chunks"):
                    console.print(
                        f"[dim]⚠ {meta['dropped_chunks']} unparseable SSE "
                        "chunk(s) dropped this stream (logged)[/dim]"
                    )
                    log_toolcall_failure({
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "model": last_model,
                        "tool": None,
                        "kind": "sse_dropped_chunks",
                        "error": f"{meta['dropped_chunks']} chunks",
                        "repaired": False,
                        "raw_args": meta.get("dropped_sample", ""),
                    })
                turn_request_ids.extend(meta.get("request_ids") or [])
                last_usage = meta.get("usage") or last_usage
                last_elapsed += meta.get("elapsed", 0.0)
                last_optimize_plan = meta.get("optimize_plan") or last_optimize_plan

                tool_calls = meta.get("tool_calls") or []
                _err = meta.get("error")
                if _err or (not tool_calls and not (reply or "").strip()):
                    # Errored (even if tool-call deltas ALSO streamed — don't
                    # execute likely-broken tools) OR an empty final reply.
                    # Appending an empty assistant message would poison EVERY
                    # later turn (the backend rejects empty text blocks with 200
                    # + an in-band ValidationException — the "?→? tok" hang), so
                    # surface it and end the turn WITHOUT polluting history. The
                    # user's message stays; consecutive user messages are
                    # accepted, so they can resend or say "continue".
                    turn_failed = True
                    console.rule(style="dim yellow", characters="─")
                    if _err:
                        console.print(
                            "[yellow]⚠ The gateway returned an error instead of a "
                            "reply:[/yellow]"
                        )
                        console.print(f"[dim]  {_rich_escape(str(_err))}[/dim]")
                    else:
                        console.print(
                            "[yellow]⚠ The model returned an empty response.[/yellow]"
                        )
                    console.print(
                        "[dim]  Work completed earlier this turn is saved — say "
                        "'continue' to pick up from there. If it keeps "
                        "happening: /compact now to shrink history, /clear to "
                        "reset, or /model to switch.[/dim]"
                    )
                    break
                if not tool_calls:
                    state["messages"].append({"role": "assistant", "content": reply})
                    # Quality guard: the model ended its turn but files it
                    # wrote still carry stub markers. Spend ONE fix-it hop
                    # with concrete evidence — cheap models respond to
                    # "script.js line 3 says 'Add game logic here'" far
                    # better than to generic scolding. Bounded: once per
                    # turn, never past the user's /hops limit, suppressed
                    # when the user asked for scaffolding.
                    if (state.get("stub_files") and not state.get("quality_hop_fired")
                            and not state.get("stub_guard_off")
                            and (not _hop_limit or hopped < _hop_limit)):
                        state["quality_hop_fired"] = True
                        state["quality_fix_msg"] = _stub_fix_message(state["stub_files"])
                        _p0, _ev0 = next(iter(state["stub_files"].items()))
                        _more = (
                            f" — and {len(state['stub_files']) - 1} more file(s)"
                            if len(state["stub_files"]) > 1 else ""
                        )
                        console.print(
                            f"[yellow]⚙ quality check: {_stub_display(_p0)} looks "
                            f"incomplete ({_rich_escape(_ev0[0])}){_more} — asking "
                            "the model to finish it[/yellow]"
                        )
                        continue
                    # Flag premature completion: the model ended its turn with
                    # plan steps still open. Surfaces the gap to the user (and
                    # the breadcrumb above keeps it in context for "continue").
                    _plan = state.get("plan")
                    if _plan is not None and not _plan.is_complete():
                        _inc = _plan.incomplete()
                        console.print(
                            f"[yellow]⚠ ended its turn with {len(_inc)} plan "
                            f"step(s) not completed:[/yellow]"
                        )
                        for _i, _s in _inc:
                            console.print(f"[yellow]    {_i}. {_s.title}[/yellow]")
                        console.print(
                            "[dim]  If it stopped early, tell it to continue.[/dim]"
                        )
                    break

                # Model called tools — execute and loop.
                _report = handle_tool_calls(tool_calls, state)
                _action = state["stall"].observe(
                    loopguard.batch_signature(tool_calls),
                    all_doomed=(
                        bool(_report["total"])
                        and _report["doomed"] == _report["total"]
                    ),
                )
                if _action in ("nudge", "renudge"):
                    state["stall_nudge_msg"] = (
                        _STALL_NUDGE if _action == "nudge" else _STALL_RENUDGE
                    )
                    console.print(
                        "[yellow]⚙ loop check: same action repeated "
                        f"{state['stall'].last_cycles}× — nudging the model to "
                        "change approach[/yellow]"
                    )
                elif _action == "stop":
                    if state["cfg"].get("stall_policy") == "keep-going":
                        # Unattended mode: never pause — keep nudging. The
                        # user watches spend; esc interrupts.
                        state["stall_nudge_msg"] = _STALL_RENUDGE
                        console.print(
                            "[yellow]⚙ stall persists — continuing per "
                            "/stall keep-going (esc to interrupt)[/yellow]"
                        )
                    else:
                        if state["stall"].stop_reason == "doom":
                            console.print(
                                "[yellow]Paused: the model produced malformed "
                                "tool calls for "
                                f"{loopguard.DOOM_STOP_HOPS} straight rounds. "
                                "Work so far is saved — try /model to switch "
                                "models, then say 'continue'.[/yellow]"
                            )
                        else:
                            console.print(
                                "[yellow]Paused: the model repeated the same "
                                "action "
                                f"{state['stall'].last_cycles} times in a row "
                                "despite reminders. Work so far is saved — say "
                                "'continue', or rephrase to unblock it. "
                                "(/stall keep-going to never pause)[/yellow]"
                            )
                        _pause_breadcrumb(state, hopped)
                        break

            # Quality guard, final honesty: the turn is over and flagged
            # files survived (fix-it hop included, or a pause preempted it).
            # Post-loop so ALL break paths land here; exception paths
            # skip it. Warn the user plainly + leave a breadcrumb so a
            # follow-up "implement fully" gives the model concrete targets.
            _stubs = state.get("stub_files") or {}
            if _stubs and not state.get("stub_guard_off"):
                console.print(
                    f"[yellow]⚠ quality check: {len(_stubs)} file(s) still "
                    "look incomplete:[/yellow]"
                )
                for _p, _ev in _stubs.items():
                    console.print(f"[yellow]    {_stub_display(_p)} — {_rich_escape(_ev[0])}[/yellow]")
                _tips = ["/model anthropic/claude-sonnet-4.5"]
                if not state["cfg"].get("auto_route"):
                    _tips.append("/route auto")
                console.print(
                    "[dim]  Cheaper models often deliver skeletons. Try "
                    + " or ".join(_tips)
                    + ", or reply 'implement the full logic, no placeholders'. "
                    "If placeholders were intentional, ignore this.[/dim]"
                )
                state["messages"].append({"role": "system", "content": (
                    "[The turn ended with files still containing placeholder "
                    "markers: "
                    + "; ".join(f"{_stub_display(p)} ({ev[0]})" for p, ev in _stubs.items())
                    + ". If the user asks to continue or to implement fully, "
                    "rewrite these files with complete working code — do not "
                    "claim they are done.]"
                )})

            # Replace the computed estimate with the gateway's own billed
            # figure when it can be fetched (one request, ids filtered to
            # this turn). Falls back to the estimate on any failure.
            if turn_request_ids and agg_cost:
                _actual = pricing.fetch_actual_costs(
                    state["cfg"], turn_request_ids, turn_started_iso
                )
                if _actual and len(_actual) == len(set(turn_request_ids)):
                    agg_cost = sum(_actual.values())
                    state["cost_estimated"] = False

            prompt_t = last_usage.get("prompt_tokens", "?")
            completion_t = last_usage.get("completion_tokens", "?")
            if not turn_failed:
                console.rule(style=BRAND_DIM, characters="─")
                # session_cost is committed in the finally below — add this
                # turn's spend here so the line shows the post-turn total.
                console.print(
                    f"[dim]{_turn_status_line(last_model, state['cfg'].get('auto_route', False), prompt_t, completion_t, agg_cost, state['session_cost'] + agg_cost, last_elapsed, state.get('cost_estimated', False))}[/dim]"
                )
            if last_optimize_plan:
                if last_optimize_plan.get("degraded"):
                    console.print(
                        f"[yellow]⚡ optimize beta: {last_optimize_plan['degraded']}[/yellow]"
                    )
                else:
                    from .optimize import savings_line
                    line = savings_line(last_optimize_plan, last_usage)
                    if line:
                        console.print(f"[dim]{line}[/dim]")
        except KeyboardInterrupt:
            console.rule(style="dim yellow", characters="─")
            # Abort means "stop everything": discard stacked messages too —
            # without this the drain would immediately launch the next
            # queued turn. Partial type-ahead deliberately survives (it
            # prefills the next prompt).
            _n_queued = len(state["input_queue"])
            if _n_queued:
                state["input_queue"].clear()
            state["esc_interrupt"].clear()
            # Completed work survives the abort — an hour of hops must not
            # vanish from context because the user pressed esc.
            _kept = _finalize_interrupted_turn(state, "abort")
            if _kept:
                console.print(
                    f"[yellow]aborted by user — {_kept} completed action(s) "
                    "kept, returning to prompt[/yellow]"
                )
            else:
                console.print("[yellow]aborted by user — returning to prompt[/yellow]")
            if _n_queued:
                console.print(f"[dim]discarded {_n_queued} queued message(s)[/dim]")
        except httpx.HTTPStatusError as e:
            # Retryable statuses were already retried with backoff inside
            # _stream_hop_with_retry — reaching here means retries exhausted
            # or a non-retryable status. Completed hops stay in history.
            console.rule(style="dim red", characters="─")
            body = _safe_response_text(e.response)
            console.print(f"[red]API error {e.response.status_code}: {body}[/red]")
            if _finalize_interrupted_turn(state, "error"):
                console.print("[dim]completed work this turn is saved — say 'continue' to resume[/dim]")
        except httpx.RequestError as e:
            # Network / connection / timeout / DNS — recoverable, stay in REPL.
            console.rule(style="dim red", characters="─")
            console.print(f"[red]Network error ({type(e).__name__}): {e}[/red]")
            if _finalize_interrupted_turn(state, "error"):
                console.print("[dim]completed work this turn is saved — say 'continue' to resume[/dim]")
        except Exception as e:  # pragma: no cover — last-line safety net
            console.rule(style="dim red", characters="─")
            console.print(f"[red]Unexpected error ({type(e).__name__}): {e}[/red]")
            console.print("[dim]session is still alive — returning to prompt[/dim]")
            _finalize_interrupted_turn(state, "error")
        finally:
            # Spend is real even when the turn aborts or errors — commit it
            # unconditionally so /cost never under-reports.
            state["session_cost"] += agg_cost


if __name__ == "__main__":
    main()
