"""Slash command handlers."""
import contextlib
import shutil
from pathlib import Path

import httpx
from rich.panel import Panel

from . import memory

from .attachments import AttachmentError, load_image
from .config import CREDENTIALS_FILE, save_api_key, save_config
from .permissions import LABELS, Mode, from_str
from .render import CODE, console, fmt_usd
from .tools import build_system_prompt

_REASONING_LEVELS = ("high", "medium", "low", "none")


def _known_model_ids(state: dict) -> "set | None":
    """Catalog ids for validation, or None when unreachable (offline)."""
    models = fetch_models_quiet(state)
    if models is None:
        return None
    return {m.get("id") for m in models if isinstance(m, dict) and m.get("id")}


def _model_suggestions(query: str, known: set, n: int = 3) -> list:
    """Top-n fuzzy matches for an unknown model id."""
    from .completer import _ranked  # lazy: completer imports this module
    try:
        return _ranked(query, sorted(k for k in known if k))[:n]
    except Exception:
        return []


def _local_route_preview(state: dict) -> None:
    """What OUR router would do with the last user message — free, instant."""
    from . import router as _router
    last = next((m for m in reversed(state.get("messages") or [])
                 if m.get("role") == "user" and isinstance(m.get("content"), str)),
                None)
    if not last:
        console.print("[dim]No conversation yet — send a prompt first.[/dim]")
        return
    text = last["content"]
    cohort, _ = _router.classify(text, has_tools=True)
    effort = state["cfg"].get("route_effort", "auto")
    level = effort if effort != "auto" else _router.estimate_difficulty(text)
    w = _router.effective_weights(state["cfg"].get("route_weights"), level)
    table = _router.load_table()
    catalog = state.get("models_cache") or fetch_models_quiet(state)
    got = _router.pick(cohort, w, table, catalog, needs_tools=True,
                       exclude=state.get("_smart_bad"))
    if got:
        console.print(
            f"[dim]local smart pick: {cohort}/{level} → "
            f"[bold]{got['model']}[/bold] (free, no request made)[/dim]"
        )
    else:
        console.print("[dim]local smart pick: unavailable — pinned model would ride.[/dim]")


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


def fetch_models_quiet(state: dict) -> "list | None":
    """Catalog fetch for tab-completion: session-cached, SILENT on every
    failure (a completion popup must never print errors into the prompt).
    `_fetch_models` below stays the loud, user-facing variant for /models."""
    cached = state.get("models_cache")
    if cached is not None:
        return cached
    try:
        cfg = state["cfg"]
        r = httpx.get(
            f"{cfg['base_url']}/models",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=10,
        )
        if r.status_code >= 400:
            return None
        data = r.json()
        models = data.get("data") if isinstance(data, dict) else data
        if isinstance(models, list):
            state["models_cache"] = models
            return models
    except Exception:
        return None
    return None


# The gateway hard-rejects anything outside this set (HTTP 422
# literal_error), so "xhigh"/"max" cannot be passed through — they are
# mapped to the nearest real level instead of failing every request.
_REASONING_ALIASES = {
    "xhigh": "high", "x-high": "high", "extra-high": "high",
    "extra high": "high", "max": "high", "maximum": "high",
    "minimal": "low", "min": "low", "medium-high": "high", "mid": "medium",
}


def _set_effort(state: dict, level: str) -> None:
    from . import router as _router
    state["cfg"]["route_effort"] = level
    save_config(state["cfg"])
    if level == "auto":
        console.print("[dim]Effort auto — difficulty detected per prompt.[/dim]")
    else:
        w = _router.effective_weights(state["cfg"].get("route_weights"), level)
        console.print(
            f"[dim]Effort {level} — every prompt now weighs capability at "
            f"{w['cap']:.0%}. /effort auto to return to per-prompt "
            "detection.[/dim]"
        )


