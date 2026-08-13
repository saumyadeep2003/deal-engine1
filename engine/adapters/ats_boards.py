"""ATS job boards — hiring velocity without a Coresignal contract.

Greenhouse, Lever and Ashby all publish a company's open roles through public,
documented, key-free endpoints that exist *to be consumed*. Between them they
cover a large share of venture-backed startups. That gives the thing Coresignal
is actually bought for — is this company hiring, in which functions, and faster
or slower than last month — from sources nobody has to be asked permission to read.

What this is NOT: a headcount. Open roles are a *leading* indicator of growth,
which is arguably more useful than a headcount snapshot, but it is a different
measurement and the field names say so (`open_roles`, not `headcount`).

Velocity comes from the engine's own history rather than a vendor: every run
stores an immutable `hiring` signal with the count of the day, so the difference
between runs is a real measurement the system took itself, with dates attached.
"""
from __future__ import annotations
import json
import re
from datetime import datetime

from .. import db
from ..models import Signal
from .base import BaseAdapter

# Public, documented, no key. Each returns the company's own open roles.
PROVIDERS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}

# Title -> function. Deliberately coarse: the useful signal is "they are hiring
# engineers, not salespeople", not a precise org chart.
FUNCTIONS = {
    # order matters: "ML Research Scientist" is research, not engineering, and the
    # engineering pattern intentionally matches "machine learning"
    "research": r"research|scientist|phd",
    "engineering": r"engineer|developer|swe|infrastructure|platform|backend|frontend|"
                   r"full.?stack|devops|sre|security|machine learning|ml |ai ",
    "product": r"product manager|product design|ux|ui designer|\bpm\b",
    "sales": r"sales|account exec|business development|\bbd\b|revenue|customer success|"
             r"solutions engineer",
    "marketing": r"marketing|growth|content|brand|demand gen",
    "operations": r"operations|finance|people|recruit|legal|hr\b|office|chief of staff",
}
LEGAL_SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|sa|bv)\b\.?",
                             re.I)


def board_slugs(name: str | None, domain: str | None) -> list[str]:
    """Candidate board identifiers, best guess first. A company's board slug is
    almost always its domain stem or its squashed name; trying a couple of cheap
    404s beats requiring a human to map every company by hand."""
    out: list[str] = []
    if domain:
        stem = re.sub(r"^www\.", "", domain.lower()).split(".")[0]
        if stem:
            out.append(stem)
    if name:
        base = LEGAL_SUFFIX_RE.sub("", name).strip()
        squashed = re.sub(r"[^a-z0-9]", "", base.lower())
        if squashed:
            out.append(squashed)
        hyphen = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
        if hyphen and hyphen not in out:
            out.append(hyphen)
    seen: set[str] = set()
    return [s for s in out if s and not (s in seen or seen.add(s))][:3]


def classify_function(title: str) -> str:
    t = (title or "").lower()
    for fn, pattern in FUNCTIONS.items():
        if re.search(pattern, t):
            return fn
    return "other"


