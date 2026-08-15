"""A standing news watch on every tracked company.

Until this adapter existed, the engine's news reading was discovery-shaped: it
read a fixed set of feeds and noticed companies that happened to appear in them.
A company already IN the pipeline was only updated if it happened to reappear in
one of those same feeds — so "tracking" was really "hoping to re-discover". A
partner who has marked something Deep Dive expects the opposite: everything
public that mentions that company, watched continuously.

Google News' RSS endpoint answers exactly that shape of question for free: a
query feed per company name, covering essentially the whole open press. Each
tracked company gets its own standing query, newest-first, and every hit becomes
a signal that is already attributed — no entity resolution guesswork, because we
asked about this company by name.

Quoting discipline: the quoted company name plus a funding-context term keeps
precision high for ambiguously-named companies ("Text", "Built" and friends are
exactly why bare-name search is not enough). Companies whose names are too
generic even for that are skipped and say so, which beats attributing someone
else's news to them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser

from .. import db
from ..models import Signal
from .base import BaseAdapter

FEED = ("https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")

# Names where even quoting cannot rescue precision. Attribution failure is worse
# than a missed article: wrong news on a tracked company misleads a partner.
GENERIC = {"text", "built", "natural", "general", "national", "global", "digital",
           "united", "form", "select", "core", "prime", "scale", "base"}


class CompanyNewsAdapter(BaseAdapter):
    name = "company_news"
    interval_minutes = 360
    requires_license = False
    max_companies = 40

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.max_companies = int(self.cfg.get("max_companies", self.max_companies))

    def probe(self) -> dict:
        """One real query for one tracked company — the exact code path."""
        prev = self.max_companies
        self.max_companies = 1
        try:
            return super().probe()
        finally:
            self.max_companies = prev

    @staticmethod
    def query_for(name: str) -> str | None:
        """The search that is safe to attribute. None means 'unsafe to watch'."""
        base = name.strip().rstrip(".")
        for suffix in (", Inc", " Inc", " LLC", " Ltd", " Corp", " Corporation"):
            if base.lower().endswith(suffix.lower()):
                base = base[: -len(suffix)].rstrip(",. ")
        if len(base) < 4 or base.lower() in GENERIC:
            return None
        # quoted name + context term: precision over recall, on purpose
        return f'"{base}" (funding OR raised OR startup OR launches OR partnership)'

    def fetch(self, since: datetime) -> list[Signal]:
        # Watch the companies a partner is most likely to act on: best-ranked
        # first, so the cap trims the tail rather than the head.
        rows = db.q("""SELECT c.id, c.name FROM companies c
                       LEFT JOIN scores s ON s.company_id=c.id AND s.id=(
                         SELECT id FROM scores WHERE company_id=c.id
                         ORDER BY scored_at DESC, id DESC LIMIT 1)
                       WHERE c.is_synthetic=0 AND c.status IN ('hot','watchlist','pipeline')
                       ORDER BY CASE c.status WHEN 'hot' THEN 0 WHEN 'watchlist' THEN 1
                                ELSE 2 END, COALESCE(s.percentile, -1) DESC
                       LIMIT ?""", (self.max_companies,))
        cutoff = since.replace(tzinfo=since.tzinfo or timezone.utc)
        signals: list[Signal] = []
        for c in rows:
            q = self.query_for(c["name"])
            if not q:
                continue          # unsafe to attribute; the gap is honest
            try:
                body, mode = self.http_get(FEED.format(q=quote_plus(q)), retries=0)
            except Exception as exc:  # noqa: BLE001 — one company must not stop the watch
                self.record_error(exc)
                continue
            parsed = feedparser.parse(body)
            for e in parsed.entries[:10]:
                when = _entry_time(e)
                if when and when < cutoff:
                    continue
                link = getattr(e, "link", None)
                title = getattr(e, "title", "") or ""
                if not link or len(title) < 15:
                    continue
                signals.append(Signal(
                    kind="news",
                    observed_at=(when or datetime.now(timezone.utc)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                    url=link,
                    dedupe_key=f"cnews:{c['id']}:{hash(link) & 0xFFFFFFFF:x}",
                    payload={"title": title[:300],
                             "summary": getattr(e, "summary", "")[:600],
                             "source": getattr(getattr(e, "source", None), "title", None),
                             "watched_company_id": c["id"],
                             "query": q, "platform": "google_news"},
                    company_name=c["name"],
                    fetch_mode=mode))
        return signals


def _entry_time(e) -> datetime | None:
    for attr in ("published", "updated"):
        v = getattr(e, attr, None)
        if v:
            try:
                d = parsedate_to_datetime(v)
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None
