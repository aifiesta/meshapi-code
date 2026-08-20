"""Smart routing — a thin, table-driven model picker.

The CLI ships a routing table (opaque per-model scores per task cohort,
compiled elsewhere) and picks a concrete model locally in ~microseconds:
classify the prompt into a cohort → filter to feasible models → score the
cohort's candidate list with the user's weights (cost/capability/speed) →
pick. No classifier tokens are ever billed and no network hop is added —
the two costs the gateway's `model:"auto"` path pays on every request.

Everything here fails OPEN: no table, unknown cohort, empty candidates,
any exception → the caller keeps its pinned model or gateway auto-routing.
A routing layer must never be able to break a request.

The methodology that produces the table is deliberately not in this repo;
the table is data. Users steer with `/route weights cost=.5 cap=.3 speed=.2`
— weights choose a point along each cohort's efficiency frontier, so no
weight setting can select a strictly-dominated model.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

# Default weights: the user-facing contract. cap="auto" means the capability
# weight is DRIVEN BY PROMPT DIFFICULTY (easy prompts barely pay for
# capability, hard prompts pay a lot) — the user tunes only cost vs speed.
# An explicit numeric cap remains a power-user override.
DEFAULT_WEIGHTS = {"cost": 0.6, "cap": "auto", "speed": 0.4}
# capability share per difficulty band when cap="auto"
AUTO_CAP = {"low": 0.12, "mid": 0.35, "high": 0.78}
# Runtime quality floor: weights choose along the frontier of COMPETENT
# models. A candidate under this cohort score is dropped whenever anything
# better survives — no speed/cost setting may select a model the table says
# can't do the task (belt-and-braces with the compiler's own floor; also
# protects against stale bundled tables).
QUALITY_FLOOR = 45

_TABLE_CACHE: "dict | None" = None


# ---------------------------------------------------------------------------
# Table loading
# ---------------------------------------------------------------------------

def load_table(path: "str | None" = None) -> "dict | None":
    """Bundled table (ships with the package); None when absent/corrupt."""
    global _TABLE_CACHE
    if _TABLE_CACHE is not None and path is None:
        return _TABLE_CACHE
    try:
        p = Path(path) if path else Path(__file__).parent / "routing_table.json"
        table = json.loads(p.read_text())
        if not isinstance(table, dict) or "models" not in table:
            return None
        if path is None:
            _TABLE_CACHE = table
        return table
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cohort classification — local signals only, zero cost, deterministic
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(
    r"```|\bdef \w+\(|\bfunction\s+\w+\(|\bclass \w+|\bimport \w+|</?\w+>|"
    r"\btraceback\b|\bstack trace\b|\bcompil(?:e|er|ation)\b|\bsyntax error\b|"
    r"\b(?:refactor|debug|unit test|regex|bug|lint)\b|\.(?:py|js|ts|go|rs|java|c|cpp|rb|sh)\b",
    re.I)
_EXTRACT_RE = re.compile(
    r"\b(?:extract|parse|json|csv|structured|schema|fields?|scrape)\b", re.I)
_MATH_RE = re.compile(
    r"\b(?:solve|prove|calculate|equation|integral|derivative|probability|theorem)\b|"
    r"[∑∫√π]|\b\d+\s*[+\-*/^]\s*\d+\b", re.I)
# "writing" = prose artifacts. A bare "write ..." (write a file, write a
# script) is NOT prose — it needs the artifact noun nearby, else it falls
# through to code/agentic/chat where it belongs.
_WRITE_RE = re.compile(
    r"\b(?:blog|essay|story|poem|caption|newsletter|tagline|slogan|headline"
    r"|article|tweet|linkedin post|cover letter|micro-?story|blurb)\b"
    r"|\b(?:write|draft|compose|rewrite)\b.{0,40}\b(?:blog|essay|story|poem|post"
    r"|email|copy|article|bio|speech|lyrics)\b", re.I)
_RESEARCH_RE = re.compile(
    r"\b(?:latest|news|current|today|recent|search the web|cite|sources?|202\d)\b", re.I)
_CLASSIFY_RE = re.compile(
    r"\b(?:classify|categorize|label|sentiment|triage|route this|yes or no)\b", re.I)


def classify(text: str, *, has_image: bool = False, has_tools: bool = False,
             history_chars: int = 0) -> tuple:
    """(cohort, confidence 0-1). Order matters: strongest signals first."""
    t = text or ""
    if has_image:
        return "vision", 0.95
    if history_chars > 120_000 or len(t) > 60_000:
        return "long-context", 0.9
    if _CODE_RE.search(t):
        # in a tool-calling session, code work IS agentic work
        return ("agentic" if has_tools else "coding"), 0.8
    if _EXTRACT_RE.search(t):
        return "extraction", 0.7
    if _MATH_RE.search(t):
        return "reasoning-math", 0.7
    if _CLASSIFY_RE.search(t):
        return "cheap-bulk", 0.7
    if _RESEARCH_RE.search(t):
        return "web-research", 0.6
    if _WRITE_RE.search(t):
        return "writing", 0.6
    if has_tools:
        return "agentic", 0.5
    return "chat", 0.4


# ---------------------------------------------------------------------------
# Difficulty — a crude marginal-gain proxy
# ---------------------------------------------------------------------------
# The most consistent routing-research finding (RouteLLM -> 2026): predict
# whether the expensive model is WORTH IT for this prompt, not just what kind
# of prompt it is. v1 is deliberately crude — deterministic surface signals
# mapped to a weight adjustment — but it breaks the "easy and hard prompts
# get identical picks" tie, which is the industry's biggest measured gap
# (RouterArena: every router under-uses cheap models on easy prompts).

_HARD_RE = re.compile(
    r"\b(?:architecture|design(?:\s+a|\s+an|\s+the)?|refactor|optimi[sz]e|prove|"
    r"derive|multi-?step|end-?to-?end|production|concurren\w*|distributed|"
    r"race condition|deadlock|migrate|benchmark|edge cases?|trade-?offs?|"
    r"scalab\w+|formal|rigorous|comprehensive|in depth|thorough)\b", re.I)
_EASY_RE = re.compile(
    r"^(?:what(?:'s| is| are)?|who|when|where|define|meaning of|translate|"
    r"convert|list|name)\b", re.I)
_CONSTRAINT_RE = re.compile(
    r"\b(?:must|should|ensure|exactly|at least|no more than|without using|"
    r"step[- ]by[- ]step)\b", re.I)


def estimate_difficulty(text: str) -> str:
    """"low" | "mid" | "high" from surface signals. Deterministic."""
    t = (text or "").strip()
    score = 0
    if len(t) > 400:
        score += 1
    if len(t) > 1200:
        score += 1
    score += min(3, len(_HARD_RE.findall(t)))
    if len(_CONSTRAINT_RE.findall(t)) >= 3:
        score += 1
    if t.count("```") >= 2:
        score += 1
    if _EASY_RE.search(t) and len(t) < 90:
        score -= 2
    elif len(t) < 40:
        score -= 1
    if score <= 0:
        return "low"
    return "high" if score >= 3 else "mid"


def difficulty_adjust(weights: "dict | None", difficulty: str) -> dict:
    """Tilt the user's weights by predicted difficulty, then renormalize.

    Easy prompt: capability matters less, cost more — route DOWN with
    confidence. Hard prompt: capability dominates — the marginal gain of a
    frontier model is real. The user's weights stay the baseline; this is a
    per-prompt tilt, never a persisted change.
    """
    w = normalize_weights(weights)
    if w.get("cap") == "auto":
        return effective_weights(w, difficulty)
    if difficulty == "low":
        w = {"cost": w["cost"] * 1.5, "cap": w["cap"] * 0.55,
             "speed": w["speed"] * 1.2}
    elif difficulty == "high":
        # Strong tilt: the cost axis spans the full 0-100 normalized range,
        # so a timid cap multiplier can't overcome a cheap model's built-in
        # ~100-point cost advantage. x3.0 makes capability decisive at
        # default weights while a deliberate cost=0.9 user still routes
        # cheap — their stated preference survives the tilt.
        w = {"cost": w["cost"] * 0.35, "cap": w["cap"] * 3.0,
             "speed": w["speed"] * 0.75}
    return normalize_weights(w)


# ---------------------------------------------------------------------------
# Picking
# ---------------------------------------------------------------------------

def _blend_price(row: dict, cohort: str) -> "float | None":
    p = (row or {}).get("pricing") or {}
    try:
        i = float(p.get("prompt_usd_per_1m") or 0)
        o = float(p.get("completion_usd_per_1m") or 0)
    except (TypeError, ValueError):
        return None
    if i == 0 and o == 0:
        return None
    wi = 0.85 if cohort in ("agentic", "coding", "long-context") else 0.75
    return wi * i + (1 - wi) * o


def normalize_weights(w: "dict | None") -> dict:
    """Normalize to sum 1. cap="auto" is preserved as a mode flag; the
    cost/speed pair is normalized over its own share."""
    out = dict(DEFAULT_WEIGHTS)
    for k in out:
        raw = (w or {}).get(k, out[k])
        if k == "cap" and (raw is None or str(raw).lower() == "auto"):
            out[k] = "auto"
            continue
        try:
            out[k] = max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    if out["cap"] == "auto":
        pair = out["cost"] + out["speed"] or 1.0
        return {"cost": out["cost"] / pair, "cap": "auto",
                "speed": out["speed"] / pair}
    total = sum(out.values()) or 1.0
    return {k: v / total for k, v in out.items()}


def effective_weights(w: "dict | None", difficulty: str) -> dict:
    """The weights a pick actually uses.

    cap="auto" (default): difficulty OWNS the capability share (AUTO_CAP),
    and the user's cost/speed ratio splits the remainder — "capability" as
    a knob is replaced by difficulty detection.
    Numeric cap (power user): the classic tilt (difficulty_adjust) applies.
    """
    w = normalize_weights(w)
    if w.get("cap") == "auto":
        cap = AUTO_CAP.get(difficulty, AUTO_CAP["mid"])
        rest = 1.0 - cap
        return {"cost": rest * w["cost"], "cap": cap, "speed": rest * w["speed"]}
    return difficulty_adjust(w, difficulty)


def pick(cohort: str, weights: "dict | None", table: "dict | None",
         catalog: "list | None", *, needs_tools: bool = False,
         needs_ctx: int = 0, incumbent: "str | None" = None,
         exclude: "set | None" = None) -> "dict | None":
    """Choose a model for `cohort`. Returns {model, cohort, ranked, sticky}
    or None (caller falls back). Pure — no I/O, no mutation."""
    try:
        if not table or not catalog:
            return None
        frontier = (table.get("frontiers") or {}).get(cohort) or []
        models = table.get("models") or {}
        cat_by_id = {m.get("id"): m for m in catalog}
        w = normalize_weights(weights)
        if w.get("cap") == "auto":
            # Caller didn't resolve difficulty (direct pick() use) — assume a
            # mid-difficulty prompt so "auto" always scores numerically.
            w = effective_weights(w, "mid")

        exclude = exclude or set()
        if incumbent in exclude:
            incumbent = None
        cands = []
        for mid in frontier:
            if mid in exclude:
                continue  # session-blacklisted: it already failed us live
            row = models.get(mid)
            cat = cat_by_id.get(mid)
            if not row or not cat:
                continue  # table/catalog drift — skip, never crash
            caps = row.get("caps") or {}
            if needs_tools and not caps.get("tools"):
                continue
            if needs_ctx and (row.get("ctx") or 0) < needs_ctx:
                continue
            q = (row.get("scores") or {}).get(cohort)
            cost = _blend_price(cat, cohort)
            if q is None or cost is None:
                continue
            cands.append((mid, float(q), cost, float(row.get("speed") or 30)))
        qualified = [c for c in cands if c[1] >= QUALITY_FLOOR]
        if qualified:
            cands = qualified
        if not cands:
            default = (table.get("defaults") or {}).get(cohort)
            if not default or default in exclude:
                return None
            return {"model": default, "cohort": cohort, "ranked": [],
                    "sticky": False}

        max_cost = max(c for _, _, c, _ in cands)
        denom = math.log10(max_cost + 1) or 1.0

        def score(t):
            _, q, cost, sp = t
            cost_n = 100 * (1 - math.log10(cost + 1) / denom)
            return w["cost"] * cost_n + w["cap"] * q + w["speed"] * sp

        ranked = sorted(cands, key=score, reverse=True)
        top = [t[0] for t in ranked[:3]]
        # Session stickiness: keep the incumbent while it's still top-3 —
        # mid-conversation model flapping costs prompt-cache hits and sanity.
        if incumbent and incumbent in top:
            return {"model": incumbent, "cohort": cohort, "sticky": True,
                    "ranked": _ranked_view(ranked, score)}
        return {"model": ranked[0][0], "cohort": cohort, "sticky": False,
                "ranked": _ranked_view(ranked, score)}
    except Exception:
        return None  # fail open, always


def _ranked_view(ranked: list, score) -> list:
    return [{"model": t[0], "score": round(score(t), 1), "cap": round(t[1]),
             "cost": round(t[2], 2), "speed": round(t[3])} for t in ranked[:3]]
