"""Slash command handlers."""
import contextlib
from pathlib import Path

import httpx
from rich.panel import Panel

from .attachments import AttachmentError, load_image
from .config import CREDENTIALS_FILE, save_api_key, save_config
from .permissions import LABELS, Mode, from_str
from .render import CODE, console, fmt_usd
from .tools import build_system_prompt

_REASONING_LEVELS = ("high", "medium", "low", "none")


def _route_preview(state: dict) -> None:
    """POST /router/select with the conversation so far and show which
    model the Auto Router would pick — without running inference."""
    cfg = state["cfg"]
    msgs = [
        {"role": m["role"], "content": m["content"]}
        for m in state["messages"]
        if m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
        and m.get("content")
    ]
    if not msgs:
        console.print(
            "[dim]Nothing to preview yet — send a message first, then "
            "/route preview shows which model the router would pick.[/dim]"
        )
        return
    try:
        r = httpx.post(
            f"{cfg['base_url']}/router/select",
            json={"messages": msgs},
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=20,
        )
    except httpx.HTTPError as e:
        console.print(f"[red]Router preview failed ({type(e).__name__}: {e})[/red]")
        return
    if r.status_code == 404:
        console.print("[dim]This gateway doesn't support router preview yet.[/dim]")
        return
    if r.status_code >= 400:
        console.print(f"[red]Router preview returned HTTP {r.status_code}: {r.text[:200]}[/red]")
        return
    try:
        data = r.json()
    except ValueError:
        console.print("[red]Router preview returned non-JSON.[/red]")
        return
    picked = (
        data.get("resolved_model_id")
        or data.get("model")
        or data.get("x_resolved_model_id")
        if isinstance(data, dict) else None
    )
    if not picked:
        import json as _json
        console.print(f"[dim]router response: {_json.dumps(data)[:300]}[/dim]")
        return
    line = f"router would pick: [bold]{picked}[/bold]"
    extra = data.get("reason") or data.get("classification")
    if extra:
        line += f"  [dim]({extra})[/dim]"
    console.print(line)


def _fetch_models(state: dict) -> "list | None":
    """GET /models once per session (cached in state). None on failure,
    with the error already printed."""
    cached = state.get("models_cache")
    if cached is not None:
        return cached
    cfg = state["cfg"]
    try:
        r = httpx.get(
            f"{cfg['base_url']}/models",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=15,
        )
    except httpx.HTTPError as e:
        console.print(f"[red]Couldn't fetch models ({type(e).__name__}: {e})[/red]")
        return None
    if r.status_code in (401, 403):
        console.print(
            f"[red]The gateway rejected your API key (HTTP {r.status_code}). "
            "/login to set a new one.[/red]"
        )
        return None
    if r.status_code >= 400:
        console.print(f"[red]GET /models returned HTTP {r.status_code}[/red]")
        return None
    try:
        data = r.json()
    except ValueError:
        console.print("[red]GET /models returned non-JSON.[/red]")
        return None
    # Accept both the OpenAI envelope {"data": [...]} and a bare list.
    models = data.get("data") if isinstance(data, dict) else data
    if not isinstance(models, list):
        console.print("[red]Unexpected /models response shape.[/red]")
        return None
    state["models_cache"] = models
    return models


def _model_price_cols(m: dict) -> tuple:
    """($/1M in, $/1M out) display strings for a catalog entry.
    Prefers discounted per-1M, then per-1M, then derives from per-1k."""
    if m.get("is_free"):
        return "free", "free"
    pricing = m.get("pricing") or {}

    def pick(kind: str) -> str:
        for key, mult in (
            (f"{kind}_usd_per_1m_discounted", 1),
            (f"{kind}_usd_per_1m", 1),
            (f"{kind}_usd_per_1k", 1000),
        ):
            v = pricing.get(key)
            if v is not None:
                try:
                    return fmt_usd(float(v) * mult)
                except (TypeError, ValueError):
                    continue
        return "—"

    return pick("prompt"), pick("completion")


