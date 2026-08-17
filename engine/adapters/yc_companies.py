"""Y Combinator companies — launch-stage discovery from a free public API.

yc-oss/api (https://github.com/yc-oss/api) republishes YC's own public company
directory as static JSON — no key, no scraping, no ToS question: the data exists
to be read. What it gives this engine is the exact population its other sources
are weakest on: companies at the moment of formation, BEFORE a Form D, with the
two fields the rest of the pipeline works hardest to reconstruct handed over for
free — a website (the domain resolver's whole job) and a one-liner written by
the company itself (the profile writer's whole job).

Scope discipline: only the newest batches (they are the launch signal; a 2016
alum is not a discovery), fetched per-batch rather than the 5MB all-companies
file — this box has 512MB and a memory history (BUILD_LOG 83). The engine's own
deterministic filter decides thesis relevance downstream, same as every source:
this adapter reports what YC lists, it does not pre-judge it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .. import db
from ..models import Signal
from .base import BaseAdapter

META_URL = "https://yc-oss.github.io/api/meta.json"
BATCH_URL = "https://yc-oss.github.io/api/batches/{slug}.json"


def domain_of(website: str | None) -> str | None:
    if not website:
        return None
    m = re.match(r"https?://(?:www\.)?([^/]+)", website.strip().lower())
    return m.group(1) if m else None


def parse_meta_batches(body: str, max_batches: int) -> list[str]:
    """Newest batch slugs first. Returns [] on any unexpected shape — a schema
    drift upstream must read as 'nothing observed', never crash a run."""
    try:
        meta = json.loads(body)
        batches = meta.get("batches") or {}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []
    # The live meta.json keys batches BY slug ({"spring-2026": {name, count, api}});
    # the first cut assumed a list of {slug: ...} dicts and silently iterated the
    # KEYS as if they were entries — 0 batches, 0 companies, health green (the
    # first live run caught it: "0 new item(s)" in 0.3s). Accept both shapes so a
    # future upstream change back to a list also parses.
    slugs: list[str] = []
    if isinstance(batches, dict):
        slugs = [k for k in batches.keys() if isinstance(k, str)]
    elif isinstance(batches, list):
        slugs = [b.get("slug") for b in batches
                 if isinstance(b, dict) and b.get("slug")]
    # meta lists newest first; trust the order but guard against reversal by
    # sorting season-year slugs when they parse
    def _key(s: str) -> tuple:
        m = re.match(r"(winter|spring|summer|fall)-(\d{4})", s)
        season_rank = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}
        return (int(m.group(2)), season_rank[m.group(1)]) if m else (0, 0)
    slugs.sort(key=_key, reverse=True)
    return slugs[:max_batches]


def parse_batch(body: str) -> list[dict]:
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for c in (data if isinstance(data, list) else []):
        if not isinstance(c, dict) or not c.get("name"):
            continue
        out.append({
            "name": c.get("name"), "slug": c.get("slug"),
            "website": c.get("website"), "one_liner": c.get("one_liner"),
            "long_description": (c.get("long_description") or "")[:600],
            "batch": c.get("batch"), "status": c.get("status"),
            "team_size": c.get("team_size"), "tags": c.get("tags") or [],
            "industries": c.get("industries") or [],
            "locations": c.get("all_locations"),
            "launched_at": c.get("launched_at"),
            "yc_url": c.get("url"),
        })
    return out


class YcCompaniesAdapter(BaseAdapter):
    name = "yc_companies"
    interval_minutes = 1440
    requires_license = False
    max_batches = 3          # the current + two previous batches; config-overridable

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.max_batches = int(self.cfg.get("max_batches", self.max_batches))

    def fetch(self, since: datetime) -> list[Signal]:
        meta_body, _ = self.http_get(META_URL)
        slugs = parse_meta_batches(meta_body, self.max_batches)
        signals: list[Signal] = []
        for slug in slugs:
            try:
                body, mode = self.http_get(BATCH_URL.format(slug=slug))
            except Exception:  # noqa: BLE001 — one missing batch file is not an outage
                continue
            for c in parse_batch(body):
                launched = c.get("launched_at")
                observed = (datetime.fromtimestamp(launched, tz=timezone.utc).isoformat()
                            if isinstance(launched, (int, float)) and launched > 0
                            else db.now_iso())
                text = " — ".join(x for x in (c["one_liner"], c["long_description"]) if x)
                signals.append(Signal(
                    kind="launch",
                    observed_at=observed,
                    url=c.get("yc_url"),
                    dedupe_key=f"yc:{c.get('slug') or c['name']}",
                    payload={"title": f"{c['name']} (YC {c.get('batch')})",
                             "summary": text[:500],
                             "batch": c.get("batch"), "status": c.get("status"),
                             "team_size": c.get("team_size"), "tags": c.get("tags"),
                             "industries": c.get("industries"),
                             "locations": c.get("locations"),
                             "website": c.get("website")},
                    raw=f"{c['name']} — {text}"[:800],
                    company_name=c["name"],
                    company_domain=domain_of(c.get("website")),
                    fetch_mode=mode))
        return signals

    def probe(self) -> dict:
        return self.probe_url(META_URL, expect="batches")
