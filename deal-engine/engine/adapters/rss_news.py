"""Component 01 — News & RSS ingest. Template adapter for every other source.

Real feeds (TechCrunch, Axios, Newcomer, Stratechery, Not Boring, The
Generalist, The Diff). Funding events are extracted deterministically with
regex — a model never invents an amount. Entries that aren't funding events are
stored as news signals feeding News Worth Reading and sector detection.
"""
from __future__ import annotations
import re
import time
from datetime import datetime, timezone

import feedparser

from ..models import Signal
from .base import BaseAdapter

AMOUNT_RE = re.compile(
    r"(?:raises|raised|lands|closes|secures|nabs|gets)\s+(?:a\s+)?\$([\d.,]+)\s*"
    r"(million|billion|M|B)\b", re.I)
STAGE_RE = re.compile(r"\b(pre-?seed|seed|series\s+[a-f]|growth)\b", re.I)
LED_BY_RE = re.compile(r"led by ([A-Z][\w.&' -]{2,40}?)(?:,| and | with |\.|;|$)")
COMPANY_RE = re.compile(r"^([A-Z][\w.'&-]*(?:\s+[A-Z][\w.'&-]*){0,3}?)[,\s]+"
                        r"(?:an?\s|the\s|which\s)?.{0,40}?(?:raises|raised|lands|closes|secures|nabs)", re.I)


# Words a headline regex can capture that are never a company name. A bad
# extraction creates a junk pipeline row, so the guard is deliberately strict:
# reject unless the candidate looks like a name.
NOT_A_COMPANY = {
    "a", "an", "the", "this", "that", "it", "he", "she", "they", "we", "i", "you",
    "new", "how", "why", "what", "when", "who", "which", "startup", "startups",
    "company", "firm", "ai", "vc", "us", "uk", "eu", "china", "india", "google",
    "report", "study", "exclusive", "breaking", "update", "natural", "one", "two",
    "first", "former", "ex", "more", "most", "another", "his", "her", "their",
    "my", "our", "some", "everyone", "nobody", "someone", "no", "yes", "not",
}


def clean_company_name(raw: str | None) -> str | None:
    """Return a plausible company name, or None. Prevents headline-regex noise
    ('A', 'Natural', 'This') from becoming pipeline entries."""
    if not raw:
        return None
    name = raw.strip(" \t\"'“”‘’,.;:-—–")
    if len(name) < 3:
        return None
    words = name.split()
    if len(words) > 5:
        return None
    if name.lower() in NOT_A_COMPANY:
        return None
    # a single lowercase-ish common word is almost never a company in a headline
    if len(words) == 1 and words[0].lower() in NOT_A_COMPANY:
        return None
    # must contain at least one capitalised token that isn't a stopword
    if not any(w[:1].isupper() and w.lower() not in NOT_A_COMPANY for w in words):
        return None
    return name


def parse_amount(m: re.Match) -> float:
    val = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower()
    return val * (1e9 if unit.startswith("b") else 1e6)


class RssNewsAdapter(BaseAdapter):
    name = "rss_news"
    interval_minutes = 60

    def fetch(self, since: datetime) -> list[Signal]:
        feeds = self.cfg.get("feeds", [])
        signals: list[Signal] = []
        ok_any = False
        for feed in feeds:
            try:
                body, mode = self.http_get(feed["url"])
                ok_any = True
            except Exception as exc:  # noqa: BLE001
                self.record_error(exc)
                continue
            parsed = feedparser.parse(body)
            for entry in parsed.entries:
                ts = entry.get("published_parsed") or entry.get("updated_parsed")
                if ts:
                    observed = datetime.fromtimestamp(time.mktime(ts), tz=timezone.utc)
                    if observed.replace(tzinfo=timezone.utc) < since.replace(tzinfo=timezone.utc):
                        continue
                    observed_iso = observed.strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    observed_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                title = entry.get("title", "")
                summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))[:2000]
                text = f"{title}. {summary}"
                link = entry.get("link")
                dedupe = f"rss:{entry.get('id', link)}"

                m = AMOUNT_RE.search(text)
                if m:
                    stage_m = STAGE_RE.search(text)
                    led_m = LED_BY_RE.search(text)
                    comp_m = COMPANY_RE.search(title)
                    signals.append(Signal(
                        kind="funding_event", observed_at=observed_iso, url=link,
                        dedupe_key=dedupe,
                        payload={"title": title, "feed": feed["name"],
                                 "feed_kind": feed.get("kind", "mainstream"),
                                 "amount_usd": parse_amount(m),
                                 "stage": stage_m.group(1).lower().replace(" ", "-") if stage_m else None,
                                 "lead_investor": led_m.group(1).strip() if led_m else None,
                                 "summary": summary[:500]},
                        raw=text[:1500],
                        company_name=clean_company_name(comp_m.group(1)) if comp_m else None,
                        fetch_mode=mode))
                else:
                    signals.append(Signal(
                        kind="news", observed_at=observed_iso, url=link, dedupe_key=dedupe,
                        payload={"title": title, "feed": feed["name"],
                                 "feed_kind": feed.get("kind", "mainstream"),
                                 "summary": summary[:500]},
                        raw=text[:1500], fetch_mode=mode))
        if not ok_any and not signals:
            raise RuntimeError("no RSS feed reachable and no snapshots available")
        return signals
