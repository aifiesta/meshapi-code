"""meshapi — terminal chat REPL for Mesh API."""
import argparse
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
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.markup import escape as _rich_escape
from rich.text import Text

from . import __version__, statusbar
from .attachments import AttachmentError, find_image_tokens, load_image
from .client import stream_chat
from .commands import handle_command
from .config import (
    CONFIG_FILE, HISTORY_FILE, clear_servers_file, load_config, load_servers,
    save_servers, secure_file,
)
from .keywatcher import KeyWatcher
from .permissions import AUTO_APPROVE, Mode, from_str, next_mode
from .plan import Plan
from . import safety
from .render import (
    BRAND, BRAND_BG, BRAND_BG_FG, BRAND_DIM, CODE, console, fmt_usd, pretty_cwd, render_stream,
)
from .tools import PLAN_TOOLS, TOOLS, build_system_prompt, execute as exec_tool, summarize_call

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
    p.add_argument("--route", choices=["cheapest", "fastest", "balanced"], help="Routing mode")
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
        Text.from_markup(f"route: [{BRAND}]{cfg.get('route') or 'default'}[/{BRAND}]"),
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


def confirm_tool_call(name: str, args: dict, watcher=None) -> bool:
    """ASK-mode prompt for a single tool call. Returns True if approved.

    `watcher` is the KeyWatcher: paused around `console.input` so the
    terminal is in canonical line-edit mode while reading the y/n answer.
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
    # Pause the keywatcher so console.input gets canonical-mode stdin.
    paused_ctx = watcher.paused() if watcher is not None else _noop_ctx()
    try:
        with paused_ctx:
            ans = console.input("[bold]y[/bold] (yes) / [bold]n[/bold] (no)  › ").strip().lower()
    except KeyboardInterrupt:
        # Bubble up so the outer turn handler can abort cleanly.
        raise
    except EOFError:
        return False
    except Exception:
        # If the input prompt itself blows up (corrupted terminal state, etc.),
        # treat it as a deny and keep the session alive.
        return False
    return ans in ("y", "yes")


@contextlib.contextmanager
def _noop_ctx():
    yield


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


def _kill_server(pid: int) -> None:
    """SIGTERM the entire process group of a tracked server (best-effort)."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
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

    port = args.get("port")
    if port is None:
        try:
            port = _find_free_port()
        except RuntimeError as e:
            return f"Error: {e}"
    elif not isinstance(port, int) or port < 1 or port > 65535:
        return f"Error: invalid port {port!r}; must be an integer in 1..65535."
    elif _port_open(port):
        return (
            f"Error: port {port} is already in use. Pick a different port or "
            "omit `port` to auto-pick a free one."
        )

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
    console.print(f"  [dim]port {port}, cwd {cwd}[/dim]")

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

    # Poll the port up to wait_seconds.
    deadline = time.monotonic() + wait_seconds
    start_t = time.monotonic()
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            with output_lock:
                tail = "\n".join(output_lines[-30:])
            return (
                f"Error: server exited with code {proc.returncode} before "
                f"opening port {port}.\nOutput:\n{tail or '(no output)'}"
            )
        if _port_open(port):
            elapsed = time.monotonic() - start_t
            # Give the server a beat to log its banner ("ready in X ms" etc.)
            time.sleep(0.4)
            with output_lock:
                preview = "\n".join(output_lines[:20])
            url = f"http://localhost:{port}"
            state.setdefault("servers", []).append({
                "pid": proc.pid, "port": port, "cmd": cmd, "url": url,
            })
            _persist_servers(state)  # survive a hard kill / crash

            # Make the URL big, plain, on its own line — most terminals
            # auto-detect bare URLs as cmd-clickable, which is more reliable
            # than rich's OSC-8 `[link=...]` markup that some terminals
            # (xterm.js, older Terminal.app) strip silently.
            from rich.panel import Panel
            console.print(f"  [green]✓ ready in {elapsed:.1f}s[/green]")
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

            return (
                f"Server up at {url} (pid {proc.pid}, ready in {elapsed:.1f}s).\n"
                "The user can already see the URL in their terminal — it was "
                "printed by the CLI. Respond with a SINGLE short text line "
                "(e.g. 'Server's up at " + url + " — open it in your browser') "
                "and END THE TURN. Do NOT call any more tools this turn — "
                "no curl, no read_file, no anything. The server keeps running "
                "in the background until meshapi exits; the user will interact "
                "with it through the browser, not through you."
            )
        time.sleep(0.2)

    # Timeout — kill the whole tree.
    _kill_server(proc.pid)
    with output_lock:
        tail = "\n".join(output_lines[-30:])
    return (
        f"Error: timed out after {wait_seconds}s waiting for port {port} to "
        f"open. Killed the server. Output so far:\n{tail or '(no output)'}"
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


def handle_tool_calls(tool_calls: list, mode: Mode, state: dict) -> None:
    """Append assistant tool_calls message + tool result messages to state.

    Every tool execution is exception-isolated: if a single tool call blows up
    (unexpected exception, terminal disconnect during approval, etc.), we log
    a clear error, feed it back to the model as the tool result, and move on
    to the next call. The session never crashes from a single bad call.
    """
    state["messages"].append({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            }
            for tc in tool_calls
        ],
    })
    for tc in tool_calls:
        try:
            args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}

        try:
            if tc["name"] in PLAN_TOOLS:
                # Plan tools are bookkeeping — no filesystem or shell side
                # effects, so we don't gate them on the approval prompt.
                result = _handle_plan_tool(tc["name"], args, state)
            else:
                # Per-mode auto-approval: each Mode declares which tool names
                # bypass the y/n prompt. Anything not in the set, OR anything
                # that fails a safety check, falls back to confirmation —
                # even in BYPASS we ask before truly dangerous shapes
                # (sensitive paths, `rm -rf /`, sudo, curl | sh, ...).
                auto_approved = tc["name"] in AUTO_APPROVE.get(mode, set())
                safety_reason: str = ""
                if auto_approved and tc["name"] == "write_file":
                    ok, reason = safety.is_path_safe_for_auto_write(
                        args.get("path"), mode
                    )
                    if not ok:
                        auto_approved = False
                        safety_reason = reason or "path safety check failed"
                elif auto_approved and tc["name"] == "read_file":
                    # BYPASS auto-approves reads; we still block sensitive
                    # paths so the model can't silently leak ~/.ssh/...
                    # to the upstream provider.
                    ok, reason = safety.is_path_safe_for_auto_read(
                        args.get("path"), mode
                    )
                    if not ok:
                        auto_approved = False
                        safety_reason = reason or "path safety check failed"
                elif auto_approved and tc["name"] in ("run_bash", "start_server"):
                    ok, reason = safety.is_command_safe_for_auto(
                        args.get("command"), mode
                    )
                    if not ok:
                        auto_approved = False
                        safety_reason = reason or "command safety check failed"
                if not auto_approved and safety_reason:
                    console.print(
                        f"[yellow]⚠ auto-approval blocked: {safety_reason}[/yellow]"
                    )
                approved = auto_approved or confirm_tool_call(
                    tc["name"], args, watcher=state.get("watcher")
                )
                if approved:
                    # 1) Action header — purple, "the AI is doing this"
                    console.print(f"[{BRAND_DIM}]⚙ {summarize_call(tc['name'], args)}[/{BRAND_DIM}]")
                    # 2) Body BEFORE execution — cyan, "this is the actual
                    #    code/command being run". For write_file we have the
                    #    full content up front; for run_bash we have the
                    #    command (output prints after exec).
                    if tc["name"] == "start_server":
                        # Header is enough; _handle_start_server prints command,
                        # readiness, URL, and a short output preview itself.
                        result = _handle_start_server(args, state)
                        if result.startswith("Error:"):
                            console.print(f"  [red]✗ {_rich_escape(result)}[/red]")
                    elif tc["name"] == "write_file":
                        _print_file_diff(args.get("path") or "", args.get("content") or "")
                        result = exec_tool(tc["name"], args)
                        _render_tool_result(tc["name"], args, result)
                    elif tc["name"] == "run_bash":
                        _print_shell_command(args.get("command") or "")
                        result = exec_tool(tc["name"], args)
                        _render_tool_result(tc["name"], args, result)
                    else:
                        result = exec_tool(tc["name"], args)
                        _render_tool_result(tc["name"], args, result)
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
            "tool_call_id": tc["id"],
            "content": result,
        })
    # Once per batch — keep the mode visible between hops in multi-step turns.
    statusbar.print_line(state)


