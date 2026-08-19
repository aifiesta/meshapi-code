"""Deterministic history compaction — what makes day-long agentic turns
survivable.

Every hop re-sends the full message list, so an uncapped turn eventually
hits the model's context limit and the gateway rejects the request with an
in-band error ("prompt is too long"). This module keeps history under the
limit with zero extra spend and zero new failure modes: no gateway calls,
no LLM summarization — a structural fold of what already happened.

Two phases, mildest first:

1. TRUNCATE — old tool results (consumed output the model already acted
   on) are cut to a short head + a note. Assistant messages are never
   touched: write_file content rides in their tool_calls arguments and is
   the model's only durable record of what it wrote.
2. FOLD — if still over threshold, whole assistant/tool runs between user
   messages are replaced by ONE system message summarizing the actions
   structurally (paths written/read, commands run + exit codes). Whole
   runs only, so no tool_call id can end up orphaned.

The dedupe contract (memory.dedupe_read) is upheld explicitly: every
mutation drops or remaps the affected session_reads entries, and any
uncertainty clears them entirely — a dropped entry costs one re-read; a
stale one gaslights the model with "already in your context".

Token counts are estimates (chars/4). That's fine: the threshold has 30%
headroom, and the in-band context error path compacts again aggressively
and retries, so an underestimate degrades to one extra round-trip.
"""
from __future__ import annotations

import json
import math

COMPACT_THRESHOLD = 0.70          # compact when est tokens exceed 70% of the limit
KEEP_RECENT_MESSAGES = 8          # working window the compactor never touches
KEEP_RECENT_AGGRESSIVE = 4        # window when recovering from a context error
TRUNCATE_TO_CHARS = 400           # what survives of a truncated tool result
DEFAULT_CONTEXT_TOKENS = 128_000  # when the model isn't in the catalog
IMAGE_PART_TOKENS = 1_500         # flat per image part; chars/4 on base64 wildly overestimates
_FOLD_SUMMARY_CAP = 2_000         # max chars of one fold summary message


def est_message_tokens(msg: dict) -> int:
    """Rough token estimate for one message: chars/4, images flat-rated."""
    tokens = 4  # per-message structural overhead
    content = msg.get("content")
    if isinstance(content, str):
        tokens += math.ceil(len(content) / 4)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                tokens += IMAGE_PART_TOKENS
            else:
                try:
                    tokens += math.ceil(len(json.dumps(part)) / 4)
                except (TypeError, ValueError):
                    tokens += 50
    if msg.get("tool_calls"):
        try:
            tokens += math.ceil(len(json.dumps(msg["tool_calls"])) / 4)
        except (TypeError, ValueError):
            tokens += 50
    return tokens


def est_history_tokens(messages: list) -> int:
    return sum(est_message_tokens(m) for m in messages or [])


def context_limit(model_id: str, models: "list | None") -> int:
    """The model's context window from the live catalog, else a safe default."""
    for m in models or []:
        if m.get("id") == model_id:
            limit = m.get("context_length") or m.get("context_window")
            if isinstance(limit, int) and limit > 0:
                return limit
            break
    return DEFAULT_CONTEXT_TOKENS


def should_compact(messages: list, limit: int,
                   threshold: float = COMPACT_THRESHOLD) -> bool:
    return est_history_tokens(messages) > limit * threshold


def _describe_action(tc: dict, results: dict) -> str:
    """One short, tool-name-free clause describing a completed action."""
    fn = tc.get("function") or {}
    name = fn.get("name") or ""
    try:
        args = json.loads(fn.get("arguments") or "{}")
        if not isinstance(args, dict):
            args = {}
    except (ValueError, TypeError):
        args = {}
    result = results.get(tc.get("id"), "")
    if name == "write_file":
        content = args.get("content")
        n = len(content) if isinstance(content, str) else "?"
        return f"wrote {args.get('path') or 'a file'} ({n} chars)"
    if name == "read_file":
        return f"read {args.get('path') or 'a file'}"
    if name == "run_bash":
        cmd = (args.get("command") or "")[:60]
        exit_code = ""
        if isinstance(result, str) and "[exit " in result:
            exit_code = " → exit " + result.rsplit("[exit ", 1)[1].split("]")[0]
        return f"ran `{cmd}`{exit_code}"
    if name == "web_search":
        return f"searched the web for {(args.get('query') or '')[:60]!r}"
    if name == "start_server":
        return f"started a background server (`{(args.get('command') or '')[:50]}`)"
    if name in ("create_plan", "update_step"):
        return "updated the plan"
    if name == "remember":
        return "saved a note"
    return f"performed an action ({name})" if name else "performed an action"


