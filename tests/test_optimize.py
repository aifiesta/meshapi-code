"""Tests for meshapi.optimize — the Mesh Optimize (BETA) lever stack.

Covers the load-bearing invariants:

* dial 0 / 0.0 / None is a byte-identical passthrough (no cache_control, no
  max_tokens, the SAME list object is returned).
* survives_pruning() agrees with what prepare() actually does to a
  read-sourced (role=="tool") message — this is the read-dedupe contract that
  memory.dedupe_read depends on (CLAUDE.md invariant).
* max_tokens injection matches _MAX_TOKENS_DEFAULTS for the classification,
  with the concrete values pinned so a future lowering is caught.
* prepare never empties an assistant message or orphans a tool result.
"""
import pytest

from meshapi import optimize as opt

MODEL = "anthropic/claude-sonnet-4.5"


# ---------------------------------------------------------------------------
# dial 0 / 0.0 / None passthrough


@pytest.mark.parametrize("dial", [0, 0.0, None])
def test_dial_zero_is_identity_passthrough(dial):
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    out, extra, plan = opt.prepare(messages, MODEL, dial, has_tools=False)

    # The SAME object is returned, unmutated — no deepcopy on the off path.
    assert out is messages
    # No max_tokens injected, no cache_control marker added anywhere.
    assert extra == {}
    assert all("cache_control" not in m for m in out)
    assert plan["levers_applied"] == []
    assert plan["tokens_pruned_est"] == 0


def test_dial_none_does_not_crash_on_comparison():
    # `not dial` must short-circuit before `dial <= 0` (None <= 0 is a
    # TypeError). This guards that ordering.
    out, extra, plan = opt.prepare([{"role": "user", "content": "x"}], MODEL,
                                   None, has_tools=True)
    assert extra == {}
    assert isinstance(plan, dict)


def test_prepare_returns_three_tuple():
    result = opt.prepare([{"role": "user", "content": "x"}], MODEL, 0.3, False)
    assert isinstance(result, tuple) and len(result) == 3
    messages, extra, plan = result
    assert isinstance(messages, list)
    assert isinstance(extra, dict)
    assert isinstance(plan, dict)


# ---------------------------------------------------------------------------
# DRIFT-GUARD: survives_pruning() must agree with prepare()'s real output.
#
# We place the target tool message OUTSIDE the recency window
# (_KEEP_RECENT_MESSAGES) so the window provides no accidental safety — which
# is precisely the regime survives_pruning() reasons about (see its docstring:
# a fresh hop appends messages, pushing old reads out of the window).


def _tool_result_survives_prepare(chars: int, dial: float) -> bool:
    """True iff prepare() leaves a read-sourced tool message of `chars`
    bytes byte-for-byte unchanged at this dial."""
    body = "A" * chars
    target = {"role": "tool", "tool_call_id": "t", "content": body}
    # target at index 1; four trailing messages push it past the recency
    # window (len 6, cutoff = 6 - 4 = 2, so index 1 is eligible for pruning).
    messages = [{"role": "user", "content": "start"}, target] + [
        {"role": "user", "content": "pad"} for _ in range(4)
    ]
    out, _extra, _plan = opt.prepare(messages, MODEL, dial, has_tools=True)
    return out[1]["content"] == body


@pytest.mark.parametrize("dial", [0, 0.2, 0.95])
@pytest.mark.parametrize("chars", [300, 799, 800, 801, 5000])
def test_survives_pruning_matches_prepare(dial, chars):
    predicted = opt.survives_pruning(chars, dial)
    actual = _tool_result_survives_prepare(chars, dial)
    assert predicted == actual, (
        f"survives_pruning({chars}, {dial})={predicted} but prepare() "
        f"{'kept' if actual else 'pruned'} it"
    )