def _set_style(state: dict, value: str) -> None:
    """Persist an output style AND apply it to the live session.

    The system prompt is built once at session start, so writing config
    alone would leave the running turn on the old style — the setting would
    look applied and do nothing until /clear. Rebuild message[0] in place.
    """
    from . import styles as _styles
    canon = _styles.normalize(value)
    if canon is None:
        console.print(
            f"[red]Unknown output style {value!r}. Options: "
            f"{', '.join(_styles.ORDER)}[/red]")
        return
    state["cfg"]["output_style"] = canon
    save_config(state["cfg"])
    msgs = state.get("messages") or []
    if msgs and msgs[0].get("role") == "system":
        msgs[0]["content"] = build_system_prompt(state["cfg"])
    console.print(
        f"[dim]Output style {_styles.label(canon)} — "
        f"{_styles.describe(canon)}.[/dim]")


def _fmt_weights(w: dict, effort: str = "auto") -> str:
    eff = "effort=auto (per-prompt)" if effort == "auto" else f"effort={effort}"
    return (f"cost={w['cost']:.2f} cap={w['cap']:.2f} "
            f"speed={w['speed']:.2f} · {eff}")


def warn_reasoning_unsupported(state: dict) -> None:
    """Note when reasoning effort is set but this model has already rejected it.

    Intentionally evidence-based: the catalog's `supports_thinking` is False
    even for gpt-5.4 / sonnet-4.6 / opus-4.8, which DO accept the field
    (verified live), so warning off that flag would be wrong far more often
    than right. We only speak up about a model we have actually watched
    reject it this session; otherwise the CLI just sends it and lets the
    retry-without-it net handle a rejection.
    """
    effort = state["cfg"].get("reasoning_effort")
    if not effort:
        return
    model_id = state["cfg"].get("model")
    if model_id in (state.get("_reasoning_rejected") or set()):
        console.print(
            f"[yellow]⚠ {model_id} rejected reasoning effort earlier this "
            "session — it will be sent without it (your setting is kept for "
            "models that accept it).[/yellow]"
        )


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


