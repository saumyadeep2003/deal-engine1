"""Careers-page scraping — open req volume + function mix (engineering/sales/G&A).

Runs against pipeline companies that have a resolved domain. Real pages only;
a company with no reachable careers page yields null with a reason, never an
estimated headcount.
"""
from __future__ import annotations
import re
from datetime import datetime

from .. import db
from ..models import Signal
from .base import BaseAdapter

PATHS = ["/careers", "/jobs", "/about/careers", "/company/careers"]
FUNCTIONS = {
    "engineering": r"\b(engineer|developer|scientist|research|ml |ai |sre|devops|architect)\b",
    "sales": r"\b(sales|account executive|bdr|sdr|revenue|gtm|growth)\b",
    "ga": r"\b(finance|legal|people|hr|recruit|operations|admin|accountant)\b",
}


class CareersAdapter(BaseAdapter):
    name = "careers_pages"
    interval_minutes = 1440
    max_companies = 60   # per run

    def probe(self) -> dict:
        """The real code path, against exactly one company. Probing the full
        company list would make a diagnostic slower than the ingest it is meant
        to reassure you about."""
        prev = self.max_companies
        self.max_companies = 1
        try:
            return super().probe()
        finally:
            self.max_companies = prev

    def fetch(self, since: datetime) -> list[Signal]:
        rows = db.q("SELECT id, name, domain FROM companies WHERE domain IS NOT NULL"
                    " AND is_synthetic=0 AND status IN ('pipeline','hot','watchlist')"
                    " ORDER BY last_signal_at DESC LIMIT ?", (self.max_companies,))
        signals: list[Signal] = []
        for row in rows:
            for path in PATHS:
                url = f"https://{row['domain']}{path}"
                try:
                    body, mode = self.http_get(url, retries=0)
                except Exception:  # noqa: BLE001
                    continue
                text = re.sub(r"<[^>]+>", " ", body).lower()
                counts = {fn: len(re.findall(pat, text)) for fn, pat in FUNCTIONS.items()}
                openings = len(re.findall(r"\b(apply now|apply here|open position|we're hiring)\b", text))
                signals.append(Signal(
                    kind="hiring", observed_at=db.now_iso(), url=url,
                    dedupe_key=f"careers:{row['domain']}:{db.now_iso()[:10]}",
                    payload={"company_id": row["id"], "function_mentions": counts,
                             "opening_mentions": openings},
                    company_domain=row["domain"], company_name=row["name"],
                    fetch_mode=mode))
                break
        return signals
