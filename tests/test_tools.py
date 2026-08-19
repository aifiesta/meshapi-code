"""Unit tests for meshapi.tools pure functions + attachments.find_image_tokens.

Covers the quality-guard heuristics (find_stub_markers, stub_guard_suppressed),
the narrow tool-arg repair (repair_tool_args — must never fabricate a closure
for truncated JSON), and the model-feedback helpers (schema_hint,
parse_error_context, validate_call, summarize_call). Also exercises the
quote-aware image-token detector.

Pure functions only — no network, no side effects beyond tmp files created by
pytest's tmp_path fixture.
"""
import json

import pytest

from meshapi import tools
from meshapi import attachments


# ---------------------------------------------------------------------------
# find_stub_markers — true positives (code files with real stub markers)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path, content",
    [
        # Tier 1 narrative phrases (the live "// Add game logic here" failure).
        ("app.js", "function play() {\n  // Add game logic here\n}\n"),
        ("app.py", "def handler():\n    # your code goes here\n"),
        ("app.py", "def parse():\n    raise NotImplementedError\n"),
        ("app.js", "// rest of the code remains the same\n"),
        # Tier 2 bare token, only inside a comment context.
        ("app.js", "function init() {\n  // TODO: implement\n}\n"),
    ],
)
def test_find_stub_markers_flags_real_stubs(path, content):
    evidence = tools.find_stub_markers(path, content)
    assert isinstance(evidence, list)
    assert evidence, f"expected a stub flag for {path!r} content={content!r}"
    # Evidence format is "line N: <trimmed>".
    assert all(e.startswith("line ") for e in evidence)
    assert len(evidence) <= 3


# ---------------------------------------------------------------------------
# find_stub_markers — false positives (the todo-app trap must NOT flag)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path, content",
    [
        # A todo app: TODO as literal UI content, not a code stub.
        ("index.html", "<h1>TODO List</h1>\n"),
        # todo / todos as ordinary identifiers.
        ("app.js", "const todos = [];\nlet todo = { done: false };\n"),
        # HTML placeholder attribute.
        ("form.html", '<input placeholder="Name" type="text">\n'),
        # CSS ::placeholder pseudo-element.
        ("styles.css", "::placeholder { color: gray; }\n"),
        # A URL that merely contains /todos (URLs are scrubbed before scan).
        ("api.js", 'const url = "https://api.example.com/todos";\n'),
        # Idiomatic empty callback / pass — no empty-body heuristic exists.
        ("app.js", "el.addEventListener('click', () => {});\n"),
        ("app.py", "def noop():\n    pass\n"),
    ],
)
def test_find_stub_markers_no_false_positive(path, content):
    assert tools.find_stub_markers(path, content) == []


def test_find_stub_markers_skips_non_code_extensions():
    # A .md/.txt file legitimately contains these phrases as prose/content;
    # the extension gate — not the content — is what keeps it unscanned.
    stubby = "// Add game logic here\nraise NotImplementedError\n"
    assert tools.find_stub_markers("notes.md", stubby) == []
    assert tools.find_stub_markers("README.txt", stubby) == []


def test_find_stub_markers_caps_evidence_at_three():
    content = "\n".join(
        [
            "// Add game logic here",
            "# your code goes here",
            "raise NotImplementedError",
            "// rest of the code remains the same",
            "// coming soon",
        ]
    )
    ev = tools.find_stub_markers("app.js", content)
    assert 0 < len(ev) <= 3


def test_find_stub_markers_never_raises_on_bad_input():
    # Pure + defensive: a bug here is a no-op, never a crash.
    assert tools.find_stub_markers("x.js", None) == []  # splitlines on None -> caught


# ---------------------------------------------------------------------------
# repair_tool_args — truncation is NEVER repaired (no fabricated closures)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        '{"path": "foo',                    # unterminated string
        '{"path": "foo"',                   # missing closing brace
        '{"a": 1,',                         # trailing comma
        '{"a":',                            # trailing colon
        '{"a": {"b": {"c": 1',              # deep nested, unclosed
        '{"path": "a", "content": "half',   # truncated mid-value
    ],
)
def test_repair_tool_args_never_fabricates_closure(raw):
    repaired, reason = tools.repair_tool_args(raw)
    # The load-bearing invariant: truncated input yields NO repaired string.
    assert repaired is None
    assert reason == "truncated"


