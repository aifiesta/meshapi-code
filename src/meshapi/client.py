"""Streaming OpenAI-compatible HTTP client for Mesh API."""
import json
from typing import Iterable, Optional

import httpx


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

    last_meta: dict = {}
    last_model: str = ""
    tool_calls_accum: dict = {}  # index -> {id, name, arguments}

    with httpx.stream("POST", url, json=payload, headers=headers, timeout=120) as r:
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

    if last_model:
        last_meta["model"] = last_model
    if tool_calls_accum:
        last_meta["tool_calls"] = [tool_calls_accum[i] for i in sorted(tool_calls_accum)]
    if last_meta:
        yield last_meta
