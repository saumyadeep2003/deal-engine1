"""The deal engine as an MCP server — the hybrid architecture, testable for free.

The engine keeps being the system of record. Postgres holds immutable signals,
Python does the deterministic filtering and every piece of arithmetic, and the
gatekeeper resolves every model-written sentence against a stored row. None of
that moves. This file only adds a second doorway onto the same house, so a Claude
conversation can ask the engine questions instead of a partner reading a dashboard.

Two design decisions worth stating, because they are what make this useful rather
than a REST mirror with different punctuation:

**Tools are shaped like questions, not like endpoints.** `investor_activity` and
`thesis_scan` are things a partner asks. `GET /api/peers` is a thing a programmer
calls. A model choosing between twelve verbs it understands picks correctly far
more often than one choosing between thirty routes it has to compose.

**Every answer carries its provenance and its gaps.** A tool that returns "12
companies" invites a confident summary of nothing. These return the figure, the
denominator, the source url and the reason a field is empty — so a conversation
built on top of them inherits the same discipline as the briefs, rather than
being the one surface where it leaks.

Nothing here imports the FastAPI app. The hosted service is untouched by design:
if this file were deleted, the deployment would not notice.

Run:  python mcp_server.py          (stdio, for Claude Desktop)
Reads DATABASE_URL exactly like the rest of the engine — point it at Supabase to
talk to the live pipeline, or leave it unset for the local SQLite copy.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from engine import db  # noqa: E402

INSTRUCTIONS = """
Thirdbase deal engine. A live venture pipeline built from SEC filings, news,
research, code and job boards, with provenance on every figure.

Read this before answering from these tools:

- Every number here traces to a stored signal. When you quote one, quote its
  source url too — the engine's own briefs do, and a chat answer that doesn't is
  the weakest link in the chain.
- Fields that read "requires PitchBook" or "requires Coresignal" are licence
  gaps, not failures, and not invitations to estimate. Say the field is unavailable.
- `coverage_report` tells you what fraction of the pipeline has been analysed.
  Check it before making a claim about "the companies" — most tools return the
  analysed subset, not the whole market.
- Rankings are percentiles WITHIN a (sector, stage) cohort, never absolute scores.
  A cohort under 20 members is flagged low-confidence and should be quoted that way.
