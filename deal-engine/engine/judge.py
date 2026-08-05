"""Judged scoring — model judgment on filter survivors only, tier-routed.

Funnel: deterministic filter (free) → cheap classifier (flash tier) →
structured judged scoring (mid tier) → briefs (flagship tier, ~8/day).
Models never do arithmetic; they return labelled judgments with reasoning,
or null. Stub mode returns None — judgment fields render as [STUB], and
composites fall back to computed-only.
"""
from __future__ import annotations
import json
import os
from typing import Optional

from pydantic import BaseModel

from . import db, llm
from .config import models_config, thesis


# How many of the top-ranked survivors get the full AI assessment. Everything
# below the cut still gets complete deterministic scoring, ranking and provenance
# — the model is the expensive part, not the pipeline.
JUDGE_TOP_N = int(os.environ.get("JUDGE_TOP_N", "10"))


class Classification(BaseModel):
    is_venture_relevant: Optional[bool] = None
    reason: Optional[str] = None


class TamEstimate(BaseModel):
    value_usd: Optional[float] = None
    assumptions: Optional[list[str]] = None
    confidence: Optional[str] = None      # low | medium | high


class JudgedScore(BaseModel):
    founder_quality: Optional[float] = None       # 0-10
    founder_reasoning: Optional[str] = None
    moat: Optional[float] = None                  # 0-10
    moat_reasoning: Optional[str] = None
    tam: Optional[TamEstimate] = None
    meta_thesis_fit: Optional[float] = None       # 0-10
    meta_thesis_reasoning: Optional[str] = None
    exit_horizon_years: Optional[float] = None
    thesis_narrative: Optional[str] = None        # the paragraph a partner reads


class ScreenedJudgement(JudgedScore):
    """Screening + judgment in ONE response. Two calls per company meant 50 model
    round-trips per search; the screening question ('is this an operating company
    or a fund/SPV?') is answered from the same context the judgment needs, so
    asking twice bought nothing but latency."""
    is_venture_relevant: Optional[bool] = None
    screening_reason: Optional[str] = None


def _context(company_id: int, max_signals: int = 12) -> str:
    """Real retrieved context only — every line carries its signal id."""
    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    lines = [f"Company: {c['name']} | sector: {c['sub_sector'] or c['sector'] or 'unknown'}"
             f" | HQ: {c['hq'] or 'unknown'} | stage: {c['stage'] or 'unknown'}"]
    for s in db.q("SELECT id, kind, observed_at, url, payload_json, raw FROM signals"
                  " WHERE company_id=? ORDER BY observed_at DESC LIMIT ?",
                  (company_id, max_signals)):
        p = json.loads(s["payload_json"])
        compact = {k: v for k, v in p.items() if v not in (None, [], "", {})}
        lines.append(f"[S:{s['id']}] {s['kind']} @ {s['observed_at'][:10]}"
                     f" {s['url'] or ''} :: {json.dumps(compact)[:500]}")
    for f in db.q("SELECT name, prior_exits, frontier_lab_alum, notes FROM founders"
                  " WHERE company_id=?", (company_id,)):
        lines.append(f"[founder] {f['name']} prior_exits={f['prior_exits']}"
                     f" frontier_lab={f['frontier_lab_alum']} {f['notes'] or ''}")
    return "\n".join(lines)


def classify_company(company_id: int) -> bool | None:
    """Flash-tier binary gate. Returns None when stubbed (nobody removed)."""
    if llm.stubbed():
        return None
    res = llm.complete_json(
        "classify",
        "You are screening deal-sourcing signals for a deep-tech/AI venture fund. "
        "Decide only whether this entity is a venture-investable operating company "
        "(not a fund, SPV, holding vehicle, public company or hobby project).",
        _context(company_id, max_signals=5), Classification, tier="classify")
    return res.is_venture_relevant if res else None


def judge_company(company_id: int) -> dict | None:
    """Mid-tier structured judgment. Null when stubbed or unparseable."""
    res = llm.complete_json(
        "score",
        "You are a venture analyst. Judge ONLY from the provided signals: founder "
        "quality (prior exits, prior company calibre, frontier-lab background, domain "
        "fit, team completeness), moat/defensibility, TAM (with explicit assumptions "
        "and a confidence level — never a bare number), fit with the meta-thesis "
        f"({thesis()['fund']['meta_thesis']}), plausible exit horizon in years, and a "
        "short thesis narrative saying why this could be one of the best deals "
        "available right now (founder, market, investors already in, traction, entry "
        "valuation). Cite signal ids [S:n] for every factual claim.",
        _context(company_id), JudgedScore, tier="score")
    if res is None:
        return None
    out = res.model_dump()
    out["model"] = models_config()["tiers"]["score"]
    return out


