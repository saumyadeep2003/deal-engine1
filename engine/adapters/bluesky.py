"""Bluesky — GP attention, from an open protocol instead of a paid API.

The fund's brief wants to know what tracked investors are paying attention to,
because a GP posting about a space is the earliest signal that exists — earlier
than a filing, earlier than press. X is the obvious home for that and costs
~$200/month; scraping it violates their terms and breaks whenever they change
their defences.

Bluesky's AT Protocol exposes the same shape of data through a public read-only
appview: no key, no auth, no terms problem, and a growing share of the technical
and investing world posts there. The watchlist mechanism is handle-based either
way, so this is a substitution, not a redesign — the day an X budget appears,
`licensed.py` already has the adapter.

Honest limitation, recorded rather than hidden: Bluesky's investor population is
smaller than X's. This gives real GP-attention signal, not equivalent coverage.
"""
from __future__ import annotations
import json
import re
from datetime import datetime

from .. import db
from ..config import thesis
from ..models import Signal
from .base import BaseAdapter
from .rss_news import AMOUNT_RE, clean_company_name, parse_amount

# Public appview — read-only, documented, no credentials.
API = "https://public.api.bsky.app/xrpc"
SEARCH = API + "/app.bsky.feed.searchPosts?q={q}&limit={limit}"
AUTHOR_FEED = API + "/app.bsky.feed.getAuthorFeed?actor={actor}&limit={limit}"

# at://did:plc:xxxx/app.bsky.feed.post/RKEY -> the human-readable permalink
URI_RE = re.compile(r"at://(?P<did>[^/]+)/app\.bsky\.feed\.post/(?P<rkey>[^/]+)$")


def post_url(handle: str | None, uri: str | None) -> str | None:
    m = URI_RE.match(uri or "")
    if not m or not handle:
        return None
    return f"https://bsky.app/profile/{handle}/post/{m.group('rkey')}"


class BlueskyAdapter(BaseAdapter):
    name = "bluesky"
    interval_minutes = 240
    requires_license = False

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.handles = self.cfg.get("handles") or []
        self.max_queries = int(self.cfg.get("max_queries", 4))
        self.limit = int(self.cfg.get("limit", 25))

    @staticmethod
    def _keywords() -> list[str]:
        """Short, searchable terms from each theme.

        A theme LABEL is written for a fund's own documents — "Robotics & Physical
        AI" — and nobody posts that phrase. Searching it returned zero results for
        this source's entire first run. The keywords a theme already carries for
        the deterministic filter are exactly the words people do use, so they are
        what gets searched; the label is only a fallback."""
        out: list[str] = []
        for t in thesis().get("themes", []):
            kws = [k for k in (t.get("keywords") or []) if 3 < len(k) < 24]
            out.extend(kws[:2] or [t.get("label", "")])
        return [k for k in out if k]

    def _queries(self) -> list[str]:
        """Thesis themes drive the search, same as every other source, so what the
        engine listens for stays defined in one place (config/thesis.yaml)."""
        templates = self.cfg.get("queries") or ["{keyword} raised"]
        kws = self._keywords()
        qs = [tpl.format(keyword=k, theme=k) for tpl in templates for k in kws]
        return qs[:self.max_queries]

    def fetch(self, since: datetime) -> list[Signal]:
        signals: list[Signal] = []
        seen: set[str] = set()

        # 1. Watched investors: what the fund's tracked GPs are actually posting.
        for handle in self.handles[:20]:
            try:
                body, _ = self.http_get(
                    AUTHOR_FEED.format(actor=handle, limit=self.limit), retries=0)
            except Exception:  # noqa: BLE001 — a dead handle must not stop the rest
                continue
            for item in (json.loads(body) or {}).get("feed", []) or []:
                sig = self._to_signal(item.get("post") or {}, watched_handle=handle)
                if sig and sig.dedupe_key not in seen:
                    seen.add(sig.dedupe_key)
                    signals.append(sig)

        # 2. Thesis-driven search: posts about the fund's themes from anyone.
        for q in self._queries():
            try:
                body, _ = self.http_get(
                    SEARCH.format(q=q.replace(" ", "+"), limit=self.limit), retries=0)
            except Exception:  # noqa: BLE001
                continue
            for post in (json.loads(body) or {}).get("posts", []) or []:
                sig = self._to_signal(post, query=q)
                if sig and sig.dedupe_key not in seen:
                    seen.add(sig.dedupe_key)
                    signals.append(sig)
        return signals

    def probe(self) -> dict:
        """One search against the public appview — no key, no auth, so this is a
        true end-to-end check of the path the adapter actually uses."""
        return self.probe_url(SEARCH.format(q="venture+funding", limit=5), expect="posts")

    def _to_signal(self, post: dict, watched_handle: str | None = None,
                   query: str | None = None) -> Signal | None:
        author = (post.get("author") or {})
        handle = author.get("handle")
        text = ((post.get("record") or {}).get("text") or "").strip()
        url = post_url(handle, post.get("uri"))
        if not text or not url or len(text) < 25:
            return None

        payload = {"author": handle, "author_name": author.get("displayName"),
                   "text": text[:600], "likes": post.get("likeCount"),
                   "reposts": post.get("repostCount"),
                   "watched_gp": bool(watched_handle), "query": query,
                   "platform": "bluesky"}
        kind = "commentary"
        name = None
        # A GP saying "X just raised $20M" is a funding signal like any other —
        # and the amount is parsed by the same regex as SEC/RSS/HN, never a model.
        m = AMOUNT_RE.search(text)
        if m:
            kind = "funding_event"
            payload["amount_usd"] = parse_amount(m)
            payload["amount_text"] = m.group(0)
            head = text.split(m.group(0))[0].strip()
            name = clean_company_name(re.split(r"\b(raises|raised|secures|closes|lands)\b",
                                               head, flags=re.I)[0])
        return Signal(
            kind=kind,
            observed_at=(post.get("indexedAt") or (post.get("record") or {}).get("createdAt")
                         or db.now_iso()),
            url=url,
            dedupe_key=f"bsky:{post.get('cid') or url}",
            payload=payload,
            company_name=name,
            fetch_mode=self._last_fetch_mode,
        )
