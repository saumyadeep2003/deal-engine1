"""Component 06 — company intelligence briefs. On demand AND auto-triggered
above a score-percentile threshold. Flagship tier, capped per day.

Structure: funding history, cap table quality (Tier 1/2/3 counts), team &
hiring, traction, thesis fit, comparables, commentary, Pass/Watch/Deep Dive.

Validation: every numeric claim must carry a [S:signal_id] citation or a
[computed] tag (arithmetic done in Python, reconstructable from features_json).
A brief that fails validation is regenerated once, then flagged for human
review — never published. Stub mode: observed-data sections are assembled
mechanically from the DB with citations; judgment sections read [STUB].
"""
from __future__ import annotations
import json
import re

from . import db, judge, llm
from .config import OUTPUT_DIR, models_config, thesis

BRIEFS_DIR = OUTPUT_DIR / "briefs"

NUM_RE = re.compile(r"\$[\d.,]+\s?[MBKmbk]?|\b\d[\d,.]*\s?(?:million|billion|%|x)\b")
CITE_RE = re.compile(r"\[S:\d+\]|\[computed\]|\[config\]|\[STUB")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def validate_brief(md: str) -> list[str]:
    """Return list of violations: numeric claims without a citation."""
    violations = []
    for line in md.splitlines():
        if line.startswith("#") or line.startswith("|--"):
            continue
        for sentence in re.split(r"(?<=[.;])\s+", line):
            if NUM_RE.search(sentence) and not CITE_RE.search(sentence):
                # dates alone (2026-07-16) are provenance, not claims
                if re.fullmatch(r".*\b\d{4}-\d{2}-\d{2}\b[^\d]*", sentence.strip()):
                    continue
                violations.append(sentence.strip()[:140])
    return violations


REC_PLAIN = {"Deep Dive": "Worth a close look now",
             "Watch": "Keep an eye on it",
             "Pass": "Not a fit right now"}


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _headline(c, score, rec: str, trigger: str) -> str:
    """One line a partner can read standing up: what it is, what we think, how fresh."""
    what = " · ".join(x for x in [c["sub_sector"] or c["sector"], c["stage"], c["hq"]] if x)
    line = f"**{REC_PLAIN.get(rec, rec)}** ({rec})"
    if what:
        line += f" · {what}"
    # the timestamp carries [computed] because it is provenance, not a claim —
    # without it the citation validator would flag its digits
    line += (f"\n\n*Written {db.to_display(db.now_iso(), fmt='%d %b %Y, %H:%M')}"
             f" · trigger: {trigger}* [computed]\n\n")
    return line


