"""The search runner — every search is a first-class, fully-tracked run.

Replaces the old opaque subprocess: the pipeline now executes in-process, one
named step at a time, writing live progress to the `run_steps` table so the
dashboard can show exactly what is happening ("Reading SEC filings… 192 items")
and how long is left (estimated from the durations of previous runs — the only
honest ETA there is).

At the end, the run freezes a snapshot of what it showed (`run_results`), so
"what did Tuesday's search find?" stays answerable forever, independent of
later re-ranking or re-ingestion.
"""
from __future__ import annotations
import json
import threading
import time
import traceback

from . import db
from . import people as people_mod
from . import profile as profile_mod
from .config import sources_config

# Plain-English step labels — these are user-facing.
SOURCE_LABELS = {
    "edgar_formd": "Reading SEC funding filings (Form D)",
    "rss_news": "Reading news feeds (TechCrunch, Bloomberg, FT…)",
    "hn": "Scanning Hacker News",
    "arxiv": "Scanning research papers (arXiv)",
    "github_trending": "Checking GitHub activity",
    "reddit": "Reading Reddit discussions",
    "careers_pages": "Reading company careers pages",
    "company_news": "Checking the news watch on every tracked company",
    "companies_house": "Reading UK registry officers (Companies House)",
    "company_website": "Visiting company websites",
}

_ACTIVE: dict = {"thread": None}


def _free_sources() -> list[dict]:
    return [s for s in sources_config()["sources"] if not s.get("requires_license")]


def plan(kind: str = "full") -> dict:
    """What a search WILL do, before anyone presses the button. Same step list the
    live panel renders, plus per-step estimates from previous runs — so the sources
    the engine checks are visible at all times, not only mid-search."""
    est = step_estimates()
    steps = [{"key": k, "label": lbl, "seconds_estimate": round(est[k], 1) if k in est else None,
              "is_source": k.startswith("collect:")} for k, lbl in build_steps(kind)]
    # a measured 0.0s step is real data, not a missing estimate — `or 20` would
    # silently inflate the total by 20s for every fast step
    known = [s["seconds_estimate"] for s in steps if s["seconds_estimate"] is not None]
    total = sum(s["seconds_estimate"] if s["seconds_estimate"] is not None else 20.0
                for s in steps)
    return {"steps": steps, "source_count": sum(1 for s in steps if s["is_source"]),
            "total_seconds_estimate": round(total),
            "basis": "estimated from your previous searches" if known
                     else "rough estimate — no searches run yet",
            "licensed_sources": [s["name"] for s in sources_config()["sources"]
                                 if s.get("requires_license")]}


def build_steps(kind: str) -> list[tuple[str, str]]:
    steps = [(f"collect:{s['name']}", SOURCE_LABELS.get(s["name"], f"Reading {s['name']}"))
             for s in _free_sources()]
    steps += [
        ("events", "Spotting founder moves & customer wins"),
        ("filter", "Filtering to the fund's focus areas"),
        ("people", "Reading founder & officer names out of SEC filings"),
        ("profiles", "Reading each company's own website for what they do"),
        ("enrich", "Gathering extra company details"),
        ("judge", "AI assessment of the top companies"),
        ("score", "Ranking everyone against similar companies"),
        ("briefs", "Writing one-page briefs for the best"),
        ("commentary", "Reading what people say about them"),
    ]
    if kind == "full":
        steps.append(("sectors", "Looking for early trends"))
    steps += [
        ("peers", "Tracking what other investors are doing"),
        ("stale", "Checking for companies gone quiet"),
        ("publish", "Updating the Excel workbook & Google Sheet"),
        ("alerts", "Checking the instant-alert rules"),
        ("snapshot", "Saving this search to history"),
    ]
    return steps


# ------------------------------------------------------------- run lifecycle

class RunCancelled(Exception):
    """Raised inside a run when the partner presses Stop."""


def is_running() -> bool:
    t = _ACTIVE.get("thread")
    return bool(t and t.is_alive())


def cancel() -> bool:
    """Ask the in-flight search to stop. It stops at the next step boundary (or
    the next company inside a long step), so the database is never left mid-write.
    Returns False when nothing is running."""
    ev = _ACTIVE.get("cancel")
    if not is_running() or ev is None:
        return False
    ev.set()
    return True


def _check_cancel() -> None:
    ev = _ACTIVE.get("cancel")
    if ev is not None and ev.is_set():
        raise RunCancelled("stopped by the partner")


