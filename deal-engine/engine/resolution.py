"""Component 03 — Entity resolution. Shared by every ingest path.

Canonical key: normalised primary domain. Fallbacks in order: LinkedIn company
URL → external UUID → fuzzy name match scoped by sector/geography.

Confidence >= 0.85 auto-merges (logged, reversible); 0.60–0.85 → review queue;
< 0.60 → new record. A wrong merge is worse than a duplicate: merges store a
full JSON snapshot of the absorbed record so unmerge() can restore it.
"""
from __future__ import annotations
import json
import re
from difflib import SequenceMatcher

from . import db
from .models import Signal

AUTO_MERGE = 0.85
REVIEW_BAND = 0.60

LEGAL_SUFFIX_RE = re.compile(
    r"[,.]?\s*\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|"
    r"plc|gmbh|sas|bv|ab|oy|pte|pty|lp|l\.p|holdings?|labs?|technologies|"
    r"technology|tech|ai|systems)\b\.?\s*$", re.I)


def normalize_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    d = domain.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].split("?")[0]
    return d or None


def normalize_name(name: str) -> str:
    n = name.strip().lower()
    prev = None
    while prev != n:                       # strip stacked suffixes: "X Labs, Inc."
        prev = n
        n = LEGAL_SUFFIX_RE.sub("", n).strip()
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    base = SequenceMatcher(None, na, nb).ratio()
    # token containment ("harmonic" vs "harmonic ai") boosts confidence
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and (ta <= tb or tb <= ta):
        base = max(base, 0.90)
    return base


def _is_synthetic_name(name: str) -> bool:
    """Synthetic demo records are namespaced DEMO-* / DEMO * and must never
    merge with (or be created as) real companies."""
    return name.upper().startswith("DEMO")


def _attach_domain(company_id: int, domain: str | None, signal_id: int | None) -> None:
    if not domain:
        return
    row = db.q1("SELECT domain FROM companies WHERE id=?", (company_id,))
    if row and not row["domain"]:
        clash = db.q1("SELECT id FROM companies WHERE domain=?", (domain,))
        if not clash:
            db.execute("UPDATE companies SET domain=? WHERE id=?", (domain, company_id))
    _add_alias(company_id, domain, "domain", 1.0, signal_id)


def _add_alias(company_id: int, alias: str, alias_type: str, confidence: float,
               signal_id: int | None = None, merged_from: str | None = None) -> None:
    exists = db.q1("SELECT id FROM company_aliases WHERE company_id=? AND alias=? AND alias_type=?",
                   (company_id, alias, alias_type))
    if not exists:
        db.insert("company_aliases", {
            "company_id": company_id, "alias": alias, "alias_type": alias_type,
            "confidence": confidence, "merged_at": db.now_iso(),
            "source_signal_id": signal_id, "merged_from": merged_from})