- Do not invent a company, an investor or a round that these tools did not return.
"""

mcp = FastMCP("thirdbase-deal-engine", instructions=INSTRUCTIONS)


def _connect() -> None:
    db.connect()


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in db.q(sql, params)]


def _backend_note() -> str:
    info = db.backend_info()
    return (f"{info.get('backend')} ({'durable' if info.get('durable') else 'local file'})")


# --------------------------------------------------------------- discovery --

@mcp.tool()
def pipeline_search(sector: str = "", stage: str = "", recommendation: str = "",
                    query: str = "", limit: int = 20) -> str:
    """Find companies in the pipeline, ranked by their percentile within their own
    (sector, stage) cohort.

    All arguments are optional filters. `query` matches the company name and
    description. `recommendation` is one of Pass, Watch, Deep Dive.

    Returns the ranked list with each company's rank, cohort, last round, tier-1
    investor count and last signal date."""
    _connect()
    where = ["c.is_synthetic=0", "c.status IN ('pipeline','hot','watchlist')"]
    params: list = []
    if sector:
        where.append("(LOWER(c.sector) LIKE ? OR LOWER(c.sub_sector) LIKE ?)")
        params += [f"%{sector.lower()}%", f"%{sector.lower()}%"]
    if stage:
        where.append("LOWER(c.stage) LIKE ?")
        params.append(f"%{stage.lower()}%")
    if query:
        where.append("(LOWER(c.name) LIKE ? OR LOWER(COALESCE(c.description,'')) LIKE ?)")
        params += [f"%{query.lower()}%", f"%{query.lower()}%"]
    sql = f"""SELECT c.id, c.name, c.description, c.sector, c.sub_sector, c.stage, c.hq,
                     c.last_signal_at, s.percentile, s.cohort_size, s.cohort_key,
                     s.cohort_low_confidence,
                     COALESCE(s.human_override, s.recommendation) rec
              FROM companies c JOIN scores s ON s.company_id=c.id
              WHERE s.id=(SELECT id FROM scores WHERE company_id=c.id
                          ORDER BY scored_at DESC, id DESC LIMIT 1)
              AND {' AND '.join(where)}
              ORDER BY s.percentile DESC LIMIT ?"""
    params.append(min(limit, 100))
    rows = _rows(sql, tuple(params))
    if recommendation:
        rows = [r for r in rows if (r.get("rec") or "").lower() == recommendation.lower()]
    out = []
    for r in rows:
        t1 = db.q1("""SELECT COUNT(*) c FROM investments v JOIN investors i
                      ON i.id=v.investor_id WHERE v.company_id=? AND i.tier=1""",
                   (r["id"],))
        rnd = db.q1("""SELECT amount_usd, stage, announced_at FROM funding_rounds
                       WHERE company_id=? ORDER BY announced_at DESC LIMIT 1""", (r["id"],))
        out.append({
            "company_id": r["id"], "name": r["name"],
            "what_they_do": r["description"] or "not established — website not read yet",
            "sector": r["sub_sector"] or r["sector"], "stage": r["stage"] or "unknown",
            "hq": r["hq"], "recommendation": r["rec"],
            "rank": (f"{r['percentile']:.0f}th percentile of {r['cohort_size']} "
                     f"in {r['cohort_key']}"
                     + (" — LOW CONFIDENCE, cohort under 20" if r["cohort_low_confidence"] else "")),
            "last_round": (f"${rnd['amount_usd'] / 1e6:.1f}M {rnd['stage'] or ''}"
                           f" on {(rnd['announced_at'] or '')[:10]}"
                           if rnd and rnd["amount_usd"] else
                           "none disclosed in free sources"),
            "tier1_investors": t1["c"] if t1 else 0,
            "last_signal": (r["last_signal_at"] or "")[:10],
        })
    return json.dumps({"matched": len(out), "companies": out,
                       "note": "Ranked within cohort, not absolutely. Use coverage_report "
                               "to see what fraction of the pipeline has been analysed.",
                       "storage": _backend_note()}, indent=1, default=str)


@mcp.tool()
def company_brief(company_id: int, write_if_missing: bool = True) -> str:
    """The full intelligence brief for one company: what they do, funding, cap
    table quality, team, traction, cohort rank, commentary, the fund's nine
    criteria, and a Pass/Watch/Deep Dive call.

    Every figure carries a [S:signal_id] citation or a [computed] tag. If no brief
    exists yet, one is written on the spot from stored evidence unless
    write_if_missing is false."""
    _connect()
    row = db.q1("""SELECT content_md, generated_at, recommendation FROM briefs
                   WHERE company_id=? AND validated=1
                   ORDER BY generated_at DESC LIMIT 1""", (company_id,))
    if not row and write_if_missing:
        from engine import llm
        from engine.briefs import generate_brief
        from engine.judge import assess_company
        judged = None if llm.stubbed() else assess_company(company_id)
        generate_brief(company_id, "mcp_request", judged, verbose=False)
        row = db.q1("""SELECT content_md, generated_at, recommendation FROM briefs
                       WHERE company_id=? AND validated=1
                       ORDER BY generated_at DESC LIMIT 1""", (company_id,))
    if not row:
        return json.dumps({"error": "no brief and none could be written",
                           "hint": "call company_evidence for the raw stored signals"})
    return json.dumps({"company_id": company_id,
                       "written_at": db.to_display(row["generated_at"]),
                       "recommendation": row["recommendation"],
                       "brief_markdown": row["content_md"]}, indent=1, default=str)


@mcp.tool()
def company_evidence(company_id: int, limit: int = 25) -> str:
    """Every stored signal for one company, with source urls and dates.

    This is the provenance layer: use it when asked to prove a claim, or when a
    brief says something a partner wants to check. Nothing here is model-written."""
    _connect()
    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    if not c:
        return json.dumps({"error": f"no company {company_id}"})
    sigs = _rows("""SELECT id, kind, observed_at, url, payload_json, fetch_mode
                    FROM signals WHERE company_id=?
                    ORDER BY observed_at DESC LIMIT ?""", (company_id, min(limit, 60)))
    for s in sigs:
        try:
            p = json.loads(s.pop("payload_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            p = {}
        s["detail"] = {k: v for k, v in p.items() if v not in (None, "", [], {})}
    from engine import hiring, people
    return json.dumps({
        "company": {"id": c["id"], "name": c["name"], "domain": c["domain"],
                    "sector": c["sub_sector"] or c["sector"], "hq": c["hq"],
                    "what_they_do": c["description"]},
        "team": people.team(company_id) or "no founders named in any filing we hold",
        "hiring": hiring.hiring(company_id),
        "investors": _rows("""SELECT i.name, i.tier FROM investments v
                              JOIN investors i ON i.id=v.investor_id
                              WHERE v.company_id=?""", (company_id,)),
        "rounds": _rows("""SELECT stage, amount_usd, valuation_usd, announced_at
                           FROM funding_rounds WHERE company_id=?
                           ORDER BY announced_at DESC""", (company_id,)),
        "signals": sigs,
    }, indent=1, default=str)


@mcp.tool()
def thesis_scan(description: str, limit: int = 10) -> str:
    """Describe a thesis or sector in plain prose and get the most relevant
    companies the engine holds, ranked by term overlap weighted by cohort rank.

    Example: "robotics for warehouse automation with a hardware moat"."""
    _connect()
    from engine.sectors import scan_thesis
    hits = scan_thesis(description, limit=min(limit, 30))
    return json.dumps({"thesis": description, "matches": len(hits), "results": hits,
                       "note": "Matched against stored descriptions and signals only — "
                               "a company absent here is absent from the pipeline, not "
                               "from the market."}, indent=1, default=str)


# ------------------------------------------------------------------ trends --

@mcp.tool()
def emerging_sectors(limit: int = 8) -> str:
    """Sub-sectors where technical signal is running ahead of mainstream coverage,
    with the evidence behind each and the best companies found inside them.

    `ratio` is technical velocity divided by mainstream consensus. A cluster whose
    consensus could not be measured carries ratio 0 and is NOT an emerging-sector
    claim — it is volume without a comparison, and the thesis text says so."""
    _connect()
    rows = _rows("""SELECT label, ratio, signal_velocity, consensus_volume,
                           source_diversity, talent_flow, is_contrarian, thesis_md,
                           terms_json, companies_json, evidence_json, detected_at
                    FROM sectors_emerging
                    ORDER BY ratio DESC, signal_velocity DESC, detected_at DESC
                    LIMIT ?""", (min(limit, 25),))
    out = []
    for r in rows:
        out.append({
            "sector": r["label"], "ratio": r["ratio"],
            "technical_velocity": r["signal_velocity"],
            "mainstream_coverage": r["consensus_volume"],
            "distinct_sources": r["source_diversity"],
            "talent_signals": r["talent_flow"],
            "contrarian": bool(r["is_contrarian"]),
            "reading": r["thesis_md"],
            "defining_terms": json.loads(r["terms_json"] or "[]"),
            "companies_in_it": json.loads(r["companies_json"] or "[]"),
            "evidence": json.loads(r["evidence_json"] or "[]")[:4],
            "detected": (r["detected_at"] or "")[:10],
        })
    return json.dumps({"clusters": out}, indent=1, default=str)


@mcp.tool()
def investor_activity(investor: str = "", days: int = 90, limit: int = 25) -> str:
    """What the tracked peer firms are doing: new investments, fund formations,
    and moves outside a firm's stated focus (flagged as thesis shifts).

    Pass `investor` to filter to one firm. This answers "who is investing in this
    space" and "which firms consistently co-invest"."""
    _connect()
    where, params = ["1=1"], []
    if investor:
        where.append("LOWER(i.name) LIKE ?")
        params.append(f"%{investor.lower()}%")
    events = _rows(f"""SELECT i.name investor, i.tier, pe.event_type, pe.is_thesis_shift,
                              pe.observed_at, c.name company, s.url
                       FROM peer_events pe JOIN investors i ON i.id=pe.investor_id
                       LEFT JOIN companies c ON c.id=pe.company_id
                       LEFT JOIN signals s ON s.id=pe.source_signal_id
                       WHERE {' AND '.join(where)}
                       ORDER BY pe.observed_at DESC LIMIT ?""",
                    tuple(params + [min(limit, 100)]))
    pairs = _rows("""SELECT i1.name a, i2.name b, COUNT(*) together
                     FROM investments v1 JOIN investments v2
                       ON v1.company_id=v2.company_id AND v1.investor_id < v2.investor_id
                     JOIN investors i1 ON i1.id=v1.investor_id
                     JOIN investors i2 ON i2.id=v2.investor_id
                     GROUP BY i1.name, i2.name HAVING COUNT(*) > 1
                     ORDER BY together DESC LIMIT 15""")
    return json.dumps({
        "events": events, "co_investor_pairs": pairs,
        "note": "Derived from SEC Form D fund formations and observed rounds in free "
                "sources. A deal-database licence (Crunchbase/PitchBook) would make "
                "this dense; today it is sparse and that sparsity is real, not a bug.",
    }, indent=1, default=str)


