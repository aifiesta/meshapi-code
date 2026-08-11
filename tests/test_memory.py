"""Tests for meshapi.memory — repo memory + session read-dedupe.

The contract everywhere is "best-effort, never raises": a memory bug must
never break a write, a read, or session start. And dedupe_read must fail
toward a normal read (return None) unless a stub is provably safe — a wrong
"already in your context" would gaslight the model.

All tests isolate ~/.meshapi/context to tmp_path (autouse fixture) so the
real user store is never touched.
"""
from pathlib import Path

import pytest

from meshapi import memory


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Point memory.CONFIG_DIR at a throwaway dir so capture()/append_note()
    can never write into the real ~/.meshapi."""
    cfg = (tmp_path / "meshapi_home").resolve()
    monkeypatch.setattr(memory, "CONFIG_DIR", cfg)
    return cfg


@pytest.fixture
def repo(tmp_path):
    # Resolve so capture()'s p.resolve().relative_to(root) doesn't trip on a
    # /tmp -> /private/tmp style symlink (macOS).
    r = (tmp_path / "repo").resolve()
    r.mkdir()
    return r


# ---------------------------------------------------------------------------
# extract_symbols — pure, never raises

PATHOLOGICAL_CONTENT = [
    b"\x00\x01\x02 raw binary bytes",   # bytes, not str
    "\x00\x00null bytes\x00",           # embedded NULs
    "def foo(): pass  # 😀 emoji ✅",     # multibyte
    "line1\r\nline2\r\nclass C:\r\n",   # CRLF
    "x" * 500_000,                       # huge string
    12345,                               # non-str scalar
    None,                                # None
    ["not", "a", "string"],             # list
    {"nope": 1},                         # dict
]


@pytest.mark.parametrize("content", PATHOLOGICAL_CONTENT)
@pytest.mark.parametrize("path", ["foo.py", "a.js", "weird.unknownext",
                                  "no_extension", "page.html", "style.css"])
def test_extract_symbols_never_raises(path, content):
    result = memory.extract_symbols(path, content)
    assert isinstance(result, list)


def test_extract_symbols_finds_python_defs():
    syms = memory.extract_symbols("m.py", "def alpha():\n    pass\nclass Beta:\n")
    assert "def alpha" in syms
    assert "class Beta" in syms


def test_extract_symbols_caps_output():
    src = "\n".join(f"def f{i}():" for i in range(100))
    syms = memory.extract_symbols("big.py", src)
    assert len(syms) <= memory.MAX_SYMBOLS_PER_FILE


# ---------------------------------------------------------------------------
# capture — best-effort, never raises


@pytest.mark.parametrize("content", PATHOLOGICAL_CONTENT)
def test_capture_never_raises_on_pathological(repo, content):
    f = repo / "a.py"
    # capture computes len()/encode() on content; bad types must be swallowed.
    memory.capture(repo, str(f), content)  # must not raise


def test_capture_persists_store(repo):
    f = repo / "a.py"
    src = "def hello():\n    return 1\n"
    f.write_text(src)
    memory.capture(repo, str(f), src)

    store = memory.load_store(repo)
    assert store.get("version") == memory.SCHEMA_VERSION
    assert "a.py" in store["files"]
    assert "def hello" in store["files"]["a.py"]["symbols"]
    assert (memory.context_dir(repo) / "repomap.json").exists()


def test_capture_ignores_paths_outside_repo(repo, tmp_path):
    outside = (tmp_path / "elsewhere.py").resolve()
    outside.write_text("def x(): pass\n")
    memory.capture(repo, str(outside), "def x(): pass\n")  # no raise
    # The store stays repo-scoped: nothing recorded for an out-of-tree file.
    store = memory.load_store(repo)
    assert store == {} or "elsewhere.py" not in store.get("files", {})


def test_capture_nonexistent_path_is_safe(repo):
    ghost = repo / "does_not_exist.py"
    memory.capture(repo, str(ghost), "def g(): pass\n")  # no raise
    # relative_to(root) succeeds (it's under repo), so it IS recorded; the
    # point is only that the missing file on disk doesn't crash capture.
    store = memory.load_store(repo)
    assert isinstance(store, dict)


# ---------------------------------------------------------------------------
# dedupe_read — stub only when provably safe; else None (normal read)


def _fresh_state():
    # msg_index 0 must be < len(messages); one placeholder message suffices.
    return {"messages": [{"role": "user", "content": "hi"}], "session_reads": {}}


def test_dedupe_stubs_when_sha_matches(repo):
    state = _fresh_state()
    f = repo / "big.txt"
    body = "L" * 400  # >= DEDUPE_MIN_CHARS and <= 800 so it survives any dial
    f.write_text(body)
    memory.record_read(state, str(f), body, msg_index=0)

    stub = memory.dedupe_read(state, str(f), dial=0.0)
    assert stub is not None
    assert "unchanged" in stub and "already earlier in this conversation" in stub
    # The stub flips stubbed_last so an immediate re-ask returns the body.
    key = str(f.resolve())
    assert state["session_reads"][key]["stubbed_last"] is True


def test_dedupe_reask_after_stub_returns_none(repo):
    state = _fresh_state()
    f = repo / "big.txt"
    body = "L" * 400
    f.write_text(body)
    memory.record_read(state, str(f), body, msg_index=0)

    first = memory.dedupe_read(state, str(f), dial=0.0)
    assert first is not None
    # Immediate re-ask: the model wants the real body, so None (normal read).
    second = memory.dedupe_read(state, str(f), dial=0.0)
    assert second is None
    # stubbed_last was reset so a later ask can stub again.
    assert state["session_reads"][str(f.resolve())]["stubbed_last"] is False


def test_dedupe_none_when_file_changed_on_disk(repo):
    state = _fresh_state()
    f = repo / "big.txt"
    body = "L" * 400
    f.write_text(body)
    memory.record_read(state, str(f), body, msg_index=0)
    # Mutate the file after recording -> sha256 mismatch -> normal read.
    f.write_text("X" * 400)
    assert memory.dedupe_read(state, str(f), dial=0.0) is None


def test_dedupe_none_below_min_chars(repo):
    state = _fresh_state()
    f = repo / "small.txt"
    body = "s" * (memory.DEDUPE_MIN_CHARS - 1)
    f.write_text(body)
    memory.record_read(state, str(f), body, msg_index=0)
    assert memory.dedupe_read(state, str(f), dial=0.0) is None


def test_dedupe_none_when_no_entry(repo):
    state = _fresh_state()
    f = repo / "unseen.txt"
    f.write_text("z" * 400)
    assert memory.dedupe_read(state, str(f), dial=0.0) is None


def test_dedupe_none_when_msg_index_out_of_range(repo):
    state = {"messages": [], "session_reads": {}}  # msg_index 0 >= len 0
    f = repo / "big.txt"
    body = "L" * 400
    f.write_text(body)
    memory.record_read(state, str(f), body, msg_index=0)
    assert memory.dedupe_read(state, str(f), dial=0.0) is None


def test_dedupe_read_source_pruned_at_high_dial(repo):
    # Read-sourced content > 2*_TRUNCATE_TO_CHARS may be pruned off the wire
    # at dial >= 0.2, so a stub would be a lie -> None.
    state = _fresh_state()
    f = repo / "large.txt"
    body = "R" * 2000
    f.write_text(body)
    memory.record_read(state, str(f), body, msg_index=0)
    assert memory.dedupe_read(state, str(f), dial=0.95) is None
    # ...but at dial 0 pruning is off, so the same content stubs.
    memory.record_read(state, str(f), body, msg_index=0)  # reset stubbed_last
    assert memory.dedupe_read(state, str(f), dial=0.0) is not None


def test_dedupe_write_source_survives_high_dial(repo):
    # Write-sourced content rides in assistant tool_calls messages, which
    # pruning never touches -> a large write still stubs at a high dial.
    state = _fresh_state()
    f = repo / "written.txt"
    body = "W" * 2000
    f.write_text(body)
    memory.record_write(state, str(f), body, msg_index=0)
    assert memory.dedupe_read(state, str(f), dial=0.95) is not None


def test_dedupe_never_raises_on_bad_input(repo):
    state = _fresh_state()
    # Nonexistent path with no entry, a None path, and an entry whose file was
    # deleted must all return None without raising.
    assert memory.dedupe_read(state, str(repo / "nope.txt"), 0.0) is None
    assert memory.dedupe_read(state, None, 0.0) is None

    f = repo / "gone.txt"
    body = "G" * 400
    f.write_text(body)
    memory.record_read(state, str(f), body, msg_index=0)
    f.unlink()  # file deleted after recording -> read_text raises -> None
    assert memory.dedupe_read(state, str(f), 0.0) is None
