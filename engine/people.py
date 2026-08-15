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


def _ingest_people(company_id: int, signal_id: int, url: str | None, issuer: str,
                   people: list[dict]) -> int:
    """The one insertion path, whichever way the related-persons block arrived.
    Idempotent: a person already recorded for a company is left alone."""
    if VEHICLE_NAME_RE.search(issuer or ""):
        return 0              # a fund's officers are not a startup's team
    added = 0
    for p in people:
        name = (p.get("name") or "").strip()
        if not name or len(name) < 4:
            continue
        if db.q1("SELECT id FROM founders WHERE company_id=? AND name=?",
                 (company_id, name)):
            continue
        role = classify_role(p.get("titles"))
        db.insert("founders", {
            "company_id": company_id, "name": name,
            "notes": f"{role} per SEC Form D ({', '.join(p.get('titles') or []) or 'no title given'})"
                     f" [S:{signal_id}] {url or ''}"[:400]})
        added += 1
    return added


def sync_from_filings(verbose: bool = True) -> int:
    """Copy Form D related persons into `founders` from payloads that carry them."""
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
        added += _ingest_people(r["company_id"], r["id"], r["url"], issuer, people)
    if verbose:
        print(f"  people: {added} founder/officer record(s) added from Form D filings")
    return added


def backfill_related_persons(limit: int = 60, verbose: bool = True) -> int:
    """Fetch the detail XML for filings whose people were never read.

    The founder gap is not new filings — it is OLD ones. The EDGAR adapter reads
    primary_doc.xml for at most `max_detail_fetches` filings per run, and every
    filing beyond the cap was stored WITHOUT its related-persons block. Signals
    are immutable, so those payloads can never be repaired in place — and founder
    coverage sat at 75/347 with the names sitting on SEC's servers, free.

    This reads the XML for filings on live companies whose payload has no
    `related_persons` key at all (a fetched-but-empty list means the filing
    genuinely named nobody — refetching it would learn nothing), writes the
    people into `founders` (a mutable table — no immutability to violate), and
    records one attempt per signal in enrichment_cache so a dead fetch is not
    hammered forever. SEC asks for a descriptive User-Agent and modest rates,
    which BaseAdapter.http_get already provides; `limit` keeps one run polite.
    Run 20's people step said "0 records" and was telling the truth about the
    wrong thing: no NEW payloads carried people, while 100+ old filings had
    never been asked."""
    from .adapters.edgar_formd import EdgarFormDAdapter
    from .enrichment import cache_put
    rows = db.q("""SELECT s.id, s.company_id, s.url, s.payload_json, c.name
                   FROM signals s JOIN companies c ON c.id=s.company_id
                   WHERE s.kind='filing' AND c.is_synthetic=0
                   AND c.status IN ('pipeline','hot','watchlist')
                   AND s.payload_json NOT LIKE '%related_persons%'
                   AND NOT EXISTS (SELECT 1 FROM enrichment_cache e
                                   WHERE e.company_id=s.company_id
                                   AND e.field='rp_attempt_' || s.id)
                   ORDER BY s.observed_at DESC LIMIT ?""", (limit,))
    adapter = EdgarFormDAdapter()
    added, fetched = 0, 0
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        cik, adsh = payload.get("cik"), payload.get("accession")
        if not cik or not adsh:
            cache_put(r["company_id"], f"rp_attempt_{r['id']}", None, "edgar detail xml",
                      0.9, unavailable_reason="filing signal carries no cik/accession")
            continue
        detail = adapter._fetch_detail(int(cik), str(adsh))
        if not detail:        # unreachable/unparseable — no cache row, retry next run
            continue
        fetched += 1
        cache_put(r["company_id"], f"rp_attempt_{r['id']}",
                  len(detail.get("related_persons") or []), "edgar detail xml", 0.9)
        issuer = str(payload.get("issuer") or detail.get("entity_name") or r["name"] or "")
        added += _ingest_people(r["company_id"], r["id"], r["url"], issuer,
                                detail.get("related_persons") or [])
    if verbose:
        print(f"  people backfill: {fetched} filing detail(s) read, "
              f"{added} founder/officer record(s) recovered")
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
