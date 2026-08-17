"""Layer 3 — deterministic filter. Free rules remove the bulk of raw signal
before anything costs money. Companies below the bar are marked Pass and
NEVER reach a model. Also assigns sector (theme) deterministically by keyword.
"""
from __future__ import annotations
import re

from . import db
from .config import thesis

_theme_res: list[tuple[str, str, re.Pattern]] | None = None

# Domains of the CHANNELS the engine reads. Entity resolution occasionally
# mis-parses a round-up or a share link and mints "ycombinator.com" as a company
# — which then hoovers up misattributed signals, ranks on velocity, and reaches
# Deep Dive with an empty brief (run 20 had it in the top-picks diff). A name
# that IS one of our sources' domains is never a prospect.
AGGREGATOR_DOMAINS = {
    "ycombinator.com", "news.ycombinator.com", "techcrunch.com", "github.com",
    "reddit.com", "arxiv.org", "sec.gov", "bloomberg.com", "reuters.com", "ft.com",
    "businesswire.com", "prnewswire.com", "globenewswire.com", "medium.com",
    "substack.com", "linkedin.com", "twitter.com", "x.com", "bsky.app",
    "producthunt.com", "crunchbase.com", "pitchbook.com", "google.com",
    "apps.apple.com", "play.google.com", "youtube.com", "wikipedia.org",
}


def plausible_company_domain(dom: str | None) -> bool:
    """Could this domain be a company's OWN website? An aggregator/press host can
    never be: news.google.com attached as a startup's domain doesn't just read
    wrong — it becomes a domain ALIAS, and every later signal from that host
    resolves onto that one company. One bad attach becomes a misattribution
    machine (observed live: 'Musical' owned news.ycombinator.com, a biotech owned
    sec.gov). Checked at every layer that writes a domain, because the cheapest
    place to stop a poisoned value is before it is stored."""
    if not dom:
        return False
    d = dom.strip().lower()
    d = d[4:] if d.startswith("www.") else d
    if d in AGGREGATOR_DOMAINS:
        return False
    return not any(d.endswith("." + a) for a in AGGREGATOR_DOMAINS)


def identity_corroborated(company_id: int) -> bool:
    """Does ANYTHING tie this record to a real operating company beyond the name?

    A multi-word name is its own evidence (mis-resolution junk is overwhelmingly
    a single word: 'Text', 'Built', 'Ballet', 'Cloud'). A single-word name needs
    one hard anchor: a validated domain, an SEC filing, a funding round, or a
    named founder. A company with none of those is a WORD that signals got
    attached to — it may still be real (a brand-new HN launch), so it is never
    deleted here; it is just not allowed to headline (see scoring.score_all)."""
    c = db.q1("SELECT name, domain FROM companies WHERE id=?", (company_id,))
    if not c:
        return False
    if c["domain"]:
        return True
    name = (c["name"] or "").strip()
    if len(name.split()) >= 2:
        return True
    if db.q1("SELECT id FROM funding_rounds WHERE company_id=? LIMIT 1", (company_id,)):
        return True
    if db.q1("SELECT id FROM founders WHERE company_id=? LIMIT 1", (company_id,)):
        return True
    if db.q1("""SELECT id FROM signals WHERE company_id=?
                AND kind IN ('filing','fund_formation') LIMIT 1""", (company_id,)):
        return True
    return False


def theme_regexes() -> list[tuple[str, str, re.Pattern]]:
    global _theme_res
    if _theme_res is None:
        _theme_res = []
        for t in thesis()["themes"]:
            pat = "|".join(re.escape(k.lower()) for k in t["keywords"])
            _theme_res.append((t["key"], t["label"], re.compile(rf"\b(?:{pat})", re.I)))
    return _theme_res


def match_theme(text: str) -> tuple[str | None, str | None]:
    best_key, best_label, best_n = None, None, 0
    for key, label, rx in theme_regexes():
        n = len(rx.findall(text))
        if n > best_n:
            best_key, best_label, best_n = key, label, n
    return best_key, best_label


def company_text(company_id: int) -> str:
    rows = db.q("SELECT payload_json, raw FROM signals WHERE company_id=?", (company_id,))
    comp = db.q1("SELECT name, description FROM companies WHERE id=?", (company_id,))
    parts = [comp["name"] or "", comp["description"] or ""]
    for r in rows:
        parts.append((r["raw"] or "")[:500])
        parts.append(r["payload_json"][:500])
    return " ".join(parts)


