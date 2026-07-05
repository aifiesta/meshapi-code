"""Startup update check against PyPI + interactive upgrade offer.

Design: poll, never push. A daemon thread fetches the latest version and
only mutates `update_state` + the cache file — it never prints or prompts.
The REPL consumes the result at safe points (before the watcher starts, or
at the top of the prompt loop) via `maybe_offer`, so the y/n prompt can
never collide with prompt_toolkit or a streaming response.

Cross-platform notes (the 0.4.5 SIGHUP lesson): nothing in this module may
be POSIX-only. On Windows we never run the upgrade in-process — the running
`meshapi.exe` console-script shim is file-locked by the OS, so pip/pipx
would fail with WinError 5; we print the command and tell the user to exit
first instead.
"""
import contextlib
import os
import subprocess
import sys
import threading
import time

import httpx

from . import __version__
from .config import load_update_check, save_update_check
from .render import console

PACKAGE = "meshapi-code"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"


def parse_version(s: str) -> tuple:
    """"0.4.10" -> (0, 4, 10); "0.5.0rc1" -> (0, 5, 0); garbage -> ().

    Numeric prefix per dot-segment, stopping at the first segment with no
    leading digits — enough for PyPI release ordering without a dependency.
    """
    parts = []
    for seg in str(s).strip().split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """True iff `latest` is strictly newer. False when either is garbage,
    so a parse failure can never produce a bogus upgrade nag."""
    lt, ct = parse_version(latest), parse_version(current)
    if not lt or not ct:
        return False
    return lt > ct


def fetch_latest(timeout: float = 5.0) -> "str | None":
    """Latest published version from PyPI, or None on any failure."""
    try:
        r = httpx.get(PYPI_URL, timeout=timeout)
        if r.status_code != 200:
            return None
        version = r.json()["info"]["version"]
        return version if isinstance(version, str) else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def start_background_check(update_state: dict) -> None:
    """Fire the PyPI check on a daemon thread. Never blocks, never raises,
    never prints — writes `update_state["latest"]` and refreshes the cache
    (preserving declined_version) for next launch, then sets `done`."""

    def _worker() -> None:
        try:
            latest = fetch_latest()
            if latest:
                update_state["latest"] = latest
                cache = load_update_check()
                cache["latest"] = latest
                cache["checked_at"] = int(time.time())
                save_update_check(cache)
        except Exception:
            pass  # best-effort: an update check must never hurt the session
        finally:
            update_state["done"].set()

    threading.Thread(
        target=_worker, daemon=True, name="meshapi-update-check"
    ).start()


def detect_upgrade_command() -> tuple:
    """(label, argv) for the upgrade command matching how meshapi was
    installed. Normalizes sys.prefix so Windows backslash/mixed-case paths
    (C:\\Users\\x\\pipx\\venvs\\...) match the same as POSIX."""
    prefix = sys.prefix.replace("\\", "/").lower()
    if "pipx/venvs" in prefix:
        return "pipx", ["pipx", "upgrade", PACKAGE]
    if "uv/tools" in prefix:
        return "uv", ["uv", "tool", "upgrade", PACKAGE]
    return "pip", [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]


def run_upgrade() -> bool:
    """Run the detected upgrade command. Returns True on success.

    Windows: print the command instead of running it — the live
    `meshapi.exe` shim is file-locked, so an in-process upgrade fails with
    WinError 5. POSIX: run it with inherited stdio so progress streams live.
    """
    label, argv = detect_upgrade_command()
    cmd_str = " ".join(argv)
    if os.name == "nt":
        console.print(
            f"[yellow]Windows can't upgrade meshapi while it's running "
            f"(the executable is locked). Exit meshapi, then run:[/yellow]\n"
            f"  [bold]{cmd_str}[/bold]"
        )
        return False
    console.print(f"[dim]$ {cmd_str}[/dim]")
    try:
        proc = subprocess.run(argv)
    except (FileNotFoundError, OSError) as e:
        console.print(
            f"[red]Couldn't run {label} ({e}). Upgrade manually:[/red]\n"
            f"  [bold]{cmd_str}[/bold]"
        )
        return False
    if proc.returncode != 0:
        hint = (
            "\n[dim]If pipx complained about a missing uv backend, try: "
            f"pipx upgrade {PACKAGE} --backend pip[/dim]"
            if label == "pipx"
            else ""
        )
        console.print(
            f"[red]Upgrade exited with code {proc.returncode}. "
            f"Run it manually:[/red]\n  [bold]{cmd_str}[/bold]{hint}"
        )
        return False
    return True


def offer_update(latest: str, watcher=None) -> None:
    """Announce `latest` and ask y/n. Yes -> upgrade + 'restart meshapi'.
    No (or EOF/Ctrl-C) -> remember the declined version so this release
    never nags again. `watcher` is the KeyWatcher, paused around input."""
    console.print(
        f"[bold cyan]⬆ meshapi {latest} available[/bold cyan] "
        f"[dim](you have {__version__})[/dim]"
    )
    ctx = watcher.paused() if watcher is not None else contextlib.nullcontext()
    try:
        with ctx:
            ans = console.input(
                "[bold]upgrade now?[/bold] y (yes) / n (no)  › "
            ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        ans = "n"
    if ans in ("y", "yes"):
        if run_upgrade():
            console.print(
                f"[green]✓ upgraded to {latest} — restart meshapi to pick "
                "it up.[/green]"
            )
    else:
        cache = load_update_check()
        cache["declined_version"] = latest
        save_update_check(cache)
        console.print(
            f"[dim]Skipping {latest}. Run /update any time to upgrade.[/dim]"
        )


def maybe_offer(update_state: dict, watcher=None) -> None:
    """The single consume point for the background check's result.

    Offers at most once per session, only for a strictly newer version the
    user hasn't already declined. Safe to call every prompt-loop turn.
    """
    if update_state.get("prompted"):
        return
    latest = update_state.get("latest")
    if not latest or not is_newer(latest, __version__):
        return
    if latest == update_state.get("declined"):
        return
    update_state["prompted"] = True
    offer_update(latest, watcher=watcher)
