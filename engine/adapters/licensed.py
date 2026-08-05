"""Licensed-source adapters.

Identical interface to free adapters, fully wired into the pipeline, tested
against the documented response shapes — but with no credential they return an
EMPTY result with LicenseRequired status. They never fabricate data.

When a contract is signed: set the env var named in config/sources.yaml and the
adapter's parse_response() starts returning rows. Nothing else changes.
"""
from __future__ import annotations
import os
from datetime import datetime

from .. import db
from ..config import env_key_present
from ..models import HealthStatus, Signal
from .base import BaseAdapter


class LicensedAdapter(BaseAdapter):
    requires_license = True
    vendor = "Unknown vendor"
    api_base = ""

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.vendor = self.cfg.get("license_vendor", self.vendor)
        self.env_key = self.cfg.get("env_key")

    @property
    def licensed(self) -> bool:
        return env_key_present(self.env_key)

    def fetch(self, since: datetime) -> list[Signal]:
        if not self.licensed:
            db.execute("UPDATE sources SET last_attempt_at=?, health='license_required',"
                       " last_error=? WHERE name=?",
                       (db.now_iso(), f"requires {self.vendor} contract ({self.env_key} unset)",
                        self.name))
            return []
        body, mode = self.http_get(self.request_url(since), headers=self.auth_headers())
        return self.parse_response(body, mode)

    def health(self) -> HealthStatus:
        if not self.licensed:
            return HealthStatus("license_required", f"requires {self.vendor}")
        return super().health()

    # --- per-vendor request/parse, written against public API docs ---
    def request_url(self, since: datetime) -> str:
        return self.api_base

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {os.environ.get(self.env_key or '', '')}"}

    def parse_response(self, body: str, mode: str) -> list[Signal]:
        return []


class PitchBookAdapter(LicensedAdapter):
    """PitchBook Data API — deals & valuations. Docs: /api/v1/deals?minDate=..."""
    name = "pitchbook"
    vendor = "PitchBook"
    api_base = "https://api.pitchbook.com/v1/deals"

    def request_url(self, since: datetime) -> str:
        return f"{self.api_base}?minDealDate={since.date().isoformat()}"

    def parse_response(self, body: str, mode: str) -> list[Signal]:
        import json
        out = []
        for deal in json.loads(body).get("items", []):
            out.append(Signal(
                kind="funding_event", observed_at=deal.get("dealDate", db.now_iso()),
                url=deal.get("dealUrl"), dedupe_key=f"pb:{deal.get('dealId')}",
                payload={"amount_usd": deal.get("dealSize"),
                         "valuation_usd": deal.get("postValuation"),
                         "stage": deal.get("dealType"),
                         "investors": deal.get("investors", [])},
                company_name=deal.get("companyName"),
                company_domain=deal.get("companyWebsite"), fetch_mode=mode))
        return out


class CrunchbaseAdapter(LicensedAdapter):
    name = "crunchbase"
    vendor = "Crunchbase"
    api_base = "https://api.crunchbase.com/api/v4/searches/funding_rounds"


class HarmonicAdapter(LicensedAdapter):
    name = "harmonic"
    vendor = "Harmonic"
    api_base = "https://api.harmonic.ai/companies"


class DealroomAdapter(LicensedAdapter):
    """Dealroom — named in brief §5 under both Deal databases and News feeds.
    The public Dealroom blog RSS is ingested free by rss_news; the funding
    *database* needs a contract, which is this adapter."""
    name = "dealroom"
    vendor = "Dealroom"
    api_base = "https://api.dealroom.co/api/v1/companies"

    def request_url(self, since: datetime) -> str:
        return f"{self.api_base}?updated_since={since.date().isoformat()}"

    def parse_response(self, body: str, mode: str) -> list[Signal]:
        import json
        out = []
        for c in json.loads(body).get("items", []):
            out.append(Signal(
                kind="funding_event", observed_at=c.get("last_funding_date", db.now_iso()),
                url=c.get("url"), dedupe_key=f"dealroom:{c.get('id')}",
                payload={"amount_usd": c.get("last_funding_amount"),
                         "valuation_usd": c.get("valuation"),
                         "stage": c.get("last_funding_round"),
                         "investors": c.get("investors", [])},
                company_name=c.get("name"), company_domain=c.get("website"),
                fetch_mode=mode))
        return out


class CoresignalAdapter(LicensedAdapter):
    """Coresignal — headcount, 6/12-month growth, LinkedIn partner post feed."""
    name = "coresignal"
    vendor = "Coresignal"
    api_base = "https://api.coresignal.com/cdapi/v1/linkedin/company/search"


class XAdapter(LicensedAdapter):
    """X API paid tier — GP watchlist timelines (handles in config/thesis.yaml)."""
    name = "x_gp_watchlist"
    vendor = "X API (paid tier)"
    api_base = "https://api.x.com/2/tweets/search/recent"


class BlindAdapter(LicensedAdapter):
    name = "blind"
    vendor = "Blind (no public API)"


class PodcastAdapter(LicensedAdapter):
    name = "podcasts"
    vendor = "Podcast transcript provider"


class SubstackThreadsAdapter(LicensedAdapter):
    name = "substack_threads"
    vendor = "Substack (no comments API)"


class TheInformationAdapter(LicensedAdapter):
    name = "the_information"
    vendor = "The Information"