def test_repair_tool_args_repairs_missing_comma_between_members():
    raw = '{"path": "a.py" "content": "x"}'  # exactly one missing comma
    repaired, reason = tools.repair_tool_args(raw)
    assert reason is None
    assert repaired is not None
    # The repaired string must re-parse to the intended object.
    assert json.loads(repaired) == {"path": "a.py", "content": "x"}
    assert repaired.count(",") == 1  # only the one comma inserted


def test_repair_tool_args_already_valid_has_no_repair_positions():
    # Valid JSON reaching the repairer (positions empty) is classed
    # unrepairable, never truncated — it is not the missing-comma shape.
    repaired, reason = tools.repair_tool_args('{"a": 1}')
    assert repaired is None
    assert reason == "unrepairable"


# ---------------------------------------------------------------------------
# stub_guard_suppressed — scaffolding intent stands the guard down
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "just scaffold the project",
        "give me a skeleton app",
        "write the boilerplate for a react app",
        "create the routes with TODOs for now",
        "don't implement the handlers yet",
        # `leave ... stub` allows at most 3 words between the two words.
        "leave the handlers as stubs",
    ],
)
def test_stub_guard_suppressed_true(text):
    assert tools.stub_guard_suppressed(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "create a todo app",                     # bare 'todo' must NOT suppress
        "implement fully, no placeholders",       # remedy reply keeps guard armed
        "add a placeholder image",                # bare 'placeholder' must NOT suppress
        "build a working snake game",
        "",
    ],
)
def test_stub_guard_suppressed_false(text):
    assert tools.stub_guard_suppressed(text) is False


def test_stub_guard_suppressed_handles_none():
    assert tools.stub_guard_suppressed(None) is False


# ---------------------------------------------------------------------------
# schema_hint — one-line reminder derived from TOOLS
# ---------------------------------------------------------------------------

def test_schema_hint_write_file_lists_required_fields():
    hint = tools.schema_hint("write_file")
    assert hint.startswith("write_file expects")
    assert '"path"' in hint and '"content"' in hint
    assert "optional" not in hint  # both fields required


def test_schema_hint_read_file():
    hint = tools.schema_hint("read_file")
    assert hint.startswith("read_file expects")
    assert '"path"' in hint


def test_schema_hint_start_server_shows_optionals_and_int_placeholder():
    hint = tools.schema_hint("start_server")
    assert '"command"' in hint
    assert "optional:" in hint
    assert "port" in hint


def test_schema_hint_integer_placeholder():
    # update_step.index is an integer -> placeholder 123, not "...".
    hint = tools.schema_hint("update_step")
    assert '"index": 123' in hint
    assert '"status"' in hint


def test_schema_hint_unknown_tool_is_empty():
    assert tools.schema_hint("no_such_tool") == ""


# ---------------------------------------------------------------------------
# parse_error_context — ±radius window with the offending char marked
# ---------------------------------------------------------------------------

def test_parse_error_context_marks_offending_char():
    raw = '{"a": 1 "b": 2}'
    out = tools.parse_error_context(raw, 8)
    assert "⟨" in out and "⟩" in out  # ⟨ ⟩ markers present


def test_parse_error_context_position_past_end():
    out = tools.parse_error_context("abc", 100)
    assert "<end>" in out


def test_parse_error_context_escapes_control_chars():
    out = tools.parse_error_context("a\nb", 1)
    assert "\\n" in out          # newline rendered as the two chars backslash-n
    assert "\n" not in out       # never a raw newline


def test_parse_error_context_adds_ellipsis_when_clipped():
    raw = "x" * 100
    out = tools.parse_error_context(raw, 50, radius=5)
    assert out.startswith("…")  # leading …
    assert out.endswith("…")    # trailing …


# ---------------------------------------------------------------------------
# validate_call — the single source of truth for "can this call run?"
# ---------------------------------------------------------------------------

