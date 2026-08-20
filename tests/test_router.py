"""Smart routing — the thin table-driven picker (router.py).

Locks the fail-open contract (routing may NEVER break a request), the
classifier's signal precedence, weight normalization, frontier scoring
semantics, and session stickiness. The table here is synthetic — the real
one is compiled elsewhere and is just data to this module.
"""
import pytest

from meshapi import router
from meshapi.router import classify, normalize_weights, pick

TABLE = {
    "table_version": 1,
    "cohorts": ["coding", "chat", "vision"],
    "models": {
        "a/cheap":  {"caps": {"tools": True, "structured": True, "vision": False},
                     "ctx": 128000, "speed": 85, "scores": {"chat": 58, "coding": 45}},
        "b/mid":    {"caps": {"tools": True, "structured": True, "vision": True},
                     "ctx": 200000, "speed": 65, "scores": {"chat": 74, "coding": 70}},
        "c/best":   {"caps": {"tools": True, "structured": True, "vision": True},
                     "ctx": 1000000, "speed": 45, "scores": {"chat": 92, "coding": 93}},
        "d/notool": {"caps": {"tools": False, "structured": True, "vision": False},
                     "ctx": 128000, "speed": 90, "scores": {"chat": 80, "coding": 60}},
    },
    "frontiers": {"chat": ["c/best", "b/mid", "a/cheap", "d/notool"],
                  "coding": ["c/best", "b/mid", "a/cheap"]},
    "defaults": {"chat": "a/cheap", "coding": "b/mid"},
}
CATALOG = [
    {"id": "a/cheap", "pricing": {"prompt_usd_per_1m": "0.15", "completion_usd_per_1m": "0.60"}},
    {"id": "b/mid",   "pricing": {"prompt_usd_per_1m": "1.00", "completion_usd_per_1m": "4.00"}},
    {"id": "c/best",  "pricing": {"prompt_usd_per_1m": "5.00", "completion_usd_per_1m": "25.00"}},
    {"id": "d/notool", "pricing": {"prompt_usd_per_1m": "0.10", "completion_usd_per_1m": "0.40"}},
]


# ---------------------------------------------------------------------------
# classify — signal precedence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, kw, cohort", [
    ("fix this traceback in server.py", {}, "coding"),
    ("fix this traceback", {"has_tools": True}, "agentic"),
    ("describe this screenshot", {"has_image": True}, "vision"),
    ("extract the invoice fields as json", {}, "extraction"),
    ("solve the equation 3x + 4 = 19", {}, "reasoning-math"),
    ("write a blog post about monsoons", {}, "writing"),
    ("what's the latest news on the election", {}, "web-research"),
    ("classify the sentiment of this review", {}, "cheap-bulk"),
    ("hello there", {}, "chat"),
    ("hello there", {"has_tools": True}, "agentic"),
])
def test_classify(text, kw, cohort):
    got, conf = classify(text, **kw)
    assert got == cohort
    assert 0 < conf <= 1


def test_classify_image_beats_everything():
    got, conf = classify("fix this code ```py\nx```", has_image=True)
    assert got == "vision" and conf > 0.9


def test_classify_long_context():
    got, _ = classify("summarize", history_chars=200_000)
    assert got == "long-context"


# ---------------------------------------------------------------------------
# weights
# ---------------------------------------------------------------------------

def test_weights_normalize_to_one():
    w = normalize_weights({"cost": 5, "cap": 3, "speed": 2})
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert w == {"cost": 0.5, "cap": 0.3, "speed": 0.2}


def test_weights_garbage_falls_back_to_defaults():
    w = normalize_weights({"cost": "x", "cap": None})
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_weights_negative_clamped():
    w = normalize_weights({"cost": -5, "cap": 1, "speed": 0})
    assert w["cost"] == 0.0 and w["cap"] == 1.0


# ---------------------------------------------------------------------------
# pick — semantics
# ---------------------------------------------------------------------------

def test_cost_heavy_picks_cheapest_feasible():
    # d/notool is the cheapest chat candidate overall…
    got = pick("chat", {"cost": 0.9, "cap": 0.05, "speed": 0.05}, TABLE, CATALOG)
    assert got["model"] == "d/notool"
    # …but with tools required (the CLI's reality), a/cheap wins
    got = pick("chat", {"cost": 0.9, "cap": 0.05, "speed": 0.05}, TABLE, CATALOG,
               needs_tools=True)
    assert got["model"] == "a/cheap"


def test_cap_heavy_picks_best():
    got = pick("chat", {"cost": 0.02, "cap": 0.95, "speed": 0.03}, TABLE, CATALOG)
    assert got["model"] == "c/best"


def test_needs_tools_excludes_toolless():
    got = pick("chat", {"cost": 1, "cap": 0, "speed": 0}, TABLE, CATALOG,
               needs_tools=True)
    assert got["model"] != "d/notool"     # cheapest overall, but can't hold tools


def test_needs_ctx_filters():
    got = pick("chat", {"cost": 0.9, "cap": 0.05, "speed": 0.05}, TABLE, CATALOG,
               needs_ctx=500_000)
    assert got["model"] == "c/best"       # only one with 1M ctx


def test_sticky_keeps_incumbent_in_top3():
    got = pick("chat", {"cost": 0.9, "cap": 0.05, "speed": 0.05}, TABLE, CATALOG,
               incumbent="b/mid")
    assert got["model"] == "b/mid" and got["sticky"] is True


def test_sticky_drops_incumbent_out_of_candidates():
    got = pick("chat", {"cost": 0.9, "cap": 0.05, "speed": 0.05}, TABLE, CATALOG,
               needs_ctx=500_000, incumbent="a/cheap")
    assert got["model"] == "c/best" and got["sticky"] is False


def test_ranked_view_exposes_axes():
    got = pick("chat", None, TABLE, CATALOG)
    assert got["ranked"] and all(
        set(r) >= {"model", "score", "cap", "cost", "speed"} for r in got["ranked"])


# ---------------------------------------------------------------------------
# fail-open — the load-bearing contract
# ---------------------------------------------------------------------------

def test_fail_open_no_table():
    assert pick("chat", None, None, CATALOG) is None


def test_fail_open_no_catalog():
    assert pick("chat", None, TABLE, None) is None


def test_fail_open_unknown_cohort_uses_default_then_none():
    assert pick("nope", None, TABLE, CATALOG) is None


def test_fail_open_all_filtered_falls_to_default():
    got = pick("chat", None, TABLE, CATALOG, needs_ctx=10_000_000)
    assert got == {"model": "a/cheap", "cohort": "chat", "ranked": [], "sticky": False}


def test_fail_open_catalog_drift_skips_missing():
    cat = [c for c in CATALOG if c["id"] != "c/best"]   # table ahead of catalog
    got = pick("chat", {"cost": 0, "cap": 1, "speed": 0}, TABLE, cat,
               needs_tools=True)
    assert got["model"] == "b/mid"    # best cap among tool-capable survivors


def test_fail_open_pick_never_raises():
    assert pick("chat", {"cost": "x"}, {"models": "garbage"}, CATALOG) is None


def test_load_table_missing_returns_none(tmp_path):
    assert router.load_table(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    assert router.load_table(str(bad)) is None
