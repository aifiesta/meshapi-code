"""Client-side cost computation from usage + the live model catalog.

The gateway does **not** return a `cost` field on chat completions — not in
the SSE stream, not on the non-streaming body (verified live 2026-08-19).
The CLI's cost line therefore had nothing to show. But everything needed to
compute it exactly is already in hand: `usage` comes back on every request,
and `GET /v1/models` carries full per-dimension pricing including the user's
own `discount_pct`.

So we compute it. Values are labelled as estimates ("~") in the UI because
the gateway never confirms them, but the arithmetic is the same the
dashboard bills on: token counts × the model's own per-1M rates, with
cached prompt tokens billed at the cache-read rate when the model has one.
"""
from __future__ import annotations


def _dec(value) -> "float | None":
    """Catalog prices are decimal STRINGS ("5.00000000") or null."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def find_model(model_id: str, models: "list | None") -> "dict | None":
    """Catalog row for a model id, tolerating the resolved-id suffix.

    The `model` echoed back is often the upstream's own build string
    ("gpt-4o-mini-2024-07-18") rather than the catalog id
    ("openai/gpt-4o-mini"), so fall back to a suffix match on the last path
    segment before giving up.
    """
    if not model_id or not models:
        return None
    for m in models:
        if m.get("id") == model_id:
            return m
    tail = model_id.rsplit("/", 1)[-1]
    best = None
    for m in models:
        mid = m.get("id") or ""
        mtail = mid.rsplit("/", 1)[-1]
        if mtail == tail:
            return m
        # "gpt-4o-mini-2024-07-18" starts with "gpt-4o-mini"
        if tail.startswith(mtail) and (best is None or len(mtail) > len(best[0])):
            best = (mtail, m)
    return best[1] if best else None


def estimate_cost(model_id: str, usage: dict,
                  models: "list | None") -> "float | None":
    """USD for one request, or None when it can't be computed honestly.

    None (rather than 0.0) whenever the model isn't in the catalog or has no
    token pricing — a confident $0.000000 would be a lie, and the caller
    omits the segment instead.
    """
    if not usage:
        return None
    row = find_model(model_id, models)
    if not row:
        return None
    pricing = row.get("pricing") or {}
    prompt_rate = _dec(pricing.get("prompt_usd_per_1m"))
    completion_rate = _dec(pricing.get("completion_usd_per_1m"))
    if prompt_rate is None and completion_rate is None:
        # Non-token pricing (per-image, per-video) — not a chat cost.
        return None
    prompt_rate = prompt_rate or 0.0
    completion_rate = completion_rate or 0.0

    try:
        prompt_t = int(usage.get("prompt_tokens") or 0)
        completion_t = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return None

    # Cached prompt tokens bill at the cache-read rate when the model has
    # one; they are a SUBSET of prompt_tokens, so discount the difference
    # rather than adding on top.
    cached_t = 0
    details = usage.get("prompt_tokens_details") or {}
    try:
        cached_t = int(details.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        cached_t = 0
    cached_t = max(0, min(cached_t, prompt_t))
    cache_rate = _dec(pricing.get("cache_read_input_usd_per_1m"))

    fresh_t = prompt_t - cached_t
    cost = (fresh_t * prompt_rate) / 1_000_000
    cost += (cached_t * (cache_rate if cache_rate is not None else prompt_rate)) / 1_000_000
    cost += (completion_t * completion_rate) / 1_000_000

    # Auto-routing bills the classifier too — the gateway reports its tokens
    # separately, and they are NOT included in prompt_tokens.
    for key, rate in (("classifier_prompt_tokens", prompt_rate),
                      ("classifier_completion_tokens", completion_rate)):
        try:
            extra = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            extra = 0
        if extra > 0:
            cost += (extra * rate) / 1_000_000

    discount = _dec(pricing.get("discount_pct"))
    if discount:
        cost *= max(0.0, 1.0 - discount / 100.0)
    return cost


def fetch_actual_costs(cfg: dict, request_ids: list, since: str,
                       timeout: float = 6.0) -> "dict | None":
    """Authoritative per-request cost from POST /v1/usage/events.

    The gateway bills each request and exposes the figure keyed by the
    `x-request-id` it returned — available immediately, streaming included
    (verified live: 0s lag). We ask for everything since the turn started
    and keep only the ids we actually issued, so another process using the
    same key can't inflate this turn's number.

    Quirk worth knowing: the schema marks `org_id` required, but the docs
    state it is *ignored* for API-key callers — an empty string satisfies
    validation and the scope stays server-derived to this key alone.

    Returns {request_id: float} (possibly partial), or None on any failure —
    the caller then keeps its computed estimate.
    """
    if not request_ids:
        return None
    try:
        import httpx
        r = httpx.post(
            f"{cfg['base_url'].rstrip('/')}/usage/events",
            headers={"Authorization": f"Bearer {cfg['api_key']}",
                     "Content-Type": "application/json"},
            json={"org_id": "", "since": since, "limit": 200},
            timeout=timeout,
        )
        if r.status_code >= 400:
            return None
        wanted = set(request_ids)
        out = {}
        for ev in (r.json() or {}).get("events") or []:
            rid = ev.get("request_id")
            if rid in wanted and ev.get("cost_usd") is not None:
                try:
                    out[rid] = float(ev["cost_usd"])
                except (TypeError, ValueError):
                    pass
        return out or None
    except Exception:
        return None
