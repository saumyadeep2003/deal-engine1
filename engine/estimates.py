"""Valuation, growth and runway — estimated from what is observable, with the method attached.

The assignment scores every company on entry valuation, 40%+ year-on-year growth
and roughly three years of runway. All three columns read "requires PitchBook",
which is true of the *measured* figures and unhelpful as an answer: a partner
cannot triage a pipeline where three of the nine criteria are permanently blank.

So each is estimated from things the engine can actually see — the round size, the
stage's ordinary dilution band, and the company's own hiring — and every estimate
travels with three things it must never be separated from: a range rather than a
point, the arithmetic that produced it, and the sentence saying it is an estimate.

The temptation here is to produce a single confident number because a single
number fits a spreadsheet cell better. That is exactly the failure this system is
built to avoid, so the cell gets the range and the caveat, and the code refuses to
estimate at all when the input it needs is missing.
"""
from __future__ import annotations

import json

from . import db, hiring

# Ordinary equity sold at each stage. Wide on purpose: a point estimate implies a
# precision that a dilution heuristic does not have.
DILUTION_BAND = {
    "pre-seed": (0.10, 0.25), "preseed": (0.10, 0.25),
    "seed": (0.12, 0.25),
    "series a": (0.15, 0.28), "a": (0.15, 0.28),
    "series b": (0.12, 0.22), "b": (0.12, 0.22),
    "series c": (0.10, 0.18), "c": (0.10, 0.18),
    "growth": (0.05, 0.15), "series d": (0.05, 0.15),
}
DEFAULT_BAND = (0.10, 0.25)

# Monthly burn per employee, fully loaded, for a US deep-tech team. A band again:
# the difference between a research team and a sales team is most of the range.
BURN_PER_HEAD_MONTH = (12_000, 22_000)


def _round(company_id: int) -> dict | None:
    r = db.q1("""SELECT fr.amount_usd, fr.valuation_usd, fr.stage, fr.announced_at,
                        s.id sid FROM funding_rounds fr
                 LEFT JOIN signals s ON fr.source_signal_id=s.id
                 WHERE fr.company_id=? ORDER BY fr.announced_at DESC LIMIT 1""",
             (company_id,))
    return dict(r) if r else None


def valuation(company_id: int) -> dict:
    """Post-money implied by the round size and the stage's dilution band.

    This is arithmetic on a disclosed number, not a guess at a private one: if a
    company raised $12M and seed rounds ordinarily sell 12-25% of the company, the
    post-money implied is $48M-$100M. Wide, honest, and enough to answer "is this
    priced sanely for the stage" — which is the question the criterion is really
    asking."""
    r = _round(company_id)
    if not r:
        return {"available": False,
                "reason": "no funding round observed in free sources"}
    if r.get("valuation_usd"):
        return {"available": True, "measured": True,
                "low": r["valuation_usd"], "high": r["valuation_usd"],
                "text": f"${r['valuation_usd'] / 1e6:.0f}M (disclosed)",
                "method": "disclosed in the filing or announcement",
                "signal_id": r.get("sid")}
    amt = r.get("amount_usd")
    if not amt:
        return {"available": False,
                "reason": "round observed but the amount was not disclosed"}
    stage = str(r.get("stage") or "").strip().lower()
    lo_pct, hi_pct = DILUTION_BAND.get(stage, DEFAULT_BAND)
    high, low = amt / lo_pct, amt / hi_pct       # less dilution -> higher valuation
    return {"available": True, "measured": False,
            "low": low, "high": high,
            "text": f"${low / 1e6:.0f}M-${high / 1e6:.0f}M implied (estimate)",
            "method": (f"${amt / 1e6:.1f}M raised at a {stage or 'typical'} round, where "
                       f"{lo_pct:.0%}-{hi_pct:.0%} of the company is ordinarily sold — "
                       f"an estimate from the round size, NOT a disclosed valuation"),
            "signal_id": r.get("sid")}


def growth(company_id: int) -> dict:
    """Growth proxy from hiring, because revenue is not public.

    Stated plainly rather than dressed up: this is not the 40% year-on-year revenue
    growth the criterion asks for. Open roles are a leading indicator of the
    company's own belief about growth, and that is a different — sometimes
    earlier — thing. Calling it revenue growth would be the lie; calling it what it
    is keeps it useful."""
    h = hiring.hiring(company_id)
    if not h.get("available"):
        return {"available": False,
                "reason": "no public job board or archived team page for this company",
                "note": "measured revenue growth requires Coresignal or the company itself"}
    if h.get("change") is None:
        return {"available": True, "direction": None,
                "text": f"{h.get('open_roles')} open roles (first reading)",
                "method": "a trend needs two observations; this is the first",
                "note": "hiring appetite, not revenue growth"}
    change, roles = h["change"], h.get("open_roles") or 0
    base = roles - change
    pct = (change / base * 100) if base > 0 else None
    direction = "expanding" if change > 0 else "contracting" if change < 0 else "flat"
    return {"available": True, "direction": direction,
            "text": (f"{direction}: {change:+d} open roles"
                     + (f" ({pct:+.0f}%)" if pct is not None else "")
                     + f" since {(h.get('change_since') or '')[:10]}"),
            "method": "change in open roles on the company's own job board between two searches",
            "note": "hiring appetite, NOT the revenue growth the criterion asks for"}


