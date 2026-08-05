"""Deterministic event classification over already-ingested signals.

Brief §3(a) requires tracking "funding events, hiring signals, product launches,
founder moves, and customer wins". Funding/hiring/launch arrive typed from their
adapters; **founder moves** and **customer wins** have to be recognised in prose.

That recognition is regex, not a model, for the usual reason: a model asked "did
this company win a customer?" will say yes. A pattern either matched or it did
not, and the matched span is stored so any classification can be checked.

Founder moves double as the brief §2(b) trend inputs — "hiring patterns at
frontier labs" and "founder migration patterns" — by tagging which frontier lab
the person came from.
"""
from __future__ import annotations
import json
import re

from . import db
from .config import thesis

# "X, former OpenAI researcher, raised…" / "ex-DeepMind founders launch…"
FROM_LAB = re.compile(
    r"\b(?:ex|former|formerly (?:of|at)|previously (?:of|at)|alum(?:nus|na)? of|"
    r"veteran of|who left|departing|departed|spun out of)\b[\s,–-]{0,4}"
    r"(?:the\s+)?(?P<lab>[A-Z][\w&.\- ]{2,28}?)\b", re.I)

ALUMNI_MARKER = re.compile(
    r"\b(ex|former|formerly|previously|alum(?:nus|na|ni)?|veteran|left|departed|"
    r"departing|spun out of|spinout from|out of)\b[\s,–-]{0,4}$", re.I)

DEAL_NOUN = re.compile(
    r"\b(contract|deal|mandate|tender|order|account|rfp|agreement|partnership|mou|loi)\b",
    re.I)

# Single-word counterparties are accepted only in a strong deal context, and only
# when they are not ordinary English. Keeps "Siemens" while rejecting "Alliance".
COMMON_WORD = {
    "alliance", "customer", "customers", "client", "clients", "partner", "partners",
    "company", "companies", "startup", "startups", "government", "enterprise",
    "enterprises", "business", "businesses", "team", "teams", "market", "markets",
    "platform", "product", "service", "services", "software", "hardware", "data",
    "cloud", "security", "network", "system", "systems", "solution", "solutions",
    "industry", "sector", "million", "billion", "revenue", "growth", "users",
    "developers", "engineers", "researchers", "students", "hospitals", "banks",
}

MOVE_VERB = re.compile(
    r"\b(leaves|left|departs|departed|exits|steps down|stepping down|resigns|resigned|"
    r"joins|joined|launches|launching|founds|founded|found|co-founded|starts|started|"
    r"to lead|spins out|spun out|is starting|new startup|new venture|new company|"
    r"emerges from stealth|out of stealth|unveils|debuts)\b", re.I)

# A customer win needs a commercial verb AND a named counterparty. The verb list
# is narrow on purpose: "wins" alone matched "won't" and "signs" matched "sign it
# with a key" on real headlines, so contractions are excluded explicitly and the
# bare verbs must be followed by a deal noun.
CUSTOMER_WIN = re.compile(
    r"\b(?:"
    r"wins?(?!['’]t)\s+(?:a\s+|the\s+)?(?:contract|deal|mandate|tender|order|account|rfp)|"
    r"won(?!['’]?t)\s+(?:a\s+|the\s+)?(?:contract|deal|mandate|tender|order|account)|"
    r"lands?\s+(?:a\s+|the\s+)?(?:contract|deal|customer|account)|"
    r"signs?(?!['’]t)\s+(?:a\s+|the\s+)?(?:contract|deal|agreement|partnership|mou|loi)|"
    r"signed\s+(?:a\s+|the\s+)?(?:contract|deal|agreement|partnership)|"
    r"awarded\s+(?:a\s+|the\s+)?(?:contract|tender|mandate)|"
    r"selected by|chosen by|picked by|adopted by|"
    r"partners?\s+with|partnership with|"
    r"deploys?\s+(?:at|with|across)|deployed\s+(?:at|by|across)|"
    r"rolls?\s+out\s+(?:at|with)|goes live\s+(?:at|with)|"
    r"expands?\s+(?:its\s+)?(?:deal|contract|partnership)\s+with"
    r")"
    r"(?P<tail>[^.;!?]{0,90})", re.I)

