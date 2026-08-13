"""Component 07 — commentary harvester. HN (Algolia) and Reddit are free and
real; X, Blind, podcasts, Substack threads are wired but LicenseRequired.

Quotes are always REAL text from real comments with real URLs. Sentiment and
themes are model judgments — stubbed loudly when no key. Promotional noise is
filtered deterministically before any model call.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

# How far before a company's first observed signal a comment can still plausibly
# be about it (stealth-mode chatter, a founder's earlier announcement).
COMMENT_GRACE_DAYS = 540

from pydantic import BaseModel

from . import db, llm
from .adapters.base import BaseAdapter

HN_COMMENTS = "https://hn.algolia.com/api/v1/search?query=%22{q}%22&tags=comment&hitsPerPage=10"

PROMO_RE = re.compile(r"(sign up|discount|promo code|our product|we built|check out my)", re.I)


class CommentJudgment(BaseModel):
    sentiment: Optional[str] = None       # positive | negative | mixed | neutral
    themes: Optional[list[str]] = None
    credibility: Optional[str] = None     # engineer | operator | investor | unknown


class _Http(BaseAdapter):
    name = "hn"          # shares the HN snapshot cache


def _clean(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").replace("&#x27;", "'").replace("&quot;", '"').strip()


GENERIC_NAMES = {"natural", "general", "national", "global", "digital", "united"}
LEGAL_TAIL_RE = re.compile(r"[,.]?\s+(inc|corp|corporation|llc|ltd|co)\.?$", re.I)


def search_name(name: str) -> str | None:
    """Base name for commentary search: strip legal suffixes, skip generics."""
    base = name
    prev = None
    while prev != base:
        prev = base
        base = LEGAL_TAIL_RE.sub("", base).strip()
    if len(base) < 4 or base.lower() in GENERIC_NAMES:
        return None
    return base


def _earliest_plausible_comment(company_id: int) -> str | None:
    """A company first seen in 2026 cannot have been discussed in 2015. Without this
    guard, a common-word name ('Pangram', 'Warp', 'Convex') collects years of unrelated
    chatter — Pangram's brief carried three 2015 comments about the word game.
    The grace window allows genuine pre-launch/stealth discussion."""
    row = db.q1("SELECT MIN(observed_at) m FROM signals WHERE company_id=?", (company_id,))
    if not row or not row["m"]:
        return None
    try:
        first = datetime.fromisoformat(str(row["m"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (first - timedelta(days=COMMENT_GRACE_DAYS)).isoformat()


def harvest_company(company_id: int, http: _Http | None = None, verbose: bool = False) -> int:
    http = http or _Http()
    c = db.q1("SELECT name FROM companies WHERE id=?", (company_id,))
    base = search_name(c["name"]) if c else None
    if not base:
        return 0
    floor_at = _earliest_plausible_comment(company_id)
    url = HN_COMMENTS.format(q=quote(base))
    try:
        body, mode = http.http_get(url, retries=1)
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for hit in json.loads(body).get("hits", []):
        text = _clean(hit.get("comment_text", ""))
        if len(text) < 40 or PROMO_RE.search(text):
            continue  # deterministic noise filter BEFORE any model call
        created = str(hit.get("created_at") or "")
        if floor_at and created and created < floor_at:
            continue  # predates the company's existence — a name collision, not commentary
        curl = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        if db.q1("SELECT id FROM commentary WHERE url=? AND company_id=?", (curl, company_id)):
            continue
        judged = llm.complete_json(
            "classify",
            f"Classify this Hacker News comment about {c['name']}: sentiment toward the "
            "company, main themes (max 3), and apparent author credibility.",
            text[:1200], CommentJudgment, tier="classify")
        db.insert("commentary", {
            "company_id": company_id, "platform": "hackernews",
            "author": hit.get("author"),
            "author_credibility": judged.credibility if judged else llm.STUB_TEXT,
            "sentiment": judged.sentiment if judged else llm.STUB_TEXT,
            "themes_json": json.dumps(judged.themes if judged else None),
            "quote": text[:400], "url": curl,
            "observed_at": hit.get("created_at", db.now_iso())})
        n += 1
    return n


def harvest_reddit_mentions(verbose: bool = True) -> int:
    """Attach already-ingested Reddit commentary signals to pipeline companies."""
    companies = db.q("SELECT id, name FROM companies WHERE is_synthetic=0"
                     " AND status IN ('hot','watchlist','pipeline')")
    n = 0
    for s in db.q("""SELECT s.id, s.url, s.observed_at, s.payload_json FROM signals s
                     JOIN sources so ON s.source_id=so.id
                     WHERE so.name='reddit' AND s.kind='commentary'"""):
        p = json.loads(s["payload_json"])
        text = f"{p.get('title', '')} {p.get('selftext', '')}"
        for c in companies:
            if len(c["name"]) >= 5 and re.search(rf"\b{re.escape(c['name'])}\b", text, re.I):
                if not db.q1("SELECT id FROM commentary WHERE url=? AND company_id=?",
                             (s["url"], c["id"])):
                    db.insert("commentary", {
                        "company_id": c["id"], "platform": f"reddit/{p.get('subreddit')}",
                        "author": p.get("author"), "sentiment": llm.STUB_TEXT if llm.stubbed() else None,
                        "quote": text[:400], "url": s["url"], "observed_at": s["observed_at"]})
                    n += 1
    return n


def prune_impossible_commentary(verbose: bool = False) -> int:
    """Remove stored quotes that predate their company's existence. Rows harvested
    before the date guard existed must not keep appearing in briefs — a bad row is
    only fixed by deleting it, not by filtering it at read time in one place and
    forgetting the other."""
    removed = 0
    for c in db.q("SELECT id FROM companies WHERE is_synthetic=0"):
        floor_at = _earliest_plausible_comment(c["id"])
        if not floor_at:
            continue
        # count first: db.execute returns lastrowid on SQLite, which is meaningless
        # for a DELETE and would under-report the clean-up as zero
        doomed = db.q1("SELECT COUNT(*) n FROM commentary WHERE company_id=?"
                       " AND observed_at < ?", (c["id"], floor_at))["n"]
        if doomed:
            db.execute("DELETE FROM commentary WHERE company_id=? AND observed_at < ?",
                       (c["id"], floor_at))
            removed += doomed
    if verbose and removed:
        print(f"  commentary: pruned {removed} quote(s) predating their company"
              " (name collisions, not commentary)")
    return removed


def run_commentary(max_companies: int = 60, verbose: bool = True) -> int:
    http = _Http()
    prune_impossible_commentary(verbose=verbose)
    rows = db.q("""SELECT c.id, c.name FROM companies c JOIN scores s ON s.company_id=c.id
                   WHERE s.id=(SELECT id FROM scores WHERE company_id=c.id
                               ORDER BY scored_at DESC, id DESC LIMIT 1)
                   AND c.is_synthetic=0 AND c.status IN ('hot','watchlist')
                   ORDER BY s.percentile DESC LIMIT ?""", (max_companies,))
    total = 0
    for r in rows:
        total += harvest_company(r["id"], http)
    total += harvest_reddit_mentions(verbose=False)
    if verbose:
        licensed = "X, Blind, podcasts, Substack threads: wired, LicenseRequired (no key)"
        print(f"  commentary: {total} real items captured from HN/Reddit for"
              f" {len(rows)} top companies; {licensed}")
    return total
