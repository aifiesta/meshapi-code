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
    assert w == pytest.approx({"cost": 0.5, "cap": 0.3, "speed": 0.2})


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


def test_fail_open_all_filtered_falls_to_feasible_default():
    # default a/cheap is feasible at modest ctx demands…
    got = pick("chat", None, TABLE, CATALOG, needs_ctx=1_500_000)
    assert got is None or got["model"] != "d/notool"
    # …but the default must obey feasibility too: nothing fits 10M ctx,
    # including the default — fall back to the caller's pin (None), never
    # hand back a model that can't hold the conversation.
    assert pick("chat", None, TABLE, CATALOG, needs_ctx=10_000_000) is None
    # and a tools-incapable default is equally unpickable
    t2 = {**TABLE, "frontiers": {"chat": []}, "defaults": {"chat": "d/notool"}}
    assert pick("chat", None, t2, CATALOG, needs_tools=True) is None
    t3 = {**TABLE, "frontiers": {"chat": []}, "defaults": {"chat": "a/cheap"}}
    got = pick("chat", None, t3, CATALOG, needs_tools=True)
    assert got["model"] == "a/cheap"      # feasible default still serves


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


# ---------------------------------------------------------------------------
# Quality floor — the codestral-writes-essays regression (live user report)
# ---------------------------------------------------------------------------

FLOOR_TABLE = {
    "models": {
        "fast/bad":   {"caps": {"tools": True}, "ctx": 128000, "speed": 99,
                       "scores": {"writing": 27}},   # codestral-shaped
        "slow/good":  {"caps": {"tools": True}, "ctx": 128000, "speed": 54,
                       "scores": {"writing": 100}},
        "mid/decent": {"caps": {"tools": True}, "ctx": 128000, "speed": 90,
                       "scores": {"writing": 84}},
    },
    "frontiers": {"writing": ["slow/good", "mid/decent", "fast/bad"]},
    "defaults": {"writing": "mid/decent"},
}
FLOOR_CATALOG = [
    {"id": "fast/bad",   "pricing": {"prompt_usd_per_1m": "0.3", "completion_usd_per_1m": "0.9"}},
    {"id": "slow/good",  "pricing": {"prompt_usd_per_1m": "5", "completion_usd_per_1m": "25"}},
    {"id": "mid/decent", "pricing": {"prompt_usd_per_1m": "0.6", "completion_usd_per_1m": "2.5"}},
]


def test_speed_max_never_picks_below_quality_floor():
    """weights choose among COMPETENT models — a 27/100 writer must lose the
    writing pick even at speed=0.8 (the exact live failure: codestral-2508,
    write=27 speed=99, picked for 'write an essay' and then refused)."""
    got = pick("writing", {"cost": 0.1, "cap": 0.1, "speed": 0.8},
               FLOOR_TABLE, FLOOR_CATALOG)
    assert got["model"] == "mid/decent"       # fastest of the qualified
    assert all(r["model"] != "fast/bad" or r["cap"] >= router.QUALITY_FLOOR
               for r in got["ranked"]) or "fast/bad" not in [r["model"] for r in got["ranked"]]


def test_floor_relaxes_when_nothing_qualifies():
    """If EVERY candidate is under the floor, still pick something (fail-open
    beats fail-closed) — the least-bad option by the user's weights."""
    table = {"models": {"only/weak": {"caps": {"tools": True}, "ctx": 9000,
                                      "speed": 80, "scores": {"writing": 30}}},
             "frontiers": {"writing": ["only/weak"]}, "defaults": {}}
    cat = [{"id": "only/weak",
            "pricing": {"prompt_usd_per_1m": "0.1", "completion_usd_per_1m": "0.2"}}]
    got = pick("writing", None, table, cat)
    assert got["model"] == "only/weak"


def test_bundled_table_frontiers_respect_the_floor():
    """The shipped table itself must not carry sub-floor frontier members."""
    table = router.load_table()
    if table is None:
        pytest.skip("no bundled table in this build")
    for cohort, mids in (table.get("frontiers") or {}).items():
        for mid in mids:
            q = table["models"][mid]["scores"].get(cohort)
            assert q is not None and q >= 30, f"{mid} on {cohort} frontier at {q}"


# ---------------------------------------------------------------------------
# Session blacklist (exclude) — models that failed live are unpickable
# ---------------------------------------------------------------------------

