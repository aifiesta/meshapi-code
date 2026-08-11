"""Tests for meshapi.client — the streaming OpenAI-compatible HTTP client.

No live network. `stream_chat` builds its own `httpx.stream(...)`, so we
monkeypatch that module attribute to a `@contextmanager` returning a fake
response whose `.iter_lines()` replays canned SSE `data: {...}` lines. This
mirrors the real wire exactly (the code only touches `.status_code`,
`.headers`, `.iter_lines()`, `.read()`, `.raise_for_status()`) without any
httpx transport/streaming subtleties.

The KEY tests are the `stream_chat` SSE cases — especially the in-band-error
regressions (HTTP 200 + `{"error": ...}` chunk used to be silently dropped,
which the user experienced as a silent hang) and the optimize-attempt
fallback (an in-band error on the optimized request must retry the raw one).
"""
import contextlib
import copy
import json

import httpx
import pytest

from meshapi import client


# --------------------------------------------------------------------------- #
# SSE / fake-response test harness
# --------------------------------------------------------------------------- #
def sse(obj) -> str:
    """One SSE data line, exactly as httpx.iter_lines() would hand it over
    (newline already stripped)."""
    return "data: " + json.dumps(obj)


DONE = "data: [DONE]"


class FakeResponse:
    """Stand-in for the streamed httpx.Response.

    stream_chat only ever calls these five members, so that is all we model.
    """

    def __init__(self, status_code=200, headers=None, lines=None):
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self._lines = list(lines or [])
        self._request = httpx.Request(
            "POST", "https://api.meshapi.ai/v1/chat/completions"
        )
        self.read_called = False

    def read(self):
        self.read_called = True
        return b""

    def iter_lines(self):
        for line in self._lines:
            yield line

    def raise_for_status(self):
        if self.status_code >= 400:
            resp = httpx.Response(
                self.status_code, headers=self.headers, request=self._request
            )
            raise httpx.HTTPStatusError(
                f"HTTP error {self.status_code}",
                request=self._request,
                response=resp,
            )
        return self


def install_stream(monkeypatch, attempts):
    """Patch `client.httpx.stream` to hand back one FakeResponse per call.

    `attempts` is a list of specs, one per expected HTTP attempt:
        {"status_code": int, "headers": dict, "lines": [str, ...]}
    Returns a `bodies` list that is appended with a deep copy of each
    request's JSON body, so tests can assert what was actually sent (e.g. the
    raw fallback payload on the second attempt).
    """
    bodies = []
    state = {"i": 0}

    @contextlib.contextmanager
    def fake_stream(method, url, *, json=None, headers=None, timeout=None, **kw):
        i = state["i"]
        state["i"] += 1
        bodies.append(copy.deepcopy(json))
        assert i < len(attempts), f"unexpected extra HTTP attempt #{i}"
        spec = attempts[i]
        yield FakeResponse(
            status_code=spec.get("status_code", 200),
            headers=spec.get("headers"),
            lines=spec.get("lines"),
        )

    monkeypatch.setattr(client.httpx, "stream", fake_stream)
    return bodies


def drive(gen):
    """Consume a stream_chat() generator, splitting its yields.

    Returns (text, progress, meta):
      - text:     concatenated content-delta strings
      - progress: list of the `stream_progress` payload dicts
      - meta:     the final meta dict (last non-progress dict), or None
    """
    text_parts, progress, metas = [], [], []
    for item in gen:
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, dict) and "stream_progress" in item:
            progress.append(item["stream_progress"])
        elif isinstance(item, dict):
            metas.append(item)
        else:  # pragma: no cover - guards against a new yield shape
            raise AssertionError(f"unexpected yield: {item!r}")
    return "".join(text_parts), progress, (metas[-1] if metas else None)


@pytest.fixture
def cfg():
    return {
        "base_url": "https://api.meshapi.ai/v1",
        "api_key": "rsk_test",
        "model": "anthropic/claude-opus-4.8",
    }


USER_MSGS = [{"role": "user", "content": "hi"}]


# --------------------------------------------------------------------------- #
# _drop_empty_assistant
# --------------------------------------------------------------------------- #
def test_drop_empty_assistant_removes_blank_variants():
    msgs = [
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": "   \n\t "},
    ]
    assert client._drop_empty_assistant(msgs) == []


