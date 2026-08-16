"""GDELT DOC 2.0 — a second, wider news watch on tracked companies. Free, no key.

The Google-News watch (company_news) reads the mainstream press through one
aggregator's lens. GDELT indexes a far broader slice of the world's news —
trade press, regional outlets, non-US coverage — through a documented public
API that exists to be queried (https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/).
For a pipeline where a third of companies have no funding round on record and
filing-only companies have no description, more independent articles per
company is corroboration: two sources saying the same thing is the difference
between "one blog" and a claim a partner can lean on.

Same attribution discipline as company_news, deliberately shared: the SAME
quoted-name query builder and the SAME generic-name refusal, because a wider
net makes wrong attribution MORE likely, not less. A company too generic to
watch on Google News is too generic to watch here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

import json

from .. import db
from ..models import Signal
from .base import BaseAdapter
from .company_news import CompanyNewsAdapter

DOC_API = ("https://api.gdeltproject.org/api/v2/doc/doc"
           "?query={q}&mode=artlist&format=json&maxrecords={n}&timespan={span}")


def parse_seendate(s: str | None) -> str:
    """GDELT's 20260816T093000Z -> ISO. Unparseable -> now (never raises)."""
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return db.now_iso()


def parse_articles(body: str) -> list[dict]:
    """Normalise GDELT's artlist. [] on any unexpected shape — GDELT sometimes
    answers HTML error pages with status 200, and those must read as 'nothing
    observed', not a crash."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for a in (data or {}).get("articles", []) or []:
        if not isinstance(a, dict) or not a.get("url") or not a.get("title"):
            continue
        out.append({"url": a["url"], "title": a["title"],
                    "domain": a.get("domain"), "language": a.get("language"),
                    "seendate": parse_seendate(a.get("seendate")),
                    "sourcecountry": a.get("sourcecountry")})
    return out


class GdeltNewsAdapter(BaseAdapter):
    name = "gdelt_news"
    interval_minutes = 720
    requires_license = False
    max_companies = 30
    max_records = 15         # per company per run — corroboration, not a firehose

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.max_companies = int(self.cfg.get("max_companies", self.max_companies))
        self.max_records = int(self.cfg.get("max_records", self.max_records))

    def fetch(self, since: datetime) -> list[Signal]:
        # Same attention rule as company_news: the companies a partner would act
        # on first — hot, then watchlist, best-ranked first.
        rows = db.q("""SELECT c.id, c.name, c.domain FROM companies c
                       LEFT JOIN scores s ON s.company_id=c.id AND s.id=(
                         SELECT id FROM scores WHERE company_id=c.id
                         ORDER BY scored_at DESC, id DESC LIMIT 1)
                       WHERE c.is_synthetic=0 AND c.status IN ('hot','watchlist')
                       ORDER BY CASE c.status WHEN 'hot' THEN 0 ELSE 1 END,
                                COALESCE(s.percentile, -1) DESC
                       LIMIT ?""", (self.max_companies,))
        signals: list[Signal] = []
        for c in rows:
            q = CompanyNewsAdapter.query_for(c["name"])
            if q is None:
                continue     # too generic to attribute — same refusal, same reason
            url = DOC_API.format(q=quote(q), n=self.max_records, span="14d")
            try:
                body, mode = self.http_get(url, retries=0)
            except Exception:  # noqa: BLE001 — one company's query must not stop the watch
                continue
            for a in parse_articles(body):
                signals.append(Signal(
                    kind="news",
                    observed_at=a["seendate"],
                    url=a["url"],
                    dedupe_key=f"gdelt:{a['url']}",
                    payload={"title": a["title"], "source": a.get("domain"),
                             "feed_kind": "mainstream", "language": a.get("language"),
                             "sourcecountry": a.get("sourcecountry"),
                             "via": "gdelt-doc-2.0"},
                    raw=a["title"],
                    company_name=c["name"], company_domain=c["domain"],
                    fetch_mode=mode))
        return signals

    def probe(self) -> dict:
        return self.probe_url(
            DOC_API.format(q=quote('"OpenAI"'), n=1, span="1d"), expect="")
