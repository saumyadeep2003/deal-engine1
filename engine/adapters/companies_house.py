"""UK Companies House — registry-grade founder data, free.

The founders problem in this engine has always been sourcing: free US sources
name people only when a Form D happens to, and a Form D names bare names with
bare titles. Companies House is a different class of source entirely — the UK's
statutory registry, with a free API that returns a company's actual officers:
names, roles, appointment dates, occupations, other directorships. For any UK
company in the pipeline this is better founder data than a paid US aggregator
would sell.

The key is free (register once at developer.company-information.service.gov.uk)
but it IS a key, so this adapter follows the Apify pattern: not licence-gated,
does nothing without the key, and says which of the two states it is in.

Output discipline: officers are emitted as `filing` signals whose payload carries
`related_persons` in exactly the shape the EDGAR adapter uses — so
`people.sync_from_filings()` ingests them without knowing this source exists.
One pipeline for people, regardless of which registry named them.

Scope: only companies the engine believes are UK (country/HQ evidence). Searching
a UK registry for a Delaware startup produces name-collision garbage, and a wrong
director on a company record is worse than none.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime

from .. import db
from ..models import Signal
from .base import BaseAdapter

API = "https://api.company-information.service.gov.uk"

UK_HINTS = ("united kingdom", "uk", "gb", "london", "cambridge", "oxford",
            "manchester", "edinburgh", "bristol", "leeds")


class CompaniesHouseAdapter(BaseAdapter):
    name = "companies_house"
    interval_minutes = 1440
    requires_license = False
    max_companies = 20

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.env_key = self.cfg.get("env_key", "COMPANIES_HOUSE_API_KEY")
        self.max_companies = int(self.cfg.get("max_companies", self.max_companies))

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.env_key) or None

    def _headers(self) -> dict:
        # HTTP basic auth, key as username, empty password — the registry's scheme
        token = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    @staticmethod
    def looks_uk(country: str | None, hq: str | None) -> bool:
        blob = f"{country or ''} {hq or ''}".lower()
        return any(h in blob for h in UK_HINTS)

    def probe(self) -> dict:
        if not self.api_key:
            return {"ok": False,
                    "detail": f"{self.env_key} is not set — the key is free "
                              "(developer.company-information.service.gov.uk)",
                    "hint": "Register once, set the key, and UK companies in the "
                            "pipeline get registry-grade officer data."}
        return self.probe_url(API + "/search/companies?q=monzo&items_per_page=1",
                              expect="company_number")

    def fetch(self, since: datetime) -> list[Signal]:
        if not self.api_key:
            return []          # health row explains; nothing is invented
        rows = db.q("""SELECT id, name, country, hq FROM companies
                       WHERE is_synthetic=0 AND status IN ('hot','watchlist','pipeline')
                       ORDER BY last_signal_at DESC LIMIT 200""")
        targets = [r for r in rows if self.looks_uk(r["country"], r["hq"])]
        signals: list[Signal] = []
        for c in targets[:self.max_companies]:
            found = self.lookup(c["name"])
            if not found:
                continue
            number, officers, profile_url = found
            if not officers:
                continue
            signals.append(Signal(
                kind="filing",
                observed_at=db.now_iso(),
                url=profile_url,
                # one record per company per registry snapshot month
                dedupe_key=f"ch:{number}:{db.now_iso()[:7]}",
                payload={"issuer": c["name"], "registry": "companies_house",
                         "company_number": number,
                         "related_persons": officers,
                         "platform": "companies_house"},
                company_name=c["name"],
                fetch_mode=self._last_fetch_mode))
        return signals

    def lookup(self, name: str) -> tuple[str, list[dict], str] | None:
        """Find the registry record and its active officers. None unless the top
        match is confidently this company — a wrong director is worse than none."""
        try:
            body, _ = self.http_get(
                API + f"/search/companies?q={name}&items_per_page=3",
                retries=0, headers=self._headers())
            items = (json.loads(body) or {}).get("items") or []
        except Exception:  # noqa: BLE001
            return None
        match = self.best_match(name, items)
        if not match:
            return None
        number = match.get("company_number")
        try:
            body, _ = self.http_get(
                API + f"/company/{number}/officers?items_per_page=10",
                retries=0, headers=self._headers())
            raw = (json.loads(body) or {}).get("items") or []
        except Exception:  # noqa: BLE001
            return None
        officers = [self.officer_to_person(o) for o in raw
                    if not o.get("resigned_on")][:8]
        return (number, [o for o in officers if o],
                f"https://find-and-update.company-information.service.gov.uk/company/{number}")

    @staticmethod
    def best_match(name: str, items: list[dict]) -> dict | None:
        """The registry match we are willing to attribute officers to. Exact
        normalised title only: fuzzy matching against a registry of five million
        entities is how someone else's board ends up on your pipeline company."""
        def norm(s: str) -> str:
            s = s.lower().strip()
            for suf in (" limited", " ltd", " plc", " llp", ", inc", " inc",
                        " llc", " corp", "."):
                s = s.removesuffix(suf).strip()
            return s
        want = norm(name)
        for it in items:
            if norm(it.get("title") or "") == want and \
                    (it.get("company_status") or "active") == "active":
                return it
        return None

    @staticmethod
    def officer_to_person(o: dict) -> dict | None:
        """Registry officer -> the related_persons shape people.py already reads."""
        name = (o.get("name") or "").strip()
        if not name or len(name) < 5:
            return None
        if "," in name:      # registry style: 'SURNAME, Forename'
            last, _, first = name.partition(",")
            name = f"{first.strip().title()} {last.strip().title()}"
        titles = [t for t in [o.get("officer_role"), o.get("occupation")] if t]
        return {"name": name, "titles": titles,
                "appointed_on": o.get("appointed_on")}