def test_validate_call_ok_cases_return_none():
    assert tools.validate_call("read_file", {"path": "a.py"}) is None
    assert tools.validate_call("write_file", {"path": "a.py", "content": "x"}) is None
    # Empty content is a valid (empty) file — must NOT error.
    assert tools.validate_call("write_file", {"path": "a.py", "content": ""}) is None
    assert tools.validate_call("run_bash", {"command": "ls"}) is None
    assert tools.validate_call("start_server", {"command": "npm run dev"}) is None
    assert tools.validate_call("web_search", {"query": "python"}) is None
    assert tools.validate_call("remember", {"note": "uses pnpm"}) is None


@pytest.mark.parametrize(
    "name, args, needle",
    [
        ("read_file", {}, "path"),
        ("write_file", {}, "path"),
        ("write_file", {"path": "a.py"}, "content"),  # content missing -> error
        ("run_bash", {}, "command"),
        ("start_server", {}, "command"),
        ("web_search", {}, "query"),
        ("remember", {}, "note"),
    ],
)
def test_validate_call_missing_field_errors(name, args, needle):
    err = tools.validate_call(name, args)
    assert isinstance(err, str)
    assert err.startswith("Error")
    assert needle in err


# ---------------------------------------------------------------------------
# summarize_call — one-line approval/log summaries
# ---------------------------------------------------------------------------

def test_summarize_call_shapes():
    assert tools.summarize_call("read_file", {"path": "/x"}) == "read_file: /x"
    s = tools.summarize_call("write_file", {"path": "/x", "content": "abcd"})
    assert s == "write_file: /x (4 chars)"
    assert tools.summarize_call("run_bash", {"command": "ls -la"}) == "run_bash: ls -la"
    assert tools.summarize_call("web_search", {"query": "q"}) == "web_search: q"
    assert tools.summarize_call("remember", {"note": "n"}) == "remember: n"


def test_summarize_call_create_plan_pluralization():
    assert tools.summarize_call("create_plan", {"steps": ["a"]}) == "create_plan (1 step)"
    assert tools.summarize_call("create_plan", {"steps": ["a", "b"]}) == "create_plan (2 steps)"


def test_summarize_call_update_step_and_server():
    assert (
        tools.summarize_call("update_step", {"index": 1, "status": "in_progress"})
        == "update_step(1, 'in_progress')"
    )
    auto = tools.summarize_call("start_server", {"command": "npm run dev"})
    assert auto == "start_server: npm run dev (auto-port)"
    fixed = tools.summarize_call("start_server", {"command": "npm run dev", "port": 3000})
    assert fixed == "start_server: npm run dev (port 3000)"


def test_summarize_call_run_bash_truncates_long_command():
    long_cmd = "echo " + "x" * 400
    s = tools.summarize_call("run_bash", {"command": long_cmd})
    assert s.endswith("…")  # trailing …
    assert len(s) < len(long_cmd)


# ---------------------------------------------------------------------------
# attachments.find_image_tokens — quote-aware detection
# ---------------------------------------------------------------------------

def test_find_image_tokens_quoted_path_with_spaces(tmp_path):
    d = tmp_path / "snake game"
    d.mkdir()
    img = d / "img.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")  # existence is what matters
    text = f"describe '{img}'"
    tokens = attachments.find_image_tokens(text)
    assert len(tokens) == 1
    raw, normalized = tokens[0]
    # Quotes kept whole in raw (so the caller strips them on rewrite),
    # normalized is the clean path.
    assert raw == f"'{img}'"
    assert normalized == str(img)
    # The caller rewrites the raw token to a placeholder — prove it works.
    assert text.replace(raw, "[Image #1]") == "describe [Image #1]"


def test_find_image_tokens_bare_http_url_detected():
    text = "look at https://example.com/pic.png here"
    tokens = attachments.find_image_tokens(text)
    assert tokens == [("https://example.com/pic.png", "https://example.com/pic.png")]


def test_find_image_tokens_backtick_is_text_escape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    # Positive control: a bare existing filename is detected...
    assert attachments.find_image_tokens("shot.png") == [("shot.png", "shot.png")]
    # ...but a leading backtick means "treat as text" — not attached.
    assert attachments.find_image_tokens("`shot.png`") == []


