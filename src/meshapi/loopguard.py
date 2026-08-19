"""Loop-control logic for the agentic turn loop — pure, stdlib-only.

The turn loop used to end at an arbitrary hop cap (8 without a plan, 60
with one) with "model wasn't converging" — with no actual convergence
check behind the message. This module replaces that blind wall with the
real checks, kept free of I/O and rich so every piece is unit-testable:

- StallDetector: notices when the model repeats the same tool batch
  (period-1 AAA and period-2 ABAB cycles) and escalates nudge → renudge →
  stop. Also stops after a run of hops where EVERY executable call was
  doomed (malformed args) — with the cap gone this rebuilds the wall a
  truly broken model must eventually hit.
- Retry policy helpers for the per-hop stream call (which statuses are
  transient, exponential backoff with jitter, in-band error triage).
- History repair for interrupted turns: seal a trailing half-answered
  tool batch with stub results (strict Anthropic-translating gateways 400
  on a tool_use id with no tool_result — and popping the assistant message
  instead would erase actions whose side effects already happened).
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from collections import deque

# Stall thresholds, counted in CYCLES of the repeating pattern (so an ABAB
# loop nudges after 6 hops, a AAA loop after 3).
STALL_NUDGE_CYCLES = 3
STALL_RENUDGE_CYCLES = 6
STALL_STOP_CYCLES = 9
# Consecutive hops where every executable call was doomed (malformed args)
# before the loop stops. Mirrors the old MAX_HOPS_NO_PLAN=8 wall.
DOOM_STOP_HOPS = 8

# Per-hop stream retry policy.
MAX_STREAM_ATTEMPTS = 5
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
# Longest server-requested wait we'll actually sit through. Claude Code has
# the same guard (`api_request_retry_after_too_long`): a Retry-After of ten
# minutes should surface as an error, not silently freeze the CLI.
RETRY_AFTER_MAX = 90.0
# Below this many tokens of room for output, shrinking max_tokens is pointless
# — the request needs less INPUT, i.e. compaction (Claude Code's
# FLOOR_OUTPUT_TOKENS).
FLOOR_OUTPUT_TOKENS = 1024

# In-band error triage tables (lowercased substring match). "context" is
# recoverable by compacting history; "transient" by waiting and retrying;
# anything else is treated as fatal for the hop (validation errors etc.).
_CONTEXT_MARKERS = (
    "context", "too long", "token limit", "maximum context",
    "prompt is too long", "input length", "input is too long",
)
_TRANSIENT_MARKERS = (
    "rate limit", "rate_limit", "429", "overloaded", "temporar", "timeout",
    "timed out", "capacity", "502", "503", "529", "unavailable", "try again",
)


def batch_signature(tool_calls: list) -> str:
    """Stable signature of a tool batch: sha256 over sorted (name, args).

    Arguments are canonicalized through json.loads/dumps(sort_keys=True)
    when they parse, so key order and whitespace don't defeat the match;
    unparseable args fall back to the stripped raw string. Call ids are
    excluded — they differ on every hop by construction.
    """
    parts = []
    for tc in tool_calls or []:
        # Accept both shapes: the accumulator's flat {name, arguments} (what
        # handle_tool_calls receives) and the OpenAI nested {"function": …}
        # (what history messages carry).
        fn = tc.get("function") or tc
        name = fn.get("name") or ""
        raw = fn.get("arguments") or ""
        try:
            canon = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
        except (ValueError, TypeError):
            canon = str(raw).strip()
        parts.append(f"{name}\x00{canon}")
    parts.sort()
    return hashlib.sha256("\x1e".join(parts).encode("utf-8", "replace")).hexdigest()


class StallDetector:
    """Detect trailing period-1 (AAA…) and period-2 (ABAB…) hop cycles.

    observe() is called once per completed hop with that hop's batch
    signature. Returns None (fine), "nudge", "renudge", or "stop".
    Deliberately nothing beyond period 2: a false positive interrupts
    legitimate work, which is worse than missing an exotic cycle — a
    legit "edit → test → edit → test" loop always interleaves different
    arguments, so its signatures differ.
    """

    def __init__(self, nudge: int = STALL_NUDGE_CYCLES,
                 renudge: int = STALL_RENUDGE_CYCLES,
                 stop: int = STALL_STOP_CYCLES,
                 doom_stop: int = DOOM_STOP_HOPS):
        self.nudge, self.renudge, self.stop = nudge, renudge, stop
        self.doom_stop = doom_stop
        self._sigs: deque = deque(maxlen=20)
        self._doom_run = 0
        self.last_cycles = 0        # trailing-cycle count at the last observe()
        self.stop_reason = None     # "repeat" | "doom" once a stop fires

    def _trailing_cycles(self, period: int) -> int:
        """How many full cycles of `period` repeat at the tail of history."""
        sigs = list(self._sigs)
        if len(sigs) < 2 * period:
            return 0
        pattern = sigs[-period:]
        if period == 2 and pattern[0] == pattern[1]:
            return 0  # that's a period-1 cycle; count it there
        cycles = 1
        i = len(sigs) - 2 * period
        while i >= 0 and sigs[i:i + period] == pattern:
            cycles += 1
            i -= period
        return cycles

    def observe(self, sig: str, all_doomed: bool = False) -> "str | None":
        if all_doomed:
            self._doom_run += 1
            if self._doom_run >= self.doom_stop:
                self.stop_reason = "doom"
                return "stop"
        else:
            self._doom_run = 0
        self._sigs.append(sig)
        cycles = max(self._trailing_cycles(1), self._trailing_cycles(2))
        self.last_cycles = cycles
        if cycles >= self.stop:
            self.stop_reason = "repeat"
            return "stop"
        if cycles == self.renudge:
            return "renudge"
        if cycles == self.nudge:
            return "nudge"
        return None


def classify_inband_error(msg: str) -> str:
    """Triage an in-band gateway error: "context" | "transient" | "fatal"."""
    m = str(msg or "").lower()
    if any(k in m for k in _CONTEXT_MARKERS):
        return "context"
    if any(k in m for k in _TRANSIENT_MARKERS):
        return "transient"
    return "fatal"


def is_retryable_status(code: int) -> bool:
    return code in RETRYABLE_STATUS


def retry_after_seconds(response) -> "float | None":
    """Seconds to wait per the response's Retry-After header, if usable.

    The server knows its own capacity better than our exponential guess, so
    an explicit Retry-After wins — but only up to RETRY_AFTER_MAX; beyond
    that the caller should fail loudly instead of freezing. Accepts both
    forms in the RFC: delta-seconds, and an HTTP-date.
    """
    try:
        raw = response.headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        secs = float(raw)
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            import datetime as _dt
            when = parsedate_to_datetime(raw)
            if when is None:
                return None
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.timezone.utc)
            secs = (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        except Exception:
            return None
    if secs <= 0:
        return 0.0
    return secs if secs <= RETRY_AFTER_MAX else None


# The gateway/Anthropic 400 that names the arithmetic: input + max_tokens is
# over the model's window. Same shape Claude Code parses.
_MAX_TOKENS_OVERFLOW_RE = re.compile(
    r"input length and `?max_tokens`? exceed context limit:\s*"
    r"(\d+)\s*\+\s*(\d+)\s*>\s*(\d+)", re.I
)


def parse_max_tokens_overflow(msg: str) -> "dict | None":
    """Parse "input length and `max_tokens` exceed context limit: N + M > L".

    Returns {"input_tokens", "max_tokens", "context_limit"} or None. This
    error is recoverable WITHOUT dropping history: ask for fewer output
    tokens. Only when the remaining room is under FLOOR_OUTPUT_TOKENS does
    it become a real context problem needing compaction.
    """
    m = _MAX_TOKENS_OVERFLOW_RE.search(str(msg or ""))
    if not m:
        return None
    try:
        return {
            "input_tokens": int(m.group(1)),
            "max_tokens": int(m.group(2)),
            "context_limit": int(m.group(3)),
        }
    except (TypeError, ValueError):
        return None


def adjusted_max_tokens(overflow: dict, margin: int = 256) -> "int | None":
    """New max_tokens that fits, or None if compaction is the only way out."""
    room = overflow["context_limit"] - overflow["input_tokens"] - margin
    if room < FLOOR_OUTPUT_TOKENS:
        return None
    # Never raise the ask — this is a shrink-only adjustment.
    return min(room, overflow["max_tokens"])


def backoff_delay(attempt: int, base: float = BACKOFF_BASE,
                  cap: float = BACKOFF_CAP, rng=random.random) -> float:
    """Exponential backoff with jitter. attempt is 1-based; rng injectable."""
    raw = min(cap, base * (2 ** max(0, attempt - 1)))
    return raw * (0.5 + rng() / 2)


_INTERRUPT_STUB = (
    "Error: interrupted by the user before this call completed. It was not "
    "(fully) executed — re-run it if still needed."
)


def seal_partial_batch(messages: list) -> int:
    """Stub-fill missing tool results for a trailing half-answered batch.

    If history ends inside a tool batch (a trailing assistant message with
    tool_calls, followed by results for only some of its ids), append a
    stub result for each missing id so every tool_use has exactly one
    tool_result. Never pops anything: earlier calls in the batch already
    executed — their side effects are real, and the model must keep
    knowing that. Returns the number of stubs added.
    """
    # Find the last assistant-with-tool_calls message; everything after it
    # must be its tool results for the batch to be "trailing".
    idx = None
    for i in range(len(messages) - 1, -1, -1):
        role = messages[i].get("role")
        if role == "assistant" and messages[i].get("tool_calls"):
            idx = i
            break
        if role in ("user", "system", "assistant"):
            return 0  # batch (if any) is not trailing — nothing dangling
    if idx is None:
        return 0
    answered = {
        m.get("tool_call_id")
        for m in messages[idx + 1:]
        if m.get("role") == "tool"
    }
    added = 0
    for tc in messages[idx]["tool_calls"]:
        tc_id = tc.get("id")
        if tc_id and tc_id not in answered:
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": _INTERRUPT_STUB,
            })
            added += 1
    return added


def completed_actions_since_user(messages: list) -> int:
    """Count tool-result messages since the last user message."""
    n = 0
    for m in reversed(messages):
        if m.get("role") == "user":
            break
        if m.get("role") == "tool":
            n += 1
    return n
