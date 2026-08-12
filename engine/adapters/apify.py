"""Apify adapter — funding discovery + company enrichment via Apify Actors.

Why Apify sits here at all: the licensed vendors (PitchBook, Coresignal…) are
enterprise contracts, and the free sources cannot see headcount, product traction
or coverage outside SEC filings. Apify runs public-web Actors on demand, which
fills part of that gap for ~$40/month instead of ~$40k/year.

What it does NOT do, deliberately:

  * It never fabricates. No token, or an Actor that returns nothing, produces an
    empty result and an honest health state — exactly like the licensed adapters.
  * It never lets a model extract a number. Funding amounts come from the same
    deterministic regex the RSS/HN adapters use, so a scraped headline is held to
    the identical evidence standard as a Form D filing.
  * It ships with Actors that scrape sources whose terms permit it (search
    results, company websites, public product listings). LinkedIn and X Actors
    exist on the Apify Store and are intentionally NOT configured here: both
    prohibit scraping in their terms, and a tool handed to a fund should not
    carry that liability. Licensed routes for those exist (Coresignal, the X API)
    and are already wired in `licensed.py`.

Configuration lives in config/sources.yaml — Actor ids, queries and field
mappings are data, not code, so switching Actors needs no Python change:

  - name: apify
    adapter: engine.adapters.apify.ApifyAdapter
    env_key: APIFY_TOKEN
    discovery:
      actor: "apify~google-search-scraper"
      queries: ["{theme} startup raises seed funding"]
      max_results: 20
    enrichment:
      actor: "apify~website-content-crawler"
      max_pages: 3
"""
from __future__ import annotations
import hashlib
import json
import os
import re
from datetime import datetime

import httpx

from .. import db
from ..config import USER_AGENT, thesis
from ..models import HealthStatus, Signal
from .base import BaseAdapter
from .rss_news import AMOUNT_RE, LED_BY_RE, STAGE_RE, clean_company_name, parse_amount

APIFY_API = "https://api.apify.com/v2"

# Team-size phrasings a company actually writes about itself. Deliberately narrow:
# "we're a team of 40" is a claim about headcount; "40 customers" is not.
TEAM_SIZE_RE = re.compile(
    r"\b(?:team of|we are|we're|company of|staff of)\s+(?:about\s+|around\s+|over\s+|~)?"
    r"(\d{1,5})\b(?:\s*\+)?\s*(?:people|employees|engineers|humans|folks|strong)?",
    re.I)
EMPLOYEE_COUNT_RE = re.compile(
    r"\b(\d{1,5})(?:\s*\+)?\s*(?:employees|team members|people)\b", re.I)


class ApifyNotConfigured(RuntimeError):
    """Raised when an Apify call is attempted with no token. Callers degrade."""


