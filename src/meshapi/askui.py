"""Interactive multiple-choice prompt the MODEL can raise mid-run.

Backs the `ask_user` tool: instead of guessing at a fork in the road (or
burning a turn asking in prose and waiting for the user to type a reply),
the model presents real options and the user picks with the arrow keys.

Layout mirrors the shape users already know from Claude Code:

    ← ☐ Target Scale  ☐ Data Import  ✔ Submit →
    What creator scale are you targeting …?

    ❯ 1. Hobbyist
         Simple tracking, minimal tax complexity
      2. Mid-tier
         Growing complexity, may have contractors
      3. Type something.

    Enter to select · Tab/Arrow keys to navigate · Esc to cancel

Multiple questions render as a tab strip; each is answered in turn and the
strip tracks which are done. The last option is always a free-text escape
("Type something.") so the user is never boxed in by the model's list.

Rendering is a prompt_toolkit Application (not full-screen) so it sits
inline in the transcript. Every failure path degrades to returning None,
which the caller turns into an ordinary "ask in prose instead" tool result
— an interactive prompt must never be able to wedge a headless run.
"""
from __future__ import annotations

import sys

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

FREE_TEXT_LABEL = "Type something."

_STYLE = Style.from_dict({
    "tab": "#8a8a8a",
    "tab.active": "bold #00d7af reverse",
    "tab.done": "#00d7af",
    "question": "bold",
    "opt": "",
    "opt.sel": "bold #00d7af",
    "desc": "#8a8a8a",
    "hint": "#6c6c6c",
})


