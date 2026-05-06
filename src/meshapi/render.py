"""Rich-based markdown live rendering and shared formatters."""
from typing import Iterable

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

console = Console()


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


def render_stream(events: Iterable) -> tuple[str, dict]:
    """Live-render markdown content; return (full_text, metadata).

    Generator yields strings (content deltas) and an optional final dict
    (usage + cost from Mesh API's last SSE chunk).
    """
    buf = ""
    meta: dict = {}
    with Live(console=console, refresh_per_second=20) as live:
        for event in events:
            if isinstance(event, str):
                buf += event
                live.update(Markdown(buf))
            elif isinstance(event, dict):
                meta.update(event)
    return buf, meta
