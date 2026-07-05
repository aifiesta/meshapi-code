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
from rich.markup import escape as _rich_escape
from rich.text import Text

from . import __version__, statusbar
from .attachments import AttachmentError, find_image_tokens, load_image
from .client import stream_chat
from .commands import handle_command, prompt_for_api_key
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
)
from .tools import (
    PLAN_TOOLS, TOOLS, build_system_prompt, execute as exec_tool,
    find_stub_markers, parse_error_context, repair_tool_args, schema_hint,
    stub_guard_suppressed, summarize_call, validate_call,
)
from .update import maybe_offer, start_background_check

# Hop caps for the tool-calling loop. A turn without a plan rarely needs many
# hops; one with a plan may legitimately span dozens of small steps (≈3-4 tool
# calls per step × 15 steps).
MAX_HOPS_NO_PLAN = 8
MAX_HOPS_WITH_PLAN = 60

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
        "--route", choices=["auto", "off"],
        help="Auto-routing: 'auto' lets the gateway pick a model per prompt",
    )
    p.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default="default",
        help="Tool permission mode (default: ask each tool). Cycle in-session with shift+tab.",
    )
    return p.parse_args(argv)


def render_banner(cfg: dict) -> None:
    info_per_line: list = [
        None,
        None,
        Text.from_markup(f"[bold {BRAND}]✦  meshapi {__version__}[/bold {BRAND}]"),
        Text.from_markup(f"cwd:   [{BRAND}]{pretty_cwd()}[/{BRAND}]"),
        Text.from_markup(f"model: [bold {BRAND}]{cfg['model']}[/bold {BRAND}]"),
        Text.from_markup(f"route: [{BRAND}]{'auto' if cfg.get('auto_route') else 'off'}[/{BRAND}]"),
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


def _drop_in_flight_turn(state: dict) -> None:
    """Roll messages back to just before the current user turn.

    Called from every error path so the next prompt starts on a clean message
    list — no dangling assistant/tool messages from a half-finished hop, no
    orphaned user turn the model would have to apologize for.
    """
    while state["messages"] and state["messages"][-1]["role"] != "user":
        state["messages"].pop()
    if state["messages"] and state["messages"][-1]["role"] == "user":
        state["messages"].pop()


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


def handle_tool_calls(tool_calls: list, state: dict) -> None:
    """Append assistant tool_calls message + tool result messages to state.

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
    shown_mode = state.get("mode")
    for p in prepared:
        _esc = state.get("esc_interrupt")
        if _esc is not None and _esc.is_set():
            # ESC pressed — abort between tool calls. _drop_in_flight_turn
            # rolls history past the half-batch, preserving the one-result-
            # per-id invariant the same way ctrl+c does.
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
                    elif name == "run_bash":
                        _print_shell_command(args.get("command") or "")
                        result = exec_tool(name, args, state["cfg"])
                        _render_tool_result(name, args, result)
                    else:
                        result = exec_tool(name, args, state["cfg"])
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


def _turn_status_line(model: str, auto_routed: bool, prompt_t, completion_t,
                      agg_cost: float, session_cost: float, elapsed: float) -> str:
    """The dim per-turn summary. Cost segments are omitted when the gateway
    returned no cost this turn (it's best-effort on some paths) — no
    dangling '—'. When auto-routed, show which model the router picked."""
    display_model = f"auto → {model}" if auto_routed else model
    segments = [display_model, f"{prompt_t}→{completion_t} tok"]
    if agg_cost:
        segments.append(fmt_usd(agg_cost))
    if session_cost:
        segments.append(f"session {fmt_usd(session_cost)}")
    segments.append(f"{elapsed:.1f}s")
    return "  •  ".join(segments)


def main() -> None:
    args = parse_args()
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
    if args.route:
        cfg["auto_route"] = args.route == "auto"

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
                ack.append("  (queued)", style="dim")
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

        if not user_input.strip():
            continue
        if user_input.startswith("/"):
            if not handle_command(user_input, state):
                break
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
        console.print()

        # Tool-calling loop: keep streaming until model returns text without
        # tool_calls or we hit the hop cap. The cap is larger when a plan is
        # active because a legitimate multi-step task may need many hops.
        state["doom_streak"] = {}  # fresh user turn — failure streaks reset
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
        try:
            # While-loop so the cap can be promoted dynamically the moment the
            # model creates a plan (a for-loop's range is frozen at construction
            # time, which previously trapped multi-step turns at 8 hops).
            hopped = 0
            max_hops = MAX_HOPS_NO_PLAN
            while True:
                if state["esc_interrupt"].is_set():
                    raise KeyboardInterrupt  # ESC pressed — abort between hops
                if state.get("plan") and max_hops < MAX_HOPS_WITH_PLAN:
                    max_hops = MAX_HOPS_WITH_PLAN
                if hopped >= max_hops:
                    console.print(
                        f"[yellow]Stopped after {hopped} tool hops — "
                        "model wasn't converging. Ask it to wrap up or revise the plan.[/yellow]"
                    )
                    # Breadcrumb: record the incomplete state in history so a
                    # "continue" turn resumes the right steps instead of the
                    # model reconstructing (or hallucinating) progress.
                    _plan = state.get("plan")
                    if _plan is not None and not _plan.is_complete():
                        state["messages"].append({
                            "role": "system",
                            "content": (
                                f"[Execution was paused after {hopped} tool hops "
                                f"with the plan incomplete {_plan.summary()}. "
                                f"Remaining steps:\n{_plan.reminder_text()}\n"
                                "When the user asks to continue, resume these "
                                "remaining steps. Do not claim the task is "
                                "finished until they are done.]"
                            ),
                        })
                    break
                hopped += 1

                # Re-ground the model in the current plan state on every hop.
                # The plan lives client-side; without this the model has to
                # reconstruct "what's left" from buried tool history and tends
                # to stop early or falsely claim completion. Injected
                # transiently (not persisted) so it always reflects live state
                # and history stays clean.
                turn_messages = state["messages"]
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
                if _extras:
                    turn_messages = state["messages"] + _extras
                _hdr = "auto" if state["cfg"].get("auto_route") else state["cfg"]["model"]
                if hopped > 1:
                    _hdr += f" · hop {hopped}"
                reply, meta = render_stream(
                    stream_chat(turn_messages, state["cfg"], tools=TOOLS),
                    header=_hdr,
                    state=state,
                )
                cost = meta.get("cost")
                if cost is not None:
                    try:
                        agg_cost += float(cost)
                    except (TypeError, ValueError):
                        pass
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
                last_usage = meta.get("usage") or last_usage
                last_elapsed += meta.get("elapsed", 0.0)
                last_optimize_plan = meta.get("optimize_plan") or last_optimize_plan

                tool_calls = meta.get("tool_calls") or []
                if not tool_calls:
                    state["messages"].append({"role": "assistant", "content": reply})
                    # Quality guard: the model ended its turn but files it
                    # wrote still carry stub markers. Spend ONE fix-it hop
                    # with concrete evidence — cheap models respond to
                    # "script.js line 3 says 'Add game logic here'" far
                    # better than to generic scolding. Bounded: once per
                    # turn, never past the hop cap, suppressed when the
                    # user asked for scaffolding.
                    if (state.get("stub_files") and not state.get("quality_hop_fired")
                            and not state.get("stub_guard_off") and hopped < max_hops):
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
                handle_tool_calls(tool_calls, state)

            # Quality guard, final honesty: the turn is over and flagged
            # files survived (fix-it hop included, or the hop cap skipped
            # it). Post-loop so BOTH break paths land here; exception paths
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

            state["session_cost"] += agg_cost
            prompt_t = last_usage.get("prompt_tokens", "?")
            completion_t = last_usage.get("completion_tokens", "?")
            console.rule(style=BRAND_DIM, characters="─")
            console.print(
                f"[dim]{_turn_status_line(last_model, state['cfg'].get('auto_route', False), prompt_t, completion_t, agg_cost, state['session_cost'], last_elapsed)}[/dim]"
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
            console.print("[yellow]aborted by user — returning to prompt[/yellow]")
            # Abort means "stop everything": discard stacked messages too —
            # without this the drain would immediately launch the next
            # queued turn. Partial type-ahead deliberately survives (it
            # prefills the next prompt).
            _n_queued = len(state["input_queue"])
            if _n_queued:
                state["input_queue"].clear()
                console.print(f"[dim]discarded {_n_queued} queued message(s)[/dim]")
            state["esc_interrupt"].clear()
            # Drop the in-flight user turn so the next prompt is a clean slate.
            # Trailing assistant/tool messages from partial hops are left in
            # history; the model can ignore them or summarize as needed.
            _drop_in_flight_turn(state)
        except httpx.HTTPStatusError as e:
            console.rule(style="dim red", characters="─")
            body = _safe_response_text(e.response)
            console.print(f"[red]API error {e.response.status_code}: {body}[/red]")
            _drop_in_flight_turn(state)
        except httpx.RequestError as e:
            # Network / connection / timeout / DNS — recoverable, stay in REPL.
            console.rule(style="dim red", characters="─")
            console.print(f"[red]Network error ({type(e).__name__}): {e}[/red]")
            _drop_in_flight_turn(state)
        except Exception as e:  # pragma: no cover — last-line safety net
            console.rule(style="dim red", characters="─")
            console.print(f"[red]Unexpected error ({type(e).__name__}): {e}[/red]")
            console.print("[dim]session is still alive — returning to prompt[/dim]")
            _drop_in_flight_turn(state)


if __name__ == "__main__":
    main()
