"""Phase 1 tests — discovery completeness, without touching the network.

Each new source is tested at its parsing seam with realistic payloads, because
that is where each one can silently go wrong: an index line mis-parsed is a
filing that never existed, a generic name watched is someone else's news on a
tracked company, a fuzzy registry match is a stranger's board on a pipeline record.

    python tests/phase1_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    # ---- 1a. EDGAR daily-index sweep --------------------------------------
    from engine.adapters.edgar_formd import EdgarFormDAdapter as E
    idx = """Form Type   Company Name                  CIK      Date Filed  File Name
--------------------------------------------------------------------------
10-K        Example Corp                  123456   20260813    edgar/data/123456/0001234-26-000001.txt
D           Helion Substrate Inc          1809726  20260813    edgar/data/1809726/0001809726-26-000004.txt
D/A         Quiet Robotics LLC            2044001  20260813    edgar/data/2044001/0002044001-26-000002.txt
D           Growth Fund Partners LP       2050000  20260813    edgar/data/2050000/0002050000-26-000001.txt
S-1         Someone Public Inc            999999   20260813    edgar/data/999999/0000999999-26-000009.txt
"""
    rows = E.parse_form_index(idx, "2026-08-13")
    check("index sweep keeps every Form D and D/A, nothing else",
          [r["form"] for r in rows] == ["D", "D/A", "D"], str([r["form"] for r in rows]))
    check("index rows carry cik, accession and issuer name",
          rows[0]["ciks"] == [1809726]
          and rows[0]["adsh"] == "0001809726-26-000004"
          and rows[0]["display_names"] == ["Helion Substrate Inc"], str(rows[0]))
    check("index rows are shaped like FTS hits (one downstream path)",
          all(set(r) >= {"adsh", "ciks", "file_date", "form", "display_names"}
              for r in rows), "")
    check("a malformed index parses to empty, never raises",
          E.parse_form_index("garbage\nno columns here", "2026-08-13") == [], "")

    # ---- 1b. company news watch -------------------------------------------
    from engine.adapters.company_news import CompanyNewsAdapter as N
    q = N.query_for("Helion Substrate, Inc.")
    check("news watch quotes the name and adds funding context",
          q is not None and '"Helion Substrate"' in q and "funding" in q, str(q))
    check("legal suffixes are stripped before quoting",
          "Inc" not in (q or ""), str(q))
    generics = [N.query_for(n) for n in ("Text", "Built", "Core", "Ab")]
    check("generic and too-short names are refused, not watched",
          all(g is None for g in generics),
          "wrong attribution on a tracked company is worse than a missed article")

    # ---- 1c. Companies House ----------------------------------------------
    from engine.adapters.companies_house import CompaniesHouseAdapter as C
    items = [
        {"title": "HELION SUBSTRATE LIMITED", "company_number": "12345678",
         "company_status": "active"},
        {"title": "HELION SUBSTRATE HOLDINGS LIMITED", "company_number": "99999999",
         "company_status": "active"},
    ]
    m = C.best_match("Helion Substrate Ltd", items)
    check("registry match is exact-normalised only",
          m is not None and m["company_number"] == "12345678", str(m))
    check("a near-miss registry name is refused",
          C.best_match("Helion Substrates", items) is None,
          "fuzzy matching a 5M-entity registry puts a stranger's board on a company")
    person = C.officer_to_person({"name": "RAMAN, Priya", "officer_role": "director",
                                  "occupation": "Engineer", "appointed_on": "2024-02-01"})
    check("registry 'SURNAME, Forename' becomes a person people.py can read",
          person == {"name": "Priya Raman", "titles": ["director", "Engineer"],
                     "appointed_on": "2024-02-01"}, str(person))
    check("a resigned/blank officer is dropped",
          C.officer_to_person({"name": ""}) is None, "")
    check("UK scoping uses country and HQ evidence",
          C.looks_uk("GB", None) and C.looks_uk(None, "London, UK")
          and not C.looks_uk("US", "San Francisco, CA"), "")

    # ---- wiring ------------------------------------------------------------
    from engine.config import sources_config
    names = {s["name"] for s in sources_config()["sources"]}
    check("both new sources are registered", {"company_news", "companies_house"} <= names,
          "")
    feeds = next(s for s in sources_config()["sources"] if s["name"] == "rss_news")["feeds"]
    wire_names = " ".join(f.get("name", "") for f in feeds)
    check("press-release wires are in the news feeds",
          "PRNewswire" in wire_names and "Business Wire" in wire_names, "")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"PHASE 1: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
