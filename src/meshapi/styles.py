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
#
# These texts are PRESCRIPTIVE on purpose. The first version described an
# intent ("answer in as few words as the question allows") and every style
# produced the same numbered list with an "Overall..." summary — on a cheap
# model a vague instruction at the end of a ~2.5k-token system prompt is
# indistinguishable from no instruction. Each style now names the concrete
# structures it requires and the ones it forbids, and each forbids something
# the others allow, so the difference is visible in one answer.
STYLES = {
    "default": (
        "Default",
        "Balanced — explains what matters, skips what doesn't",
        "",                      # no extra instruction; the base prompt stands
    ),
    "concise": (
        "Concise",
        "Short and direct — answer first, no lists, no summary",
        "OUTPUT STYLE — CONCISE. Lead with the direct answer in your first "
        "sentence. Keep an explanatory reply under about 80 words. Do NOT "
        "use a numbered or bulleted list unless the user explicitly asked "
        "for a list or for steps — write prose. Do NOT end with a summary "
        "or restatement; closing lines that begin 'Overall', 'In summary', "
        "'In essence' or 'In conclusion' are forbidden. No preamble, no "
        "restating the question, no narrating what you are about to do, no "
        "offers of further help. Stop as soon as the question is answered. "
        "This governs PROSE ONLY — never shorten the actual work: write "
        "complete files, run every step the task needs, and never substitute "
        "a placeholder for real content to save space.",
    ),
    "explanatory": (
        "Explanatory",
        "Digs into mechanism and trade-offs, not just what",
        "OUTPUT STYLE — EXPLANATORY. Go past WHAT something is to HOW and "
        "WHY it works: the mechanism underneath, the reason it is built that "
        "way, and what it is traded off against. Every answer must name at "
        "least one explicit trade-off, limitation or alternative that was "
        "given up — phrase it as a contrast ('X buys you Y at the cost of "
        "Z'). When you are doing work rather than answering a question, "
        "narrate the decisions: why this approach over the alternative you "
        "rejected, and on what evidence. Do not pad with definitions the "
        "user already has, and do not merely list features — a bare feature "
        "list with no causal explanation is exactly what this style exists "
        "to replace.",
    ),
    "learning": (
        "Learning",
        "Teaching mode — analogy, worked example, then one thing to try",
        "OUTPUT STYLE — LEARNING. Teach the idea rather than reciting it. "
        "Name the underlying concept explicitly so it can be looked up, and "
        "ground it in ONE concrete analogy or a small worked example with "
        "real values — not an abstract description. Call out the mistake or "
        "misconception the idea protects against. End EVERY reply with a "
        "single final line that starts with 'Try this:' proposing one small "
        "concrete thing the user can do or check for themselves to test "
        "their understanding. Where a piece of the task would teach more by "
        "being done than read, offer it as an optional exercise — then, "
        "unless the user takes it up, finish the whole task yourself. Never "
        "leave work undone in the name of teaching.",
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
