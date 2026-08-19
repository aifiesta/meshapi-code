"""Unit tests for meshapi.loopguard — the stall detector, retry policy,
and interrupted-turn history repair that replaced the blind hop cap.

Pure functions + a small stateful detector; no network, no I/O.
"""
import pytest

from meshapi import loopguard
from meshapi.loopguard import (
    StallDetector, backoff_delay, batch_signature, classify_inband_error,
    completed_actions_since_user, is_retryable_status, seal_partial_batch,
)


def call(name, args, cid="x"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args}}


# ---------------------------------------------------------------------------
# batch_signature
# ---------------------------------------------------------------------------

def test_signature_ignores_ids_and_key_order():
    a = batch_signature([call("write_file", '{"path": "a", "content": "x"}', "call_1")])
    b = batch_signature([call("write_file", '{"content":"x","path":"a"}', "call_99")])
    assert a == b


def test_signature_ignores_batch_order():
    a = batch_signature([call("read_file", '{"path":"a"}'), call("read_file", '{"path":"b"}')])
    b = batch_signature([call("read_file", '{"path":"b"}'), call("read_file", '{"path":"a"}')])
    assert a == b


def test_signature_differs_on_args():
    assert (batch_signature([call("read_file", '{"path":"a"}')])
            != batch_signature([call("read_file", '{"path":"b"}')]))


def test_signature_unparseable_args_fall_back_to_raw():
    a = batch_signature([call("run_bash", '{"command": broken')])
    b = batch_signature([call("run_bash", '  {"command": broken ')])
    assert a == b  # stripped raw match


# ---------------------------------------------------------------------------
# StallDetector — period-1 (AAA)
# ---------------------------------------------------------------------------

def test_aaa_nudge_renudge_stop_thresholds():
    d = StallDetector()
    seen = [d.observe("A") for _ in range(9)]
    assert seen[2] == "nudge"     # 3rd identical hop
    assert seen[5] == "renudge"   # 6th
    assert seen[8] == "stop"      # 9th
    # in-between hops stay quiet
    assert seen[3] is None and seen[4] is None and seen[6] is None


def test_progress_resets_the_streak():
    d = StallDetector()
    d.observe("A"); d.observe("A")
    assert d.observe("B") is None       # different sig — no cycle
    assert d.observe("A") is None
    assert d.observe("A") is None       # only 2 trailing As now


# ---------------------------------------------------------------------------
# StallDetector — period-2 (ABAB) at 2x thresholds; ABC never detects
# ---------------------------------------------------------------------------

def test_abab_detected_at_double_hops():
    d = StallDetector()
    results = []
    for i in range(18):
        results.append(d.observe("A" if i % 2 == 0 else "B"))
    assert results[5] == "nudge"     # 6 hops = 3 AB cycles
    assert results[11] == "renudge"  # 12 hops
    assert results[17] == "stop"     # 18 hops


def test_abc_rotation_never_flags():
    d = StallDetector()
    for i in range(30):
        assert d.observe("ABC"[i % 3]) is None


# ---------------------------------------------------------------------------
# StallDetector — doomed-hop wall
# ---------------------------------------------------------------------------

def test_doom_stop_after_consecutive_all_doomed_hops():
    d = StallDetector()
    out = None
    for i in range(loopguard.DOOM_STOP_HOPS):
        out = d.observe(f"sig{i}", all_doomed=True)  # differing sigs — no cycle
    assert out == "stop"


def test_doom_counter_resets_on_clean_hop():
    d = StallDetector()
    for i in range(loopguard.DOOM_STOP_HOPS - 1):
        assert d.observe(f"s{i}", all_doomed=True) != "stop"
    assert d.observe("clean", all_doomed=False) is None
    assert d.observe("s-again", all_doomed=True) != "stop"  # streak restarted


# ---------------------------------------------------------------------------
# Retry policy helpers
# ---------------------------------------------------------------------------

def test_retryable_statuses():
    for code in (408, 429, 500, 502, 503, 504, 529):
        assert is_retryable_status(code)
    for code in (400, 401, 403, 404, 422):
        assert not is_retryable_status(code)


def test_backoff_is_deterministic_with_injected_rng_and_capped():
    assert backoff_delay(1, base=1.0, cap=30.0, rng=lambda: 1.0) == 1.0
    assert backoff_delay(2, base=1.0, cap=30.0, rng=lambda: 1.0) == 2.0
    assert backoff_delay(10, base=1.0, cap=30.0, rng=lambda: 1.0) == 30.0  # capped
    # jitter floor is half the raw delay
    assert backoff_delay(3, base=1.0, cap=30.0, rng=lambda: 0.0) == 2.0


@pytest.mark.parametrize("msg, expected", [
    ("This model's maximum context length is 200000 tokens", "context"),
    ("ValidationException: prompt is too long", "context"),
    ("input length exceeds limit", "context"),
    ("rate limit exceeded, retry later", "transient"),
    ("Error 429: Too Many Requests", "transient"),
    ("upstream temporarily overloaded", "transient"),
    ("request timed out", "transient"),
    ("503 Service Unavailable", "transient"),
    ("text content blocks must be non-empty", "fatal"),
    ("invalid tool schema", "fatal"),
    ("", "fatal"),
])
def test_classify_inband_error(msg, expected):
    assert classify_inband_error(msg) == expected


# ---------------------------------------------------------------------------
# seal_partial_batch
# ---------------------------------------------------------------------------

