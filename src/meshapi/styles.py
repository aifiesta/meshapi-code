"""Output styles — how the assistant writes, not what it can do.

A style only changes the prose contract (length, whether reasoning is shown,
whether teaching asides appear). It never touches tool access, permissions or
routing, so switching styles can't change what a turn is *able* to do.

Kept as a separate pure module so the prompt text is unit-testable without
booting the REPL, and so `tools.build_system_prompt` stays a single append.

DELIBERATELY TOOL-NAME-FREE: naming tools in prose tips Anthropic models into
XML tool-use mode and they emit `<function_calls>` as text (see
build_system_prompt's docstring — the same trap applies to anything appended
to it).
"""

DEFAULT = "default"

# value -> (label, one-line description for the picker, system-prompt text)
STYLES = {
    "default": (
        "Default",
        "Balanced — explains what matters, skips what doesn't",
        "",                      # no extra instruction; the base prompt stands
    ),
    "concise": (
        "Concise",
        "Short and direct — minimal prose, no preamble",
        "OUTPUT STYLE — CONCISE. Answer in as few words as the question "
        "honestly allows. No preamble, no restating the request, no summary "
        "of what you just did unless the user asked for one. Prefer a short "
        "sentence or a tight list over a paragraph. Skip pleasantries and "
        "self-narration entirely. Never pad a short answer to look thorough. "
        "Brevity applies to PROSE ONLY — never shorten actual work: write "
        "complete files, run every step the task needs, and never replace "
        "real content with a placeholder to save space.",
    ),
    "explanatory": (
        "Explanatory",
        "Narrates the reasoning and trade-offs behind each decision",
        "OUTPUT STYLE — EXPLANATORY. As you work, explain the reasoning "
        "behind your decisions: why this approach over the alternatives, "
        "what trade-off you accepted, what you ruled out and on what "
        "evidence. When you touch an unfamiliar part of the codebase, say "
        "briefly what it does and how it fits. Keep explanations tied to the "
        "concrete work in front of you — this is commentary on real "
        "decisions, not a general tutorial, and it never replaces doing the "
        "work.",
    ),
    "learning": (
        "Learning",
        "Teaching mode — explains concepts and invites you to try things",
        "OUTPUT STYLE — LEARNING. Treat the user as someone who wants to "
        "understand the material, not just receive a result. Explain the "
        "underlying concept when one is doing real work in your solution, "
        "name the pattern or technique you used so it can be looked up, and "
        "point out the mistake that the approach avoids. Where a small piece "
        "of the task would teach more by being done than read, offer it to "
        "the user as an optional exercise — then, unless they take it up, "
        "finish the whole task yourself. Never leave work undone in the name "
        "of teaching.",
    ),
}

ORDER = ("concise", "default", "explanatory", "learning")

# Forgiving spellings so a typed value behaves like the picker.
_ALIASES = {
    "brief": "concise", "short": "concise", "terse": "concise",
    "minimal": "concise", "compact": "concise",
    "normal": "default", "balanced": "default", "standard": "default",
    "verbose": "explanatory", "detailed": "explanatory",
    "explain": "explanatory", "explanation": "explanatory",
    "teaching": "learning", "teach": "learning", "tutor": "learning",
    "learn": "learning",
}


def normalize(value) -> "str | None":
    """Canonical style name, or None if unrecognized. Never raises."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("_", "-")
    if v in STYLES:
        return v
    return _ALIASES.get(v)


def label(value: str) -> str:
    """Human label for a style value (falls back to the raw value)."""
    row = STYLES.get(normalize(value) or "")
    return row[0] if row else str(value)


def describe(value: str) -> str:
    row = STYLES.get(normalize(value) or "")
    return row[1] if row else ""


def block(value) -> str:
    """System-prompt text for a style. '' for default/unknown."""
    row = STYLES.get(normalize(value) or "")
    return row[2] if row else ""


def options() -> list:
    """[(value, description)] in picker order, for askui.slider."""
    return [(v, STYLES[v][1]) for v in ORDER]