class ApifyAdapter(BaseAdapter):
    """Discovery source. Runs a search Actor per thesis theme and turns the real
    result URLs + titles into funding/news signals with full provenance."""

    name = "apify"
    interval_minutes = 720
    requires_license = False          # not a licence — just an API token

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.env_key = self.cfg.get("env_key", "APIFY_TOKEN")
        self.discovery = self.cfg.get("discovery") or {}
        self.enrichment = self.cfg.get("enrichment") or {}

    # ---- token / health ----------------------------------------------------

    @property
    def token(self) -> str | None:
        return os.environ.get(self.env_key) or None

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def health(self) -> HealthStatus:
        if not self.configured:
            return HealthStatus("unknown",
                                f"{self.env_key} not set — Apify actors are switched off "
                                "(no scraped data, and nothing invented in its place)")
        return super().health()

    # ---- Actor execution ---------------------------------------------------

    def _cache_key(self, actor: str, payload: dict) -> str:
        blob = json.dumps({"actor": actor, "input": payload}, sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:16]

    def run_actor(self, actor: str, payload: dict, timeout_s: int = 180) -> list[dict]:
        """Run an Actor synchronously and return its dataset items.

        Uses run-sync-get-dataset-items, which blocks until the run finishes and
        returns the rows in one call — the right shape for a pipeline step that
        must either produce evidence or admit it produced none.

        Results are snapshot-cached exactly like every other source, so an
        offline demo replays REAL previous output instead of silently producing
        an empty run that looks like 'no deals found'.
        """
        if not self.configured:
            raise ApifyNotConfigured(f"{self.env_key} is not set")
        cache = self._cache_path(f"actor:{actor}:{self._cache_key(actor, payload)}")
        url = f"{APIFY_API}/acts/{actor}/run-sync-get-dataset-items"
        try:
            r = httpx.post(url, params={"token": self.token, "timeout": timeout_s},
                           json=payload, timeout=timeout_s + 30,
                           headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            items = r.json()
            if not isinstance(items, list):
                items = []
            cache.write_text(json.dumps({"url": url, "actor": actor,
                                         "fetched_at": db.now_iso(), "items": items}))
            self._last_fetch_mode = "live"
            return items
        except Exception:
            if cache.exists():
                snap = json.loads(cache.read_text())
                self._last_fetch_mode = "cached_snapshot"
                return snap.get("items") or []
            raise

    # ---- discovery ---------------------------------------------------------

    def _queries(self) -> list[str]:
        """Thesis themes × configured query templates. The fund's themes drive
        what is searched, so config/thesis.yaml stays the single source of intent."""
        templates = self.discovery.get("queries") or ["{theme} startup raises funding round"]
        themes = [t["label"] for t in thesis().get("themes", [])][:8]
        return [tpl.format(theme=th) for tpl in templates for th in themes] or templates

    def fetch(self, since: datetime) -> list[Signal]:
        if not self.configured:
            db.execute("UPDATE sources SET last_attempt_at=?, health='unknown',"
                       " last_error=? WHERE name=?",
                       (db.now_iso(), f"{self.env_key} not set — Apify switched off", self.name))
            return []
        actor = self.discovery.get("actor") or "apify~google-search-scraper"
        max_results = int(self.discovery.get("max_results", 20))
        signals: list[Signal] = []
        seen: set[str] = set()

        for query in self._queries():
            payload = {"queries": query, "maxPagesPerQuery": 1,
                       "resultsPerPage": max_results, **(self.discovery.get("input") or {})}
            try:
                items = self.run_actor(actor, payload)
            except ApifyNotConfigured:
                return []
            for item in items:
                for res in (item.get("organicResults") or [item]):
                    sig = self._result_to_signal(res, query)
                    if sig and sig.dedupe_key not in seen:
                        seen.add(sig.dedupe_key)
                        signals.append(sig)
        return signals

    def _result_to_signal(self, res: dict, query: str) -> Signal | None:
        """One search result -> one signal. Amount and stage come from regex on
        the real title/description; a model is never asked what a number is."""
        url = res.get("url") or res.get("link")
        title = (res.get("title") or "").strip()
        desc = (res.get("description") or res.get("snippet") or "").strip()
        if not url or not title:
            return None
        text = f"{title}. {desc}"

        payload: dict = {"title": title, "description": desc[:500], "query": query,
                         "source": "apify_search"}
        kind = "news"
        m = AMOUNT_RE.search(text)
        if m:
            kind = "funding_event"
            payload["amount_usd"] = parse_amount(m)   # same parser as RSS/HN, by design
            payload["amount_text"] = m.group(0)
        st = STAGE_RE.search(text)
        if st:
            payload["stage"] = st.group(1).lower()
        led = LED_BY_RE.search(text)
        if led:
            payload["lead_investor"] = led.group(1).strip()

        name = clean_company_name(title.split(" raises")[0].split(" secures")[0]
                                  .split(" closes")[0].split(" lands")[0])
        return Signal(
            kind=kind,
            observed_at=(res.get("date") or db.now_iso()),
            url=url,
            dedupe_key=f"apify:{hashlib.sha1(url.encode()).hexdigest()[:20]}",
            payload=payload,
            company_name=name,
            company_domain=_domain_of(url) if kind == "funding_event" else None,
            fetch_mode=self._last_fetch_mode,
        )


# ---- company enrichment ----------------------------------------------------

def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1).lower() if m else None


