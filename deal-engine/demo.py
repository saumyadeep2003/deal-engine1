"""Scripted end-to-end demo — the local verification run. Target: < 3 minutes.

    pip install -r requirements.txt && python demo.py

Every pipeline row it produces is real (EDGAR, HN, RSS, arXiv, GitHub) or an
honest empty state. The ONLY synthetic records are the two clearly-marked
mechanism demos (entity resolution, staleness), confined to the Demo Cases tab.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import (commentary, db, enrichment, events, filters, firms, health, ingest,
                    judge, llm, peers, scoring, sectors)
from engine.briefs import auto_briefs
from engine.config import sources_config
from outputs import alerts as alerts_mod
from outputs import digest as digest_mod
from outputs import excel as excel_mod

T0 = time.time()


def step(n: int, title: str) -> None:
    print(f"\n[{time.time() - T0:5.1f}s] STEP {n:02d} — {title}")
    print("-" * 72)


def main() -> None:
    db.connect()
    ingest.register_sources()

    step(1, "Ingest from live free sources (live-first; falls back to cached real snapshots)")
    cov = firms.coverage()
    print(f"  firm dataset: {cov['total_firms']} firms indexed "
          f"({cov['from_dataset']} from {cov['dataset_path'] or 'no dataset file'}, "
          f"{cov['from_config']} from thesis.yaml)")
    if cov["note"]:
        print(f"    note: {cov['note']}")
    licensed = [s for s in sources_config()["sources"] if s.get("requires_license")]
    print("  licensed adapters wired but skipped (no key — LicenseRequired, never fabricated):")
    for s in licensed:
        print(f"    - {s['name']:20s} requires {s['license_vendor']} (set {s['env_key']})")
    free = [s["name"] for s in sources_config()["sources"] if not s.get("requires_license")]
    ingest.run_ingest(only=free)

    step(2, "Entity resolution — four name variants collapse into one record (SYNTHETIC demo)")
    sys.path.insert(0, str(Path(__file__).parent / "db"))
    import seed as seed_mod
    seed_mod.seed_investors()
    seed_mod.seed_demo_cases()
    demo = db.q1("SELECT id FROM companies WHERE name='DEMO-Alpha Systems'")
    for a in db.q("SELECT alias, alias_type, confidence FROM company_aliases WHERE company_id=?",
                  (demo["id"],)):
        print(f"    variant {a['alias']!r:38s} type={a['alias_type']:7s} conf={a['confidence']}")
    n_records = db.q1("SELECT COUNT(*) c FROM companies WHERE name LIKE 'DEMO%Alpha%'"
                      " OR name LIKE 'DEMO Alpha%'")["c"]
    print(f"    -> {n_records} company record (merge logged in company_aliases; reversible"
          " via resolution.unmerge)")

    step(3, "Deterministic filter (free rules before anything costs money)")
    events.derive_events()          # founder moves + customer wins, regex-classified
    filters.run_filter()

    step(4, "Enrichment — survivors only; licence-gated fields stay null with a reason")
    enrichment.run_enrichment()

    step(5, "Scoring — computed -> judged -> cohort percentile; token spend per stage")
    judged = judge.run_judged_scoring()
    scoring.score_all(judged)
    for u in llm.usage_by_stage() or [{"stage": "(no LLM calls — stub mode)", "model": "-",
                                       "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                       "stubbed": 0}]:
        print(f"    tokens: stage={u['stage']:14s} model={u['model']:12s} calls={u['calls']}"
              f" prompt={u['prompt_tokens']} completion={u['completion_tokens']}"
              f" stubbed={u['stubbed']}")

    step(6, "Briefs — auto-triggered above threshold percentile (+ on-demand any time)")
    n = auto_briefs(judged)
    print(f"    {n} brief(s) published to output/briefs/ (citation-validated; uncited numbers"
          " are rejected)")

    step(7, "Commentary harvest — HN/Reddit real; X/Blind/podcasts wired but licensed")
    commentary.run_commentary()

    step(8, "Sector detection — ratios, talent flow, and sourcing INSIDE each cluster")
    sectors.detect_sectors()
    import json as _json
    for s in db.q("""SELECT label, ratio, talent_flow, companies_json FROM sectors_emerging
                     ORDER BY ratio DESC LIMIT 3"""):
        found = _json.loads(s["companies_json"] or "[]")
        print(f"    {s['label'][:34]:36s} ratio={s['ratio']:<6} talent_flow={s['talent_flow']}")
        for c in found[:3]:
            print(f"      -> {c['company'][:32]:34s} {c['percentile']}th pct "
                  f"({c['evidence']})")
    tf = events.talent_flow_summary()
    if tf:
        print("    founder migration by originating lab: "
              + ", ".join(f"{t['lab']} x{t['moves']}" for t in tf))

    step(9, "Peer tracking — co-investor matrix + thesis-shift flags")
    peers.run_peer_tracking()

    step(10, "Excel workbook — 9 required tabs + Provenance + Demo Cases, two-way sync")
    scoring.maintain_staleness()   # 90-day sweep: flag for review, never delete
    excel_mod.write_workbook()

    step(11, "Digest render (Mon/Wed/Fri schedule; hard caps; honest empty sections)")
    digest_mod.build_digest()

    step(12, "Instant alerts — three deterministic routing conditions")
    alerts_mod.run_alerts()

    print(f"\n[{time.time() - T0:5.1f}s] Source health check (nothing fails silently):")
    health.check_sources()

    print(f"\n[{time.time() - T0:5.1f}s] STEP 13 — chat, pre-loaded with the brief's three questions")
    from chat import EXAMPLE_QUESTIONS, answer
    for q in EXAMPLE_QUESTIONS:
        print(f"\npartner> {q}")
        print(answer(q))

    print(f"\nDone in {time.time() - T0:.1f}s. Workbook: output/deal_pipeline.xlsx — "
          "digest: output/digests/ — briefs: output/briefs/")
    if sys.stdin.isatty():
        print("Dropping into interactive chat (Ctrl-D to exit).")
        from chat import main as chat_main
        chat_main()


if __name__ == "__main__":
    main()
