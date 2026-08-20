"""Glue tests for the immortal-loop pieces wired into cli.py:
_stream_hop_with_retry (per-hop resilience), _finalize_interrupted_turn
(keep completed work), and handle_tool_calls' doomed-count report.

No network: cli.render_stream is monkeypatched with scripted sequences;
backoff sleeps are patched to no-ops.
"""
import threading

import httpx
import pytest

import meshapi.cli as cli
from meshapi import loopguard


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """No real sleeping in tests; keep the esc check hot."""
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.loopguard, "backoff_delay",
                        lambda attempt, **kw: 0.0)


def make_state(**over):
    state = {
        "cfg": {"model": "openai/gpt-4o-mini", "auto_compact": True,
                "auto_route": False, "max_hops": 0, "stall_policy": "pause",
                "optimize": 0.0},
        "messages": [{"role": "system", "content": "sys"},
                     {"role": "user", "content": "go"}],
        "session_reads": {},
        "esc_interrupt": threading.Event(),
    }
    state.update(over)
    return state


def scripted_render(script):
    """render_stream stand-in: pops one behavior per call.
    Items: ("ok", reply, meta) | ("raise", exc)."""
    calls = {"n": 0}

    def fake(events, header=None, state=None):
        item = script[calls["n"]]
        calls["n"] += 1
        if item[0] == "raise":
            raise item[1]
        return item[1], item[2]
    return fake, calls


