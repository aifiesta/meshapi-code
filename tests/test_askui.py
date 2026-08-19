"""The interactive option picker behind the ask_user tool.

The prompt_toolkit Application itself needs a terminal, so these cover the
pure pieces: rendering, state transitions, and the headless refusal that
keeps a non-tty run from hanging on a UI it can't draw.
"""
from meshapi import askui
from meshapi.askui import FREE_TEXT_LABEL, _AskState, _render


QS = [
    {"question": "What scale are you targeting?", "header": "Target Scale",
     "options": [{"label": "Hobbyist", "description": "Simple tracking"},
                 {"label": "Pro", "description": "Full business ops"}]},
    {"question": "Which data source?", "header": "Data Import",
     "options": [{"label": "CSV"}, {"label": "API"}]},
]


def text_of(ft):
    return "".join(t for _, t in ft)


def test_free_text_option_always_appended():
    st = _AskState(QS)
    assert st.rows(0) == ["Hobbyist", "Pro", FREE_TEXT_LABEL]


def test_render_single_question_has_no_tab_strip():
    st = _AskState([QS[0]])
    out = text_of(_render(st, 80))
    assert "What scale are you targeting?" in out
    assert "☐" not in out and "→" not in out
    assert "❯ 1. Hobbyist" in out
    assert "Simple tracking" in out
    assert "Esc to cancel" in out


def test_render_multi_question_shows_tabs_and_progress():
    st = _AskState(QS)
    out = text_of(_render(st, 100))
    assert "Target Scale" in out and "Data Import" in out
    assert "☐" in out
    st.answers[0] = "Hobbyist"
    out2 = text_of(_render(st, 100))
    assert "✔" in out2                      # answered question is ticked
    assert "Tab/←→ to switch question" in out2


def test_render_cursor_moves():
    st = _AskState([QS[0]])
    st.cursor[0] = 1
    out = text_of(_render(st, 80))
    assert "❯ 2. Pro" in out
    assert "❯ 1." not in out


def test_multi_select_shows_checkboxes():
    q = dict(QS[0]); q["multi_select"] = True
    st = _AskState([q])
    st.selected[0].add(1)
    out = text_of(_render(st, 80))
    assert "[x] Pro" in out and "[ ] Hobbyist" in out
    assert "Space to toggle" in out


def test_long_question_and_description_are_clipped():
    q = {"question": "Q" * 500, "options": [{"label": "a", "description": "D" * 500},
                                            {"label": "b"}]}
    out = text_of(_render(_AskState([q]), 60))
    assert "…" in out
    assert max(len(line) for line in out.splitlines()) < 120


def test_all_answered_gate():
    st = _AskState(QS)
    assert not st.all_answered()
    st.answers[0] = "Hobbyist"
    assert not st.all_answered()
    st.answers[1] = "CSV"
    assert st.all_answered()


def test_headless_returns_none(monkeypatch):
    """A non-tty run must refuse rather than block forever."""
    monkeypatch.setattr(askui.sys.stdin, "isatty", lambda: False)
    assert askui.ask(QS) is None


def test_empty_questions_returns_none():
    assert askui.ask([]) is None


def test_picker_erases_itself_on_exit():
    """A frozen mid-question frame in scrollback reads as 'stuck on question
    3' — the Application must erase on exit; the caller prints the summary."""
    import inspect
    src = inspect.getsource(askui.ask)
    assert "erase_when_done=True" in src