def _clip(text: str, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


class _AskState:
    def __init__(self, questions: list):
        self.questions = questions
        self.q_index = 0
        self.cursor = [0] * len(questions)
        self.answers: list = [None] * len(questions)
        self.selected: list = [set() for _ in questions]  # multi-select
        self.cancelled = False

    @property
    def q(self) -> dict:
        return self.questions[self.q_index]

    def rows(self, qi: int) -> list:
        """Option labels for question qi, plus the free-text escape."""
        return [o["label"] for o in self.questions[qi]["options"]] + [FREE_TEXT_LABEL]

    def all_answered(self) -> bool:
        return all(a is not None for a in self.answers)


def _render(st: _AskState, width: int) -> FormattedText:
    out: list = []
    # --- tab strip (only when there's more than one question) -------------
    if len(st.questions) > 1:
        out.append(("class:hint", "← "))
        for i, q in enumerate(st.questions):
            mark = "✔" if st.answers[i] is not None else "☐"
            label = _clip(q.get("header") or f"Q{i + 1}", 18)
            style = "class:tab.active" if i == st.q_index else (
                "class:tab.done" if st.answers[i] is not None else "class:tab")
            out.append((style, f" {mark} {label} "))
            out.append(("", " "))
        out.append(("class:hint", "→\n\n"))

    # --- question --------------------------------------------------------
    q = st.q
    out.append(("class:question", _clip(q["question"], max(20, width - 2))))
    out.append(("", "\n\n"))

    # --- options ---------------------------------------------------------
    multi = bool(q.get("multi_select"))
    for idx, label in enumerate(st.rows(st.q_index)):
        is_cursor = idx == st.cursor[st.q_index]
        picked = idx in st.selected[st.q_index]
        pointer = "❯ " if is_cursor else "  "
        box = ""
        if multi and label != FREE_TEXT_LABEL:
            box = "[x] " if picked else "[ ] "
        style = "class:opt.sel" if is_cursor else "class:opt"
        out.append((style, f"{pointer}{idx + 1}. {box}{label}"))
        out.append(("", "\n"))
        if label != FREE_TEXT_LABEL:
            desc = (q["options"][idx].get("description") or "").strip()
            if desc:
                out.append(("class:desc", f"     {_clip(desc, max(20, width - 6))}"))
                out.append(("", "\n"))
    out.append(("", "\n"))

    # --- footer ----------------------------------------------------------
    bits = ["Enter to select"]
    if multi:
        bits.insert(0, "Space to toggle")
    if len(st.questions) > 1:
        bits.append("Tab/←→ to switch question")
    bits.append("↑↓ to move")
    bits.append("Esc to cancel")
    out.append(("class:hint", " · ".join(bits)))
    return FormattedText(out)


def ask(questions: list, free_text_reader=None) -> "list | None":
    """Run the picker. Returns a list of answers, or None if cancelled.

    Each answer is a str (single-select / free text) or list[str]
    (multi-select). `free_text_reader` is injected by tests; by default the
    free-text option reads one line from the terminal.
    """
    if not questions:
        return None
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None  # headless: caller falls back to asking in prose

    st = _AskState(questions)
    kb = KeyBindings()

    def _advance(app):
        """Move to the next unanswered question, or finish."""
        if st.all_answered():
            app.exit()
            return
        for offset in range(1, len(st.questions) + 1):
            nxt = (st.q_index + offset) % len(st.questions)
            if st.answers[nxt] is None:
                st.q_index = nxt
                return
        app.exit()

    @kb.add("up")
    @kb.add("c-p")
    def _(event):
        n = len(st.rows(st.q_index))
        st.cursor[st.q_index] = (st.cursor[st.q_index] - 1) % n

    @kb.add("down")
    @kb.add("c-n")
    def _(event):
        n = len(st.rows(st.q_index))
        st.cursor[st.q_index] = (st.cursor[st.q_index] + 1) % n

    @kb.add("tab")
    @kb.add("right")
    def _(event):
        st.q_index = (st.q_index + 1) % len(st.questions)

    @kb.add("s-tab")
    @kb.add("left")
    def _(event):
        st.q_index = (st.q_index - 1) % len(st.questions)

    @kb.add("space")
    def _(event):
        q = st.q
        if not q.get("multi_select"):
            return
        idx = st.cursor[st.q_index]
        if st.rows(st.q_index)[idx] == FREE_TEXT_LABEL:
            return
        sel = st.selected[st.q_index]
        sel.discard(idx) if idx in sel else sel.add(idx)

    @kb.add("enter")
    def _(event):
        qi = st.q_index
        idx = st.cursor[qi]
        rows = st.rows(qi)
        if rows[idx] == FREE_TEXT_LABEL:
            # Read the custom answer after tearing the picker down, so the
            # two prompts never fight over stdin.
            st.answers[qi] = {"__free_text__": True}
            event.app.exit()
            return
        if st.q.get("multi_select"):
            sel = st.selected[qi]
            if not sel:
                sel.add(idx)
            st.answers[qi] = [rows[i] for i in sorted(sel)]
        else:
            st.answers[qi] = rows[idx]
        _advance(event.app)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _(event):
        st.cancelled = True
        event.app.exit()

    for n in range(1, 10):
        @kb.add(str(n))
        def _(event, _n=n):
            if _n <= len(st.rows(st.q_index)):
                st.cursor[st.q_index] = _n - 1

    def _content():
        try:
            width = app.output.get_size().columns
        except Exception:
            width = 80
        return _render(st, width)

    app = Application(
        layout=Layout(Window(FormattedTextControl(_content), always_hide_cursor=True)),
        key_bindings=kb,
        style=_STYLE,
        full_screen=False,
        # Erase the picker when it exits: a frozen mid-question frame left in
        # scrollback reads as "stuck on question 3". The caller prints a
        # clean answered-summary in its place.
        erase_when_done=True,
    )

    while True:
        try:
            app.run()
        except (EOFError, KeyboardInterrupt):
            return None
        if st.cancelled:
            return None
        # Free-text answers are collected outside the Application.
        pending = [
            i for i, a in enumerate(st.answers)
            if isinstance(a, dict) and a.get("__free_text__")
        ]
        for qi in pending:
            reader = free_text_reader or _default_free_text
            text = reader(st.questions[qi]["question"])
            st.answers[qi] = (text or "").strip() or None
            if st.answers[qi] is None:
                st.cursor[qi] = 0  # empty input: re-ask rather than record ""
        if st.all_answered():
            return st.answers
        if not pending:
            return st.answers


def _default_free_text(question: str) -> str:
    from prompt_toolkit import prompt as pt_prompt
    try:
        return pt_prompt("  your answer › ")
    except (EOFError, KeyboardInterrupt):
        return ""


# ---------------------------------------------------------------------------
# Horizontal slider — the /effort picker (reusable for any enum setting)
# ---------------------------------------------------------------------------

_SLIDER_STYLE = Style.from_dict({
    "title": "bold",
    "rail": "#6c6c6c",
    "stop": "#8a8a8a",
    "stop.sel": "bold #af87ff",
    "marker": "#af87ff",
    "endlabel": "#8a8a8a",
    "desc": "#6c6c6c italic",
    "hint": "#6c6c6c",
})

_CELL = 10  # column width per stop


def _slider_text(options: list, idx: int, title: str,
                 left_label: str, right_label: str) -> FormattedText:
    """Pure renderer for the slider frame. options: [(value, desc), ...]."""
    n = len(options)
    width = n * _CELL
    out: list = [("class:title", f" {title}\n\n")]
    # end labels
    out.append(("class:endlabel", " " + left_label.ljust(width - len(right_label) - 1)
                + right_label + "\n"))
    # marker row
    marker_pad = idx * _CELL + (_CELL // 2 - 1)
    out.append(("", " " * (marker_pad + 1)))
    out.append(("class:marker", "▲"))
    out.append(("", "\n"))
    # rail
    out.append(("class:rail", " " + "─" * width + "\n"))
    # stops row
    out.append(("", " "))
    for i, (value, _desc) in enumerate(options):
        label = value.center(_CELL)
        out.append(("class:stop.sel" if i == idx else "class:stop", label))
    out.append(("", "\n"))
    # description of the selected stop
    desc = options[idx][1]
    if desc:
        pad = max(0, idx * _CELL + _CELL // 2 - len(desc) // 2)
        pad = min(pad, max(0, width - len(desc)))
        out.append(("class:desc", " " * (pad + 1) + desc + "\n"))
    out.append(("class:hint", "\n ←/→ to adjust · Enter to confirm · Esc to cancel"))
    return FormattedText(out)


def slider(title: str, options: list, current: str = None,
           left_label: str = "Faster", right_label: str = "Smarter") -> tuple:
    """Interactive horizontal picker. options: [(value, one-line desc), ...].

    Returns (status, value): ("picked", v) | ("cancelled", None) |
    ("unavailable", None) for non-tty terminals — the caller falls back to
    plain text. Erases itself on exit like the question picker.
    """
    if not options:
        return ("unavailable", None)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return ("unavailable", None)
    values = [v for v, _ in options]
    state = {"idx": values.index(current) if current in values else 0,
             "picked": None, "cancelled": False}
    kb = KeyBindings()

    @kb.add("left")
    def _(event):
        state["idx"] = max(0, state["idx"] - 1)

    @kb.add("right")
    def _(event):
        state["idx"] = min(len(options) - 1, state["idx"] + 1)

    @kb.add("home")
    def _(event):
        state["idx"] = 0

    @kb.add("end")
    def _(event):
        state["idx"] = len(options) - 1

    @kb.add("enter")
    def _(event):
        state["picked"] = values[state["idx"]]
        event.app.exit()

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _(event):
        state["cancelled"] = True
        event.app.exit()

    for i in range(1, min(10, len(options) + 1)):
        @kb.add(str(i))
        def _(event, _i=i):
            state["idx"] = _i - 1

    app = Application(
        layout=Layout(Window(
            FormattedTextControl(lambda: _slider_text(
                options, state["idx"], title, left_label, right_label)),
            always_hide_cursor=True)),
        key_bindings=kb,
        style=_SLIDER_STYLE,
        full_screen=False,
        erase_when_done=True,
    )
    try:
        app.run()
    except (EOFError, KeyboardInterrupt):
        return ("cancelled", None)
    if state["cancelled"] or state["picked"] is None:
        return ("cancelled", None)
    return ("picked", state["picked"])
