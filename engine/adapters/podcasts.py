"""Podcast episodes as commentary — metadata on Render, transcripts locally.

What GPs say on podcasts is the commentary the engine cannot get from X without
a paid tier — and podcast RSS is free, public, and built to be read. This
adapter runs in two layers with different costs:

* THIS adapter (runs everywhere, including the 512MB box): reads the configured
  shows' RSS feeds and emits a `commentary` signal whenever an episode's title
  or show notes mention a tracked company by name. Cheap, honest, attributed —
  show notes are the podcast's own words about its own episode.
* `scripts/transcribe_podcasts.py` (run LOCALLY — transcription does not fit the
  free tier): downloads recent episodes, transcribes with faster-whisper, and
  stores the sentences around each tracked-company mention as deeper commentary
  signals, pointed at the same database. The adapter never depends on it; the
  script only ever adds.

Attribution discipline is the news watches', shared deliberately: a company too
generic to search for safely is too generic to match in show notes, and the
match is against the stripped name with word boundaries — "Scale" appearing
inside "scaling" is not a mention.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from .. import db
from ..models import Signal
from .base import BaseAdapter
from .company_news import CompanyNewsAdapter


def tracked_names(limit: int = 120) -> list[dict]:
    """Companies worth listening for, with their safe match patterns."""
    rows = db.q("""SELECT c.id, c.name, c.domain FROM companies c
                   WHERE c.is_synthetic=0 AND c.status IN ('hot','watchlist')
                   ORDER BY c.last_signal_at DESC LIMIT ?""", (limit,))
    out = []
    for r in rows:
        if CompanyNewsAdapter.query_for(r["name"]) is None:
            continue                       # too generic to attribute — same bar
        base = r["name"].strip().rstrip(".")
        for suffix in (", Inc", " Inc", " LLC", " Ltd", " Corp", " Corporation"):
            if base.lower().endswith(suffix.lower()):
                base = base[: -len(suffix)].rstrip(",. ")
        out.append({"id": r["id"], "name": r["name"], "domain": r["domain"],
                    "pattern": re.compile(rf"\b{re.escape(base)}\b", re.I)})
    return out


def mention_snippet(text: str, pattern: re.Pattern, window: int = 240) -> str | None:
    m = pattern.search(text or "")
    if not m:
        return None
    lo = max(0, m.start() - window // 2)
    return text[lo: m.end() + window // 2].strip()


def parse_feed(body: str, show_name: str) -> list[dict]:
    """Episodes out of one RSS body. [] on anything unexpected."""
    parsed = feedparser.parse(body)
    out = []
    for e in (parsed.entries or [])[:25]:
        title = getattr(e, "title", None)
        if not title:
            continue
        summary = re.sub(r"<[^>]+>", " ", getattr(e, "summary", "") or "")[:4000]
        published = getattr(e, "published", None)
        try:
            when = parsedate_to_datetime(published).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            when = db.now_iso()
        audio = None
        for enc in (getattr(e, "enclosures", None) or []):
            href = enc.get("href") if isinstance(enc, dict) else getattr(enc, "href", None)
            if href:
                audio = href
                break
        out.append({"title": title, "summary": summary, "published": when,
                    "link": getattr(e, "link", None), "audio_url": audio,
                    "show": show_name})
    return out


class PodcastsAdapter(BaseAdapter):
    name = "podcast_notes"
    interval_minutes = 1440
    requires_license = False

    def fetch(self, since: datetime) -> list[Signal]:
        feeds = self.cfg.get("feeds") or []
        watch = tracked_names()
        signals: list[Signal] = []
        for feed in feeds:
            try:
                body, mode = self.http_get(feed["url"], retries=0)
            except Exception:  # noqa: BLE001 — one dead feed must not stop the rest
                continue
            for ep in parse_feed(body, feed.get("name", feed["url"])):
                blob = f"{ep['title']} {ep['summary']}"
                for c in watch:
                    snip = mention_snippet(blob, c["pattern"])
                    if not snip:
                        continue
                    signals.append(Signal(
                        kind="commentary",
                        observed_at=ep["published"],
                        url=ep["link"] or feed["url"],
                        dedupe_key=f"podcast:{ep['show']}:{ep['title'][:80]}:{c['id']}",
                        payload={"quote": snip, "episode": ep["title"],
                                 "show": ep["show"], "audio_url": ep["audio_url"],
                                 "via": "podcast show notes",
                                 "note": "episode metadata only — run scripts/"
                                         "transcribe_podcasts.py locally for the "
                                         "full-transcript version"},
                        raw=snip,
                        company_name=c["name"], company_domain=c["domain"],
                        fetch_mode=mode))
        return signals

    def probe(self) -> dict:
        feeds = self.cfg.get("feeds") or []
        if not feeds:
            return {"ok": False, "detail": "no podcast feeds configured in "
                                           "config/sources.yaml (feeds: [{name, url}])"}
        return self.probe_url(feeds[0]["url"], expect="")
