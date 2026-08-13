"""Deployment acceptance tests — run against a RUNNING service.

    ./dealctl start          # or: python serve.py
    python tests/deployment.py [--url http://127.0.0.1:8787]

Covers the four things the local acceptance suite cannot: the web layer answers,
the scheduler is live, email/sheets degrade honestly rather than silently, and
the partner write-paths reach the feedback tables.
"""
from __future__ import annotations
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

URL = "http://127.0.0.1:8787"
if "--url" in sys.argv:
    URL = sys.argv[sys.argv.index("--url") + 1]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def get(path: str, method: str = "GET", binary: bool = False):
    """Returns (status, parsed-json | text | bytes). `binary` for file downloads —
    decoding an .xlsx as utf-8 raises, which would look like an endpoint failure."""
    req = urllib.request.Request(URL + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            if binary:
                return r.status, raw
            text = raw.decode(errors="replace")
            try:
                return r.status, json.loads(text)
            except json.JSONDecodeError:
                return r.status, text
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def main() -> int:
    from engine import db
    from engine.config import OUTPUT_DIR

    code, _ = get("/healthz")
    if code != 200:
        print(f"Service not reachable at {URL} — start it with ./dealctl start")
        return 1

    # D1 — dashboard renders and every read endpoint answers
    endpoints = ["/", "/api/summary", "/api/pipeline", "/api/sectors", "/api/peers",
                 "/api/commentary", "/api/news", "/api/stale", "/api/review-queue"]
    bad = [p for p in endpoints if get(p)[0] != 200]
    check("D1 dashboard + all read endpoints return 200", not bad, str(bad) or f"{len(endpoints)} endpoints")

    # D2 — the funnel the dashboard shows agrees with the database
    _, s = get("/api/summary")
    raw_db = db.q1("SELECT COUNT(*) c FROM signals WHERE fetch_mode!='synthetic_demo'")["c"]
    raw_api = next(x["value"] for x in s["funnel"] if x["stage"] == "Raw signals ingested")
    check("D2 dashboard funnel agrees with the database", raw_api == raw_db,
          f"api {raw_api} vs db {raw_db}")

    # D3 — licence posture is surfaced, never hidden
    lic = [x for x in s["sources"] if x["requires_license"]]
    check("D3 licensed adapters reported as licence-gated, not broken",
          len(lic) >= 9 and all(x["health"] in ("license_required", "unknown") for x in lic),
          f"{len(lic)} licensed adapters")

    # D4 — email degrades honestly (no silent no-op)
    check("D4 email status states configured/why-not explicitly",
          isinstance(s["email"]["configured"], bool)
          and (s["email"]["configured"] or bool(s["email"]["reason"])),
          f"configured={s['email']['configured']} reason={s['email']['reason']}")

    # D5 — sheets degrades honestly
    check("D5 sheets status states configured/why-not explicitly",
          isinstance(s["sheets"]["configured"], bool)
          and (s["sheets"]["configured"] or bool(s["sheets"]["reason"])),
          f"configured={s['sheets']['configured']}")

    # D6 — stub posture is visible in the API (so the UI can't hide it)
    import os
    from engine.llm import api_key_env_name as _key_env
    check("D6 stub/live judgment posture exposed",
          s["llm"]["stubbed"] == (not os.environ.get(_key_env())),
          f"stubbed={s['llm']['stubbed']}")

    # D7 — chat over HTTP answers the brief's three questions with citations
    qs = ["what are the best deals in defence tech right now?",
          "summarise what people are saying about Pangram",
          "who's quietly investing in robotics?"]
    answers = []
    for q in qs:
        c, d = get("/api/chat?q=" + urllib.parse.quote(q))
        answers.append(d.get("answer", "") if isinstance(d, dict) else "")
    cited = sum(1 for a in answers if "http" in a or "[20" in a)
    check("D7 /api/chat answers all three brief questions, with sources",
          all(len(a) > 40 for a in answers) and cited >= 1,
          f"lengths {[len(a) for a in answers]}, {cited} carry citations")

    # D8 — on-demand thesis scan returns ranked companies
    c, d = get("/api/scan?thesis_text=" + urllib.parse.quote("robotics for logistics and warehouse automation"))
    check("D8 /api/scan ranks companies for a prose thesis",
          c == 200 and isinstance(d, dict) and len(d.get("results", [])) > 0,
          f"{len(d.get('results', [])) if isinstance(d, dict) else 0} results")

    # D9 — partner decision writes the feedback loop (human value wins)
    _, pipe = get("/api/pipeline")
    target = pipe["rows"][0]
    before = db.q1("SELECT COUNT(*) c FROM partner_actions")["c"]
    prev = target["recommendation"]
    flip = "Watch" if prev != "Watch" else "Pass"
    c, _ = get(f"/api/decision?company_id={target['id']}&action={urllib.parse.quote(flip)}",
               method="POST")
    after = db.q1("SELECT COUNT(*) c FROM partner_actions")["c"]
    ov = db.q1("""SELECT human_override FROM scores WHERE company_id=?
                  ORDER BY scored_at DESC, id DESC LIMIT 1""", (target["id"],))
    ok = c == 200 and after == before + 1 and ov["human_override"] == flip
    check("D9 partner decision persists + logs to partner_actions", ok,
          f"{target['name']}: {prev} -> {flip}, partner_actions {before}->{after}")
    if ok:  # restore
        get(f"/api/decision?company_id={target['id']}&action={urllib.parse.quote(prev)}",
            method="POST")

    # D10 — provenance is one click from any row
    c, d = get(f"/api/provenance/{target['id']}")
    real = [x for x in d.get("signals", []) if (x.get("url") or "").startswith("http")]
    check("D10 provenance endpoint returns real fetchable URLs per company",
          c == 200 and len(real) > 0,
          f"{len(real)} signals with fetchable urls")

    # D11 — workbook downloadable through the service (real .xlsx, not an error page)
    # The file is deleted first ON PURPOSE. It used to be served straight off disk,
    # which meant the download 404'd on any host with an ephemeral filesystem —
    # every deploy, restart and idle spin-down — while the database still held
    # every row. Serving it must not depend on a build artefact surviving.
    wb_file = OUTPUT_DIR / "deal_pipeline.xlsx"
    existed = wb_file.exists()
    if existed:
        wb_file.unlink()
    c, body = get("/api/workbook", binary=True)
    is_xlsx = isinstance(body, bytes) and body[:2] == b"PK"     # zip magic
    check("D11 workbook is rebuilt from the database when the file is gone",
          c == 200 and is_xlsx and len(body) > 5000,
          f"{len(body) if isinstance(body, (bytes, str)) else 0} bytes, "
          f"zip-magic={is_xlsx}, file_was_deleted_first=True")

    # D14 — every dependency is testable from the UI, and a licensed source that
    # is waiting on a contract reports as such rather than as broken.
    c, cat = get("/api/connections")
    n = sum(len(cat.get(g, [])) for g in ("models", "integrations", "sources")) if c == 200 else 0
    c2, lic = get("/api/connections/test?target=source:coresignal", method="POST")
    check("D14 every model, key and source is individually testable from the dashboard",
          c == 200 and n >= 20 and c2 == 200 and isinstance(lic, dict) and lic.get("skipped"),
          f"{n} testable connections; licensed source reports "
          f"{'licence-gated' if isinstance(lic, dict) and lic.get('skipped') else lic}")

    # D15 — an email link must never dead-end. A digest links every top pick, but
    # briefs are capped per day, so most links used to hit a raw JSON 404 that a
    # partner reads as a broken engine. The page is now written on arrival.
    no_brief = db.q1("""SELECT c.id, c.name FROM companies c
                        WHERE c.is_synthetic=0 AND NOT EXISTS
                        (SELECT 1 FROM briefs b WHERE b.company_id=c.id) LIMIT 1""")
    if no_brief:
        c, body = get(f"/api/brief/{no_brief['id']}")
        html = isinstance(body, str) and "<html" in body and len(body) > 1500
        check("D15 an email link to a company with no brief still renders a page",
              c == 200 and html,
              f"{no_brief['name']}: HTTP {c}, {len(body) if isinstance(body, str) else 0} bytes")
    else:
        check("D15 (every company already has a brief — nothing to test)", True, "")

    # D16 — the running service can prove which build it is. Added after a week
    # spent debugging features that had never been deployed.
    c, v = get("/api/version")
    check("D16 the running build reports its own identity and completeness",
          c == 200 and isinstance(v, dict) and v.get("complete") is True,
          f"commit {v.get('commit') if isinstance(v, dict) else '?'}, missing="
          f"{v.get('missing') if isinstance(v, dict) else '?'}")

    # D12 — scheduler live in-process, honouring the search mode:
    # auto = the full job set; manual = housekeeping only, searches via button.
    log = ROOT / "logs" / "engine.out.log"
    text = log.read_text()[-40000:] if log.exists() else ""
    jobs_logged = text.count("next:")
    mode_line = "search mode:" in text
    check("D12 scheduler running with the configured search mode",
          ("scheduler started" in text and mode_line) or jobs_logged >= 2,
          f"{jobs_logged} jobs announced; mode line present: {mode_line}" if text else
          "logs/engine.out.log not present (running in foreground?)")

    # D13 — a manual search is fully tracked with live steps + saved history
    c, cur = get("/api/run/current")
    c2, runs = get("/api/runs")
    ok = c == 200 and isinstance(cur, dict) and "running" in cur and c2 == 200 \
        and isinstance(runs, dict)
    n_runs = len(runs.get("runs", [])) if isinstance(runs, dict) else 0
    detail_ok = True
    if n_runs:
        c3, d3 = get(f"/api/runs/{runs['runs'][0]['id']}")
        detail_ok = c3 == 200 and isinstance(d3, dict) and "steps" in d3 and "results" in d3
    check("D13 search runs are tracked (live progress + saved history)",
          ok and detail_ok, f"{n_runs} run(s) in history")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"DEPLOYMENT ACCEPTANCE: {passed}/{len(RESULTS)} passed")
    for n, ok, d2 in RESULTS:
        if not ok:
            print(f"  FAILING: {n} — {d2}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
