"""Permission modes for tool calls — cycle with Shift+Tab."""
from enum import Enum


class Mode(Enum):
    ASK = "ask"        # prompt for each tool call (default — safest)
    BYPASS = "bypass"  # auto-execute without asking (fast — like `--yolo`)
    NONE = "none"      # don't expose tools to the model at all (read-only chat)


ORDER = [Mode.ASK, Mode.BYPASS, Mode.NONE]

LABELS = {
    Mode.ASK: "approve each",
    Mode.BYPASS: "bypass perms",
    Mode.NONE: "no access",
}

HINTS = {
    Mode.ASK: "model can request file/shell ops; you confirm each one",
    Mode.BYPASS: "model executes file/shell ops automatically — be careful",
    Mode.NONE: "chat only — model has no filesystem or shell access",
}


def next_mode(m: Mode) -> Mode:
    return ORDER[(ORDER.index(m) + 1) % len(ORDER)]


def from_str(s: str) -> Mode:
    s = s.strip().lower()
    for m in Mode:
        if m.value == s:
            return m
    raise ValueError(f"unknown mode: {s} (try {', '.join(m.value for m in Mode)})")
