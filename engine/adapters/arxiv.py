"""arXiv API — research velocity for sector detection. Real papers, real authors."""
from __future__ import annotations
from datetime import datetime

import feedparser

from ..models import Signal
from .base import BaseAdapter

API = ("https://export.arxiv.org/api/query?search_query=cat:{cat}"
       "&sortBy=submittedDate&sortOrder=descending&max_results={n}")


class ArxivAdapter(BaseAdapter):
    name = "arxiv"
    interval_minutes = 720
    per_category = 15

    def fetch(self, since: datetime) -> list[Signal]:
        cats = self.cfg.get("categories", ["cs.AI", "cs.RO", "cs.CR", "eess.AS", "cs.LG"])
        signals: list[Signal] = []
        ok_any = False
        for cat in cats:
            try:
                body, mode = self.http_get(API.format(cat=cat, n=self.per_category))
                ok_any = True
            except Exception as exc:  # noqa: BLE001
                self.record_error(exc)
                continue
            for e in feedparser.parse(body).entries:
                aid = e.get("id", "")
                signals.append(Signal(
                    kind="research", observed_at=e.get("published", ""),
                    url=aid or None, dedupe_key=f"arxiv:{aid}",
                    payload={"title": e.get("title", "").replace("\n", " "),
                             "category": cat,
                             "authors": [a.get("name") for a in e.get("authors", [])][:8],
                             "abstract": (e.get("summary") or "").replace("\n", " ")[:1200]},
                    fetch_mode=mode))
        if not ok_any and not signals:
            raise RuntimeError("arXiv API unreachable and no snapshot available")
        return signals