def _clear_stale_running() -> None:
    """No live thread but rows still say 'running' — a crashed or killed run.
    Never let that block the button."""
    if is_running():
        return
    open_runs = db.q("SELECT id FROM runs WHERE status='running'")
    for r in open_runs:
        db.execute("UPDATE runs SET status='failed', error='interrupted (no live worker)',"
                   " finished_at=? WHERE id=?", (db.now_iso(), r["id"]))
        db.execute("UPDATE run_steps SET status='failed', detail='interrupted'"
                   " WHERE run_id=? AND status IN ('running','pending')", (r["id"],))


def recover_interrupted() -> None:
    """A crash/restart mid-run must not leave a phantom 'running' search."""
    db.execute("UPDATE runs SET status='failed', error='interrupted by restart',"
               " finished_at=? WHERE status='running'", (db.now_iso(),))
    db.execute("UPDATE run_steps SET status='failed', detail='interrupted'"
               " WHERE status IN ('running','pending') AND run_id IN"
               " (SELECT id FROM runs WHERE error='interrupted by restart')")


def step_estimates() -> dict[str, float]:
    """Median-ish per-step seconds from the last 3 completed runs."""
    rows = db.q("""SELECT rs.key, rs.seconds FROM run_steps rs
                   JOIN runs r ON rs.run_id=r.id
                   WHERE r.status='done' AND rs.status='done' AND rs.seconds IS NOT NULL
                   ORDER BY r.id DESC LIMIT 60""")
    by_key: dict[str, list[float]] = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(float(r["seconds"]))
    return {k: sorted(v)[len(v) // 2] for k, v in by_key.items() if v}


def start(kind: str = "full", trigger_by: str = "manual") -> int | None:
    """Begin a search in a background thread. Returns run_id, or None if one
    is already in progress. NOTHING else in the system starts a search."""
    if is_running():
        return None
    _clear_stale_running()
    run_id = db.insert("runs", {"kind": kind, "trigger_by": trigger_by,
                                "status": "running", "started_at": db.now_iso()})
    for seq, (key, label) in enumerate(build_steps(kind), start=1):
        db.insert("run_steps", {"run_id": run_id, "seq": seq, "key": key,
                                "label": label, "status": "pending"})
    t = threading.Thread(target=_execute, args=(run_id, kind), daemon=True,
                         name=f"search-run-{run_id}")
    _ACTIVE["cancel"] = threading.Event()
    _ACTIVE["thread"] = t
    t.start()
    return run_id


class _Step:
    def __init__(self, run_id: int, key: str):
        self.run_id, self.key = run_id, key
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.time()
        db.execute("UPDATE run_steps SET status='running', started_at=? WHERE run_id=? AND key=?",
                   (db.now_iso(), self.run_id, self.key))
        return self

    def progress(self, detail: str, items: int | None = None) -> None:
        db.execute("UPDATE run_steps SET detail=?, items=COALESCE(?, items)"
                   " WHERE run_id=? AND key=?", (detail, items, self.run_id, self.key))

    def __exit__(self, exc_type, exc, tb):
        status = "failed" if exc else "done"
        db.execute("""UPDATE run_steps SET status=?, finished_at=?, seconds=?,
                      detail=COALESCE(?, detail) WHERE run_id=? AND key=?""",
                   (status, db.now_iso(), round(time.time() - self.t0, 1),
                    str(exc)[:200] if exc else None, self.run_id, self.key))
        return False   # never swallow — _execute decides what a failure means


def _execute(run_id: int, kind: str) -> None:
    from . import commentary, enrichment, events, filters, ingest, judge, scoring, sectors
    from . import peers as peers_mod
    from .briefs import auto_briefs
    t0 = time.time()
    failed_steps: list[str] = []

    def run_step(key, fn):
        _check_cancel()                       # stop cleanly between steps
        try:
            with _Step(run_id, key) as st:
                return fn(st)
        except RunCancelled:
            raise                             # cancellation is not a step failure
        except Exception:  # noqa: BLE001 — one broken step must not kill the search
            failed_steps.append(key)
            traceback.print_exc()
            return None

    try:
        ingest.register_sources()
        from datetime import datetime, timedelta, timezone
        from .config import thesis
        since = datetime.now(timezone.utc) - timedelta(days=thesis()["filters"]["lookback_days"])

        # -- collect, one source at a time so the dashboard names each --
        for adapter in ingest.load_adapters([s["name"] for s in _free_sources()]):
            def collect(st, adapter=adapter):
                st.progress("connecting…")
                signals = adapter.safe_fetch(since)
                stats = ingest.store_signals(adapter.name, signals)
                st.progress(f"{stats['new']} new item(s), {stats['duplicate']} already known",
                            items=len(signals))
            run_step(f"collect:{adapter.name}", collect)

        run_step("events", lambda st: st.progress(json.dumps(
            events.derive_events(verbose=False)), items=None))
        run_step("filter", lambda st: st.progress(
            f"{filters.run_filter(verbose=False)['companies_kept']} companies match"))
        # Founder names have been sitting inside filings, unread, since day one.
        # Syncing them BEFORE judging is the whole point: the judge builds its
        # evidence from the founders table, so doing this afterwards would leave
        # founder quality assessed on nothing for another whole run.
        run_step("people", lambda st: st.progress(
            f"{people_mod.sync_from_filings(verbose=False)} founder/officer record(s)"
            " from filings"))
        run_step("profiles", lambda st: st.progress(
            f"{profile_mod.backfill(verbose=False)} company profile(s) written from"
            " their own websites"))
        run_step("enrich", lambda st: st.progress(
            f"{enrichment.run_enrichment(verbose=False)} companies enriched"))

        judged_box: dict = {}

        def judge_step(st):
            def cb(i, n, name):
                _check_cancel()               # the longest step, so also cancellable inside
                st.progress(f"assessing {name} ({i} of {n})", items=n)
            judged_box.update(judge.run_judged_scoring(verbose=False, progress_cb=cb))
        run_step("judge", judge_step)

        run_step("score", lambda st: st.progress(
            f"{scoring.score_all(judged_box, verbose=False)['scored']} companies ranked"))
        run_step("briefs", lambda st: st.progress(
            f"{auto_briefs(judged_box, verbose=False)} brief(s) written"))
        run_step("commentary", lambda st: st.progress(
            f"{commentary.run_commentary(verbose=False)} quote(s) captured"))
        if kind == "full":
            run_step("sectors", lambda st: st.progress(
                f"{sectors.detect_sectors(verbose=False)} trend(s) found"))
        run_step("peers", lambda st: st.progress(json.dumps(
            peers_mod.run_peer_tracking(verbose=False))))
        run_step("stale", lambda st: st.progress(
            f"{scoring.maintain_staleness(verbose=False)} flagged for review"))

        def publish(st):
            from outputs import excel, gsheets
            excel.write_workbook(verbose=False)
            st.progress("Excel written")
            res = gsheets.sync(verbose=False)
            st.progress(f"Excel written; Google Sheet: {res.get('status')}")
        run_step("publish", publish)

        def alerts_step(st):
            from outputs import alerts
            st.progress(f"{alerts.run_alerts(verbose=False)} alert(s) fired")
        run_step("alerts", alerts_step)

        run_step("snapshot", lambda st: st.progress(
            f"{snapshot_results(run_id)} companies saved to history"))

        stats = _final_stats(run_id)
        status = "done" if not failed_steps else "done"   # partial failures reported per-step
        db.execute("""UPDATE runs SET status=?, finished_at=?, seconds=?, stats_json=?,
                      error=? WHERE id=?""",
                   (status, db.now_iso(), round(time.time() - t0, 1), json.dumps(stats),
                    ("steps failed: " + ", ".join(failed_steps)) if failed_steps else None,
                    run_id))
    except RunCancelled:
        db.execute("UPDATE run_steps SET status='skipped', detail='stopped'"
                   " WHERE run_id=? AND status IN ('pending','running')", (run_id,))
        db.execute("UPDATE runs SET status='cancelled', finished_at=?, seconds=?,"
                   " error='stopped by the partner' WHERE id=?",
                   (db.now_iso(), round(time.time() - t0, 1), run_id))
        print(f"  search {run_id} stopped on request")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        db.execute("UPDATE runs SET status='failed', finished_at=?, seconds=?, error=?"
                   " WHERE id=?",
                   (db.now_iso(), round(time.time() - t0, 1), str(exc)[:300], run_id))


# ----------------------------------------------------------------- snapshot

def snapshot_results(run_id: int) -> int:
    """Freeze what this search shows. `is_new` marks companies never seen in
    any earlier search — the 'new this search' badge."""
    from .scoring import latest_scores
    rows = latest_scores(("hot", "watchlist", "pipeline", "stale_review"))
    n = 0
    for c in rows:
        prior = db.q1("SELECT 1 x FROM run_results WHERE company_id=? AND run_id<?",
                      (c["id"], run_id))
        pct, size = c["percentile"] or 0, c["cohort_size"] or 1
        rank = max(1, min(size, size - round(pct / 100 * size) + 1))
        db.insert("run_results", {
            "run_id": run_id, "company_id": c["id"], "name": c["name"],
            "sector": c["sub_sector"] or c["sector"], "stage": c["stage"],
            "recommendation": c.get("human_override") or c["recommendation"],
            "percentile": pct, "cohort_size": size, "rank_in_cohort": rank,
            "is_new": 0 if prior else 1, "captured_at": db.now_iso()})
        n += 1
    return n


def _final_stats(run_id: int) -> dict:
    top = db.q1("SELECT COUNT(*) c FROM run_results WHERE run_id=? AND"
                " recommendation='Deep Dive'", (run_id,))
    new = db.q1("SELECT COUNT(*) c FROM run_results WHERE run_id=? AND is_new=1", (run_id,))
    total = db.q1("SELECT COUNT(*) c FROM run_results WHERE run_id=?", (run_id,))
    signals = db.q1("SELECT COUNT(*) c FROM signals WHERE fetch_mode!='synthetic_demo'")
    return {"companies": total["c"], "top_picks": top["c"], "new_companies": new["c"],
            "signals_total": signals["c"]}


# ------------------------------------------------------------------ queries

def current() -> dict | None:
    run = db.q1("SELECT * FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1")
    if not run:
        return None
    if not is_running():           # DB says running but no thread — stale row
        recover_interrupted()
        return None
    return run_payload(run["id"], include_results=False)


def run_payload(run_id: int, include_results: bool = True) -> dict | None:
    run = db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run:
        return None
    steps = [dict(r) for r in db.q(
        "SELECT seq, key, label, status, started_at, seconds, items, detail"
        " FROM run_steps WHERE run_id=? ORDER BY seq", (run_id,))]
    est = step_estimates()
    remaining = sum(est.get(s["key"], 20.0) for s in steps
                    if s["status"] in ("pending", "running"))
    out = {"id": run["id"], "kind": run["kind"], "trigger_by": run["trigger_by"],
           "status": run["status"], "started_at": run["started_at"],
           "finished_at": run["finished_at"], "seconds": run["seconds"],
           "error": run["error"],
           "stats": json.loads(run["stats_json"]) if run["stats_json"] else None,
           "steps": steps,
           "eta_seconds_remaining": round(remaining) if run["status"] == "running" else 0,
           "eta_basis": "estimated from your previous searches" if est
                        else "first search — no history to estimate from yet"}
    if include_results:
        out["results"] = [dict(r) for r in db.q(
            """SELECT company_id, name, sector, stage, recommendation, percentile,
                      cohort_size, rank_in_cohort, is_new
               FROM run_results WHERE run_id=? ORDER BY percentile DESC""", (run_id,))]
        prev = db.q1("SELECT id FROM runs WHERE id<? AND status='done' ORDER BY id DESC"
                     " LIMIT 1", (run_id,))
        if prev:
            cur_top = {r["name"] for r in out["results"] if r["recommendation"] == "Deep Dive"}
            prev_top = {r["name"] for r in db.q(
                "SELECT name FROM run_results WHERE run_id=? AND recommendation='Deep Dive'",
                (prev["id"],))}
            out["diff_vs_previous"] = {"previous_run_id": prev["id"],
                                       "new_top_picks": sorted(cur_top - prev_top),
                                       "dropped_top_picks": sorted(prev_top - cur_top)}
    return out


def history(limit: int = 20) -> list[dict]:
    rows = db.q("SELECT * FROM runs WHERE status IN ('done','failed','cancelled')"
                " ORDER BY id DESC LIMIT ?", (limit,))
    return [{"id": r["id"], "kind": r["kind"], "trigger_by": r["trigger_by"],
             "status": r["status"], "started_at": r["started_at"],
             "seconds": r["seconds"],
             "stats": json.loads(r["stats_json"]) if r["stats_json"] else None,
             "error": r["error"]} for r in rows]
