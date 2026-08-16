"""One HTTP fetch, one place to choose how it is made.

Every HTML source in the engine flows through `BaseAdapter.http_get`, which flowed
through plain httpx. That is correct for the APIs and RSS feeds — EDGAR, HN's
Algolia API, arXiv, GitHub, Reddit JSON, Google-News RSS — they are not defended,
and httpx impersonates nothing it needs to. It is WRONG for the two sources a
partner actually reads a company OUT of: the company's own website and its careers
page. A plain-httpx User-Agent gets a 403 or a JavaScript shell there, which is the
direct cause of "the rank-1 brief has no description": no page read, no profile, no
'what they do'.

Scrapling (https://github.com/D4Vinci/Scrapling, BSD-3) closes exactly that gap:

* `Fetcher.get` sends real browser TLS/HTTP2 fingerprints WITHOUT launching a
  browser, so it clears fingerprint-based blocks that stop httpx — on the same free
  Render tier, no browser binary. This is the default upgrade.
* `StealthyFetcher` / `DynamicFetcher` DO launch a (camoufox / chromium) browser and
  render JavaScript, and can solve Cloudflare. They need a browser install the free
  tier has no room for, so they are opt-in via SCRAPLING_MODE and meant for a local
  or larger run.

Four disciplines carried from the rest of the engine:

1. Graceful degradation. If scrapling is not installed, httpx is used, silently and
   correctly. `requirements.txt` does not have to change for the engine to run; the
   dependency lives in `requirements-scrapling.txt` and is import-guarded here.
2. Honest provenance. The ENGINE that fetched (httpx / scrapling-http /
   scrapling-stealth / scrapling-dynamic) is recorded, so a page a stealth browser
   cleared is never quietly presented as an ordinary fetch.
3. ToS discipline unchanged. This is a TRANSPORT upgrade for sources the engine
   already fetches. It adds no source. A better fetcher is not a licence: PitchBook,
   Crunchbase, X, Coresignal, LinkedIn and Blind stay key-gated stubs and are never
   routed here, because their terms prohibit automated collection regardless of
   whether a fetcher could defeat their bot wall. Capability is not permission.
4. No new failure mode. A browser engine that is requested but unavailable
   downgrades to scrapling-http, then to httpx — it never raises for being absent.
"""
from __future__ import annotations

import os

import httpx

# ---- availability, resolved once -------------------------------------------
_SCRAPLING = {"checked": False, "Fetcher": None, "Stealthy": None, "Dynamic": None}


def _load_scrapling() -> None:
    if _SCRAPLING["checked"]:
        return
    _SCRAPLING["checked"] = True
    try:
        from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher
        _SCRAPLING["Fetcher"] = Fetcher
        _SCRAPLING["Stealthy"] = StealthyFetcher
        _SCRAPLING["Dynamic"] = DynamicFetcher
    except Exception:  # noqa: BLE001 — not installed, or optional browser deps missing
        pass


def scrapling_available() -> bool:
    _load_scrapling()
    return _SCRAPLING["Fetcher"] is not None


# ---- mode selection --------------------------------------------------------
# SCRAPLING_MODE (env), lowercased — the GLOBAL policy:
#   off / httpx    -> always plain httpx (the pre-Scrapling behaviour), ignore per-source requests
#   auto (default) -> respect each source's own `fetch_engine`; every source that does NOT
#                     ask stays on httpx. So the 12 API/RSS sources — which httpx already
#                     fetches perfectly — are untouched, and only company_website/
#                     careers_pages (which ask for stealth) get the upgrade. Routing an
#                     API through scrapling adds its curl retry stack for zero benefit.
#   http/stealth/dynamic -> FORCE that engine on every source (override); a global sweep.
VALID_MODES = ("off", "httpx", "auto", "http", "stealth", "dynamic")


def global_mode() -> str:
    m = (os.environ.get("SCRAPLING_MODE") or "auto").strip().lower()
    return m if m in VALID_MODES else "auto"


