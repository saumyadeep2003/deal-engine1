"""Component 16 — conversational interface. CLI REPL (production: Slack Bolt).

Quantitative questions are answered with SQL — "who's quietly investing in
robotics" is a database query wearing a narrative. Qualitative questions
retrieve real signals/commentary and cite source + date on every answer.
LLM synthesis is used when a key is present; otherwise raw cited evidence is
shown with a loud [STUB] marker instead of fake analysis.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import db, gatekeeper, llm  # noqa: E402
from engine.filters import match_theme, theme_regexes  # noqa: E402
from engine.sectors import scan_thesis, _tokens  # noqa: E402

EXAMPLE_QUESTIONS = [
    "what are the best deals in defence tech right now?",
    "summarise what people are saying about AMP Robotics on X",
    "who's quietly investing in robotics?",
]


def _cite(url: str | None, date: str | None) -> str:
    return f" [{(date or '')[:10]} — {url or 'no url'}]"


def best_deals(sector_text: str) -> str:
    key, label = match_theme(sector_text)
    if not key:
        hits = scan_thesis(sector_text, limit=5)
        if not hits:
            return ("No theme match and no companies overlap that description in the corpus. "
                    "Honest empty answer — widen the ingest lookback or refine the thesis.")
        return "Closest matches by description overlap:\n" + "\n".join(
            f"  {i + 1}. {h['company']} ({h['sector']}) — relevance {h['relevance']:.1f}"
            for i, h in enumerate(hits))
    rows = db.q("""SELECT c.name, c.hq, c.stage, s.percentile, s.cohort_size, s.recommendation,
                          s.human_override, c.last_signal_at,
                          (SELECT url FROM signals WHERE company_id=c.id
                           ORDER BY observed_at DESC LIMIT 1) url
                   FROM companies c JOIN scores s ON s.company_id=c.id
                   WHERE s.id=(SELECT id FROM scores WHERE company_id=c.id
                               ORDER BY scored_at DESC, id DESC LIMIT 1)
                   AND c.sector=? AND c.is_synthetic=0
                   AND c.status IN ('hot','watchlist','pipeline')
                   ORDER BY s.percentile DESC LIMIT 6""", (key,))
    if not rows:
        return f"No {label} companies in the pipeline yet (real sources only, no invention)."
    lines = [f"Best {label} deals in the pipeline (ranked within cohort, not in isolation):"]
    for i, r in enumerate(rows, 1):
        rec = r["human_override"] or r["recommendation"]
        lines.append(f"  {i}. {r['name']} ({r['hq'] or 'HQ?'}) — {r['percentile']:.0f}th pct"
                     f" of {r['cohort_size']} — {rec}{_cite(r['url'], r['last_signal_at'])}")
    if rows and rows[0]["cohort_size"] < 20:
        lines.append("  note: cohort < 20 members — ranking flagged low-confidence.")
    return "\n".join(lines)


def commentary_about(company_text: str) -> str:
    comp = None
    for c in db.q("SELECT id, name FROM companies WHERE is_synthetic=0"):
        if c["name"].lower() in company_text.lower() or (
                len(c["name"]) > 5 and c["name"].split()[0].lower() in company_text.lower()):
            comp = c
            break
    if not comp:
        return "That company isn't in the pipeline. (I only answer from ingested data.)"
    rows = db.q("SELECT * FROM commentary WHERE company_id=? ORDER BY observed_at DESC LIMIT 6",
                (comp["id"],))
    x_note = ("X/Twitter commentary requires the paid X API (adapter wired, no key) — "
              "showing free-source commentary instead (HN/Reddit).")
    if not rows:
        return (f"No commentary captured yet for {comp['name']}. {x_note} "
                "Nothing is invented in its place.")
    lines = [f"What people are saying about {comp['name']} ({x_note}):"]
    for r in rows:
        lines.append(f"  - [{r['platform']}] ({r['sentiment']}) “{(r['quote'] or '')[:160]}”"
                     f"{_cite(r['url'], r['observed_at'])}")
    if not llm.stubbed():
        quotes = "\n".join(r["quote"] or "" for r in rows)
        summary = llm.complete("chat", "Summarise the sentiment in these real quotes in 2 lines. "
                               "Do not add facts not present.", quotes, tier="chat")
        # a summary of quotes may only contain what the quotes contain
        summary, removed = gatekeeper.verify_text(
            summary, gatekeeper.evidence_from_text(quotes, comp["name"],
                                                   company_id=comp["id"]))
        gatekeeper.record(comp["id"], "chat_commentary", removed)
        lines.append(f"  Summary: {summary}")
    return "\n".join(lines)


def quiet_investors(sector_text: str) -> str:
    key, label = match_theme(sector_text)
    if not key:
        return "Which sector? No thesis theme matched."
    rows = db.q("""
        SELECT i.name, i.tier, COUNT(pe.id) events,
               SUM(CASE WHEN pe.event_type='fund_formation' THEN 1 ELSE 0 END) funds,
               MAX(pe.observed_at) latest,
               (SELECT url FROM signals WHERE id=(SELECT source_signal_id FROM peer_events
                 WHERE investor_id=i.id ORDER BY observed_at DESC LIMIT 1)) url
        FROM peer_events pe JOIN investors i ON pe.investor_id=i.id
        LEFT JOIN companies c ON pe.company_id=c.id
        LEFT JOIN signals s ON pe.source_signal_id=s.id
        WHERE c.sector=? OR s.payload_json LIKE ?
        GROUP BY i.id ORDER BY events DESC LIMIT 8""", (key, f"%{key.split('_')[0]}%"))
    # 'quietly' = activity in filings/vehicles but little news coverage
    if not rows:
        return (f"No {label} investor activity observed yet in free sources "
                "(EDGAR fund formations + RSS syndicates). With Crunchbase/PitchBook "
                "licensed this fills in densely.")
    lines = [f"Investors active in {label} (from real Form D vehicles + observed rounds; "
             "'quiet' = filings without press):"]
    for r in rows:
        news_n = db.q1("SELECT COUNT(*) c FROM signals WHERE kind='news' AND raw LIKE ?",
                       (f"%{r['name']}%",))["c"]
        quiet = " — QUIET (filings, no press seen)" if news_n == 0 else f" — {news_n} press mentions"
        lines.append(f"  - {r['name']} (T{r['tier'] or '?'}): {r['events']} event(s),"
                     f" {r['funds'] or 0} fund vehicle(s){quiet}{_cite(r['url'], r['latest'])}")
    return "\n".join(lines)


def answer(q: str) -> str:
    ql = q.lower()
    if re.search(r"best deals|top deals|best companies", ql):
        return best_deals(q)
    if re.search(r"saying about|commentary|sentiment|people .* about", ql):
        return commentary_about(q)
    if re.search(r"quietly investing|who'?s investing|investing in", ql):
        return quiet_investors(q)
    if re.search(r"emerging|sector.* (tomorrow|quarter)|sub-?sectors", ql):
        rows = db.q("SELECT * FROM sectors_emerging ORDER BY ratio DESC LIMIT 3")
        if not rows:
            return "No emerging sector cleared the evidence bar yet (source-diversity gated)."
        out = ["Emerging sub-sectors by signal-to-consensus ratio:"]
        for r in rows:
            ev = json.loads(r["evidence_json"] or "[]")
            out.append(f"  - {r['label']} — ratio {r['ratio']}, diversity {r['source_diversity']}"
                       f"{' [CONTRARIAN]' if r['is_contrarian'] else ''}"
                       + (f" e.g. {ev[0]['url']}" if ev else ""))
        return "\n".join(out)
    # fallback: retrieval + (LLM | cited evidence)
    hits = scan_thesis(q, limit=5)
    ev_lines = []
    for h in hits:
        comp = db.q1("SELECT id FROM companies WHERE name=?", (h["company"],))
        sig = db.q1("SELECT url, observed_at, raw FROM signals WHERE company_id=?"
                    " ORDER BY observed_at DESC LIMIT 1", (comp["id"],)) if comp else None
        if sig:
            ev_lines.append(f"  - {h['company']}: {(sig['raw'] or '')[:100]}"
                            f"{_cite(sig['url'], sig['observed_at'])}")
    if llm.stubbed():
        return ("[STUB: no API key — synthesis unavailable] Raw cited evidence instead:\n"
                + ("\n".join(ev_lines) if ev_lines else "  (nothing relevant ingested)"))
    ctx = "\n".join(ev_lines)
    resp = llm.complete("chat", "Answer the partner's question ONLY from this evidence; cite "
                        "the given urls/dates; say so plainly if the evidence is insufficient.",
                        f"Question: {q}\nEvidence:\n{ctx}", tier="chat")
    # The answer is allowed to be a rearrangement of ctx and nothing more. Chat is
    # where a partner is most likely to act on a single sentence without opening
    # the brief behind it, so it gets the same gate as anything written to disk.
    resp, removed = gatekeeper.verify_text(resp, gatekeeper.evidence_from_text(ctx, q))
    gatekeeper.record(0, "chat_answer", removed, ref=q[:120])
    return resp + "\nSources:\n" + ctx


def main() -> None:
    print("Deal-engine chat. Ctrl-D to exit. Try:")
    for q in EXAMPLE_QUESTIONS:
        print(f"  • {q}")
    while True:
        try:
            q = input("\npartner> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        print(answer(q))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(answer(" ".join(sys.argv[1:])))
    else:
        main()
