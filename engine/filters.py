"""Layer 3 — deterministic filter. Free rules remove the bulk of raw signal
before anything costs money. Companies below the bar are marked Pass and
NEVER reach a model. Also assigns sector (theme) deterministically by keyword.
"""
from __future__ import annotations
import re

from . import db
from .config import thesis

_theme_res: list[tuple[str, str, re.Pattern]] | None = None


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

    for c in candidates:
        cid, name = c["id"], (c["name"] or "")
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
