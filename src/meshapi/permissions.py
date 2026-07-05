"""Permission modes for tool calls — cycle with Shift+Tab.

Four modes, escalating from "ask for everything" → "auto-approve everything":

    default       ask for each tool call (safest — no indicator displayed)
    accept-edits  auto-approve write_file; still ask for shell / start_server
    auto          auto-approve write_file + run_bash + web_search
    bypass        auto-approve everything (use with care)

web_search rides with run_bash (AUTO+): its only risk is leaking the query
string off-machine, and run_bash can already `curl` anything anywhere — a
strictly more powerful channel. DEFAULT/ACCEPT_EDITS confirm every search
and show the query verbatim.

AUTO_APPROVE drives the dispatch in handle_tool_calls — it's the set of tool
names that don't go through the y/n confirmation in a given mode. Plan tools
(create_plan, update_step) are always auto-approved regardless; they don't
touch the filesystem.
"""
from enum import Enum


class Mode(Enum):
    DEFAULT = "default"            # ask for every tool call
    ACCEPT_EDITS = "accept-edits"  # auto-approve write_file
    AUTO = "auto"                  # auto-approve write_file + run_bash
    BYPASS = "bypass"              # auto-approve everything


ORDER = [Mode.DEFAULT, Mode.ACCEPT_EDITS, Mode.AUTO, Mode.BYPASS]

# Display labels. DEFAULT is intentionally blank — no indicator shown.
LABELS = {
    Mode.DEFAULT: "",
    Mode.ACCEPT_EDITS: "accept edits on",
    Mode.AUTO: "auto mode on",
    Mode.BYPASS: "bypass permissions on",
}

# Tools that bypass the y/n confirmation in each mode. The dispatch in
# handle_tool_calls checks `tc.name in AUTO_APPROVE[mode]`.
AUTO_APPROVE: dict = {
    Mode.DEFAULT:      set(),
    Mode.ACCEPT_EDITS: {"write_file"},
    Mode.AUTO:         {"write_file", "run_bash", "web_search"},
    Mode.BYPASS:       {"write_file", "run_bash", "read_file", "start_server",
                        "web_search"},
}

# Modes that warrant a more aggressive footer hint.
SHOW_ESC_HINT = {Mode.BYPASS}


def next_mode(m: Mode) -> Mode:
    return ORDER[(ORDER.index(m) + 1) % len(ORDER)]


# Aliases the user can pass on the command line or to /mode.
_ALIASES = {
    "default": Mode.DEFAULT, "ask": Mode.DEFAULT, "blank": Mode.DEFAULT,
    "accept-edits": Mode.ACCEPT_EDITS, "accept_edits": Mode.ACCEPT_EDITS,
    "edits": Mode.ACCEPT_EDITS, "accept": Mode.ACCEPT_EDITS,
    "auto": Mode.AUTO,
    "bypass": Mode.BYPASS, "yolo": Mode.BYPASS,
}


def from_str(s: str) -> Mode:
    s = (s or "").strip().lower()
    if s in _ALIASES:
        return _ALIASES[s]
    for m in Mode:
        if m.value == s:
            return m
    valid = ", ".join(m.value for m in Mode)
    raise ValueError(f"unknown mode: {s!r} (try {valid})")
