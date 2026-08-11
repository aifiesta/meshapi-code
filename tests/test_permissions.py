"""Tests for meshapi.permissions — the shift+tab mode cycle and the
auto-approve escalation ladder.

Two invariants matter:

* next_mode cycles DEFAULT -> ACCEPT_EDITS -> AUTO -> BYPASS -> DEFAULT.
* AUTO_APPROVE grows monotonically along ORDER: each mode auto-approves a
  superset of the previous mode's tools. A regression that, say, dropped
  write_file from AUTO would make a stricter mode approve something a looser
  one doesn't — exactly what this catches.
"""
from meshapi import permissions as perm
from meshapi.permissions import Mode


def test_next_mode_cycle_order():
    assert perm.next_mode(Mode.DEFAULT) == Mode.ACCEPT_EDITS
    assert perm.next_mode(Mode.ACCEPT_EDITS) == Mode.AUTO
    assert perm.next_mode(Mode.AUTO) == Mode.BYPASS
    assert perm.next_mode(Mode.BYPASS) == Mode.DEFAULT


def test_next_mode_is_a_full_cycle():
    seen = []
    m = Mode.DEFAULT
    for _ in range(len(perm.ORDER)):
        seen.append(m)
        m = perm.next_mode(m)
    # Visited every mode exactly once and wrapped back to the start.
    assert seen == perm.ORDER
    assert m == Mode.DEFAULT


def test_order_is_the_canonical_escalation():
    assert perm.ORDER == [Mode.DEFAULT, Mode.ACCEPT_EDITS, Mode.AUTO, Mode.BYPASS]


def test_auto_approve_grows_monotonically():
    for prev, nxt in zip(perm.ORDER, perm.ORDER[1:]):
        prev_set = perm.AUTO_APPROVE[prev]
        next_set = perm.AUTO_APPROVE[nxt]
        assert prev_set <= next_set, (
            f"{nxt.value} must auto-approve a superset of {prev.value}: "
            f"lost {prev_set - next_set}"
        )


def test_auto_approve_exact_membership():
    # Pin the concrete escalation so a silent widening/narrowing is caught.
    assert perm.AUTO_APPROVE[Mode.DEFAULT] == set()
    assert perm.AUTO_APPROVE[Mode.ACCEPT_EDITS] == {"write_file"}
    assert perm.AUTO_APPROVE[Mode.AUTO] == {"write_file", "run_bash", "web_search"}
    assert perm.AUTO_APPROVE[Mode.BYPASS] == {
        "write_file", "run_bash", "read_file", "start_server", "web_search",
    }


def test_default_auto_approves_nothing():
    assert perm.AUTO_APPROVE[Mode.DEFAULT] == set()


def test_bypass_is_the_widest():
    widest = perm.AUTO_APPROVE[Mode.BYPASS]
    for m in perm.ORDER:
        assert perm.AUTO_APPROVE[m] <= widest


def test_every_mode_has_label_color_and_approve_set():
    for m in Mode:
        assert m in perm.LABELS
        assert m in perm.RICH_COLOR
        assert m in perm.AUTO_APPROVE


def test_from_str_aliases_round_trip():
    assert perm.from_str("default") == Mode.DEFAULT
    assert perm.from_str("yolo") == Mode.BYPASS
    assert perm.from_str("edits") == Mode.ACCEPT_EDITS
    assert perm.from_str("auto") == Mode.AUTO
    # Canonical enum values also parse.
    for m in Mode:
        assert perm.from_str(m.value) == m


def test_from_str_rejects_unknown():
    import pytest
    with pytest.raises(ValueError):
        perm.from_str("nonsense-mode")
