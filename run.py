"""Supervisor — starts every scheduled job in one APScheduler process.

Each job name maps 1:1 onto an n8n workflow in the production architecture
(n8n queue mode + Redis). Nothing here requires Docker, Postgres, cloud
accounts or paid keys.
"""
from __future__ import annotations
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from engine import db, ingest, filters, enrichment, judge, scoring, commentary, sectors, peers, health
from engine.config import thesis
from outputs import alerts as alerts_mod
from outputs import digest as digest_mod
from outputs import excel as excel_mod


def job_ingest():
    ingest.run_ingest()


def job_filter_score():
    filters.run_filter()
    enrichment.run_enrichment()
    judged = judge.run_judged_scoring()
    scoring.score_all(judged)
    from engine.briefs import auto_briefs
    auto_briefs(judged)


def job_commentary():
    commentary.run_commentary()


def job_sectors():
    sectors.detect_sectors()


def job_peers():
    peers.run_peer_tracking()


def job_excel():
    excel_mod.write_workbook()


def job_digest():
    digest_mod.build_digest()


def job_alerts():
    alerts_mod.run_alerts()


def job_health():
    health.check_sources()


def job_stale_sweep():
    scoring.maintain_staleness()   # flag for partner review — NEVER deletes


def main() -> None:
    db.connect()
    ingest.register_sources()
    sched = BackgroundScheduler()
    hour = thesis()["digest"]["hour_local"]
    add = sched.add_job
    add(job_ingest, "interval", minutes=60, id="01_news_rss_ingest+02_sec_form_d")
    add(job_filter_score, "interval", minutes=120, id="03_resolution+05_scoring+06_briefs")
    add(job_commentary, "interval", minutes=240, id="07_commentary_harvester")
    add(job_peers, "interval", minutes=240, id="08_peer_set_tracker")
    add(job_excel, "interval", minutes=120, id="09_excel_writer")
    # The assignment specified Mon/Wed/Fri; the fund asked for a morning brief
    # every day. `digest.days` in config/thesis.yaml is the switch — changing the
    # cadence is a config edit, not a code change, which is the point of keeping
    # the schedule out of Python.
    days = ",".join(thesis()["digest"].get("days") or ["mon", "wed", "fri"])
    add(job_digest, CronTrigger(day_of_week=days, hour=hour), id="10_digest_assembly")
    add(job_alerts, "interval", minutes=30, id="11_instant_alerts")
    add(job_sectors, "interval", minutes=720, id="12_sector_detection")
    add(job_health, "interval", minutes=60, id="13_error_handler+14_source_health")
    add(job_stale_sweep, "interval", minutes=1440, id="90d_stale_sweep")
    sched.start()
    print("Supervisor running. Jobs:")
    for j in sched.get_jobs():
        print(f"  {j.id:45s} next: {j.next_run_time}")
    print("(components 15 on-demand scan & 16 chat run via chat.py)")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        sched.shutdown()


if __name__ == "__main__":
    main()