def test_drop_empty_assistant_keeps_assistant_with_tool_calls():
    m = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_0", "type": "function", "function": {}}],
    }
    assert client._drop_empty_assistant([m]) == [m]


def test_drop_empty_assistant_keeps_real_text():
    m = {"role": "assistant", "content": "here is my answer"}
    assert client._drop_empty_assistant([m]) == [m]


def test_drop_empty_assistant_keeps_other_roles_even_when_blank():
    msgs = [
        {"role": "user", "content": ""},
        {"role": "tool", "content": "", "tool_call_id": "call_0"},
        {"role": "system", "content": "   "},
    ]
    assert client._drop_empty_assistant(msgs) == msgs


def test_drop_empty_assistant_keeps_multimodal_list_content_no_crash():
    # Regression: content is a list of blocks (multimodal). _blank() must not
    # call .strip() on a list, and the message must be KEPT.
    m = {
        "role": "assistant",
        "content": [{"type": "text", "text": "look at this"}],
    }
    assert client._drop_empty_assistant([m]) == [m]


def test_drop_empty_assistant_does_not_mutate_input():
    msgs = [
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "keep me"},
    ]
    snapshot = copy.deepcopy(msgs)
    out = client._drop_empty_assistant(msgs)
    assert msgs == snapshot           # input untouched
    assert out is not msgs            # a fresh list
    assert len(msgs) == 2 and len(out) == 1


# --------------------------------------------------------------------------- #
# build_payload
# --------------------------------------------------------------------------- #
def test_build_payload_model_from_cfg_when_not_auto(cfg):
    p = client.build_payload(USER_MSGS, cfg)
    assert p["model"] == "anthropic/claude-opus-4.8"
    assert p["stream"] is True
    assert p["messages"] is USER_MSGS


def test_build_payload_model_auto_when_auto_route(cfg):
    cfg["auto_route"] = True
    p = client.build_payload(USER_MSGS, cfg)
    assert p["model"] == "auto"


def test_build_payload_includes_fallback_models(cfg):
    cfg["fallback_models"] = ["openai/gpt-4o-mini", "anthropic/claude-opus-4.8"]
    p = client.build_payload(USER_MSGS, cfg)
    assert p["models"] == ["openai/gpt-4o-mini", "anthropic/claude-opus-4.8"]
    # copied, not aliased to the cfg list
    assert p["models"] is not cfg["fallback_models"]


def test_build_payload_omits_models_when_absent_or_empty(cfg):
    assert "models" not in client.build_payload(USER_MSGS, cfg)
    cfg["fallback_models"] = []
    assert "models" not in client.build_payload(USER_MSGS, cfg)


def test_build_payload_reasoning_effort_passthrough(cfg):
    cfg["reasoning_effort"] = "high"
    assert client.build_payload(USER_MSGS, cfg)["reasoning_effort"] == "high"
    # "none" is a real level (non-empty string → truthy) and is passed through
    cfg["reasoning_effort"] = "none"
    assert client.build_payload(USER_MSGS, cfg)["reasoning_effort"] == "none"


def test_build_payload_omits_reasoning_effort_when_none(cfg):
    cfg["reasoning_effort"] = None
    assert "reasoning_effort" not in client.build_payload(USER_MSGS, cfg)
    assert "reasoning_effort" not in client.build_payload(USER_MSGS, cfg)  # absent key


def test_build_payload_tools_and_tool_choice_only_when_tools(cfg):
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    p = client.build_payload(USER_MSGS, cfg, tools=tools)
    assert p["tools"] == tools
    assert p["tool_choice"] == "auto"

    p2 = client.build_payload(USER_MSGS, cfg)
    assert "tools" not in p2 and "tool_choice" not in p2

    p3 = client.build_payload(USER_MSGS, cfg, tools=[])  # empty tools == none
    assert "tools" not in p3 and "tool_choice" not in p3


# --------------------------------------------------------------------------- #
# ToolCallAccumulator
# --------------------------------------------------------------------------- #
def test_accumulator_normal_single_call_split_across_deltas():
    acc = client.ToolCallAccumulator()
    acc.add({"index": 0, "id": "call_1",
             "function": {"name": "read_file", "arguments": '{"path":'}})
    acc.add({"index": 0, "function": {"arguments": '"x"}'}})
    calls = acc.finalize()
    assert len(calls) == 1
    assert acc.dropped == 0
    call = calls[0]
    assert call["id"] == "call_1"
    assert call["name"] == "read_file"
    assert json.loads(call["arguments"]) == {"path": "x"}
    assert "_idx" not in call  # scratch key stripped