CONTRACT_VALUE = re.compile(r"[$€£]\s?\d[\d,.]*\s?(?:million|billion|m\b|bn\b|b\b)?", re.I)

# words that make the match about money, awards or sport rather than a customer
NOT_A_CUSTOMER = re.compile(
    r"\b(funding|round|seed|series [a-f]|investment|valuation|award|prize|lawsuit|"
    r"patent|approval|grant|election|game|match|title|medal|championship|"
    r"show hn|ask hn|my startup|our product|we built|launch hn)\b", re.I)

# A counterparty must read as an organisation, not any capitalised token.
ORG_SUFFIX = re.compile(
    r"\b(inc|inc\.|corp|corp\.|corporation|llc|ltd|limited|plc|gmbh|ag|sa|nv|bv|ab|oy|"
    r"as|spa|srl|kk|pte|pty|group|holdings?|bank|airlines|airways|university|"
    r"hospital|health|healthcare|systems|technologies|labs|motors|energy|telecom|"
    r"insurance|pharma|pharmaceuticals|foods|retail|stores|logistics|ministry|"
    r"department|agency|administration|council|authority)\b", re.I)

# Capitalised words that are never a counterparty on their own.
NOT_AN_ORG = {
    "ai", "the", "this", "that", "these", "those", "open", "show", "hn", "new", "our",
    "its", "their", "his", "her", "us", "usa", "uk", "eu", "api", "sdk", "llm", "gpu",
    "saas", "ceo", "cto", "cfo", "vp", "svp", "series", "inc", "corp", "llc", "ltd",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "q1", "q2", "q3", "q4",
}


def _labs() -> list[str]:
    return thesis().get("frontier_labs", [])


def _match_lab(text: str) -> str | None:
    """A founder move requires an explicit departure/alumni construction tying a
    *person* to a configured frontier lab.

    A bare lab mention is NOT enough: on real headlines
    "Microsoft launches new in-house AI models ... versus OpenAI" satisfied
    "lab named + a move verb somewhere" and was wrongly classified. So the lab
    must appear inside (or within 40 characters of) an ex-/former-/spun-out-of
    construction, and must not be the subject of the sentence.
    """
    labs = _labs()
    for m in FROM_LAB.finditer(text):
        window = text[m.start():m.end() + 40]
        for lab in labs:
            hit = re.search(rf"(?<![\w]){re.escape(lab)}(?![\w])", window, re.I)
            if not hit:
                continue
            # Accept only when an alumni marker sits immediately before the lab
            # ("Ex-DeepMind researchers…", "former OpenAI CTO…"). Reject when the
            # lab is the sentence's actor ("Microsoft launches… versus OpenAI").
            abs_pos = m.start() + hit.start()
            lead = text[max(0, abs_pos - 24):abs_pos].lower()
            if ALUMNI_MARKER.search(lead):
                return lab
    return None


def _plausible_org(name: str, allow_single: bool = False) -> bool:
    """Counterparty must read as an organisation: an org suffix, a multi-word
    proper name, an entity already known to the database, or — only in an
    explicit deal context — a single proper noun that isn't ordinary English."""
    n = name.strip()
    if not n or n.lower() in NOT_AN_ORG:
        return False
    words = [w for w in n.split() if w]
    if len(words) == 1 and words[0].lower() in NOT_AN_ORG:
        return False
    if ORG_SUFFIX.search(n):
        return True
    if len(words) >= 2 and all(w[:1].isupper() for w in words):
        return True
    known = db.q1("""SELECT 1 FROM companies WHERE lower(name)=lower(?)
                     UNION SELECT 1 FROM investors WHERE lower(name)=lower(?)""", (n, n))
    if known:
        return True
    if allow_single and len(words) == 1 and len(n) >= 4 \
            and n.lower() not in COMMON_WORD and n[:1].isupper():
        return True
    return False


