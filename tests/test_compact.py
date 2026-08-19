"""Unit tests for meshapi.compact — token estimation and the deterministic
two-phase history compactor that keeps day-long turns under the model's
context limit while upholding the dedupe_read contract.
"""
import copy

from meshapi import compact
from meshapi.compact import (
    compact_history, context_limit, est_history_tokens, est_message_tokens,
    should_compact,
)


def user(text):
    return {"role": "user", "content": text}


def assistant_batch(calls, idx_prefix="c"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"{idx_prefix}{i}", "type": "function",
         "function": {"name": name, "arguments": args}}
        for i, (name, args) in enumerate(calls)]}


def tool(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def make_history(n_hops=3, result_chars=2000):
    """system + user + n_hops × (assistant batch + tool result) + final text."""
    msgs = [{"role": "system", "content": "sys prompt"}, user("do the thing")]
    for h in range(n_hops):
        msgs.append(assistant_batch(
            [("run_bash", '{"command": "make step%d"}' % h)], idx_prefix=f"h{h}_"))
        msgs.append(tool(f"h{h}_0", "out " * (result_chars // 4) + "\n[exit 0]"))
    msgs.append({"role": "assistant", "content": "done"})
    return msgs


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

def test_est_string_content():
    assert est_message_tokens({"role": "user", "content": "x" * 400}) == 104  # 100 + 4


def test_est_tool_calls_counted():
    m = assistant_batch([("write_file", '{"path":"a","content":"' + "x" * 400 + '"}')])
    assert est_message_tokens(m) > 100


def test_est_image_parts_flat_rated():
    m = {"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100_000}},
    ]}
    est = est_message_tokens(m)
    assert compact.IMAGE_PART_TOKENS <= est < compact.IMAGE_PART_TOKENS + 100


def test_context_limit_catalog_hit_and_fallback():
    models = [{"id": "openai/gpt-4o-mini", "context_length": 128000},
              {"id": "x/no-len"}]
    assert context_limit("openai/gpt-4o-mini", models) == 128000
    assert context_limit("x/no-len", models) == compact.DEFAULT_CONTEXT_TOKENS
    assert context_limit("missing/model", models) == compact.DEFAULT_CONTEXT_TOKENS
    assert context_limit("any", None) == compact.DEFAULT_CONTEXT_TOKENS


def test_should_compact_threshold():
    msgs = [user("x" * 4000)]  # ~1004 tokens
    assert should_compact(msgs, limit=1000)         # 1004 > 700
    assert not should_compact(msgs, limit=2000)     # 1004 < 1400


# ---------------------------------------------------------------------------
# Phase 1 — truncation
# ---------------------------------------------------------------------------

def test_phase1_truncates_only_old_large_tool_results():
    msgs = make_history(n_hops=6, result_chars=2000)
    state = {"messages": msgs, "session_reads": {}}
    rep = compact_history(state)
    assert rep["truncated"] > 0 and rep["folded"] == 0
    assert rep["after_tok"] < rep["before_tok"]
    # recent window untouched: the last tool result keeps its full body
    last_tool = [m for m in msgs if m["role"] == "tool"][-1]
    assert "[compacted:" not in last_tool["content"]
    # an old one is cut
    first_tool = [m for m in msgs if m["role"] == "tool"][0]
    assert "[compacted:" in first_tool["content"]
    assert len(first_tool["content"]) < 600


def test_phase1_boundary_800_chars_untouched():
    msgs = [{"role": "system", "content": "s"}, user("go")]
    msgs.append(assistant_batch([("run_bash", '{"command":"x"}')]))
    msgs.append(tool("c0", "x" * 800))
    # pad so the tool result is outside the keep window
    for i in range(compact.KEEP_RECENT_MESSAGES + 1):
        msgs.append(user(f"pad{i}"))
    state = {"messages": copy.deepcopy(msgs), "session_reads": {}}
    assert compact_history(state) == {}          # 800 == 2*400 → untouched
    state2 = {"messages": copy.deepcopy(msgs), "session_reads": {}}
    state2["messages"][3]["content"] = "x" * 801
    rep = compact_history(state2)
    assert rep["truncated"] == 1


def test_phase1_never_touches_assistant_messages():
    big_write = assistant_batch([("write_file", '{"path":"a.js","content":"' + "z" * 5000 + '"}')])
    msgs = [{"role": "system", "content": "s"}, user("go"), big_write, tool("c0", "OK — wrote")]
    for i in range(compact.KEEP_RECENT_MESSAGES + 1):
        msgs.append(user(f"pad{i}"))
    state = {"messages": msgs, "session_reads": {}}
    compact_history(state)
    assert "z" * 5000 in msgs[2]["tool_calls"][0]["function"]["arguments"]


def test_phase1_drops_read_dedupe_entry_keeps_write():
    msgs = make_history(n_hops=6, result_chars=2000)
    # tool result at index 3 is the first hop's; pretend it was a read
    reads = {
        "/tmp/read.txt": {"sha16": "a", "chars": 2000, "lines": 9,
                          "source": "read", "msg_index": 3, "stubbed_last": False},
        "/tmp/wrote.txt": {"sha16": "b", "chars": 2000, "lines": 9,
                           "source": "write", "msg_index": 2, "stubbed_last": False},
    }
    state = {"messages": msgs, "session_reads": reads}
    rep = compact_history(state)
    assert rep["truncated"] >= 1
    assert "/tmp/read.txt" not in reads      # body was cut — entry must die
    assert "/tmp/wrote.txt" in reads         # assistant msg untouched


def test_phase1_idempotent():
    msgs = make_history(n_hops=6, result_chars=2000)
    state = {"messages": msgs, "session_reads": {}}
    rep1 = compact_history(state)
    rep2 = compact_history(state)
    assert rep1["truncated"] > 0
    assert rep2 == {}  # nothing left to do


# ---------------------------------------------------------------------------
# Phase 2 — folding
# ---------------------------------------------------------------------------

def _big_history():
    """History big enough that truncation alone can't get under threshold."""
    msgs = [{"role": "system", "content": "sys"}, user("build it")]
    for h in range(20):
        msgs.append(assistant_batch(
            [("write_file", '{"path":"f%d.js","content":"%s"}' % (h, "w" * 300)),
             ("run_bash", '{"command":"node f%d.js"}' % h)],
            idx_prefix=f"h{h}_"))
        msgs.append(tool(f"h{h}_0", "OK — wrote 300 chars"))
        msgs.append(tool(f"h{h}_1", "ok\n[exit 0]"))
    msgs.append(user("continue"))
    for h in range(20, 24):
        msgs.append(assistant_batch(
            [("run_bash", '{"command":"make step%d"}' % h)], idx_prefix=f"h{h}_"))
        msgs.append(tool(f"h{h}_0", "fine\n[exit 0]"))
    return msgs


def test_phase2_folds_and_history_stays_valid():
    msgs = _big_history()
    state = {"messages": msgs, "session_reads": {}}
    rep = compact_history(state, limit=1000)   # tiny limit forces folding
    assert rep["folded"] >= 1
    # validity: every tool message's id has its assistant parent before it
    for i, m in enumerate(state["messages"]):
        if m.get("role") != "tool":
            continue
        parents = [
            p for p in state["messages"][:i]
            if p.get("role") == "assistant"
            and any(tc["id"] == m["tool_call_id"] for tc in p.get("tool_calls") or [])
        ]
        assert parents, f"orphan tool result at index {i}"
    # every surviving assistant tool_calls id has exactly one result after it
    for i, m in enumerate(state["messages"]):
        for tc in (m.get("tool_calls") or []):
            n = sum(1 for r in state["messages"][i:]
                    if r.get("role") == "tool" and r.get("tool_call_id") == tc["id"])
            assert n == 1, f"tool id {tc['id']} has {n} results"


def test_phase2_keeps_system_and_user_messages():
    msgs = _big_history()
    users_before = [m["content"] for m in msgs if m["role"] == "user"]
    state = {"messages": msgs, "session_reads": {}}
    compact_history(state, limit=1000)
    assert state["messages"][0]["role"] == "system"
    assert [m["content"] for m in state["messages"] if m["role"] == "user"] == users_before


def test_phase2_summary_mentions_actions():
    msgs = _big_history()
    state = {"messages": msgs, "session_reads": {}}
    compact_history(state, limit=1000)
    summaries = [m for m in state["messages"]
                 if m["role"] == "system" and "summarized to save space" in m["content"]]
    assert summaries
    assert "wrote f0.js (300 chars)" in summaries[0]["content"]
    assert "exit 0" in summaries[0]["content"]


def test_phase2_remaps_and_drops_session_reads():
    msgs = _big_history()
    last_idx = len(msgs) - 1                       # inside keep window → survives
    reads = {
        "/tmp/folded.txt": {"sha16": "a", "chars": 900, "lines": 5,
                            "source": "read", "msg_index": 4, "stubbed_last": False},
        "/tmp/recent.txt": {"sha16": "b", "chars": 900, "lines": 5,
                            "source": "read", "msg_index": last_idx, "stubbed_last": False},
    }
    state = {"messages": msgs, "session_reads": reads}
    compact_history(state, limit=1000)
    assert "/tmp/folded.txt" not in reads          # its message was folded away
    assert "/tmp/recent.txt" in reads
    new_idx = reads["/tmp/recent.txt"]["msg_index"]
    assert state["messages"][new_idx]["role"] == "tool"  # still points at its message


def test_phase2_not_run_when_under_threshold_after_phase1():
    msgs = make_history(n_hops=6, result_chars=2000)
    state = {"messages": msgs, "session_reads": {}}
    rep = compact_history(state, limit=10_000_000)  # huge limit — never over
    assert rep["folded"] == 0


def test_deterministic():
    m1 = {"messages": _big_history(), "session_reads": {}}
    m2 = {"messages": copy.deepcopy(m1["messages"]), "session_reads": {}}
    compact_history(m1, limit=1000)
    compact_history(m2, limit=1000)
    assert m1["messages"] == m2["messages"]


def test_phase2_never_bisects_a_straddling_batch():
    """A batch whose assistant sits just outside the keep window and whose
    results sit inside must fold or survive as a unit — never orphan."""
    msgs = [{"role": "system", "content": "sys"}, user("go")]
    for h in range(10):
        msgs.append(assistant_batch(
            [("run_bash", '{"command":"step%d"}' % h)], idx_prefix=f"h{h}_"))
        msgs.append(tool(f"h{h}_0", "out\n[exit 0]"))
    # final batch: assistant + KEEP_RECENT_MESSAGES results → the window
    # boundary lands mid-batch
    n = compact.KEEP_RECENT_MESSAGES
    msgs.append(assistant_batch(
        [("run_bash", '{"command":"final %d"}' % i) for i in range(n)],
        idx_prefix="fin_"))
    for i in range(n):
        msgs.append(tool(f"fin_{i}", "ok\n[exit 0]"))
    state = {"messages": msgs, "session_reads": {}}
    compact_history(state, limit=100)  # force folding hard
    for i, m in enumerate(state["messages"]):
        if m.get("role") != "tool":
            continue
        assert any(
            p.get("role") == "assistant"
            and any(tc["id"] == m["tool_call_id"] for tc in p.get("tool_calls") or [])
            for p in state["messages"][:i]
        ), f"orphan tool result at {i}"


# ---------------------------------------------------------------------------
# Transcript-backed compaction — lossy but recoverable
# ---------------------------------------------------------------------------

def test_fold_summary_points_at_transcript(tmp_path, monkeypatch):
    """The summary must tell the model where the exact text still lives —
    otherwise compaction is a dead end and it redoes or invents work."""
    written = {}

    def fake_append(session_id, msgs):
        written["n"] = written.get("n", 0) + len(msgs)
        return str(tmp_path / f"{session_id}.jsonl")

    monkeypatch.setattr("meshapi.config.append_transcript", fake_append)
    monkeypatch.setattr("meshapi.config.transcript_path",
                        lambda sid: tmp_path / f"{sid}.jsonl")
    state = {"messages": _big_history(), "session_reads": {},
             "session_id": "sess1"}
    compact_history(state, limit=1000)
    summaries = [m for m in state["messages"]
                 if m["role"] == "system" and "summarized to save space" in m["content"]]
    assert summaries
    assert "read the full transcript at" in summaries[0]["content"]
    assert "sess1.jsonl" in summaries[0]["content"]
    assert written["n"] > 0            # history really was persisted first


def test_no_session_id_still_compacts_without_pointer():
    state = {"messages": _big_history(), "session_reads": {}}
    rep = compact_history(state, limit=1000)
    assert rep["folded"] >= 1
    summaries = [m for m in state["messages"]
                 if m["role"] == "system" and "summarized to save space" in m["content"]]
    assert "read the full transcript" not in summaries[0]["content"]


def test_transcript_persists_only_new_messages(tmp_path, monkeypatch):
    """Second compaction must not re-append what's already on disk."""
    calls = []
    monkeypatch.setattr("meshapi.config.append_transcript",
                        lambda sid, msgs: calls.append(len(msgs)) or str(tmp_path / "t.jsonl"))
    monkeypatch.setattr("meshapi.config.transcript_path", lambda sid: tmp_path / "t.jsonl")
    state = {"messages": _big_history(), "session_reads": {}, "session_id": "s"}
    compact_history(state, limit=1000)
    first = calls[0]
    state["messages"] += _big_history()[1:]      # more work happens
    compact_history(state, limit=1000)
    assert len(calls) == 2
    assert calls[1] < first + 5   # only the delta, not the whole history again
