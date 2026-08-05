"""Component 08 — peer set tracker.

- Detects new investments/fund formations by tracked firms (from real EDGAR +
  RSS signals; the `investments` join table quietly powers everything).
- Recomputes the co-investor co-occurrence matrix.
- Thesis-shift: each event compared against the firm's stated focus (config)
  and its observed sector distribution → deviation flag.
"""
from __future__ import annotations
import json

from . import db
from .filters import match_theme
from .config import thesis


def observed_distribution(investor_id: int) -> dict[str, int]:
    rows = db.q("""SELECT c.sector, COUNT(*) n FROM investments v
                   JOIN companies c ON v.company_id=c.id
                   WHERE v.investor_id=? AND c.sector IS NOT NULL GROUP BY c.sector""",
                (investor_id,))
    return {r["sector"]: r["n"] for r in rows}


def _event_theme(event) -> str | None:
    if event["sector"]:
        return event["sector"]
    if event["payload_json"]:
        issuer = json.loads(event["payload_json"]).get("issuer") or ""
        key, _ = match_theme(issuer)
        return key
    return None


def run_peer_tracking(verbose: bool = True) -> dict:
    stated = thesis().get("stated_focus", {})

    # derive/refresh observed sector distribution per investor
    for inv in db.q("SELECT id, name FROM investors"):
        dist = observed_distribution(inv["id"])
        if dist:
            db.execute("UPDATE investors SET sector_distribution_json=? WHERE id=?",
                       (json.dumps(dist), inv["id"]))

    # investment events from investments rows that have no peer_event yet
    for v in db.q("""SELECT v.*, i.name inv_name, c.sector, c.name comp FROM investments v
                     JOIN investors i ON v.investor_id=i.id
                     JOIN companies c ON v.company_id=c.id AND c.is_synthetic=0"""):
        if db.q1("SELECT id FROM peer_events WHERE investor_id=? AND company_id=?"
                 " AND event_type='investment'", (v["investor_id"], v["company_id"])):
            continue
        db.insert("peer_events", {
            "investor_id": v["investor_id"], "company_id": v["company_id"],
            "event_type": "investment", "observed_at": v["announced_at"],
            "source_signal_id": v["source_signal_id"]})

    # thesis-shift detection on all events for firms with a stated focus
    shifts = 0
    for e in db.q("""SELECT pe.id, pe.investor_id, i.name inv_name, c.sector,
                            s.payload_json
                     FROM peer_events pe JOIN investors i ON pe.investor_id=i.id
                     LEFT JOIN companies c ON pe.company_id=c.id
                     LEFT JOIN signals s ON pe.source_signal_id=s.id
                     WHERE pe.is_thesis_shift=0"""):
        focus = stated.get(e["inv_name"])
        if not focus:
            continue
        theme = _event_theme(e)
        if theme and theme not in focus:
            dist = observed_distribution(e["investor_id"])
            total = sum(dist.values()) or 1
            deviation = 1.0 - (dist.get(theme, 0) / total)
            db.execute("UPDATE peer_events SET is_thesis_shift=1, deviation_score=?"
                       " WHERE id=?", (round(deviation, 3), e["id"]))
            shifts += 1

    pairs = db.q1("""SELECT COUNT(*) c FROM (
        SELECT 1 FROM investments v1 JOIN investments v2
        ON v1.company_id=v2.company_id AND v1.investor_id<v2.investor_id
        GROUP BY v1.investor_id, v2.investor_id)""")["c"]
    events = db.q1("SELECT COUNT(*) c FROM peer_events")["c"]
    if verbose:
        print(f"  peer tracking: {events} events, {pairs} co-investor pairs,"
              f" {shifts} new thesis-shift flags")
    return {"events": events, "pairs": pairs, "thesis_shifts": shifts}
