"""Slash-command abbreviation: any unique prefix runs the command; ambiguous
prefixes list candidates; exact names always win over longer commands they
prefix (/mode never becomes /model).
"""
import pytest

from meshapi import commands
from meshapi.commands import resolve_command


@pytest.fixture(autouse=True)
def no_disk_writes(monkeypatch):
    monkeypatch.setattr(commands, "save_config", lambda cfg: None)


def make_state():
    return {"cfg": {"model": "openai/gpt-4o-mini", "auto_route": False,
                    "route_mode": "off", "route_effort": "auto",
                    "route_weights": {"cost": 0.5, "cap": 0.3, "speed": 0.2},
                    "max_hops": 0, "stall_policy": "pause", "system": "x",
                    "reasoning_effort": None, "optimize": 0.0,
                    "auto_compact": True},
            "messages": [{"role": "system", "content": "s"}],
            "session_cost": 0.0, "session_reads": {}}


@pytest.mark.parametrize("abbrev, full", [
    ("/eff", "/effort"),
    ("/ef", "/effort"),
    ("/comp", "/compact"),
    ("/rou", "/route"),
    ("/sta", "/stall"),
    ("/hop", "/hops"),
    ("/hel", "/help"),
])
def test_unique_prefix_resolves(abbrev, full):
    assert resolve_command(abbrev)[0] == full


def test_exact_match_beats_prefix_of_longer():
    assert resolve_command("/mode")[0] == "/mode"      # not /model or /models


def test_ambiguous_prefix_lists_candidates():
    resolved, matches = resolve_command("/mod")
    assert resolved is None
    assert set(matches) == {"/mode", "/model", "/models"}


def test_unknown_prefix():
    assert resolve_command("/zzz") == (None, [])


def test_abbreviated_effort_sets_config(capsys):
    state = make_state()
    assert commands.handle_command("/eff xhigh", state) is True
    assert state["cfg"]["route_effort"] == "xhigh"
    out = capsys.readouterr().out
    assert "→ /effort" in out            # resolution is visible
    assert "Effort xhigh" in out


def test_effort_toplevel_equals_route_effort(capsys):
    a, b = make_state(), make_state()
    commands.handle_command("/effort max", a)
    commands.handle_command("/route effort max", b)
    assert a["cfg"]["route_effort"] == b["cfg"]["route_effort"] == "max"


def test_ambiguous_command_is_safe(capsys):
    state = make_state()
    assert commands.handle_command("/mod", state) is True
    out = capsys.readouterr().out
    assert "ambiguous" in out and "/model" in out and "/mode" in out
    assert state["cfg"]["model"] == "openai/gpt-4o-mini"   # nothing changed


def test_unknown_command_still_reports(capsys):
    state = make_state()
    assert commands.handle_command("/nope", state) is True
    assert "Unknown command" in capsys.readouterr().out


def test_abbreviation_is_live_control():
    import meshapi.cli as cli
    assert cli.is_live_control("/eff max")
    assert cli.is_live_control("/effort low")
    assert not cli.is_live_control("/mod")       # ambiguous -> not steerable
