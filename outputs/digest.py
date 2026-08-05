"""Component 10 — Mon/Wed/Fri digest. Short, scannable, hard caps per section.
An honest empty section beats padding. Rendered to output/digests/ as HTML
(production target: Resend).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import db, llm, scoring  # noqa: E402
from engine.config import OUTPUT_DIR, thesis  # noqa: E402
from engine.filters import match_theme  # noqa: E402

DIGEST_DIR = OUTPUT_DIR / "digests"


def curate_news(verbose: bool = True, max_llm_rationales: int = 15) -> int:
    """Two passes, cheap before expensive — the same funnel rule as everywhere:

    1. Deterministic relevance for EVERY uncurated item (theme match × source
       weight + HN points). Free, instant, no model.
    2. The one-line 'why it matters' rationale — model judgment — only for the
       top slice by that score. Generating a rationale for all 200 backlog items
       inline was a 10-minute digest build once real (rate-paced) LLM calls
       replaced the instant stub; the tail was never going to be shown anyway.
    Numbers never come from the model either way."""
    weights = {"analysis": 1.4, "mainstream": 1.0}
    scored = []
    n = 0
    for item in db.q("""SELECT n.*, s.payload_json FROM news_items n
                        LEFT JOIN signals s ON n.signal_id=s.id
                        WHERE n.relevance_score IS NULL LIMIT 400"""):
        p = json.loads(item["payload_json"] or "{}")
        text = f"{item['title']} {p.get('summary', '')}"
        theme_key, theme_label = match_theme(text)
        base = 2.0 if theme_key else 0.0
        base *= weights.get(p.get("feed_kind", "mainstream"), 1.0)
        base += min((p.get("points") or 0) / 100.0, 1.0)      # HN points, real
        db.execute("UPDATE news_items SET relevance_score=? WHERE id=?",
                   (round(base, 2), item["id"]))
        if theme_key and base >= 2.0:
            scored.append((base, item["id"], text, theme_label))
        n += 1

    scored.sort(key=lambda x: -x[0])
    for base, item_id, text, theme_label in scored[:max_llm_rationales]:
        if llm.stubbed():
            why = f"{llm.STUB_TEXT} — matched thesis theme: {theme_label}"
        else:
            why = llm.complete(
                "digest",
                "One line (<25 words): why does this news item matter to a deep-tech/AI "
                f"venture fund whose themes include {theme_label}? Only use the provided text.",
                text[:800], tier="classify").strip()[:200]
        db.execute("UPDATE news_items SET why_it_matters=? WHERE id=?", (why, item_id))
    if verbose:
        print(f"  news curation: {n} items scored deterministically; rationale generated "
              f"for top {min(len(scored), max_llm_rationales)}"
              + (" (stubbed — no key)" if llm.stubbed() else ""))
    return n


def _since_last_digest() -> str:
    row = db.q1("SELECT sent_at FROM digests WHERE kind='mwf_digest'"
                " ORDER BY sent_at DESC LIMIT 1")
    return row["sent_at"] if row else "1970-01-01"


def build_digest(verbose: bool = True) -> Path:
    caps = thesis()["digest"]["caps"]
    since = _since_last_digest()
    curate_news(verbose=False)

    deals = [c for c in scoring.latest_scores(("hot",))
             if (c["last_signal_at"] or "") > since]
    deals = scoring.apply_focus_split(deals, caps["deals"])

    sector_calls = [dict(r) for r in db.q(
        "SELECT * FROM sectors_emerging WHERE detected_at > ? ORDER BY ratio DESC LIMIT ?",
        (since, caps["sector_calls"]))]

    news = [dict(r) for r in db.q(
        """SELECT * FROM news_items WHERE why_it_matters IS NOT NULL AND published_at > ?
           ORDER BY relevance_score DESC LIMIT ?""", (since, caps["news"]))]

    peer_moves = [dict(r) for r in db.q(
        """SELECT pe.*, i.name inv, i.tier, c.name comp, s.url, s.payload_json
           FROM peer_events pe JOIN investors i ON pe.investor_id=i.id
           LEFT JOIN companies c ON pe.company_id=c.id
           LEFT JOIN signals s ON pe.source_signal_id=s.id
           WHERE pe.observed_at > ? AND (i.tier=1 OR pe.is_thesis_shift=1)
           ORDER BY pe.observed_at DESC LIMIT ?""", (since, caps["peer_moves"]))]

    def section(title, rows, render):
        html = f"<h2>{title}</h2>"
        if not rows:
            return html + "<p class='empty'>Nothing met the bar since the last digest — " \
                          "empty by design, not padded.</p>"
        return html + "".join(render(r) for r in rows)

    def deal_html(c):
        feats = json.loads(c["features_json"])["computed"]
        rationale = (f"{c['percentile']:.0f}th percentile of {c['cohort_size']} in "
                     f"{c['cohort_key']}; tier-1 count {feats['tier1_count']['value']}; "
                     f"signal velocity {feats['signal_velocity']['value']}/30d. ")
        if not llm.stubbed():
            judged = json.loads(c["features_json"]).get("judged") or {}
            if judged.get("thesis_narrative"):
                rationale += judged["thesis_narrative"][:300]
        else:
            rationale += llm.STUB_TEXT
        slug_link = f"../briefs/{c['name'].lower().replace(' ', '-')[:60]}.md"
        return (f"<div class='deal'><b>{c['name']}</b> — {c['sub_sector'] or c['sector'] or ''}"
                f" <a href='{slug_link}'>full brief</a><p>{rationale}</p></div>")

    def sector_html(s):
        return (f"<div><b>{s['label']}</b> — signal/consensus {s['ratio']}"
                f"{' <b>[CONTRARIAN]</b>' if s['is_contrarian'] else ''}<br>"
                f"<small>{s['thesis_md']}</small></div>")

    def news_html(n):
        return (f"<div><a href='{n['url']}'>{n['title']}</a> <small>({n['source']},"
                f" {(n['published_at'] or '')[:10]})</small><br><em>{n['why_it_matters']}"
                f"</em></div>")

    def peer_html(e):
        vehicle = e["comp"] or (json.loads(e["payload_json"]).get("issuer")
                                if e["payload_json"] else "?")
        shift = " — <b>OFF-THESIS</b>" if e["is_thesis_shift"] else ""
        return (f"<div>{e['inv']} (T{e['tier']}) — {e['event_type']}: {vehicle}"
                f"{shift} <a href='{e['url'] or '#'}'>source</a></div>")

    html = f"""<!doctype html><html><head><meta charset='utf-8'><style>
    body{{font-family:Georgia,serif;max-width:720px;margin:2rem auto;color:#222}}
    h1{{border-bottom:3px solid #1F3B57}} h2{{color:#1F3B57;margin-top:1.6rem}}
    .deal{{margin:0.8rem 0;padding:0.6rem;background:#f6f8fa;border-left:3px solid #1F3B57}}
    .empty{{color:#888;font-style:italic}} small{{color:#666}}
    </style></head><body>
    <h1>Thirdbase deal digest — {db.now_iso()[:10]}</h1>
    <p><small>Window: since {since[:10]}. Every figure traces to a stored signal;
    licence-gated fields are marked, judgment fields are stubbed without an API key.</small></p>
    {section(f"Top new deals (cap {caps['deals']})", deals, deal_html)}
    {section(f"Sector calls (cap {caps['sector_calls']})", sector_calls, sector_html)}
    {section(f"Worth reading (cap {caps['news']})", news, news_html)}
    {section("Peer set activity — tier-1 moves & thesis shifts", peer_moves, peer_html)}
    </body></html>"""

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"digest_{db.now_iso()[:10]}.html"
    path.write_text(html)
    db.insert("digests", {"sent_at": db.now_iso(), "kind": "mwf_digest",
                          "contents_json": json.dumps({
                              "deals": [d["name"] for d in deals],
                              "sectors": [s["label"] for s in sector_calls],
                              "news": [n["title"] for n in news],
                              "peer_moves": len(peer_moves)}),
                          "item_count": len(deals) + len(sector_calls) + len(news)
                          + len(peer_moves)})
    if verbose:
        print(f"  digest rendered: {path} (deals={len(deals)}, sectors={len(sector_calls)},"
              f" news={len(news)}, peer={len(peer_moves)} — empty sections stay empty)")
    return path


if __name__ == "__main__":
    build_digest()
