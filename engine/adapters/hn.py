"""Hacker News via the Algolia API — funding/launch stories + engineer sentiment.

Story ingest lives here; per-company comment harvesting is in
engine/commentary.py (component 07), which also uses this API.
"""
from __future__ import annotations
import json
from datetime import datetime
from urllib.parse import quote

from ..models import Signal
from .base import BaseAdapter

SEARCH = ("https://hn.algolia.com/api/v1/search_by_date?query={q}&tags=story"
          "&hitsPerPage={n}&numericFilters=created_at_i%3E{ts}")

QUERIES = ['"raises"', '"Series A"', '"Series B"', '"seed round"', '"funding"',
           '"launches"', '"startup"', '"acquires"', '"open source"', '"robotics"',
           '"defense"']
SHOW_HN = ("https://hn.algolia.com/api/v1/search_by_date?tags=show_hn"
           "&hitsPerPage={n}&numericFilters=created_at_i%3E{ts}")


class HackerNewsAdapter(BaseAdapter):
    name = "hn"
    interval_minutes = 120
    per_query = 20

    def fetch(self, since: datetime) -> list[Signal]:
        # day-rounded so request URLs are stable within a day (cache-friendly)
        from datetime import time as dtime, timezone as tz
        ts = int(datetime.combine(since.date(), dtime.min, tzinfo=tz.utc).timestamp())
        signals: list[Signal] = []
        seen: set[str] = set()
        urls = [SEARCH.format(q=quote(q), n=self.per_query, ts=ts) for q in QUERIES]
        urls.append(SHOW_HN.format(n=self.per_query, ts=ts))
        ok_any = False
        for url in urls:
            try:
                body, mode = self.http_get(url)
                ok_any = True
            except Exception as exc:  # noqa: BLE001
                self.record_error(exc)
                continue
            for hit in json.loads(body).get("hits", []):
                oid = hit.get("objectID")
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                title = hit.get("title") or ""
                is_show = "show_hn" in (hit.get("_tags") or [])
                from .rss_news import AMOUNT_RE, STAGE_RE, parse_amount
                m = AMOUNT_RE.search(title)
                kind = "launch" if is_show else ("funding_event" if m else "news")
                extra = {}
                if m:
                    stage_m = STAGE_RE.search(title)
                    extra = {"amount_usd": parse_amount(m),
                             "stage": stage_m.group(1).lower().replace(" ", "-") if stage_m else None}
                signals.append(Signal(
                    kind=kind,
                    observed_at=hit.get("created_at", ""),
                    url=f"https://news.ycombinator.com/item?id={oid}",
                    dedupe_key=f"hn:{oid}",
                    payload={"title": title, "points": hit.get("points"),
                             "num_comments": hit.get("num_comments"),
                             "external_url": hit.get("url"),
                             "author": hit.get("author"),
                             "story_text": (hit.get("story_text") or "")[:1000], **extra},
                    raw=title,
                    company_name=self._company_from_title(title, is_show),
                    fetch_mode=mode))
        if not ok_any and not signals:
            raise RuntimeError("HN Algolia unreachable and no snapshot available")
        return signals

    @staticmethod
    def _company_from_title(title: str, is_show: bool) -> str | None:
        import re
        from .rss_news import clean_company_name
        if is_show:
            m = re.match(r"Show HN:\s*([A-Z][\w.-]*(?:\s+[A-Z][\w.-]*){0,2})", title)
            return clean_company_name(m.group(1)) if m else None
        m = re.match(r"^([A-Z][\w.'&-]*(?:\s+[A-Z][\w.'&-]*){0,3}?)\s+[Rr]aises\b", title)
        return clean_company_name(m.group(1)) if m else None