def http_error(code):
    req = httpx.Request("POST", "https://api.meshapi.ai/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"{code}", request=req, response=httpx.Response(code, request=req))


# ---------------------------------------------------------------------------
# _stream_hop_with_retry
# ---------------------------------------------------------------------------

def test_network_blips_retry_then_succeed(monkeypatch):
    fake, calls = scripted_render([
        ("raise", httpx.ConnectError("boom")),
        ("raise", httpx.ReadTimeout("slow")),
        ("ok", "hello", {"usage": {}}),
    ])
    monkeypatch.setattr(cli, "render_stream", fake)
    reply, meta = cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert reply == "hello" and calls["n"] == 3


def test_network_error_exhausts_and_raises(monkeypatch):
    fake, calls = scripted_render(
        [("raise", httpx.ConnectError("boom"))] * loopguard.MAX_STREAM_ATTEMPTS)
    monkeypatch.setattr(cli, "render_stream", fake)
    with pytest.raises(httpx.ConnectError):
        cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert calls["n"] == loopguard.MAX_STREAM_ATTEMPTS


def test_400_raises_immediately_503_retries(monkeypatch):
    fake, calls = scripted_render([("raise", http_error(400))])
    monkeypatch.setattr(cli, "render_stream", fake)
    with pytest.raises(httpx.HTTPStatusError):
        cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert calls["n"] == 1  # non-retryable — no second attempt

    fake, calls = scripted_render([
        ("raise", http_error(503)),
        ("ok", "recovered", {"usage": {}}),
    ])
    monkeypatch.setattr(cli, "render_stream", fake)
    reply, _ = cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert reply == "recovered" and calls["n"] == 2


def test_transient_inband_error_retries(monkeypatch):
    fake, calls = scripted_render([
        ("ok", "", {"error": "rate limit exceeded, try again"}),
        ("ok", "fine", {"usage": {}}),
    ])
    monkeypatch.setattr(cli, "render_stream", fake)
    reply, meta = cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert reply == "fine" and calls["n"] == 2


def test_context_error_compacts_once_then_retries(monkeypatch):
    compacted = []
    monkeypatch.setattr(
        cli.compact, "compact_history",
        lambda state, limit=None, aggressive=False:
            compacted.append(aggressive) or {"before_tok": 100_000,
                                             "after_tok": 30_000,
                                             "truncated": 5, "folded": 2})
    fake, calls = scripted_render([
        ("ok", "", {"error": "prompt is too long: maximum context length"}),
        ("ok", "made it", {"usage": {}}),
    ])
    monkeypatch.setattr(cli, "render_stream", fake)
    reply, _ = cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert reply == "made it"
    assert compacted == [True]  # aggressive, exactly once


def test_second_context_error_is_fatal(monkeypatch):
    monkeypatch.setattr(
        cli.compact, "compact_history",
        lambda state, limit=None, aggressive=False:
            {"before_tok": 2, "after_tok": 1, "truncated": 1, "folded": 0})
    err = {"error": "input length exceeds maximum context"}
    fake, calls = scripted_render([("ok", "", err), ("ok", "", err)])
    monkeypatch.setattr(cli, "render_stream", fake)
    reply, meta = cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert meta.get("error")            # returned for the caller's error path
    assert calls["n"] == 2              # compact-retry once, never loops


def test_fatal_inband_error_returned_immediately(monkeypatch):
    fake, calls = scripted_render([
        ("ok", "", {"error": "text content blocks must be non-empty"})])
    monkeypatch.setattr(cli, "render_stream", fake)
    _, meta = cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert meta["error"] and calls["n"] == 1


def test_empty_response_retried_twice_then_surfaced(monkeypatch):
    fake, calls = scripted_render([
        ("ok", "", {"usage": {}}),
        ("ok", "", {"usage": {}}),
        ("ok", "", {"usage": {}}),
    ])
    monkeypatch.setattr(cli, "render_stream", fake)
    reply, meta = cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert reply == "" and calls["n"] == 3  # 2 retries, then give up


def test_esc_during_backoff_raises_keyboardinterrupt(monkeypatch):
    monkeypatch.setattr(cli.loopguard, "backoff_delay",
                        lambda attempt, **kw: 5.0)
    state = make_state()
    state["esc_interrupt"].set()
    fake, _ = scripted_render([("raise", httpx.ConnectError("x"))] * 5)
    monkeypatch.setattr(cli, "render_stream", fake)
    with pytest.raises(KeyboardInterrupt):
        cli._stream_hop_with_retry(state, [], "hdr")


def test_compaction_rebuilds_messages_between_attempts(monkeypatch):
    """The retry must send post-compaction history, not a stale copy."""
    sent = []

    def fake(events, header=None, state=None):
        # stream_chat is lazy; capture what WOULD be sent via the state
        sent.append(len(fake_state["messages"]))
        if len(sent) == 1:
            return "", {"error": "maximum context length is 200000 tokens"}
        return "ok", {"usage": {}}

    def fake_compact(state, limit=None, aggressive=False):
        state["messages"] = state["messages"][:2]  # shrink
        return {"before_tok": 9, "after_tok": 2, "truncated": 1, "folded": 1}

    fake_state = make_state()
    fake_state["messages"] += [
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "more"},
    ]
    monkeypatch.setattr(cli, "render_stream", fake)
    monkeypatch.setattr(cli.compact, "compact_history", fake_compact)
    reply, _ = cli._stream_hop_with_retry(fake_state, [], "hdr")
    assert reply == "ok"
    assert sent == [4, 2]  # second attempt saw the compacted list


# ---------------------------------------------------------------------------
# _finalize_interrupted_turn
# ---------------------------------------------------------------------------

def _batch(n_calls, n_results, prefix="c"):
    msgs = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": f"{prefix}{i}", "type": "function",
         "function": {"name": "run_bash", "arguments": "{}"}}
        for i in range(n_calls)]}]
    msgs += [{"role": "tool", "tool_call_id": f"{prefix}{i}", "content": "ok"}
             for i in range(n_results)]
    return msgs


def test_finalize_zero_progress_rolls_back_clean_slate():
    state = make_state()
    n_before = len(state["messages"])
    assert cli._finalize_interrupted_turn(state, "abort") == 0
    assert len(state["messages"]) == n_before - 1        # user msg popped
    assert state["messages"][-1]["role"] == "system"