def test_survives_pruning_boundary_is_two_truncate_chars():
    # Pin the boundary math so a change to _TRUNCATE_TO_CHARS forces a
    # revisit of this helper (the CLAUDE.md invariant).
    boundary = 2 * opt._TRUNCATE_TO_CHARS
    assert opt.survives_pruning(boundary, 0.2) is True
    assert opt.survives_pruning(boundary + 1, 0.2) is False
    # Below the pruning dial, everything survives regardless of size.
    assert opt.survives_pruning(10 ** 6, 0.19) is True


# ---------------------------------------------------------------------------
# max_tokens injection


def test_max_tokens_default_values_are_pinned():
    # A future lowering of these ceilings (the old 1024 clipped long replies)
    # must trip this test.
    assert opt._MAX_TOKENS_DEFAULTS == {
        "routine": 4096,
        "standard": 4096,
        "complex": 8192,
        "agentic": 8192,
    }


@pytest.mark.parametrize(
    "messages, has_tools, expected_class, expected_max",
    [
        ([{"role": "user", "content": "hi"}], False, "routine", 4096),
        (
            [{"role": "user", "content": "please tell me about the weather"}] * 5,
            False,
            "standard",
            4096,
        ),
        ([{"role": "user", "content": "please refactor this module"}], False,
         "complex", 8192),
        (
            [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "tool", "tool_call_id": "c", "content": "d"},
            ],
            True,
            "complex",
            8192,
        ),
        ([{"role": "user", "content": "x"}] * 7, True, "agentic", 8192),
    ],
)
def test_max_tokens_matches_classification(messages, has_tools, expected_class,
                                           expected_max):
    _out, extra, plan = opt.prepare(messages, MODEL, 0.3, has_tools)
    assert plan["classification"] == expected_class
    assert extra["max_tokens"] == expected_max
    # And it is sourced from the constant table, not a hardcoded literal.
    assert extra["max_tokens"] == opt._MAX_TOKENS_DEFAULTS[plan["classification"]]
    assert "max_tokens_default" in plan["levers_applied"]


# ---------------------------------------------------------------------------
# prepare never empties an assistant message or orphans a tool result


def _agentic_history():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do X"},
        {
            "role": "assistant",
            "content": "reading the file now",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "A" * 5000},
        {"role": "assistant", "content": "done reading"},
        {"role": "user", "content": "continue"},
        {
            "role": "assistant",
            "content": "reading another file",
            "tool_calls": [
                {"id": "call_2", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "B" * 5000},
        {"role": "assistant", "content": "final answer"},
    ]


def test_prepare_preserves_structure_and_never_orphans():
    messages = _agentic_history()
    original_assistant = [m["content"] for m in messages
                          if m["role"] == "assistant"]

    out, _extra, plan = opt.prepare(messages, MODEL, 0.95, has_tools=True)

    # No message dropped -> no orphaned tool result, no missing assistant.
    assert len(out) == len(messages)

    # Every tool result still non-empty (pruning truncates to a stub, never
    # blanks it) and every tool_call_id still has a matching assistant call.
    assistant_call_ids = {
        tc["id"]
        for m in out if m["role"] == "assistant"
        for tc in m.get("tool_calls", [])
    }
    for m in out:
        if m["role"] == "tool":
            assert m["content"], "tool result was blanked"
            assert m["tool_call_id"] in assistant_call_ids, "orphaned tool result"

    # Assistant messages are never touched.
    assert [m["content"] for m in out if m["role"] == "assistant"] == original_assistant

    # The OLD tool result (index 3, outside the recency window) was pruned;
    # the RECENT one (index 7, inside the window) was left whole.
    assert out[3]["content"].startswith("A" * opt._TRUNCATE_TO_CHARS)
    assert len(out[3]["content"]) < 5000
    assert out[7]["content"] == "B" * 5000
    assert plan["tokens_pruned_est"] > 0


def test_input_list_never_mutated():
    messages = _agentic_history()
    before = [dict(m) for m in messages]
    opt.prepare(messages, MODEL, 0.95, has_tools=True)
    assert messages == before