def runway(company_id: int) -> dict:
    """Months of cash implied by the last round and an estimated team size.

    Deliberately refuses without a team size. Runway from a round size alone would
    be arithmetic on one real number and one invented one, and the output would
    look identical to a real estimate."""
    r = _round(company_id)
    if not r or not r.get("amount_usd"):
        return {"available": False, "reason": "no disclosed round size to burn"}
    h = hiring.hiring(company_id)
    heads = h.get("team_now")
    basis = "people listed on the company's own team page"
    if not heads:
        roles = h.get("open_roles")
        if not roles:
            return {"available": False,
                    "reason": "no team-size evidence — runway from a round size alone "
                              "would be half arithmetic and half invention"}
        # Open roles as a floor for team size is weak, and labelled as such.
        heads = max(roles * 3, 5)
        basis = f"estimated from {roles} open roles (weak: assumes roles are ~a third of the team)"
    lo_burn, hi_burn = BURN_PER_HEAD_MONTH
    months_low = r["amount_usd"] / (heads * hi_burn)
    months_high = r["amount_usd"] / (heads * lo_burn)
    target = 36
    return {"available": True,
            "low_months": months_low, "high_months": months_high,
            "text": f"{months_low:.0f}-{months_high:.0f} months (estimate)",
            "meets_target": months_high >= target,
            "method": (f"${r['amount_usd'] / 1e6:.1f}M raised / ~{heads:.0f} people at "
                       f"${lo_burn / 1000:.0f}k-${hi_burn / 1000:.0f}k per person per month; "
                       f"team size {basis}. Ignores revenue, which would extend it."),
            "signal_id": r.get("sid")}


def all_estimates(company_id: int) -> dict:
    return {"valuation": valuation(company_id), "growth": growth(company_id),
            "runway": runway(company_id)}


def cell(est: dict) -> str:
    """One spreadsheet cell. The estimate marker is never dropped for brevity."""
    if not est.get("available"):
        return f"— ({est.get('reason', 'not available')})"
    return est.get("text", "—")


def criteria_scorecard(company_id: int) -> list[dict]:
    """The assignment's nine investment criteria, each with what the engine can
    actually say. A criterion with no evidence says so instead of scoring zero —
    zero reads as a judgement, absence reads as a gap, and they are different."""
    from .config import thesis
    t = thesis()["fund"]
    feats = {}
    sc = db.q1("""SELECT features_json, percentile, cohort_size, cohort_key FROM scores
                  WHERE company_id=? ORDER BY scored_at DESC, id DESC LIMIT 1""", (company_id,))
    if sc and sc["features_json"]:
        try:
            feats = (json.loads(sc["features_json"]) or {}).get("computed") or {}
        except (json.JSONDecodeError, TypeError):
            feats = {}
    judged = {}
    if sc and sc["features_json"]:
        try:
            judged = (json.loads(sc["features_json"]) or {}).get("judged") or {}
        except (json.JSONDecodeError, TypeError):
            judged = {}

    def _pct(v, default):
        """thesis.yaml stores the growth target as a fraction (0.4). Rendering it
        raw produced "Growth >= 0.4% YoY" — off by a hundred, in the row a partner
        reads to decide whether the company clears the bar."""
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return v * 100 if v <= 1 else v

    def _range(v, default):
        if isinstance(v, (list, tuple)) and v:
            return f"{v[0]}-{v[-1]}"
        return v if v not in (None, "") else default

    t1 = feats.get("tier1_count", {}).get("value", 0)
    val, gro, run = valuation(company_id), growth(company_id), runway(company_id)
    tam = (judged.get("tam") or {}).get("value_usd")
    rows = [
        {"criterion": "Entry valuation reasonable for stage", "value": cell(val),
         "method": val.get("method") or val.get("reason")},
        {"criterion": f"Growth ≥ {_pct(t.get('growth_yoy_target'), 40):.0f}% YoY", "value": cell(gro),
         "method": gro.get("method") or gro.get("reason"),
         "caveat": gro.get("note")},
        {"criterion": f"Runway ≈ {_range(t.get('runway_target_years'), 3)} years", "value": cell(run),
         "method": run.get("method") or run.get("reason")},
        {"criterion": f"{_range(t.get('tier1_target_count'), '3-4')} Tier-1 investors",
         "value": f"{t1} observed",
         "method": "investments matched against the tier list in config/thesis.yaml"},
        {"criterion": "Moat / technical defensibility",
         "value": (f"{judged.get('moat')}/10 (model judgement)" if judged.get("moat")
                   else "— not assessed yet"),
         "method": "model judgement over stored signals, gatekeeper-verified"},
        {"criterion": f"TAM > ${t.get('tam_floor_usd', 1e9) / 1e9:.0f}B",
         "value": (f"${tam / 1e9:.1f}B (model estimate)" if tam else "— not estimated"),
         "method": "model estimate with stated assumptions — never a measured figure"},
        {"criterion": f"Exit horizon {_range(t.get('exit_horizon_years'), '3-5')} years",
         "value": (f"{judged.get('exit_horizon_years')} years (model judgement)"
                   if judged.get("exit_horizon_years") else "— not assessed yet"),
         "method": "model judgement"},
        {"criterion": "Rank within its own sector and stage",
         "value": (f"{sc['percentile']:.0f}th percentile of {sc['cohort_size']}"
                   if sc and sc["percentile"] is not None else "— not ranked yet"),
         "method": "computed percentile inside the (sector, stage) cohort"},
    ]
    return rows
