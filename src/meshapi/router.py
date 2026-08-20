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

# Default weights: the user-facing contract. Must sum to ~1 (renormalized).
DEFAULT_WEIGHTS = {"cost": 0.5, "cap": 0.3, "speed": 0.2}

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
    out = dict(DEFAULT_WEIGHTS)
    for k in out:
        try:
            v = float((w or {}).get(k, out[k]))
            out[k] = max(0.0, v)
        except (TypeError, ValueError):
            pass
    total = sum(out.values()) or 1.0
    return {k: v / total for k, v in out.items()}


def pick(cohort: str, weights: "dict | None", table: "dict | None",
         catalog: "list | None", *, needs_tools: bool = False,
         needs_ctx: int = 0, incumbent: "str | None" = None) -> "dict | None":
    """Choose a model for `cohort`. Returns {model, cohort, ranked, sticky}
    or None (caller falls back). Pure — no I/O, no mutation."""
    try:
        if not table or not catalog:
            return None
        frontier = (table.get("frontiers") or {}).get(cohort) or []
        models = table.get("models") or {}
        cat_by_id = {m.get("id"): m for m in catalog}
        w = normalize_weights(weights)

        cands = []
        for mid in frontier:
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
        if not cands:
            default = (table.get("defaults") or {}).get(cohort)
            return {"model": default, "cohort": cohort, "ranked": [],
                    "sticky": False} if default else None

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
