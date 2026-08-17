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

from . import fetching
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
        # Which transport made the last live fetch (httpx / scrapling-http /
        # scrapling-stealth / scrapling-dynamic). Recorded so a page a stealth
        # browser cleared is never presented as an ordinary fetch. An adapter can
        # request a specific engine for its HTML — e.g. a JS-heavy company site —
        # via `fetch_engine:` in config/sources.yaml; unavailable engines
        # downgrade rather than fail (see engine/adapters/fetching.py).
        self._last_fetch_engine = "httpx"
        self._fetch_engine = self.cfg.get("fetch_engine")

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

    def http_get(self, url: str, retries: int = 2, headers: dict | None = None,
                 engine: str | None = None) -> tuple[str, str]:
        """Return (body, mode). mode: 'live' or 'cached_snapshot'.

        Live fetch first (with backoff on transient errors). If the network is
        unreachable, fall back to a snapshot of a real earlier fetch of the SAME
        url. If neither works, raise — a silent empty success is the failure
        mode this system is built to avoid.

        The transport is chosen by `engine` (or the adapter's `fetch_engine`
        config, or the global SCRAPLING_MODE) in engine/adapters/fetching.py: an
        upgraded fetcher clears the 403s and JS shells that made company and
        careers pages come back empty. The (body, mode) contract is unchanged, so
        every caller and the snapshot cache keep working; which engine actually
        fetched is recorded on self._last_fetch_engine for provenance.
        """
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        want_engine = engine or self._fetch_engine
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                body, status, link, used = fetching.raw_fetch(
                    url, hdrs, timeout=20, follow_redirects=True, engine=want_engine)
                if status >= 400:
                    # rebuild a real httpx error so classify_error still decides
                    # transient (429/5xx -> retry) vs permanent (403/404 -> stop)
                    req = httpx.Request("GET", url)
                    raise httpx.HTTPStatusError(
                        f"{status} for {url}", request=req,
                        response=httpx.Response(status, request=req))
                # some APIs carry the useful count in a header (GitHub's Link
                # rel="last" is how contributor totals are obtained)
                self._last_link_header = link
                # store snapshot so offline runs stay honest and reproducible
                self._cache_path(url).write_text(json.dumps(
                    {"url": url, "fetched_at": db.now_iso(), "body": body,
                     "engine": used}))
                self._last_fetch_mode = "live"
                self._last_fetch_engine = used
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
            self._last_fetch_engine = snap.get("engine", "httpx")
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

    # ---- live probe ----------------------------------------------------------

    def probe(self) -> dict:
        """One real request, right now, so 'is this source live?' can be answered
        by pressing something rather than by trusting the last scheduled run.

        The default runs a short-window fetch, which is the most honest test
        available: it exercises the exact code the pipeline uses. Adapters whose
        fetch is expensive (one that walks every tracked company, say) override
        this with a single representative call — the point is proof of
        reachability, not a second ingest.

        `cached_snapshot` is reported rather than hidden. A source answering from
        the offline cache is *working*, but it is not live, and those are
        different facts a partner is entitled to tell apart."""
        t = time.time()
        signals = self.fetch(self.probe_since())
        mode = self._last_fetch_mode
        via = (f" via {self._last_fetch_engine}"
               if self._last_fetch_engine and self._last_fetch_engine != "httpx" else "")
        return {"ok": True, "seconds": round(time.time() - t, 1), "fetch_mode": mode,
                "fetch_engine": self._last_fetch_engine,
                "detail": f"{len(signals)} item(s) returned"
                          + (" from the offline snapshot cache, not live"
                             if mode == "cached_snapshot" else " live") + via}

    @staticmethod
    def probe_since() -> datetime:
        from datetime import timedelta, timezone as _tz
        return datetime.now(_tz.utc) - timedelta(days=2)

    def probe_url(self, url: str, expect: str = "") -> dict:
        """Helper for adapters that override probe(): fetch one URL and report."""
        t = time.time()
        body, mode = self.http_get(url, retries=0)
        ok = (expect in body) if expect else bool(body)
        return {"ok": ok, "seconds": round(time.time() - t, 1), "fetch_mode": mode,
                "detail": (f"{len(body)} bytes from {url.split('/')[2]}"
                           + (" (offline snapshot, not live)"
                              if mode == "cached_snapshot" else " live")
                           + ("" if ok else f" — expected '{expect}' in the response"))}

    def safe_fetch(self, since: datetime) -> list[Signal]:
        """fetch() wrapped with heartbeat + error classification.

        An adapter that RETURNED signals but wants its health degraded anyway
        sets self._force_degraded to a reason string — the case where every
        response came from the offline snapshot cache: data flows, but it is
        stale, and a green light over stale data is how EDGAR sat frozen for
        days reading as healthy."""
        self._force_degraded = None
        try:
            signals = self.fetch(since)
            if getattr(self, "_force_degraded", None):
                self.record_error(RuntimeError(self._force_degraded))
                print(f"  ! {self.name}: DEGRADED — {self._force_degraded}")
            else:
                self.record_ok()
            return signals
        except Exception as exc:  # noqa: BLE001
            self.record_error(exc)
            kind = classify_error(exc)
            print(f"  ! {self.name}: {kind} error — {type(exc).__name__}: {str(exc)[:120]}")
            return []