def resolve(signal: Signal, signal_id: int | None = None) -> int | None:
    """Resolve a signal to a canonical company id (creating one if needed).

    Returns None when the signal carries no company identity at all
    (e.g. an arXiv paper or a macro news item).
    """
    domain = normalize_domain(signal.company_domain)
    name = (signal.company_name or "").strip()
    payload = signal.payload or {}
    linkedin = payload.get("linkedin_url")
    ext_uuid = payload.get("external_uuid")

    if not domain and not name and not linkedin and not ext_uuid:
        return None

    # 1) canonical domain
    if domain:
        row = db.q1("SELECT id FROM companies WHERE domain=?", (domain,))
        if row:
            if name:
                _add_alias(row["id"], name, "name", 1.0, signal_id)
            return row["id"]
        alias = db.q1("SELECT company_id FROM company_aliases WHERE alias=? AND alias_type='domain'",
                      (domain,))
        if alias:
            return alias["company_id"]

    # 2) linkedin URL / 3) external UUID
    for value, atype in ((linkedin, "linkedin"), (ext_uuid, "uuid")):
        if value:
            alias = db.q1("SELECT company_id FROM company_aliases WHERE alias=? AND alias_type=?",
                          (value, atype))
            if alias:
                if name:
                    _add_alias(alias["company_id"], name, "name", 0.95, signal_id)
                return alias["company_id"]

    # 4) fuzzy name, scoped by sector/geo when the candidate record has them
    if name:
        exact = db.q1("SELECT company_id FROM company_aliases WHERE alias_type='name' AND alias=?",
                      (normalize_name(name),))
        if exact:
            _add_alias(exact["company_id"], name, "name", 1.0, signal_id)
            _attach_domain(exact["company_id"], domain, signal_id)
            return exact["company_id"]
        synthetic = _is_synthetic_name(name)
        best_id, best_conf = None, 0.0
        for row in db.q("SELECT id, name, sector, country FROM companies WHERE is_synthetic=?",
                        (1 if synthetic else 0,)):
            conf = name_similarity(name, row["name"])
            if conf < REVIEW_BAND:
                continue
            sector_hint = payload.get("sector")
            if sector_hint and row["sector"] and sector_hint != row["sector"]:
                conf -= 0.15   # sector disagreement penalises a fuzzy match
            geo_hint = payload.get("state") or payload.get("country")
            if geo_hint and row["country"] and geo_hint != row["country"]:
                conf -= 0.05
            if conf > best_conf:
                best_id, best_conf = row["id"], conf

        if best_id and best_conf >= AUTO_MERGE:
            _add_alias(best_id, name, "name", round(best_conf, 3), signal_id)
            _add_alias(best_id, normalize_name(name), "name", round(best_conf, 3), signal_id)
            _attach_domain(best_id, domain, signal_id)
            return best_id
        if best_id and best_conf >= REVIEW_BAND:
            db.insert("review_queue", {
                "kind": "merge", "confidence": round(best_conf, 3),
                "payload_json": json.dumps({
                    "candidate_company_id": best_id, "incoming_name": name,
                    "incoming_domain": domain, "signal_id": signal_id}),
                "created_at": db.now_iso()})
            # ambiguous → create a NEW record; a wrong merge destroys data silently

    # create new company
    company_id = db.insert("companies", {
        "domain": domain, "name": name or domain or "(unnamed)",
        "description": payload.get("summary") or payload.get("description"),
        "country": "US" if payload.get("state") else payload.get("country"),
        "hq": payload.get("location"),
        "founded_year": payload.get("year_of_incorporation"),
        "is_synthetic": 1 if _is_synthetic_name(name) else 0,
        "last_signal_at": signal.observed_at, "created_at": db.now_iso()})
    if name:
        _add_alias(company_id, normalize_name(name), "name", 1.0, signal_id)
        _add_alias(company_id, name, "name", 1.0, signal_id)
    if domain:
        _add_alias(company_id, domain, "domain", 1.0, signal_id)
    return company_id


def merge_companies(keep_id: int, absorb_id: int, confidence: float,
                    resolved_by: str = "auto") -> None:
    """Merge absorb→keep. Snapshot the absorbed record first (reversible)."""
    absorb = db.q1("SELECT * FROM companies WHERE id=?", (absorb_id,))
    if not absorb or keep_id == absorb_id:
        return
    snapshot = json.dumps(dict(absorb))
    for table, col in (("signals", "company_id"), ("funding_rounds", "company_id"),
                       ("investments", "company_id"), ("commentary", "company_id"),
                       ("founders", "company_id"), ("scores", "company_id"),
                       ("enrichment_cache", "company_id")):
        try:
            db.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (keep_id, absorb_id))
        except Exception:  # UNIQUE clashes (e.g. enrichment_cache) — keep target's row
            db.execute(f"DELETE FROM {table} WHERE {col}=?", (absorb_id,))
    db.execute("UPDATE company_aliases SET company_id=? WHERE company_id=?", (keep_id, absorb_id))
    _add_alias(keep_id, absorb["name"], "name", confidence, merged_from=snapshot)
    db.execute("DELETE FROM companies WHERE id=?", (absorb_id,))
    db.execute("UPDATE companies SET last_signal_at=(SELECT MAX(observed_at) FROM signals"
               " WHERE company_id=?) WHERE id=?", (keep_id, keep_id))


def unmerge(alias_id: int) -> int | None:
    """Restore a previously merged company from its snapshot. Returns new id."""
    row = db.q1("SELECT * FROM company_aliases WHERE id=? AND merged_from IS NOT NULL", (alias_id,))
    if not row:
        return None
    snap = json.loads(row["merged_from"])
    snap.pop("id", None)
    restored = db.insert("companies", snap)
    db.execute("DELETE FROM company_aliases WHERE id=?", (alias_id,))
    return restored
