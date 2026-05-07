"""Slash command handlers."""
from pathlib import Path

from rich.panel import Panel

from .config import save_config
from .permissions import LABELS, Mode, from_str
from .render import console, fmt_usd
from .tools import build_system_prompt

ROUTES = {"cheapest", "fastest", "balanced"}


def handle_command(cmd: str, state: dict) -> bool:
    """Handle slash commands. Returns True if app should continue."""
    parts = cmd.strip().split(maxsplit=1)
    name = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if name in ("/exit", "/quit", "/q"):
        return False

    if name == "/clear":
        state["messages"] = [{"role": "system", "content": build_system_prompt(state["cfg"])}]
        state["session_cost"] = 0.0
        console.print("[dim]Conversation cleared.[/dim]")

    elif name == "/model":
        if arg:
            state["cfg"]["model"] = arg
            save_config(state["cfg"])
            console.print(f"[dim]Model set to {arg}[/dim]")
        else:
            console.print(f"[dim]Current model: {state['cfg']['model']}[/dim]")

    elif name == "/route":
        if not arg:
            console.print(f"[dim]Current route: {state['cfg'].get('route') or 'default'}[/dim]")
        elif arg in ROUTES or arg == "default":
            state["cfg"]["route"] = None if arg == "default" else arg
            save_config(state["cfg"])
            console.print(f"[dim]Routing set to {arg}[/dim]")
        else:
            console.print(f"[red]Unknown route. Try: {', '.join(sorted(ROUTES))}, default[/red]")

    elif name == "/file":
        path = Path(arg).expanduser()
        if path.exists():
            content = path.read_text()
            state["messages"].append({
                "role": "user",
                "content": f"File: {path.name}\n\n```\n{content}\n```",
            })
            console.print(f"[dim]Added {path.name} ({len(content)} chars) to context[/dim]")
        else:
            console.print(f"[red]File not found: {path}[/red]")

    elif name == "/system":
        if arg:
            state["cfg"]["system"] = arg
            state["messages"] = [{"role": "system", "content": build_system_prompt(state["cfg"])}]
            console.print("[dim]System prompt updated and conversation reset.[/dim]")
        else:
            console.print(f"[dim]{state['cfg']['system']}[/dim]")

    elif name == "/cost":
        console.print(f"[dim]Session spend: {fmt_usd(state.get('session_cost', 0))}[/dim]")

    elif name == "/mode":
        if not arg:
            cur = state.get("mode", Mode.ASK)
            console.print(f"[dim]Current mode: {LABELS[cur]} ({cur.value})[/dim]")
        else:
            try:
                state["mode"] = from_str(arg)
                console.print(f"[dim]Mode set to {LABELS[state['mode']]}[/dim]")
            except ValueError as e:
                console.print(f"[red]{e}[/red]")

    elif name == "/help":
        console.print(Panel.fit(
            "/exit             end session\n"
            "/clear            reset conversation\n"
            "/model <name>     switch model (e.g. anthropic/claude-sonnet-4.5)\n"
            "/route <mode>     cheapest|fastest|balanced|default\n"
            "/mode <perm>      ask|bypass|none  (or shift+tab to cycle)\n"
            "/file <path>      add file to context\n"
            "/system <txt>     set system prompt\n"
            "/cost             show session spend\n"
            "/help             show this",
            title="commands",
            border_style="cyan",
        ))
    else:
        console.print(f"[red]Unknown command: {name}. Type /help[/red]")
    return True
