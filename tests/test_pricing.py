"""Cost computation from usage × catalog pricing.

The gateway returns no `cost` field (verified live), so the CLI computes it.
These lock the arithmetic — including the cases where returning None beats
returning a confident wrong number.
"""
from meshapi import pricing

CATALOG = [
    {"id": "openai/gpt-4o-mini", "pricing": {
        "prompt_usd_per_1m": "0.15000000",
        "completion_usd_per_1m": "0.60000000",
        "cache_read_input_usd_per_1m": "0.07500000",
        "discount_pct": None}},
    {"id": "anthropic/claude-opus-4.8", "pricing": {
        "prompt_usd_per_1m": "5.00000000",
        "completion_usd_per_1m": "25.00000000",
        "cache_read_input_usd_per_1m": "0.50000000",
        "discount_pct": "15.00"}},
    {"id": "some/image-model", "pricing": {
        "prompt_usd_per_1m": None, "completion_usd_per_1m": None,
        "image_output_usd_per_image": "0.04"}},
]


def test_basic_token_cost():
    usage = {"prompt_tokens": 19, "completion_tokens": 8}
    got = pricing.estimate_cost("openai/gpt-4o-mini", usage, CATALOG)
    assert abs(got - (19 * 0.15 + 8 * 0.60) / 1e6) < 1e-15


def test_resolved_upstream_id_still_matches():
    """The echoed model is often the upstream build string."""
    usage = {"prompt_tokens": 100, "completion_tokens": 100}
    a = pricing.estimate_cost("gpt-4o-mini-2024-07-18", usage, CATALOG)
    b = pricing.estimate_cost("openai/gpt-4o-mini", usage, CATALOG)
    assert a == b and a > 0


def test_cached_tokens_billed_at_cache_rate_not_added_twice():
    usage = {"prompt_tokens": 1000, "completion_tokens": 0,
             "prompt_tokens_details": {"cached_tokens": 800}}
    got = pricing.estimate_cost("openai/gpt-4o-mini", usage, CATALOG)
    expected = (200 * 0.15 + 800 * 0.075) / 1e6
    assert abs(got - expected) < 1e-15


def test_cached_tokens_capped_at_prompt_tokens():
    usage = {"prompt_tokens": 10, "completion_tokens": 0,
             "prompt_tokens_details": {"cached_tokens": 9999}}
    got = pricing.estimate_cost("openai/gpt-4o-mini", usage, CATALOG)
    assert got == (10 * 0.075) / 1e6


def test_discount_applied():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    got = pricing.estimate_cost("anthropic/claude-opus-4.8", usage, CATALOG)
    assert abs(got - 5.00 * 0.85) < 1e-9


def test_auto_route_classifier_tokens_are_billed():
    """Auto-routing bills the classifier too, reported separately."""
    usage = {"prompt_tokens": 10, "completion_tokens": 10,
             "classifier_prompt_tokens": 1451,
             "classifier_completion_tokens": 26}
    got = pricing.estimate_cost("openai/gpt-4o-mini", usage, CATALOG)
    expected = (10 * 0.15 + 10 * 0.60 + 1451 * 0.15 + 26 * 0.60) / 1e6
    assert abs(got - expected) < 1e-15


def test_unknown_model_returns_none_not_zero():
    assert pricing.estimate_cost("nope/nope", {"prompt_tokens": 5}, CATALOG) is None


def test_non_token_priced_model_returns_none():
    assert pricing.estimate_cost("some/image-model", {"prompt_tokens": 5}, CATALOG) is None


def test_no_usage_or_no_catalog_returns_none():
    assert pricing.estimate_cost("openai/gpt-4o-mini", {}, CATALOG) is None
    assert pricing.estimate_cost("openai/gpt-4o-mini", {"prompt_tokens": 5}, None) is None


def test_garbage_usage_returns_none():
    assert pricing.estimate_cost(
        "openai/gpt-4o-mini", {"prompt_tokens": "abc"}, CATALOG) is None


# ---------------------------------------------------------------------------
# Authoritative cost via POST /v1/usage/events
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _install_post(monkeypatch, resp, capture=None):
    import httpx

    def fake_post(url, headers=None, json=None, timeout=None):
        if capture is not None:
            capture.update(url=url, body=json)
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(httpx, "post", fake_post)


CFG = {"base_url": "https://api.meshapi.ai/v1", "api_key": "rsk_x"}


def test_fetch_actual_costs_matches_only_our_ids(monkeypatch):
    payload = {"events": [
        {"request_id": "req_a", "cost_usd": "0.00000800"},
        {"request_id": "req_b", "cost_usd": "0.00001620"},
        {"request_id": "req_other", "cost_usd": "9.99"},   # someone else's
    ]}
    cap = {}
    _install_post(monkeypatch, _FakeResp(200, payload), cap)
    got = pricing.fetch_actual_costs(CFG, ["req_a", "req_b"], "2026-08-19T00:00:00Z")
    assert got == {"req_a": 8e-06, "req_b": 1.62e-05}
    # org_id must be sent (schema requires it) but empty (ignored for API keys)
    assert cap["body"]["org_id"] == ""
    assert cap["body"]["since"] == "2026-08-19T00:00:00Z"
    assert cap["url"].endswith("/usage/events")


def test_fetch_actual_costs_partial_returns_what_it_found(monkeypatch):
    _install_post(monkeypatch, _FakeResp(200, {"events": [
        {"request_id": "req_a", "cost_usd": "0.001"}]}))
    got = pricing.fetch_actual_costs(CFG, ["req_a", "req_b"], "s")
    assert got == {"req_a": 0.001}      # caller decides whether that's enough


def test_fetch_actual_costs_none_on_error_status(monkeypatch):
    _install_post(monkeypatch, _FakeResp(403))
    assert pricing.fetch_actual_costs(CFG, ["req_a"], "s") is None


def test_fetch_actual_costs_none_on_exception(monkeypatch):
    import httpx
    _install_post(monkeypatch, httpx.ConnectError("down"))
    assert pricing.fetch_actual_costs(CFG, ["req_a"], "s") is None


def test_fetch_actual_costs_no_ids_skips_the_call(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not call the API with no ids")
    import httpx
    monkeypatch.setattr(httpx, "post", explode)
    assert pricing.fetch_actual_costs(CFG, [], "s") is None


def test_fetch_actual_costs_ignores_unparseable_cost(monkeypatch):
    _install_post(monkeypatch, _FakeResp(200, {"events": [
        {"request_id": "req_a", "cost_usd": "abc"},
        {"request_id": "req_b", "cost_usd": None}]}))
    assert pricing.fetch_actual_costs(CFG, ["req_a", "req_b"], "s") is None
