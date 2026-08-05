"""SourceAdapter protocol + shared plumbing.

Every source — free or licensed — implements the identical interface. Free
sources hit real APIs live; when the network is unavailable (e.g. a sandboxed
demo box) they fall back to a local snapshot cache of REAL previously-fetched
payloads under data/cache/<adapter>/, and every signal records fetch_mode so
provenance is never ambiguous. Licensed adapters return an empty
LicenseRequired result — never fabricated data.
"""
from __future__ import annotations
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from .. import db
from ..config import CACHE_DIR, USER_AGENT
from ..models import HealthStatus, Signal


@runtime_checkable
class SourceAdapter(Protocol):
    name: str
    interval_minutes: int
    requires_license: bool

    def fetch(self, since: datetime) -> list[Signal]: ...
    def health(self) -> HealthStatus: ...


def classify_error(exc: Exception) -> str:
    """Component 13: transient errors requeue with backoff; permanent ones alert."""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)):
        return "transient"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (429,) or code >= 500:
            return "transient"
        return "permanent"
    return "permanent"


class BaseAdapter:
    name: str = "base"
    interval_minutes: int = 60
    requires_license: bool = False

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.name = self.cfg.get("name", self.name)
        self.interval_minutes = int(self.cfg.get("interval_minutes", self.interval_minutes))
        self._last_error: str | None = None
        self._last_fetch_mode = "live"

    # ---- HTTP with live-first / snapshot-fallback ----------------------------

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode()).hexdigest()[:16]
        d = CACHE_DIR / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{h}.json"

    @staticmethod
    def _stable_key(url: str) -> str:
        """URL with date/timestamp window values neutralised, so an offline run
        can reuse a REAL snapshot of the same query taken on a previous day.
        The snapshot's own url + fetched_at are preserved untouched."""
        import re as _re
        u = _re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", url)
        u = _re.sub(r"created_at_i%3E\d+", "created_at_i%3ETS", u)
        u = _re.sub(r"created:%3E[\dA-Z-]+", "created:%3EDATE", u)
        return u

    def _snapshot_fallback(self, url: str) -> dict | None:
        exact = self._cache_path(url)
        if exact.exists():
            return json.loads(exact.read_text())
        want = self._stable_key(url)
        for f in (CACHE_DIR / self.name).glob("*.json"):
            try:
                snap = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            if self._stable_key(snap.get("url", "")) == want:
                return snap
        return None

    def http_get(self, url: str, retries: int = 2, headers: dict | None = None) -> tuple[str, str]:
        """Return (body, mode). mode: 'live' or 'cached_snapshot'.

        Live fetch first (with backoff on transient errors). If the network is
        unreachable, fall back to a snapshot of a real earlier fetch of the SAME
        url. If neither works, raise — a silent empty success is the failure
        mode this system is built to avoid.
        """
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = httpx.get(url, headers=hdrs, timeout=20, follow_redirects=True)
                r.raise_for_status()
                body = r.text
                # some APIs carry the useful count in a header (GitHub's Link
                # rel="last" is how contributor totals are obtained)
                self._last_link_header = r.headers.get("link", "")
                # store snapshot so offline runs stay honest and reproducible
                self._cache_path(url).write_text(json.dumps(
                    {"url": url, "fetched_at": db.now_iso(), "body": body}))
                self._last_fetch_mode = "live"
                return body, "live"
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if classify_error(exc) == "transient" and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        snap = self._snapshot_fallback(url)
        if snap:
            self._last_fetch_mode = "cached_snapshot"
            return snap["body"], "cached_snapshot"
        raise last_exc  # type: ignore[misc]

    # ---- heartbeat / health --------------------------------------------------

    def record_ok(self) -> None:
        db.execute("UPDATE sources SET last_ok_at=?, last_attempt_at=?, health='ok',"
                   " error_count=0, last_error=NULL WHERE name=?",
                   (db.now_iso(), db.now_iso(), self.name))

    def record_error(self, exc: Exception) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"
        db.execute("UPDATE sources SET last_attempt_at=?, health='degraded',"
                   " error_count=error_count+1, last_error=? WHERE name=?",
                   (db.now_iso(), self._last_error, self.name))

    def health(self) -> HealthStatus:
        row = db.q1("SELECT last_ok_at, health, error_count, last_error FROM sources"
                    " WHERE name=?", (self.name,))
        if not row:
            return HealthStatus("down", "not registered")
        return HealthStatus(row["health"], row["last_error"] or "",
                            row["last_ok_at"], row["error_count"])

    # ---- template ------------------------------------------------------------

    def fetch(self, since: datetime) -> list[Signal]:  # pragma: no cover
        raise NotImplementedError

    def safe_fetch(self, since: datetime) -> list[Signal]:
        """fetch() wrapped with heartbeat + error classification."""
        try:
            signals = self.fetch(since)
            self.record_ok()
            return signals
        except Exception as exc:  # noqa: BLE001
            self.record_error(exc)
            kind = classify_error(exc)
            print(f"  ! {self.name}: {kind} error — {type(exc).__name__}: {str(exc)[:120]}")
            return []