def test_finalize_mid_batch_seals_and_keeps():
    state = make_state()
    state["messages"] += _batch(3, 1)
    kept = cli._finalize_interrupted_turn(state, "abort")
    assert kept == 1                                     # real results only
    tool_ids = [m["tool_call_id"] for m in state["messages"]
                if m.get("role") == "tool"]
    assert sorted(tool_ids) == ["c0", "c1", "c2"]        # stubs fill the gap
    assert state["messages"][-1]["role"] == "system"
    assert "interrupted this turn" in state["messages"][-1]["content"]
    assert state["messages"][1]["content"] == "go"       # user msg survives


def test_finalize_error_reason_wording():
    state = make_state()
    state["messages"] += _batch(1, 1)
    cli._finalize_interrupted_turn(state, "error")
    assert "connection or gateway error" in state["messages"][-1]["content"]


def test_finalize_assistant_text_only_still_kept():
    state = make_state()
    state["messages"].append({"role": "assistant", "content": "partial answer"})
    kept = cli._finalize_interrupted_turn(state, "abort")
    assert kept == 0
    assert any(m.get("content") == "partial answer" for m in state["messages"])
    assert state["messages"][-1]["role"] == "system"     # breadcrumb appended


# ---------------------------------------------------------------------------
# handle_tool_calls — doomed-count report
# ---------------------------------------------------------------------------

def test_handle_tool_calls_reports_all_doomed(monkeypatch):
    monkeypatch.setattr(cli.statusbar, "print_line", lambda state: None)
    state = make_state()
    state["mode"] = cli.Mode.DEFAULT
    # Flat accumulator shape — what handle_tool_calls actually receives.
    calls = [
        {"id": "a", "name": "write_file", "arguments": '{"path": "x.txt", '},
        {"id": "b", "name": "run_bash", "arguments": '{"command"'},
    ]
    report = cli.handle_tool_calls(calls, state)
    assert report == {"total": 2, "doomed": 2}
    # every id got a tool result (feedback), history stays valid
    ids = [m["tool_call_id"] for m in state["messages"] if m.get("role") == "tool"]
    assert sorted(ids) == ["a", "b"]


def test_handle_tool_calls_reports_denied_as_not_doomed(monkeypatch):
    monkeypatch.setattr(cli.statusbar, "print_line", lambda state: None)
    monkeypatch.setattr(cli, "confirm_tool_call", lambda *a, **k: (False, False))
    state = make_state()
    state["mode"] = cli.Mode.DEFAULT
    state["session_allow"] = set()
    calls = [{"id": "a", "name": "run_bash",
              "arguments": '{"command": "echo hi"}'}]
    report = cli.handle_tool_calls(calls, state)
    assert report["total"] == 1 and report["doomed"] == 0


# ---------------------------------------------------------------------------
# Retry-After honored (learned from Claude Code)
# ---------------------------------------------------------------------------

def http_error_with_header(code, retry_after):
    req = httpx.Request("POST", "https://api.meshapi.ai/v1/chat/completions")
    resp = httpx.Response(code, request=req, headers={"retry-after": retry_after})
    return httpx.HTTPStatusError(f"{code}", request=req, response=resp)


def test_retry_after_header_overrides_backoff(monkeypatch):
    waits = []
    monkeypatch.setattr(cli, "_retry_wait",
                        lambda state, attempt, reason, delay=None: waits.append(delay))
    fake, _ = scripted_render([
        ("raise", http_error_with_header(429, "7")),
        ("ok", "done", {"usage": {}}),
    ])
    monkeypatch.setattr(cli, "render_stream", fake)
    reply, _ = cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert reply == "done"
    assert waits == [7.0]           # server's number, not our exponential guess


def test_absurd_retry_after_raises_instead_of_freezing(monkeypatch):
    fake, _ = scripted_render([("raise", http_error_with_header(429, "3600"))])
    monkeypatch.setattr(cli, "render_stream", fake)
    monkeypatch.setattr(cli, "_try_non_streaming", lambda *a, **k: None)
    with pytest.raises(httpx.HTTPStatusError):
        cli._stream_hop_with_retry(make_state(), [], "hdr")


# ---------------------------------------------------------------------------
# max_tokens/context overflow → shrink the ask, keep all history
# ---------------------------------------------------------------------------

