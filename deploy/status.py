"""Render the operational status report for `./dealctl status`.

Kept as a module rather than inline shell-python so it is readable and testable.

    python deploy/status.py [--url http://127.0.0.1:8787]
"""
from __future__ import annotations
import json
import sys
import urllib.error
import urllib.request

BOLD, DIM, RED, YEL, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[0m"


def fetch(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/api/summary", timeout=20) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def main() -> int:
    url = "http://127.0.0.1:8787"
    if "--url" in sys.argv:
        url = sys.argv[sys.argv.index("--url") + 1]
    d = fetch(url)
    if d is None:
        print(f"{RED}  HTTP: not responding at {url}{RESET}")
        print(f"{DIM}  start it with ./dealctl start, or check ./dealctl logs{RESET}")
        return 1

    print(f"{BOLD}Pipeline{RESET}")
    for s in d["funnel"]:
        print(f"  {s['stage']:<34}{s['value']:>8,}")
    r = d["recommendations"]
    mix = f"{r.get('Deep Dive', 0)} / {r.get('Watch', 0)} / {r.get('Pass', 0)}"
    print(f"  {'Deep Dive / Watch / Pass':<34}{mix:>8}")
    print(f"  {'Briefs published':<34}{d['briefs']:>8}")
    print(f"  {'Signals filtered out':<34}{str(d['signals_filtered_pct']) + '%':>8}")
    print(f"  {'Companies dropped by filter':<34}{str(d['companies_filtered_pct']) + '%':>8}")

    print(f"\n{BOLD}Posture{RESET}")
    if d["llm"]["stubbed"]:
        print(f"  LLM judgment    {YEL}STUB{RESET} — no LLM API key; judgment fields are "
              "deliberately unusable, computed scoring unaffected")
    else:
        spend = sum((u.get("pt") or 0) + (u.get("ct") or 0) for u in d["llm"]["usage"])
        print(f"  LLM judgment    live — {spend:,} tokens logged across "
              f"{len(d['llm']['usage'])} stage(s)")
    e = d["email"]
    if e["configured"] and e["to"]:
        print(f"  Email           sending to {', '.join(e['to'])} via {e['provider']}")
    else:
        print(f"  Email           {YEL}not sending{RESET} ({e['reason']}) — digests render "
              "to output/digests/")
    g = d["sheets"]
    if g["configured"]:
        last = (g.get("last_sync") or {})
        print(f"  Google Sheet    configured — last sync {last.get('status', 'never')}"
              + (f" ({last.get('spreadsheet_url')})" if last.get("spreadsheet_url") else ""))
    else:
        print(f"  Google Sheet    not configured — local workbook is the source of truth")

    free = [s for s in d["sources"] if not s["requires_license"]]
    lic = [s for s in d["sources"] if s["requires_license"]]
    bad = [s for s in free if s["health"] in ("degraded", "down")]
    cached = sum(s.get("cached") or 0 for s in free)
    print(f"  Free sources    {len(free)} registered, {len(bad)} degraded, "
          f"{cached} signal(s) from cached snapshots")
    for s in bad:
        print(f"    {YEL}!{RESET} {s['name']}: {s['health']}"
              + (f" — last ok {s['last_ok_at']}" if s["last_ok_at"] else " — never succeeded"))
    print(f"  Licence-gated   {len(lic)} adapters wired, returning LicenseRequired")

    job = (d.get("job") or {})
    if job.get("running"):
        print(f"\n{BOLD}Job{RESET}\n  running: {job['running']}")
    elif job.get("last"):
        j = job["last"]
        print(f"\n{BOLD}Last manual run{RESET}\n  {j.get('finished')} in "
              f"{j.get('seconds')}s (rc={j.get('returncode')})")
    print(f"\n{DIM}dashboard: {url}{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
