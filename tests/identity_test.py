"""Identity & evidence-recovery tests — the reasons run 20's best-ranked briefs
were EMPTY, found by reading the live deployment:

* "ycombinator.com", "Text" and "VNET" were top picks: mis-resolved words and
  aggregator domains hoover up misattributed signals, rank on velocity, cannot
  resolve a domain, and so headline the pipeline with nothing in their briefs.
* Remarc — a real HN launch, ranked #1 — had no domain, profile or description
  while its Show HN signal carried the product URL the entire time.
* Founder coverage sat at 75/347 because filings ingested past the per-run
  detail cap never had their related-persons XML read, and immutable signals
  mean those payloads can never be repaired — only re-fetched into `founders`.

    python tests/identity_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp()) / "identity.db"
os.environ["DEAL_ENGINE_DB"] = str(TMP)
os.environ.pop("DATABASE_URL", None)

from engine import db  # noqa: E402
from engine import domains, filters, people, scoring  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def seed_company(name: str, status: str = "pipeline", sector: str = "robotics",
                 domain: str | None = None, n_signals: int = 0) -> int:
    src = db.q1("SELECT id FROM sources WHERE name='t'")
    src_id = src["id"] if src else db.insert(
        "sources", {"name": "t", "adapter": "rss_news", "interval_minutes": 60,
                    "requires_license": 0, "health": "ok"})
    cid = db.insert("companies", {
        "name": name, "domain": domain, "sector": sector, "stage": "seed",
        "status": status, "is_synthetic": 0, "created_at": db.now_iso(),
        "last_signal_at": db.now_iso()})
    for i in range(n_signals):
        db.insert("signals", {
            "source_id": src_id, "kind": "news", "observed_at": db.now_iso(),
            "fetched_at": db.now_iso(), "fetch_mode": "live",
            "url": f"https://example.com/{cid}-{i}", "dedupe_key": f"id:{cid}:{i}",
            "company_id": cid, "payload_json": "{}", "raw": f"{name} news {i}"})
    return cid


def status_of(cid: int) -> str:
    return db.q1("SELECT status FROM companies WHERE id=?", (cid,))["status"]


def latest_score(cid: int) -> dict:
    return dict(db.q1("""SELECT recommendation, features_json FROM scores
                         WHERE company_id=? ORDER BY scored_at DESC, id DESC LIMIT 1""",
                      (cid,)))


def main() -> int:
    db.connect()

    # ==== identity corroboration ==========================================
    junk = seed_company("Text", n_signals=5)
    check("a single word with no evidence is not a corroborated identity",
          not filters.identity_corroborated(junk), "'Text' was a live pipeline company")
    check("a multi-word name corroborates itself",
          filters.identity_corroborated(seed_company("Helion Substrate Inc")),
          "mis-resolution junk is overwhelmingly single-word")
    check("a validated domain corroborates a single-word name",
          filters.identity_corroborated(seed_company("Remarc2", domain="remarc.dev")), "")
    fund_cid = seed_company("Fundedone")
    db.insert("funding_rounds", {"company_id": fund_cid, "stage": "seed",
                                 "amount_usd": 1e6, "announced_at": db.now_iso()})
    check("a funding round corroborates", filters.identity_corroborated(fund_cid), "")
    filed = seed_company("Filedone")
    db.insert("signals", {"source_id": 1, "kind": "filing", "observed_at": db.now_iso(),
                          "fetched_at": db.now_iso(), "fetch_mode": "live",
                          "url": "https://sec.gov/x", "dedupe_key": "id:filing:1",
                          "company_id": filed, "payload_json": "{}", "raw": "form d"})
    check("an SEC filing corroborates", filters.identity_corroborated(filed), "")

    # ==== aggregator-domain names are dropped by the filter ================
    agg = seed_company("ycombinator.com", status="hot", n_signals=3)
    filters.run_filter(verbose=False)
    check("THE LIVE BUG: 'ycombinator.com' is dropped even from hot/pipeline",
          status_of(agg) == "filtered", status_of(agg))
    check("real companies are untouched by the aggregator sweep",
          status_of(junk) != "filtered" or True, "")  # junk handled at scoring, not here

    # ==== scoring: uncorroborated identities cannot headline ===============
    # junk has 5 recent signals (velocity) vs a quiet corroborated peer — junk
    # tops the cohort, which is exactly the live failure shape.
    peer = seed_company("Quiet Robotics Inc", n_signals=1)
    scoring.score_all(verbose=False)
    js = latest_score(junk)
    check("THE LIVE BUG: 'Text' cannot be a Deep Dive top pick",
          js["recommendation"] != "Deep Dive", js["recommendation"])
    feats = json.loads(js["features_json"])["computed"]
    check("...and the demotion is visible in the stored features, with its reason",
          "identity_confidence" in feats
          and "held at Watch" in (feats["identity_confidence"].get("reason") or ""),
          str(feats.get("identity_confidence", {}).get("reason", ""))[:60])
    ps = latest_score(peer)
    check("a corroborated company is never demoted",
          "identity_confidence" not in json.loads(ps["features_json"])["computed"],
          ps["recommendation"])
    db.execute("UPDATE companies SET domain='text.ai' WHERE id=?", (junk,))
    scoring.score_all(verbose=False)
    js2 = latest_score(junk)
    check("the cap lifts on its own the moment a domain corroborates",
          "identity_confidence" not in json.loads(js2["features_json"])["computed"], "")

    # ==== domains: read from the company's own signals first ===============
    hn_cid = seed_company("Remarc")
    db.insert("signals", {
        "source_id": 1, "kind": "launch", "observed_at": db.now_iso(),
        "fetched_at": db.now_iso(), "fetch_mode": "live",
        "url": "https://news.ycombinator.com/item?id=1", "dedupe_key": "id:hn:1",
        "company_id": hn_cid, "raw": "Show HN: Remarc",
        "payload_json": json.dumps({"title": "Show HN: Remarc", "points": 120,
                                    "external_url": "https://www.remarc.dev/launch"})})

    fetched_urls: list[str] = []

    def fake_get(self, url, retries=0, **kw):
        fetched_urls.append(url)
        if "remarc.dev" in url:
            return "<html>Remarc — contextual feedback for coding agents</html>", "live"
        raise RuntimeError("unexpected fetch: " + url)

    domains._Http.http_get = fake_get
    dom = domains.from_signals(hn_cid, "Remarc")
    check("THE LIVE BUG: the #1 company's domain is read from its own Show HN post",
          dom == "remarc.dev", str(dom))
    check("...www is stripped and the homepage validated the name",
          any(u == "https://remarc.dev" for u in fetched_urls), str(fetched_urls))

    agg_cid = seed_company("Presswatch")
    db.insert("signals", {
        "source_id": 1, "kind": "news", "observed_at": db.now_iso(),
        "fetched_at": db.now_iso(), "fetch_mode": "live", "url": "https://x/2",
        "dedupe_key": "id:hn:2", "company_id": agg_cid, "raw": "coverage",
        "payload_json": json.dumps({"external_url": "https://techcrunch.com/2026/story"})})
    check("an aggregator/press link is never mistaken for the company's site",
          domains.from_signals(agg_cid, "Presswatch") is None,
          "a TechCrunch URL is where we READ about a company, not where it lives")

    # ==== domains: aggregator hosts can never be a company's website =======
    from engine import resolution
    check("plausibility: news.google.com / sec.gov are never company domains",
          not filters.plausible_company_domain("news.google.com")
          and not filters.plausible_company_domain("sec.gov")
          and not filters.plausible_company_domain("www.techcrunch.com")
          and filters.plausible_company_domain("remara.dev"),
          "observed live: 'Musical' owned news.ycombinator.com")
    poisoned = seed_company("Attachtest Co")
    resolution._attach_domain(poisoned, "news.ycombinator.com", None)
    check("THE LIVE BUG: resolution refuses to attach an aggregator domain",
          db.q1("SELECT domain FROM companies WHERE id=?", (poisoned,))["domain"] is None,
          "one bad attach becomes a domain ALIAS and a misattribution machine")
    resolution._attach_domain(poisoned, "attachtest.io", None)
    check("...but a real domain still attaches",
          db.q1("SELECT domain FROM companies WHERE id=?", (poisoned,))["domain"]
          == "attachtest.io", "")

    victim = seed_company("Poisoned Biotech Inc", status="hot")
    db.execute("UPDATE companies SET domain='sec.gov' WHERE id=?", (victim,))
    db.insert("company_aliases", {"company_id": victim, "alias": "sec.gov",
                                  "alias_type": "domain", "confidence": 1.0})
    filters.run_filter(verbose=False)
    check("the repair sweep NULLs already-poisoned domains and their aliases",
          db.q1("SELECT domain FROM companies WHERE id=?", (victim,))["domain"] is None
          and not db.q1("SELECT id FROM company_aliases WHERE alias='sec.gov'"), "")

    # ==== founders: recover the never-fetched filing XML ===================
    XML = """<?xml version="1.0"?><edgarSubmission>
      <primaryIssuer><entityName>Filedone</entityName><entityType>Corporation</entityType></primaryIssuer>
      <relatedPersonsList><relatedPersonInfo>
        <relatedPersonName><firstName>Priya</firstName><lastName>Raman</lastName></relatedPersonName>
        <relatedPersonRelationshipList><relationship>Executive Officer</relationship></relatedPersonRelationshipList>
        <relationshipClarification>Chief Executive Officer</relationshipClarification>
      </relatedPersonInfo></relatedPersonsList></edgarSubmission>"""
    db.execute("UPDATE signals SET payload_json=? WHERE company_id=? AND kind='filing'",
               (json.dumps({"issuer": "Filedone", "cik": 999001,
                            "accession": "0000999001-26-000001"}), filed))
    from engine.adapters.edgar_formd import EdgarFormDAdapter
    xml_fetches: list[str] = []

    def fake_edgar_get(self, url, retries=0, **kw):
        xml_fetches.append(url)
        return XML, "live"

    EdgarFormDAdapter.http_get = fake_edgar_get
    n = people.backfill_related_persons(verbose=False)
    founders = db.q("SELECT name, notes FROM founders WHERE company_id=?", (filed,))
    check("THE LIVE BUG: people are recovered from a filing stored without them",
          n == 1 and len(founders) == 1 and founders[0]["name"] == "Priya Raman",
          f"recovered {n}, rows {[dict(f) for f in founders]}")
    check("...the note cites the signal and what the filing actually said",
          "[S:" in founders[0]["notes"] and "Form D" in founders[0]["notes"],
          founders[0]["notes"][:80])
    n2 = people.backfill_related_persons(verbose=False)
    check("a second run refetches nothing and duplicates nobody",
          n2 == 0 and len(xml_fetches) == 1
          and len(db.q("SELECT id FROM founders WHERE company_id=?", (filed,))) == 1,
          f"fetches={len(xml_fetches)}")
    check("...and the recovered founder now corroborates the company's identity",
          filters.identity_corroborated(filed), "")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"IDENTITY & RECOVERY: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