def parse_board(provider: str, body: str) -> list[dict]:
    """Normalise the three providers into one row shape. Returns [] rather than
    raising on an unexpected payload — a board that changed format must not stop
    the pipeline, and an empty list is honestly 'nothing observed'."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return []
    rows: list[dict] = []
    if provider == "greenhouse":
        for j in (data or {}).get("jobs", []) or []:
            rows.append({"title": j.get("title"),
                         "location": ((j.get("location") or {}) or {}).get("name"),
                         "url": j.get("absolute_url"),
                         "posted_at": j.get("updated_at") or j.get("first_published"),
                         "department": ", ".join(d.get("name", "") for d in
                                                 (j.get("departments") or []))})
    elif provider == "lever":
        for j in (data if isinstance(data, list) else []):
            cats = j.get("categories") or {}
            rows.append({"title": j.get("text"), "location": cats.get("location"),
                         "url": j.get("hostedUrl"), "posted_at": j.get("createdAt"),
                         "department": cats.get("team")})
    elif provider == "ashby":
        for j in (data or {}).get("jobs", []) or []:
            rows.append({"title": j.get("title"), "location": j.get("location"),
                         "url": j.get("jobUrl"), "posted_at": j.get("publishedAt"),
                         "department": j.get("department")})
    return [r for r in rows if r.get("title")]


class AtsBoardsAdapter(BaseAdapter):
    """Runs per company rather than as a global crawl — there is no 'all boards'
    endpoint, and there shouldn't be: you look up the companies you care about."""

    name = "ats_boards"
    interval_minutes = 720
    requires_license = False

    def fetch(self, since: datetime) -> list[Signal]:
        """Check the boards of companies that survived the filter. One signal per
        company per run, carrying the day's open-role count — so velocity is
        derived from the engine's own dated observations, not a vendor's claim."""
        limit = int(self.cfg.get("max_companies", 25))
        rows = db.q("""SELECT c.id, c.name, c.domain FROM companies c
                       WHERE c.is_synthetic=0 AND c.status IN ('pipeline','hot','watchlist')
                       ORDER BY c.last_signal_at DESC LIMIT ?""", (limit,))
        signals: list[Signal] = []
        for c in rows:
            found = self.fetch_company(c["name"], c["domain"])
            if not found:
                continue
            provider, slug, jobs = found
            mix: dict[str, int] = {}
            for j in jobs:
                mix[classify_function(j["title"])] = mix.get(classify_function(j["title"]), 0) + 1
            board_url = {"greenhouse": f"https://boards.greenhouse.io/{slug}",
                         "lever": f"https://jobs.lever.co/{slug}",
                         "ashby": f"https://jobs.ashbyhq.com/{slug}"}[provider]
            signals.append(Signal(
                kind="hiring",
                observed_at=db.now_iso(),
                url=board_url,
                # one row per company per day: re-running does not double-count,
                # and the day-over-day series is what velocity is computed from
                dedupe_key=f"ats:{provider}:{slug}:{db.now_iso()[:10]}",
                payload={"provider": provider, "slug": slug, "open_roles": len(jobs),
                         "function_mix": mix,
                         "locations": sorted({j["location"] for j in jobs if j.get("location")})[:8],
                         "sample_titles": [j["title"] for j in jobs[:8]]},
                company_name=c["name"], company_domain=c["domain"],
                fetch_mode=self._last_fetch_mode))
        return signals

    def probe(self) -> dict:
        """Reachability of the three board providers, without depending on any
        particular company having a board today.

        A deliberately nonsense slug is used: a provider that is up answers it
        with a clean 404, and a provider that is unreachable raises. That
        distinction is the whole question, and it does not go stale the way
        "look up Stripe's board" would the day Stripe moves ATS."""
        import time as _t
        results, reachable = [], 0
        for provider, tpl in PROVIDERS.items():
            t = _t.time()
            try:
                self.http_get(tpl.format(slug="deal-engine-probe-no-such-board"), retries=0)
                results.append(f"{provider}: reachable")     # answered at all
                reachable += 1
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "404" in msg or "Not Found" in msg:
                    results.append(f"{provider}: reachable (clean 404)")
                    reachable += 1
                else:
                    results.append(f"{provider}: {type(exc).__name__} {msg[:60]}")
            del t
        return {"ok": reachable > 0, "detail": f"{reachable}/3 providers answering — "
                                               + "; ".join(results)}

    def fetch_company(self, name: str | None, domain: str | None) -> tuple[str, str, list] | None:
        """First provider that answers with a real board wins."""
        for slug in board_slugs(name, domain):
            for provider, tpl in PROVIDERS.items():
                try:
                    body, _ = self.http_get(tpl.format(slug=slug), retries=0)
                except Exception:  # noqa: BLE001 — a 404 just means "not their ATS"
                    continue
                jobs = parse_board(provider, body)
                if jobs:
                    return provider, slug, jobs
        return None


def hiring_velocity(company_id: int) -> dict:
    """Change in open roles between the two most recent observations the engine
    made itself. Returns a stated reason instead of a number when there is only
    one observation — a first reading is not a trend."""
    rows = db.q("""SELECT observed_at, payload_json FROM signals
                   WHERE company_id=? AND kind='hiring' AND payload_json LIKE '%open_roles%'
                   ORDER BY observed_at DESC LIMIT 2""", (company_id,))
    if not rows:
        return {"value": None, "reason": "no public job board found for this company"}
    try:
        latest = json.loads(rows[0]["payload_json"])
    except (json.JSONDecodeError, TypeError):
        return {"value": None, "reason": "unreadable hiring signal"}
    out = {"open_roles": latest.get("open_roles"),
           "function_mix": latest.get("function_mix"),
           "observed_at": rows[0]["observed_at"], "source": latest.get("provider")}
    if len(rows) < 2:
        out["change"] = None
        out["reason"] = ("first observation — a trend needs at least two runs, "
                         "which is why this is measured rather than asserted")
        return out
    prev = json.loads(rows[1]["payload_json"])
    a, b = prev.get("open_roles"), latest.get("open_roles")
    out["change"] = (b - a) if (isinstance(a, int) and isinstance(b, int)) else None
    out["previous_observed_at"] = rows[1]["observed_at"]
    return out