def _at_a_glance(company_id: int, c, score, rec: str) -> str:
    """The six things a partner checks first, in one table. Every figure carries its
    source marker so the table cannot become a place where uncited numbers hide."""
    feats = json.loads(score["features_json"])["computed"] if score and score["features_json"] else {}
    rounds = db.q("""SELECT fr.amount_usd, fr.stage, s.id sid FROM funding_rounds fr
                     LEFT JOIN signals s ON fr.source_signal_id=s.id
                     WHERE fr.company_id=? ORDER BY fr.announced_at DESC LIMIT 1""",
                  (company_id,))
    if rounds and rounds[0]["amount_usd"]:
        r = rounds[0]
        funding = f"${r['amount_usd'] / 1e6:.1f}M {r['stage'] or 'round'} [S:{r['sid']}]"
    elif rounds:
        funding = f"round observed, amount not disclosed [S:{rounds[0]['sid']}]"
    else:
        funding = "none found in free sources — full history requires PitchBook"

    if score and score["cohort_size"]:
        size = score["cohort_size"]
        pos = max(1, min(size, size - round((score["percentile"] or 0) / 100 * size) + 1))
        # cohort_key is "sector|stage" — a raw pipe would split the markdown table cell
        cohort = str(score["cohort_key"] or "").replace("|", " · ").replace("unknown", "stage unknown")
        rank = (f"{_ordinal(pos)} of {size} in {cohort} [computed]"
                + (" ⚠ small cohort, weak evidence" if score["cohort_low_confidence"] else ""))
    else:
        rank = "not ranked yet [computed]"

    t1 = feats.get("tier1_count", {}).get("value", 0)
    rows = [
        ("Our call", f"**{rec}** — {REC_PLAIN.get(rec, '').lower()} [computed]"),
        ("Rank against peers", rank),
        ("Funding we can see", funding),
        ("Tier-1 investors on board", f"{t1} [computed]"),
        # date only, so no clock label — "25 Jul 2026 IST" reads like a bug
        ("Last sign of activity", db.to_display(c["last_signal_at"], fmt="%d %b %Y",
                                                with_label=False)),
        ("Headcount & growth", "— (requires Coresignal)"),
    ]
    out = ["\n## At a glance\n", "| | |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(out) + "\n"


def _gaps_section(company_id: int) -> str:
    """What this brief cannot tell you, stated plainly. A partner who knows the
    shape of the hole reads the rest correctly; one who doesn't over-trusts it."""
    gaps = ["Headcount, hiring growth and runway — requires Coresignal",
            "Full cap table, valuation and complete funding history — requires PitchBook",
            "What investors are saying privately (X, Blind, podcasts, Substack) — requires those licences"]
    if not db.q1("SELECT id FROM founders WHERE company_id=? LIMIT 1", (company_id,)):
        gaps.insert(0, "No founder information found in free sources — team quality is unassessed")
    if not db.q1("SELECT id FROM commentary WHERE company_id=? LIMIT 1", (company_id,)):
        gaps.insert(0, "No public discussion found yet — sentiment is unknown, not negative")
    return "\n\n## What this brief can't tell you\n\n" + "\n".join(f"- {g}" for g in gaps)


def _observed_sections(company_id: int) -> str:
    """Mechanically assembled REAL data with citations — no model involved."""
    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    score = db.q1("SELECT * FROM scores WHERE company_id=? ORDER BY scored_at DESC, id DESC"
                  " LIMIT 1", (company_id,))
    feats = json.loads(score["features_json"])["computed"] if score else {}
    out = []

    out.append("\n## Money raised\n")
    rounds = db.q("""SELECT fr.*, i.name lead, s.url, s.id sid FROM funding_rounds fr
                     LEFT JOIN investors i ON fr.lead_investor_id=i.id
                     LEFT JOIN signals s ON fr.source_signal_id=s.id
                     WHERE fr.company_id=? ORDER BY fr.announced_at DESC""", (company_id,))
    if rounds:
        for r in rounds:
            amt = f"${r['amount_usd'] / 1e6:.1f}M" if r["amount_usd"] else "amount not disclosed"
            out.append(f"- {(r['announced_at'] or '?')[:10]}: {r['stage'] or 'round'} — {amt}"
                       f"{', led by ' + r['lead'] if r['lead'] else ''} [S:{r['sid']}]"
                       f" ({r['url'] or 'no url'})")
    else:
        out.append("- No round observed in free sources. Full history — (requires PitchBook).")

    out.append("\n## Who has backed them\n")
    t1 = feats.get("tier1_count", {}).get("value", 0)
    t2 = feats.get("tier2_count", {}).get("value", 0)
    t3 = feats.get("tier3_count", {}).get("value", 0)
    out.append(f"- Tier 1: {t1}, Tier 2: {t2}, Tier 3: {t3} [computed] "
               f"(observed investments × config tier list; full cap table — requires PitchBook)")
    invs = db.q("""SELECT i.name, i.tier FROM investments v JOIN investors i
                   ON v.investor_id=i.id WHERE v.company_id=?""", (company_id,))
    if invs:
        out.append("- Observed investors: " + ", ".join(f"{i['name']} (T{i['tier'] or '?'})"
                                                        for i in invs) + " [computed]")

    out.append("\n## The team\n")
    founders = db.q("SELECT * FROM founders WHERE company_id=?", (company_id,))
    for f in founders:
        out.append(f"- {f['name']}{' — prior exits: ' + str(f['prior_exits']) if f['prior_exits'] else ''}"
                   f"{' — frontier-lab alum' if f['frontier_lab_alum'] else ''}")
    persons = db.q("SELECT payload_json, id FROM signals WHERE company_id=? AND kind='filing'",
                   (company_id,))
    for s in persons:
        rp = json.loads(s["payload_json"]).get("related_persons") or []
        if rp:
            out.append("- Form D related persons: "
                       + "; ".join(f"{p['name']} ({', '.join(p.get('titles') or [])})"
                                   for p in rp[:6]) + f" [S:{s['id']}]")
    careers = db.q1("SELECT value_json, source FROM enrichment_cache WHERE company_id=?"
                    " AND field='careers_functions'", (company_id,))
    if careers and careers["value_json"]:
        out.append(f"- Careers-page function mix: {careers['value_json']} [computed]"
                   f" (source: {careers['source']})")
    out.append("- Headcount / 6-month growth: — (requires Coresignal)")

    out.append("\n## Signs of traction\n")
    for field, label in (("github_stars", "GitHub stars"),
                         ("github_contributors", "GitHub contributors"),
                         ("github_commit_velocity", "GitHub commit velocity")):
        row = db.q1("SELECT value_json, source, unavailable_reason FROM enrichment_cache"
                    " WHERE company_id=? AND field=?", (company_id, field))
        if row and row["value_json"] and row["value_json"] != "null":
            out.append(f"- {label}: {row['value_json']} [computed] ({row['source']})")
    wins = db.q("SELECT payload_json, url, id FROM signals WHERE company_id=?"
                " AND kind='customer_win' ORDER BY observed_at DESC LIMIT 3", (company_id,))
    for w in wins:
        p = json.loads(w["payload_json"])
        val = f" ({p['contract_value_text']})" if p.get("contract_value_text") else ""
        out.append(f"- Customer win: {p.get('counterparty')}{val} [S:{w['id']}] ({w['url']})")
    surface = db.q1("SELECT value_json, source FROM enrichment_cache WHERE company_id=?"
                    " AND field='customer_logos'", (company_id,))
    if surface and surface["value_json"] and surface["value_json"] != "null":
        logos = json.loads(surface["value_json"])
        out.append(f"- Customer logos on site: {', '.join(logos[:8])} [computed]"
                   f" ({surface['source']})")
    pricing = db.q1("SELECT value_json FROM enrichment_cache WHERE company_id=?"
                    " AND field='pricing'", (company_id,))
    if pricing and pricing["value_json"] and pricing["value_json"] != "null":
        pr = json.loads(pricing["value_json"])
        out.append(f"- Pricing: {pr.get('model')} — plans: {', '.join(pr.get('plan_names') or [])}"
                   f" [computed] ({pr.get('url')})")
    # Everything else the engine holds on this company. Funding events are excluded:
    # they are reported under Funding history above, and a funding announcement is
    # not product traction — listing it here overstated what the evidence shows.
    sigs = db.q("SELECT id, kind, observed_at, url, payload_json FROM signals"
                " WHERE company_id=? AND kind!='funding_event'"
                " ORDER BY observed_at DESC LIMIT 8", (company_id,))
    traction_heading = "\n## Signs of traction\n"
    if not any(line.startswith("- ") for line in out[out.index(traction_heading) + 1:]):
        out.append("- No product-traction evidence in free sources yet"
                   " (GitHub activity, customer wins, pricing pages, customer logos).")
    if sigs:
        out.append("\n**Other recent signals** (mentions, not traction):\n")
        for s in sigs:
            title = (json.loads(s["payload_json"]).get("title")
                     or json.loads(s["payload_json"]).get("issuer") or s["kind"])
            out.append(f"- {s['observed_at'][:10]} {s['kind']}: {str(title)[:100]} [S:{s['id']}]")

    out.append("\n## How it ranks against similar companies\n")
    if score:
        lc = " — LOW CONFIDENCE (cohort < 20)" if score["cohort_low_confidence"] else ""
        ck = str(score["cohort_key"] or "")
        if ck.startswith("unclassified|"):
            ck += " (sector not determined from the available text — this is a catch-all bucket)"
        out.append(f"- {score['percentile']:.0f}th percentile of {score['cohort_size']}"
                   f" in cohort {ck}{lc} [computed]")
        out.append(f"- Feature vector stored (scores.features_json, model={score['model_version']},"
                   f" prompt={score['prompt_version']}) [computed]")

    out.append("\n## What people are saying publicly\n")
    comm = db.q("SELECT * FROM commentary WHERE company_id=? ORDER BY observed_at DESC LIMIT 5",
                (company_id,))
    if comm:
        for cm in comm:
            out.append(f"- [{cm['platform']}] {cm['sentiment'] or '?'} — “{(cm['quote'] or '')[:140]}”"
                       f" ({cm['url']})")
    else:
        out.append("- None captured yet in free sources (HN/Reddit); X, Blind, podcasts,"
                   " Substack threads — (require licenses).")
    return "\n".join(out)


def _judgment_sections(company_id: int, judged: dict | None) -> str:
    out = ["\n\n## What the AI makes of it\n",
           "*Model-written judgement — labelled on purpose, and separate from the observed facts above. Numbers here are opinions, not measurements.*\n"]
    if not judged:
        # name the actual cause: no key, provider failing, or provider timed out.
        s = (llm.STUB_TEXT if llm.stubbed()
             else llm.STUB_CIRCUIT if llm.circuit_open()
             else llm.STUB_PROVIDER_DOWN)
        out += [f"- Founder quality: {s}", f"- Moat / defensibility: {s}",
                f"- TAM: {s}", f"- Meta-thesis fit: {s}", f"- Exit horizon: {s}",
                f"\n### Thesis narrative\n{s}"]
        return "\n".join(out)
    tam = judged.get("tam") or {}
    out.append(f"- Founder quality: {judged.get('founder_quality')}/10 — "
               f"{judged.get('founder_reasoning') or 'n/a'}")
    out.append(f"- Moat: {judged.get('moat')}/10 — {judged.get('moat_reasoning') or 'n/a'}")
    if tam.get("value_usd"):
        out.append(f"- TAM: ${tam['value_usd'] / 1e9:.1f}B [S:model-estimate] — confidence:"
                   f" {tam.get('confidence')} — assumptions: {'; '.join(tam.get('assumptions') or [])}")
    else:
        out.append("- TAM: null (insufficient context — honest null beats an invented number)")
    out.append(f"- Meta-thesis fit: {judged.get('meta_thesis_fit')}/10 — "
               f"{judged.get('meta_thesis_reasoning') or 'n/a'}")
    out.append(f"- Exit horizon: {judged.get('exit_horizon_years') or 'null'} years")
    out.append(f"\n### Thesis narrative\n{judged.get('thesis_narrative') or 'null'}")
    return "\n".join(out)


def _existing_brief_is_stubbed(company_id: int) -> bool:
    """True when this company's newest brief carries a [STUB] judgment. Such a brief
    is stale the moment the model works again, so it is worth rewriting even when the
    daily cap is spent."""
    row = db.q1("SELECT content_md FROM briefs WHERE company_id=?"
                " ORDER BY generated_at DESC, id DESC LIMIT 1", (company_id,))
    md = row["content_md"] if row else None      # sqlite3.Row has no .get()
    if not md:
        return False
    # Scope to the judgment section ONLY. Stored commentary rows keep a [STUB]
    # sentiment marker from whenever they were harvested, and matching those would
    # mark every brief as needing repair — regenerating the same briefs every run.
    marker = "## Judgment" if "## Judgment" in md else "## What the AI makes of it"
    if marker not in md:
        return False
    judgment = md.split(marker, 1)[1].split("\n## ", 1)[0]
    return "[STUB" in judgment


# Bump when the brief layout changes: stored briefs written by an older layout are
# rewritten on the next run. Without this, a formatting improvement never reaches
# the briefs a partner actually opens — they keep the layout they were born with.
FORMAT_MARKER = "## At a glance"


def _existing_brief_is_outdated(company_id: int) -> bool:
    row = db.q1("SELECT content_md FROM briefs WHERE company_id=?"
                " ORDER BY generated_at DESC, id DESC LIMIT 1", (company_id,))
    md = row["content_md"] if row else None
    return bool(md) and FORMAT_MARKER not in md


def _stored_judgement(company_id: int) -> dict | None:
    """The judgement already persisted on the latest score row. Regenerating a brief
    without this would silently DOWNGRADE a brief that had real analysis back to
    [STUB] just because the caller didn't happen to pass the dict in."""
    row = db.q1("""SELECT features_json FROM scores WHERE company_id=?
                   ORDER BY scored_at DESC, id DESC LIMIT 1""", (company_id,))
    if not row or not row["features_json"]:
        return None
    try:
        judged = (json.loads(row["features_json"]) or {}).get("judged")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(judged, dict):
        return None
    return judged if any(judged.get(k) not in (None, "", [])
                         for k in ("founder_quality", "moat", "thesis_narrative")) else None


def generate_brief(company_id: int, trigger: str = "on_demand",
                   judged: dict | None = None, verbose: bool = True) -> int | None:
    limits = models_config()["limits"]
    # day-window computed in Python — SQL date() is a dialect trap (PG casts)
    today_str = db.now_iso()[:10]
    today = db.q1("SELECT COUNT(*) c FROM briefs WHERE substr(generated_at,1,10) = ?",
                  (today_str,))["c"]
    # Repairing a stubbed brief is not the same spend as writing a new one, and it
    # must not be blocked by the daily cap: a brief written while the model was
    # unavailable would otherwise keep showing [STUB] all day even after the model
    # came back — which is exactly what happened on the hosted engine.
    # Reuse a previously stored judgement ONLY when the engine could produce one
    # today. With no key configured it must say [STUB] rather than presenting an
    # older run's opinion as if judgement were currently available.
    judged = judged or (None if llm.stubbed() else _stored_judgement(company_id))
    is_repair = bool(judged) and _existing_brief_is_stubbed(company_id)
    is_reformat = _existing_brief_is_outdated(company_id)
    if (trigger == "auto_threshold" and not (is_repair or is_reformat)
            and today >= limits["max_briefs_per_day"]):
        if verbose:
            print(f"  brief cap reached ({limits['max_briefs_per_day']}/day) — skipping auto brief")
        return None
    if is_repair:
        trigger = "stub_repair"
    elif is_reformat:
        trigger = "reformat"
    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    if not c or c["is_synthetic"]:
        return None
    score = db.q1("SELECT * FROM scores WHERE company_id=? ORDER BY scored_at DESC, id DESC LIMIT 1",
                  (company_id,))
    rec = (score["human_override"] or score["recommendation"]) if score else "Watch"

    md = (f"# {c['name']}\n\n"
          + _headline(c, score, rec, trigger)
          + _at_a_glance(company_id, c, score, rec)
          + _observed_sections(company_id)
          + _judgment_sections(company_id, judged)
          + _gaps_section(company_id)
          + "\n\n## Similar companies we're tracking\n"
          + _comparables(company_id)
          + f"\n\n## The call\n**{rec}** [computed]"
            f" — set by percentile thresholds in config/thesis.yaml. A partner's own"
            f" call always overrides this and is recorded."
          # A rank is only as strong as the cohort behind it. Promoting on a
          # 5-company bucket without saying so overstates the evidence.
          + (f"\n\n> Caveat: this rank comes from a cohort of only"
             f" {score['cohort_size']} comparable companies"
             + (" in the 'unclassified' catch-all bucket"
                if str(score["cohort_key"] or "").startswith("unclassified|") else "")
             + ". Treat it as a prompt to look, not as evidence of relative quality —"
               " a wider cohort (or licensed data) is what makes the ranking meaningful.\n"
             if score and score["cohort_low_confidence"] else "\n"))

    violations = validate_brief(md)
    if violations and judged:
        # regenerate judgment once with violations appended
        md2 = md  # observed sections are mechanically cited; violations imply judged text
        judged2 = judge.judge_company(company_id)
        if judged2:
            md2 = md.replace(_judgment_sections(company_id, judged),
                             _judgment_sections(company_id, judged2))
        violations2 = validate_brief(md2)
        if not violations2:
            md, violations = md2, []
        else:
            violations = violations2
    validated = 0 if violations else 1
    brief_id = db.insert("briefs", {
        "company_id": company_id, "content_md": md, "recommendation": rec,
        "generated_at": db.now_iso(), "trigger": trigger, "validated": validated,
        "validation_notes": json.dumps(violations) if violations else None})
    if violations:
        db.insert("review_queue", {"kind": "brief_validation", "confidence": None,
                                   "payload_json": json.dumps({"brief_id": brief_id,
                                                               "violations": violations}),
                                   "created_at": db.now_iso()})
        if verbose:
            print(f"  ! brief for {c['name']} FAILED citation validation ({len(violations)}"
                  " uncited numeric claims) — flagged for review, NOT published")
        return None
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    (BRIEFS_DIR / f"{_slug(c['name'])}.md").write_text(md)
    if verbose:
        print(f"  brief published: {c['name']} -> output/briefs/{_slug(c['name'])}.md")
    return brief_id


def _comparables(company_id: int) -> str:
    """Peers must come from the SAME cohort the percentile was computed in.
    Scoring buckets a company with no sector into 'unclassified', so bailing out
    on a null sector produced briefs that claimed a percentile in one breath and
    'no cohort assigned' in the next — a self-contradiction on the page."""
    c = db.q1("SELECT sector, stage FROM companies WHERE id=?", (company_id,))
    if not c:
        return "- No cohort assigned yet."
    if c["sector"]:
        rows = db.q("""SELECT name, market_rank FROM companies WHERE sector=? AND id!=?
                       AND is_synthetic=0 AND status IN ('hot','watchlist','pipeline')
                       ORDER BY market_rank LIMIT 5""", (c["sector"], company_id))
    else:
        rows = db.q("""SELECT name, market_rank FROM companies WHERE sector IS NULL AND id!=?
                       AND is_synthetic=0 AND status IN ('hot','watchlist','pipeline')
                       ORDER BY market_rank LIMIT 5""", (company_id,))
    if not rows:
        return "- No comparables in pipeline yet for this cohort."
    if not c["sector"]:
        return ("- Cohort is the 'unclassified' catch-all (sector not determined from the "
                "available text), so these are weak comparables:\n"
                + "\n".join(f"- {r['name']} (market rank {r['market_rank']} in cohort) [computed]"
                            for r in rows))
    return "\n".join(f"- {r['name']} (market rank {r['market_rank']} in cohort) [computed]"
                     for r in rows)


def auto_briefs(judged_results: dict[int, dict], verbose: bool = True) -> int:
    """Auto-trigger briefs above the configured percentile threshold."""
    thr = thesis()["scoring"]["brief_auto_threshold_percentile"]
    rows = db.q("""SELECT c.id FROM companies c JOIN scores s ON s.company_id=c.id
                   WHERE s.id=(SELECT id FROM scores WHERE company_id=c.id
                               ORDER BY scored_at DESC, id DESC LIMIT 1)
                   AND s.percentile >= ? AND c.is_synthetic=0
                   AND c.status IN ('hot','watchlist','pipeline')
                   ORDER BY s.percentile DESC""", (thr,))
    n = 0
    for r in rows:
        if generate_brief(r["id"], "auto_threshold", (judged_results or {}).get(r["id"]),
                          verbose=verbose):
            n += 1
    return n