def test_find_image_tokens_nonexistent_local_path_not_detected():
    # Liberal-then-verify: an image-extension path that doesn't exist is ignored.
    assert attachments.find_image_tokens("/nope/does/not/exist.png") == []


# ---------------------------------------------------------------------------
# read_file cap (READ_LIMIT) — huge reads must not enter history whole
# ---------------------------------------------------------------------------

def test_read_file_at_limit_untouched(tmp_path):
    p = tmp_path / "exact.txt"
    p.write_text("x" * tools.READ_LIMIT)
    out = tools.execute("read_file", {"path": str(p)})
    assert out == "x" * tools.READ_LIMIT
    assert "[truncated" not in out


def test_read_file_over_limit_truncated_with_notice(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("x" * (tools.READ_LIMIT + 1))
    out = tools.execute("read_file", {"path": str(p)})
    assert out.startswith("x" * 100)
    assert f"file is {tools.READ_LIMIT + 1} chars" in out
    assert "ranged shell command" in out
    # body kept is exactly READ_LIMIT chars
    assert out.index("\n[truncated") == tools.READ_LIMIT


# ---------------------------------------------------------------------------
# run_bash optional timeout — clamped, coerced, validated
# ---------------------------------------------------------------------------

def test_run_bash_timeout_validation():
    assert tools.validate_call("run_bash", {"command": "ls", "timeout": "abc"}) is not None
    assert tools.validate_call("run_bash", {"command": "ls", "timeout": None}) is None
    assert tools.validate_call("run_bash", {"command": "ls", "timeout": 300}) is None
    assert tools.validate_call("run_bash", {"command": "ls", "timeout": "300"}) is None
    assert tools.validate_call("run_bash", {"command": "ls", "timeout": 30.5}) is None


def test_run_bash_timeout_used_and_clamped():
    # A 1s sleep with timeout=1 clamps to the 5s floor -> completes fine.
    out = tools.execute("run_bash", {"command": "sleep 1 && echo done", "timeout": 1})
    assert "done" in out and "[exit 0]" in out


def test_run_bash_custom_timeout_expires():
    out = tools.execute("run_bash", {"command": "sleep 20", "timeout": 5})
    assert "timed out after 5s" in out
    assert "max 600s" in out


def test_run_bash_schema_mentions_timeout():
    rb = next(t for t in tools.TOOLS if t["function"]["name"] == "run_bash")
    props = rb["function"]["parameters"]["properties"]
    assert "timeout" in props
    assert props["timeout"]["type"] == "integer"
    assert "required" not in props  # timeout stays optional
    assert rb["function"]["parameters"]["required"] == ["command"]


# ---------------------------------------------------------------------------
# ask_user tool schema + validation
# ---------------------------------------------------------------------------

def test_ask_user_in_tools_and_is_interactive():
    names = [t["function"]["name"] for t in tools.TOOLS]
    assert "ask_user" in names
    assert "ask_user" in tools.INTERACTIVE_TOOLS


def test_ask_user_validation_rejects_thin_calls():
    assert tools.validate_call("ask_user", {}) is not None
    assert tools.validate_call("ask_user", {"questions": []}) is not None
    # a question needs >= 2 options
    one = {"questions": [{"question": "a?", "options": [{"label": "x"}]}]}
    assert "at least 2" in tools.validate_call("ask_user", one)
    # option needs a label
    noLabel = {"questions": [{"question": "a?", "options": [{}, {}]}]}
    assert "label" in tools.validate_call("ask_user", noLabel)
    # too many questions
    many = {"questions": [{"question": f"q{i}?",
                           "options": [{"label": "a"}, {"label": "b"}]}
                          for i in range(5)]}
    assert "at most 4" in tools.validate_call("ask_user", many)


def test_ask_user_validation_accepts_good_call():
    good = {"questions": [{"question": "Which stack?", "header": "Stack",
                           "options": [{"label": "Next.js", "description": "React"},
                                       {"label": "Django"}]}]}
    assert tools.validate_call("ask_user", good) is None


def test_ask_user_summary():
    out = tools.summarize_call("ask_user", {"questions": [
        {"question": "Which database should we use?"}, {"question": "b?"}]})
    assert out.startswith("ask_user: Which database")
    assert "+1 more" in out
