"""Shared shapes: the common Signal every adapter emits, and health status."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Signal:
    """One observation from one source. Immutable once stored."""
    kind: str                      # funding_event | filing | launch | news | research | repo | commentary | hiring | fund_formation
    observed_at: str               # ISO timestamp of the underlying event
    url: str | None                # real, fetchable provenance URL
    dedupe_key: str                # stable per-source id → idempotent ingest
    payload: dict = field(default_factory=dict)
    raw: str | None = None
    # entity-resolution hints
    company_name: str | None = None
    company_domain: str | None = None
    fetch_mode: str = "live"       # live | cached_snapshot


@dataclass
class HealthStatus:
    status: str                    # ok | degraded | license_required | down
    detail: str = ""
    last_ok_at: str | None = None
    error_count: int = 0


class LicenseRequired(Exception):
    """Raised/returned by adapters whose upstream needs a paid contract."""
    def __init__(self, vendor: str):
        self.vendor = vendor
        super().__init__(f"requires {vendor}")
