"""Streaming OpenAI-compatible HTTP client for Mesh API."""
import json
from typing import Iterable

import httpx


def stream_chat(messages: list, cfg: dict) -> Iterable:
    """Yield content deltas, then a final {'usage':..., 'cost':...} dict.

    Mesh API is OpenAI-compatible but adds `cost` to the final SSE chunk.
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

    last_meta: dict = {}
    last_model: str = ""
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
                delta = choices[0].get("delta", {}).get("content")
                if delta:
                    yield delta

            usage = obj.get("usage")
            cost = obj.get("cost")
            if usage or cost:
                last_meta = {"usage": usage, "cost": cost}

    if last_model:
        last_meta["model"] = last_model
    if last_meta:
        yield last_meta