def _resolve(engine: str | None) -> str:
    """The engine actually used for this call, after policy and availability.
    `engine` is the per-source/per-call REQUEST (from `fetch_engine` in
    sources.yaml, or an explicit http_get(engine=...)); None means the source did
    not ask for anything special."""
    g = global_mode()
    if g in ("off", "httpx"):
        return "httpx"                       # pin httpx, ignore per-source requests
    want = g if g in ("http", "stealth", "dynamic") else engine   # global override, else the source's ask
    if not want:
        return "httpx"                       # auto + source didn't ask -> httpx, unchanged
    if not scrapling_available():
        return "httpx"                       # asked for an upgrade that isn't installed
    if want == "http":
        return "scrapling-http"
    if want == "stealth":
        return "scrapling-stealth" if _SCRAPLING["Stealthy"] else "scrapling-http"
    if want == "dynamic":
        return "scrapling-dynamic" if _SCRAPLING["Dynamic"] else "scrapling-http"
    return "scrapling-http"


# ---- the fetch itself ------------------------------------------------------

def raw_fetch(url: str, headers: dict, timeout: float = 20.0,
              follow_redirects: bool = True,
              engine: str | None = None) -> tuple[str, int, str, str]:
    """Fetch one URL. Returns (body, status_code, link_header, engine_used).

    Never raises for an HTTP status — the caller (`BaseAdapter.http_get`) decides
    what a 4xx/5xx means, so the existing transient/permanent classification and
    snapshot-cache fallback keep working unchanged. Raises only on a genuine
    transport failure (connection refused, timeout), and those are real httpx
    exceptions so `classify_error` still understands them: when scrapling itself
    throws mid-request, this falls through to httpx rather than surfacing a
    scrapling-specific error the rest of the system has never heard of."""
    used = _resolve(engine)

    if used != "httpx":
        try:
            return _scrapling_fetch(url, headers, timeout, follow_redirects, used)
        except Exception:  # noqa: BLE001 — scrapling couldn't execute; httpx is the floor
            used = "httpx"

    r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=follow_redirects)
    return r.text, r.status_code, r.headers.get("link", ""), "httpx"


def _scrapling_fetch(url: str, headers: dict, timeout: float, follow_redirects: bool,
                     used: str) -> tuple[str, int, str, str]:
    if used == "scrapling-http":
        # retries=1: scrapling's default of 3 internal attempts sits UNDER our own
        # retry loop, so one dead startup site cost 3 x timeout before our layer
        # even saw the failure — measured on the live box as the website collect
        # step growing 37s -> 609s. Passed defensively: if this scrapling version
        # doesn't accept the kwarg, its default stands rather than the call dying.
        try:
            page = _SCRAPLING["Fetcher"].get(
                url, headers=headers or None, timeout=timeout,
                follow_redirects=follow_redirects, stealthy_headers=True, retries=1)
        except TypeError:
            page = _SCRAPLING["Fetcher"].get(
                url, headers=headers or None, timeout=timeout,
                follow_redirects=follow_redirects, stealthy_headers=True)
    elif used == "scrapling-stealth":
        # a browser engine ignores our header dict and generates its own; that is
        # the point of stealth, and it is why the engine is recorded distinctly.
        page = _SCRAPLING["Stealthy"].fetch(
            url, headless=True, solve_cloudflare=True, network_idle=True)
    else:  # scrapling-dynamic
        page = _SCRAPLING["Dynamic"].fetch(url, headless=True, network_idle=True)

    body = _body_of(page)
    status = int(getattr(page, "status", 0) or 0)
    link = ""
    page_headers = getattr(page, "headers", None)
    if isinstance(page_headers, dict):
        link = page_headers.get("link") or page_headers.get("Link") or ""
    return body, status, link, used


def _body_of(page) -> str:
    """Scrapling's Response exposes the document a few ways depending on version;
    take the first that yields text, so a minor upstream rename does not blank
    every page the engine reads."""
    for attr in ("html_content", "content", "text", "body"):
        val = getattr(page, attr, None)
        if val is None:
            continue
        if isinstance(val, bytes):
            try:
                return val.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
        if isinstance(val, str) and val:
            return val
    return str(page)