def main() -> None:
    args = parse_args()
    cfg = load_config()
    if args.model:
        cfg["model"] = args.model
    if args.route:
        cfg["route"] = args.route

    if not cfg["api_key"]:
        console.print(
            "[red]No API key found. Set MESHAPI_API_KEY env var or edit "
            f"{CONFIG_FILE}[/red]"
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

    # Out-of-prompt key watcher: lets shift+tab cycle mode during streaming
    # and tool execution. Paused while prompt_toolkit owns stdin.
    watcher = KeyWatcher(on_shift_tab=_cycle_mode)
    state["watcher"] = watcher  # so confirm_tool_call can pause around y/n input

    # Touch the history file with 0600 up front so prompt_toolkit doesn't
    # create it world-readable on first write.
    HISTORY_FILE.touch(mode=0o600, exist_ok=True)
    secure_file(HISTORY_FILE)
    session = PromptSession(
        history=ScrubbedFileHistory(str(HISTORY_FILE)),
        key_bindings=kb,
    )

    render_banner(cfg)
    _adopt_orphaned_servers(state)
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

    for _sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(_sig, _signal_shutdown)
        except (ValueError, OSError):
            pass

    while True:
        try:
            # cwd separator above the input box. The mode indicator is no
            # longer printed here — it lives in the bottom_toolbar below the
            # prompt, which prompt_toolkit repaints live on shift+tab:
            #   ──────────────────────────────────────────── cli_for_meshapi_v1
            #   › ...
            #   ⏵⏵ bypass permissions on  (shift+tab to cycle) · esc to interrupt
            console.rule(
                title=f"[{BRAND_DIM}]{Path.cwd().name}[/{BRAND_DIM}]",
                align="right",
                style=BRAND_DIM,
                characters="─",
            )
            with watcher.paused():
                # Hand stdin off to prompt_toolkit (canonical-mode termios).
                # The prompt itself is just "› "; the mode indicator is the
                # bottom_toolbar, repainted live by the s-tab binding's
                # event.app.invalidate(). "noreverse" kills prompt_toolkit's
                # default inverted bar so the toggle reads as plain text.
                user_input = session.prompt(
                    prompt_message,
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
                _plan = state.get("plan")
                if _plan is not None and not _plan.is_complete():
                    turn_messages = state["messages"] + [{
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
                    }]
                reply, meta = render_stream(
                    stream_chat(turn_messages, state["cfg"], tools=TOOLS)
                )
                cost = meta.get("cost")
                if cost is not None:
                    try:
                        agg_cost += float(cost)
                    except (TypeError, ValueError):
                        pass
                last_model = meta.get("model") or last_model
                last_usage = meta.get("usage") or last_usage
                last_elapsed += meta.get("elapsed", 0.0)
                last_optimize_plan = meta.get("optimize_plan") or last_optimize_plan

                tool_calls = meta.get("tool_calls") or []
                if not tool_calls:
                    state["messages"].append({"role": "assistant", "content": reply})
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
                handle_tool_calls(tool_calls, state["mode"], state)

            state["session_cost"] += agg_cost
            prompt_t = last_usage.get("prompt_tokens", "?")
            completion_t = last_usage.get("completion_tokens", "?")
            cost_str = fmt_usd(agg_cost) if agg_cost else "—"
            console.rule(style=BRAND_DIM, characters="─")
            console.print(
                f"[dim]{last_model}  •  {prompt_t}→{completion_t} tok  •  {cost_str}  •  "
                f"session {fmt_usd(state['session_cost'])}  •  {last_elapsed:.1f}s[/dim]"
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
