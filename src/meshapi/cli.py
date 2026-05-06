"""meshapi — terminal chat REPL for Mesh API."""
import argparse
import sys

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.panel import Panel

from . import __version__
from .client import stream_chat
from .commands import handle_command
from .config import CONFIG_FILE, HISTORY_FILE, load_config
from .render import console, fmt_usd, render_stream


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
    console.print(Panel.fit(
        f"meshapi {__version__}\n"
        f"model: [bold cyan]{cfg['model']}[/bold cyan]\n"
        f"route: [cyan]{cfg.get('route') or 'default'}[/cyan]\n"
        "type /help for commands, /exit to quit",
        border_style="cyan",
    ))

    while True:
        try:
            user_input = session.prompt(
                "you > ",
                style=Style.from_dict({"prompt": "ansicyan bold"}),
            )
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
            tokens = (
                f"{usage.get('prompt_tokens', '?')} → {usage.get('completion_tokens', '?')} tok"
                if usage else ""
            )
            cost_line = f"[dim]{tokens}  •  {fmt_usd(cost)}  •  session {fmt_usd(state['session_cost'])}[/dim]"
            console.print(cost_line)
        except httpx.HTTPStatusError as e:
            console.print(f"[red]API error {e.response.status_code}: {e.response.text}[/red]")
            state["messages"].pop()
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            state["messages"].pop()
        console.print()


if __name__ == "__main__":
    main()
