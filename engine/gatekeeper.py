"""Gatekeeper — nothing the model writes reaches a partner unless the engine can
point at the row it came from.

The motivating failure is small and completely invisible: a brief that says
"backed by Sequoia and Benchmark [S:99999]". It is well-formed, it carries a
citation marker, it passes a regex validator, and every word of it is invented.
Signal 99999 does not exist; no such investment row exists. A partner reads it,
believes it, and the engine has done real damage while looking careful.

So the rule here is deliberately blunt: a sentence written by a model survives
only if the engine can trace it. Three checks, matching what actually gets
fabricated:

  1. CITATIONS  — every [S:n] must be a signal that exists AND belongs to this
                  company. Borrowing another company's signal id is the easiest
                  way to look sourced while being wrong.
  2. NUMBERS    — every figure in model prose must match a value the engine
                  stored. Not "looks plausible" — matches, to 1%.
  3. NAMES      — a named investor or firm must appear in this company's
                  evidence. "Backed by Sequoia" when no such investment exists
                  is the exact failure above.

Enforcement is per sentence, not per brief. The offending sentence is removed
and replaced with a visible marker; everything the engine CAN stand behind still
publishes. Killing a whole brief over one bad clause would push people back to
reading raw signals, which is worse.

What is deliberately NOT policed: labelled opinion. "Founder quality: 7/10" is a
judgement, marked as one, in a section headed "What the AI makes of it". The
gatekeeper's job is to stop invented FACTS, not to stop the model having a view.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import db

REMOVED_MARKER = "[REMOVED: claim could not be traced to a stored source]"

# ----------------------------------------------------------------- patterns --

CITE_RE = re.compile(r"\[S:(\d+)\]")
MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([KMBTkmbt])?\b")
SCALED_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|trillion)\b", re.I)
PLAIN_RE = re.compile(r"(?<![\w.$])(\d[\d,]*(?:\.\d+)?)(?![\w.])")

SCALE = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12,
         "thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}

# Ratings and horizons are opinions the section header already labels as such.
OPINION_NUM_RE = re.compile(r"\b\d{1,2}\s?/\s?10\b|\b\d{1,2}(?:\.\d)?\s*(?:-\s*\d{1,2}\s*)?years?\b",
                            re.I)

# A model that says "no data" must not be punished for saying it clearly.
ABSENCE_RE = re.compile(
    r"\b(no |not |none|insufficient|unknown|unclear|absent|missing|cannot|can't|"
    r"unavailable|undisclosed|nothing)\b", re.I)

# Firms get invented far more often than people, and they carry the most weight
# with a reader — "Sequoia is in" changes a decision. This list is the floor;
# every name in the engine's own investors table is added on top at runtime.
KNOWN_FIRMS = {
    "sequoia", "sequoia capital", "andreessen horowitz", "a16z", "benchmark",
    "accel", "y combinator", "ycombinator", "tiger global", "softbank",
    "index ventures", "lightspeed", "greylock", "kleiner perkins", "founders fund",
    "general catalyst", "bessemer", "insight partners", "khosla ventures", "khosla",
    "nea", "new enterprise associates", "menlo ventures", "coatue", "thrive capital",
    "iconiq", "battery ventures", "redpoint", "craft ventures", "initialized",
    "first round", "union square ventures", "bain capital ventures", "gv",
    "google ventures", "m12", "intel capital", "salesforce ventures", "sapphire",
    "norwest", "mayfield", "matrix partners", "canaan", "dcvc", "lux capital",
    "8vc", "playground global", "eclipse ventures", "root ventures", "susa ventures",
    "homebrew", "village global", "pear vc", "amplify partners", "wing venture",
    "costanoa", "uncork capital", "freestyle", "precursor ventures", "hustle fund",
    "sierra ventures", "scale venture partners", "felicis", "spark capital",
    "ribbit capital", "dragoneer", "altimeter", "d1 capital", "founders collective",
}

# Phrasings that assert a relationship with a named party. These are checked
# hardest because they are both the most load-bearing and the most fabricated.
BACKER_RE = re.compile(
    r"\b(?:backed by|funded by|led by|investors? (?:include|are|is)|investment from|"
    r"raised from|participation from|supported by|portfolio of)\s+(?P<who>[^.;:\n]{2,120})",
    re.I)
PROPER_RE = re.compile(r"\b([A-Z][A-Za-z0-9&'’.-]+(?:\s+[A-Z][A-Za-z0-9&'’.-]+){0,3})\b")

STOPWORD_PROPER = {
    "the", "a", "an", "and", "or", "but", "this", "that", "these", "those", "it",
    "we", "they", "i", "if", "in", "on", "at", "of", "for", "to", "with", "by",
    "founder", "founders", "team", "company", "moat", "tam", "series", "seed",
    "pre-seed", "round", "stage", "sector", "market", "ai", "ml", "llm", "saas",
    "api", "gpu", "r&d", "ceo", "cto", "coo", "cfo", "vp", "phd", "mit", "form d",
    "sec", "edgar", "github", "hacker news", "reddit", "linkedin", "however",
    "given", "based", "while", "although", "overall", "note", "no", "not", "null",
    "n/a", "none", "unknown", "tier", "tier 1", "tier 2", "tier 3", "us", "usa",
    "uk", "eu", "china", "india", "europe", "san francisco", "new york", "london",
    "boston", "seattle", "austin", "berlin", "paris", "singapore", "tel aviv",
}


# ------------------------------------------------------------------ evidence --

@dataclass
class Evidence:
    """Everything the engine actually holds about one company, flattened into the
    three shapes the checks need. Built once per verification pass — a brief
    checks dozens of sentences against the same evidence."""

    company_id: int
    signal_ids: set[int] = field(default_factory=set)
    numbers: set[float] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    blob: str = ""

    def has_number(self, value: float) -> bool:
        """1% tolerance, because the engine itself rounds: 12,500,000 is rendered
        as $12.5M, and a model repeating that back is quoting, not inventing."""
        for v in self.numbers:
            if v == value:
                return True
            scale = max(abs(v), abs(value))
            if scale and abs(v - value) <= scale * 0.01:
                return True
        return False

    def mentions(self, name: str) -> bool:
        n = _norm_name(name)
        return bool(n) and (n in self.names or n in self.blob)


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9 &]", "", (s or "").lower()).strip()


def _add_numbers(target: set[float], text: str) -> None:
    """Store each figure at every scale the engine might render it at. A stored
    amount_usd of 12500000 must satisfy a brief that says $12.5M, 12.5 million,
    or 12,500,000 — all three are the same measurement."""
    for m in MONEY_RE.finditer(text or ""):
        base = _to_float(m.group(1))
        if base is None:
            continue
        mult = SCALE.get((m.group(2) or "").lower(), 1.0)
        target.add(base * mult)
        target.add(base)
    for m in SCALED_RE.finditer(text or ""):
        base = _to_float(m.group(1))
        if base is not None:
            target.add(base * SCALE[m.group(2).lower()])
            target.add(base)
    for m in PLAIN_RE.finditer(text or ""):
        v = _to_float(m.group(1))
        if v is None:
            continue
        target.add(v)
        if v >= 1000:                      # the same value seen as K / M / B
            target.update({v / 1e3, v / 1e6, v / 1e9})


def _to_float(s: str) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def build_evidence(company_id: int) -> Evidence:
    """Read every row the engine stored about this company. Deliberately generous
    about what counts as evidence — the gatekeeper should only ever fire on things
    the engine genuinely never saw, not on things it saw in an unexpected table."""
    ev = Evidence(company_id=company_id)
    chunks: list[str] = []

    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    if c:
        for k in ("name", "domain", "sector", "sub_sector", "stage", "hq", "description"):
            try:
                v = c[k]
            except (KeyError, IndexError):
                v = None
            if v:
                chunks.append(str(v))
                ev.names.add(_norm_name(str(v)))

    for a in db.q("SELECT alias FROM company_aliases WHERE company_id=?", (company_id,)):
        chunks.append(a["alias"] or "")
        ev.names.add(_norm_name(a["alias"] or ""))

    for s in db.q("SELECT id, kind, observed_at, url, payload_json, raw FROM signals"
                  " WHERE company_id=?", (company_id,)):
        ev.signal_ids.add(int(s["id"]))
        chunks += [str(s["url"] or ""), str(s["payload_json"] or ""), str(s["raw"] or "")[:4000],
                   str(s["observed_at"] or "")]

    for r in db.q("SELECT * FROM funding_rounds WHERE company_id=?", (company_id,)):
        chunks.append(json.dumps({k: r[k] for k in r.keys()}, default=str)
                      if hasattr(r, "keys") else str(r))

    for i in db.q("""SELECT i.name, i.tier FROM investments v JOIN investors i
                     ON v.investor_id=i.id WHERE v.company_id=?""", (company_id,)):
        chunks.append(i["name"] or "")
        ev.names.add(_norm_name(i["name"] or ""))

    for f in db.q("SELECT * FROM founders WHERE company_id=?", (company_id,)):
        chunks.append(json.dumps({k: f[k] for k in f.keys()}, default=str))
        ev.names.add(_norm_name(f["name"] or ""))

    for e in db.q("SELECT field, value_json, source FROM enrichment_cache WHERE company_id=?",
                  (company_id,)):
        chunks += [str(e["value_json"] or ""), str(e["source"] or "")]

    for cm in db.q("SELECT platform, quote, url FROM commentary WHERE company_id=?",
                   (company_id,)):
        chunks += [str(cm["quote"] or ""), str(cm["url"] or "")]

    sc = db.q1("SELECT * FROM scores WHERE company_id=? ORDER BY scored_at DESC, id DESC LIMIT 1",
               (company_id,))
    if sc:
        # the computed feature block only — NOT features_json['judged'], which is
        # the model's own prior output. Letting yesterday's invention validate
        # today's would make the gatekeeper launder hallucinations instead of
        # catching them.
        try:
            feats = json.loads(sc["features_json"] or "{}").get("computed") or {}
        except (json.JSONDecodeError, TypeError):
            feats = {}
        chunks.append(json.dumps(feats, default=str))
        for k in ("percentile", "cohort_size", "composite", "market_rank"):
            try:
                if sc[k] is not None:
                    chunks.append(str(sc[k]))
            except (KeyError, IndexError):
                pass

    ev.blob = " \n ".join(chunks).lower()
    _add_numbers(ev.numbers, " \n ".join(chunks))
    ev.names.discard("")
    return ev


def evidence_from_text(*texts: str, company_id: int = 0) -> Evidence:
    """Evidence that is just the source text in front of the model. Used where the
    model is asked to summarise one document — a news item, a set of quotes. The
    rule is the same as everywhere else: it may rephrase what it was given and it
    may not add to it."""
    ev = Evidence(company_id=company_id)
    joined = " \n ".join(t or "" for t in texts)
    ev.blob = joined.lower()
    _add_numbers(ev.numbers, joined)
    return ev


def _known_firm_names() -> set[str]:
    """Every firm the engine has ever heard of. A firm in this set that is NOT in
    a company's evidence is the strongest possible signal of invention: the model
    reached for a name it knows rather than one it was given."""
    names = set(KNOWN_FIRMS)
    try:
        for r in db.q("SELECT name FROM investors"):
            n = _norm_name(r["name"] or "")
            if len(n) > 3:
                names.add(n)
    except Exception:  # noqa: BLE001 — verification must never take the run down
        pass
    return names


# -------------------------------------------------------------------- checks --

def check_sentence(sentence: str, ev: Evidence, firms: set[str] | None = None) -> list[str]:
    """Reasons this sentence cannot be published. Empty list = it survives."""
    reasons: list[str] = []
    text = sentence.strip()
    if not text:
        return reasons

    for m in CITE_RE.finditer(text):
        sid = int(m.group(1))
        if sid not in ev.signal_ids:
            exists = db.q1("SELECT company_id FROM signals WHERE id=?", (sid,))
            reasons.append(f"cites [S:{sid}], which "
                           + ("belongs to a different company"
                              if exists else "does not exist"))

    if not ABSENCE_RE.search(text):
        for value, shown in _claimed_numbers(text):
            if not ev.has_number(value):
                reasons.append(f"states {shown}, which matches no stored value")

    firms = _known_firm_names() if firms is None else firms
    for name in _claimed_entities(text):
        n = _norm_name(name)
        if n in firms and not ev.mentions(n):
            reasons.append(f"names “{name}”, which appears nowhere in this "
                           "company's evidence")
    return reasons


def _claimed_numbers(text: str) -> list[tuple[float, str]]:
    """Figures that assert a measurement. Ratings out of ten and year-count
    horizons are excluded: the brief labels those as opinion, and demanding a
    stored source for an opinion would delete the analysis wholesale."""
    masked = OPINION_NUM_RE.sub(" ", text)
    masked = CITE_RE.sub(" ", masked)
    masked = re.sub(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", " ", masked)   # ISO dates
    out: list[tuple[float, str]] = []
    for m in MONEY_RE.finditer(masked):
        base = _to_float(m.group(1))
        if base is None:
            continue
        out.append((base * SCALE.get((m.group(2) or "").lower(), 1.0), m.group(0).strip()))
    stripped = MONEY_RE.sub(" ", masked)
    for m in SCALED_RE.finditer(stripped):
        base = _to_float(m.group(1))
        if base is not None:
            out.append((base * SCALE[m.group(2).lower()], m.group(0).strip()))
    stripped = SCALED_RE.sub(" ", stripped)
    for m in PLAIN_RE.finditer(stripped):
        v = _to_float(m.group(1))
        # 1-10 as bare integers are almost always list positions or scores, and
        # flagging "3 co-founders" as a hallucination when the founders table has
        # three rows would be the validator being wrong, loudly.
        if v is not None and v > 10:
            out.append((v, m.group(0).strip()))
    return out


def _claimed_entities(text: str) -> list[str]:
    """Named parties. Backer phrasings are split on separators so 'backed by
    Sequoia and Benchmark' yields both, not one run-on that matches nothing."""
    found: list[str] = []
    for m in BACKER_RE.finditer(text):
        for part in re.split(r",| and | & |/|\+", m.group("who")):
            part = part.strip(" .;:\"'()").strip()
            if part and part.lower() not in STOPWORD_PROPER:
                found.append(part)
    for m in PROPER_RE.finditer(text):
        cand = m.group(1).strip()
        if cand.lower() in STOPWORD_PROPER or len(cand) < 3:
            continue
        found.append(cand)
    seen: set[str] = set()
    return [f for f in found if not (f.lower() in seen or seen.add(f.lower()))]


