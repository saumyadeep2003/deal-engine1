"""Reddit public JSON — investor/operator commentary (component 07 input).

Free but fragile: Reddit rate-limits unauthenticated clients aggressively.
Degrades to snapshot cache or an honest empty result — never fabricated posts.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from ..models import Signal
from .base import BaseAdapter

URL = "https://www.reddit.com/r/{sub}/new.json?limit=50"


class RedditAdapter(BaseAdapter):
    name = "reddit"
    interval_minutes = 240

    def fetch(self, since: datetime) -> list[Signal]:
        subs = self.cfg.get("subreddits", ["startups", "MachineLearning", "venturecapital"])
        signals: list[Signal] = []
        for sub in subs:
            try:
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