def _batch_msgs(n_calls, n_results):
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": "run_bash", "arguments": "{}"}}
                for i in range(n_calls)]}]
    for i in range(n_results):
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
    return msgs


def test_seal_clean_history_adds_nothing():
    msgs = _batch_msgs(2, 2)
    assert seal_partial_batch(msgs) == 0
    assert len(msgs) == 4


def test_seal_fills_missing_results():
    msgs = _batch_msgs(3, 1)
    assert seal_partial_batch(msgs) == 2
    ids = [m["tool_call_id"] for m in msgs if m.get("role") == "tool"]
    assert sorted(ids) == ["c0", "c1", "c2"]
    assert "interrupted" in msgs[-1]["content"]


def test_seal_ignores_text_only_assistant_tail():
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "done"}]
    assert seal_partial_batch(msgs) == 0


def test_seal_ignores_history_ending_on_user():
    msgs = [{"role": "user", "content": "hi"}]
    assert seal_partial_batch(msgs) == 0


def test_seal_stops_at_intervening_system_message():
    msgs = _batch_msgs(2, 1)
    msgs.append({"role": "system", "content": "[breadcrumb]"})
    # batch is no longer trailing — don't stub across the system message
    assert seal_partial_batch(msgs) == 0


# ---------------------------------------------------------------------------
# completed_actions_since_user
# ---------------------------------------------------------------------------

def test_completed_actions_counts_tool_results_since_last_user():
    msgs = _batch_msgs(2, 2)
    assert completed_actions_since_user(msgs) == 2
    msgs.append({"role": "user", "content": "next"})
    assert completed_actions_since_user(msgs) == 0


def test_stop_reason_and_cycles_exposed():
    d = StallDetector()
    for _ in range(9):
        d.observe("A")
    assert d.stop_reason == "repeat"
    assert d.last_cycles == 9
    d2 = StallDetector()
    for i in range(loopguard.DOOM_STOP_HOPS):
        d2.observe(f"s{i}", all_doomed=True)
    assert d2.stop_reason == "doom"


def test_signature_accepts_flat_accumulator_shape():
    """handle_tool_calls receives FLAT {id, name, arguments} dicts from
    ToolCallAccumulator.finalize() — the signature must see the real args,
    not hash every batch to the same empty value (which would fire false
    stalls after 3 hops)."""
    flat = [{"id": "c1", "name": "read_file", "arguments": '{"path":"a"}'}]
    nested = [call("read_file", '{"path":"a"}', "c9")]
    assert batch_signature(flat) == batch_signature(nested)
    other = [{"id": "c1", "name": "read_file", "arguments": '{"path":"b"}'}]
    assert batch_signature(flat) != batch_signature(other)


# ---------------------------------------------------------------------------
# Retry-After (server-stated wait beats our exponential guess)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, value):
        self.headers = {"retry-after": value} if value is not None else {}


def test_retry_after_numeric_seconds():
    assert loopguard.retry_after_seconds(_Resp("12")) == 12.0
    assert loopguard.retry_after_seconds(_Resp("0")) == 0.0


def test_retry_after_absent_returns_none():
    assert loopguard.retry_after_seconds(_Resp(None)) is None


def test_retry_after_too_long_is_refused():
    """A 10-minute wait must surface as an error, not freeze the CLI."""
    assert loopguard.retry_after_seconds(_Resp("600")) is None


def test_retry_after_http_date():
    from email.utils import format_datetime
    import datetime as dt
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=20)
    got = loopguard.retry_after_seconds(_Resp(format_datetime(when)))
    assert got is not None and 15 <= got <= 25


def test_retry_after_garbage_is_ignored():
    assert loopguard.retry_after_seconds(_Resp("soon-ish")) is None


# ---------------------------------------------------------------------------
# max_tokens / context overflow — recoverable without dropping history
# ---------------------------------------------------------------------------

def test_parse_max_tokens_overflow():
    msg = "input length and `max_tokens` exceed context limit: 195000 + 8192 > 200000"
    got = loopguard.parse_max_tokens_overflow(msg)
    assert got == {"input_tokens": 195000, "max_tokens": 8192,
                   "context_limit": 200000}


def test_parse_max_tokens_overflow_without_backticks():
    msg = "input length and max_tokens exceed context limit: 10 + 20 > 25"
    assert loopguard.parse_max_tokens_overflow(msg)["input_tokens"] == 10


def test_parse_max_tokens_overflow_non_match():
    assert loopguard.parse_max_tokens_overflow("prompt is too long") is None
    assert loopguard.parse_max_tokens_overflow("") is None


def test_adjusted_max_tokens_shrinks_to_fit():
    ovf = {"input_tokens": 195000, "max_tokens": 8192, "context_limit": 200000}
    assert loopguard.adjusted_max_tokens(ovf, margin=256) == 4744


def test_adjusted_max_tokens_never_raises_the_ask():
    ovf = {"input_tokens": 1000, "max_tokens": 512, "context_limit": 200000}
    assert loopguard.adjusted_max_tokens(ovf) == 512  # not the huge room


def test_adjusted_max_tokens_none_when_no_room():
    """Under the output floor it's a context problem — compaction, not shrink."""
    ovf = {"input_tokens": 199900, "max_tokens": 8192, "context_limit": 200000}
    assert loopguard.adjusted_max_tokens(ovf) is None
