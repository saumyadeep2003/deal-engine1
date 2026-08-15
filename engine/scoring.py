"""Layer 5 — scoring. Computed criteria (arithmetic, never a model) → optional
model judgment on survivors → percentile within (sector, stage) cohort.

The primary output is the cohort percentile, never a bare 0–100 score.
Every score stores its full feature vector + model/prompt versions so any
ranking can be reconstructed and defended.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from . import db
from .config import thesis

MODEL_VERSION_COMPUTED = "computed-v1"
PROMPT_VERSION = "score-p1"


def _days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def computed_features(company_id: int) -> dict:
    """All free/deterministic criteria. Null features carry a stated reason —
    an empty cell with a reason beats a plausible cell with none."""
    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    f: dict[str, dict] = {}

    tiers = db.q("""SELECT i.tier, COUNT(DISTINCT i.id) n FROM investments v
                    JOIN investors i ON v.investor_id=i.id
                    WHERE v.company_id=? GROUP BY i.tier""", (company_id,))
    tier_counts = {1: 0, 2: 0, 3: 0}
    for r in tiers:
        if r["tier"] in tier_counts:
            tier_counts[r["tier"]] = r["n"]
    f["tier1_count"] = {"value": tier_counts[1], "source": "investments (EDGAR/RSS derived)"}
    f["tier2_count"] = {"value": tier_counts[2], "source": "investments"}
    f["tier3_count"] = {"value": tier_counts[3], "source": "investments"}

    f["signal_velocity"] = {
        "value": db.q1("SELECT COUNT(*) c FROM signals WHERE company_id=? AND"
                       " observed_at >= datetime('now','-30 days')", (company_id,))["c"],
        "source": "signals, 30-day window"}
    f["source_diversity"] = {
        "value": db.q1("SELECT COUNT(DISTINCT source_id) c FROM signals WHERE company_id=?",
                       (company_id,))["c"], "source": "signals"}
    f["theme_match"] = {"value": 1.0 if c["sector"] else 0.0, "source": "deterministic keyword filter"}

    rounds = db.q("SELECT amount_usd, valuation_usd, announced_at, stage FROM funding_rounds"
                  " WHERE company_id=? ORDER BY announced_at DESC", (company_id,))
    last_amount = next((r["amount_usd"] for r in rounds if r["amount_usd"]), None)
    if last_amount:
        # sweet spot for a seed→growth fund: $1M–$100M raises score highest
        v = 1.0 if 1e6 <= last_amount <= 1e8 else (0.5 if last_amount < 1e6 else 0.3)
        f["offering_size_fit"] = {"value": v, "amount_usd": last_amount,
                                  "source": "funding_rounds (Form D / RSS)"}
    else:
        f["offering_size_fit"] = {"value": None, "reason": "no disclosed amount in free sources"}

    days = _days_since(c["last_signal_at"])
    f["recency"] = {"value": max(0.0, 1 - (days or 90) / 90), "days_since_signal": days,
                    "source": "signals"}

    # ---- licence-gated criteria: null with a stated reason, never estimated ----
    f["headcount"] = {"value": None, "reason": "requires Coresignal"}
    f["headcount_growth_6m"] = {"value": None, "reason": "requires Coresignal"}
    f["yoy_growth"] = {"value": None, "reason": "requires Coresignal (headcount proxy) or disclosed revenue"}
    f["valuation_usd"] = {"value": next((r["valuation_usd"] for r in rounds if r["valuation_usd"]), None),
                          "reason": None if any(r["valuation_usd"] for r in rounds)
                          else "requires PitchBook"}
    # runway needs burn (headcount × function mix) — state the assumption honestly
    f["runway_years"] = {"value": None,
                         "reason": "requires headcount (Coresignal) to estimate burn;"
                                   " assumption: fully-loaded $220k/head/yr"}
    ec = db.q1("SELECT value_json FROM enrichment_cache WHERE company_id=? AND field='github_stars'",
               (company_id,))
    if ec and ec["value_json"]:
        f["github_stars"] = {"value": json.loads(ec["value_json"]), "source": "GitHub API"}
    return f


def composite_from_features(f: dict, judged: dict | None = None) -> float:
    w = dict(thesis()["scoring"]["weights"])
    total_w, acc = 0.0, 0.0
    norm = {
        "tier1_count": lambda v: min(v / 4.0, 1.0),
        "signal_velocity": lambda v: min(v / 5.0, 1.0),
        "theme_match": lambda v: v,
        "offering_size_fit": lambda v: v,
        "recency": lambda v: v,
        "source_diversity": lambda v: min(v / 3.0, 1.0),
    }
    for key, weight in w.items():
        val = f.get(key, {}).get("value")
        if val is None:
            continue  # weights renormalise over available features
        acc += weight * norm[key](float(val))
        total_w += weight
    computed = acc / total_w if total_w else 0.0
    if judged:  # blended only when a REAL (non-stub) judgment exists
        jw = thesis()["scoring"]["judged_weights"]
        jt, ja = 0.0, 0.0
        for key, weight in jw.items():
            val = judged.get(key)
            if isinstance(val, (int, float)):
                ja += weight * (float(val) / 10.0)
                jt += weight
        if jt:
            return 0.6 * computed + 0.4 * (ja / jt)
    return computed


def score_all(judged_results: dict[int, dict] | None = None, verbose: bool = True) -> dict:
    """Score every pipeline company; convert composites to cohort percentiles."""
    cfg = thesis()["scoring"]
    floor = thesis()["filters"]["min_composite_floor"]
    companies = db.q("SELECT id, sector, stage FROM companies WHERE is_synthetic=0"
                     " AND status IN ('pipeline','hot','watchlist')")
    scored: list[dict] = []
    for c in companies:
        f = computed_features(c["id"])
        judged = (judged_results or {}).get(c["id"])
        comp = composite_from_features(f, judged)
        scored.append({"company_id": c["id"], "composite": comp,
                       "cohort_key": f"{c['sector'] or 'unclassified'}|{c['stage'] or 'unknown'}",
                       "features": f, "judged": judged})

    # cohort percentiles
    by_cohort: dict[str, list[dict]] = {}
    for s in scored:
        by_cohort.setdefault(s["cohort_key"], []).append(s)
    for cohort, members in by_cohort.items():
        members.sort(key=lambda s: s["composite"])
        n = len(members)
        for idx, s in enumerate(members):
            s["percentile"] = round(100.0 * (idx + 1) / n, 1) if n > 1 else 50.0
            s["cohort_size"] = n
            s["low_confidence"] = n < cfg["cohort_min_size"]

    # market rank within cohort by traction proxies (arithmetic, not a model)
    for cohort, members in by_cohort.items():
        ranked = sorted(members, key=lambda s: (
            -(s["features"]["signal_velocity"]["value"] or 0),
            -(s["features"].get("offering_size_fit", {}).get("amount_usd") or 0)))
        for pos, s in enumerate(ranked, start=1):
            db.execute("UPDATE companies SET market_rank=? WHERE id=?", (pos, s["company_id"]))

    from .filters import identity_corroborated
    demoted = 0
    for s in scored:
        if s["composite"] < floor:
            rec = "Pass"
        elif s["percentile"] >= cfg["recommendation_thresholds"]["deep_dive"]:
            rec = "Deep Dive"
        elif s["percentile"] >= cfg["recommendation_thresholds"]["watch"]:
            rec = "Watch"
        else:
            rec = "Pass"
        # A single-word name with no domain, no filing, no round and no founder is
        # a word that signals got attached to. Velocity of misattributed signals
        # was putting these at the TOP of the pipeline ("Text" and "VNET" were
        # top-picks on run 20) — with no site to profile, their briefs were empty,
        # at rank 1. Held at Watch, never deleted: a Deep Dive call also spends
        # Apollo credits and strong-model calls, and both belong to companies that
        # verifiably exist. The moment ONE anchor lands (domain resolves, filing
        # arrives, founder named), the cap lifts on its own. A partner's override
        # is stored separately and still outranks this, like every computed call.
        if rec == "Deep Dive" and not identity_corroborated(s["company_id"]):
            rec = "Watch"
            demoted += 1
            s["features"]["identity_confidence"] = {
                "value": 0.0,
                "reason": "single-word name with no validated domain, SEC filing, "
                          "funding round or named founder — held at Watch until any "
                          "one of those corroborates that the company exists"}
        prev = db.q1("SELECT human_override FROM scores WHERE company_id=?"
                     " ORDER BY scored_at DESC LIMIT 1", (s["company_id"],))
        db.insert("scores", {
            "company_id": s["company_id"], "composite": round(s["composite"], 4),
            "percentile": s["percentile"], "cohort_key": s["cohort_key"],
            "cohort_size": s["cohort_size"],
            "cohort_low_confidence": 1 if s["low_confidence"] else 0,
            "features_json": json.dumps({"computed": s["features"], "judged": s["judged"]}),
            "recommendation": rec,
            "human_override": prev["human_override"] if prev else None,
            "model_version": (s["judged"] or {}).get("model") or MODEL_VERSION_COMPUTED,
            "prompt_version": PROMPT_VERSION, "scored_at": db.now_iso()})
        status = {"Deep Dive": "hot", "Watch": "watchlist", "Pass": "pipeline"}[rec]
        db.execute("UPDATE companies SET status=? WHERE id=? AND status!='stale_review'",
                   (status, s["company_id"]))
    if verbose:
        recs = db.q("""SELECT recommendation, COUNT(*) n FROM scores s
                       WHERE s.id=(SELECT id FROM scores s2 WHERE s2.company_id=s.company_id
                                   ORDER BY scored_at DESC, id DESC LIMIT 1)
                       GROUP BY recommendation""")
        print(f"  scored {len(scored)} companies across {len(by_cohort)} cohorts: "
              + ", ".join(f"{r['recommendation']}={r['n']}" for r in recs)
              + (f" ({demoted} uncorroborated identit(ies) held at Watch)"
                 if demoted else ""))
    return {"scored": len(scored), "cohorts": {k: len(v) for k, v in by_cohort.items()}}


def maintain_staleness(verbose: bool = True) -> int:
    """90-day sweep: flag for partner review, NEVER delete. Includes the
    synthetic demo record so the mechanism is demonstrable (Demo Cases tab)."""
    import json as _json
    stale_days = thesis()["scoring"]["stale_days"]
    rows = db.q("""SELECT id, name, is_synthetic FROM companies
                   WHERE status IN ('pipeline','hot','watchlist')
                   AND last_signal_at IS NOT NULL
                   AND julianday('now') - julianday(last_signal_at) > ?""", (stale_days,))
    for r in rows:
        db.execute("UPDATE companies SET status='stale_review' WHERE id=?", (r["id"],))
        db.insert("review_queue", {
            "kind": "stale",
            "payload_json": _json.dumps({"company_id": r["id"], "name": r["name"],
                                         "is_synthetic": bool(r["is_synthetic"])}),
            "created_at": db.now_iso()})
    if verbose and rows:
        print(f"  staleness sweep: {len(rows)} flagged for partner review (0 deleted): "
              + ", ".join(r["name"] for r in rows))
    return len(rows)


def latest_scores(statuses: tuple = ("hot", "watchlist", "pipeline")) -> list[dict]:
    rows = db.q(f"""
        SELECT c.*, s.composite, s.percentile, s.cohort_key, s.cohort_size,
               s.cohort_low_confidence, s.recommendation, s.human_override, s.features_json,
               s.scored_at
        FROM companies c
        JOIN scores s ON s.company_id = c.id
        WHERE s.id = (SELECT id FROM scores WHERE company_id=c.id ORDER BY scored_at DESC, id DESC LIMIT 1)
          AND c.is_synthetic=0 AND c.status IN ({','.join('?' * len(statuses))})
        ORDER BY s.percentile DESC""", statuses)
    return [dict(r) for r in rows]


def apply_focus_split(candidates: list[dict], limit: int) -> list[dict]:
    """60/40 dominant-tech vs tactical — a portfolio constraint at the ranking
    layer, not a per-company score."""
    split = thesis()["fund"]["focus_split"]
    dom_themes = set(thesis()["dominant_tech_themes"])
    dom_cap = round(limit * split["dominant_tech"])
    out, n_dom = [], 0
    for c in sorted(candidates, key=lambda x: -(x.get("percentile") or 0)):
        is_dom = c.get("sector") in dom_themes
        if is_dom and n_dom >= dom_cap and len(out) < limit:
            others = [x for x in candidates if x.get("sector") not in dom_themes and x not in out]
            if others:
                continue
        if len(out) < limit:
            out.append(c)
            n_dom += 1 if is_dom else 0
    return out