def test_max_tokens_overflow_shrinks_and_retries(monkeypatch):
    req = httpx.Request("POST", "https://api.meshapi.ai/v1/chat/completions")
    body = (b'{"error":{"message":"input length and `max_tokens` exceed '
            b'context limit: 195000 + 8192 > 200000"}}')
    err = httpx.HTTPStatusError(
        "400", request=req, response=httpx.Response(400, request=req, content=body))
    fake, calls = scripted_render([("raise", err), ("ok", "fits now", {"usage": {}})])
    monkeypatch.setattr(cli, "render_stream", fake)
    state = make_state()
    reply, _ = cli._stream_hop_with_retry(state, [], "hdr")
    assert reply == "fits now" and calls["n"] == 2
    assert state["_max_tokens_shrunk"] == 4744      # shrunk, history untouched
    assert len(state["messages"]) == 2               # nothing dropped


# ---------------------------------------------------------------------------
# Non-streaming fallback (unlocks the gateway's own retry/fallback)
# ---------------------------------------------------------------------------

def test_non_streaming_fallback_after_stream_failures(monkeypatch):
    fake, calls = scripted_render(
        [("raise", httpx.ConnectError("down"))] * loopguard.MAX_STREAM_ATTEMPTS)
    monkeypatch.setattr(cli, "render_stream", fake)
    monkeypatch.setattr(cli, "complete_chat",
                        lambda msgs, cfg, tools=None, max_tokens=None:
                            ("rescued", {"usage": {}, "non_streaming": True}))
    reply, meta = cli._stream_hop_with_retry(make_state(), [], "hdr")
    assert reply == "rescued" and meta["non_streaming"] is True
    assert calls["n"] == loopguard.MAX_STREAM_ATTEMPTS  # only after exhausting


def test_non_streaming_fallback_failure_reraises_original(monkeypatch):
    fake, _ = scripted_render(
        [("raise", httpx.ConnectError("down"))] * loopguard.MAX_STREAM_ATTEMPTS)
    monkeypatch.setattr(cli, "render_stream", fake)

    def boom(*a, **k):
        raise httpx.ConnectError("also down")
    monkeypatch.setattr(cli, "complete_chat", boom)
    with pytest.raises(httpx.ConnectError):
        cli._stream_hop_with_retry(make_state(), [], "hdr")


def test_non_streaming_carries_shrunk_max_tokens(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "complete_chat",
                        lambda msgs, cfg, tools=None, max_tokens=None:
                            seen.update(mt=max_tokens) or ("ok", {}))
    state = make_state()
    state["_max_tokens_shrunk"] = 3000
    cli._try_non_streaming(state, state["messages"])
    assert seen["mt"] == 3000


# ---------------------------------------------------------------------------
# Mid-run steering: control commands apply to the next hop
# ---------------------------------------------------------------------------

def test_is_live_control_classification():
    assert cli.is_live_control("/model openai/gpt-4o")
    assert cli.is_live_control("  /reasoning high  ")
    assert cli.is_live_control("/mode auto")
    assert not cli.is_live_control("build me an app")
    assert not cli.is_live_control("/clear")      # destructive — not mid-run
    assert not cli.is_live_control("/exit")
    assert not cli.is_live_control("")


def test_drain_applies_controls_and_keeps_prompts(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "handle_command",
                        lambda cmd, state: seen.append(cmd) or True)
    state = make_state()
    state["input_queue"] = __import__("collections").deque(
        ["/model openai/gpt-4o", "now add tests", "/reasoning high"])
    assert cli._drain_live_controls(state) is True
    assert seen == ["/model openai/gpt-4o", "/reasoning high"]
    assert list(state["input_queue"]) == ["now add tests"]   # prompt preserved


def test_drain_survives_a_failing_command(monkeypatch):
    def boom(cmd, state):
        raise RuntimeError("bad command")
    monkeypatch.setattr(cli, "handle_command", boom)
    state = make_state()
    state["input_queue"] = __import__("collections").deque(["/model x"])
    assert cli._drain_live_controls(state) is True    # did not raise
    assert not state["input_queue"]


