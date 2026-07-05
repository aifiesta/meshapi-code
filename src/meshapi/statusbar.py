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
from prompt_toolkit.formatted_text import FormattedText
from rich.text import Text

from .permissions import LABELS, Mode, SHOW_ESC_HINT
from .render import console


# prompt_toolkit fg colors per mode. Mirrors the rich colors in print_line so
# the toolbar (shown live while the prompt is focused) matches the scrollback
# line (shown between tool hops).
_PT_COLOR = {
    Mode.BYPASS: "ansired",
    Mode.AUTO: "ansiyellow",
    Mode.ACCEPT_EDITS: "ansicyan",
    Mode.DEFAULT: "ansigreen",
}


def bottom_toolbar(state: dict):
    """prompt_toolkit bottom-toolbar: the live mode indicator under the input.

    Unlike `print_line` (a one-shot scrollback line), this is re-evaluated on
    every render, so pressing shift+tab — which calls `event.app.invalidate()`
    — repaints it immediately. That's what makes the mode visibly change while
    you're at the prompt.

    Right-aligned to match the mockup, with a trailing blank line for bottom
    padding. DEFAULT mode shows a dim "default mode" so cycling is still
    visible (print_line stays silent in DEFAULT to keep the transcript clean,
    but here we want the toggle to read as a live control).
    """
    m = state.get("mode")
    label_text = LABELS.get(m, "") if m is not None else ""
    if label_text:
        body = f"⏵⏵ {label_text}"
        color = _PT_COLOR.get(m, "ansigreen")
    else:
        body = "default mode"
        color = "ansibrightblack"
    try:
        from prompt_toolkit.application import get_app

        cols = get_app().output.get_size().columns
    except Exception:
        cols = 80

    # body width (⏵⏵ render double-width, so +2 over len) plus a 3-col right
    # margin: padding flush to `cols` wraps onto a second line on terminals
    # that differ on edge-column handling.
    body_w = len(body) + (2 if label_text else 0)
    budget = cols - body_w - 3

    # Degrade the hint to whatever fits, longest-first, so a narrow terminal
    # never wraps the toolbar onto two lines.
    esc = " · esc to interrupt" if m in SHOW_ESC_HINT else ""
    for candidate in (f"  (shift+tab to cycle){esc}", "  (shift+tab to cycle)", esc, ""):
        if len(candidate) <= budget:
            hint = candidate
            break
    else:
        hint = ""

    pad = max(0, budget - len(hint))
    return FormattedText([
        # Leading blank line separates the mode indicator from the input
        # line, and the trailing blank keeps it off the terminal's bottom
        # edge — the input no longer sits flush against the bottom of the
        # screen (3 reserved rows: blank / indicator / blank).
        ("", "\n"),
        ("", " " * pad),
        (f"{color} bold", body),
        ("ansibrightblack", hint),
        ("", "\n"),
    ])


def print_line(state: dict) -> None:
    """Render a single right-aligned mode line, color-coded by current mode.

    DEFAULT mode renders nothing — the transcript stays clean when there's
    no special permission state to report.
    """
    m = state.get("mode")
    if m is None:
        return
    label_text = LABELS.get(m, "")
    if not label_text:
        return  # DEFAULT mode — no indicator
    if m == Mode.BYPASS:
        color = "red"
    elif m == Mode.AUTO:
        color = "yellow"
    elif m == Mode.ACCEPT_EDITS:
        color = "cyan"
    else:
        color = "green"
    hint = "  (shift+tab to cycle)"
    if m in SHOW_ESC_HINT:
        hint += " · esc to interrupt"
    text = Text()
    text.append(f"⏵⏵ {label_text}", style=f"bold {color}")
    text.append(hint, style="dim")
    try:
        console.print(text, justify="right")
    except Exception:
        pass