def _print_models_table(models: list, title: str) -> None:
    from rich.table import Table

    table = Table(title=title, border_style="cyan", title_style="bold cyan")
    table.add_column("model", overflow="fold")
    table.add_column("ctx", justify="right")
    table.add_column("type")
    table.add_column("think", justify="center")
    table.add_column("modalities")
    table.add_column("$/1M in", justify="right")
    table.add_column("$/1M out", justify="right")
    for m in sorted(models, key=lambda m: str(m.get("id") or "")):
        ctx = m.get("context_length")
        ctx_s = f"{ctx // 1000}k" if isinstance(ctx, int) and ctx >= 1000 else (str(ctx) if ctx else "—")
        mods_in = "+".join(m.get("input_modalities") or []) or "text"
        mods_out = "+".join(m.get("output_modalities") or []) or "text"
        p_in, p_out = _model_price_cols(m)
        table.add_row(
            str(m.get("id") or "?"),
            ctx_s,
            str(m.get("model_type") or "text"),
            "✓" if m.get("supports_thinking") else "",
            f"{mods_in}→{mods_out}",
            p_in,
            p_out,
        )
    console.print(table)


def _verify_api_key(key: str, base_url: str) -> tuple:
    """Best-effort live check against GET /models. Returns (ok, note).

    Only an explicit 401/403 rejects the key — network trouble or an
    unexpected status accepts it with a note, so onboarding never
    hard-fails because the user happens to be offline.
    """
    try:
        r = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
    except httpx.HTTPError as e:
        return True, f"couldn't reach the gateway to verify ({type(e).__name__}) — saved anyway"
    if r.status_code in (401, 403):
        return False, f"the gateway rejected this key (HTTP {r.status_code})"
    return True, ""


