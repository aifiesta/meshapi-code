"""Mesh Optimize (BETA) — gateway-level token savings, applied client-side.

BETA FEATURE. Off by default. Enable with `/optimize 0.3` or the `optimize`
config key. Behavior, savings math, and the lever stack may change between
releases. Set `/optimize 0` to bypass everything.

Python port of the phase 1 levers from the mesh-optimize reference
implementation (https://github.com/raushan-aifiesta/mesh-optimize):

  dial 0+    cache_control breakpoint injection on stable prefixes,
             max_tokens defaults per task class
  dial 0.2+  pruning of tool results the model already consumed

Hard rules carried over from the reference implementation:
  - deterministic: same input always produces the same output
  - never touches content inside a client-set cache breakpoint
  - savings are reported honestly or not at all
  - the original message list is never mutated
"""
import copy
import hashlib
import json
import re

# tokens, chars/4 estimate. Below the per-model minimum a cache_control
# marker silently does nothing, so we do not inject one.
_CACHE_MINIMUMS = [
    (re.compile(r"fable"), 2048),
    (re.compile(r"sonnet-4-6"), 2048),
    (re.compile(r"opus"), 4096),
    (re.compile(r"haiku-4-5"), 4096),
]
_DEFAULT_CACHE_MINIMUM = 2048

_KEEP_RECENT_MESSAGES = 4
_TRUNCATE_TO_CHARS = 400

_CODE_RE = re.compile(r"```|(?:\bfunction\b|\bclass\b|\bimport\b|\bdef\b)\s")
_COMPLEX_RE = re.compile(
    r"\b(refactor|implement|debug|architect|migrate|optimi[sz]e|analy[sz]e)\b",
    re.IGNORECASE,
)

_MAX_TOKENS_DEFAULTS = {
    "routine": 1024,
    "standard": 1024,
    "complex": 4096,
    "agentic": 4096,
}


def normalize_model(model: str) -> str:
    """anthropic/claude-opus-4.8 -> claude-opus-4-8 (bare, dashed)."""
    bare = (model or "").lower().rsplit("/", 1)[-1]
    return bare.replace(".", "-")


def _cache_minimum(model: str) -> int:
    bare = normalize_model(model)
    for pattern, minimum in _CACHE_MINIMUMS:
        if pattern.search(bare):
            return minimum
    return _DEFAULT_CACHE_MINIMUM


def _est_tokens(value) -> int:
    if value is None:
        return 0
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return -(-len(text) // 4)  # ceil division


def _msg_tokens(message: dict) -> int:
    return _est_tokens(message.get("content")) + 4


def _classify(messages: list, has_tools: bool) -> str:
    depth = len(messages)
    sample = ""
    for message in messages[-6:]:
        content = message.get("content")
        sample += (content if isinstance(content, str) else json.dumps(content)) + "\n"
    if has_tools and depth > 6:
        return "agentic"
    if has_tools:
        return "complex"
    if _CODE_RE.search(sample) or _COMPLEX_RE.search(sample):
        return "complex"
    if sum(_msg_tokens(m) for m in messages[-6:]) < 150 and depth <= 4:
        return "routine"
    return "standard"


def _has_client_breakpoints(messages: list) -> bool:
    return any("cache_control" in m for m in messages)


def prepare(messages: list, model: str, dial: float, has_tools: bool) -> tuple:
    """Apply the lever stack for the given dial. BETA.

    Returns (optimized_messages, extra_payload, plan). The input list is
    never mutated. dial 0 returns everything untouched.
    """
    plan = {
        "dial": dial,
        "classification": "standard",
        "levers_applied": [],
        "tokens_pruned_est": 0,
        "audit": [],
    }
    if not dial or dial <= 0:
        return messages, {}, plan

    out = copy.deepcopy(messages)
    plan["classification"] = _classify(out, has_tools)

    # lever: tool result pruning (dial 0.2+). Old tool outputs were already
    # consumed by the model in the turn they answered; the full payload is
    # dead weight on every later request.
    if dial >= 0.2:
        cutoff = len(out) - _KEEP_RECENT_MESSAGES
        chars_removed = 0
        for i in range(max(cutoff, 0)):
            message = out[i]
            content = message.get("content")
            if (
                message.get("role") == "tool"
                and isinstance(content, str)
                and len(content) > _TRUNCATE_TO_CHARS * 2
            ):
                digest = hashlib.sha256(content.encode()).hexdigest()
                truncated = (
                    content[:_TRUNCATE_TO_CHARS]
                    + f"\n[mesh: pruned {len(content) - _TRUNCATE_TO_CHARS} chars "
                    "of consumed tool output]"
                )
                message["content"] = truncated
                chars_removed += len(content) - len(truncated)
                plan["audit"].append({
                    "lever": "tool_result_pruning",
                    "action": f"truncated tool result at message {i}",
                    "content_sha256": digest,
                })
        if chars_removed:
            plan["tokens_pruned_est"] = -(-chars_removed // 4)
            plan["levers_applied"].append("tool_result_pruning")

    # lever: cache_control injection (dial 0+). Skips entirely when the
    # client placed its own breakpoints. Runs after pruning so breakpoints
    # land on the final bytes.
    if not _has_client_breakpoints(out):
        minimum = _cache_minimum(model)
        applied = False
        if out and out[0].get("role") == "system":
            first_tokens = _est_tokens(out[0].get("content"))
            if first_tokens >= minimum:
                out[0]["cache_control"] = {"type": "ephemeral"}
                plan["audit"].append({
                    "lever": "cache_injection",
                    "action": f"breakpoint on system message (~{first_tokens} tok)",
                })
                applied = True
        if len(out) >= 3:
            prefix_tokens = sum(_msg_tokens(m) for m in out[:-1])
            anchor = out[-2]
            if prefix_tokens >= minimum and "cache_control" not in anchor:
                anchor["cache_control"] = {"type": "ephemeral"}
                plan["audit"].append({
                    "lever": "cache_injection",
                    "action": f"breakpoint on history (~{prefix_tokens} tok prefix)",
                })
                applied = True
        if applied:
            plan["levers_applied"].append("cache_injection")

    # lever: max_tokens default per task class (dial 0+). A backstop against
    # runaway generation, applied only because the CLI does not set one.
    extra = {"max_tokens": _MAX_TOKENS_DEFAULTS[plan["classification"]]}
    plan["levers_applied"].append("max_tokens_default")
    plan["audit"].append({
        "lever": "max_tokens_default",
        "action": f"max_tokens={extra['max_tokens']} for {plan['classification']} task",
    })

    return out, extra, plan


def savings_line(plan: dict, usage: dict) -> str:
    """One-line honest savings summary for the post-turn status line.

    Only reports what is measurable: pruned tokens (chars/4 estimate) and
    cache fields when the gateway surfaces them in usage. No counterfactual
    guessing.
    """
    if not plan or not plan.get("levers_applied"):
        return ""
    parts = []
    pruned = plan.get("tokens_pruned_est", 0)
    if pruned:
        parts.append(f"~{pruned} tok pruned")
    usage = usage or {}
    cache_read = usage.get("cache_read_input_tokens") or 0
    if cache_read:
        parts.append(f"{cache_read} tok from cache (90% off)")
    if "cache_injection" in plan["levers_applied"] and not cache_read:
        parts.append("cache breakpoints set")
    detail = ", ".join(parts) if parts else ", ".join(plan["levers_applied"])
    return f"⚡ optimize beta (dial {plan['dial']}): {detail}"