JUDGE_PROMPT = (
    "You are a venture analyst at a deep-tech/AI fund. Do TWO things in one answer.\n"
    "(1) SCREEN: is this a venture-investable operating company — not a fund, SPV, "
    "holding vehicle, public company or hobby project? Set is_venture_relevant and "
    "screening_reason. If it is false, leave every other field null and stop.\n"
    "(2) JUDGE (only if relevant): founder quality (prior exits, prior company calibre, "
    "frontier-lab background, domain fit, team completeness), moat/defensibility, TAM "
    "(with explicit assumptions and a confidence level — never a bare number), fit with "
    "the meta-thesis ({meta}), plausible exit horizon in years, and a short thesis "
    "narrative saying why this could be one of the best deals available right now "
    "(founder, market, investors already in, traction, entry valuation). "
    "Cite signal ids [S:n] for every factual claim. Be concise."
)


def assess_company(company_id: int) -> dict | None:
    """One call: screen + judge. Returns the judged dict, or {'is_venture_relevant':
    False} for a rejected entity, or None when stubbed/unparseable."""
    res = llm.complete_json("score", JUDGE_PROMPT.format(meta=thesis()["fund"]["meta_thesis"]),
                            _context(company_id), ScreenedJudgement, tier="score")
    if res is None:
        return None
    out = res.model_dump()
    out["model"] = models_config()["tiers"]["score"]
    return out


# ---- reuse: a company with no new signals cannot have a new judgment ---------

def _signal_fingerprint(company_id: int) -> str:
    """Cheap identity of a company's evidence: newest signal id + count. If neither
    moved since the last judgment, re-asking the model would burn a call to receive
    the same answer."""
    r = db.q1("SELECT COUNT(*) n, COALESCE(MAX(id), 0) mx FROM signals WHERE company_id=?",
              (company_id,))
    return f"{r['n']}:{r['mx']}"


def _cached_judgement(company_id: int) -> dict | None:
    row = db.q1("""SELECT features_json FROM scores WHERE company_id=?
                   ORDER BY scored_at DESC, id DESC LIMIT 1""", (company_id,))
    if not row or not row["features_json"]:
        return None
    try:
        feats = json.loads(row["features_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    judged = feats.get("judged")
    if not judged or not isinstance(judged, dict):
        return None
    if judged.get("evidence_fingerprint") != _signal_fingerprint(company_id):
        return None
    return judged


def run_judged_scoring(max_companies: int | None = None, verbose: bool = True,
                       progress_cb=None) -> dict[int, dict]:
    """Judge the top survivors by computed composite. Returns {company_id: judged}.

    progress_cb(i, n, company_name) lets the search runner surface live
    "assessing X (7 of 10)" progress in the dashboard.

    Three things keep this step from dominating a search: only the top N by
    computed composite are judged at all, each takes ONE model call rather than
    two, and a company whose evidence has not changed since its last judgment
    reuses that judgment instead of paying for an identical answer."""
    from .scoring import composite_from_features, computed_features
    cap = max_companies if max_companies is not None else JUDGE_TOP_N
    rows = db.q("SELECT id FROM companies WHERE is_synthetic=0 AND status IN"
                " ('pipeline','hot','watchlist')")
    ranked = sorted(((r["id"], composite_from_features(computed_features(r["id"])))
                     for r in rows), key=lambda x: -x[1])[:cap]
    results: dict[int, dict] = {}
    reused = 0
    stub = llm.stubbed()
    for i, (cid, _) in enumerate(ranked, start=1):
        if stub:
            break
        if progress_cb:
            row = db.q1("SELECT name FROM companies WHERE id=?", (cid,))
            try:
                progress_cb(i, len(ranked), row["name"] if row else f"#{cid}")
            except Exception:  # noqa: BLE001 — progress must never break judging
                pass
        cached = _cached_judgement(cid)
        if cached is not None:
            reused += 1
            if cached.get("is_venture_relevant") is False:
                db.execute("UPDATE companies SET status='filtered' WHERE id=?", (cid,))
                continue
            results[cid] = cached
            continue
        judged = assess_company(cid)
        if not judged:
            continue
        judged["evidence_fingerprint"] = _signal_fingerprint(cid)
        if judged.get("is_venture_relevant") is False:
            db.execute("UPDATE companies SET status='filtered' WHERE id=?", (cid,))
            continue
        results[cid] = judged
    if verbose:
        mode = f"STUB (no {llm.api_key_env_name()} — computed-only scoring, judgment fields read [STUB])" \
            if stub else (f"judged {len(results)}/{len(ranked)} survivors"
                          f" ({reused} reused unchanged, "
                          f"{len(ranked) - reused} model call(s))")
        print(f"  judged scoring: {mode}")
    return results