# --------------------------------------------------------------- enforcement --

def _split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?;])\s+|\n+", text or "") if s.strip()]


def verify_text(text: str, ev: Evidence, firms: set[str] | None = None
                ) -> tuple[str, list[dict]]:
    """Sentence-level scrub. Returns the publishable text and what was removed.

    Removal is visible on purpose. Silently deleting a sentence would leave a
    brief that reads as complete while a claim has vanished; the marker tells a
    partner that the model asserted something the engine could not stand behind,
    which is itself useful information about the model and the coverage."""
    if not text or not str(text).strip():
        return text, []
    firms = _known_firm_names() if firms is None else firms
    kept: list[str] = []
    removed: list[dict] = []
    for sentence in _split_sentences(str(text)):
        reasons = check_sentence(sentence, ev, firms)
        if reasons:
            removed.append({"sentence": sentence.strip()[:300], "reasons": reasons})
        else:
            kept.append(sentence.strip())
    if not removed:
        return str(text), []
    body = " ".join(kept).strip()
    return ((body + " " + REMOVED_MARKER).strip() if body else REMOVED_MARKER), removed


# Free-text fields a model fills in. The numeric ratings beside them are labelled
# opinion and pass through untouched; these are the fields that make assertions.
PROSE_FIELDS = ("founder_reasoning", "moat_reasoning", "meta_thesis_reasoning",
                "thesis_narrative", "screening_reason", "exit_reasoning")

