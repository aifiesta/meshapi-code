"""Slash command handlers."""
from pathlib import Path

from rich.panel import Panel

from .attachments import AttachmentError, load_image
from .config import save_config
from .permissions import LABELS, Mode, from_str
from .render import CODE, console, fmt_usd
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

    elif name == "/image":
        if not arg:
            queued = state.get("pending_attachments") or []
            if not queued:
                console.print(
                    "[dim]/image <path-or-url>  attach an image to the next prompt[/dim]"
                )
            else:
                console.print(f"[dim]{len(queued)} image(s) queued for next prompt[/dim]")
        else:
            try:
                part, info = load_image(arg.strip())
            except AttachmentError as e:
                console.print(f"[red]Can't attach: {e}[/red]")
            else:
                # Per-session image budget check (SSRF + 20 MB per-image
                # are already enforced inside load_image).
                from .safety import SESSION_IMAGE_BYTE_CAP
                sent = state.get("session_image_bytes", 0)
                queued_bytes = sum(
                    int(a.get("size_bytes", 0))
                    for a in (state.get("pending_attachments") or [])
                )
                if sent + queued_bytes + info["size_bytes"] > SESSION_IMAGE_BYTE_CAP:
                    cap_mb = SESSION_IMAGE_BYTE_CAP // (1024 * 1024)
                    console.print(
                        f"[red]Can't attach: would exceed session image budget "
                        f"({cap_mb} MB).[/red]"
                    )
                else:
                    state.setdefault("pending_attachments", []).append({
                        "part": part,
                        "size_bytes": info["size_bytes"],
                        "name": info["name"],
                    })
                    size_kb = max(1, info["size_bytes"] // 1024)
                    console.print(
                        f"[{CODE}]📎 attached {info['name']} ({size_kb} KB, {info['mime']})[/{CODE}]"
                    )

    elif name == "/clear-attach":
        had = len(state.get("pending_attachments") or [])
        state["pending_attachments"] = []
        if had:
            console.print(f"[dim]Dropped {had} queued attachment(s).[/dim]")
        else:
            console.print("[dim]Nothing queued.[/dim]")

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
            cur = state.get("mode", Mode.DEFAULT)
            console.print(f"[dim]Current mode: {LABELS[cur]} ({cur.value})[/dim]")
        else:
            try:
                state["mode"] = from_str(arg)
                console.print(f"[dim]Mode set to {LABELS[state['mode']]}[/dim]")
            except ValueError as e:
                console.print(f"[red]{e}[/red]")

    elif name == "/help":
        console.print(Panel.fit(
            "/exit              end session\n"
            "/clear             reset conversation\n"
            "/model <name>      switch model (e.g. anthropic/claude-sonnet-4.5)\n"
            "/route <mode>      cheapest|fastest|balanced|default\n"
            "/mode <perm>       default|accept-edits|auto|bypass  (or shift+tab)\n"
            "/file <path>       add text file to context\n"
            "/image <path|url>  attach an image (base64) to the next prompt\n"
            "/clear-attach      drop any queued image attachments\n"
            "/system <txt>      set system prompt\n"
            "/cost              show session spend\n"
            "/help              show this\n\n"
            "[dim]Image paths in a prompt auto-attach: drop /path/img.png in your\n"
            "input and it's sent as a base64 image part. Wrap in backticks to keep\n"
            "it as text. Multiple images per prompt are supported.\n\n"
            "Anything you /file, /image, or that the model reads via tools is sent\n"
            "to the Mesh API gateway and the upstream model — including file\n"
            "contents, screenshots, and shell output. Don't attach secrets.\n"
            "Mode auto-approvals: accept-edits auto-writes inside cwd; auto adds\n"
            "shell commands; bypass auto-approves everything (still asks before\n"
            "writing to ~/.ssh, /etc, rm -rf, sudo, curl|sh, etc.).[/dim]",
            title="commands",
            border_style="cyan",
        ))
    else:
        console.print(f"[red]Unknown command: {name}. Type /help[/red]")
    return True
