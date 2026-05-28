"""Mode-line rendering — a single inline right-aligned line printed at the
moments where the user actually looks: above each prompt and once after each
batch of tool calls.

This file used to maintain a sticky bottom bar via a DEC scroll region. That
fought with rich.Live (whose auto-refresh emits erase-to-end-of-screen and
ignores scroll regions) and with prompt_toolkit's multi-line input (which
doesn't know about scroll regions at all). Across enough terminals — VS Code
xterm.js especially — the result was unreliable: literal escape codes
appearing in scrollback, content fading mid-prompt, etc. The pragmatic
replacement here scrolls with the conversation but is always present at the
point the user can act on it.
"""
from rich.text import Text

from .permissions import LABELS, Mode
from .render import console


def print_line(state: dict) -> None:
    """Render a single right-aligned mode line, color-coded by current mode."""
    m = state.get("mode")
    if m is None:
        return
    if m == Mode.BYPASS:
        color = "red"
    elif m == Mode.NONE:
        color = "yellow"
    else:
        color = "green"
    text = Text()
    text.append(f"▶▶ {LABELS[m]}", style=f"bold {color}")
    text.append("  (shift+tab to cycle)", style="dim")
    try:
        console.print(text, justify="right")
    except Exception:
        pass
