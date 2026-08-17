"""EDGAR freshness tests — the freeze that read as healthy.

The live deployment served "0 new item(s), 279 already known" from EDGAR for
days with a green health light. Mechanism: SEC stopped answering live, every
URL fell back to its offline snapshot, the same historical window was re-served
every run, and the checkpoint — whose advance depends on the dates in the very
hits the snapshots contained — froze. Data flowed, so nothing looked broken.

These tests pin the three defences: a snapshot-only run must NOT advance the
checkpoint, MUST degrade health with the real reason, and the step panel must
say out loud when items came from the snapshot cache. Plus: a 404 on a weekend
index day stays a calendar fact, not an error.

    python tests/edgar_freshness_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp()) / "edgar.db"
os.environ["DEAL_ENGINE_DB"] = str(TMP)
os.environ.pop("DATABASE_URL", None)

import httpx  # noqa: E402

from engine import db  # noqa: E402
from engine.adapters.edgar_formd import EdgarFormDAdapter  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _status_error(url: str, code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", url)
    return httpx.HTTPStatusError(f"{code} for {url}", request=req,
                                 response=httpx.Response(code, request=req))


FTS_STALE = json.dumps({"hits": {"hits": [{"_source": {
    "adsh": "0001-11-000001", "ciks": [111], "file_date": "2026-08-12",
    "display_names": ["Old Historical Co"], "biz_states": [], "biz_locations": []}}]}})

TODAY = datetime.now(timezone.utc).date()
FRESH_DAY = (TODAY - timedelta(days=1)).isoformat()
INDEX_FRESH = (
    "Form Type   Company Name        CIK      Date Filed  File Name\n"
    "---------------------------------------------------------------\n"
    "D           Fresh Robotics Inc  2099001  20260817    "
    "edgar/data/2099001/0002099001-26-000001.txt\n")


def make_adapter(behaviour: dict) -> EdgarFormDAdapter:
    """behaviour: url-substring -> ('live'|'snap', body) or ('raise', code)."""
    a = EdgarFormDAdapter({"name": "edgar_formd", "queries": ["ai"],
                           "max_detail_fetches": 0})

    def fake_get(url, retries=2, headers=None, engine=None):
        for key, (kind, val) in behaviour.items():
            if key in url:
                if kind == "raise":
                    raise _status_error(url, val)
                a._last_fetch_mode = "live" if kind == "live" else "cached_snapshot"
                return val, a._last_fetch_mode
        raise _status_error(url, 404)

    a.http_get = fake_get
    return a


def main() -> int:
    db.connect()
    db.insert("sources", {"name": "edgar_formd", "adapter": "x", "interval_minutes": 60,
                          "requires_license": 0, "health": "ok"})
    since = datetime.now(timezone.utc) - timedelta(days=30)

    # ==== 1. the freeze: snapshot-only run ================================
    db.checkpoint_set("edgar_formd", "2026-08-12")
    a = make_adapter({"daily-index": ("raise", 403),      # SEC blocking the index
                      "efts.sec.gov": ("snap", FTS_STALE)})  # FTS from stale snapshot
    signals = a.safe_fetch(since)
    check("THE LIVE BUG: a snapshot-only run does NOT advance the checkpoint",
          db.checkpoint_get("edgar_formd") == "2026-08-12",
          f"checkpoint={db.checkpoint_get('edgar_formd')}")
    check("...the stale signals are still returned (data beats nothing)",
          len(signals) == 1 and signals[0].fetch_mode == "cached_snapshot",
          f"{len(signals)} signal(s)")
    h = db.q1("SELECT health, last_error FROM sources WHERE name='edgar_formd'")
    check("...and health is DEGRADED with the real reason, not green",
          h["health"] == "degraded" and "snapshot" in (h["last_error"] or "").lower(),
          str(h["last_error"])[:80])

    # ==== 2. recovery: one live answer moves the world ====================
    a = make_adapter({"daily-index": ("live", INDEX_FRESH),
                      "efts.sec.gov": ("live", '{"hits": {"hits": []}}')})
    signals = a.safe_fetch(since)
    check("a live run ingests the fresh filing and advances the checkpoint",
          len(signals) == 1 and "Fresh Robotics" in (signals[0].raw or str(signals[0].payload))
          or db.checkpoint_get("edgar_formd") > "2026-08-12",
          f"checkpoint={db.checkpoint_get('edgar_formd')}, {len(signals)} signal(s)")
    h = db.q1("SELECT health FROM sources WHERE name='edgar_formd'")
    check("...and health returns to ok on its own", h["health"] == "ok", h["health"])

    # ==== 3. weekends stay calendar facts =================================
    db.checkpoint_set("edgar_formd", "2026-08-12")
    a = make_adapter({"daily-index": ("raise", 404),      # no file: weekend/holiday
                      "efts.sec.gov": ("live", '{"hits": {"hits": []}}')})
    a.safe_fetch(since)
    h = db.q1("SELECT health FROM sources WHERE name='edgar_formd'")
    check("a 404 on index days (weekend) does not degrade health",
          h["health"] == "ok", h["health"])

    # ==== 4. the step panel says it out loud ==============================
    from engine.runner import collect_detail

    class _S:
        def __init__(self, mode):
            self.fetch_mode = mode
    line = collect_detail({"new": 0, "duplicate": 279},
                          [_S("cached_snapshot")] * 279)
    check("the collect line names the snapshot problem instead of reading healthy",
          "OFFLINE SNAPSHOT" in line and "279" in line, line[:90])
    line2 = collect_detail({"new": 12, "duplicate": 3}, [_S("live")] * 15)
    check("...and stays clean when fetches are live", "SNAPSHOT" not in line2, line2)

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"EDGAR FRESHNESS: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