# A rating is only worth as much as the reasoning under it. If EVERY sentence
# justifying a score was untraceable, the score is not a conservative estimate —
# it is the residue of a fabrication, and leaving "7/10" on the page with the
# reasoning deleted is the exact "buildup" this whole module exists to stop.
SCORE_FOR_REASON = {"founder_reasoning": "founder_quality",
                    "moat_reasoning": "moat",
                    "meta_thesis_reasoning": "meta_thesis_fit",
                    "exit_reasoning": "exit_horizon_years"}


def verify_judgement(judged: dict | None, company_id: int,
                     ev: Evidence | None = None) -> tuple[dict | None, list[dict]]:
    """Scrub a judgement dict in place of publication. Applied at render time
    rather than at generation time on purpose: judgements stored by earlier runs
    (before this existed, or under a weaker model) are cleaned on their way to a
    reader instead of staying trusted because they are already in the database."""
    if not judged:
        return judged, []
    ev = ev or build_evidence(company_id)
    firms = _known_firm_names()
    out = dict(judged)
    removed: list[dict] = []
    for f in PROSE_FIELDS:
        if isinstance(out.get(f), str) and out[f].strip():
            clean, gone = verify_text(out[f], ev, firms)
            out[f] = clean
            for g in gone:
                removed.append({**g, "field": f})
            if gone and clean.strip() == REMOVED_MARKER and f in SCORE_FOR_REASON:
                out[SCORE_FOR_REASON[f]] = None      # the score falls with its reasoning
    tam = out.get("tam")
    if isinstance(tam, dict):
        assumptions, kept = tam.get("assumptions") or [], []
        for a in assumptions:
            clean, gone = verify_text(str(a), ev, firms)
            kept.append(clean)
            for g in gone:
                removed.append({**g, "field": "tam.assumptions"})
        if assumptions:
            tam = {**tam, "assumptions": kept}
            out["tam"] = tam
    return out, removed