def prompt_for_api_key(cfg: dict, watcher=None) -> bool:
    """Interactive key setup: hidden-input prompt → live verify → persist 0600.

    Used on first run (no key anywhere) and by /login. Returns True if a
    working key was saved into both the credentials file and cfg. `watcher`
    is the KeyWatcher, paused around input so it doesn't eat keystrokes
    (None at startup — the watcher isn't running yet).
    """
    console.print(Panel.fit(
        "Connect your Mesh API key\n\n"
        "[dim]Grab one at[/dim] https://app.meshapi.ai [dim]→ API Keys. "
        "Keys start with[/dim] rsk_\n"
        "[dim]Input is hidden — paste the key and press enter. "
        "Ctrl+C to cancel.[/dim]",
        border_style="cyan",
    ))
    for _ in range(3):
        ctx = watcher.paused() if watcher is not None else contextlib.nullcontext()
        try:
            with ctx:
                key = console.input("API key › ", password=True).strip().strip("'\"")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return False
        if not key:
            console.print("[yellow]Nothing entered — paste your rsk_… key.[/yellow]")
            continue
        if not key.startswith("rsk_"):
            console.print(
                "[yellow]⚠ that doesn't look like a Mesh data-plane key (they "
                "start with rsk_) — checking it against the gateway anyway.[/yellow]"
            )
        ok, note = _verify_api_key(key, cfg["base_url"])
        if not ok:
            console.print(f"[red]✗ {note}. Try again.[/red]")
            continue
        try:
            save_api_key(key)
        except OSError as e:
            console.print(f"[red]Couldn't write {CREDENTIALS_FILE}: {e}[/red]")
            return False
        cfg["api_key"] = key
        if note:
            console.print(f"[yellow]⚠ {note}[/yellow]")
        console.print(
            f"[green]✓ key saved[/green] [dim]→ {CREDENTIALS_FILE} (0600). "
            "The MESHAPI_API_KEY env var overrides it; /login replaces it.[/dim]"
        )
        return True
    console.print(
        "[red]Giving up after 3 attempts. Double-check your key at "
        "https://app.meshapi.ai and run meshapi again.[/red]"
    )
    return False


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
        sub = arg.strip().lower()
        if not sub:
            if state["cfg"].get("auto_route"):
                console.print(
                    "[dim]route: auto — the gateway picks a model per prompt "
                    f"(pinned: {state['cfg']['model']})[/dim]"
                )
            else:
                console.print(f"[dim]route: off (model: {state['cfg']['model']})[/dim]")
        elif sub == "auto":
            state["cfg"]["auto_route"] = True
            save_config(state["cfg"])
            console.print(
                "[dim]Auto-routing on — each prompt goes to the model the "
                "gateway's router picks. /route off to pin back to "
                f"{state['cfg']['model']}.[/dim]"
            )
        elif sub in ("off", "default"):
            state["cfg"]["auto_route"] = False
            save_config(state["cfg"])
            console.print(f"[dim]Auto-routing off — pinned to {state['cfg']['model']}.[/dim]")
        elif sub == "preview":
            _route_preview(state)
        else:
            console.print("[red]Usage: /route auto | off | preview[/red]")

    elif name == "/models":
        models = _fetch_models(state)
        if models is not None:
            q = arg.strip().lower()
            if not q:
                subset, title = models, f"models ({len(models)})"
            elif q == "free":
                subset = [m for m in models if m.get("is_free")]
                title = f"free models ({len(subset)})"
            else:
                subset = [
                    m for m in models
                    if q in f"{m.get('id', '')} {m.get('name', '')} {m.get('description', '')}".lower()
                ]
                title = f"models matching '{q}' ({len(subset)})"
            if not subset:
                console.print(f"[dim]No models match '{arg.strip()}'.[/dim]")
            else:
                _print_models_table(subset, title)

    elif name == "/fallback":
        if not arg:
            fb = state["cfg"].get("fallback_models") or []
            if fb:
                note = (
                    "  [yellow](auto-route is on — combined semantics are "
                    "gateway-defined)[/yellow]"
                    if state["cfg"].get("auto_route") else ""
                )
                console.print(f"[dim]fallback: {' → '.join(fb)}[/dim]{note}")
            else:
                console.print("[dim]fallback: none — /fallback <m1> <m2> to set[/dim]")
        elif arg.strip().lower() in ("off", "none", "clear"):
            state["cfg"]["fallback_models"] = []
            save_config(state["cfg"])
            console.print("[dim]Fallback models cleared.[/dim]")
        else:
            wanted = arg.replace(",", " ").split()
            catalog = _fetch_models(state)
            if catalog:
                known = {m.get("id") for m in catalog}
                for miss in [w for w in wanted if w not in known]:
                    console.print(
                        f"[yellow]⚠ {miss} isn't in the model catalog — "
                        "keeping it anyway[/yellow]"
                    )
            state["cfg"]["fallback_models"] = wanted
            save_config(state["cfg"])
            console.print(f"[dim]Fallback order: {' → '.join(wanted)}[/dim]")

    elif name == "/reasoning":
        v = arg.strip().lower()
        if not v:
            cur = state["cfg"].get("reasoning_effort")
            console.print(f"[dim]reasoning effort: {cur or 'off'}[/dim]")
        elif v == "off":
            state["cfg"]["reasoning_effort"] = None
            save_config(state["cfg"])
            console.print("[dim]Reasoning effort off — not sent with requests.[/dim]")
        elif v in _REASONING_LEVELS:
            state["cfg"]["reasoning_effort"] = v
            save_config(state["cfg"])
            console.print(f"[dim]Reasoning effort set to {v}.[/dim]")
        else:
            console.print(
                f"[red]Usage: /reasoning {'|'.join(_REASONING_LEVELS)}|off[/red]"
            )

    elif name == "/update":
        from . import __version__
        from .update import fetch_latest, is_newer, offer_update
        console.print("[dim]checking pypi.org…[/dim]")
        latest = fetch_latest(timeout=10)
        if latest is None:
            console.print("[red]Couldn't reach PyPI to check for updates.[/red]")
        elif is_newer(latest, __version__):
            # Explicit /update ignores a previously declined version — the
            # user is asking.
            offer_update(latest, watcher=state.get("watcher"))
        else:
            console.print(
                f"[dim]meshapi {__version__} is up to date "
                f"(PyPI latest: {latest}).[/dim]"
            )

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

    elif name == "/optimize":
        # BETA: Mesh Optimize dial. 0 = off (full bypass), up to 0.95.
        if not arg:
            cur = float(state["cfg"].get("optimize") or 0)
            label = f"{cur}" if cur > 0 else "off"
            console.print(
                f"[dim]optimize (beta): {label}\n"
                "usage: /optimize <0 to 0.95>   e.g. /optimize 0.3\n"
                "       /optimize off\n"
                "0+ injects prompt cache breakpoints and max_tokens defaults; "
                "0.2+ also prunes consumed tool results from old turns. "
                "Savings appear in the status line after each turn. This is a "
                "beta feature; set /optimize off to bypass entirely.[/dim]"
            )
        else:
            raw = arg.strip().lower()
            try:
                value = 0.0 if raw == "off" else float(raw)
            except ValueError:
                console.print("[red]Not a number. Use 0 to 0.95, or 'off'.[/red]")
            else:
                if not 0 <= value <= 0.95:
                    console.print("[red]Dial range is 0 to 0.95.[/red]")
                else:
                    state["cfg"]["optimize"] = value
                    save_config(state["cfg"])
                    if value > 0:
                        console.print(
                            f"[dim]optimize (beta) set to {value}. Levers: cache "
                            "injection, max_tokens defaults"
                            + (", tool result pruning" if value >= 0.2 else "")
                            + ". /optimize off to disable.[/dim]"
                        )
                    else:
                        console.print("[dim]optimize off. Requests pass through untouched.[/dim]")

    elif name == "/login":
        prompt_for_api_key(state["cfg"], watcher=state.get("watcher"))

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
            "/exit                      end session\n"
            "/clear                     reset conversation\n"
            "/model <name>              switch model (e.g. anthropic/claude-sonnet-4.5)\n"
            "/models [free|<query>]     browse the catalog (context, $/1M pricing)\n"
            "/route auto|off|preview    auto-route each prompt to the best model\n"
            "/fallback <m1> <m2> | off  ordered fallback models if the primary fails\n"
            "/reasoning <level>         high|medium|low|none|off reasoning effort\n"
            "/mode <perm>               default|accept-edits|auto|bypass  (or shift+tab)\n"
            "/file <path>               add text file to context\n"
            "/image <path|url>          attach an image (base64) to the next prompt\n"
            "/clear-attach              drop any queued image attachments\n"
            "/system <txt>              set system prompt\n"
            "/cost                      show session spend\n"
            "/optimize <dial>           token savings, beta: 0 off, up to 0.95\n"
            "/login                     set or replace your API key\n"
            "/update                    check PyPI for a newer meshapi\n"
            "/help                      show this\n\n"
            "[dim]Image paths in a prompt auto-attach: drop /path/img.png in your\n"
            "input and it's sent as a base64 image part. Wrap in backticks to keep\n"
            "it as text. Multiple images per prompt are supported.\n\n"
            "Anything you /file, /image, or that the model reads via tools is sent\n"
            "to the Mesh API gateway and the upstream model — including file\n"
            "contents, screenshots, and shell output. Web searches send the query\n"
            "to the gateway's search provider. Don't attach secrets.\n"
            "Mode auto-approvals: accept-edits auto-writes inside cwd; auto adds\n"
            "shell commands + web search; bypass auto-approves everything (still\n"
            "asks before writing to ~/.ssh, /etc, rm -rf, sudo, curl|sh, etc.).[/dim]",
            title="commands",
            border_style="cyan",
        ))
    else:
        console.print(f"[red]Unknown command: {name}. Type /help[/red]")
    return True
