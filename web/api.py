"""FastAPI web layer — partner-facing dashboard + JSON API.

Read-mostly by design: the pipeline's source of truth stays the SQLite DB and
the scheduled jobs. The three write endpoints are the ones a partner genuinely
needs — record a Pass/Watch/Deep Dive decision (feedback loop), request a brief,
and trigger a refresh.

Every response carries provenance: urls and dates, never a bare number.
Bound to 127.0.0.1 by default — this is fund data on a laptop, not a public site.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import db, llm, scoring  # noqa: E402
from engine.config import OUTPUT_DIR, ROOT, thesis  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Thirdbase Deal Engine", docs_url="/api/docs",
              description="Deal sourcing & discovery engine — local partner interface.")

# ---- light abuse damping for the hosted, open demo -------------------------
# Not authentication (the demo is deliberately open); just a global budget so a
# crawler cannot drain the LLM quota or hammer refresh. Limits are far above
# anything a human interviewer would hit; when exceeded the endpoint answers
# honestly instead of pretending to work.
import time as _time

_BUDGETS: dict[str, list] = {}          # name -> [window_start_epoch, count]
_LIMITS = {"chat": (3600, 60), "scan": (3600, 120), "refresh": (3600, 6),
           "brief": (3600, 12), "decision": (3600, 60), "digest_send": (3600, 6),
           "llm_test": (3600, 20)}


def _within_budget(name: str) -> bool:
    window, cap = _LIMITS[name]
    now = _time.time()
    slot = _BUDGETS.setdefault(name, [now, 0])
    if now - slot[0] > window:
        slot[0], slot[1] = now, 0
    slot[1] += 1
    return slot[1] <= cap


def _budget_or_429(name: str) -> None:
    if not _within_budget(name):
        raise HTTPException(429, f"hosted-demo rate limit for '{name}' reached — this open "
                                 "demo caps expensive operations per hour; try again later "
                                 "or run the engine locally (see the repo README)")


# --------------------------------------------------------------------------- UI

@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse((STATIC / "dashboard.html").read_text())


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "time": db.now_iso()}


# ------------------------------------------------------------------------- data

@app.get("/api/summary")
def summary() -> dict:
    """Funnel counts + stub/licence posture. Everything a KPI row needs."""
    from outputs import email_send, gsheets
    q1 = db.q1
    raw = q1("SELECT COUNT(*) c FROM signals WHERE fetch_mode!='synthetic_demo'")["c"]
    companies = q1("SELECT COUNT(*) c FROM companies WHERE is_synthetic=0")["c"]
    surviving = q1("""SELECT COUNT(*) c FROM companies WHERE is_synthetic=0
                      AND status IN ('pipeline','hot','watchlist')""")["c"]
    # Signal-level survival is the cost model the funnel is built on: how much
    # raw volume never reaches an enrichment or model call.
    surviving_signals = q1("""SELECT COUNT(*) c FROM signals s
                              JOIN companies c2 ON s.company_id=c2.id
                              WHERE c2.status IN ('pipeline','hot','watchlist')
                              AND c2.is_synthetic=0""")["c"]
    recs = {r["rec"]: r["n"] for r in db.q("""
        SELECT COALESCE(s.human_override, s.recommendation) rec, COUNT(*) n
        FROM companies c JOIN scores s ON s.company_id=c.id
        WHERE s.id=(SELECT id FROM scores WHERE company_id=c.id
                    ORDER BY scored_at DESC, id DESC LIMIT 1)
        AND c.is_synthetic=0 GROUP BY rec""")}
    briefs = q1("SELECT COUNT(*) c FROM briefs WHERE validated=1")["c"]
    tokens = db.q("""SELECT stage, model, COUNT(*) calls, SUM(prompt_tokens) pt,
                     SUM(completion_tokens) ct, SUM(stubbed) stubbed
                     FROM llm_usage GROUP BY stage, model ORDER BY stage""")
    sources = db.q("""SELECT so.name, so.health, so.requires_license, so.license_vendor,
                      so.interval_minutes, so.last_ok_at, so.error_count,
                      COUNT(s.id) signals,
                      SUM(CASE WHEN s.fetch_mode='cached_snapshot' THEN 1 ELSE 0 END) cached
                      FROM sources so LEFT JOIN signals s ON s.source_id=so.id
                      WHERE so.name NOT LIKE 'demo_%' AND so.name != 'derived_events'
                      GROUP BY so.id ORDER BY so.requires_license, so.name""")
    return {
        "funnel": [
            {"stage": "Raw signals ingested", "value": raw},
            {"stage": "Companies resolved", "value": companies},
            {"stage": "Survived deterministic filter", "value": surviving},
            {"stage": "Watch or better", "value": recs.get("Watch", 0) + recs.get("Deep Dive", 0)},
            {"stage": "Deep Dive", "value": recs.get("Deep Dive", 0)},
        ],
        "recommendations": {k: recs.get(k, 0) for k in ("Deep Dive", "Watch", "Pass")},
        "briefs": briefs,
        "signals_filtered_pct": round(100 * (1 - surviving_signals / raw), 1) if raw else 0,
        "companies_filtered_pct": round(100 * (1 - surviving / companies), 1) if companies else 0,
        "surviving_signals": surviving_signals,
        "llm": {"stubbed": llm.stubbed(),
                "key_env": llm.api_key_env_name(),
                "circuit_open": llm.circuit_open(),
                "last_error": llm.last_error(),
                "usage": [dict(r) for r in tokens]},
        "sources": _with_connection_info([dict(r) for r in sources]),
        "email": email_send.status(),
        "sheets": gsheets.status(),
        "job": _job_compat(),
        "search_mode": _search_mode(),
        "storage": db.backend_info(),
        "display_tz": db.display_tz(),
        "generated_at": db.now_iso(),
    }


def _search_mode() -> str:
    import os
    return (os.environ.get("SEARCH_MODE") or "manual").lower()


# What each paid source would unlock, so "switched off" is never a mystery.
UNLOCKS = {
    "pitchbook": "full funding history, valuations, complete cap tables",
    "crunchbase": "funding rounds and investor lists beyond SEC filings",
    "harmonic": "company + founder graph, early-stage coverage",
    "dealroom": "European coverage and funding data",
    "coresignal": "headcount and 6-month hiring growth (feeds runway estimates)",
    "x_gp_watchlist": "what tracked GPs are posting — the earliest sector signal",
    "blind": "employee sentiment at target companies",
    "podcasts": "investor commentary from podcast transcripts",
    "substack_threads": "investor newsletter commentary",
    "the_information": "scoop-level reporting on rounds and hires",
}


def _with_connection_info(rows: list[dict]) -> list[dict]:
    """Attach the env var that switches each source on, and what it unlocks —
    the dashboard should tell you how to connect a source, not just that it is off."""
    from engine.config import sources_config
    cfg = {s["name"]: s for s in sources_config()["sources"]}
    for r in rows:
        c = cfg.get(r["name"], {})
        r["env_key"] = c.get("env_key")
        r["env_key_set"] = bool(c.get("env_key") and os.environ.get(c["env_key"]))
        r["unlocks"] = UNLOCKS.get(r["name"])
    return rows


def _job_compat() -> dict:
    """runner state in the shape older clients/tests expect."""
    from engine import runner
    cur = runner.current()
    last_rows = runner.history(limit=1)
    last = None
    if last_rows:
        r = last_rows[0]
        last = {"kind": r["kind"], "finished": r["started_at"], "seconds": r["seconds"],
                "returncode": 0 if r["status"] == "done" else 1, "stats": r["stats"]}
    return {"running": ({"kind": cur["kind"], "started": cur["started_at"],
                         "run_id": cur["id"]} if cur else None),
            "last": last}


@app.get("/api/pipeline")
def pipeline(status: str = Query("all"), sector: str = Query("all"),
             q: str = Query("")) -> dict:
    rows = scoring.latest_scores(("hot", "watchlist", "pipeline", "stale_review"))
    out = []
    for c in rows:
        rec = c.get("human_override") or c.get("recommendation")
        if status != "all" and rec != status:
            continue
        if sector != "all" and (c.get("sector") or "unclassified") != sector:
            continue
        if q and q.lower() not in (c["name"] or "").lower():
            continue
        feats = json.loads(c["features_json"])["computed"]
        rnd = db.q1("""SELECT fr.amount_usd, fr.valuation_usd, fr.announced_at, fr.stage,
                              i.name lead, s.url
                       FROM funding_rounds fr LEFT JOIN investors i ON fr.lead_investor_id=i.id
                       LEFT JOIN signals s ON fr.source_signal_id=s.id
                       WHERE fr.company_id=? ORDER BY fr.announced_at DESC LIMIT 1""", (c["id"],))
        src = db.q1("""SELECT url, observed_at FROM signals WHERE company_id=?
                       AND url IS NOT NULL ORDER BY observed_at DESC LIMIT 1""", (c["id"],))
        brief = db.q1("""SELECT id FROM briefs WHERE company_id=? AND validated=1
                         ORDER BY generated_at DESC LIMIT 1""", (c["id"],))
        comm = db.q1("SELECT COUNT(*) n FROM commentary WHERE company_id=?", (c["id"],))
        out.append({
            "id": c["id"], "name": c["name"],
            "description": c["description"] or "—",
            "sector": c["sector"] or "unclassified",
            "sector_label": c["sub_sector"] or c["sector"] or "unclassified",
            "stage": (rnd["stage"] if rnd and rnd["stage"] else c["stage"]) or "unknown",
            "hq": c["hq"] or "—",
            "last_round_usd": rnd["amount_usd"] if rnd else None,
            "last_round_date": (rnd["announced_at"] or "")[:10] if rnd else None,
            "valuation": rnd["valuation_usd"] if rnd and rnd["valuation_usd"] else None,
            "valuation_note": None if (rnd and rnd["valuation_usd"]) else "requires PitchBook",
            "lead_investor": rnd["lead"] if rnd and rnd["lead"] else None,
            "tier1": feats.get("tier1_count", {}).get("value", 0),
            "tier2": feats.get("tier2_count", {}).get("value", 0),
            "tier3": feats.get("tier3_count", {}).get("value", 0),
            "headcount_note": "requires Coresignal",
            "growth_note": "requires Coresignal",
            "percentile": c["percentile"], "cohort": c["cohort_key"],
            "cohort_size": c["cohort_size"],
            "low_confidence": bool(c["cohort_low_confidence"]),
            "market_rank": c["market_rank"],
            "recommendation": rec,
            "human_override": c.get("human_override"),
            "last_signal": (c["last_signal_at"] or "")[:10],
            "source_url": src["url"] if src else None,
            "has_brief": bool(brief), "commentary_count": comm["n"] if comm else 0,
            "status": c["status"],
        })
    return {"count": len(out), "rows": out}


@app.get("/api/sectors")
def sectors() -> dict:
    from engine.events import talent_flow_summary
    rows = []
    for s in db.q("SELECT * FROM sectors_emerging ORDER BY ratio DESC"):
        ev = json.loads(s["evidence_json"] or "[]")
        rows.append({"label": s["label"], "ratio": s["ratio"],
                     "signal_velocity": s["signal_velocity"],
                     "consensus_volume": s["consensus_volume"],
                     "source_diversity": s["source_diversity"],
                     "talent_flow": s["talent_flow"],
                     "terms": json.loads(s["terms_json"] or "[]"),
                     "companies": json.loads(s["companies_json"] or "[]"),
                     "contrarian": bool(s["is_contrarian"]),
                     "thesis": s["thesis_md"], "detected": (s["detected_at"] or "")[:10],
                     "evidence": ev[:6]})
    return {"count": len(rows), "rows": rows,
            "talent_flow_by_lab": talent_flow_summary()}


@app.get("/api/events")
def derived_events(kind: str = Query("all")) -> dict:
    """Brief §3(a) founder moves and customer wins, with the matched span as evidence."""
    kinds = ("founder_move", "customer_win") if kind == "all" else (kind,)
    ph = ",".join("?" for _ in kinds)
    rows = []
    for r in db.q(f"""SELECT s.kind, s.observed_at, s.url, s.raw, s.payload_json,
                             c.name company
                      FROM signals s LEFT JOIN companies c ON s.company_id=c.id
                      WHERE s.kind IN ({ph}) ORDER BY s.observed_at DESC LIMIT 100""", kinds):
        p = json.loads(r["payload_json"])
        rows.append({"kind": r["kind"], "company": r["company"],
                     "observed_at": r["observed_at"], "url": r["url"],
                     "frontier_lab": p.get("frontier_lab"),
                     "counterparty": p.get("counterparty"),
                     "contract_value_text": p.get("contract_value_text"),
                     "evidence_span": r["raw"]})
    return {"count": len(rows), "rows": rows}


@app.get("/api/peers")
def peers() -> dict:
    events = [dict(r) for r in db.q("""
        SELECT pe.event_type, pe.is_thesis_shift, pe.deviation_score, pe.observed_at,
               i.name investor, i.tier, c.name company, c.sector, s.url, s.payload_json
        FROM peer_events pe JOIN investors i ON pe.investor_id=i.id
        LEFT JOIN companies c ON pe.company_id=c.id
        LEFT JOIN signals s ON pe.source_signal_id=s.id
        ORDER BY pe.observed_at DESC LIMIT 200""")]
    for e in events:
        if not e["company"] and e["payload_json"]:
            e["company"] = json.loads(e["payload_json"]).get("issuer")
        e.pop("payload_json", None)
    heat = [dict(r) for r in db.q("""
        SELECT i1.name a, i2.name b, COUNT(DISTINCT v1.company_id) n,
               GROUP_CONCAT(DISTINCT c.name) companies
        FROM investments v1 JOIN investments v2
             ON v1.company_id=v2.company_id AND v1.investor_id < v2.investor_id
        JOIN investors i1 ON v1.investor_id=i1.id
        JOIN investors i2 ON v2.investor_id=i2.id
        JOIN companies c ON v1.company_id=c.id AND c.is_synthetic=0
        GROUP BY i1.name, i2.name ORDER BY n DESC LIMIT 60""")]
    return {"events": events, "heatmap": heat}


@app.get("/api/commentary")
def commentary(company_id: int | None = None) -> dict:
    sql = """SELECT cm.*, c.name company FROM commentary cm
             LEFT JOIN companies c ON cm.company_id=c.id
             WHERE (c.is_synthetic IS NULL OR c.is_synthetic=0)"""
    params: tuple = ()
    if company_id:
        sql += " AND cm.company_id=?"
        params = (company_id,)
    sql += " ORDER BY cm.observed_at DESC LIMIT 200"
    rows = []
    for r in db.q(sql, params):
        d = dict(r)
        d["themes"] = json.loads(d.pop("themes_json") or "null")
        rows.append(d)
    return {"count": len(rows), "rows": rows}


@app.get("/api/news")
def news() -> dict:
    return {"rows": [dict(r) for r in db.q(
        """SELECT * FROM news_items WHERE why_it_matters IS NOT NULL
           ORDER BY relevance_score DESC LIMIT 25""")]}


@app.get("/api/stale")
def stale() -> dict:
    days = thesis()["scoring"]["stale_days"]
    return {"stale_days": days, "rows": [dict(r) for r in db.q(
        """SELECT name, sector, stage, last_signal_at, status, is_synthetic,
                  CAST(julianday('now') - julianday(last_signal_at) AS INTEGER) days_quiet
           FROM companies WHERE last_signal_at IS NOT NULL
           AND julianday('now') - julianday(last_signal_at) > ?
           AND status != 'removed' ORDER BY days_quiet DESC""", (days,))]}


@app.get("/api/review-queue")
def review_queue() -> dict:
    rows = []
    for r in db.q("SELECT * FROM review_queue WHERE status='open' ORDER BY id DESC LIMIT 100"):
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        rows.append(d)
    return {"count": len(rows), "rows": rows}


@app.get("/api/provenance/{company_id}")
def provenance(company_id: int) -> dict:
    c = db.q1("SELECT name FROM companies WHERE id=?", (company_id,))
    if not c:
        raise HTTPException(404, "unknown company")
    sigs = [dict(r) for r in db.q(
        """SELECT s.kind, s.observed_at, s.fetched_at, s.fetch_mode, s.url, so.name source
           FROM signals s JOIN sources so ON s.source_id=so.id
           WHERE s.company_id=? ORDER BY s.observed_at DESC""", (company_id,))]
    enr = [dict(r) for r in db.q(
        """SELECT field, value_json, unavailable_reason, source, confidence, fetched_at
           FROM enrichment_cache WHERE company_id=?""", (company_id,))]
    score = db.q1("""SELECT model_version, prompt_version, features_json, scored_at
                     FROM scores WHERE company_id=? ORDER BY scored_at DESC LIMIT 1""",
                  (company_id,))
    return {"company": c["name"], "signals": sigs, "enrichment": enr,
            "score": dict(score) if score else None}


@app.get("/api/brief/{company_id}", response_class=HTMLResponse)
def brief(company_id: int) -> HTMLResponse:
    row = db.q1("""SELECT content_md FROM briefs WHERE company_id=? AND validated=1
                   ORDER BY generated_at DESC LIMIT 1""", (company_id,))
    if not row:
        raise HTTPException(404, "no validated brief for this company yet")
    return HTMLResponse(f"<pre>{row['content_md']}</pre>")


@app.get("/api/brief/{company_id}/raw")
def brief_raw(company_id: int) -> dict:
    row = db.q1("""SELECT content_md, recommendation, generated_at, trigger FROM briefs
                   WHERE company_id=? AND validated=1 ORDER BY generated_at DESC LIMIT 1""",
                (company_id,))
    if not row:
        raise HTTPException(404, "no validated brief for this company yet")
    return dict(row)


@app.get("/api/workbook")
def workbook():
    path = OUTPUT_DIR / "deal_pipeline.xlsx"
    if not path.exists():
        raise HTTPException(404, "workbook not generated yet")
    return FileResponse(path, filename="deal_pipeline.xlsx")


@app.get("/api/digest/latest", response_class=HTMLResponse)
def latest_digest() -> HTMLResponse:
    files = sorted((OUTPUT_DIR / "digests").glob("digest_*.html"))
    if not files:
        raise HTTPException(404, "no digest rendered yet")
    return HTMLResponse(files[-1].read_text())


# ------------------------------------------------------------------- interaction

@app.get("/api/scan")
def scan(thesis_text: str = Query(..., min_length=3), limit: int = 10) -> dict:
    """Component 15 — on-demand sector scan from a prose thesis."""
    _budget_or_429("scan")
    from engine.sectors import scan_thesis
    return {"thesis": thesis_text, "results": scan_thesis(thesis_text, limit)}


@app.get("/api/chat")
def chat(q: str = Query(..., min_length=2)) -> dict:
    """Component 16 — the same answering engine as chat.py, over HTTP."""
    _budget_or_429("chat")
    from chat import answer
    return {"question": q, "answer": answer(q), "stubbed": llm.stubbed()}


@app.post("/api/decision")
def decision(company_id: int, action: str, partner: str = "partner",
             note: str = "") -> dict:
    """Feedback loop: a partner decision always wins and is logged against the
    feature vector that produced the recommendation."""
    _budget_or_429("decision")
    if action not in ("Pass", "Watch", "Deep Dive"):
        raise HTTPException(400, "action must be Pass, Watch or Deep Dive")
    score = db.q1("""SELECT id, recommendation, human_override, composite, features_json
                     FROM scores WHERE company_id=? ORDER BY scored_at DESC, id DESC LIMIT 1""",
                  (company_id,))
    if not score:
        raise HTTPException(404, "company has no score yet")
    db.execute("UPDATE scores SET human_override=? WHERE id=?", (action, score["id"]))
    db.insert("partner_actions", {
        "company_id": company_id, "partner": partner, "action": "override",
        "score_at_time": score["composite"],
        "features_at_time_json": score["features_json"],
        "note": note or f"dashboard: {score['human_override'] or score['recommendation']}"
                        f" -> {action}",
        "created_at": db.now_iso()})
    status_map = {"Deep Dive": "hot", "Watch": "watchlist", "Pass": "pipeline"}
    db.execute("UPDATE companies SET status=? WHERE id=? AND status!='stale_review'",
               (status_map[action], company_id))
    return {"ok": True, "company_id": company_id, "action": action,
            "logged_to": "partner_actions"}


@app.post("/api/brief/{company_id}/generate")
def generate_brief_now(company_id: int) -> dict:
    """Component 06, on-demand path."""
    _budget_or_429("brief")
    from engine.briefs import generate_brief
    from engine.judge import judge_company
    judged = None if llm.stubbed() else judge_company(company_id)
    bid = generate_brief(company_id, "on_demand", judged, verbose=False)
    if not bid:
        raise HTTPException(422, "brief failed citation validation — flagged for review,"
                                " not published")
    return {"ok": True, "brief_id": bid, "stubbed_judgment": llm.stubbed()}


@app.post("/api/refresh")
def refresh(full: bool = True) -> dict:
    """Start a search NOW. This endpoint (the dashboard button) is the ONLY
    thing that starts a search in manual mode — every run is tracked step by
    step in the runs/run_steps tables the dashboard reads live."""
    _budget_or_429("refresh")
    from engine import runner
    run_id = runner.start(kind="full" if full else "quick", trigger_by="manual")
    if run_id is None:
        return JSONResponse({"ok": False, "reason": "a search is already running"},
                            status_code=409)
    return {"ok": True, "started": True, "run_id": run_id,
            "note": "poll /api/run/current for live step progress"}


@app.post("/api/llm/test")
def llm_test(model: str | None = None, hard: bool = False) -> dict:
    """Make one real call to the model provider and report exactly what happened —
    key rejected, model unavailable, out of credits, too slow, or working.

    `model=` probes any model without a redeploy; `hard=true` uses a judging-sized
    prompt, which is the only test that reflects what the pipeline actually does."""
    _budget_or_429("llm_test")
    return llm.self_test(model_override=model, hard=hard)


@app.get("/api/run/plan")
def run_plan() -> dict:
    """Every place a search looks, and roughly how long each takes — visible
    before you press the button, not only while it runs."""
    from engine import runner
    return runner.plan("full")


@app.post("/api/run/cancel")
def run_cancel() -> dict:
    """Stop the running search. It halts at the next safe point, keeps whatever
    it has already collected, and is recorded as 'stopped' — not as a failure."""
    from engine import runner
    if not runner.cancel():
        return JSONResponse({"ok": False, "reason": "no search is running"},
                            status_code=409)
    return {"ok": True, "stopping": True,
            "note": "the search stops at the next step; the page will update itself"}


@app.get("/api/run/current")
def run_current() -> dict:
    """The live search, with per-step status and an honest ETA."""
    from engine import runner
    cur = runner.current()
    return {"running": bool(cur), "run": cur}


@app.get("/api/runs")
def runs_history(limit: int = 20) -> dict:
    """Previous searches — date, duration, what each found."""
    from engine import runner
    return {"runs": runner.history(limit)}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict:
    """One past search: its steps, the exact deals it showed (frozen snapshot),
    and the diff against the search before it."""
    from engine import runner
    payload = runner.run_payload(run_id)
    if not payload:
        raise HTTPException(404, "unknown search")
    return payload


_DIGEST_STATE: dict = {"running": False, "last": None}


def _send_digest_bg() -> None:
    from outputs import digest as digest_mod
    from outputs import email_send
    _DIGEST_STATE["running"] = True
    try:
        path = digest_mod.build_digest(verbose=False)
        res = email_send.send_digest(path, verbose=False)
        _DIGEST_STATE["last"] = {"finished": db.now_iso(),
                                 "rendered": str(path.relative_to(ROOT)), **res}
    except Exception as exc:  # noqa: BLE001
        _DIGEST_STATE["last"] = {"finished": db.now_iso(), "delivered": False,
                                 "detail": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        _DIGEST_STATE["running"] = False


@app.post("/api/digest/send")
def send_digest_now(background: BackgroundTasks) -> dict:
    """Build + send in the background — news curation makes real LLM calls now,
    so a synchronous response would hang the button for minutes."""
    _budget_or_429("digest_send")
    if _DIGEST_STATE["running"]:
        return {"ok": True, "started": False, "note": "a digest send is already running"}
    background.add_task(_send_digest_bg)
    return {"ok": True, "started": True,
            "note": "building + sending in background; poll /api/digest/status"}


@app.get("/api/digest/status")
def digest_status() -> dict:
    return dict(_DIGEST_STATE)
