"""Unit tests for meshapi.render — the USD formatter and the tool ticker.

fmt_usd is the shared money formatter (ports dashboard fmtUsd): always 6
decimals, K/M abbreviations, graceful on garbage. run_with_ticker wraps a
blocking call in a transient rich Live spinner and must return the call's
value, propagate exceptions (including KeyboardInterrupt), and always reset
state["live_active"].

No network; rich Live runs fine headless.
"""
import pytest

from meshapi.render import fmt_usd, run_with_ticker


# ---------------------------------------------------------------------------
# fmt_usd
# ---------------------------------------------------------------------------

def test_fmt_usd_zero_is_six_decimals():
    assert fmt_usd(0) == "$0.000000"
    assert fmt_usd(0.0) == "$0.000000"


def test_fmt_usd_small_values_keep_six_decimals():
    assert fmt_usd(0.001234) == "$0.001234"
    assert fmt_usd(1.5) == "$1.500000"
    # Precision that plain 2-decimal rounding would corrupt.
    assert fmt_usd(0.0000009) == "$0.000001"


def test_fmt_usd_thousands_abbreviation():
    assert fmt_usd(1000) == "$1.00K"
    assert fmt_usd(1500) == "$1.50K"
    assert fmt_usd(12_340) == "$12.34K"


def test_fmt_usd_millions_abbreviation():
    assert fmt_usd(1_000_000) == "$1.00M"
    assert fmt_usd(2_500_000) == "$2.50M"


def test_fmt_usd_accepts_numeric_string():
    # The cost SSE field arrives as a string USD amount.
    assert fmt_usd("1.5") == "$1.500000"
    assert fmt_usd("1500") == "$1.50K"


@pytest.mark.parametrize("garbage", [None, "abc", "", "not-a-number", [], {}])
def test_fmt_usd_garbage_degrades_gracefully(garbage):
    assert fmt_usd(garbage) == "$0.000000"


def test_fmt_usd_always_starts_with_dollar():
    for v in (0, 0.5, 999.999833, 5000, 3_000_000, "7.25"):
        assert fmt_usd(v).startswith("$")


# ---------------------------------------------------------------------------
# run_with_ticker
# ---------------------------------------------------------------------------

def test_run_with_ticker_returns_value():
    assert run_with_ticker("working", lambda: 42) == 42
    assert run_with_ticker("working", lambda: "hello", state=None) == "hello"


def test_run_with_ticker_sets_then_resets_live_active():
    state = {"live_active": False}
    observed = {}

    def fn():
        observed["during"] = state["live_active"]
        return "ok"

    result = run_with_ticker("label", fn, state=state)
    assert result == "ok"
    # live_active is raised while the Live owns the screen...
    assert observed["during"] is True
    # ...and always lowered on the way out.
    assert state["live_active"] is False


def test_run_with_ticker_propagates_exception_and_resets():
    state = {"live_active": False}

    def boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        run_with_ticker("label", boom, state=state)
    assert state["live_active"] is False


def test_run_with_ticker_propagates_keyboard_interrupt_and_resets():
    state = {"live_active": False}

    def interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_with_ticker("label", interrupt, state=state)
    assert state["live_active"] is False


def test_run_with_ticker_exception_with_none_state():
    # state=None must not crash on either the set or the reset path.
    def boom():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError, match="x"):
        run_with_ticker("label", boom, state=None)


def test_run_with_ticker_keyboard_interrupt_with_none_state():
    def interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_with_ticker("label", interrupt, state=None)