def _fold_run(run: list, transcript: "str | None" = None) -> "dict | None":
    """Summarize one contiguous assistant/tool run as a single system msg."""
    actions = []
    replies = 0
    # Map tool_call_id -> result content for exit-code extraction.
    results = {
        m.get("tool_call_id"): m.get("content")
        for m in run if m.get("role") == "tool"
    }
    for m in run:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                actions.append(_describe_action(tc, results))
            if isinstance(m.get("content"), str) and m["content"].strip():
                replies += 1
    if replies:
        actions.append(f"replied to the user ({replies} message{'s' if replies > 1 else ''})")
    if not actions:
        return None
    body = "; ".join(actions)
    if len(body) > _FOLD_SUMMARY_CAP:
        shown, dropped = [], 0
        used = 0
        for a in actions:
            if used + len(a) + 2 > _FOLD_SUMMARY_CAP:
                dropped += 1
            else:
                shown.append(a)
                used += len(a) + 2
        body = "; ".join(shown) + f"; …and {dropped} more action(s)"
    recover = ""
    if transcript:
        # Compaction is lossy but must not be a dead end: name where the
        # exact text still lives so the model can read it back instead of
        # guessing or redoing the work.
        recover = (
            " If you need exact details from before this point (code you "
            "wrote, error text, file contents), read the full transcript at "
            f"{transcript} — it is complete and unmodified."
        )
    return {
        "role": "system",
        "content": (
            "[Earlier work in this session, summarized to save space: "
            + body
            + ". Files on disk reflect this work."
            + recover
            + "]"
        ),
    }


def _persist_transcript(state: dict, messages: list) -> "str | None":
    """Write the about-to-be-folded messages to the session transcript."""
    session_id = state.get("session_id")
    if not session_id:
        return None
    try:
        from .config import append_transcript, transcript_path
        already = state.get("_transcript_upto", 0)
        new_slice = messages[already:]
        if new_slice:
            append_transcript(session_id, new_slice)
            state["_transcript_upto"] = len(messages)
        return str(transcript_path(session_id))
    except Exception:
        return None


def compact_history(state: dict, limit: "int | None" = None,
                    aggressive: bool = False) -> dict:
    """Shrink state["messages"] in place; keep session_reads coherent.

    `limit` gates phase 2: folding runs only while the estimate still
    exceeds threshold×limit after truncation (or unconditionally when
    `aggressive` and no limit is known — the context-error recovery path).
    Returns {"before_tok", "after_tok", "truncated", "folded"} or {} when
    nothing was done. Must only be called between complete tool batches
    (hop top / retry path) — never mid-batch.
    """
    messages = state.get("messages") or []
    keep_recent = KEEP_RECENT_AGGRESSIVE if aggressive else KEEP_RECENT_MESSAGES
    before_tok = est_history_tokens(messages)
    reads = state.get("session_reads") or {}

    # ---- Phase 1: truncate old consumed tool results -----------------------
    truncated = 0
    cutoff = max(1, len(messages) - keep_recent)
    truncated_idx = set()
    for i in range(1, cutoff):  # index 0 = system prompt, never touched
        m = messages[i]
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str) or len(content) <= 2 * TRUNCATE_TO_CHARS:
            continue
        if content.endswith(" chars of consumed tool output removed]"):
            continue  # already compacted
        m["content"] = (
            content[:TRUNCATE_TO_CHARS]
            + f"\n[compacted: {len(content) - TRUNCATE_TO_CHARS} chars of "
            "consumed tool output removed]"
        )
        truncated += 1
        truncated_idx.add(i)
    if truncated_idx:
        # Read-sourced dedupe entries whose body was just cut can no longer
        # claim "full content is in context". Write-sourced entries point at
        # assistant messages (untouched here) and survive.
        for key in [k for k, e in reads.items()
                    if e.get("msg_index") in truncated_idx and e.get("source") != "write"]:
            reads.pop(key, None)

    # ---- Phase 2: fold whole assistant/tool runs (only if still over) ------
    folded = 0
    still_over = (
        limit is not None
        and est_history_tokens(messages) > limit * COMPACT_THRESHOLD
    ) or (limit is None and aggressive)
    if still_over:
        # Persist BEFORE dropping anything — the summary below points here.
        transcript = _persist_transcript(state, messages)
        try:
            new_messages = [messages[0]]  # system prompt survives
            index_map = {0: 0}
            run: list = []
            run_start = None

            def flush(run, run_start):
                nonlocal folded
                if not run:
                    return
                summary = _fold_run(run, transcript)
                if summary is not None:
                    new_messages.append(summary)
                    folded += 1
                else:
                    for off, m in enumerate(run):
                        index_map[run_start + off] = len(new_messages)
                        new_messages.append(m)

            cutoff2 = max(1, len(messages) - keep_recent)
            # Never bisect a tool batch: if the window boundary lands on a
            # tool result, walk back to its assistant parent so the whole
            # batch stays together (folded or kept — orphans are invalid).
            while cutoff2 > 1 and messages[cutoff2].get("role") == "tool":
                cutoff2 -= 1
            for i in range(1, len(messages)):
                m = messages[i]
                in_window = i >= cutoff2
                if not in_window and m.get("role") in ("assistant", "tool"):
                    if run_start is None:
                        run_start = i
                    run.append(m)
                    continue
                flush(run, run_start)
                run, run_start = [], None
                index_map[i] = len(new_messages)
                new_messages.append(m)
            flush(run, run_start)
            messages[:] = new_messages
            # Remap surviving dedupe entries; drop the ones whose message fell.
            for key in list(reads.keys()):
                old_idx = reads[key].get("msg_index")
                if old_idx in index_map:
                    reads[key]["msg_index"] = index_map[old_idx]
                else:
                    reads.pop(key, None)
        except Exception:
            # Fixup uncertainty must never gaslight dedupe — clear it all;
            # the cost is a handful of re-reads.
            state["session_reads"] = {}

    after_tok = est_history_tokens(messages)
    if not truncated and not folded:
        return {}
    return {
        "before_tok": before_tok,
        "after_tok": after_tok,
        "truncated": truncated,
        "folded": folded,
    }