def test_accumulator_no_index_does_not_merge_parallel_calls():
    # Two parallel calls, neither carrying `index`, distinct ids. The old
    # `tc.get("index", 0)` merged them into one bucket = concatenated garbage.
    acc = client.ToolCallAccumulator()
    acc.add({"id": "call_a",
             "function": {"name": "read_file", "arguments": '{"path":"a"}'}})
    acc.add({"id": "call_b",
             "function": {"name": "read_file", "arguments": '{"path":"b"}'}})
    calls = acc.finalize()
    assert len(calls) == 2
    args = [json.loads(c["arguments"]) for c in calls]
    assert {"path": "a"} in args and {"path": "b"} in args
    ids = {c["id"] for c in calls}
    assert ids == {"call_a", "call_b"}


def test_accumulator_orphan_fragment_lower_index_two_pass_merge():
    # Named call at index 1 with empty args; the argument fragment arrived at a
    # LOWER index (0) with no name. The two-pass finalize must merge the donor
    # into the named call rather than dropping it (the old single-pass walk
    # processed the low-index donor before any named call existed).
    acc = client.ToolCallAccumulator()
    acc.add({"index": 1, "id": "call_1",
             "function": {"name": "write_file", "arguments": ""}})
    acc.add({"index": 0, "function": {"arguments": '{"path":"a.txt"}'}})
    calls = acc.finalize()
    assert len(calls) == 1
    assert acc.dropped == 0
    assert calls[0]["name"] == "write_file"
    assert json.loads(calls[0]["arguments"]) == {"path": "a.txt"}


def test_accumulator_missing_id_synthesized_to_call_n():
    acc = client.ToolCallAccumulator()
    acc.add({"index": 0,
             "function": {"name": "read_file", "arguments": '{"path":"x"}'}})
    calls = acc.finalize()
    assert len(calls) == 1
    assert calls[0]["id"] == "call_0"  # deterministic synthesis


def test_accumulator_nameless_empty_bucket_dropped_and_counted():
    acc = client.ToolCallAccumulator()
    acc.add({"id": "call_ghost"})  # id only: no name, no args → unexecutable
    calls = acc.finalize()
    assert calls == []
    assert acc.dropped == 1


def test_accumulator_ghost_dropped_alongside_good_call():
    acc = client.ToolCallAccumulator()
    acc.add({"index": 0, "id": "call_good",
             "function": {"name": "read_file", "arguments": '{"path":"x"}'}})
    acc.add({"index": 1, "id": "call_ghost"})  # nameless, argless
    calls = acc.finalize()
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert acc.dropped == 1


