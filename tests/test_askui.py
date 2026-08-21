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


# ---------------------------------------------------------------------------
# The /effort slider (horizontal enum picker)
# ---------------------------------------------------------------------------

from meshapi.askui import _slider_text, slider

OPTS = [("auto", "detects per prompt"), ("low", "cheapest competent"),
        ("medium", "balanced"), ("high", "strong models"),
        ("xhigh", "frontier"), ("max", "best, cost no object")]


def test_slider_frame_renders_all_stops():
    out = text_of(_slider_text(OPTS, 0, "Effort", "Faster", "Smarter"))
    for v, _ in OPTS:
        assert v in out
    assert "Effort" in out and "Faster" in out and "Smarter" in out
    assert "←/→ to adjust · Enter to confirm · Esc to cancel" in out


def test_slider_marker_tracks_selection():
    lines0 = text_of(_slider_text(OPTS, 0, "E", "L", "R")).splitlines()
    lines5 = text_of(_slider_text(OPTS, 5, "E", "L", "R")).splitlines()
    m0 = next(l for l in lines0 if "▲" in l).index("▲")
    m5 = next(l for l in lines5 if "▲" in l).index("▲")
    assert m5 > m0                      # marker moves right with the index


def test_slider_shows_selected_description():
    out = text_of(_slider_text(OPTS, 4, "E", "L", "R"))
    assert "frontier" in out
    assert "cheapest competent" not in out   # only the selected desc shows


def test_slider_unavailable_off_tty(monkeypatch):
    monkeypatch.setattr(askui.sys.stdin, "isatty", lambda: False)
    assert slider("Effort", OPTS) == ("unavailable", None)


def test_slider_empty_options():
    assert slider("E", []) == ("unavailable", None)


# ---- slider layout (regression: fixed cells crammed long labels) ----

LONG_OPTS = [("concise", "Short and direct"),
             ("default", "Balanced"),
             ("explanatory", "Narrates the reasoning behind each decision"),
             ("learning", "Teaching mode")]


def _lines(idx, width=78, opts=LONG_OPTS):
    return text_of(_slider_text(opts, idx, "Output style", "Terse",
                                "Teaching", term_width=width)).splitlines()


def test_labels_never_touch_even_when_longer_than_the_cell():
    # "explanatory" is 11 chars; the old fixed 10-char cell made adjacent
    # labels collide and pushed the stops row wider than the rail.
    stops = [l for l in _lines(0) if "explanatory" in l][0]
    assert "  explanatory  " in stops
    for a, b in (("concise", "default"), ("default", "explanatory"),
                 ("explanatory", "learning")):
        between = stops.split(a)[1].split(b)[0]
        assert len(between) >= 2, (a, b, between)


def test_marker_aligns_with_the_selected_tick():
    for idx in range(len(LONG_OPTS)):
        lines = _lines(idx)
        marker = next(l for l in lines if "▲" in l)
        rail = next(l for l in lines if "┬" in l)
        assert rail[marker.index("▲")] == "┬", idx


def test_labels_keep_a_gap_at_every_width():
    # The invariant is simply that no two labels run together: the stops row
    # must always split into exactly one token per option. At 34 columns the
    # untruncated version produced "explanatlearning" as a single word.
    for width in (30, 34, 40, 46, 60, 78, 120):
        stops = next(l for l in _lines(0, width) if "concis" in l)
        assert len(stops.split()) == len(LONG_OPTS), (width, stops)


def test_rail_and_stops_row_stay_within_the_terminal():
    for width in (34, 46, 60, 78, 120):
        for idx in range(len(LONG_OPTS)):
            for line in _lines(idx, width):
                assert len(line) <= width, (width, idx, line)


def test_long_description_wraps_instead_of_overrunning():
    lines = _lines(2, 78)
    desc = [l for l in lines if "Narrates" in l]
    assert desc and all(len(l) <= 78 for l in desc)


def test_narrow_terminal_shortens_the_hint():
    assert "to adjust" in "\n".join(_lines(0, 78))
    assert "to adjust" not in "\n".join(_lines(0, 34))


def test_single_stop_does_not_divide_by_zero():
    out = text_of(_slider_text([("only", "just one")], 0, "T", "L", "R",
                               term_width=40))
    assert "only" in out
