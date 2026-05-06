"""meshapi — terminal chat REPL for Mesh API."""
import argparse
import sys
from pathlib import Path

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.text import Text

from . import __version__
from .client import stream_chat
from .commands import handle_command
from .config import CONFIG_FILE, HISTORY_FILE, load_config
from .render import BRAND, BRAND_BG, BRAND_BG_FG, BRAND_DIM, console, fmt_usd, pretty_cwd, render_stream

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


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="meshapi", description="Terminal chat for Mesh API")
    p.add_argument("--version", action="version", version=f"meshapi {__version__}")
    p.add_argument("--model", help="Override model for this session (e.g. openai/gpt-4o-mini)")
    p.add_argument("--route", choices=["cheapest", "fastest", "balanced"], help="Routing mode")
    return p.parse_args(argv)


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
        "messages": [{"role": "system", "content": cfg["system"]}],
        "session_cost": 0.0,
    }

    session = PromptSession(history=FileHistory(str(HISTORY_FILE)))

    info_per_line: list = [
        None,
        None,
        Text.from_markup(f"[bold {BRAND}]✦  meshapi {__version__}[/bold {BRAND}]"),
        Text.from_markup(f"cwd:   [{BRAND}]{pretty_cwd()}[/{BRAND}]"),
        Text.from_markup(f"model: [bold {BRAND}]{cfg['model']}[/bold {BRAND}]"),
        Text.from_markup(f"route: [{BRAND}]{cfg.get('route') or 'default'}[/{BRAND}]"),
    ]

    console.print()  # top gap so banner doesn't crowd the shell prompt
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
    console.print()  # bottom gap before the first prompt rule

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
        try:
            reply, meta = render_stream(stream_chat(state["messages"], state["cfg"]))
            state["messages"].append({"role": "assistant", "content": reply})

            cost = meta.get("cost")
            if cost is not None:
                try:
                    state["session_cost"] += float(cost)
                except (TypeError, ValueError):
                    pass
            usage = meta.get("usage") or {}
            model = meta.get("model") or state["cfg"]["model"]
            elapsed = meta.get("elapsed", 0.0)
            prompt_t = usage.get("prompt_tokens", "?")
            completion_t = usage.get("completion_tokens", "?")
            cost_str = fmt_usd(cost) if cost is not None else "—"
            console.rule(style=BRAND_DIM, characters="─")
            console.print(
                f"[dim]{model}  •  {prompt_t}→{completion_t} tok  •  {cost_str}  •  "
                f"session {fmt_usd(state['session_cost'])}  •  {elapsed:.1f}s[/dim]"
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