def enrich_company(company_id: int, adapter: ApifyAdapter | None = None,
                   verbose: bool = False) -> dict:
    """Crawl a company's own website through Apify and store what it says about
    itself: team size, positioning, pricing presence.

    Team size from a company's own About page is weaker evidence than Coresignal's
    licensed headcount, so it is stored with a lower confidence and a source of
    'apify:<actor>' — a partner reading the workbook can see it is self-reported,
    not measured. Returning nothing is a valid outcome and is recorded as such.
    """
    from ..enrichment import cache_put as cache_set

    adapter = adapter or _adapter_from_config()
    c = db.q1("SELECT id, name, domain FROM companies WHERE id=?", (company_id,))
    if not c:
        return {"ok": False, "reason": "unknown company"}
    if not adapter or not adapter.configured:
        cache_set(company_id, "self_reported_headcount", None,
                  unavailable_reason="— (Apify not configured: APIFY_TOKEN unset)",
                  source="apify", confidence=0.0)
        return {"ok": False, "reason": "APIFY_TOKEN not set"}
    if not c["domain"]:
        cache_set(company_id, "self_reported_headcount", None,
                  unavailable_reason="— (no company domain known to crawl)",
                  source="apify", confidence=0.0)
        return {"ok": False, "reason": "no domain"}

    actor = (adapter.enrichment.get("actor") or "apify~website-content-crawler")
    payload = {"startUrls": [{"url": f"https://{c['domain']}"}],
               "maxCrawlPages": int(adapter.enrichment.get("max_pages", 3)),
               **(adapter.enrichment.get("input") or {})}
    try:
        items = adapter.run_actor(actor, payload)
    except Exception as exc:  # noqa: BLE001
        cache_set(company_id, "self_reported_headcount", None,
                  unavailable_reason=f"— (Apify crawl failed: {type(exc).__name__})",
                  source=f"apify:{actor}", confidence=0.0)
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    text = " ".join((i.get("text") or i.get("markdown") or "") for i in items)[:200_000]
    found: dict = {}

    size = TEAM_SIZE_RE.search(text) or EMPLOYEE_COUNT_RE.search(text)
    if size:
        n = int(size.group(1))
        if 1 <= n <= 100_000:                      # a 7-digit "team" is a parse error
            found["self_reported_headcount"] = n
            cache_set(company_id, "self_reported_headcount", n,
                      source=f"apify:{actor}", confidence=0.5)
    if "self_reported_headcount" not in found:
        cache_set(company_id, "self_reported_headcount", None,
                  unavailable_reason="— (not stated on the company's own site;"
                                     " measured headcount requires Coresignal)",
                  source=f"apify:{actor}", confidence=0.0)

    pricing = [i.get("url") for i in items
               if "pricing" in str(i.get("url") or "").lower()]
    if pricing:
        found["pricing_page"] = pricing[0]
        cache_set(company_id, "pricing_page", pricing[0],
                  source=f"apify:{actor}", confidence=0.8)

    if verbose:
        print(f"  apify enrich {c['name']}: {found or 'nothing found (recorded as such)'}")
    return {"ok": True, "company": c["name"], "found": found, "pages": len(items),
            "fetch_mode": adapter._last_fetch_mode}


def _adapter_from_config() -> ApifyAdapter | None:
    from ..config import sources_config
    for s in sources_config().get("sources", []):
        if s.get("name") == "apify":
            return ApifyAdapter(s)
    return None


def self_test() -> dict:
    """One cheap real call, so 'is Apify wired up?' is answerable from the
    dashboard rather than by reading logs. Never raises."""
    adapter = _adapter_from_config() or ApifyAdapter({})
    if not adapter.configured:
        return {"ok": False, "configured": False, "env_key": adapter.env_key,
                "reason": f"{adapter.env_key} is not set in this environment"}
    try:
        r = httpx.get(f"{APIFY_API}/users/me", params={"token": adapter.token},
                      timeout=20, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        return {"ok": True, "configured": True, "env_key": adapter.env_key,
                "account": data.get("username") or data.get("id"),
                "plan": (data.get("plan") or {}).get("id") if isinstance(data.get("plan"), dict)
                        else data.get("plan")}
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        hint = ("the token was rejected — check APIFY_TOKEN in your environment"
                if "401" in msg or "403" in msg else
                "could not reach api.apify.com — network or token problem")
        return {"ok": False, "configured": True, "env_key": adapter.env_key,
                "reason": hint, "provider_message": msg[:300]}
