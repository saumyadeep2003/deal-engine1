"""PatentsView (USPTO) — patents and inventors for tracked companies. Free key.

The moat criterion has been judged from press adjectives since day one; a
granted patent is the one moat artifact a government database will simply hand
over. The PatentSearch API (https://search.patentsview.org) is USPTO patent
data behind a free API key (PATENTSVIEW_API_KEY) — like Companies House: not a
licence, a signup.

Two disciplines, both inherited from the registry adapters:

* Exact-normalised assignee matching only. Fuzzy-matching a database of every
  US patent assignee is how "Apex Robotics Inc" inherits Apple's portfolio; a
  missed patent is recoverable, a stranger's patent in a moat argument is not.
* Inventors are recorded as inventors, never promoted into `founders`. An
  inventor on the company's patent is often a founder — and the judge can see
  the names side by side and say so — but the filing said "inventor", and this
  system records what filings actually said (people.py, rule one).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from .. import db
from ..models import Signal
from .base import BaseAdapter

SEARCH_URL = "https://search.patentsview.org/api/v1/patent/"
LEGAL_RE = re.compile(r"[,.]?\s+(inc|corp|corporation|llc|ltd|co|plc|limited)\.?$", re.I)


def norm_org(name: str | None) -> str:
    """Normalise an org name for the exact-match test: legal suffixes off,
    punctuation out, case folded."""
    base = LEGAL_RE.sub("", (name or "").strip())
    prev = None
    while prev != base:                      # "X, Inc." -> "X," -> "X"
        prev, base = base, LEGAL_RE.sub("", base).strip().rstrip(",. ")
    return re.sub(r"[^a-z0-9]", "", base.lower())


def build_query(company_name: str) -> str | None:
    """The API query for one company, or None when the name is too thin to
    match safely (same bar as the news watches)."""
    base = LEGAL_RE.sub("", company_name.strip()).strip().rstrip(",. ")
    if len(base) < 4:
        return None
    q = {"_text_phrase": {"assignees.assignee_organization": base}}
    f = ["patent_id", "patent_title", "patent_date", "patent_abstract",
         "assignees.assignee_organization",
         "inventors.inventor_name_first", "inventors.inventor_name_last"]
    return (f"{SEARCH_URL}?q={json.dumps(q, separators=(',', ':'))}"
            f"&f={json.dumps(f, separators=(',', ':'))}"
            f'&o={{"size":25}}')


def parse_patents(body: str, company_name: str) -> list[dict]:
    """Patents whose assignee EXACT-normalise-matches the company. [] on any
    unexpected shape or on assignees that merely resemble the name."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    want = norm_org(company_name)
    if not want:
        return []
    out = []
    for p in (data or {}).get("patents", []) or []:
        if not isinstance(p, dict) or not p.get("patent_id"):
            continue
        orgs = [a.get("assignee_organization") for a in (p.get("assignees") or [])
                if isinstance(a, dict)]
        if not any(norm_org(o) == want for o in orgs if o):
            continue                          # similar is not the same company
        inventors = []
        for i in (p.get("inventors") or []):
            if isinstance(i, dict):
                nm = " ".join(x for x in (i.get("inventor_name_first"),
                                          i.get("inventor_name_last")) if x).strip()
                if nm:
                    inventors.append(nm)
        out.append({"patent_id": p["patent_id"], "title": p.get("patent_title"),
                    "date": p.get("patent_date"),
                    "abstract": (p.get("patent_abstract") or "")[:400],
                    "inventors": inventors[:12]})
    return out


class PatentsAdapter(BaseAdapter):
    name = "patents"
    interval_minutes = 10080     # weekly: granted patents do not move daily
    requires_license = False     # free key, not a licence — like companies_house
    max_companies = 25

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.env_key = self.cfg.get("env_key", "PATENTSVIEW_API_KEY")
        self.max_companies = int(self.cfg.get("max_companies", self.max_companies))

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.env_key) or None

    def fetch(self, since: datetime) -> list[Signal]:
        if not self.api_key:
            return []            # health row explains; nothing is invented
        rows = db.q("""SELECT c.id, c.name, c.domain FROM companies c
                       LEFT JOIN scores s ON s.company_id=c.id AND s.id=(
                         SELECT id FROM scores WHERE company_id=c.id
                         ORDER BY scored_at DESC, id DESC LIMIT 1)
                       WHERE c.is_synthetic=0 AND c.status IN ('hot','watchlist')
                       AND NOT EXISTS (SELECT 1 FROM enrichment_cache e
                                       WHERE e.company_id=c.id AND e.field='patents_attempt'
                                       AND datetime(e.fetched_at, '+168 hours') > datetime('now'))
                       ORDER BY CASE c.status WHEN 'hot' THEN 0 ELSE 1 END,
                                COALESCE(s.percentile, -1) DESC
                       LIMIT ?""", (self.max_companies,))
        from ..enrichment import cache_put
        signals: list[Signal] = []
        for c in rows:
            url = build_query(c["name"])
            if url is None:
                cache_put(c["id"], "patents_attempt", None, "patentsview", 0.9,
                          unavailable_reason="name too generic to match an assignee safely")
                continue
            try:
                body, mode = self.http_get(url, retries=0,
                                           headers={"X-Api-Key": self.api_key})
            except Exception:  # noqa: BLE001 — retried next week, recorded nowhere false
                continue
            patents = parse_patents(body, c["name"])
            cache_put(c["id"], "patents_attempt", len(patents), "patentsview api", 0.9)
            for p in patents:
                signals.append(Signal(
                    kind="patent",
                    observed_at=f"{p['date']}T00:00:00+00:00" if p.get("date") else db.now_iso(),
                    url=f"https://patents.google.com/patent/US{p['patent_id']}",
                    dedupe_key=f"uspto:{p['patent_id']}",
                    payload={"title": p["title"], "patent_id": p["patent_id"],
                             "granted": p.get("date"), "inventors": p["inventors"],
                             "abstract": p["abstract"],
                             "note": "granted US patent, assignee exact-matched to this "
                                     "company via PatentsView"},
                    raw=f"US patent {p['patent_id']}: {p['title']} "
                        f"(inventors: {', '.join(p['inventors'][:5])})"[:500],
                    company_name=c["name"], company_domain=c["domain"],
                    fetch_mode=mode))
        return signals

    def probe(self) -> dict:
        if not self.api_key:
            return {"ok": False, "license_required": False,
                    "detail": f"{self.env_key} is not set — the key is FREE at "
                              "https://patentsview.org (account -> API key); with it, "
                              "granted patents + inventors become moat evidence"}
        return self.probe_url(build_query("International Business Machines"),
                              expect="patent")
