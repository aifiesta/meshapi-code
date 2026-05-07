"""meshapi — terminal chat REPL for Mesh API."""
import argparse
import json
import re
import sys
from pathlib import Path

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.text import Text

from . import __version__
from .client import stream_chat
from .commands import handle_command
from .config import CONFIG_FILE, HISTORY_FILE, load_config, secure_file
from .permissions import HINTS, LABELS, Mode, from_str, next_mode
from .render import (
    BRAND, BRAND_BG, BRAND_BG_FG, BRAND_DIM, console, fmt_usd, pretty_cwd, render_stream,
)
from .tools import TOOLS, build_system_prompt, execute as exec_tool, summarize_call

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
        default="ask",
        help="Tool permission mode (default: ask). Cycle in-session with shift+tab.",
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


def confirm_tool_call(name: str, args: dict) -> bool:
    """ASK-mode prompt for a single tool call. Returns True if approved."""
    summary = summarize_call(name, args)
    console.print(f"[bold {BRAND}]⚙ approve tool call?[/bold {BRAND}]  [dim]{summary}[/dim]")
    if name in ("read_file", "write_file"):
        console.print(_resolved_path_line(args.get("path") or ""))
    if name == "write_file":
        preview = (args.get("content") or "")[:300]
        console.print(f"[dim]──[/dim]\n{preview}{'…' if len(args.get('content') or '') > 300 else ''}\n[dim]──[/dim]")
    elif name == "run_bash":
        console.print(f"[dim]$ {args.get('command')}[/dim]")
    try:
        ans = console.input("[bold]y[/bold] (yes) / [bold]n[/bold] (no)  › ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return False
    return ans in ("y", "yes")


def handle_tool_calls(tool_calls: list, mode: Mode, state: dict) -> None:
    """Append assistant tool_calls message + tool result messages to state."""
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
        except json.JSONDecodeError:
            args = {}
        approved = mode == Mode.BYPASS or confirm_tool_call(tc["name"], args)
        if approved:
            console.print(f"[{BRAND_DIM}]⚙ {summarize_call(tc['name'], args)}[/{BRAND_DIM}]")
            result = exec_tool(tc["name"], args)
            preview = result[:200].replace("\n", " ")
            tail = "…" if len(result) > 200 else ""
            console.print(f"[dim]  → {preview}{tail}[/dim]")
        else:
            result = "User denied this tool call."
            console.print(f"[dim]  → denied by user[/dim]")
        state["messages"].append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": result,
        })


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
    }

    kb = KeyBindings()

    @kb.add("s-tab")  # Shift+Tab
    def _(event):
        state["mode"] = next_mode(state["mode"])
        event.app.invalidate()

    def bottom_toolbar():
        m = state["mode"]
        color = "ansired" if m == Mode.BYPASS else "ansiyellow" if m == Mode.NONE else "ansigreen"
        return FormattedText([
            ("", "  mode: "),
            (f"bold {color}", LABELS[m]),
            ("", f"   {HINTS[m]}   "),
            ("ansibrightblack", "shift+tab to cycle"),
        ])

    # Touch the history file with 0600 up front so prompt_toolkit doesn't
    # create it world-readable on first write.
    HISTORY_FILE.touch(mode=0o600, exist_ok=True)
    secure_file(HISTORY_FILE)
    session = PromptSession(
        history=ScrubbedFileHistory(str(HISTORY_FILE)),
        key_bindings=kb,
        bottom_toolbar=bottom_toolbar,
    )

    render_banner(cfg)

    while True:
        try:
            console.rule(
                title=f"[{BRAND_DIM}]{Path.cwd().name}[/{BRAND_DIM}]",
                align="right",
                style=BRAND_DIM,
                characters="─",
            )
            user_input = session.prompt(
                "› ",
                style=Style.from_dict({
                    "prompt": f"bold fg:{BRAND} bg:{BRAND_BG}",
                    "": f"fg:{BRAND_BG_FG} bg:{BRAND_BG}",
                    "bottom-toolbar": f"fg:{BRAND_BG_FG} bg:{BRAND_BG}",
                }),
            )
            console.rule(style=BRAND_DIM, characters="─")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]bye[/dim]")
            break

        if not user_input.strip():
            continue
        if user_input.startswith("/"):
            if not handle_command(user_input, state):
                break
            continue

        state["messages"].append({"role": "user", "content": user_input})
        console.print()

        # Tool-calling loop: keep streaming until model returns text without tool_calls.
        agg_cost = 0.0
        last_model = state["cfg"]["model"]
        last_usage: dict = {}
        last_elapsed = 0.0
        try:
            for _hop in range(8):  # safety cap
                tools_arg = TOOLS if state["mode"] != Mode.NONE else None
                reply, meta = render_stream(
                    stream_chat(state["messages"], state["cfg"], tools=tools_arg)
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

                tool_calls = meta.get("tool_calls") or []
                if not tool_calls:
                    state["messages"].append({"role": "assistant", "content": reply})
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
        except httpx.HTTPStatusError as e:
            console.rule(style="dim red", characters="─")
            console.print(f"[red]API error {e.response.status_code}: {e.response.text}[/red]")
            state["messages"].pop()
        except Exception as e:
            console.rule(style="dim red", characters="─")
            console.print(f"[red]Error: {e}[/red]")
            state["messages"].pop()


if __name__ == "__main__":
    main()