def run_filter(verbose: bool = True) -> dict:
    cfg = thesis()["filters"]
    exclude = {n.lower() for n in cfg["exclude_public_companies"]}
    raw_signals = db.q1("SELECT COUNT(*) c FROM signals")["c"]

    candidates = db.q("SELECT id, name, description FROM companies"
                      " WHERE is_synthetic=0 AND status IN ('candidate','filtered')")
    kept, dropped = 0, 0
    reasons: dict[str, int] = {}

    def drop(cid: int, why: str) -> None:
        nonlocal dropped
        dropped += 1
        reasons[why] = reasons.get(why, 0) + 1
        db.execute("UPDATE companies SET status='filtered' WHERE id=?", (cid,))

    # Aggregator-domain names are junk wherever they already sit — including rows
    # a previous run promoted to pipeline/hot before this rule existed. Status
    # change only; the record and its signals stay, like every other drop here.
    for r in db.q("""SELECT id, name FROM companies WHERE is_synthetic=0
                     AND status IN ('pipeline','hot','watchlist')"""):
        if (r["name"] or "").strip().lower() in AGGREGATOR_DOMAINS:
            drop(r["id"], "aggregator domain mis-resolved as a company")

    # REPAIR: domains already poisoned before the write-side gates existed. A
    # company whose "website" is news.google.com / sec.gov corrupts everything
    # downstream of the domain field — corroboration, the site scraper, and
    # (worst) domain-alias resolution, which glues every future signal from that
    # host onto this one company. Null the field, remove the alias; the real
    # domain resolver gets another clean shot next step.
    repaired = 0
    for r in db.q("SELECT id, name, domain FROM companies WHERE domain IS NOT NULL"):
        if not plausible_company_domain(r["domain"]):
            db.execute("UPDATE companies SET domain=NULL WHERE id=?", (r["id"],))
            db.execute("DELETE FROM company_aliases WHERE company_id=? AND"
                       " alias_type='domain' AND alias=?", (r["id"], r["domain"]))
            repaired += 1
    if repaired:
        print(f"  filter: NULLed {repaired} aggregator/press domain(s) wrongly attached "
              "as company websites (and their aliases)")

    for c in candidates:
        cid, name = c["id"], (c["name"] or "")
        if name.strip().lower() in AGGREGATOR_DOMAINS:
            drop(cid, "aggregator domain mis-resolved as a company")
            continue
        if name.lower() in exclude or any(name.lower().startswith(e + " ") for e in exclude):
            drop(cid, "excluded public/megacorp")
            continue
        if re.search(r"\([A-Z]{2,6}\)\s*$", name):
            drop(cid, "public company (ticker in EDGAR display name)")
            continue
        if re.search(r"\b(business trust|master series|a series of|series of)\b", name, re.I):
            drop(cid, "pooled/series vehicle, not an operating company")
            continue

        sigs = db.q("SELECT kind, observed_at, payload_json FROM signals WHERE company_id=?", (cid,))
        if not sigs:
            drop(cid, "no signals")
            continue

        # Form D sanity: amount window (unknown amount is allowed — detail may be gated)
        import json as _json
        filing_amounts = []   # Form D offering window applies to filings only —
        kinds = set()         # a $3.5B disclosed venture round is a deal, not a fund
        for s in sigs:
            kinds.add(s["kind"])
            p = _json.loads(s["payload_json"])
            if s["kind"] == "filing":
                for f in ("total_offering_usd", "total_sold_usd"):
                    if p.get(f):
                        filing_amounts.append(float(p[f]))
        if filing_amounts and max(filing_amounts) < cfg["min_offering_usd"]:
            drop(cid, "offering below floor")
            continue
        if filing_amounts and min(filing_amounts) > cfg["max_offering_usd"]:
            drop(cid, "offering above ceiling (likely a fund)")
            continue

        # theme match required for soft kinds; filings pass on amount window
        text = company_text(cid)
        theme_key, theme_label = match_theme(text)
        needs_theme = kinds <= set(cfg["require_theme_match_for"])
        if needs_theme and not theme_key:
            drop(cid, "no thesis theme match")
            continue

        # GitHub-only "companies" (repo owners) need real traction signal
        if kinds == {"repo"}:
            stars = 0
            for s in sigs:
                p = _json.loads(s["payload_json"])
                stars = max(stars, int(p.get("stars") or 0))
            if stars < 300 or not theme_key:
                drop(cid, "repo-only owner without traction/theme")
                continue

        if theme_key:
            db.execute("UPDATE companies SET sector=?, sub_sector=? WHERE id=?",
                       (theme_key, theme_label, cid))
        db.execute("UPDATE companies SET status='pipeline' WHERE id=? AND status IN"
                   " ('candidate','filtered')", (cid,))
        kept += 1

    surviving_signals = db.q1(
        "SELECT COUNT(*) c FROM signals s JOIN companies c2 ON s.company_id=c2.id"
        " WHERE c2.status IN ('pipeline','hot','watchlist') AND c2.is_synthetic=0")["c"]
    removed_pct = 100.0 * (1 - surviving_signals / raw_signals) if raw_signals else 0.0
    stats = {"raw_signals": raw_signals, "surviving_signals": surviving_signals,
             "removed_pct": round(removed_pct, 1), "companies_kept": kept,
             "companies_dropped": dropped, "drop_reasons": {k: v for k, v in reasons.items()}}
    if verbose:
        print(f"  deterministic filter: {raw_signals} raw signals -> "
              f"{surviving_signals} on surviving companies ({removed_pct:.1f}% removed)")
        print(f"  companies: {kept} kept, {dropped} dropped {reasons}")
    return stats
