"""Fetch-engine tests — the Scrapling transport upgrade for HTML sources.

The proposition to verify is narrow and load-bearing: an HTML source that a plain
httpx User-Agent gets a 403 (or a JS shell) on — a company site, a careers page —
now clears via Scrapling's browser-fingerprint fetcher, WITHOUT changing the
(body, mode) contract every caller and the snapshot cache depend on, WITHOUT
adding a hard dependency (httpx stays the floor when scrapling is absent), and
WITHOUT ever routing a licensed/ToS-protected provider through it.

Every fetch here is a scripted fake — no network, no real browser — because these
are tests of the ROUTING and the DEGRADATION, which is where an integration like
this goes wrong.

    python tests/fetching_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp()) / "fetch.db"
os.environ["DEAL_ENGINE_DB"] = str(TMP)
os.environ.pop("DATABASE_URL", None)
os.environ.pop("SCRAPLING_MODE", None)

from engine import db  # noqa: E402
from engine.adapters import fetching  # noqa: E402
from engine.adapters.base import BaseAdapter  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ---- a fake scrapling, installable and removable at will -------------------

class _Resp:
    def __init__(self, text: str, status: int = 200, headers: dict | None = None):
        self.html_content = text
        self.status = status
        self.headers = headers or {}


CALLS: list[tuple[str, str]] = []       # (engine, url)


def _install_fake_scrapling(http=None, stealth=None, dynamic=None) -> None:
    class Fetcher:
        @staticmethod
        def get(url, **kw):
            CALLS.append(("scrapling-http", url))
            return (http or _Resp)("http-body for " + url)

    class StealthyFetcher:
        @staticmethod
        def fetch(url, **kw):
            CALLS.append(("scrapling-stealth", url))
            return (stealth or _Resp)("stealth-body for " + url)

    class DynamicFetcher:
        @staticmethod
        def fetch(url, **kw):
            CALLS.append(("scrapling-dynamic", url))
            return (dynamic or _Resp)("dynamic-body for " + url)

    fetching._SCRAPLING.update({"checked": True, "Fetcher": Fetcher,
                                "Stealthy": StealthyFetcher, "Dynamic": DynamicFetcher})


def _remove_scrapling() -> None:
    fetching._SCRAPLING.update({"checked": True, "Fetcher": None,
                                "Stealthy": None, "Dynamic": None})


# ---- a fake httpx.get, so 'the network' is scripted ------------------------

HTTPX_CALLS: list[str] = []


class _HttpxResp:
    def __init__(self, text="httpx-body", status=200, headers=None):
        self.text, self.status_code = text, status
        self.headers = headers or {}


def _fake_httpx_get(result):
    def _get(url, **kw):
        HTTPX_CALLS.append(url)
        if isinstance(result, Exception):
            raise result
        return result(url) if callable(result) else result
    return _get


def reset() -> None:
    CALLS.clear()
    HTTPX_CALLS.clear()
    os.environ.pop("SCRAPLING_MODE", None)


def main() -> int:
    db.connect()
    import httpx
    # hermetic: point the snapshot cache at a throwaway dir so a leftover snapshot
    # from a previous run can never satisfy a fetch the test expects to fail.
    import shutil
    from engine.adapters import base as _base
    _base.CACHE_DIR = Path(tempfile.mkdtemp()) / "cache"
    _base.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fetching.httpx.get = _fake_httpx_get(lambda u: _HttpxResp("httpx-body for " + u))
    # base.py imports httpx too, for building the status error — leave real.

    # ==== 1. no scrapling installed -> httpx, silently and correctly =======
    reset()
    _remove_scrapling()
    a = BaseAdapter({"name": "t_api"})
    body, mode = a.http_get("https://api.example.com/x")
    check("with scrapling absent, httpx is used and the contract is unchanged",
          body == "httpx-body for https://api.example.com/x" and mode == "live"
          and a._last_fetch_engine == "httpx", f"{mode}/{a._last_fetch_engine}")
    check("scrapling_available() is honest about absence",
          not fetching.scrapling_available(), "")

    # ==== 2. installed + auto -> API sources stay on httpx ================
    reset()
    _install_fake_scrapling()
    a = BaseAdapter({"name": "t_api"})       # no fetch_engine -> did not ask
    body, mode = a.http_get("https://api.example.com/x")
    check("auto mode leaves a plain (API) source on httpx — no needless curl stack",
          a._last_fetch_engine == "httpx" and HTTPX_CALLS
          and not [c for c in CALLS if c[0].startswith("scrapling")],
          f"{a._last_fetch_engine}, scrapling_calls={len(CALLS)}")

    # ==== 2b. installed + auto -> a source that ASKS gets scrapling =======
    reset()
    _install_fake_scrapling()
    a = BaseAdapter({"name": "t_html", "fetch_engine": "http"})
    body, mode = a.http_get("https://co.example.com/")
    check("auto mode upgrades a source that requests an engine",
          a._last_fetch_engine == "scrapling-http" and "http-body" in body
          and not HTTPX_CALLS, f"{a._last_fetch_engine}")

    # ==== 2c. SCRAPLING_MODE=http forces the upgrade on every source ======
    reset()
    _install_fake_scrapling()
    os.environ["SCRAPLING_MODE"] = "http"
    a = BaseAdapter({"name": "t_api2"})      # a plain API source
    body, mode = a.http_get("https://api.example.com/y")
    check("SCRAPLING_MODE=http forces scrapling even on a source that didn't ask",
          a._last_fetch_engine == "scrapling-http", a._last_fetch_engine)
    reset()

    # ==== 3. an adapter can request the stealth browser engine ============
    reset()
    _install_fake_scrapling()
    web = BaseAdapter({"name": "company_website", "fetch_engine": "stealth"})
    body, mode = web.http_get("https://startup.example/")
    check("THE POINT: a JS-heavy site adapter gets the stealth browser fetcher",
          web._last_fetch_engine == "scrapling-stealth" and "stealth-body" in body,
          web._last_fetch_engine)

    # ==== 4. stealth requested but browser unavailable -> graceful downgrade
    reset()
    _install_fake_scrapling()
    fetching._SCRAPLING["Stealthy"] = None      # browser deps not installed
    web = BaseAdapter({"name": "company_website", "fetch_engine": "stealth"})
    body, mode = web.http_get("https://startup.example/")
    check("stealth downgrades to scrapling-http when no browser is present",
          web._last_fetch_engine == "scrapling-http" and mode == "live",
          web._last_fetch_engine)
    _install_fake_scrapling()  # restore a fully-present fake for later tests

    # ==== 5. SCRAPLING_MODE=off forces httpx even when installed ==========
    reset()
    _install_fake_scrapling()
    os.environ["SCRAPLING_MODE"] = "off"
    a = BaseAdapter({"name": "t_off"})
    body, mode = a.http_get("https://co.example.com/")
    check("SCRAPLING_MODE=off pins httpx even with scrapling installed",
          a._last_fetch_engine == "httpx" and HTTPX_CALLS, a._last_fetch_engine)
    reset()

    # ==== 6. scrapling raising mid-request falls through to httpx =========
    reset()

    class _Boom:
        @staticmethod
        def get(url, **kw):
            raise RuntimeError("scrapling exploded")
    fetching._SCRAPLING.update({"checked": True, "Fetcher": _Boom,
                                "Stealthy": None, "Dynamic": None})
    a = BaseAdapter({"name": "t_boom"})
    body, mode = a.http_get("https://co.example.com/")
    check("a scrapling exception degrades to httpx, never surfaces",
          a._last_fetch_engine == "httpx" and "httpx-body" in body and HTTPX_CALLS,
          a._last_fetch_engine)

    # ==== 7. a scrapling 403 is classified permanent (not retried forever) =
    reset()
    _install_fake_scrapling(http=lambda t: _Resp(t, status=403))
    a = BaseAdapter({"name": "t_403", "fetch_engine": "http"})
    try:
        a.http_get("https://walled.example/", retries=2)
        raised = False
    except httpx.HTTPStatusError as e:
        raised = e.response.status_code == 403
    # scrapling is called once (permanent -> no retry), and NOT fallen back to httpx
    scr_calls = [c for c in CALLS if c[0] == "scrapling-http"]
    check("a 403 from scrapling raises a permanent HTTPStatusError, tried once",
          raised and len(scr_calls) == 1 and not HTTPX_CALLS,
          f"raised={raised}, scr={len(scr_calls)}, httpx={len(HTTPX_CALLS)}")

    # ==== 8. a scrapling 503 is transient -> retried ======================
    reset()
    _install_fake_scrapling(http=lambda t: _Resp(t, status=503))
    a = BaseAdapter({"name": "t_503", "fetch_engine": "http"})
    try:
        a.http_get("https://flaky.example/", retries=1)
    except httpx.HTTPStatusError:
        pass
    check("a 503 from scrapling is transient and retried",
          len([c for c in CALLS if c[0] == "scrapling-http"]) == 2,
          str(len([c for c in CALLS if c[0] == 'scrapling-http'])))

    # ==== 9. provenance: the engine is stamped into the snapshot ==========
    reset()
    _install_fake_scrapling()
    web = BaseAdapter({"name": "prov_site", "fetch_engine": "stealth"})
    web.http_get("https://startup.example/")
    import json as _json
    snap = _json.loads(web._cache_path("https://startup.example/").read_text())
    check("the fetching engine is recorded in the snapshot, not hidden",
          snap.get("engine") == "scrapling-stealth", str(snap.get("engine")))

    # ==== 10. ToS guard: licensed sources are never wired to a fetcher =====
    from engine.config import sources_config
    licensed = [s for s in sources_config()["sources"] if s.get("requires_license")]
    check("no licensed/ToS-protected source is given a fetch_engine",
          all(not s.get("fetch_engine") for s in licensed),
          f"{len(licensed)} licensed sources checked — a fetcher is not a licence")
    html_sources = [s for s in sources_config()["sources"]
                    if s.get("fetch_engine")]
    check("only the two intended HTML sources opt into an engine",
          {s["name"] for s in html_sources} == {"company_website", "careers_pages"},
          str(sorted(s["name"] for s in html_sources)))

    # ==== 11. an absent optional package must never flag the build stale ===
    reset()
    _remove_scrapling()
    from engine import version
    vi = version.info()
    check("scrapling absent -> version still reports complete, nothing 'missing'",
          vi["complete"] and vi["missing"] == []
          and vi["optional"]["scrapling_installed"]["on"] is False,
          "the first deploy without it printed 'RUNNING OLDER CODE' over a current build")
    check("...and the optional entry says how to enable it",
          "requirements-scrapling" in vi["optional"]["scrapling_installed"]["how_to_enable"],
          "")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"FETCH ENGINE: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
