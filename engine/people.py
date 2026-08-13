"""Founders and officers, from the filings that already name them.

This module exists because of a silent, total failure. SEC Form D filings carry a
`related_persons` block — the executives and directors of the issuing company,
with their titles. The EDGAR adapter has always captured it, and briefs have
always printed it. Nothing ever wrote it into the `founders` table.

The consequence was not cosmetic. `judge._context()` builds the model's evidence
from the `founders` table, so every company in the pipeline was assessed for
"founder quality" — the assignment's first scoring criterion — with **zero
founder evidence in the prompt**. The coverage report read `Founders identified:
0 of 160`, and the data had been sitting one table away the entire time.

A Form D related person is not necessarily a founder: the block lists executive
officers, directors and promoters. The distinction is kept rather than smoothed
over — `role` records what the filing actually said, and a director is not
promoted to founder because it would make the brief read better.
"""
from __future__ import annotations

import json
import re

from . import db

# Titles that indicate someone runs the company rather than sits on its board.
FOUNDER_TITLE_RE = re.compile(r"founder|co-?founder", re.I)
EXEC_TITLE_RE = re.compile(r"chief|ceo|cto|coo|cfo|president|managing member|partner", re.I)
DIRECTOR_TITLE_RE = re.compile(r"director|board", re.I)

# Vehicles file Form Ds too, and their "related persons" are fund staff, not
# operators. Recording them as founders would put a fund's CFO on a startup's team.
VEHICLE_NAME_RE = re.compile(r"\b(fund|lp\b|l\.p\.|partners|capital|ventures|holdings|"
                             r"spv|trust|management)\b", re.I)


def classify_role(titles: list[str] | None) -> str:
    t = " ".join(titles or [])
    if FOUNDER_TITLE_RE.search(t):
        return "founder"
    if EXEC_TITLE_RE.search(t):
        return "executive"
    if DIRECTOR_TITLE_RE.search(t):
        return "director"
    return "related person"


def sync_from_filings(verbose: bool = True) -> int:
    """Copy Form D related persons into `founders`. Idempotent: a person already
    recorded for a company is left alone, so re-running never duplicates a team."""
    rows = db.q("""SELECT s.id, s.company_id, s.url, s.payload_json, c.name
                   FROM signals s JOIN companies c ON c.id=s.company_id
                   WHERE s.kind IN ('filing','fund_formation') AND s.company_id IS NOT NULL
                   AND c.is_synthetic=0""")
    added = 0
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        people = payload.get("related_persons") or []
        if not people:
            continue
        issuer = str(payload.get("issuer") or r["name"] or "")
        if VEHICLE_NAME_RE.search(issuer):
            continue          # a fund's officers are not a startup's team
        for p in people:
            name = (p.get("name") or "").strip()
            if not name or len(name) < 4:
                continue
            if db.q1("SELECT id FROM founders WHERE company_id=? AND name=?",
                     (r["company_id"], name)):
                continue
            role = classify_role(p.get("titles"))
            db.insert("founders", {
                "company_id": r["company_id"], "name": name,
                "notes": f"{role} per SEC Form D ({', '.join(p.get('titles') or []) or 'no title given'})"
                         f" [S:{r['id']}] {r['url'] or ''}"[:400]})
            added += 1
    if verbose:
        print(f"  people: {added} founder/officer record(s) added from Form D filings")
    return added


def team(company_id: int) -> list[dict]:
    rows = db.q("""SELECT name, notes, prior_exits, frontier_lab_alum
                   FROM founders WHERE company_id=? ORDER BY id""", (company_id,))
    out = []
    for r in rows:
        notes = r["notes"] or ""
        out.append({"name": r["name"],
                    "role": notes.split(" per SEC")[0] if " per SEC" in notes else None,
                    "notes": notes,
                    "prior_exits": r["prior_exits"],
                    "frontier_lab_alum": bool(r["frontier_lab_alum"])})
    return out