# --------------------------------------------------------------------------- #
# stream_chat — SSE behavior (the key regression tests)
# --------------------------------------------------------------------------- #
def test_stream_chat_normal_content_then_meta(monkeypatch, cfg):
    lines = [
        sse({"choices": [{"delta": {"content": "Hello"}}]}),
        sse({"choices": [{"delta": {"content": " world"}}]}),
        sse({"choices": [{"delta": {}}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5},
             "cost": "0.0001"}),
        DONE,
    ]
    install_stream(monkeypatch, [
        {"headers": {"x-resolved-model-id": "anthropic/claude-opus-4.8"},
         "lines": lines},
    ])
    text, _progress, meta = drive(client.stream_chat(USER_MSGS, cfg))

    assert text == "Hello world"
    assert meta is not None
    assert meta["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}
    assert meta["cost"] == "0.0001"
    # resolved-model header is authoritative when no chunk carries `model`
    assert meta["model"] == "anthropic/claude-opus-4.8"
    assert "error" not in meta


def test_stream_chat_inband_error_on_http_200_surfaces_as_meta_error(monkeypatch, cfg):
    # HTTP 200, then an in-band {"error": ...} chunk with no choices/usage.
    # This used to be silently dropped → the CLI "hang". It must surface.
    lines = [
        sse({"choices": [{"delta": {"content": "thinking"}}]}),
        sse({"error": {"message": "boom"}}),
        DONE,  # never reached (error breaks the loop) but harmless
    ]
    install_stream(monkeypatch, [{"lines": lines}])
    text, _progress, meta = drive(client.stream_chat(USER_MSGS, cfg))

    assert text == "thinking"
    assert meta is not None
    assert meta["error"] == "boom"


def test_stream_chat_inband_error_on_optimized_attempt_falls_back_to_raw(monkeypatch, cfg):
    # optimize dial > 0 → two attempts [optimized, raw]. An in-band error on
    # the FIRST (optimized) attempt must degrade to the raw request, and the
    # raw attempt succeeds. The optimize beta must never fail a turn.
    cfg["optimize"] = 0.3
    first_attempt = [sse({"error": {"message": "boom"}})]
    second_attempt = [
        sse({"choices": [{"delta": {"content": "recovered"}}]}),
        sse({"choices": [{"delta": {}}],
             "usage": {"prompt_tokens": 3}, "cost": "0.002"}),
        DONE,
    ]
    bodies = install_stream(monkeypatch, [
        {"lines": first_attempt},
        {"lines": second_attempt},
    ])
    text, _progress, meta = drive(client.stream_chat(USER_MSGS, cfg))

    # It actually retried: exactly two HTTP attempts were made.
    assert len(bodies) == 2
    # Attempt 1 = optimized (carries the optimize max_tokens lever);
    # attempt 2 = raw payload (no max_tokens).
    assert "max_tokens" in bodies[0]
    assert "max_tokens" not in bodies[1]

    # The raw attempt's output is what the caller sees — no surfaced error.
    assert text == "recovered"
    assert meta is not None
    assert "error" not in meta
    assert meta["cost"] == "0.002"
    # plan records the graceful degradation
    plan = meta["optimize_plan"]
    assert plan["levers_applied"] == []
    assert "in-band" in plan["degraded"]


def test_stream_chat_unparseable_sse_line_counts_dropped_chunks(monkeypatch, cfg):
    lines = [
        "data: {this is not valid json",      # unparseable → dropped_chunks += 1
        sse({"choices": [{"delta": {"content": "ok"}}]}),
        sse({"choices": [{"delta": {}}],
             "usage": {"prompt_tokens": 1}, "cost": "0"}),
        DONE,
    ]
    install_stream(monkeypatch, [{"lines": lines}])
    text, _progress, meta = drive(client.stream_chat(USER_MSGS, cfg))

    assert text == "ok"
    assert meta is not None
    assert meta["dropped_chunks"] == 1
    assert meta["dropped_sample"].startswith("{this is not valid json")


def test_stream_chat_http_4xx_raises_to_caller(monkeypatch, cfg):
    # A 4xx on the last (only) attempt propagates httpx.HTTPStatusError.
    install_stream(monkeypatch, [{"status_code": 400, "lines": []}])
    with pytest.raises(httpx.HTTPStatusError):
        list(client.stream_chat(USER_MSGS, cfg))


def test_stream_chat_tool_call_deltas_accumulate_into_meta(monkeypatch, cfg):
    lines = [
        sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_abc",
            "function": {"name": "write_file", "arguments": '{"path":"a.txt",'}
        }]}}]}),
        sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "function": {"arguments": '"content":"hi"}'}
        }]}}]}),
        sse({"choices": [{"delta": {}}],
             "usage": {"prompt_tokens": 4}, "cost": "0"}),
        DONE,
    ]
    install_stream(monkeypatch, [{"lines": lines}])
    text, progress, meta = drive(client.stream_chat(USER_MSGS, cfg))

    assert text == ""  # tool call, no assistant text
    assert meta is not None
    calls = meta["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["id"] == "call_abc"
    assert calls[0]["name"] == "write_file"
    assert json.loads(calls[0]["arguments"]) == {"path": "a.txt", "content": "hi"}
    # the spinner-feed progress events were emitted (and are NOT the meta dict)
    assert progress and progress[-1]["tool"] == "write_file"


def test_stream_chat_empty_stream_yields_no_error_and_empty_text(monkeypatch, cfg):
    # No content, no usage, no error — just an immediate [DONE]. The generator
    # must complete cleanly: empty text and no meta (nothing truthy to yield).
    install_stream(monkeypatch, [{"lines": [DONE]}])
    text, progress, meta = drive(client.stream_chat(USER_MSGS, cfg))

    assert text == ""
    assert progress == []
    assert meta is None  # last_meta stayed {} → nothing yielded
