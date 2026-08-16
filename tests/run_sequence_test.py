"""Run-sequence tests — the order of the pipeline IS a correctness property.

Two orderings were wrong for multiple runs and every step still reported
success, which is what makes sequence bugs the quiet kind:

* careers/company_website collects ran BEFORE the domain resolver, so a website
  found this run was only read (and its profile only written) NEXT run — three
  consecutive runs said "N domains found, 0 profiles written".
* apollo_enrich ran BEFORE scoring, so it could only ever enrich the PREVIOUS
  run's Deep Dive list — observed and questioned by the user watching the panel.

    python tests/run_sequence_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DEAL_ENGINE_DB"] = str(Path(tempfile.mkdtemp()) / "seq.db")
os.environ.pop("DATABASE_URL", None)

from engine import runner  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    keys = [k for k, _ in runner.build_steps("full")]
    pos = {k: i for i, k in enumerate(keys)}

    def before(a: str, b: str) -> bool:
        return a in pos and b in pos and pos[a] < pos[b]

    check("THE BUG: domains resolves BEFORE the company's website is visited",
          before("domains", "collect:company_website")
          and before("domains", "collect:careers_pages"),
          "a website found this run is read this run")
    check("...and the website visit lands BEFORE profiles are written",
          before("collect:company_website", "profiles"),
          "so the profile is written from the page in the same run")
    check("THE BUG: apollo enriches AFTER scoring — this run's Deep Dive picks",
          before("score", "collect:apollo_enrich"),
          "before scoring, this run's picks do not exist yet")
    check("...and apollo lands BEFORE briefs render its headcount/growth",
          before("collect:apollo_enrich", "briefs"), "")

    # the orderings that were already right must stay right
    check("filter runs before people/domains/judge",
          before("filter", "people") and before("filter", "domains")
          and before("filter", "judge"), "")
    check("people (founder sync) runs before judge — evidence before assessment",
          before("people", "judge"), "")
    check("judge runs before score; score before briefs",
          before("judge", "score") and before("score", "briefs"), "")
    check("publish/alerts/snapshot close the run, in that order",
          before("publish", "alerts") and before("alerts", "snapshot")
          and keys[-1] == "snapshot", "")
    check("discovery sources (edgar, rss, hn) still open the run",
          keys[0].startswith("collect:") and before("collect:edgar_formd", "events"),
          keys[0])

    # every source still appears exactly once — regrouping must not drop or dupe
    collects = [k for k in keys if k.startswith("collect:")]
    free = {s["name"] for s in runner._free_sources()}
    check("every free source is collected exactly once",
          sorted(c.split(":", 1)[1] for c in collects) == sorted(free)
          and len(collects) == len(set(collects)),
          f"{len(collects)} collect steps for {len(free)} sources")

    # quick kind='quick' variant stays consistent too
    qkeys = [k for k, _ in runner.build_steps("quick")]
    check("the non-full variant keeps the same ordering rules",
          qkeys.index("domains") < qkeys.index("collect:company_website")
          < qkeys.index("profiles")
          and qkeys.index("score") < qkeys.index("collect:apollo_enrich"), "")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"RUN SEQUENCE: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
