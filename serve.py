"""Single always-on process: scheduler + web dashboard.

This is what the launchd service runs. The APScheduler jobs from run.py and the
FastAPI app share one process, one SQLite file, one log stream.

Sleep-aware: a laptop that was asleep at the scheduled hour must not silently
skip a digest. Jobs use coalesce + a generous misfire grace, and on startup a
catch-up pass sends today's digest if today is a digest day and none went out.

    python serve.py            # foreground
    open http://127.0.0.1:8787
"""
from __future__ import annotations
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path  # noqa: F401  (used by bootstrap_if_empty)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from engine import db, ingest, runner
from engine.config import LOG_DIR, WEB_HOST, WEB_PORT, env, thesis

# manual = searches happen ONLY when a person presses the button (also caps LLM
# spend). auto = the self-updating schedule the fund's brief asked for.
SEARCH_MODE = (env("SEARCH_MODE", "manual") or "manual").lower()

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("dealengine")

DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _job(fn, name: str):
    def wrapped():
        log.info("job start: %s", name)
        try:
            fn()
            log.info("job done: %s", name)
        except Exception:  # noqa: BLE001 — one failing job must not stop the scheduler
            log.exception("job FAILED: %s", name)
    wrapped.__name__ = name
    return wrapped


# ----------------------------------------------------------------- job bodies

def job_ingest():
    from engine.config import sources_config
    free = [s["name"] for s in sources_config()["sources"] if not s.get("requires_license")]
    ingest.run_ingest(only=free)


def job_score():
    from engine import enrichment, events, filters, judge, scoring
    from engine.briefs import auto_briefs
    events.derive_events()      # founder moves + customer wins (brief §3a)
    filters.run_filter()
    enrichment.run_enrichment()
    judged = judge.run_judged_scoring()
    scoring.score_all(judged)
    auto_briefs(judged)


def job_commentary():
    from engine import commentary
    commentary.run_commentary()


def job_sectors():
    from engine import sectors
    sectors.detect_sectors()


def job_peers():
    from engine import peers
    peers.run_peer_tracking()


def job_publish():
    """Staleness sweep -> workbook -> Google Sheet mirror. One renderer, two homes."""
    from engine import scoring
    from outputs import excel, gsheets
    scoring.maintain_staleness()
    excel.write_workbook()
    gsheets.sync()


def job_digest():
    from outputs import digest, email_send
    path = digest.build_digest()
    email_send.send_digest(path)


def job_alerts():
    from outputs import alerts
    alerts.run_alerts()


def job_health():
    from engine import health
    health.check_sources()


def job_logrotate(max_bytes: int = 20 * 1024 * 1024, keep: int = 3) -> None:
    """Keep launchd's captured stdout/stderr from growing without bound."""
    for name in ("engine.out.log", "engine.err.log"):
        p = LOG_DIR / name
        if p.exists() and p.stat().st_size > max_bytes:
            for i in range(keep - 1, 0, -1):
                older, newer = p.with_suffix(f".log.{i + 1}"), p.with_suffix(f".log.{i}")
                if newer.exists():
                    newer.replace(older)
            p.replace(p.with_suffix(".log.1"))
            p.touch()
            log.info("rotated %s", name)


# --------------------------------------------------------------- digest catch-up

def digest_catchup() -> None:
    """If today is a digest day and nothing went out, send it now. Covers the
    laptop-was-asleep case, which a bare cron trigger would silently miss."""
    cfg = thesis()["digest"]
    today = datetime.now().weekday()
    if today not in {DAY_MAP[d] for d in cfg["days"]}:
        return
    today_local = datetime.now().strftime("%Y-%m-%d")
    sent = db.q1("""SELECT id FROM digests WHERE kind='mwf_digest'
                    AND substr(sent_at,1,10) = ?""", (today_local,))
    if sent:
        return
    if datetime.now().hour < cfg["hour_local"]:
        return   # not due yet today; the cron trigger will handle it
    log.info("digest catch-up: today is a digest day and none was sent — sending now")
    job_digest()


# ---------------------------------------------------------------------- wiring

def job_search():
    """Auto-mode scheduled search — goes through the same tracked runner as the
    button, so scheduled runs appear in history identically."""
    runner.start(kind="full", trigger_by="scheduled")


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(job_defaults={
        "coalesce": True,          # a sleeping laptop collapses missed runs into one
        "misfire_grace_time": 6 * 3600,
        "max_instances": 1,
    })
    add = sched.add_job
    if SEARCH_MODE == "auto":
        hour = thesis()["digest"]["hour_local"]
        days = ",".join(thesis()["digest"]["days"])
        add(_job(job_search, "hourly_search"), "interval", minutes=60, id="hourly_search")
        add(_job(job_digest, "10_digest_assembly+email"),
            CronTrigger(day_of_week=days, hour=hour), id="10_digest_assembly+email")
        add(_job(job_alerts, "11_instant_alerts"), "interval", minutes=30,
            id="11_instant_alerts")
    # manual mode registers NO search/digest/alert jobs: nothing runs on its own.
    # Only zero-cost housekeeping stays (no fetches, no LLM calls):
    add(_job(job_health, "13_error_handler+14_source_health"), "interval", minutes=60,
        id="13_error_handler+14_source_health")
    add(_job(job_logrotate, "log_rotation"), "interval", hours=12, id="log_rotation")
    return sched


def bootstrap_if_empty() -> None:
    """Seed configuration on an empty database. Whether a SEARCH runs is
    governed strictly by SEARCH_MODE: in manual mode nothing runs until a
    person presses the button — an empty dashboard says so instead."""
    n = db.q1("SELECT COUNT(*) c FROM companies")["c"]
    inv = db.q1("SELECT COUNT(*) c FROM investors")["c"]
    if inv == 0:
        sys.path.insert(0, str(Path(__file__).parent / "db"))
        import seed as seed_mod
        seed_mod.seed_investors()
        seed_mod.seed_demo_cases()
        log.info("bootstrap: seeded investors + demo cases (empty database)")
    if n == 0:
        if SEARCH_MODE == "auto":
            runner.start(kind="full", trigger_by="boot")
            log.info("bootstrap: empty pipeline — auto mode, search started")
        else:
            log.info("bootstrap: empty pipeline — manual mode, waiting for the button")


@asynccontextmanager
async def lifespan(app):  # noqa: ANN001
    db.connect()
    ingest.register_sources()
    runner.recover_interrupted()   # a restart mid-search must not leave a phantom run
    sched = build_scheduler()
    sched.start()
    log.info("search mode: %s%s", SEARCH_MODE,
             " — searches run ONLY via the button" if SEARCH_MODE != "auto" else "")
    log.info("storage: %s", db.backend_info())
    log.info("scheduler started with %d jobs", len(sched.get_jobs()))
    for j in sched.get_jobs():
        log.info("  %-52s next: %s", j.id, j.next_run_time)
    if SEARCH_MODE == "auto":
        try:
            digest_catchup()
        except Exception:  # noqa: BLE001
            log.exception("digest catch-up failed")
    try:
        bootstrap_if_empty()
    except Exception:  # noqa: BLE001
        log.exception("bootstrap failed")
    log.info("dashboard on http://%s:%d", WEB_HOST, WEB_PORT)
    yield
    sched.shutdown(wait=False)
    log.info("scheduler stopped")


def build_app():
    from web.api import app as api_app
    api_app.router.lifespan_context = lifespan
    return api_app


app = build_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level="info", access_log=False)
