"""What the company actually does, in its own words.

The workbook has always had a "one-line description" column because the brief
asks for one. It was filled from whatever `summary` field arrived with the signal
that created the company — which, for a company first seen inside a funding
round-up, is the round-up's own summary. One live example, verbatim from the
pipeline: leadmagic.io was described as *"19 Series A Cybersecurity Startups That
Raised $626M · Escape · Qevlar AI · Reclaim…"*. That is the article's headline,
not the company, and it was sitting in a partner-facing column.

So the description is rebuilt from a source that can only be about this company:
its own website. The site is already scraped for positioning (title, meta
description, H1/H2) and, where Apify is configured, for page text. A model turns
that into two or three readable sentences and a product list — and then the
gatekeeper checks every sentence back against the scraped text, so a company that
does not say something about itself cannot have it said on its behalf.

Where there is no usable site text there is no profile. A named absence is worth
more than a paragraph assembled from press coverage, because a partner reading
"what they do" is entitled to assume it came from the company.
"""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel

from . import db, gatekeeper, llm
from .enrichment import cache_get as _cache_row, cache_put


def cache_value(company_id: int, field: str):
    """enrichment_cache.cache_get returns the ROW; callers want the value. Doing
    the unwrap here keeps the two meanings of "get" from being confused again."""
    row = _cache_row(company_id, field)
    if not row or not row["value_json"]:
        return None
    try:
        return json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError):
        return None


class CompanyProfile(BaseModel):
    intro: Optional[str] = None            # 2-3 sentences, plain English
    products: Optional[list[str]] = None   # named products / offerings
    customers: Optional[str] = None        # who it is sold to, if the site says
    one_liner: Optional[str] = None        # <= 18 words, for the workbook column


PROMPT = (
    "You are writing the opening of a venture brief. Using ONLY the text below, "
    "which was scraped from the company's own website, write:\n"
    "  intro      — 2-3 plain sentences on what this company does and for whom\n"
    "  products   — the named products or offerings, as a list (empty if unnamed)\n"
    "  customers  — who it sells to, ONLY if the text says so\n"
    "  one_liner  — the same thing in 18 words or fewer\n\n"
    "Rules that matter more than completeness: use no fact that is not in the text. "
    "Do not name investors, customers, funding amounts or headcounts unless the text "
    "names them. If the text is too thin to describe the company, return nulls — an "
    "honest null is useful and an invented description is worse than a blank column, "
    "because a partner will act on it."
)

# Marketing pages open with slogans; a brief that opens with one is worthless.
MIN_SOURCE_CHARS = 120


def source_text(company_id: int) -> tuple[str, str] | None:
    """The company's own words, and where they came from. None when the engine has
    never successfully read the company's site."""
    parts: list[str] = []
    url = None

    row = db.q1("""SELECT url, payload_json FROM signals WHERE company_id=? AND kind='surface'
                   ORDER BY observed_at DESC LIMIT 1""", (company_id,))
    if row:
        url = row["url"]
        try:
            p = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            p = {}
        pos = p.get("positioning") or {}
        parts += [str(pos.get(k) or "") for k in ("title", "meta_description", "h1", "h2")]
        if p.get("pricing"):
            plans = (p["pricing"] or {}).get("plan_names") or []
            if plans:
                parts.append("Plans offered: " + ", ".join(map(str, plans)))
        # customer_logos is a DICT ({"names": [...], "evidence": ...}) from the
        # website adapter — slicing it worked nowhere and crashed loudest on
        # Python 3.12, where dict[:10] raises KeyError(slice(None, 10, None)).
        # That one line took down the briefs AND publish steps of a whole run.
        logos = p.get("customer_logos")
        names = logos.get("names") if isinstance(logos, dict) else logos
        if isinstance(names, (list, tuple)) and names:
            parts.append("Logos shown on the site: " + ", ".join(map(str, list(names)[:10])))

    # Apify's crawl, when the token is configured, gives real page prose rather
    # than just the head tags — much better raw material for two sentences.
    crawl = cache_value(company_id, "site_text")
    if crawl:
        parts.append(str(crawl)[:4000])

    text = "\n".join(x for x in parts if x and x.strip())
    if len(text) < MIN_SOURCE_CHARS:
        return None
    return text, (url or "the company's website")


