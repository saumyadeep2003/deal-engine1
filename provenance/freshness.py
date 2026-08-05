"""Provenance layer — answers "where did this come from?" in one query.

Every stored value carries source, fetched_at and confidence:
- signals: source_id + url + fetched_at + fetch_mode (live / cached_snapshot)
- enrichment_cache: source + fetched_at + ttl_hours + confidence (+ reason when null)
- scores: features_json embeds per-feature source; model_version + prompt_version
The workbook's Provenance tab is rendered from here + outputs/excel.py.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import db  # noqa: E402


def freshness_report() -> list[dict]:
    return [dict(r) for r in db.q("""
        SELECT so.name source, so.health, so.requires_license, so.license_vendor,
               so.interval_minutes, so.last_ok_at, so.error_count,
               COUNT(s.id) signals, MAX(s.fetched_at) latest_fetch,
               SUM(CASE WHEN s.fetch_mode='live' THEN 1 ELSE 0 END) live,
               SUM(CASE WHEN s.fetch_mode='cached_snapshot' THEN 1 ELSE 0 END) cached
        FROM sources so LEFT JOIN signals s ON s.source_id=so.id
        GROUP BY so.id ORDER BY so.requires_license, so.name""")]


def trace_value(company_name: str, field: str) -> list[dict]:
    """One-click provenance: which real URLs is this company's data built from?"""
    comp = db.q1("SELECT id FROM companies WHERE name=?", (company_name,))
    if not comp:
        return []
    return [dict(r) for r in db.q(
        """SELECT s.kind, s.observed_at, s.fetched_at, s.fetch_mode, s.url
           FROM signals s WHERE s.company_id=? ORDER BY s.observed_at DESC""",
        (comp["id"],))]


if __name__ == "__main__":
    for row in freshness_report():
        print(row)
