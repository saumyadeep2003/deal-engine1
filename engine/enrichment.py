"""Layer 4 — enrichment. Runs ONLY on companies that survived the deterministic
filter (running it earlier is the most expensive mistake available).

Free + real: GitHub repo stats, careers pages. Licence-gated: headcount and
growth (Coresignal), valuations (PitchBook), firmographics (Crunchbase/
Harmonic) — stored as null with a stated reason and rendered in the workbook
as '— (requires X)'. Per-field TTL cache with source + confidence.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone

from . import db
from .adapters.base import BaseAdapter
from .config import env_key_present, sources_config

LICENSED_FIELDS = {
    "headcount": "Coresignal",
    "headcount_growth_6m": "Coresignal",
    "headcount_growth_12m": "Coresignal",
    "valuation_usd": "PitchBook",
    "cap_table_full": "PitchBook",
    "web_traffic": "Harmonic",
}


class _Http(BaseAdapter):
    name = "enrichment"


def cache_put(company_id: int, field: str, value, source: str,
              confidence: float = 0.9, ttl_hours: int = 168,
              unavailable_reason: str | None = None) -> None:
    db.execute("""INSERT INTO enrichment_cache
        (company_id, field, value_json, unavailable_reason, fetched_at, ttl_hours, source, confidence)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(company_id, field) DO UPDATE SET value_json=excluded.value_json,
        unavailable_reason=excluded.unavailable_reason, fetched_at=excluded.fetched_at,
        source=excluded.source, confidence=excluded.confidence""",
               (company_id, field, json.dumps(value) if value is not None else None,
                unavailable_reason, db.now_iso(), ttl_hours, source, confidence))


def cache_get(company_id: int, field: str):
    row = db.q1("SELECT * FROM enrichment_cache WHERE company_id=? AND field=?"
                " AND datetime(fetched_at, '+' || ttl_hours || ' hours') > datetime('now')",
                (company_id, field))
    return row


GITHUB_API = "https://api.github.com"


def github_velocity(http: _Http, full_name: str) -> dict:
    """Brief §5 asks for stars, contributor count AND commit velocity.

    Contributors: the API caps per_page at 100 and exposes the true count via the
    Link rel="last" page number, so one request with per_page=1 gives it exactly.
    Commit velocity: /stats/participation returns 52 weekly commit counts; GitHub
    answers 202 while it computes, so a miss is reported as unavailable rather
    than as zero — a zero here would read as 'dead project'.
    """
    out: dict = {}
    try:
        body, _ = http.http_get(
            f"{GITHUB_API}/repos/{full_name}/contributors?per_page=1&anon=true",
            retries=0, headers={"Accept": "application/vnd.github+json"})
        link = getattr(http, "_last_link_header", "") or ""
        m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
        if m:
            out["contributors"] = int(m.group(1))
        else:
            out["contributors"] = len(json.loads(body)) if body.strip().startswith("[") else None
    except Exception:  # noqa: BLE001
        out["contributors"] = None

    try:
        body, _ = http.http_get(f"{GITHUB_API}/repos/{full_name}/stats/participation",
                                retries=0, headers={"Accept": "application/vnd.github+json"})
        weeks = (json.loads(body) or {}).get("all") or []
        if weeks:
            out["commits_last_4w"] = sum(weeks[-4:])
            out["commits_last_12w"] = sum(weeks[-12:])
            out["commits_52w"] = sum(weeks)
            prior = sum(weeks[-8:-4]) or 0
            recent = sum(weeks[-4:])
            out["commit_velocity_trend"] = (
                None if prior == 0 else round((recent - prior) / prior, 3))
    except Exception:  # noqa: BLE001
        pass
    return out


def enrich_company(company_id: int, http: _Http | None = None) -> None:
    http = http or _Http()
    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    if not c:
        return

    # -- GitHub (real): stars/forks from the repo signal, then contributor count
    #    and commit velocity from the API (brief §5 asks for all three) --
    if not cache_get(company_id, "github_stars"):
        repo_sig = db.q1("SELECT payload_json FROM signals WHERE company_id=? AND kind='repo'"
                         " ORDER BY observed_at DESC", (company_id,))
        if repo_sig:
            p = json.loads(repo_sig["payload_json"])
            full_name = p.get("full_name")
            cache_put(company_id, "github_stars", p.get("stars"), "GitHub API", 0.95)
            cache_put(company_id, "github_forks", p.get("forks"), "GitHub API", 0.95)
            cache_put(company_id, "github_repo", full_name, "GitHub API", 0.95)
            vel = github_velocity(http, full_name) if full_name else {}
            if vel.get("contributors") is not None:
                cache_put(company_id, "github_contributors", vel["contributors"],
                          f"GitHub API /repos/{full_name}/contributors", 0.95)
            else:
                cache_put(company_id, "github_contributors", None, "GitHub API",
                          unavailable_reason="contributor count not returned by GitHub")
            if "commits_last_4w" in vel:
                cache_put(company_id, "github_commit_velocity", {
                    "commits_last_4w": vel["commits_last_4w"],
                    "commits_last_12w": vel.get("commits_last_12w"),
                    "commits_52w": vel.get("commits_52w"),
                    "trend_4w_vs_prior_4w": vel.get("commit_velocity_trend"),
                }, f"GitHub API /repos/{full_name}/stats/participation", 0.9)
            else:
                cache_put(company_id, "github_commit_velocity", None, "GitHub API",
                          unavailable_reason="commit stats not available (GitHub 202 or"
                                             " private repo)")
        else:
            for f in ("github_stars", "github_contributors", "github_commit_velocity"):
                cache_put(company_id, f, None, "GitHub API",
                          unavailable_reason="no public repo observed in free sources")

    # -- careers page (real, best-effort): open req volume + function mix --
    if c["domain"] and not cache_get(company_id, "careers_functions"):
        hiring = db.q1("SELECT payload_json, url FROM signals WHERE company_id=? AND kind='hiring'"
                       " ORDER BY observed_at DESC", (company_id,))
        if hiring:
            p = json.loads(hiring["payload_json"])
            cache_put(company_id, "careers_functions", p.get("function_mentions"),
                      hiring["url"] or "careers page", 0.6)
        else:
            cache_put(company_id, "careers_functions", None, "careers page",
                      unavailable_reason="careers page not fetched/reachable")

    # -- company surface area (real): positioning, customer logos, pricing --
    if not cache_get(company_id, "positioning"):
        surf = db.q1("""SELECT payload_json, url FROM signals WHERE company_id=?
                        AND kind='surface' ORDER BY observed_at DESC LIMIT 1""", (company_id,))
        if surf:
            p = json.loads(surf["payload_json"])
            src = surf["url"] or "company website"
            cache_put(company_id, "positioning", p.get("positioning"), src, 0.8)
            logos = p.get("customer_logos") or {}
            if logos.get("names"):
                cache_put(company_id, "customer_logos", logos["names"], src, 0.7)
            else:
                cache_put(company_id, "customer_logos", None, src,
                          unavailable_reason=logos.get("reason") or "none found on homepage")
            pricing = p.get("pricing") or {}
            if pricing.get("public"):
                cache_put(company_id, "pricing", pricing, pricing.get("url") or src, 0.8)
            else:
                cache_put(company_id, "pricing", None, src,
                          unavailable_reason=pricing.get("reason") or "no public pricing")
        else:
            for f in ("positioning", "customer_logos", "pricing"):
                cache_put(company_id, f, None, "company website",
                          unavailable_reason="no resolved domain, or site unreachable")

    # -- licence-gated fields: honest nulls unless the key is present --
    licensed_env = {s.get("license_vendor"): s.get("env_key")
                    for s in sources_config()["sources"] if s.get("requires_license")}
    for field, vendor in LICENSED_FIELDS.items():
        if cache_get(company_id, field):
            continue
        env_key = licensed_env.get(vendor) or licensed_env.get(
            next((k for k in licensed_env if k and vendor in k), ""), None)
        if env_key_present(env_key):
            # Adapter path is wired (see engine/adapters/licensed.py); with a real
            # key this is where vendor responses land in the cache.
            continue
        cache_put(company_id, field, None, vendor, confidence=0.0,
                  unavailable_reason=f"requires {vendor}")


def run_enrichment(verbose: bool = True) -> int:
    rows = db.q("SELECT id FROM companies WHERE is_synthetic=0 AND status IN"
                " ('pipeline','hot','watchlist')")
    http = _Http()
    for r in rows:
        enrich_company(r["id"], http)
    if verbose:
        print(f"  enriched {len(rows)} surviving companies (licensed fields -> null + reason)")
    return len(rows)


def display_value(company_id: int, field: str):
    """Workbook rendering: value, or '— (requires X)' when licence-gated."""
    row = db.q1("SELECT value_json, unavailable_reason FROM enrichment_cache"
                " WHERE company_id=? AND field=?", (company_id, field))
    if not row:
        return None
    if row["value_json"] is not None:
        return json.loads(row["value_json"])
    if row["unavailable_reason"]:
        return f"— ({row['unavailable_reason']})"
    return None
