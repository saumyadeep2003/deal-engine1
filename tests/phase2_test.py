"""Phase 2 tests — the free-source package, tested at its parsing seams.

Same philosophy as phase1: each source is tested where it can silently go
wrong. A mis-parsed batch file is a YC company that never existed; a fuzzy
assignee match is a stranger's patent in a moat argument; "Scale" matching
inside "scaling" is someone else's podcast quote on a tracked company.

    python tests/phase2_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp()) / "p2.db"
os.environ["DEAL_ENGINE_DB"] = str(TMP)
os.environ.pop("DATABASE_URL", None)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    # ---- 2a. YC companies --------------------------------------------------
    from engine.adapters.yc_companies import domain_of, parse_batch, parse_meta_batches
    meta = json.dumps({"batches": [
        {"name": "Winter 2027", "slug": "winter-2027"},
        {"name": "Fall 2026", "slug": "fall-2026"},
        {"name": "Spring 2026", "slug": "spring-2026"},
        {"name": "Winter 2016", "slug": "winter-2016"}]})
    slugs = parse_meta_batches(meta, 3)
    check("YC: newest batches win, alumni batches don't",
          slugs == ["winter-2027", "fall-2026", "spring-2026"], str(slugs))
    # the LIVE meta.json keys batches BY slug (a dict, not a list) — the first
    # deployed run returned 0 companies in 0.3s because the parser iterated the
    # keys as if they were entries. Both shapes must parse.
    meta_dict = json.dumps({"batches": {
        "winter-2012": {"name": "Winter 2012", "count": 66},
        "spring-2026": {"name": "Spring 2026", "count": 196},
        "fall-2026": {"name": "Fall 2026", "count": 14},
        "winter-2027": {"name": "Winter 2027", "count": 1}}})
    check("YC: THE LIVE SHAPE — dict-keyed batches parse newest-first",
          parse_meta_batches(meta_dict, 3) == ["winter-2027", "fall-2026", "spring-2026"],
          str(parse_meta_batches(meta_dict, 3)))
    check("YC: malformed meta parses to empty, never raises",
          parse_meta_batches("not json", 3) == []
          and parse_meta_batches('{"batches": "wrong"}', 3) == [], "")
    batch = json.dumps([{
        "id": 29155, "name": "jo", "slug": "jo", "website": "https://askjo.ai",
        "one_liner": "The personal AI agent.", "long_description": "jo is...",
        "batch": "Spring 2026", "status": "Active", "team_size": 2,
        "tags": ["AI Assistant"], "industries": ["Consumer"],
        "all_locations": "San Francisco, CA", "launched_at": 1734654585,
        "url": "https://www.ycombinator.com/companies/jo"},
        {"no_name": True}])
    rows = parse_batch(batch)
    check("YC: a company record carries name, website, one-liner and batch",
          len(rows) == 1 and rows[0]["name"] == "jo"
          and rows[0]["website"] == "https://askjo.ai"
          and rows[0]["batch"] == "Spring 2026", str(rows[:1])[:80])
    check("YC: the website hands the domain resolver its answer for free",
          domain_of("https://www.askjo.ai/about") == "askjo.ai"
          and domain_of(None) is None, "")

    # ---- 2b. GDELT ---------------------------------------------------------
    from engine.adapters.gdelt_news import parse_articles, parse_seendate
    body = json.dumps({"articles": [
        {"url": "https://example.com/a", "title": "Helion Substrate raises $12M",
         "domain": "example.com", "seendate": "20260815T093000Z",
         "language": "English", "sourcecountry": "Germany"},
        {"title": "no url — dropped"}, "not-a-dict"]})
    arts = parse_articles(body)
    check("GDELT: articles normalise; rows without url/title are dropped",
          len(arts) == 1 and arts[0]["seendate"].startswith("2026-08-15T09:30:00"),
          str(arts[:1])[:80])
    check("GDELT: an HTML error page (status 200) reads as nothing observed",
          parse_articles("<html>rate limited</html>") == [], "")
    check("GDELT: an unparseable seendate degrades to now, never raises",
          parse_seendate("garbage") > "2026", "")
    from engine.adapters.company_news import CompanyNewsAdapter as N
    check("GDELT shares the news watch's generic-name refusal (same bar, same code)",
          N.query_for("Text") is None and N.query_for("Helion Substrate, Inc.") is not None,
          "a wider net makes wrong attribution MORE dangerous")

    # ---- 2c. PatentsView ---------------------------------------------------
    from engine.adapters.patents import build_query, norm_org, parse_patents
    check("patents: org normalisation strips legal suffixes and punctuation",
          norm_org("Helion Substrate, Inc.") == norm_org("Helion Substrate")
          and norm_org("Apex Robotics Corp") == "apexrobotics", norm_org("Apex Robotics Corp"))
    check("patents: a too-short name refuses to query at all",
          build_query("Ab") is None and build_query("Helion Substrate Inc") is not None, "")
    resp = json.dumps({"patents": [
        {"patent_id": "12345678", "patent_title": "Cold-chain robotic gripper",
         "patent_date": "2026-03-04", "patent_abstract": "A gripper...",
         "assignees": [{"assignee_organization": "Helion Substrate, Inc."}],
         "inventors": [{"inventor_name_first": "Priya", "inventor_name_last": "Raman"}]},
        {"patent_id": "99999999", "patent_title": "Someone else's patent",
         "assignees": [{"assignee_organization": "Helion Substrate Holdings LLC II"}],
         "inventors": []}]})
    pats = parse_patents(resp, "Helion Substrate Inc")
    check("THE DISCIPLINE: exact-normalised assignee match only — similar is rejected",
          len(pats) == 1 and pats[0]["patent_id"] == "12345678",
          "a stranger's patent in a moat argument is unrecoverable")
    check("patents: inventors ride along as inventors",
          pats[0]["inventors"] == ["Priya Raman"], str(pats[0]["inventors"]))
    check("patents: garbage response parses to empty, never raises",
          parse_patents("<html>502</html>", "X Co") == [], "")

    # ---- 2d. podcasts ------------------------------------------------------
    from engine import db
    from engine.adapters.podcasts import mention_snippet, parse_feed, tracked_names
    db.connect()
    db.insert("sources", {"name": "t", "adapter": "x", "interval_minutes": 60,
                          "requires_license": 0, "health": "ok"})
    for name, status in [("Helion Substrate Inc", "hot"), ("Scale", "hot"),
                         ("Text", "watchlist")]:
        db.insert("companies", {"name": name, "status": status, "is_synthetic": 0,
                                "created_at": db.now_iso(),
                                "last_signal_at": db.now_iso()})
    watch = tracked_names()
    names = {w["name"] for w in watch}
    check("podcasts: generic tracked names are refused, exactly like the news watch",
          "Helion Substrate Inc" in names and "Text" not in names and "Scale" not in names,
          str(sorted(names)))
    helion = next(w for w in watch if w["name"] == "Helion Substrate Inc")
    check("podcasts: word-boundary matching — no substring false positives",
          mention_snippet("we discussed Helion Substrate's robots", helion["pattern"])
          is not None
          and mention_snippet("helionsubstrateish nonsense", helion["pattern"]) is None,
          "'Scale' inside 'scaling' is the failure this prevents")
    rss = """<?xml version="1.0"?><rss><channel><title>Test VC Pod</title>
      <item><title>Ep 12: Warehouse robots with Helion Substrate</title>
        <description>&lt;p&gt;We talk to the founders of Helion Substrate about cold chains.&lt;/p&gt;</description>
        <pubDate>Fri, 15 Aug 2026 08:00:00 GMT</pubDate>
        <link>https://pod.example/ep12</link>
        <enclosure url="https://cdn.example/ep12.mp3" type="audio/mpeg"/>
      </item></channel></rss>"""
    eps = parse_feed(rss, "Test VC Pod")
    check("podcasts: RSS parses to title/notes/date/audio",
          len(eps) == 1 and eps[0]["audio_url"] == "https://cdn.example/ep12.mp3"
          and eps[0]["published"].startswith("2026-08-15"), str(eps[:1])[:90])

    # ---- 2d-bis. profiles fall back to the company's own launch words ------
    from engine.adapters.yc_companies import YcCompaniesAdapter  # noqa: F401
    from engine import profile as profile_mod
    launch_co = db.insert("companies", {"name": "Remara Robotics", "status": "hot",
                                        "is_synthetic": 0, "created_at": db.now_iso(),
                                        "last_signal_at": db.now_iso()})
    db.insert("signals", {
        "source_id": 1, "kind": "launch", "observed_at": db.now_iso(),
        "fetched_at": db.now_iso(), "fetch_mode": "live",
        "url": "https://www.ycombinator.com/companies/remara",
        "dedupe_key": "yc:remara", "company_id": launch_co,
        "payload_json": json.dumps({
            "title": "Remara Robotics (YC Spring 2026)",
            "summary": "Autonomous cold-chain picking arms for regional grocery "
                       "distributors — retrofit into existing warehouses in a weekend."}),
        "raw": "Remara Robotics — autonomous picking arms"})
    src_txt = profile_mod.source_text(launch_co)
    check("THE FIX: a company with no readable site gets its launch-listing words",
          src_txt is not None and "cold-chain" in src_txt[0]
          and "ycombinator.com" in src_txt[1],
          "the founders wrote that about themselves — weaker than a site, better "
          "than the blank 54 of 61 briefs showed")
    no_evidence = db.insert("companies", {"name": "Silent Co", "status": "hot",
                                          "is_synthetic": 0, "created_at": db.now_iso(),
                                          "last_signal_at": db.now_iso()})
    check("...and a company with neither site nor launch stays an honest None",
          profile_mod.source_text(no_evidence) is None, "no invented descriptions")

    # ---- 2e. ATS: three new providers --------------------------------------
    from engine.adapters.ats_boards import PROVIDERS, parse_board
    check("ATS: six providers registered",
          set(PROVIDERS) >= {"greenhouse", "lever", "ashby", "smartrecruiters",
                             "workable", "recruitee"}, str(sorted(PROVIDERS)))
    sr = json.dumps({"content": [{"name": "ML Engineer", "releasedDate": "2026-08-01",
                                  "location": {"city": "Berlin", "country": "de"},
                                  "actions": {"applyOnWeb": "https://sr.example/1"}}]})
    wk = json.dumps({"jobs": [{"title": "Account Executive", "city": "NYC",
                               "country": "US", "published_on": "2026-08-02",
                               "url": "https://wk.example/2"}]})
    rc = json.dumps({"offers": [{"title": "Robotics Engineer", "location": "Remote",
                                 "published_at": "2026-08-03",
                                 "careers_url": "https://rc.example/3"}]})
    check("ATS: SmartRecruiters postings normalise to the shared row shape",
          parse_board("smartrecruiters", sr)[0]["title"] == "ML Engineer", "")
    check("ATS: Workable postings normalise",
          parse_board("workable", wk)[0]["title"] == "Account Executive", "")
    check("ATS: Recruitee postings normalise",
          parse_board("recruitee", rc)[0]["title"] == "Robotics Engineer", "")
    check("ATS: a provider payload in the wrong shape yields [], never raises",
          parse_board("smartrecruiters", '{"unexpected": 1}') == []
          and parse_board("workable", "not json") == [], "")

    # ---- wiring ------------------------------------------------------------
    from engine.config import sources_config
    names = {s["name"] for s in sources_config()["sources"]}
    check("all four new sources are registered",
          {"yc_companies", "gdelt_news", "patents", "podcast_notes"} <= names, "")
    free = {s["name"] for s in sources_config()["sources"] if not s.get("requires_license")}
    check("all four are free (the licensed stubs are untouched)",
          {"yc_companies", "gdelt_news", "patents", "podcast_notes"} <= free, "")
    pod = next(s for s in sources_config()["sources"] if s["name"] == "podcast_notes")
    check("podcast feeds are configured (adapter has shows to read)",
          len(pod.get("feeds") or []) >= 3, f"{len(pod.get('feeds') or [])} feeds")
    from engine import runner
    keys = [k for k, _ in runner.build_steps("full")]
    check("all four collect in the discovery group, before the filter",
          all(keys.index(f"collect:{n}") < keys.index("filter")
              for n in ("yc_companies", "gdelt_news", "patents", "podcast_notes")), "")
    from engine import version
    f = version.features()
    check("/api/version proves the package is in the running build",
          f.get("yc_companies") and f.get("gdelt_news") and f.get("patents_source")
          and f.get("podcast_notes") and f.get("ats_six_providers"), "")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"PHASE 2: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
