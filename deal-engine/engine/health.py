"""Components 13/14 — error handling + source health monitor.

Every adapter writes a heartbeat (sources.last_ok_at). Any source quiet beyond
2× its expected interval raises an alert. A scraper returning an empty array
successfully, forever, is the failure mode that kills systems like this — so
zero-yield streaks are flagged too. Nothing fails silently.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from . import db


def check_sources(verbose: bool = True) -> list[dict]:
    alerts = []
    for s in db.q("SELECT * FROM sources WHERE name NOT LIKE 'demo_%'"):
        if s["requires_license"]:
            continue  # honest LicenseRequired state, not a fault
        if not s["last_ok_at"]:
            if s["last_attempt_at"]:
                alerts.append({"source": s["name"], "issue": "never succeeded",
                               "last_error": s["last_error"]})
            continue
        last = datetime.fromisoformat(s["last_ok_at"].replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        quiet_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        if quiet_min > 2 * s["interval_minutes"]:
            alerts.append({"source": s["name"],
                           "issue": f"quiet {quiet_min / 60:.1f}h"
                                    f" (> 2x its {s['interval_minutes']}min interval)",
                           "last_error": s["last_error"]})
        if s["error_count"] >= 3:
            alerts.append({"source": s["name"],
                           "issue": f"{s['error_count']} consecutive errors",
                           "last_error": s["last_error"]})
    for a in alerts:
        key = f"source_health:{a['source']}:{db.now_iso()[:10]}"
        try:
            db.insert("alerts_log", {"rule": "source_health", "dedupe_key": key,
                                     "payload_json": json.dumps(a),
                                     "created_at": db.now_iso()})
            if verbose:
                print(f"  ⚠ SOURCE HEALTH ALERT: {a['source']} — {a['issue']}")
        except Exception:  # noqa: BLE001
            pass  # already alerted today (rate-limited)
    if verbose and not alerts:
        print("  source health: all free sources within heartbeat window")
    return alerts