def test_exclude_removes_candidate_and_incumbent():
    got = pick("chat", {"cost": 0.05, "cap": 0.9, "speed": 0.05}, TABLE, CATALOG,
               exclude={"c/best"}, incumbent="c/best")
    assert got["model"] != "c/best"
    assert got["sticky"] is False


def test_exclude_blocks_the_default_fallback_too():
    got = pick("chat", None, TABLE, CATALOG, needs_ctx=10_000_000,
               exclude={"a/cheap"})     # a/cheap is the cohort default
    assert got is None                   # nothing safe left -> caller's pin rides


# ---------------------------------------------------------------------------
# Difficulty axis — crude marginal-gain proxy
# ---------------------------------------------------------------------------

from meshapi.router import difficulty_adjust, estimate_difficulty


@pytest.mark.parametrize("text, want", [
    ("what is 2+2?", "low"),
    ("write a haiku", "low"),
    ("fix the bug in this function please", "low"),
    ("design a distributed rate limiter with race condition handling, "
     "prove correctness, cover edge cases", "high"),
    ("refactor this module to be thread-safe and add comprehensive "
     "edge case tests", "high"),
])
def test_estimate_difficulty(text, want):
    assert estimate_difficulty(text) == want


def _axes(w):
    return {k: v for k, v in w.items() if k in ("cost", "cap", "speed")}


def test_difficulty_adjust_tilts_and_normalizes():
    base = {"cost": 0.5, "cap": 0.3, "speed": 0.2}
    hi = _axes(difficulty_adjust(base, "high"))
    lo = _axes(difficulty_adjust(base, "low"))
    mid = _axes(difficulty_adjust(base, "mid"))
    assert abs(sum(hi.values()) - 1) < 1e-9
    assert hi["cap"] > mid["cap"] > lo["cap"]
    assert lo["cost"] > mid["cost"] > hi["cost"]
    assert mid == pytest.approx(base)


def test_difficulty_changes_the_pick():
    """Same cohort, same base weights: hard prompt escalates, easy stays cheap."""
    lo_w = difficulty_adjust({"cost": 0.5, "cap": 0.3, "speed": 0.2}, "low")
    hi_w = difficulty_adjust({"cost": 0.5, "cap": 0.3, "speed": 0.2}, "high")
    lo_pick = pick("chat", lo_w, TABLE, CATALOG, needs_tools=True)["model"]
    hi_pick = pick("chat", hi_w, TABLE, CATALOG, needs_tools=True)["model"]
    assert lo_pick == "a/cheap"
    assert hi_pick == "c/best"


def test_extreme_cost_user_survives_hard_tilt():
    """cost=0.9 is a deliberate user choice — a hard prompt must not
    override it into a frontier-price model."""
    w = difficulty_adjust({"cost": 0.9, "cap": 0.05, "speed": 0.05}, "high")
    got = pick("chat", w, TABLE, CATALOG, needs_tools=True)
    assert got["model"] == "a/cheap"


# ---------------------------------------------------------------------------
# Effort levels — difficulty auto-detected or user-forced (final design)
# ---------------------------------------------------------------------------

from meshapi.router import EFFORT_LEVELS, effective_weights


def test_effort_cap_share_is_monotonic():
    base = {"cost": 0.5, "cap": 0.3, "speed": 0.2}
    caps = [effective_weights(base, lvl)["cap"]
            for lvl in ("low", "medium", "high", "xhigh", "max")]
    assert caps == sorted(caps)                 # strictly escalating depth
    assert caps[0] < 0.2 and caps[-1] > 0.95


def test_effort_medium_equals_mid_baseline():
    base = {"cost": 0.5, "cap": 0.3, "speed": 0.2}
    assert effective_weights(base, "medium") == pytest.approx(base)
    assert effective_weights(base, "mid") == pytest.approx(base)


def test_effort_levels_change_the_pick():
    base = {"cost": 0.5, "cap": 0.3, "speed": 0.2}
    lo = pick("chat", effective_weights(base, "low"), TABLE, CATALOG,
              needs_tools=True)["model"]
    mx = pick("chat", effective_weights(base, "max"), TABLE, CATALOG,
              needs_tools=True)["model"]
    assert lo == "a/cheap"
    assert mx == "c/best"


def test_effort_levels_constant_is_complete():
    assert EFFORT_LEVELS == ("auto", "low", "medium", "high", "xhigh", "max")