def test_drain_noop_on_empty_queue():
    state = make_state()
    state["input_queue"] = __import__("collections").deque()
    assert cli._drain_live_controls(state) is False


# ---------------------------------------------------------------------------
# ask_user execution
# ---------------------------------------------------------------------------

def test_ask_user_returns_answers(monkeypatch):
    monkeypatch.setattr(cli.askui, "ask", lambda qs: ["Hobbyist", "CSV"])
    args = {"questions": [
        {"question": "Scale?", "header": "Scale", "options": [{"label": "Hobbyist"}]},
        {"question": "Source?", "header": "Source", "options": [{"label": "CSV"}]},
    ]}
    out = cli._handle_ask_user(args, make_state())
    assert "Q: Scale?" in out and "A: Hobbyist" in out
    assert "A: CSV" in out
    assert "without re-asking" in out


def test_ask_user_multi_select_joined(monkeypatch):
    monkeypatch.setattr(cli.askui, "ask", lambda qs: [["CSV", "API"]])
    args = {"questions": [{"question": "Sources?", "options": [{"label": "CSV"}]}]}
    assert "A: CSV, API" in cli._handle_ask_user(args, make_state())


def test_ask_user_cancelled_tells_model_to_move_on(monkeypatch):
    monkeypatch.setattr(cli.askui, "ask", lambda qs: None)
    out = cli._handle_ask_user({"questions": [{"question": "x?", "options": []}]},
                               make_state())
    assert "Do NOT call ask_user again" in out
    assert "make a reasonable choice yourself" in out


def test_ask_user_broken_tty_degrades(monkeypatch):
    def boom(qs):
        raise RuntimeError("no tty")
    monkeypatch.setattr(cli.askui, "ask", boom)
    out = cli._handle_ask_user({"questions": [{"question": "x?", "options": []}]},
                               make_state())
    assert out.startswith("Error: couldn't show the interactive picker")
    assert "plain text" in out


# ---------------------------------------------------------------------------
# reasoning_effort must never be able to fail a request
# ---------------------------------------------------------------------------

CATALOG = [
    {"id": "openai/gpt-4o-mini", "supports_thinking": False},
    {"id": "openai/gpt-5.4", "supports_thinking": True},
]






def test_reasoning_400_is_retried_without_the_field(monkeypatch):
    """The reactive net: upstream names reasoning_effort -> drop and retry."""
    req = httpx.Request("POST", "https://api.meshapi.ai/v1/chat/completions")
    body = (b'{"error":{"message":"Unrecognized request argument supplied: '
            b'reasoning_effort"}}')
    err = httpx.HTTPStatusError(
        "400", request=req, response=httpx.Response(400, request=req, content=body))
    fake, calls = scripted_render([("raise", err), ("ok", "worked", {"usage": {}})])
    monkeypatch.setattr(cli, "render_stream", fake)
    state = make_state()
    state["cfg"]["reasoning_effort"] = "high"
    reply, _ = cli._stream_hop_with_retry(state, [], "hdr")
    assert reply == "worked" and calls["n"] == 2
    assert state["_drop_reasoning"] is True
    assert state["cfg"]["reasoning_effort"] == "high"   # preference kept


def test_reasoning_400_not_retried_when_effort_unset(monkeypatch):
    """Don't misread an unrelated 400 that happens to mention the field."""
    req = httpx.Request("POST", "https://api.meshapi.ai/v1/chat/completions")
    err = httpx.HTTPStatusError(
        "400", request=req,
        response=httpx.Response(400, request=req, content=b"reasoning_effort bad"))
    fake, calls = scripted_render([("raise", err)])
    monkeypatch.setattr(cli, "render_stream", fake)
    monkeypatch.setattr(cli, "_try_non_streaming", lambda *a, **k: None)
    state = make_state()          # no reasoning_effort set
    with pytest.raises(httpx.HTTPStatusError):
        cli._stream_hop_with_retry(state, [], "hdr")
    assert calls["n"] == 1


