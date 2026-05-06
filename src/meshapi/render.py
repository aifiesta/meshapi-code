"""Rich-based markdown live rendering and shared formatters."""
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

console = Console()


def _detect_theme() -> str:
    """Returns 'dark' or 'light'. Override with MESHAPI_THEME=light|dark."""
    forced = os.environ.get("MESHAPI_THEME", "").strip().lower()
    if forced in ("light", "dark"):
        return forced
    # COLORFGBG is set by Konsole, urxvt, some VS Code configs. Format: "fg;bg".
    parts = os.environ.get("COLORFGBG", "").strip().split(";")
    if len(parts) >= 2:
        try:
            return "dark" if int(parts[-1]) < 8 else "light"
        except ValueError:
            pass
    return "dark"  # most devs use dark; safe default


# Brand palette — Mesh API purple, theme-adaptive
BRAND = "#6f5af5"  # foreground brand, same on both themes
if _detect_theme() == "dark":
    BRAND_DIM = "#9d92e8"   # lighter dim — visible on dark bg
    BRAND_BG = "#2d2454"    # darker, brand-tinted highlight against ~#000-#1e1e1e
else:
    BRAND_DIM = "#5a4ec4"   # darker dim — visible on light bg
    BRAND_BG = "#ebe4fc"    # pale lavender highlight against white


def fmt_usd(value) -> str:
    """USD formatter matching dashboard `fmtUsd` (routersvc-client/src/lib/utils.ts).

    Always 6 decimals; K/M abbreviations for large values. Never use raw f-string
    rounding for money — `999.999833` would render `$1000.00`.
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "$0.000000"
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"${n / 1_000:.2f}K"
    return f"${n:.6f}"


def pretty_cwd() -> str:
    """Show cwd as `~/relative` if under $HOME, else absolute."""
    cwd = Path.cwd()
    home = Path.home()
    try:
        return f"~/{cwd.relative_to(home)}"
    except ValueError:
        return str(cwd)


class _StreamView:
    """Renderable: meshing-around spinner + elapsed timer above streamed markdown."""

    def __init__(self) -> None:
        self.start = time.monotonic()
        self.buf = ""
        self.first_token_at: Optional[float] = None
        self.done = False
        self._spinner = Spinner("dots", style=BRAND)

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def __rich_console__(self, console, options):
        if self.done:
            if self.buf:
                yield Markdown(self.buf)
            return
        label = "meshing around" if not self.buf else "still meshing"
        self._spinner.text = Text(f"{label}... {self.elapsed():.1f}s", style=BRAND_DIM)
        if self.buf:
            yield Markdown(self.buf)
            yield self._spinner
        else:
            yield self._spinner


def render_stream(events: Iterable) -> tuple[str, dict]:
    """Live-render streamed content with a meshing-around spinner + timer.

    Returns (full_text, metadata). Generator yields strings (content deltas)
    and an optional final dict (usage + cost + model from the SSE tail).
    `meta['elapsed']` and `meta['ttft']` (time-to-first-token) are added on
    the way out.
    """
    view = _StreamView()
    meta: dict = {}
    with Live(view, console=console, refresh_per_second=12, auto_refresh=True) as live:
        for event in events:
            if isinstance(event, str):
                if view.first_token_at is None:
                    view.first_token_at = view.elapsed()
                view.buf += event
            elif isinstance(event, dict):
                meta.update(event)
        view.done = True
        live.refresh()
    meta["elapsed"] = view.elapsed()
    if view.first_token_at is not None:
        meta["ttft"] = view.first_token_at
    return view.buf, meta
