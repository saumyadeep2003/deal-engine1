"""How much of the pipeline has actually been analysed, and what is holding the rest back.

Every stage of this engine is capped — deliberately, because each one costs
either money or somebody's rate limit. But a cap that is invisible is
indistinguishable from a bug. "Only ten companies have an AI assessment" reads
as broken; "ten of a hundred and sixty, because JUDGE_TOP_N is ten and each
search advances it by ten" reads as a dial, and a dial can be turned.

So this module answers one question per stage: how many of the companies that
COULD have this actually do, and what is the name of the thing limiting it. No
stage is allowed to report a bare number — a fraction without its denominator
and its cap is the kind of statistic that ends an interview badly.
"""
from __future__ import annotations

import os

from . import db

ELIGIBLE = "is_synthetic=0 AND status IN ('pipeline','hot','watchlist')"


def _count(sql: str, params: tuple = ()) -> int:
    # sqlite3.Row has no .get(), psycopg dict_row does — index access works on both
    row = db.q1(sql, params)
    try:
        return int(row["c"]) if row is not None and row["c"] is not None else 0
    except (KeyError, IndexError, TypeError):
        return 0


def _eligible_distinct(table: str, extra: str = "") -> str:
    """Distinct companies in `table` that are still in the pipeline.

    Scoping matters: evidence rows outlive the companies they belong to (a company
    can be filtered out after its signals were stored), so an unscoped count
    produced "43 of 36 = 119%" — a number that discredits every other row in the
    table next to it."""
    return (f"SELECT COUNT(DISTINCT t.company_id) c FROM {table} t "
            f"JOIN companies co ON co.id=t.company_id "
            f"WHERE co.is_synthetic=0 AND co.status IN ('pipeline','hot','watchlist')"
            + (f" AND t.{extra}" if extra else ""))


def _stage(name: str, have: int, eligible: int, cap: str, meaning: str,
           blocked_by: str | None = None) -> dict:
    return {"stage": name, "have": have, "eligible": eligible,
            "pct": round(100 * have / eligible, 1) if eligible else 0.0,
            "cap": cap, "meaning": meaning, "blocked_by": blocked_by,
            "complete": eligible > 0 and have >= eligible}


def report() -> dict:
    from .config import models_config, thesis
    eligible = _count(f"SELECT COUNT(*) c FROM companies WHERE {ELIGIBLE}")
    total = _count("SELECT COUNT(*) c FROM companies WHERE is_synthetic=0")

    # --- AI assessment ------------------------------------------------------
    # A judgement lives in scores.features_json under "judged". Counting rows
    # where that key holds something real is the only honest measure: a score row
    # exists for every company, judged or not.
    judged = _count("""SELECT COUNT(*) c FROM companies co
                       WHERE co.is_synthetic=0 AND co.status IN ('pipeline','hot','watchlist')
                       AND EXISTS (SELECT 1 FROM scores s WHERE s.company_id=co.id
                                   AND s.features_json LIKE '%thesis_narrative%')""")
    judge_n = os.environ.get("JUDGE_TOP_N", "10")

    # --- briefs -------------------------------------------------------------
    briefs = _count(f"""SELECT COUNT(DISTINCT b.company_id) c FROM briefs b
                        JOIN companies co ON co.id=b.company_id
                        WHERE b.validated=1 AND co.is_synthetic=0""")
    thr = thesis()["scoring"]["brief_auto_threshold_percentile"]
    above_thr = _count("""SELECT COUNT(*) c FROM companies co JOIN scores s ON s.company_id=co.id
                          WHERE s.id=(SELECT id FROM scores WHERE company_id=co.id
                                      ORDER BY scored_at DESC, id DESC LIMIT 1)
                          AND s.percentile >= ? AND co.is_synthetic=0
                          AND co.status IN ('pipeline','hot','watchlist')""", (thr,))

    # --- hiring -------------------------------------------------------------
    hiring = _count(_eligible_distinct("signals", "kind='hiring'"))

    # --- other evidence -----------------------------------------------------
    commentary = _count(_eligible_distinct("commentary"))
    enriched = _count(_eligible_distinct("enrichment_cache"))
    founders = _count(_eligible_distinct("founders"))
    rounds = _count(_eligible_distinct("funding_rounds"))
    news_total = _count("SELECT COUNT(*) c FROM news_items")
    news_why = _count("SELECT COUNT(*) c FROM news_items WHERE why_it_matters IS NOT NULL")

    stages = [
        _stage("Found and resolved", total, total, "none",
               "every company the sources surfaced, de-duplicated into one record"),
        _stage("Survived the thesis filter", eligible, total, "config/thesis.yaml themes",
               "deterministic keyword + stage + geography rules — free, no model"),
        _stage("Funding round on record", rounds, eligible,
               "what SEC/RSS/Apify disclosed",
               "a round we can point at a filing or an article for",
               "PitchBook would fill the rest"),
        _stage("AI assessment written", judged, eligible, f"JUDGE_TOP_N={judge_n} per search",
               "founder quality, moat, TAM, thesis fit — the expensive step",
               f"raise JUDGE_TOP_N, or run more searches: each one now judges up to "
               f"{judge_n} companies that do not yet have an assessment"),
        _stage("Hiring signal captured", hiring, eligible,
               "ats_boards max_companies + whether the company uses a public board",
               "open roles and function mix from the company's own job board",
               "many startups use an ATS we do not read, or none at all"),
        _stage("Full brief published", briefs, eligible,
               f"auto only above the {thr}th percentile, {models_config()['limits']['max_briefs_per_day']}/day",
               "the one-pager a partner reads",
               f"{above_thr} companies currently clear the {thr}th-percentile bar; any "
               "other company gets one written the moment its link is opened"),
        _stage("Founders identified", founders, eligible, "SEC Form D related persons",
               "named people behind the company",
               "free sources name founders only when a filing does"),
        _stage("Public discussion captured", commentary, eligible, "HN + Reddit only",
               "what engineers and investors said in public",
               "X, Blind, podcasts and Substack all require licences"),
        _stage("Extra detail gathered", enriched, eligible, "post-filter survivors only",
               "GitHub activity, pricing, customer logos, careers mix"),
    ]

    return {
        "stages": stages,
        "news": {"items": news_total, "with_rationale": news_why,
                 "cap": "top 15 by deterministic relevance get a model-written line",
                 "meaning": "every item is scored for relevance without a model; only the "
                            "top slice gets the one-line 'why it matters'"},
        "headline": _headline(stages),
        "generated_at": db.now_iso(),
    }


def _headline(stages: list[dict]) -> str:
    weakest = min((s for s in stages if s["eligible"]), key=lambda s: s["pct"], default=None)
    if not weakest:
        return "Nothing in the pipeline yet — run a search."
    return (f"Thinnest coverage: {weakest['stage']} at {weakest['pct']}% "
            f"({weakest['have']} of {weakest['eligible']}). Limited by {weakest['cap']}.")