# -------------------------------------------------------------- qualitative --

@mcp.tool()
def commentary(company_id: int = 0, company_name: str = "", limit: int = 15) -> str:
    """What investors, operators and engineers have said publicly about a company —
    real quotes with source urls and dates.

    Sentiment labels are model judgements; the quotes themselves are verbatim."""
    _connect()
    if not company_id and company_name:
        row = db.q1("SELECT id FROM companies WHERE LOWER(name) LIKE ? LIMIT 1",
                    (f"%{company_name.lower()}%",))
        company_id = row["id"] if row else 0
    if not company_id:
        return json.dumps({"error": "give a company_id or a company_name that matches"})
    rows = _rows("""SELECT platform, author, sentiment, quote, url, observed_at
                    FROM commentary WHERE company_id=?
                    ORDER BY observed_at DESC LIMIT ?""", (company_id, min(limit, 50)))
    return json.dumps({
        "company_id": company_id, "quotes": rows,
        "coverage_note": "Free sources are Hacker News and Reddit. X, Blind, podcast "
                         "transcripts and Substack all require paid licences, so an "
                         "empty result means 'not found in free sources', never "
                         "'nobody is talking about them'.",
    }, indent=1, default=str)


@mcp.tool()
def news_worth_reading(limit: int = 10) -> str:
    """Curated news, essays and analysis, each with a one-line reason it matters to
    the fund. Relevance is scored deterministically; the rationale is model-written
    and checked back against the article text."""
    _connect()
    rows = _rows("""SELECT title, url, source, published_at, why_it_matters, relevance_score
                    FROM news_items WHERE why_it_matters IS NOT NULL
                    ORDER BY relevance_score DESC, published_at DESC LIMIT ?""",
                 (min(limit, 40),))
    return json.dumps({"items": rows}, indent=1, default=str)