def audit_citations(md: str, company_id: int, ev: Evidence | None = None) -> list[str]:
    """Every [S:n] in a finished document, checked for existence and ownership.
    This is the check the old validator was missing: it confirmed a citation was
    SHAPED correctly and never that it pointed at anything real."""
    ev = ev or build_evidence(company_id)
    bad: list[str] = []
    for sid in {int(m.group(1)) for m in CITE_RE.finditer(md or "")}:
        if sid in ev.signal_ids:
            continue
        row = db.q1("SELECT company_id FROM signals WHERE id=?", (sid,))
        bad.append(f"[S:{sid}] {'belongs to company ' + str(row['company_id']) if row else 'does not exist'}")
    return sorted(bad)


def record(company_id: int, surface: str, removed: list[dict],
           ref: str | None = None) -> None:
    """Persist every removal. A gatekeeper with no audit trail is a censor: the
    point is to be able to answer 'what did the model try to say, and why was it
    stopped' — for a partner asking, and for judging whether the model or the
    coverage is the thing that needs fixing."""
    if not removed:
        return
    try:
        db.insert("gatekeeper_events", {
            "company_id": company_id, "surface": surface, "ref": ref,
            "removed_count": len(removed),
            "detail_json": json.dumps(removed)[:20000],
            "created_at": db.now_iso()})
    except Exception:  # noqa: BLE001 — auditing must never break publication
        pass


def stats(limit_days: int = 30) -> dict:
    """What the gatekeeper has actually caught — shown on the dashboard so the
    claim 'nothing unsourced gets published' is evidenced, not asserted."""
    try:
        rows = db.q("""SELECT surface, COUNT(*) events, SUM(removed_count) removed
                       FROM gatekeeper_events GROUP BY surface""")
        recent = db.q("""SELECT company_id, surface, detail_json, created_at
                         FROM gatekeeper_events ORDER BY id DESC LIMIT 5""")
    except Exception:  # noqa: BLE001
        return {"available": False, "by_surface": [], "recent": []}
    return {"available": True,
            "by_surface": [dict(r) for r in rows],
            "total_removed": sum((r["removed"] or 0) for r in rows),
            "recent": [{"company_id": r["company_id"], "surface": r["surface"],
                        "created_at": r["created_at"],
                        "detail": json.loads(r["detail_json"] or "[]")[:3]}
                       for r in recent]}