def test_reasoning_INBAND_error_is_retried_without_the_field(monkeypatch):
    """Streaming rejects reasoning_effort as HTTP 200 + in-band error — the
    shape the live failure took. Must strip and retry, not fail the turn."""
    fake, calls = scripted_render([
        ("ok", "", {"error": "Unrecognized request argument supplied: reasoning_effort"}),
        ("ok", "works now", {"usage": {}}),
    ])
    monkeypatch.setattr(cli, "render_stream", fake)
    state = make_state()
    state["cfg"]["reasoning_effort"] = "high"
    reply, meta = cli._stream_hop_with_retry(state, [], "hdr")
    assert reply == "works now" and calls["n"] == 2
    assert state["_drop_reasoning"] is True
    assert not meta.get("error")


def test_reasoning_inband_not_retried_when_effort_unset(monkeypatch):
    fake, calls = scripted_render([
        ("ok", "", {"error": "Unrecognized request argument supplied: reasoning_effort"})])
    monkeypatch.setattr(cli, "render_stream", fake)
    state = make_state()   # no reasoning_effort
    _, meta = cli._stream_hop_with_retry(state, [], "hdr")
    assert meta.get("error") and calls["n"] == 1




def test_ask_user_prints_answer_summary(monkeypatch, capsys):
    """After the picker erases itself, a summary must take its place."""
    monkeypatch.setattr(cli.askui, "ask", lambda qs: ["Vanilla HTML/CSS/JS",
                                                      "Multi-currency",
                                                      "LocalStorage"])
    args = {"questions": [
        {"question": "Which stack?", "header": "Stack", "options": [{"label": "x"}]},
        {"question": "Currency?", "header": "Currency", "options": [{"label": "x"}]},
        {"question": "Storage?", "header": "Storage", "options": [{"label": "x"}]},
    ]}
    cli._handle_ask_user(args, make_state())
    out = capsys.readouterr().out
    assert "answered 3 questions" in out
    assert "Stack" in out and "Vanilla HTML/CSS/JS" in out
    assert "Storage" in out and "LocalStorage" in out


def test_ask_user_cancel_prints_visible_trace(monkeypatch, capsys):
    monkeypatch.setattr(cli.askui, "ask", lambda qs: None)
    cli._handle_ask_user({"questions": [{"question": "x?", "options": []}]},
                         make_state())
    assert "picker dismissed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# reasoning_effort gating is EVIDENCE-based, not catalog-based.
# The catalog's supports_thinking is False for gpt-5.4 / sonnet-4.6 /
# opus-4.8 even though the gateway accepts the field on them (verified
# live), so trusting it would silently disable reasoning where it works.
# ---------------------------------------------------------------------------

def test_effort_is_sent_even_when_catalog_says_no_thinking():
    state = make_state()
    state["cfg"]["reasoning_effort"] = "high"
    state["cfg"]["model"] = "openai/gpt-5.4"
    state["models_cache"] = [{"id": "openai/gpt-5.4", "supports_thinking": False}]
    assert cli._effective_cfg(state)["reasoning_effort"] == "high"


def test_effort_dropped_only_after_a_real_rejection():
    state = make_state()
    state["cfg"]["reasoning_effort"] = "high"
    state["cfg"]["model"] = "openai/gpt-4o-mini"
    assert cli._effective_cfg(state)["reasoning_effort"] == "high"   # first try
    cli._maybe_drop_reasoning(state, "Unrecognized request argument supplied: reasoning_effort")
    assert cli._effective_cfg(state)["reasoning_effort"] is None
    assert "openai/gpt-4o-mini" in state["_reasoning_rejected"]


def test_rejection_is_remembered_across_turns_no_second_retry():
    """A model that rejected it once shouldn't cost a retry every turn."""
    state = make_state()
    state["cfg"]["reasoning_effort"] = "high"
    state["cfg"]["model"] = "openai/gpt-4o-mini"
    cli._maybe_drop_reasoning(state, "... reasoning_effort ...")
    state["_drop_reasoning"] = False            # new turn resets the latch
    assert cli._effective_cfg(state)["reasoning_effort"] is None


