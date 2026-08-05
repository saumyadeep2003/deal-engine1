"""Ingestion orchestrator: adapter registry → signals → entity resolution → derived rows.

Signals are immutable; everything else (companies, rounds, investments,
news_items, peer_events) is derived from them and re-derivable.
"""
from __future__ import annotations
import importlib
import json
from datetime import datetime, timedelta, timezone

from . import db, firms, resolution
from .config import sources_config, thesis
from .models import Signal


def load_adapters(only: list[str] | None = None) -> list:
    adapters = []
    for src in sources_config()["sources"]:
        if only and src["name"] not in only:
            continue
        module_name, cls_name = src["adapter"].rsplit(".", 1)
        cls = getattr(importlib.import_module(module_name), cls_name)
        adapters.append(cls(src))
    return adapters


def register_sources() -> None:
    for src in sources_config()["sources"]:
        row = db.q1("SELECT id FROM sources WHERE name=?", (src["name"],))
        if not row:
            db.insert("sources", {
                "name": src["name"], "adapter": src["adapter"],
                "interval_minutes": src.get("interval_minutes", 60),
                "requires_license": 1 if src.get("requires_license") else 0,
                "license_vendor": src.get("license_vendor"),
                "health": "license_required" if src.get("requires_license") else "unknown"})


def _investor_id(name: str) -> int:
    row = db.q1("SELECT id FROM investors WHERE name=?", (name,))
    if row:
        return row["id"]
    # tier comes from the firm dataset / configured tier lists (engine/firms.py)
    rec = firms.match(name)
    return db.insert("investors", {"name": name, "tier": rec["tier"] if rec else None})


def _derive(signal: Signal, signal_id: int, company_id: int | None, source_name: str) -> None:
    p = signal.payload
    if signal.kind == "funding_event" and company_id:
        exists = db.q1("SELECT id FROM funding_rounds WHERE source_signal_id=?", (signal_id,))
        if not exists:
            lead_id = _investor_id(p["lead_investor"]) if p.get("lead_investor") else None
            round_id = db.insert("funding_rounds", {
                "company_id": company_id, "stage": p.get("stage"),
                "amount_usd": p.get("amount_usd"), "valuation_usd": p.get("valuation_usd"),
                "announced_at": signal.observed_at, "lead_investor_id": lead_id,
                "source_signal_id": signal_id})
            investors = list(p.get("investors") or [])
            if p.get("lead_investor"):
                investors.append(p["lead_investor"])
            for inv in set(investors):
                iid = _investor_id(inv)
                try:
                    db.insert("investments", {
                        "investor_id": iid, "company_id": company_id, "round_id": round_id,
                        "is_lead": 1 if inv == p.get("lead_investor") else 0,
                        "announced_at": signal.observed_at, "source_signal_id": signal_id})
                except Exception:
                    pass
            if p.get("stage"):
                db.execute("UPDATE companies SET stage=? WHERE id=? AND (stage IS NULL OR stage='unknown')",
                           (p["stage"], company_id))
    elif signal.kind == "filing" and company_id:
        # Form D company raise → treat as a funding round with the filed amount
        exists = db.q1("SELECT id FROM funding_rounds WHERE source_signal_id=?", (signal_id,))
        if not exists and (p.get("total_offering_usd") or p.get("total_sold_usd")):
            db.insert("funding_rounds", {
                "company_id": company_id, "stage": None,
                "amount_usd": p.get("total_sold_usd") or p.get("total_offering_usd"),
                "announced_at": signal.observed_at, "source_signal_id": signal_id})
    elif signal.kind == "fund_formation" and p.get("known_firm"):
        iid = _investor_id(p["known_firm"])
        exists = db.q1("SELECT id FROM peer_events WHERE source_signal_id=?", (signal_id,))
        if not exists:
            db.insert("peer_events", {
                "investor_id": iid, "event_type": "fund_formation",
                "observed_at": signal.observed_at, "source_signal_id": signal_id})
    elif signal.kind == "news":
        exists = db.q1("SELECT id FROM news_items WHERE signal_id=?", (signal_id,))
        if not exists and p.get("title"):
            db.insert("news_items", {
                "title": p["title"], "url": signal.url,
                "source": p.get("feed") or source_name,
                "published_at": signal.observed_at, "signal_id": signal_id})


def store_signals(source_name: str, signals: list[Signal]) -> dict:
    source_id = db.get_source_id(source_name)
    new, dup = 0, 0
    for s in signals:
        sid = db.insert_signal(source_id, s.kind, s.observed_at, s.payload, s.url,
                               s.dedupe_key, raw=s.raw, fetch_mode=s.fetch_mode)
        if sid is None:
            dup += 1
            continue
        new += 1
        company_id = resolution.resolve(s, sid)
        if company_id:
            db.execute("UPDATE signals SET company_id=? WHERE id=?", (company_id, sid))
            # portable "keep the newer timestamp" (SQLite scalar MAX ≠ PG GREATEST)
            db.execute("UPDATE companies SET last_signal_at=? WHERE id=? AND"
                       " (last_signal_at IS NULL OR last_signal_at < ?)",
                       (s.observed_at, company_id, s.observed_at))
        _derive(s, sid, company_id, source_name)
    return {"new": new, "duplicate": dup}


def run_ingest(lookback_days: int | None = None, only: list[str] | None = None,
               verbose: bool = True) -> dict:
    register_sources()
    days = lookback_days or thesis()["filters"]["lookback_days"]
    since = datetime.now(timezone.utc) - timedelta(days=days)
    totals: dict[str, dict] = {}
    for adapter in load_adapters(only):
        if adapter.requires_license and not getattr(adapter, "licensed", False):
            totals[adapter.name] = {"new": 0, "duplicate": 0,
                                    "skipped": f"LicenseRequired ({getattr(adapter, 'vendor', '?')})"}
            adapter.fetch(since)   # records license_required health honestly
            if verbose:
                print(f"  - {adapter.name}: skipped — requires "
                      f"{getattr(adapter, 'vendor', 'license')} (interface wired, no key)")
            continue
        signals = adapter.safe_fetch(since)
        stats = store_signals(adapter.name, signals)
        stats["fetched"] = len(signals)
        totals[adapter.name] = stats
        if verbose:
            print(f"  - {adapter.name}: {len(signals)} fetched, {stats['new']} new, "
                  f"{stats['duplicate']} already ingested")
    return totals