# ----------------------------------------------------------------- posture --

@mcp.tool()
def coverage_report() -> str:
    """How much of the pipeline has actually been analysed, stage by stage, and the
    named setting limiting each one.

    Check this before generalising. "The companies in the pipeline" and "the
    companies with an AI assessment" are usually very different sets, and the
    difference is a cap, not a finding."""
    _connect()
    from engine import coverage
    return json.dumps(coverage.report(), indent=1, default=str)


@mcp.tool()
def engine_status() -> str:
    """Which build is running, whether the model provider is answering, which data
    sources are live or licence-gated, and what the gatekeeper has blocked."""
    _connect()
    from engine import gatekeeper, llm, version
    sources = _rows("""SELECT name, health, requires_license, license_vendor, last_ok_at,
                              error_count FROM sources
                       WHERE name NOT LIKE 'demo_%' AND name != 'derived_events'
                       ORDER BY requires_license, name""")
    return json.dumps({
        "build": version.info(),
        "storage": db.backend_info(),
        "model": {"stubbed": llm.stubbed(), "circuit_open": llm.circuit_open(),
                  "last_error": llm.last_error()},
        "sources": sources,
        "gatekeeper": gatekeeper.stats(),
    }, indent=1, default=str)


# ------------------------------------------------------------------ writes --

