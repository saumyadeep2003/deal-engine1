"""Domain resolution — the missing first step under 'what does this company do'.

The profile writer (engine/profile.py) already does what a reader wants: read the
company's own site, write 4-5 grounded lines, gatekeeper-check every sentence.
It never fired for most of the pipeline because a Form D filing carries a name
and an address, no website — and every downstream capability keyed on domain
(profile, job board, careers page, Apollo) silently sat out.

Resolution uses Clearbit's public autocomplete endpoint (free, keyless, now run
by HubSpot) and then VALIDATES before attaching: the candidate site's homepage
must actually carry the company's name. A wrong domain is worse than none — it
would feed a stranger's products into this company's brief, which is the exact
class of error the gatekeeper exists to prevent. Unvalidated candidates are
recorded as candidates, never attached.
"""
from __future__ import annotations

import json
import re

from . import db
from .adapters.base import BaseAdapter

SUGGEST = "https://autocomplete.clearbit.com/v1/companies/suggest?query={q}"
LEGAL_RE = re.compile(r"[,.]?\s+(inc|corp|corporation|llc|ltd|co|plc|limited)\.?$", re.I)
GENERIC = {"text", "built", "core", "form", "select", "prime", "scale", "general"}


class _Http(BaseAdapter):
    name = "domain_resolver"


def base_name(name: str) -> str | None:
    b = name.strip()
    prev = None
    while prev != b:
        prev, b = b, LEGAL_RE.sub("", b).strip()
    if len(b) < 4 or b.lower() in GENERIC:
        return None
    return b


def resolve(name: str, http: _Http | None = None) -> str | None:
    """Name -> validated domain, or None. Validation is the point: the suggest
    endpoint matches famous companies well and startups loosely, so the homepage
    must mention the company's name before anything is attached."""
    b = base_name(name)
    if not b:
        return None
    http = http or _Http()
    try:
        body, _ = http.http_get(SUGGEST.format(q=b.replace(" ", "%20")), retries=0)
        cands = json.loads(body or "[]")
    except Exception:  # noqa: BLE001
        return None
    want = re.sub(r"[^a-z0-9]", "", b.lower())
    for c in cands[:3]:
        dom = (c.get("domain") or "").lower()
        cname = re.sub(r"[^a-z0-9]", "", (c.get("name") or "").lower())
        if not dom or cname != want:
            continue                     # exact-normalised name match only
        try:                             # the site itself must know this name
            page, _ = http.http_get(f"https://{dom}", retries=0)
            if b.lower().split()[0] in (page or "").lower()[:20000]:
                return dom
        except Exception:  # noqa: BLE001
            continue
    return None


def backfill(limit: int = 40, verbose: bool = True) -> int:
    """Resolve domains for the best-ranked companies that lack one — best first,
    so every downstream reader (profile, hiring, Apollo) lights up where a
    partner will actually look. One attempt per company per 30 days, recorded
    either way so misses are visible and not silently retried forever."""
    rows = db.q("""SELECT c.id, c.name FROM companies c
                   LEFT JOIN scores s ON s.company_id=c.id AND s.id=(
                     SELECT id FROM scores WHERE company_id=c.id
                     ORDER BY scored_at DESC, id DESC LIMIT 1)
                   WHERE c.is_synthetic=0 AND c.domain IS NULL
                   AND c.status IN ('hot','watchlist','pipeline')
                   AND NOT EXISTS (SELECT 1 FROM enrichment_cache e
                                   WHERE e.company_id=c.id AND e.field='domain_attempt'
                                   AND datetime(e.fetched_at, '+720 hours') > datetime('now'))
                   ORDER BY CASE c.status WHEN 'hot' THEN 0 WHEN 'watchlist' THEN 1
                            ELSE 2 END, COALESCE(s.percentile, -1) DESC
                   LIMIT ?""", (limit,))
    from .enrichment import cache_put
    http = _Http()
    n = 0
    for r in rows:
        dom = resolve(r["name"], http)
        cache_put(r["id"], "domain_attempt", dom or None,
                  "clearbit autocomplete + homepage validation", 0.7,
                  unavailable_reason=None if dom else "no validated match")
        if not dom:
            continue
        # never steal another company's domain — that is a merge decision, not ours
        if db.q1("SELECT id FROM companies WHERE domain=?", (dom,)):
            continue
        db.execute("UPDATE companies SET domain=? WHERE id=?", (dom, r["id"]))
        n += 1
    if verbose:
        print(f"  domains: {n}/{len(rows)} resolved and validated")
    return n
