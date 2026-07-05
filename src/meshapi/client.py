"""Streaming OpenAI-compatible HTTP client for Mesh API."""
import json
from typing import Iterable, Optional

import httpx

from .optimize import prepare


def _is_complete_json(s: str) -> bool:
    if not s.strip():
        return False
    try:
        json.loads(s)
        return True
    except json.JSONDecodeError:
        return False


class ToolCallAccumulator:
    """Accumulate streamed tool_call deltas into complete calls.

    OpenAI-spec providers key every delta by `index`, but Mesh fronts many
    providers and not all of them play by the book. Observed misbehaviors,
    each of which used to surface as a doomed call like
    `write_file: (missing path) (0 chars)`:

      - deltas with no `index` at all — the old `tc.get("index", 0)` merged
        parallel calls into one bucket, concatenating their JSON arguments
        into garbage like `{"path":"a"}{"path":"b"}`
      - argument fragments arriving under a different index than the call's
        name — a named call with empty args plus an orphan holding the args
      - missing `id`s — replaying the assistant message with `"id": ""`
        breaks the tool-result pairing on strict providers next hop
      - nameless buckets that can't be executed or replayed

    Routing rules: an explicit `index` always wins; a known `id` continues
    its call; a new `id` starts a new call; a keyless/indexless fragment
    belongs to the call currently in flight. `finalize()` then repairs
    orphans, drops the unexecutable, and synthesizes missing ids.
    """

    def __init__(self):
        self._by_index: dict = {}
        self._order: list = []    # buckets in arrival order
        self._current = None      # last bucket touched — target for bare fragments

    def add(self, tc: dict) -> None:
        idx = tc.get("index")
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        if isinstance(idx, int):
            bucket = self._by_index.get(idx)
            if bucket is None:
                bucket = self._new_bucket(idx)
        elif tc_id:
            bucket = next((b for b in self._order if b["id"] == tc_id), None)
            if bucket is None:  # new id, no index: a new call is starting
                bucket = self._new_bucket(self._next_free_index())
        else:  # continuation fragment — belongs to the call in flight
            bucket = self._current or self._new_bucket(0)
        if tc_id:
            bucket["id"] = tc_id
        if fn.get("name"):
            bucket["name"] = fn["name"]
        if fn.get("arguments"):
            bucket["arguments"] += fn["arguments"]
        self._current = bucket

    def _new_bucket(self, idx: int) -> dict:
        b = {"id": "", "name": "", "arguments": "", "_idx": idx}
        self._by_index[idx] = b
        self._order.append(b)
        return b

    def _next_free_index(self) -> int:
        i = len(self._by_index)
        while i in self._by_index:
            i += 1
        return i

    def finalize(self) -> list:
        """Return completed calls, repairing what can be repaired.

        - An orphan bucket (no name, no id, args only) is an argument stream
          that arrived under the wrong index: merge it into the nearest
          earlier named call whose args are still empty/incomplete. Never
          merge into a call whose args already parse — that would corrupt a
          good call to rescue a broken one.
        - Nameless leftovers are dropped (not executable, and replaying them
          in the assistant message 400s on strict providers).
        - Missing ids get a deterministic `call_<n>` so the assistant/tool
          message pairing survives the next hop.
        """
        calls: list = []
        for b in sorted(self._order, key=lambda b: b["_idx"]):
            if not b["name"] and not b["id"] and b["arguments"]:
                target = next(
                    (p for p in reversed(calls)
                     if p["name"] and not _is_complete_json(p["arguments"])),
                    None,
                )
                if target is not None:
                    target["arguments"] += b["arguments"]
                continue  # merged, or unrecoverable — never surface alone
            calls.append(b)
        calls = [c for c in calls if c["name"]]
        for n, c in enumerate(calls):
            if not c["id"]:
                c["id"] = f"call_{n}"
            c.pop("_idx", None)
        return calls


def build_payload(messages: list, cfg: dict, tools: Optional[list] = None) -> dict:
    """Build the /chat/completions request body from session config.

    Pure function (unit-testable without a network). Mesh extensions:
      - auto_route  -> model:"auto" (gateway Auto Router picks per prompt)
      - fallback_models -> `models` ordered fallback list
      - reasoning_effort -> passed through when set ("none" is a real level;
        None means "don't send")
    """
    payload: dict = {
        "model": "auto" if cfg.get("auto_route") else cfg["model"],
        "messages": messages,
        "stream": True,
    }
    if cfg.get("fallback_models"):
        payload["models"] = list(cfg["fallback_models"])
    if cfg.get("reasoning_effort"):
        payload["reasoning_effort"] = cfg["reasoning_effort"]
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def stream_chat(
    messages: list,
    cfg: dict,
    tools: Optional[list] = None,
) -> Iterable:
    """Yield content deltas, then a final dict with usage/cost/model/tool_calls.

    Mesh API is OpenAI-compatible:
      - `cost` arrives in the final SSE chunk alongside `usage`.
      - `tool_calls` arrive as deltas indexed by position; we accumulate them
        and surface as the meta dict's `tool_calls` field.

    When the `optimize` dial is set (BETA), the request is rewritten by the
    phase 1 lever stack in optimize.py before sending, and the plan rides on
    the final meta dict as `optimize_plan`. If the gateway rejects the
    optimized request, we retry the raw request once, so the beta can never
    be the reason a turn fails.
    """
    url = f"{cfg['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    payload = build_payload(messages, cfg, tools)

    plan: dict = {}
    attempts = [payload]
    dial = float(cfg.get("optimize") or 0)
    if dial > 0:
        # Pass the model string actually being sent ("auto" falls back to
        # optimize's default cache-minimum heuristic — harmless).
        opt_messages, extra, plan = prepare(
            messages, payload["model"], dial, has_tools=bool(tools)
        )
        if plan.get("levers_applied"):
            optimized = {**payload, **extra, "messages": opt_messages}
            attempts = [optimized, payload]  # raw payload is the fallback

    last_meta: dict = {}
    last_model: str = ""
    accum = ToolCallAccumulator()

    for attempt_index, body in enumerate(attempts):
        is_last_attempt = attempt_index == len(attempts) - 1
        with httpx.stream("POST", url, json=body, headers=headers, timeout=120) as r:
            if r.status_code >= 400:
                r.read()  # so e.response.text works in the caller
                if not is_last_attempt:
                    # Optimized request rejected; degrade to the raw request.
                    plan = {
                        "dial": dial,
                        "levers_applied": [],
                        "degraded": f"gateway returned {r.status_code}, sent raw request",
                    }
                    continue
            r.raise_for_status()
            # When auto-routed, the gateway names the concrete model it
            # picked in this header; SSE chunks' `model` agrees, but the
            # header is the authoritative belt-and-braces.
            resolved = r.headers.get("x-resolved-model-id")
            if resolved:
                last_model = resolved
            for line in r.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if obj.get("model"):
                    last_model = obj["model"]

                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta", {})

                    content = delta.get("content")
                    if content:
                        yield content

                    for tc in delta.get("tool_calls") or []:
                        accum.add(tc)

                usage = obj.get("usage")
                cost = obj.get("cost")
                if usage or cost:
                    last_meta = {"usage": usage, "cost": cost}
        break  # this attempt streamed successfully

    if last_model:
        last_meta["model"] = last_model
    tool_calls = accum.finalize()
    if tool_calls:
        last_meta["tool_calls"] = tool_calls
    if plan:
        last_meta["optimize_plan"] = plan
    if last_meta:
        yield last_meta
