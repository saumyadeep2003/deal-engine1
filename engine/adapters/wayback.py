"""Wayback Machine — team growth from a company's own archived pages.

Coresignal's headcount is bought for one question: is this team growing? The
Internet Archive answers a version of it for free. A company's /team or /about
page lists its people; the archive holds dated snapshots of that page; the
difference between an old snapshot and a recent one is a growth measurement
taken from the company's own words, with two citable URLs.

The counting method matters, so it is stated rather than buried: team pages
almost always link each person's profile, so the number of distinct
`linkedin.com/in/...` links on the page is a robust proxy for team size. Note
what that is and is not — this reads an ARCHIVED COPY of the company's own page.
It does not touch LinkedIn, scrape it, or require its permission.

Confidence is deliberately low (0.4). A redesigned page, a "leadership only"
section or a page that never listed people will all mislead this metric, so it
is reported with its evidence and never presented as a measured headcount.
"""
from __future__ import annotations
import json
import re
from datetime import datetime

from .. import db
from ..models import Signal
from .base import BaseAdapter

CDX = ("http://web.archive.org/cdx/search/cdx?url={url}&output=json"
       "&fl=timestamp,original&filter=statuscode:200&collapse=timestamp:6&limit={limit}")
SNAPSHOT = "http://web.archive.org/web/{ts}/{url}"

TEAM_PATHS = ("team", "about", "about-us", "company", "people")
PROFILE_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9._-]{3,})", re.I)


def count_people(html: str) -> int:
    """Distinct profile links on the page. Distinct matters: a nav bar or footer
    repeating one link must not read as a hire."""
    return len({m.group(1).lower().rstrip("/") for m in PROFILE_RE.finditer(html or "")})


class WaybackAdapter(BaseAdapter):
    name = "wayback_team"
    interval_minutes = 10080          # weekly: archives do not change hourly
    requires_license = False

    def fetch(self, since: datetime) -> list[Signal]:
        limit = int(self.cfg.get("max_companies", 10))
        rows = db.q("""SELECT c.id, c.name, c.domain FROM companies c
                       JOIN scores s ON s.company_id=c.id
                       WHERE s.id=(SELECT id FROM scores WHERE company_id=c.id
                                   ORDER BY scored_at DESC, id DESC LIMIT 1)
                       AND c.is_synthetic=0 AND c.domain IS NOT NULL
                       AND c.status IN ('hot','watchlist')
                       ORDER BY s.percentile DESC LIMIT ?""", (limit,))
        out: list[Signal] = []
        for c in rows:
            res = self.team_trend(c["domain"])
            if not res:
                continue
            out.append(Signal(
                kind="hiring",
                observed_at=db.now_iso(),
                url=res["latest_url"],
                dedupe_key=f"wayback:{c['domain']}:{res['latest_ts']}",
                payload={**res, "method": "distinct profile links on the company's own "
                                          "archived team page", "platform": "wayback"},
                company_name=c["name"], company_domain=c["domain"],
                fetch_mode=self._last_fetch_mode))
        return out

    def probe(self) -> dict:
        """One CDX query against a domain the archive certainly holds. Testing
        with a tracked company would conflate 'the archive is down' with 'this
        company was never archived' — only the first is a fault."""
        return self.probe_url(CDX.format(url="example.com", limit=2))

    def team_trend(self, domain: str) -> dict | None:
        """Oldest vs newest archived snapshot of a team page. None when the archive
        has too little to say — an absent measurement, not a zero."""
        for path in TEAM_PATHS:
            target = f"{domain}/{path}"
            try:
                body, _ = self.http_get(CDX.format(url=target, limit=20), retries=0)
                snaps = json.loads(body or "[]")
            except Exception:  # noqa: BLE001
                continue
            rows = [r for r in snaps[1:] if len(r) >= 2] if snaps else []
            if len(rows) < 2:
                continue
            first, last = rows[0], rows[-1]
            try:
                old_html, _ = self.http_get(SNAPSHOT.format(ts=first[0], url=first[1]), retries=0)
                new_html, _ = self.http_get(SNAPSHOT.format(ts=last[0], url=last[1]), retries=0)
            except Exception:  # noqa: BLE001
                continue
            old_n, new_n = count_people(old_html), count_people(new_html)
            if old_n == 0 and new_n == 0:
                continue                      # the page never listed people this way
            return {"people_then": old_n, "people_now": new_n,
                    "change": new_n - old_n,
                    "from_date": _ts_to_iso(first[0]), "to_date": _ts_to_iso(last[0]),
                    "earliest_url": SNAPSHOT.format(ts=first[0], url=first[1]),
                    "latest_url": SNAPSHOT.format(ts=last[0], url=last[1]),
                    "latest_ts": last[0], "page": path,
                    "confidence": 0.4,
                    "caveat": "counted from archived copies of the company's own team "
                              "page; a redesign or a leadership-only section will skew it"}
        return None


def _ts_to_iso(ts: str) -> str | None:
    """Wayback timestamps are YYYYMMDDhhmmss."""
    try:
        return datetime.strptime(str(ts)[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None
