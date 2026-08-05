"""GitHub search — open-source velocity as an early technical signal.

Real stars/forks via the public search API (unauthenticated; rate-limited).
Per-repo contributor/commit-velocity detail happens in enrichment (component 04)
only for companies that survive the deterministic filter.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta

from ..models import Signal
from .base import BaseAdapter

SEARCH = ("https://api.github.com/search/repositories?q=created:%3E{date}"
          "+stars:%3E{stars}+topic:{topic}&sort=stars&order=desc&per_page=25")
TOPICS = ["ai", "agents", "llm", "robotics", "security"]


class GithubTrendingAdapter(BaseAdapter):
    name = "github_trending"
    interval_minutes = 720
    min_stars = 100

    def fetch(self, since: datetime) -> list[Signal]:
        date = (datetime.utcnow() - timedelta(days=90)).date().isoformat()
        signals: list[Signal] = []
        ok_any = False
        for topic in TOPICS:
            url = SEARCH.format(date=date, stars=self.min_stars, topic=topic)
            try:
                body, mode = self.http_get(url, headers={"Accept": "application/vnd.github+json"})
                ok_any = True
            except Exception as exc:  # noqa: BLE001
                self.record_error(exc)
                continue
            for repo in json.loads(body).get("items", []):
                signals.append(Signal(
                    kind="repo", observed_at=repo.get("created_at", ""),
                    url=repo.get("html_url"), dedupe_key=f"gh:{repo.get('id')}",
                    payload={"full_name": repo.get("full_name"),
                             "description": repo.get("description"),
                             "stars": repo.get("stargazers_count"),
                             "forks": repo.get("forks_count"),
                             "language": repo.get("language"),
                             "topic": topic,
                             "owner": (repo.get("owner") or {}).get("login")},
                    company_name=(repo.get("owner") or {}).get("login"),
                    fetch_mode=mode))
        if not ok_any and not signals:
            raise RuntimeError("GitHub API unreachable and no snapshot available")
        return signals