def resolve_command(name: str) -> "tuple[str | None, list]":
    """Resolve a possibly-abbreviated command to its full name.

    Exact match wins outright (so /mode never resolves to /model). Otherwise
    a UNIQUE prefix resolves ("/eff" -> "/effort"); an ambiguous prefix
    returns the candidates for a helpful message. (None, []) = unknown.
    """
    from .completer import COMMANDS  # lazy: completer imports this module
    known = set(COMMANDS) | {"/exit", "/quit", "/q", "/effort", "/output"}
    if name in known:
        return name, [name]
    matches = sorted(c for c in known if c.startswith(name))
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def handle_command(cmd: str, state: dict) -> bool:
    """Handle slash commands. Returns True if app should continue.

    Abbreviations welcome: any unique prefix runs the command ("/eff high"
    == "/effort high"); ambiguous prefixes list their candidates.
    """
    parts = cmd.strip().split(maxsplit=1)
    name = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    resolved, matches = resolve_command(name)
    if resolved is None:
        if matches:
            console.print(
                f"[yellow]{name} is ambiguous:[/yellow] [dim]"
                + "  ".join(matches) + "[/dim]"
            )
            return True
        # fall through: the unknown-command message at the end handles it
    elif resolved != name:
        console.print(f"[dim]→ {resolved}[/dim]")
        name = resolved

    if name in ("/exit", "/quit", "/q"):
        return False

    if name == "/clear":
        state["messages"] = [{"role": "system", "content": build_system_prompt(state["cfg"])}]
        state["session_cost"] = 0.0
        state["session_reads"] = {}  # history is gone — dedupe must not lie
        state["plan"] = None  # a plan from the wiped conversation must not haunt new turns
        console.print("[dim]Conversation cleared.[/dim]")

    elif name == "/model":
        if arg:
            if arg.strip().lower() == "auto":
                # "auto" isn't a catalog id, so the validation below would reject
                # it with a confusing "Unknown model: auto" — but the user
                # clearly wants the gateway to pick per prompt, which is
                # /route auto. Do that instead of rejecting a real directive.
                state["cfg"]["auto_route"] = True
                save_config(state["cfg"])
                console.print(
                    "[dim]Auto-routing on — the gateway picks a model per prompt "
                    "(same as [bold]/route auto[/bold]; [bold]/route off[/bold] "
                    "to pin a specific model again).[/dim]"
                )
                return True
            # Validate against the live catalog BEFORE persisting — an
            # unknown model written to config.json breaks every future
            # launch, and the error only surfaces on the next prompt
            # (external tester report). Offline → set with a warning
            # (can't hard-block users without network).
            known = _known_model_ids(state)
            if known is not None and arg not in known:
                console.print(f"[red]Unknown model: {arg}[/red]")
                sugg = _model_suggestions(arg, known)
                if sugg:
                    console.print(f"[dim]Did you mean: {', '.join(sugg)}?  (/models to browse)[/dim]")
                else:
                    console.print("[dim]/models to browse the catalog[/dim]")
            else:
                state["cfg"]["model"] = arg
                save_config(state["cfg"])
                note = "" if known is not None else " (couldn't verify against the catalog — offline?)"
                console.print(f"[dim]Model set to {arg}{note}[/dim]")
                # An explicit /model is the user overriding routing — honor it
                # for the in-flight turn (mid-run steer) and say clearly what
                # persists. Silent overrides are the most confusing state in
                # the CLI.
                if state["cfg"].get("route_mode") == "smart":
                    state["_smart_pick"] = None      # this turn: your model
                    state["_smart_last"] = None      # don't sticky the old pick
                    console.print(
                        f"[yellow]⚠ smart routing is on — {arg} applies to the "
                        "current turn, but the router picks again on the next "
                        "prompt. [bold]/route off[/bold] to pin it for "
                        "good.[/yellow]"
                    )
                if state["cfg"].get("auto_route"):
                    console.print(
                        f"[yellow]⚠ auto-routing is on — prompts still go to the "
                        f"model the gateway picks per prompt, not {arg}. Run "
                        f"[bold]/route off[/bold] to actually pin {arg}.[/yellow]"
                    )
                # A reasoning_effort left over from a thinking model would
                # make every request to a non-thinking model fail with 400.
                warn_reasoning_unsupported(state)
        else:
            # No arg = show the current pin, and flag when it isn't the model
            # actually being used because auto-routing overrides it.
            if state["cfg"].get("auto_route"):
                console.print(
                    f"[dim]Current model: {state['cfg']['model']} "
                    "[yellow](inactive — auto-routing is on; /route off to use "
                    "it)[/yellow][/dim]"
                )
            else:
                console.print(f"[dim]Current model: {state['cfg']['model']}[/dim]")

    elif name == "/output":
        from . import styles as _styles
        sub = arg.strip()
        if not sub:
            # Interactive picker, same widget as /effort and /route.
            from . import askui as _askui
            import contextlib as _ctx
            watcher = state.get("watcher")
            ctx = watcher.paused() if watcher is not None else _ctx.nullcontext()
            cur = _styles.normalize(state["cfg"].get("output_style")) or _styles.DEFAULT
            try:
                with ctx:
                    status, picked = _askui.slider(
                        "Output style", _styles.options(), current=cur,
                        left_label="Terse", right_label="Teaching")
            except Exception:
                status, picked = "unavailable", None
            if status == "picked":
                if picked != cur:
                    _set_style(state, picked)
                else:
                    console.print(
                        f"[dim]Output style stays {_styles.label(cur)}.[/dim]")
                return True
            if status == "cancelled":
                console.print("[dim]Cancelled.[/dim]")
                return True
            # non-tty: report, don't guess
            console.print(
                f"[dim]Output style {_styles.label(cur)} — "
                f"{_styles.describe(cur)}. Set with /output "
                f"{'|'.join(_styles.ORDER)}.[/dim]")
            return True
        _set_style(state, sub)

    elif name == "/effort":
        # First-class shortcut — effort is a primary control, not a /route
        # footnote. Identical semantics to "/route effort …".
        return handle_command(("/route effort " + arg).strip(), state)

    elif name == "/route":
        sub = arg.strip().lower()
        if not sub:
            # Interactive mode picker (same widget as /effort); non-tty or
            # Esc falls back to the status display below.
            from . import askui as _askui
            import contextlib as _ctx
            watcher = state.get("watcher")
            ctx = watcher.paused() if watcher is not None else _ctx.nullcontext()
            cur_mode = state["cfg"].get("route_mode", "off")
            if cur_mode != "smart" and state["cfg"].get("auto_route"):
                cur_mode = "auto"
            try:
                with ctx:
                    status, picked = _askui.slider(
                        "Route", [
                            ("off", "pin one model (you choose)"),
                            ("auto", "gateway picks (billed classifier)"),
                            ("smart", "local pick — free, weighted"),
                        ], current=cur_mode,
                        left_label="Manual", right_label="Smarter")
            except Exception:
                status, picked = "unavailable", None
            if status == "picked" and picked != cur_mode:
                return handle_command("/route " + picked, state)
            if status == "picked":
                console.print(f"[dim]route stays {picked}.[/dim]")
                return True
            if status == "cancelled":
                console.print("[dim]Cancelled.[/dim]")
                return True
            if state["cfg"].get("route_mode") == "smart":
                from . import router as _router
                w = _router.normalize_weights(state["cfg"].get("route_weights"))
                console.print(
                    f"[dim]route: smart — local pick per prompt · {_fmt_weights(w, state['cfg'].get('route_effort', 'auto'))} "
                    "(/route why after a prompt)[/dim]"
                )
            elif state["cfg"].get("auto_route"):
                console.print(
                    "[dim]route: auto — the gateway picks a model per prompt "
                    f"(pinned: {state['cfg']['model']})[/dim]"
                )
            else:
                console.print(f"[dim]route: off (model: {state['cfg']['model']})[/dim]")
        elif sub == "auto":
            state["cfg"]["auto_route"] = True
            state["cfg"]["route_mode"] = "auto"
            save_config(state["cfg"])
            console.print(
                "[dim]Auto-routing on — each prompt goes to the model the "
                "gateway's router picks. /route off to pin back to "
                f"{state['cfg']['model']}.[/dim]"
            )
        elif sub in ("off", "default"):
            state["cfg"]["auto_route"] = False
            state["cfg"]["route_mode"] = "off"
            save_config(state["cfg"])
            console.print(f"[dim]Auto-routing off — pinned to {state['cfg']['model']}.[/dim]")
        elif sub == "preview":
            if state["cfg"].get("route_mode") == "smart":
                _local_route_preview(state)   # our pick, free
            else:
                _route_preview(state)         # gateway's pick
        elif sub == "smart":
            from . import router as _router
            if _router.load_table() is None:
                console.print(
                    "[yellow]No routing table bundled with this build — smart "
                    "routing unavailable ( /route auto uses the gateway's "
                    "router instead).[/yellow]"
                )
            else:
                state["cfg"]["route_mode"] = "smart"
                state["cfg"]["auto_route"] = False  # mutually exclusive
                save_config(state["cfg"])
                w = _router.normalize_weights(state["cfg"].get("route_weights"))
                console.print(
                    "[dim]Smart routing on — the CLI picks a model per prompt "
                    "locally (no classifier tokens, no extra latency). Weights: "
                    f"{_fmt_weights(w, state['cfg'].get('route_effort', 'auto'))} — "
                    "tune with /route weights, force depth with /route effort.[/dim]"
                )
        elif sub.startswith("weights"):
            from . import router as _router
            pairs = sub.removeprefix("weights").strip()
            if not pairs:
                w = _router.normalize_weights(state["cfg"].get("route_weights"))
                console.print(
                    f"[dim]weights: {_fmt_weights(w, state['cfg'].get('route_effort', 'auto'))}\n"
                    "usage: /route weights cost=0.5 cap=0.3 speed=0.2\n"
                    "       /route effort auto|low|medium|high|xhigh|max[/dim]"
                )
            else:
                try:
                    parsed = {}
                    for tok in pairs.replace(",", " ").split():
                        k, v = tok.split("=", 1)
                        k = {"capability": "cap", "quality": "cap"}.get(k, k)
                        if k not in ("cost", "cap", "speed"):
                            raise ValueError(f"unknown axis {k!r} "
                                             "(effort has its own command: /route effort)")
                        parsed[k] = float(v)
                except (ValueError, TypeError) as e:
                    console.print(f"[red]Couldn't parse weights ({e}). "
                                  "Example: /route weights cost=0.5 cap=0.3 speed=0.2[/red]")
                else:
                    merged = {**state["cfg"].get("route_weights", {}), **parsed}
                    state["cfg"]["route_weights"] = _router.normalize_weights(merged)
                    save_config(state["cfg"])
                    w = state["cfg"]["route_weights"]
                    console.print(
                        f"[dim]weights set: {_fmt_weights(w, state['cfg'].get('route_effort', 'auto'))}. Applies from "
                        "the next prompt.[/dim]"
                    )
        elif sub.startswith("effort"):
            from . import router as _router
            level = sub.removeprefix("effort").strip()
            if not level:
                # Interactive slider (Claude Code-style). Falls back to the
                # plain help on non-tty terminals or when cancelled.
                from . import askui as _askui
                opts = [("auto", "detects per prompt"),
                        ("low", "cheapest competent"),
                        ("medium", "balanced"),
                        ("high", "strong models"),
                        ("xhigh", "frontier"),
                        ("max", "best, cost no object")]
                watcher = state.get("watcher")
                import contextlib as _ctx
                ctx = watcher.paused() if watcher is not None else _ctx.nullcontext()
                cur = state["cfg"].get("route_effort", "auto")
                try:
                    with ctx:
                        status, picked = _askui.slider(
                            "Effort", opts, current=cur,
                            left_label="Faster", right_label="Smarter")
                except Exception:
                    status, picked = "unavailable", None
                if status == "picked":
                    _set_effort(state, picked)
                elif status == "cancelled":
                    console.print("[dim]Cancelled.[/dim]")
                else:
                    console.print(
                        f"[dim]effort: {cur}\n"
                        "auto = detect per prompt; a fixed level tilts "
                        "capability for EVERY prompt:\n"
                        "  low: cheapest competent · medium: balanced · high: "
                        "strong models · xhigh: frontier · max: best "
                        "regardless of cost\n"
                        "usage: /route effort auto|low|medium|high|xhigh|max[/dim]"
                    )
            elif level in _router.EFFORT_LEVELS:
                _set_effort(state, level)
            else:
                console.print(
                    f"[red]Unknown effort {level!r}. Use "
                    f"{'|'.join(_router.EFFORT_LEVELS)}[/red]"
                )
        elif sub == "why":
            info = state.get("_smart_pick_info")
            if not info:
                console.print("[dim]No smart pick this session yet — /route smart "
                              "then send a prompt.[/dim]")
            else:
                sticky = " (kept from earlier in the session)" if info.get("sticky") else ""
                diff = info.get("difficulty")
                dtxt = f"  ·  difficulty: {diff}" if diff else ""
                console.print(
                    f"[dim]cohort: {info['cohort']}{dtxt}  →  picked "
                    f"[bold]{info['model']}[/bold]{sticky}[/dim]"
                )
                if diff == "low":
                    console.print("[dim]    (easy prompt — cost weighted up, capability down)[/dim]")
                elif diff == "high":
                    console.print("[dim]    (hard prompt — capability weighted up)[/dim]")
                for r in info.get("ranked") or []:
                    console.print(
                        f"[dim]    {r['model']:40} score {r['score']:>6}  "
                        f"cap {r['cap']:>3}  ${r['cost']:<7} speed {r['speed']}[/dim]"
                    )
        else:
            console.print("[red]Usage: /route auto | smart | off | preview | "
                          "weights [k=v …] | effort [level] | why[/red]")

    elif name == "/models":
        models = _fetch_models(state)
        if models is not None:
            q = arg.strip().lower()
            if not q:
                subset, title = models, f"models ({len(models)})"
            elif q == "free":
                subset = [m for m in models if m.get("is_free")]
                title = f"free models ({len(subset)})"
            elif q in ("thinking", "reasoning"):
                # Capability filter, not a name match — these are the models
                # /reasoning can actually be sent to.
                subset = [
                    m for m in models
                    if m.get("supports_thinking") and m.get("supports_completions_api")
                ]
                title = f"thinking models ({len(subset)})"
            elif q == "tools":
                subset = [m for m in models if m.get("supports_tools")]
                title = f"tool-calling models ({len(subset)})"
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
                    if state["cfg"].get("auto_route")
                    else ("  [yellow](smart routing is on — the gateway may "
                          "fall back past the router's pick)[/yellow]"
                          if state["cfg"].get("route_mode") == "smart" else "")
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
            # Reject unknown models when the catalog is reachable — a
            # persisted bogus fallback silently breaks failover exactly
            # when it's needed. Offline → keep with a warning.
            known = _known_model_ids(state)
            if known is not None:
                missing = [w for w in wanted if w not in known]
                if missing:
                    console.print(f"[red]Unknown model(s): {', '.join(missing)}[/red]")
                    sugg = _model_suggestions(missing[0], known)
                    if sugg:
                        console.print(f"[dim]Did you mean: {', '.join(sugg)}?  (/models to browse)[/dim]")
                    console.print("[dim]Fallback list unchanged.[/dim]")
                    return True
            else:
                console.print(
                    "[yellow]⚠ couldn't verify against the catalog (offline?) "
                    "— setting anyway[/yellow]"
                )
            state["cfg"]["fallback_models"] = wanted
            save_config(state["cfg"])
            console.print(f"[dim]Fallback order: {' → '.join(wanted)}[/dim]")

    elif name == "/reasoning":
        v = arg.strip().lower()
        if not v:
            cur = state["cfg"].get("reasoning_effort")
            console.print(
                f"[dim]reasoning effort: {cur or 'off'}\n"
                f"usage: /reasoning {'|'.join(_REASONING_LEVELS)}|off\n"
                "higher effort = more thinking tokens (better on hard "
                "problems, slower and pricier). Only sent to models that "
                "support thinking.[/dim]"
            )
        elif v == "off":
            state["cfg"]["reasoning_effort"] = None
            save_config(state["cfg"])
            console.print("[dim]Reasoning effort off — not sent with requests.[/dim]")
        elif v in _REASONING_LEVELS or v in _REASONING_ALIASES:
            mapped = _REASONING_ALIASES.get(v, v)
            state["cfg"]["reasoning_effort"] = mapped
            save_config(state["cfg"])
            if mapped != v:
                console.print(
                    f"[yellow]The gateway accepts only "
                    f"{'|'.join(_REASONING_LEVELS)} — '{v}' mapped to "
                    f"'{mapped}'.[/yellow]"
                )
            else:
                console.print(f"[dim]Reasoning effort set to {mapped}.[/dim]")
            warn_reasoning_unsupported(state)
        else:
            console.print(
                f"[red]Usage: /reasoning {'|'.join(_REASONING_LEVELS)}|off[/red]"
            )

    elif name == "/memory":
        root = state.get("memory_root") or Path.cwd().resolve()
        sub = arg.strip().lower()
        if not sub:
            store = memory.load_store(root)
            notes = memory.load_notes(root)
            n_files = len((store or {}).get("files", {}))
            n_notes = len([l for l in notes.splitlines() if l.strip().startswith("-")])
            enabled = state["cfg"].get("repo_memory", True)
            console.print(
                f"[dim]repo memory: {'on' if enabled else 'off'} — "
                f"{n_files} file(s) mapped, {n_notes} note(s) for this directory\n"
                f"store: {memory.context_dir(root)}\n"
                "usage: /memory notes | clear | on | off[/dim]"
            )
        elif sub == "notes":
            notes = memory.load_notes(root).strip()
            console.print(notes if notes else "[dim]no notes yet[/dim]")
        elif sub == "clear":
            shutil.rmtree(memory.context_dir(root), ignore_errors=True)
            state["session_reads"] = {}
            console.print("[dim]Repo memory for this directory deleted.[/dim]")
        elif sub in ("on", "off"):
            state["cfg"]["repo_memory"] = sub == "on"
            save_config(state["cfg"])
            console.print(
                f"[dim]repo memory {sub} — takes effect on the next "
                "session or /clear.[/dim]"
            )
        else:
            console.print("[red]Usage: /memory [notes | clear | on | off][/red]")

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
        # Guard every step: `/file` with no arg used to resolve Path("")
        # to "." (the cwd), pass exists(), and crash the whole CLI on
        # read_text() — PermissionError on Windows, IsADirectoryError on
        # Linux/macOS (external user report, cross-platform).
        if not arg:
            console.print("[dim]/file <path>  add a text file to context[/dim]")
        else:
            path = Path(arg).expanduser()
            if not path.is_file():
                if path.is_dir():
                    console.print(f"[red]Not a file (it's a directory): {path}[/red]")
                else:
                    console.print(f"[red]File not found: {path}[/red]")
            else:
                try:
                    size = path.stat().st_size
                    if size > 2_000_000:
                        console.print(
                            f"[red]Too large ({size // 1_000_000} MB): /file caps at "
                            "2 MB — a bigger file would flood the model's context. "
                            "Ask the model to read the parts it needs instead.[/red]"
                        )
                    else:
                        content = path.read_text()
                        state["messages"].append({
                            "role": "user",
                            "content": f"File: {path.name}\n\n```\n{content}\n```",
                        })
                        console.print(
                            f"[dim]Added {path.name} ({len(content)} chars) to context[/dim]"
                        )
                except (OSError, UnicodeDecodeError) as e:
                    console.print(f"[red]Can't read {path}: {e}[/red]")

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
            state["session_reads"] = {}  # history is gone — dedupe must not lie
            console.print("[dim]System prompt updated and conversation reset.[/dim]")
        else:
            console.print(f"[dim]{state['cfg']['system']}[/dim]")

    elif name == "/cost":
        spend = state.get("session_cost", 0)
        tilde = "~" if state.get("cost_estimated") else ""
        console.print(f"[dim]Session spend: {tilde}{fmt_usd(spend)}[/dim]")
        if state.get("cost_estimated"):
            console.print(
                "[dim]  (computed from token usage × the catalog's own per-1M "
                "rates — the gateway does not return a cost field)[/dim]"
            )
        # Authoritative balance straight from the gateway, when reachable.
        try:
            cfg = state["cfg"]
            r = httpx.get(
                f"{cfg['base_url'].rstrip('/')}/balance",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=10,
            )
            if r.status_code < 400:
                b = r.json()
                console.print(
                    f"[dim]Account balance: ${float(b.get('available_usd', 0)):.4f} "
                    f"available[/dim]"
                )
        except Exception:
            pass

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

    elif name == "/hops":
        if not arg:
            cur = int(state["cfg"].get("max_hops") or 0)
            label = "unlimited" if cur == 0 else str(cur)
            console.print(
                f"[dim]hop limit: {label}"
                + (" (stall detection still stops runaway loops)" if cur == 0 else "")
                + "\nusage: /hops <n>   pause each turn after n tool hops\n"
                "       /hops off   unlimited (default)[/dim]"
            )
        else:
            raw = arg.strip().lower()
            try:
                value = 0 if raw in ("off", "0") else int(raw)
            except ValueError:
                console.print("[red]Not a number. Use a positive integer, or 'off'.[/red]")
            else:
                if value < 0:
                    console.print("[red]Hop limit can't be negative. Use a positive integer or 'off'.[/red]")
                else:
                    state["cfg"]["max_hops"] = value
                    save_config(state["cfg"])
                    if value:
                        console.print(
                            f"[dim]hop limit set to {value} — each turn pauses "
                            "after that many tool hops (work is kept; say "
                            "'continue' to resume).[/dim]"
                        )
                    else:
                        console.print(
                            "[dim]hop limit off — turns run until the task is "
                            "done (stall detection still stops runaway "
                            "loops; esc interrupts).[/dim]"
                        )

    elif name == "/compact":
        from . import compact as _compact
        sub = arg.strip().lower()
        if sub in ("", "now"):
            models = fetch_models_quiet(state)
            model_id = state.get("last_model") or state["cfg"].get("model") or ""
            limit = _compact.context_limit(model_id, models)
            if sub == "now":
                rep = _compact.compact_history(state, limit=limit, aggressive=True)
                if rep:
                    console.print(
                        f"[dim]compacted: ~{rep['before_tok'] // 1000}k → "
                        f"~{rep['after_tok'] // 1000}k tok (est) — "
                        f"{rep['truncated']} result(s) truncated, "
                        f"{rep['folded']} run(s) folded[/dim]"
                    )
                else:
                    console.print("[dim]nothing to compact — history is already lean.[/dim]")
            else:
                est = _compact.est_history_tokens(state.get("messages") or [])
                pct = (est / limit * 100) if limit else 0
                auto = "on" if state["cfg"].get("auto_compact", True) else "off"
                console.print(
                    f"[dim]context: ~{est // 1000}k of ~{limit // 1000}k tok "
                    f"(est {pct:.0f}%) · auto-compact {auto}\n"
                    "usage: /compact now          compact history right now\n"
                    "       /compact auto on|off  toggle automatic compaction[/dim]"
                )
        elif sub in ("auto on", "auto off"):
            on = sub.endswith("on")
            state["cfg"]["auto_compact"] = on
            save_config(state["cfg"])
            detail = (
                "on — long turns stay under the model's context limit" if on
                else "off — long turns may hit the context limit and pause"
            )
            console.print(f"[dim]auto-compact {detail}.[/dim]")
        else:
            console.print("[red]Usage: /compact [now|auto on|auto off][/red]")

    elif name == "/stall":
        cur = state["cfg"].get("stall_policy") or "pause"
        if not arg:
            console.print(
                f"[dim]stall policy: {cur}\n"
                "usage: /stall pause       pause the turn when the model repeats "
                "itself despite nudges (default)\n"
                "       /stall keep-going  never pause — keep nudging (unattended "
                "runs; esc still interrupts)[/dim]"
            )
        else:
            raw = arg.strip().lower()
            if raw in ("pause", "keep-going", "keepgoing", "keep_going"):
                value = "pause" if raw == "pause" else "keep-going"
                state["cfg"]["stall_policy"] = value
                save_config(state["cfg"])
                console.print(f"[dim]stall policy set to {value}.[/dim]")
            else:
                console.print("[red]Usage: /stall [pause|keep-going][/red]")

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
            "/route auto|smart|off      routing: gateway auto, local smart pick, or pinned\n"
            "/route weights k=v | why   tune smart weights (cost/cap/speed); explain the pick\n"
            "/fallback <m1> <m2> | off  ordered fallback models if the primary fails\n"
            "/reasoning <level>         high|medium|low|none|off reasoning effort\n"
            "/mode <perm>               default|accept-edits|auto|bypass  (or shift+tab)\n"
            "/file <path>               add text file to context\n"
            "/image <path|url>          attach an image (base64) to the next prompt\n"
            "/clear-attach              drop any queued image attachments\n"
            "/system <txt>              set system prompt\n"
            "/cost                      show session spend\n"
            "/effort <auto|low..max>    routing depth (how strong a model to pick)\n"
            "/hops <n|off>              pause turns after n tool hops (off = unlimited)\n"
            "/compact [now|auto on|off] context usage + history compaction\n"
            "/stall pause|keep-going    what to do when the model repeats itself\n"
            "/output [concise|default|explanatory|learning]  how answers are written\n"
            "/optimize <dial>           token savings, beta: 0 off, up to 0.95\n"
            "/memory [notes|clear|on|off]  repo memory: map + notes from past sessions\n"
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
            "asks before writing to ~/.ssh, /etc, rm -rf, sudo, curl|sh, etc.).\n"
            "At any approval prompt, answer 'a' to auto-approve that tool for\n"
            "the rest of the session. shift+tab works mid-run too.[/dim]",
            title="commands",
            border_style="cyan",
        ))
    else:
        console.print(f"[red]Unknown command: {name}. Type /help[/red]")
    return True
