"""Reddit public JSON — investor/operator commentary (component 07 input).

Free but fragile: Reddit rate-limits unauthenticated clients aggressively.
Degrades to snapshot cache or an honest empty result — never fabricated posts.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from ..models import Signal
from .base import BaseAdapter

import os

URL = "https://www.reddit.com/r/{sub}/new.json?limit=50"
OAUTH_URL = "https://oauth.reddit.com/r/{sub}/new?limit=50"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"


class RedditAdapter(BaseAdapter):
    """Reddit returned zero for this deployment's entire life.

    The public JSON endpoint works from a laptop and is blocked from data-centre
    IPs — Render, AWS, anywhere this would actually run. It failed honestly
    (health degraded, no invented posts) but the assignment names Reddit three
    times as a commentary source, and an honest zero is still a zero.

    So an authenticated route is used when credentials exist. Reddit's app-only
    OAuth is free, needs no user account linkage, and is not rate-limited the way
    anonymous access is. Without credentials it falls back to the public endpoint
    and, when that is blocked, says which of the two it tried."""

    name = "reddit"
    interval_minutes = 240
    _token: str | None = None

    def _oauth_token(self) -> str | None:
        cid = os.environ.get("REDDIT_CLIENT_ID")
        secret = os.environ.get("REDDIT_CLIENT_SECRET")
        if not (cid and secret):
            return None
        if self._token:
            return self._token
        try:
            import httpx
            from ..config import USER_AGENT
            r = httpx.post(TOKEN_URL, auth=(cid, secret),
                           data={"grant_type": "client_credentials"},
                           headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
            self._token = r.json().get("access_token")
            return self._token
        except Exception:  # noqa: BLE001 — fall back to the public endpoint
            return None

    def fetch(self, since: datetime) -> list[Signal]:
        subs = self.cfg.get("subreddits", ["startups", "MachineLearning", "venturecapital"])
        signals: list[Signal] = []
        token = self._oauth_token()
        for sub in subs:
            try:
                if token:
                    body, mode = self.http_get(
                        OAUTH_URL.format(sub=sub),
                        headers={"Authorization": f"Bearer {token}"})
                else:
                    body, mode = self.http_get(URL.format(sub=sub))
            except Exception as exc:  # noqa: BLE001
                self.record_error(exc)
                continue
            for child in json.loads(body).get("data", {}).get("children", []):
                d = child.get("data", {})
                created = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
                if created < since.replace(tzinfo=timezone.utc):
                    continue
                signals.append(Signal(
                    kind="commentary", observed_at=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    url=f"https://www.reddit.com{d.get('permalink', '')}",
                    dedupe_key=f"reddit:{d.get('id')}",
                    payload={"subreddit": sub, "title": d.get("title"),
                             "author": d.get("author"), "score": d.get("score"),
                             "num_comments": d.get("num_comments"),
                             "selftext": (d.get("selftext") or "")[:1200]},
                    fetch_mode=mode))
        return signals  # empty is acceptable here; health row records degradation

    def probe(self) -> dict:
        """Report WHICH route was used, because "reddit: 0 items" is the same
        output whether the subreddit was quiet or the IP was blocked."""
        token = self._oauth_token()
        route = "authenticated (REDDIT_CLIENT_ID set)" if token else "anonymous public JSON"
        try:
            res = super().probe()
            res["detail"] = f"{route} — {res.get('detail')}"
            if not token:
                res["hint"] = ("Anonymous Reddit is blocked from data-centre IPs. Create a "
                               "free 'script' app at reddit.com/prefs/apps and set "
                               "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET.")
            return res
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"{route} failed: {type(exc).__name__}: {exc}"[:200],
                    "hint": ("Reddit blocks anonymous requests from hosted IPs. Set "
                             "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (free) to use the "
                             "authenticated route.") if not token else None}