def classify_signal(kind: str, text: str) -> list[dict]:
    """Return zero or more derived events with the span that justified each."""
    out: list[dict] = []
    if not text:
        return out

    if kind in ("news", "launch", "commentary", "funding_event"):
        lab = _match_lab(text)
        if lab and MOVE_VERB.search(text):
            m = MOVE_VERB.search(text)
            out.append({"event": "founder_move", "frontier_lab": lab,
                        "span": text[max(0, m.start() - 70):m.end() + 70].strip()})

        for m in CUSTOMER_WIN.finditer(text):
            window = text[max(0, m.start() - 60):m.end() + 60]
            if NOT_A_CUSTOMER.search(window):
                continue
            tail = (m.group("tail") or "").strip()
            # a verb phrase that already names a deal noun is strong context, so a
            # single-word counterparty ("Siemens") is admissible there
            allow_single = bool(DEAL_NOUN.search(m.group(0)))
            party = None
            for cand in re.finditer(r"\b([A-Z][\w&.\-]{2,}(?:\s+[A-Z][\w&.\-]{1,}){0,3})", tail):
                if _plausible_org(cand.group(1), allow_single=allow_single):
                    party = cand.group(1).strip()
                    break
            if not party:
                continue
            val = CONTRACT_VALUE.search(window)
            out.append({"event": "customer_win", "counterparty": party,
                        "contract_value_text": val.group(0) if val else None,
                        "span": window.strip()})
            break     # one per signal; the span carries the evidence
    return out


def derive_events(verbose: bool = True) -> dict:
    """Sweep signals, emit typed derived signals, and record founder provenance.

    Derived signals are appended (never overwriting the source signal) under the
    synthetic source name 'derived_events', so provenance stays traceable back to
    the original url via `parent_signal_id` in the payload.
    """
    source_id = db.get_source_id("derived_events")
    counts = {"founder_move": 0, "customer_win": 0}
    rows = db.q("""SELECT s.id, s.kind, s.company_id, s.observed_at, s.url, s.raw,
                          s.payload_json
                   FROM signals s
                   WHERE s.kind IN ('news','launch','commentary','funding_event')
                   AND s.fetch_mode != 'synthetic_demo'""")
    for r in rows:
        p = json.loads(r["payload_json"])
        text = " ".join(filter(None, [
            str(p.get("title") or ""), str(p.get("summary") or ""),
            str(p.get("story_text") or "")[:600], str(p.get("selftext") or "")[:600],
            (r["raw"] or "")[:600]]))
        for ev in classify_signal(r["kind"], text):
            kind = ev.pop("event")
            dedupe = f"derived:{kind}:{r['id']}"
            payload = {**ev, "parent_signal_id": r["id"], "parent_kind": r["kind"],
                       "title": p.get("title")}
            sid = db.insert_signal(source_id, kind, r["observed_at"], payload,
                                   r["url"], dedupe, raw=ev.get("span"),
                                   company_id=r["company_id"])
            if sid is None:
                continue
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "founder_move" and r["company_id"]:
                _record_frontier_alum(r["company_id"], ev.get("frontier_lab"), r["url"])
    if verbose:
        print(f"  derived events: {counts['founder_move']} founder move(s), "
              f"{counts['customer_win']} customer win(s) — regex-classified, span stored")
    return counts


def _record_frontier_alum(company_id: int, lab: str | None, url: str | None) -> None:
    """Populate founders.frontier_lab_alum from real evidence only."""
    if not lab:
        return
    existing = db.q1("SELECT id, notes FROM founders WHERE company_id=? AND"
                     " frontier_lab_alum=1", (company_id,))
    if existing:
        return
    db.insert("founders", {
        "company_id": company_id, "name": "(unnamed — from press mention)",
        "frontier_lab_alum": 1,
        "notes": f"frontier-lab background: {lab} (evidence: {url or 'no url'})"})


def talent_flow_summary(days: int = 90) -> list[dict]:
    """Brief §2(b): where founder attention is moving, by originating lab."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.q("""SELECT payload_json, observed_at, url FROM signals
                   WHERE kind='founder_move' AND observed_at >= ?""", (cutoff,))
    by_lab: dict[str, dict] = {}
    for r in rows:
        lab = json.loads(r["payload_json"]).get("frontier_lab") or "unknown"
        rec = by_lab.setdefault(lab, {"lab": lab, "moves": 0, "latest": None, "urls": []})
        rec["moves"] += 1
        rec["latest"] = max(rec["latest"] or "", r["observed_at"] or "")
        if r["url"] and len(rec["urls"]) < 3:
            rec["urls"].append(r["url"])
    return sorted(by_lab.values(), key=lambda x: -x["moves"])