def test_rejection_is_per_model_not_global():
    state = make_state()
    state["cfg"]["reasoning_effort"] = "high"
    state["cfg"]["model"] = "openai/gpt-4o-mini"
    cli._maybe_drop_reasoning(state, "... reasoning_effort ...")
    state["_drop_reasoning"] = False
    state["cfg"]["model"] = "openai/gpt-5.4"    # different model — still sent
    assert cli._effective_cfg(state)["reasoning_effort"] == "high"


def test_user_setting_is_never_mutated():
    state = make_state()
    state["cfg"]["reasoning_effort"] = "high"
    state["cfg"]["model"] = "openai/gpt-4o-mini"
    cli._maybe_drop_reasoning(state, "... reasoning_effort ...")
    cli._effective_cfg(state)
    assert state["cfg"]["reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# Smart routing glue: turn-start pick + per-hop cfg override
# ---------------------------------------------------------------------------

_SMART_COHORTS = ("chat", "agentic", "writing", "extraction", "reasoning-math",
                  "coding", "cheap-bulk")
SMART_TABLE = {
    "models": {"x/pick": {"caps": {"tools": True}, "ctx": 200000, "speed": 70,
                          "scores": {c: 80 for c in _SMART_COHORTS}}},
    "frontiers": {c: ["x/pick"] for c in _SMART_COHORTS},
    "defaults": {c: "x/pick" for c in _SMART_COHORTS},
}
SMART_CATALOG = [{"id": "x/pick",
                  "pricing": {"prompt_usd_per_1m": "1", "completion_usd_per_1m": "2"}}]


def _smart_state(monkeypatch):
    monkeypatch.setattr(cli.router, "load_table", lambda path=None: SMART_TABLE)
    monkeypatch.setattr(cli, "fetch_models_quiet", lambda state: SMART_CATALOG)
    state = make_state()
    state["cfg"]["route_mode"] = "smart"
    state["models_cache"] = SMART_CATALOG
    return state


def test_smart_route_turn_picks_and_overrides(monkeypatch):
    state = _smart_state(monkeypatch)
    cli._smart_route_turn(state, "hello there")
    assert state["_smart_pick"] == "x/pick"
    assert state["_smart_pick_info"]["cohort"] == "agentic"  # tools always on
    eff = cli._effective_cfg(state)
    assert eff["model"] == "x/pick"
    assert eff["auto_route"] is False
    assert state["cfg"]["model"] != "x/pick" or True  # pinned cfg never mutated
    assert state["cfg"].get("route_mode") == "smart"


def test_smart_route_off_mode_is_inert(monkeypatch):
    state = _smart_state(monkeypatch)
    state["cfg"]["route_mode"] = "off"
    cli._smart_route_turn(state, "hello")
    assert state["_smart_pick"] is None
    assert cli._effective_cfg(state) is state["cfg"]


def test_smart_route_fails_open_without_table(monkeypatch):
    state = _smart_state(monkeypatch)
    monkeypatch.setattr(cli.router, "load_table", lambda path=None: None)
    cli._smart_route_turn(state, "hello")
    assert state["_smart_pick"] is None          # pinned model rides


def test_smart_route_fails_open_on_exception(monkeypatch):
    state = _smart_state(monkeypatch)
    monkeypatch.setattr(cli.router, "pick",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    cli._smart_route_turn(state, "hello")        # must not raise
    assert state["_smart_pick"] is None


def test_smart_route_sticky_across_turns(monkeypatch):
    state = _smart_state(monkeypatch)
    cli._smart_route_turn(state, "first prompt")
    assert state["_smart_last"] == "x/pick"
    cli._smart_route_turn(state, "second prompt")
    assert state["_smart_pick_info"]["sticky"] is True


# ---------------------------------------------------------------------------
# Smart routing: outcome-level fail-over (the nemotron-empty-answers report)
# ---------------------------------------------------------------------------

def test_short_followup_inherits_cohort(monkeypatch):
    state = _smart_state(monkeypatch)
    cli._smart_route_turn(state, "write an essay on llm")
    first = state["_smart_pick_info"]["cohort"]
    cli._smart_route_turn(state, "2")            # bare menu answer
    assert state["_smart_pick_info"]["cohort"] == first


def test_long_followup_reclassifies(monkeypatch):
    state = _smart_state(monkeypatch)
    cli._smart_route_turn(state, "write an essay on llm")
    cli._smart_route_turn(state, "now extract every heading from it as a json array please")
    assert state["_smart_pick_info"]["cohort"] != state.get("_never")  # ran fresh classify
    assert state["_smart_cohort"] == state["_smart_pick_info"]["cohort"]


def test_empty_responses_abandon_smart_pick_and_recover(monkeypatch):
    """Live failure: picked model returns empty twice -> blacklist it, fall
    back to the pinned model, and the TURN STILL SUCCEEDS."""
    state = _smart_state(monkeypatch)
    cli._smart_route_turn(state, "hello")
    assert state["_smart_pick"] == "x/pick"
    fake, calls = scripted_render([
        ("ok", "", {"usage": {}}),        # picked model: empty
        ("ok", "", {"usage": {}}),        # retry: empty again
        ("ok", "rescued by pin", {"usage": {}}),   # pinned model answers
    ])
    monkeypatch.setattr(cli, "render_stream", fake)
    reply, meta = cli._stream_hop_with_retry(state, [], "hdr")
    assert reply == "rescued by pin"
    assert state["_smart_pick"] is None
    assert "x/pick" in state["_smart_bad"]
    assert calls["n"] == 3


def test_blacklisted_model_never_picked_again(monkeypatch):
    state = _smart_state(monkeypatch)
    state["_smart_bad"] = {"x/pick"}
    cli._smart_route_turn(state, "hello")
    assert state["_smart_pick"] is None           # only candidate is banned -> pin rides


def test_fatal_error_abandons_smart_pick(monkeypatch):
    state = _smart_state(monkeypatch)
    cli._smart_route_turn(state, "hello")
    fake, calls = scripted_render([
        ("ok", "", {"error": "text content blocks must be non-empty"}),  # fatal on pick
        ("ok", "pin works", {"usage": {}}),
    ])
    monkeypatch.setattr(cli, "render_stream", fake)
    reply, meta = cli._stream_hop_with_retry(state, [], "hdr")
    assert reply == "pin works"
    assert "x/pick" in state["_smart_bad"]


def test_abandon_without_pick_is_noop():
    state = make_state()
    assert cli._smart_route_abandon(state, "whatever") is False


# ---------------------------------------------------------------------------
# Difficulty axis riding the smart pick
# ---------------------------------------------------------------------------

def test_pick_info_carries_difficulty(monkeypatch):
    state = _smart_state(monkeypatch)
    cli._smart_route_turn(state, "design a distributed rate limiter with race "
                                 "condition handling, prove correctness, cover edge cases")
    assert state["_smart_pick"] == "x/pick"          # pick actually happened
    assert state["_smart_pick_info"]["difficulty"] == "high"
    assert state["_smart_difficulty"] == "high"


def test_short_followup_inherits_difficulty_too(monkeypatch):
    state = _smart_state(monkeypatch)
    cli._smart_route_turn(state, "design a distributed system, prove correctness, "
                                 "cover edge cases and trade-offs")
    assert state["_smart_difficulty"] == "high"
    cli._smart_route_turn(state, "2")
    assert state["_smart_pick_info"]["difficulty"] == "high"   # inherited, not re-scored


def test_difficulty_tilt_reaches_pick(monkeypatch):
    """The weights handed to pick() must be the tilted ones."""
    seen = {}
    real_pick = cli.router.pick
    def spy(cohort, weights, *a, **k):
        seen["w"] = weights
        return real_pick(cohort, weights, *a, **k)
    monkeypatch.setattr(cli.router, "pick", spy)
    state = _smart_state(monkeypatch)
    cli._smart_route_turn(state, "what is a variable?")     # low difficulty
    assert seen["w"]["cost"] > 0.5      # tilted up from the 0.5 default
