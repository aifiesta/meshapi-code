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
# Two color tracks:
#   BRAND* (purple)   — Mesh/AI actions: tool invocations, plan, costs, banner
#   CODE   (cyan)     — code & shell content: file bodies, commands, output
# Keeping these visually distinct makes the transcript scannable: at a glance
# you can tell whether a line is "the model is doing something" vs "this is the
# actual code/command being run."
if _detect_theme() == "dark":
    BRAND = "#8b78f7"       # bumped lighter on dark — official #6f5af5 reads dim on dark wine/black
    BRAND_DIM = "#aea3f0"   # lighter dim — clearly visible on dark backgrounds
    BRAND_BG = "#372d73"    # mid-dark purple — clearly visible without being loud
    BRAND_BG_FG = "#f5f0ff" # near-white with slight purple tint for input text
    CODE = "#7dd3fc"        # sky-300 — code/shell content
else:
    BRAND = "#6f5af5"       # official brand color — strong contrast on white
    BRAND_DIM = "#5a4ec4"   # darker dim — visible on light bg
    BRAND_BG = "#ebe4fc"    # pale lavender highlight against white
    BRAND_BG_FG = "#2c2540" # near-black with purple tint for input text on light theme
    CODE = "#0369a1"        # sky-700 for light theme


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


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class _StreamView:
    """Renderable: phase-aware spinner + elapsed timer above streamed markdown.

    Three phases the user can tell apart at a glance:
      no output yet      → "meshing around... 3.1s"
      text streaming     → "still meshing · ↓ ~1.2k tok · 8.4s"
      tool args streaming→ "preparing write_file (↓ 3.2k chars) · 12.4s"
    The tool phase used to be dead silence — an 8KB write_file argument
    streamed with zero feedback.
    """

    def __init__(self, header: str = "") -> None:
        self.start = time.monotonic()
        self.buf = ""
        self.first_token_at: Optional[float] = None
        self.done = False
        self.header = header  # static turn context: "model · hop N"
        self.tool_name: Optional[str] = None
        self.tool_chars = 0
        self._spinner = Spinner("dots", style=BRAND)

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def note_progress(self, payload: dict) -> None:
        """Feed from client.stream_chat's stream_progress events."""
        if payload.get("tool"):
            self.tool_name = payload["tool"]
        if payload.get("chars"):
            self.tool_chars = payload["chars"]

    def _label(self) -> str:
        if self.tool_chars:
            tool = self.tool_name or "tool call"
            return f"preparing {tool} (↓ {_fmt_k(self.tool_chars)} chars)"
        if self.buf:
            # Rough live estimate; the real count arrives with final usage.
            return f"still meshing · ↓ ~{_fmt_k(max(1, len(self.buf) // 4))} tok"
        return "meshing around"

    def __rich_console__(self, console, options):
        if self.done:
            # Transcript stays clean: the header/spinner are live-only.
            if self.buf:
                yield Markdown(self.buf)
            return
        if self.header:
            yield Text(f"✦ {self.header}", style=BRAND_DIM)
        self._spinner.text = Text(f"{self._label()} · {self.elapsed():.1f}s", style=BRAND_DIM)
        if self.buf:
            yield Markdown(self.buf)
            yield self._spinner
        else:
            yield self._spinner


def render_stream(events: Iterable, header: str = "") -> tuple[str, dict]:
    """Live-render streamed content with a phase-aware spinner + timer.

    `header` is a static turn-context line ("model · hop 3") shown above
    the stream while it's live; it does not persist into the transcript.
    Returns (full_text, metadata). Generator yields strings (content deltas)
    and an optional final dict (usage + cost + model from the SSE tail).
    `meta['elapsed']` and `meta['ttft']` (time-to-first-token) are added on
    the way out.
    """
    view = _StreamView(header)
    meta: dict = {}
    with Live(view, console=console, refresh_per_second=12, auto_refresh=True) as live:
        for event in events:
            if isinstance(event, str):
                if view.first_token_at is None:
                    view.first_token_at = view.elapsed()
                view.buf += event
            elif isinstance(event, dict):
                if "stream_progress" in event:
                    # Spinner feed only — never merged into meta.
                    view.note_progress(event["stream_progress"] or {})
                    continue
                meta.update(event)
        view.done = True
        live.refresh()
    meta["elapsed"] = view.elapsed()
    if view.first_token_at is not None:
        meta["ttft"] = view.first_token_at
    return view.buf, meta
