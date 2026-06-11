"""Streaming OpenAI-compatible HTTP client for Mesh API."""
import json
from typing import Iterable, Optional

import httpx

from .optimize import prepare


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
    payload: dict = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
    }
    if cfg.get("route"):
        payload["route"] = cfg["route"]
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    plan: dict = {}
    attempts = [payload]
    dial = float(cfg.get("optimize") or 0)
    if dial > 0:
        opt_messages, extra, plan = prepare(
            messages, cfg["model"], dial, has_tools=bool(tools)
        )
        if plan.get("levers_applied"):
            optimized = {**payload, **extra, "messages": opt_messages}
            attempts = [optimized, payload]  # raw payload is the fallback

    last_meta: dict = {}
    last_model: str = ""
    tool_calls_accum: dict = {}  # index -> {id, name, arguments}

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
                        idx = tc.get("index", 0)
                        bucket = tool_calls_accum.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.get("id"):
                            bucket["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            bucket["name"] = fn["name"]
                        if fn.get("arguments"):
                            bucket["arguments"] += fn["arguments"]

                usage = obj.get("usage")
                cost = obj.get("cost")
                if usage or cost:
                    last_meta = {"usage": usage, "cost": cost}
        break  # this attempt streamed successfully

    if last_model:
        last_meta["model"] = last_model
    if tool_calls_accum:
        last_meta["tool_calls"] = [tool_calls_accum[i] for i in sorted(tool_calls_accum)]
    if plan:
        last_meta["optimize_plan"] = plan
    if last_meta:
        yield last_meta
