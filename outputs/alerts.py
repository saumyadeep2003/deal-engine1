"""Component 11 — instant alerts. Three deterministic routing conditions
(rules, not model calls), rate-limited and de-duplicated per company:

1. 2+ Tier 1 firms co-investing in the same company/round
2. A tracked firm investing outside its stated focus (thesis shift)
3. A watched founder starting a new company
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import db  # noqa: E402
from engine.config import OUTPUT_DIR, thesis  # noqa: E402

ALERTS_DIR = OUTPUT_DIR / "alerts"


def _fire(rule: str, company_id: int | None, payload: dict, verbose: bool) -> bool:
    """Rate-limited (per company per rule per day) + deduplicated."""
    window = thesis()["alerts"]["rate_limit_per_company_hours"]
    key = f"{rule}:{company_id or payload.get('investor', '?')}:{db.now_iso()[:10]}"
    recent = db.q1("""SELECT id FROM alerts_log WHERE rule=? AND company_id IS ?
                      AND created_at > datetime('now', ?)""",
                   (rule, company_id, f"-{window} hours"))
    if recent:
        return False
    try:
        db.insert("alerts_log", {"rule": rule, "company_id": company_id,
                                 "dedupe_key": key, "payload_json": json.dumps(payload),
                                 "created_at": db.now_iso()})
    except Exception:  # noqa: BLE001  — duplicate dedupe_key
        return False
    ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    html = (f"<html><body style=\"font-family:system-ui,-apple-system,sans-serif\">"
            f"<h2>⚡ INSTANT ALERT — {rule}</h2>"
            f"<pre style=\"background:#f9f9f7;padding:12px;border-radius:6px\">"
            f"{json.dumps(payload, indent=2)}</pre>"
            f"<p>Routed immediately — does not wait for the next digest.</p></body></html>")
    fn = ALERTS_DIR / f"alert_{rule}_{db.now_iso().replace(':', '')}.html"
    fn.write_text(html)
    if verbose:
        print(f"  ⚡ INSTANT ALERT [{rule}]: {json.dumps(payload)[:140]}")
    from . import email_send
    res = email_send.send_alert(rule, payload, html, verbose=verbose)
    row = db.q1("SELECT id FROM alerts_log WHERE dedupe_key=?", (key,))
    if row:
        db.execute("UPDATE alerts_log SET delivered=?, delivery_detail=? WHERE id=?",
                   (1 if res["delivered"] else 0, res["detail"], row["id"]))
    return True


def check_tier1_coinvest(verbose: bool = True) -> int:
    n_min = thesis()["alerts"]["tier1_coinvest_min"]
    fired = 0
    rows = db.q("""SELECT v.company_id, c.name, COUNT(DISTINCT v.investor_id) n,
                          GROUP_CONCAT(DISTINCT i.name) firms
                   FROM investments v JOIN investors i ON v.investor_id=i.id AND i.tier=1
                   JOIN companies c ON v.company_id=c.id AND c.is_synthetic=0
                   GROUP BY v.company_id, c.name
                   HAVING COUNT(DISTINCT v.investor_id) >= ?""", (n_min,))
    for r in rows:
        if _fire("tier1_coinvest", r["company_id"],
                 {"company": r["name"], "tier1_firms": r["firms"], "count": r["n"]}, verbose):
            fired += 1
    return fired


def check_thesis_shift(verbose: bool = True) -> int:
    fired = 0
    rows = db.q("""SELECT pe.*, i.name inv, c.name comp, s.payload_json, s.url
                   FROM peer_events pe JOIN investors i ON pe.investor_id=i.id
                   LEFT JOIN companies c ON pe.company_id=c.id
                   LEFT JOIN signals s ON pe.source_signal_id=s.id
                   WHERE pe.is_thesis_shift=1""")
    for r in rows:
        vehicle = r["comp"] or (json.loads(r["payload_json"]).get("issuer")
                                if r["payload_json"] else "?")
        if _fire("thesis_shift", r["company_id"],
                 {"investor": r["inv"], "vehicle": vehicle,
                  "deviation": r["deviation_score"], "source": r["url"]}, verbose):
            fired += 1
    return fired


def check_watched_founders(verbose: bool = True) -> int:
    watched = thesis().get("watched_founders", [])
    fired = 0
    for name in watched:
        rows = db.q("""SELECT s.id, s.company_id, s.url, s.observed_at, c.name comp
                       FROM signals s LEFT JOIN companies c ON s.company_id=c.id
                       WHERE (s.raw LIKE ? OR s.payload_json LIKE ?)
                       AND s.fetch_mode != 'synthetic_demo'
                       AND s.observed_at > datetime('now', '-30 days')""",
                    (f"%{name}%", f"%{name}%"))
        for r in rows:
            if _fire("watched_founder", r["company_id"],
                     {"founder": name, "company": r["comp"],
                      "signal_url": r["url"], "observed_at": r["observed_at"]}, verbose):
                fired += 1
    return fired


def run_alerts(verbose: bool = True) -> int:
    total = (check_tier1_coinvest(verbose) + check_thesis_shift(verbose)
             + check_watched_founders(verbose))
    if verbose and not total:
        print("  instant alerts: no routing condition met (rules evaluated, nothing fired)")
    return total


if __name__ == "__main__":
    run_alerts()