@mcp.tool()
def record_decision(company_id: int, action: str, note: str = "",
                    partner: str = "partner") -> str:
    """Record a partner's Pass / Watch / Deep Dive call.

    The human value always wins over the model's recommendation and the
    disagreement is logged against the feature vector that produced it — that log
    is what any future recalibration would learn from."""
    _connect()
    if action not in ("Pass", "Watch", "Deep Dive"):
        return json.dumps({"error": "action must be Pass, Watch or Deep Dive"})
    score = db.q1("""SELECT id, recommendation, human_override, composite, features_json
                     FROM scores WHERE company_id=?
                     ORDER BY scored_at DESC, id DESC LIMIT 1""", (company_id,))
    if not score:
        return json.dumps({"error": "company has no score yet"})
    was = score["human_override"] or score["recommendation"]
    db.execute("UPDATE scores SET human_override=? WHERE id=?", (action, score["id"]))
    db.insert("partner_actions", {
        "company_id": company_id, "partner": partner, "action": "override",
        "score_at_time": score["composite"],
        "features_at_time_json": score["features_json"],
        "note": note or f"via Claude: {was} -> {action}", "created_at": db.now_iso()})
    db.execute("UPDATE companies SET status=? WHERE id=? AND status!='stale_review'",
               ({"Deep Dive": "hot", "Watch": "watchlist", "Pass": "pipeline"}[action],
                company_id))
    return json.dumps({"ok": True, "company_id": company_id, "was": was, "now": action,
                       "logged_to": "partner_actions"})


@mcp.tool()
def start_search(full: bool = True) -> str:
    """Start a new search across every live source. Returns immediately with a run
    id; the run takes 15-30 minutes.

    Poll `search_progress` rather than waiting. Prefer answering from what the
    engine already holds unless the partner explicitly wants fresh data."""
    _connect()
    from engine import runner
    run_id = runner.start(kind="full" if full else "quick", trigger_by="claude_mcp")
    if run_id is None:
        return json.dumps({"ok": False, "reason": "a search is already running"})
    return json.dumps({"ok": True, "run_id": run_id,
                       "note": "poll search_progress; typically 15-30 minutes"})


@mcp.tool()
def search_progress() -> str:
    """Step-by-step progress of the search that is running now, or the result of the
    last one if none is running.

    Use this after start_search rather than waiting: a run takes 15-30 minutes and
    each step reports what it found as it finishes, so partial progress is
    answerable long before the run is."""
    _connect()
    run = db.q1("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    if not run:
        return json.dumps({"note": "no search has been run yet"})
    steps = _rows("""SELECT key, label, status, detail, seconds, items,
                            started_at, finished_at
                     FROM run_steps WHERE run_id=? ORDER BY seq, id""", (run["id"],))
    done = sum(1 for s in steps if s["status"] in ("done", "failed"))
    return json.dumps({
        "run": dict(run), "progress": f"{done}/{len(steps)} steps",
        "still_running": run["status"] == "running", "steps": steps,
    }, indent=1, default=str)


if __name__ == "__main__":
    # stdio: the transport Claude Desktop speaks. No port, no inbound network, no
    # new hosting — which is the whole point of testing the hybrid this way.
    if os.environ.get("DEAL_ENGINE_MCP_SELFTEST"):
        _connect()
        print(f"MCP server OK — storage: {_backend_note()}", file=sys.stderr)
        sys.exit(0)
    mcp.run()
