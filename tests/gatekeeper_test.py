"""Gatekeeper tests — the two questions that matter about a filter.

Does it catch what it claims to catch (recall), and does it leave true statements
alone (precision)? A gatekeeper that fails the second is worse than none: people
stop trusting the marker, then stop reading the section, then turn it off.

Both halves run against a throwaway DB seeded with a company whose evidence is
known exactly, so every expectation here is checkable by hand.

    python tests/gatekeeper_test.py
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp()) / "gk.db"
os.environ["DEAL_ENGINE_DB"] = str(TMP)
os.environ.pop("DATABASE_URL", None)

from engine import db  # noqa: E402
from engine import gatekeeper as gk  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def seed() -> tuple[int, int, int]:
    """One company with fully known evidence, plus a second company whose signal
    exists — so 'cited a real id belonging to someone else' can be tested, which
    is the failure a shape-only validator can never see."""
    db.connect()
    src = db.insert("sources", {"name": "test_rss", "adapter": "rss_news",
                                "interval_minutes": 60, "requires_license": 0,
                                "health": "ok"})
    cid = db.insert("companies", {
        "name": "Helion Substrate", "domain": "helionsubstrate.com", "sector": "robotics",
        "sub_sector": "warehouse robotics", "stage": "seed", "hq": "Boston, MA",
        "description": "Autonomous picking arms for cold-chain warehouses.",
        "status": "hot", "is_synthetic": 0, "created_at": db.now_iso(),
        "last_signal_at": db.now_iso()})
    other = db.insert("companies", {
        "name": "Unrelated Corp", "status": "pipeline", "is_synthetic": 0,
        "created_at": db.now_iso(), "last_signal_at": db.now_iso()})

    sid = db.insert("signals", {
        "source_id": src, "kind": "funding_event", "observed_at": db.now_iso(),
        "fetched_at": db.now_iso(), "fetch_mode": "live",
        "url": "https://example.com/helion-seed",
        "dedupe_key": "gk:1", "company_id": cid,
        "payload_json": '{"amount_usd": 12500000, "stage": "seed", '
                        '"lead": "Eclipse Ventures", "title": "Helion Substrate raises $12.5M"}',
        "raw": "Helion Substrate raises $12.5M seed led by Eclipse Ventures."})
    other_sid = db.insert("signals", {
        "source_id": src, "kind": "news", "observed_at": db.now_iso(),
        "fetched_at": db.now_iso(), "fetch_mode": "live", "url": "https://example.com/other",
        "dedupe_key": "gk:2", "company_id": other, "payload_json": "{}",
        "raw": "Unrelated Corp does something."})

    inv = db.insert("investors", {"name": "Eclipse Ventures", "tier": 1})
    db.insert("investments", {"company_id": cid, "investor_id": inv,
                              "source_signal_id": sid, "announced_at": db.now_iso()})
    db.insert("funding_rounds", {"company_id": cid, "stage": "seed", "amount_usd": 12500000,
                                 "announced_at": db.now_iso(), "source_signal_id": sid,
                                 "lead_investor_id": inv})
    db.insert("founders", {"company_id": cid, "name": "Priya Raman", "prior_exits": 1,
                           "frontier_lab_alum": 0})
    return cid, sid, other_sid


def main() -> None:
    cid, sid, other_sid = seed()
    ev = gk.build_evidence(cid)

    # --- evidence is actually assembled ------------------------------------
    check("evidence collects this company's signal ids", ev.signal_ids == {sid},
          f"{ev.signal_ids}")
    check("evidence knows the stored amount at both scales",
          ev.has_number(12_500_000) and ev.has_number(12.5), "")
    check("evidence knows the named investor and founder",
          ev.mentions("Eclipse Ventures") and ev.mentions("Priya Raman"), "")

    # --- RECALL: the four things models actually fabricate ------------------
    cases = [
        ("invented signal id",
         "The team shipped a pilot with three customers [S:99999].", "does not exist"),
        ("real signal id belonging to another company",
         f"Revenue grew sharply last quarter [S:{other_sid}].", "different company"),
        ("invented backer",
         "Helion Substrate is backed by Sequoia and Benchmark.", "Sequoia"),
        ("invented figure",
         "The company raised $50M at a $400M valuation.", "matches no stored value"),
    ]
    for label, text, needle in cases:
        reasons = gk.check_sentence(text, ev)
        check(f"catches: {label}", any(needle in r for r in reasons),
              "; ".join(reasons)[:90] or "NOTHING FLAGGED")

    # --- PRECISION: true statements must survive untouched ------------------
    true_text = (f"Helion Substrate raised $12.5M in a seed round led by Eclipse Ventures "
                 f"[S:{sid}]. Founder Priya Raman has one prior exit. "
                 "Warehouse robotics is a plausible fit for the fund's automation theme.")
    clean, removed = gk.verify_text(true_text, ev)
    check("leaves true, sourced statements alone", not removed,
          "; ".join(r["reasons"][0] for r in removed)[:120])
    check("true text is returned byte-identical", clean == true_text, "")

    opinion = ("Founder quality: 8/10 — domain fit is strong. Exit horizon: 6 years. "
               "Moat is thin today but the data flywheel could deepen it.")
    _, removed_op = gk.verify_text(opinion, ev)
    check("labelled opinion (n/10, horizons) is not treated as a factual claim",
          not removed_op, "; ".join(r["reasons"][0] for r in removed_op)[:120])

    honest_null = ("No founder information was found in the provided signals, so founder "
                   "quality is null. There is no evidence of revenue.")
    _, removed_null = gk.verify_text(honest_null, ev)
    check("honest statements of absence survive", not removed_null,
          "; ".join(r["reasons"][0] for r in removed_null)[:120])

    # --- ENFORCEMENT: drop the sentence, keep the brief ---------------------
    mixed = (f"Helion Substrate raised $12.5M led by Eclipse Ventures [S:{sid}]. "
             "It is also backed by Sequoia [S:99999]. "
             "The warehouse robotics market is consolidating.")
    clean, removed = gk.verify_text(mixed, ev)
    check("only the offending sentence is dropped",
          "Eclipse Ventures" in clean and "consolidating" in clean
          and "Sequoia" not in clean, clean[:100])
    check("removal is announced, not silent", gk.REMOVED_MARKER in clean, "")
    check("removal reasons are recorded for audit",
          len(removed) == 1 and len(removed[0]["reasons"]) >= 2,
          str(removed[0]["reasons"])[:100] if removed else "none")

    # --- judgement dict scrub ----------------------------------------------
    judged = {"founder_quality": 8, "moat": 6,
              "founder_reasoning": "Priya Raman previously exited a company.",
              "moat_reasoning": "Defensibility comes from a $900M proprietary dataset.",
              "thesis_narrative": f"A $12.5M seed led by Eclipse Ventures [S:{sid}] puts "
                                  "them ahead of peers.",
              "tam": {"value_usd": 4.0e10, "confidence": "low",
                      "assumptions": ["12,000 cold-chain warehouses globally"]}}
    out, removed_j = gk.verify_judgement(judged, cid)
    check("judgement scrub keeps a grounded narrative",
          gk.REMOVED_MARKER not in out["thesis_narrative"], out["thesis_narrative"][:80])
    check("judgement scrub removes an invented figure in reasoning",
          gk.REMOVED_MARKER in out["moat_reasoning"], out["moat_reasoning"][:80])
    # A rating whose entire justification was fabricated must fall with it. Keeping
    # "6/10" beside a removal marker would be the same confident emptiness the whole
    # module exists to stop — while the rating whose reasoning SURVIVED stands.
    check("a rating whose whole justification was removed is nulled too",
          out["moat"] is None, f"moat={out['moat']}")
    check("a rating with surviving reasoning is left alone",
          out["founder_quality"] == 8, f"founder_quality={out['founder_quality']}")
    check("judgement removals name their field",
          any(r["field"] == "moat_reasoning" for r in removed_j), "")

    # --- citation audit over a whole document ------------------------------
    md = f"# Helion\n- Raised $12.5M [S:{sid}]\n- Something [S:{other_sid}] and [S:4242]\n"
    bad = gk.audit_citations(md, cid, ev)
    check("document audit resolves every citation", len(bad) == 2, "; ".join(bad))

    # --- audit trail --------------------------------------------------------
    gk.record(cid, "test", removed_j, ref="unit")
    row = db.q1("SELECT surface, removed_count FROM gatekeeper_events"
                " ORDER BY id DESC LIMIT 1")
    check("removals persist to gatekeeper_events",
          bool(row) and row["surface"] == "test" and row["removed_count"] == len(removed_j),
          str(dict(row)) if row else "no row")
    check("stats() reports what was stopped",
          gk.stats().get("available") and gk.stats().get("total_removed", 0) > 0, "")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"GATEKEEPER: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
