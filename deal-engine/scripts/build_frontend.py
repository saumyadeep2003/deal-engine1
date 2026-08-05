"""Assemble the Vercel frontend for the LIVE engine.

    python scripts/build_frontend.py --backend https://YOUR-SERVICE.onrender.com

Writes `frontend/`:
  index.html      the real dashboard (live mode, untouched behaviour)
  vercel.json     rewrites /api/* and /healthz to the Render backend, so the
                  browser only ever talks to the Vercel origin — no CORS, and
                  the interviewer sees one URL running the complete flow
  snapshot/       the static baked snapshot as a fallback (…vercel.app/snapshot),
                  present only if scripts/export_static_demo.py has been run

Deploy on Vercel with Root Directory = frontend. Re-run this script and push
whenever the backend URL changes.
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "frontend"
PLACEHOLDER = "https://deal-engine.onrender.com"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=PLACEHOLDER,
                    help="your Render service URL (https://….onrender.com)")
    args = ap.parse_args()
    backend = args.backend.rstrip("/")

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()

    # the live dashboard, exactly as the engine serves it
    shutil.copy(ROOT / "web" / "static" / "dashboard.html", OUT / "index.html")

    (OUT / "vercel.json").write_text(json.dumps({
        "rewrites": [
            {"source": "/api/:path*", "destination": f"{backend}/api/:path*"},
            {"source": "/healthz", "destination": f"{backend}/healthz"},
        ],
        "headers": [{"source": "/(.*)",
                     "headers": [{"key": "X-Robots-Tag", "value": "noindex"}]}],
    }, indent=2))

    snap = ROOT / "vercel-demo"
    if snap.exists():
        shutil.copytree(snap, OUT / "snapshot")
        print("  included static snapshot at /snapshot (fallback if the backend is down)")

    (OUT / "README.md").write_text(
        "# Vercel frontend (live engine)\n\n"
        "Deploy with Root Directory = `frontend`. `vercel.json` proxies `/api/*` to the "
        f"Render backend:\n\n    {backend}\n\n"
        "If that is not your backend URL, re-run\n\n"
        "    python scripts/build_frontend.py --backend https://YOUR-SERVICE.onrender.com\n\n"
        "and push. `/snapshot` serves the static baked demo as a fallback.\n")

    if backend == PLACEHOLDER:
        print(f"  NOTE: backend is the placeholder {PLACEHOLDER} — re-run with --backend "
              "once your Render URL exists, or edit frontend/vercel.json directly.")
    n = sum(1 for f in OUT.rglob("*") if f.is_file())
    print(f"  frontend/ built: {n} files, proxying /api/* -> {backend}")


if __name__ == "__main__":
    main()
