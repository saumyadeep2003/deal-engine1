"""Seed: investor tier lists from config + the two clearly-marked synthetic
demo cases (entity-resolution collapse, staleness sweep).

NO synthetic pipeline companies are created here — the pipeline populates
itself from real sources. Synthetic records are named DEMO-*, flagged
is_synthetic=1, and confined to the Demo Cases tab.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import db, ingest  # noqa: E402
from engine.config import thesis  # noqa: E402
from engine.models import Signal  # noqa: E402


def seed_investors() -> int:
    t = thesis()
    tiers = t["investor_tiers"]
    stated = t.get("stated_focus", {})
    n = 0
    for tier_num, key in ((1, "tier1"), (2, "tier2"), (3, "tier3")):
        for name in tiers[key]:
            if not db.q1("SELECT id FROM investors WHERE name=?", (name,)):
                db.insert("investors", {
                    "name": name, "tier": tier_num,
                    "stated_focus_json": json.dumps(stated.get(name)) if name in stated else None})
                n += 1
    return n


def seed_demo_cases() -> None:
    """Two synthetic mechanism demos. Everything here is fictional and flagged."""
    now = datetime.now(timezone.utc)

    # 1) Entity resolution demo: one fictional company under four name variants
    #    arriving from four different (demo) sources.
    variants = [
        ("demo_src_rss", "DEMO-Alpha Systems", None),
        ("demo_src_edgar", "DEMO-Alpha Systems, Inc.", None),
        ("demo_src_hn", "DEMO-Alpha", "demo-alpha.example"),
        ("demo_src_news", "DEMO Alpha Sys", None),
    ]
    for i, (src, name, domain) in enumerate(variants):
        sig = Signal(kind="news", observed_at=(now - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     url=None, dedupe_key=f"demo:alpha:{i}",
                     payload={"title": f"[SYNTHETIC DEMO] mention of {name}",
                              "summary": "Fictional record used only to demonstrate entity resolution."},
                     company_name=name, company_domain=domain, fetch_mode="synthetic_demo")
        ingest.store_signals(src, [sig])

    # 2) Staleness demo: fictional company, last signal backdated 100 days.
    if not db.q1("SELECT id FROM companies WHERE name=?", ("DEMO-Stalewatch Robotics",)):
        db.insert("companies", {
            "name": "DEMO-Stalewatch Robotics", "domain": "demo-stalewatch.example",
            "description": "[SYNTHETIC DEMO] fictional company for the 90-day staleness sweep.",
            "sector": "robotics", "stage": "seed", "is_synthetic": 1,
            "status": "pipeline",
            "last_signal_at": (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "created_at": db.now_iso()})


def main() -> None:
    ingest.register_sources()
    n = seed_investors()
    seed_demo_cases()
    demo = db.q("SELECT id, name, is_synthetic FROM companies WHERE is_synthetic=1")
    print(f"Seeded {n} investors from config/thesis.yaml")
    print(f"Synthetic demo records (Demo Cases tab only): {[dict(d) for d in demo]}")


if __name__ == "__main__":
    main()
