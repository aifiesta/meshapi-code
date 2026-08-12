"""Tests for meshapi.update.

Pure logic only — no network. `fetch_latest` / `start_background_check` do
the real HTTP; the functions under test here (parse_version, is_newer,
detect_upgrade_command) never touch the wire.
"""
import sys

import pytest

from meshapi import update


# --------------------------------------------------------------------------
# parse_version
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0.5.6", (0, 5, 6)),
        ("0.4.10", (0, 4, 10)),      # multi-digit segment, not lexical
        ("0.5.0rc1", (0, 5, 0)),     # pre-release suffix stripped per-segment
        ("garbage", ()),             # no leading digits anywhere
        ("", ()),                    # empty
    ],
)
def test_parse_version(raw, expected):
    assert update.parse_version(raw) == expected


def test_parse_version_stops_at_first_non_numeric_segment():
    # "1.x.3" -> (1,) : parsing halts at the first digitless segment.
    assert update.parse_version("1.x.3") == (1,)


# --------------------------------------------------------------------------
# is_newer
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "latest, current",
    [
        ("0.5.7", "0.5.6"),   # patch bump
        ("0.6.0", "0.5.9"),   # minor bump beats a higher patch
        ("0.5.10", "0.5.9"),  # numeric, not lexical (10 > 9)
    ],
)
def test_is_newer_true(latest, current):
    assert update.is_newer(latest, current) is True


@pytest.mark.parametrize(
    "latest, current",
    [
        ("0.5.6", "0.5.6"),   # equal is not newer
        ("0.5.5", "0.5.6"),   # older
        ("0.5.9", "0.5.10"),  # older, numeric-aware
    ],
)
def test_is_newer_false_for_equal_or_older(latest, current):
    assert update.is_newer(latest, current) is False


@pytest.mark.parametrize(
    "latest, current",
    [
        ("garbage", "0.5.6"),   # unparseable latest
        ("0.5.7", "garbage"),   # unparseable current
        ("", "0.5.6"),          # empty latest
        ("0.5.7", ""),          # empty current
        ("garbage", "garbage"), # both unparseable
    ],
)
def test_is_newer_false_when_either_side_is_garbage(latest, current):
    # Regression: a parse failure must NEVER produce a bogus upgrade nag.
    assert update.is_newer(latest, current) is False


# --------------------------------------------------------------------------
# detect_upgrade_command  (driven by sys.prefix)
# --------------------------------------------------------------------------

def test_detect_upgrade_command_pipx(monkeypatch):
    monkeypatch.setattr(
        update.sys, "prefix", "/home/u/.local/pipx/venvs/meshapi-code"
    )
    label, argv = update.detect_upgrade_command()
    assert label == "pipx"
    assert argv == ["pipx", "upgrade", update.PACKAGE]


def test_detect_upgrade_command_uv(monkeypatch):
    monkeypatch.setattr(
        update.sys, "prefix", "/home/u/.local/share/uv/tools/meshapi-code"
    )
    label, argv = update.detect_upgrade_command()
    assert label == "uv"
    assert argv == ["uv", "tool", "upgrade", update.PACKAGE]


def test_detect_upgrade_command_pip_fallback(monkeypatch):
    monkeypatch.setattr(update.sys, "prefix", "/usr/local")
    label, argv = update.detect_upgrade_command()
    assert label == "pip"
    # First element is the running interpreter; the rest is a fixed pip call.
    assert argv[0] == sys.executable
    assert argv[1:] == ["-m", "pip", "install", "--upgrade", update.PACKAGE]


def test_detect_upgrade_command_windows_path_normalized(monkeypatch):
    # Backslashes + mixed case must still match the pipx branch (the docstring
    # promise about C:\\Users\\x\\pipx\\venvs\\...).
    monkeypatch.setattr(
        update.sys, "prefix", r"C:\Users\X\pipx\Venvs\meshapi-code"
    )
    label, argv = update.detect_upgrade_command()
    assert label == "pipx"
    assert argv == ["pipx", "upgrade", update.PACKAGE]


# --- _resolve_tool: uv/pipx path resolution (regression for the [Errno 2] bug
#     where a uv-installed meshapi runs with a PATH that omits uv's bin dir) ---

def test_resolve_tool_returns_abspath_when_found(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name, path=None: "/abs/bin/uv")
    assert update._resolve_tool("uv") == "/abs/bin/uv"


def test_resolve_tool_searches_common_bin_dirs(monkeypatch):
    seen = {}

    def fake_which(name, path=None):
        seen["path"] = path or ""
        return None  # simulate not-on-PATH so we can inspect the search path

    monkeypatch.setattr(update.shutil, "which", fake_which)
    # bare name returned when nothing is found (caller then errors clearly)
    assert update._resolve_tool("uv") == "uv"
    # ...but the search path was augmented with the usual uv/pipx locations
    assert "/.local/bin" in seen["path"]
    assert "/.cargo/bin" in seen["path"]
    assert "/opt/homebrew/bin" in seen["path"]


def test_resolve_tool_survives_empty_path(monkeypatch):
    monkeypatch.setenv("PATH", "")
    # must not raise even with no PATH; falls back to the fixed dirs
    assert isinstance(update._resolve_tool("uv"), str)