def build(company_id: int, force: bool = False) -> dict | None:
    """Write (or reuse) this company's profile. Returns None when the site was
    never read — the caller then states that, rather than filling the gap."""
    if not force:
        cached = cache_value(company_id, "company_profile")
        if isinstance(cached, dict):
            return cached

    try:
        src = source_text(company_id)
    except Exception:  # noqa: BLE001 — one malformed payload must not kill a run step
        return None
    if not src:
        return None
    text, url = src

    if llm.stubbed():
        # Without a model the head tags are still the company's own words. Worse
        # prose, same provenance — and infinitely better than the round-up headline
        # this column used to carry.
        head = " ".join(text.split("\n")[:3])[:300]
        out = {"intro": head, "products": [], "customers": None,
               "one_liner": head[:140], "source_url": url,
               "method": "verbatim from the company's own site (no model configured)"}
        cache_put(company_id, "company_profile", out, url, 0.6)
        return out

    res = llm.complete_json("brief", PROMPT, text[:6000], CompanyProfile, tier="brief")
    if res is None:
        return None
    out = res.model_dump()
    if not (out.get("intro") or out.get("one_liner")):
        return None

    # The model was given one document and told to stay inside it. The gatekeeper
    # is what makes that a guarantee rather than an instruction: every sentence is
    # checked back against the very text it was handed.
    ev = gatekeeper.evidence_from_text(text, company_id=company_id)
    firms = gatekeeper._known_firm_names()
    removed_all: list[dict] = []
    for field in ("intro", "one_liner", "customers"):
        if isinstance(out.get(field), str) and out[field].strip():
            clean, removed = gatekeeper.verify_text(out[field], ev, firms)
            out[field] = clean
            removed_all += [{**r, "field": field} for r in removed]
    if out.get("products"):
        kept = []
        for p in out["products"]:
            # a product name the site never prints is an invention, and product
            # names are exactly the kind of plausible detail a model supplies
            if str(p).lower() in text.lower():
                kept.append(p)
            else:
                removed_all.append({"sentence": str(p), "field": "products",
                                    "reasons": ["product name does not appear on the site"]})
        out["products"] = kept
    gatekeeper.record(company_id, "company_profile", removed_all, ref=url)

    out["source_url"] = url
    out["method"] = "written from the company's own website, every sentence checked back against it"
    cache_put(company_id, "company_profile", out, url, 0.8)

    # Replace the description column at source. This is the field that carried a
    # funding round-up's headline for months.
    one = (out.get("one_liner") or out.get("intro") or "").strip()
    if one and gatekeeper.REMOVED_MARKER not in one:
        db.execute("UPDATE companies SET description=? WHERE id=?", (one[:300], company_id))
    return out


def section(company_id: int) -> str:
    """The opening of a brief: what this company does, before any judgement."""
    p = build(company_id)
    if not p:
        c = db.q1("SELECT domain FROM companies WHERE id=?", (company_id,))
        why = ("no website on record for this company"
               if not (c and c["domain"]) else
               "the engine has not successfully read this company's website yet")
        return ("\n## What they do\n\n- Not established — " + why +
                ". Nothing is inferred from press coverage: a description a partner "
                "reads should come from the company.\n")
    out = ["\n## What they do\n"]
    if p.get("intro"):
        out.append(p["intro"])
    if p.get("products"):
        out.append("\n**Products:** " + ", ".join(p["products"]))
    if p.get("customers"):
        out.append(f"\n**Sold to:** {p['customers']}")
    out.append(f"\n*Source: {p.get('source_url')} — {p.get('method')}.* [computed]")
    return "\n".join(out) + "\n"


def one_liner(company_id: int) -> str | None:
    p = build(company_id)
    if not p:
        return None
    line = (p.get("one_liner") or p.get("intro") or "").strip()
    return line[:200] or None


def backfill(limit: int = 60, verbose: bool = True) -> int:
    """Profile the companies whose sites have actually been read.

    This used to take the top N by percentile alone — and the top of the ranking
    is dominated by filing-only companies with no website at all, so the batch
    was spent proving over and over that nothing could be profiled while the
    companies the website adapter HAD visited sat below the cut. Three runs in a
    row reported "0 profiles written" with fresh surface signals in the table.
    Source material first, THEN rank: a profile can only be written from a page
    the engine has read, so a company with a surface signal outranks a domainless
    company at the 100th percentile — for this list only."""
    rows = db.q("""SELECT c.id FROM companies c
                   LEFT JOIN scores s ON s.company_id=c.id AND s.id=(
                     SELECT id FROM scores WHERE company_id=c.id
                     ORDER BY scored_at DESC, id DESC LIMIT 1)
                   WHERE c.is_synthetic=0 AND c.status IN ('pipeline','hot','watchlist')
                   ORDER BY (EXISTS(SELECT 1 FROM signals sg WHERE sg.company_id=c.id
                                    AND sg.kind='surface')) DESC,
                            COALESCE(s.percentile, -1) DESC LIMIT ?""", (limit,))
    n = 0
    for r in rows:
        try:
            if build(r["id"]):
                n += 1
        except Exception:  # noqa: BLE001 — one bad page must not stop the batch
            continue
    if verbose:
        print(f"  company profiles: {n}/{len(rows)} written from the companies' own sites")
    return n
