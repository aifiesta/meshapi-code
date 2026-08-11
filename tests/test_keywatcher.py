"""Tests for keywatcher._InputParser — the pure byte-level cbreak parser.

The parser is load-bearing (type-ahead, shift+tab, Enter-vs-paste, ESC) and
explicitly designed to be unit-testable without a terminal. These lock in the
hard-won behaviors documented in the module + CLAUDE.md.
"""
from meshapi.keywatcher import _InputParser


def ev(chunk: bytes):
    return _InputParser().feed(chunk)


def test_plain_text():
    assert ev(b"hello") == [("text", "hello")]


def test_backspace_del_and_bs():
    assert ("backspace",) in ev(b"\x7f")
    assert ("backspace",) in ev(b"\x08")


def test_ctrl_u_kill_line():
    assert ("kill_line",) in ev(b"\x15")


def test_text_then_backspace_order():
    assert ev(b"ab\x7f") == [("text", "ab"), ("backspace",)]


def test_enter_at_end_is_pending_not_submit():
    p = _InputParser()
    assert p.feed(b"hi\n") == [("text", "hi")]  # newline held as pending
    assert p.has_pending is True
    assert p.on_timeout() == [("submit",)]      # timeout resolves to submit
    assert p.has_pending is False


def test_enter_midchunk_is_literal_newline():
    # A newline NOT at the end of the chunk = paste, kept as literal text.
    assert ev(b"a\nb") == [("text", "a"), ("text", "\n"), ("text", "b")]


def test_pending_newline_resolves_to_text_when_more_bytes_follow():
    p = _InputParser()
    assert p.feed(b"line1\n") == [("text", "line1")]
    assert p.has_pending is True
    # more bytes arrive → it was a paste, the held newline becomes literal text
    out = p.feed(b"line2")
    assert ("text", "\n") in out and ("text", "line2") in out
    assert p.has_pending is False


def test_shift_tab_csi_z():
    assert ev(b"\x1b[Z") == [("shift_tab",)]


def test_bare_esc_resolves_on_timeout():
    p = _InputParser()
    assert p.feed(b"\x1b") == []          # ESC alone: wait to disambiguate
    assert p.has_pending is True
    assert p.on_timeout() == [("esc",)]


def test_arrow_keys_dropped():
    for final in (b"A", b"B", b"C", b"D"):
        assert ev(b"\x1b[" + final) == []  # arrows produce no events


def test_alt_key_dropped():
    # ESC then a non-[/O/] byte = Alt+key → both dropped
    assert ev(b"\x1bx") == []


def test_cr_becomes_submit_pending():
    p = _InputParser()
    assert p.feed(b"hi\r") == [("text", "hi")]
    assert p.on_timeout() == [("submit",)]


def test_crlf_is_single_newline():
    # \r\n must not produce two newlines/submits
    p = _InputParser()
    assert p.feed(b"hi\r\n") == [("text", "hi")]
    assert p.on_timeout() == [("submit",)]


def test_crlf_split_across_chunks():
    p = _InputParser()
    assert p.feed(b"hi\r") == [("text", "hi")]  # \r → pending newline + swallow_lf
    assert p.feed(b"\n") == []                  # paired \n swallowed, no 2nd newline
    assert p.on_timeout() == [("submit",)]      # the single Enter still resolves


def test_utf8_multibyte_is_text_not_csi():
    # '›' = e2 80 ba. 0x9b (a UTF-8 continuation byte) must NOT be treated as
    # an 8-bit CSI — a pasted '›' would otherwise corrupt the buffer.
    assert ev("›".encode("utf-8")) == [("text", "›")]


def test_utf8_split_across_feeds_accumulates():
    p = _InputParser()
    b = "é".encode("utf-8")  # 2 bytes
    out = p.feed(b[:1]) + p.feed(b[1:])
    assert ("text", "é") in out


def test_double_esc_first_resolves():
    # ESC ESC: the first ESC resolves to ('esc',), parser stays in ESC
    out = ev(b"\x1b\x1b")
    assert ("esc",) in out


def test_runaway_csi_poisoned_no_shift_tab():
    # A long CSI param run must be swallowed; a trailing Z must NOT fire
    # shift_tab (poisoned), and no param junk leaks as text.
    chunk = b"\x1b[" + b"1;" * 40 + b"Z"
    out = ev(chunk)
    assert ("shift_tab",) not in out
    assert all(kind != "text" for kind, *_ in out)


def test_has_pending_false_at_ground():
    assert _InputParser().has_pending is False
