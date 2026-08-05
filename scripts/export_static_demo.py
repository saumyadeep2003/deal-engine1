"""Export a static snapshot of the dashboard for Vercel (or any static host).

Vercel is serverless — it cannot run the scheduler, SQLite, or long jobs — so
what deploys there is an honest SNAPSHOT: the real dashboard UI reading baked
JSON captured from a real pipeline run, clearly bannered as such. The live
engine stays a `git clone && ./deploy/install.sh` away.

    python scripts/export_static_demo.py       # writes vercel-demo/

What gets baked:
  api/*.json                 every read endpoint's real response
  api/provenance/<id>.json   one-click provenance per pipeline company
  api/brief/<id>.json        every validated brief
  api/chat.json              real answers to the example questions (computed now)
  api/workbook.xlsx          the actual workbook
  digest.html                the latest digest
  index.html                 the same dashboard, with a fetch interceptor + banner
"""
from __future__ import annotations
import json
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "vercel-demo"


def main() -> None:
    from fastapi.testclient import TestClient

    from engine import db, ingest
    from web.api import app

    db.connect()
    ingest.register_sources()
    client = TestClient(app)

    api = OUT / "api"
    if OUT.exists():
        shutil.rmtree(OUT)
    (api / "provenance").mkdir(parents=True)
    (api / "brief").mkdir(parents=True)

    def bake(path: str, dest: str) -> dict | None:
        r = client.get(path)
        if r.status_code != 200:
            print(f"  skip {path} ({r.status_code})")
            return None
        (api / dest).write_text(json.dumps(r.json(), default=str))
        print(f"  baked {dest}")
        return r.json()

    summary = bake("/api/summary", "summary.json")
    pipeline = bake("/api/pipeline", "pipeline.json")
    (api / "runs").mkdir(exist_ok=True)
    bake("/api/run/plan", "run-plan.json")
    runs = bake("/api/runs", "runs.json")
    for r in (runs or {}).get("runs", [])[:10]:
        bake(f"/api/runs/{r['id']}", f"runs/{r['id']}.json")
    bake("/api/sectors", "sectors.json")
    bake("/api/peers", "peers.json")
    bake("/api/commentary", "commentary.json")
    bake("/api/news", "news.json")
    bake("/api/stale", "stale.json")
    bake("/api/review-queue", "review-queue.json")
    bake("/api/events", "events.json")

    for row in (pipeline or {}).get("rows", []):
        cid = row["id"]
        bake(f"/api/provenance/{cid}", f"provenance/{cid}.json")
        r = client.get(f"/api/brief/{cid}/raw")
        if r.status_code == 200:
            (api / "brief" / f"{cid}.json").write_text(json.dumps(r.json(), default=str))

    # real chat/scan answers, computed now against the real DB
    from chat import EXAMPLE_QUESTIONS, answer
    from engine.sectors import scan_thesis
    chat_qs = EXAMPLE_QUESTIONS + [
        "summarise what people are saying about Pangram",
        "what are the emerging sub-sectors?",
        "who's quietly investing in defence tech?",
    ]
    baked_chat = {q: answer(q) for q in dict.fromkeys(chat_qs)}
    (api / "chat.json").write_text(json.dumps(baked_chat))
    scans = {t: scan_thesis(t, 10) for t in [
        "autonomous inspection robots for energy infrastructure",
        "robotics for logistics and warehouse automation",
        "AI for defence and satellite intelligence",
    ]}
    (api / "scan.json").write_text(json.dumps(scans, default=str))
    print(f"  baked chat.json ({len(baked_chat)} answers), scan.json ({len(scans)} theses)")

    # binary artefacts
    wb = ROOT / "output" / "deal_pipeline.xlsx"
    if wb.exists():
        shutil.copy(wb, api / "workbook.xlsx")
    digests = sorted((ROOT / "output" / "digests").glob("digest_*.html"))
    if digests:
        shutil.copy(digests[-1], OUT / "digest.html")
    briefs_dir = ROOT / "output" / "briefs"
    if briefs_dir.exists():
        shutil.copytree(briefs_dir, OUT / "briefs")

    # ---- index.html: the same dashboard + interceptor + banner ----
    html = (ROOT / "web" / "static" / "dashboard.html").read_text()
    captured = (summary or {}).get("generated_at", "")[:16].replace("T", " ")
    shim = f"""
<script>
/* ---- STATIC DEMO SHIM (only present in the exported snapshot) ---------- */
window.STATIC_DEMO = true;
window.STATIC_CAPTURED = {json.dumps(captured + " UTC")};
(() => {{
  const real = window.fetch.bind(window);
  const J = (obj, status = 200) => Promise.resolve(new Response(
    JSON.stringify(obj), {{ status, headers: {{ "Content-Type": "application/json" }} }}));
  window.fetch = (url, opts = {{}}) => {{
    const u = String(url);
    if (!u.startsWith("/api/")) return real(url, opts);
    const [path, qs] = u.split("?");
    const params = new URLSearchParams(qs || "");
    if ((opts.method || "GET").toUpperCase() !== "GET")
      return J({{ ok: false, reason: "static snapshot — live actions need the running engine" }}, 409);
    if (path === "/api/pipeline")
      return real("api/pipeline.json").then(r => r.json()).then(d => {{
        let rows = d.rows;
        const st = params.get("status"), sec = params.get("sector"),
              q = (params.get("q") || "").toLowerCase();
        if (st && st !== "all") rows = rows.filter(r => (r.human_override || r.recommendation) === st);
        if (sec && sec !== "all") rows = rows.filter(r => r.sector === sec);
        if (q) rows = rows.filter(r => (r.name || "").toLowerCase().includes(q));
        return J({{ count: rows.length, rows }});
      }});
    if (path === "/api/chat")
      return real("api/chat.json").then(r => r.json()).then(d => J({{
        question: params.get("q"),
        answer: d[params.get("q")] ||
          "This is a static snapshot — free-form questions need the running engine " +
          "(git clone, ./deploy/install.sh). Try the example buttons: those answers " +
          "were computed live against the real database at capture time.",
        stubbed: true }}));
    if (path === "/api/scan")
      return real("api/scan.json").then(r => r.json()).then(d => J({{
        thesis: params.get("thesis_text"),
        results: d[params.get("thesis_text")] || [] }}));
    if (path === "/api/run/current") return J({{ running: false, run: null }});
    if (path === "/api/run/plan") return real("api/run-plan.json");
    if (path === "/api/runs") return real("api/runs.json");
    const mr = path.match(/^\\/api\\/runs\\/(\\d+)$/);
    if (mr) return real(`api/runs/${{mr[1]}}.json`);
    const m1 = path.match(/^\\/api\\/provenance\\/(\\d+)$/);
    if (m1) return real(`api/provenance/${{m1[1]}}.json`);
    const m2 = path.match(/^\\/api\\/brief\\/(\\d+)\\/raw$/);
    if (m2) return real(`api/brief/${{m2[1]}}.json`);
    if (path === "/api/workbook") return real("api/workbook.xlsx");
    return real(`api${{path.slice(4)}}.json`);
  }};
  addEventListener("DOMContentLoaded", () => {{
    const bar = document.createElement("div");
    bar.style.cssText = "background:#1F3B57;color:#fff;padding:9px 16px;font-size:13px;" +
      "display:flex;gap:10px;align-items:center;flex-wrap:wrap";
    bar.innerHTML = "";
    const strong = document.createElement("strong");
    strong.textContent = "Static demo snapshot";
    const span = document.createElement("span");
    span.textContent = "— real pipeline data captured " + window.STATIC_CAPTURED +
      ". The live engine (scheduler, ingest, chat, decisions) runs locally: ";
    const a = document.createElement("a");
    a.href = "https://github.com/"; a.textContent = "clone the repo → ./deploy/install.sh";
    a.style.color = "#9ec5f4"; a.id = "repoLink";
    bar.append(strong, span, a);
    document.body.prepend(bar);
    const css = document.createElement("style");
    css.textContent = "#btnRefresh,#btnDigest{{display:none!important}}";
    document.head.appendChild(css);
  }});
}})();
</script>
"""
    html = html.replace("<title>Thirdbase Deal Engine</title>",
                        "<title>Thirdbase Deal Engine — demo snapshot</title>" + shim)
    html = html.replace('href="/api/workbook"', 'href="api/workbook.xlsx"')
    (OUT / "index.html").write_text(html)

    (OUT / "vercel.json").write_text(json.dumps({
        "headers": [{"source": "/(.*)",
                     "headers": [{"key": "X-Robots-Tag", "value": "noindex"}]}]}, indent=2))
    (OUT / "README.md").write_text(
        "# Static demo snapshot\n\nDeployed to Vercel as a static site. Real pipeline data "
        f"captured {captured} UTC from the live engine; the engine itself runs locally "
        "(see the repository root: `./deploy/install.sh`).\n\nRegenerate after any pipeline "
        "run with:\n\n    python scripts/export_static_demo.py\n")

    n_files = sum(1 for _ in OUT.rglob("*") if _.is_file())
    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"\nexported {n_files} files, {total / 1024:.0f} KB -> {OUT}")


if __name__ == "__main__":
    main()
