"""What code is actually running, answerable in one glance.

This exists because of a specific, expensive failure. For a week the hosted
engine served code from an older commit while the repository, the local folder
and every conversation about it assumed otherwise. Features were "not working"
that had never been deployed; a Google Sheets fix was debugged twice; an Excel
download was called broken when the running build simply predated the fix.
Nothing in the product could distinguish "this feature is broken" from "this
feature is not there", and that is the distinction that matters first.

So the running service reports its own identity: the commit it was built from,
and a list of capability markers checked at runtime rather than declared. A
marker is true because the code answered, not because a constant says so — a
version string can be copied forward by a bad deploy, an import cannot.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def commit() -> str | None:
    """The build's commit. Render exposes it as an env var; a local checkout has
    git. Neither is guaranteed, and an honest None beats an invented hash."""
    for key in ("RENDER_GIT_COMMIT", "SOURCE_VERSION", "GIT_COMMIT", "COMMIT_SHA"):
        v = os.environ.get(key)
        if v:
            return v[:12]
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()[:12]
    except Exception:  # noqa: BLE001
        pass
    return None


def branch() -> str | None:
    return os.environ.get("RENDER_GIT_BRANCH") or None


# Each entry: a capability a reader might ask about, and a probe that proves the
# code for it is present in THIS process. Deliberately import-based — a build
# that lost a file fails its marker instead of quietly serving the old path.
def features() -> dict:
    def _has(module: str, attr: str | None = None) -> bool:
        try:
            mod = __import__(module, fromlist=["_"])
            return bool(getattr(mod, attr)) if attr else True
        except Exception:  # noqa: BLE001
            return False

    return {
        "gatekeeper": _has("engine.gatekeeper", "verify_judgement"),
        "connection_tests": _has("engine.connections", "catalogue"),
        "workbook_rebuilds_itself": _has("outputs.excel", "ensure_workbook"),
        "sheets_diagnosis": _has("outputs.gsheets", "diagnose"),
        "apify": _has("engine.adapters.apify", "ApifyAdapter"),
        "ats_boards": _has("engine.adapters.ats_boards", "AtsBoardsAdapter"),
        "bluesky": _has("engine.adapters.bluesky", "BlueskyAdapter"),
        "wayback_team": _has("engine.adapters.wayback", "WaybackAdapter"),
        "digest_recipient_editable": _has("outputs.email_send", "set_recipients"),
        "ist_timestamps": _has("engine.db", "to_display"),
        "hiring_from_free_sources": _has("engine.hiring", "summary_line"),
        "coverage_report": _has("engine.coverage", "report"),
        "company_profiles": _has("engine.profile", "section"),
        "founders_from_filings": _has("engine.people", "sync_from_filings"),
        "criteria_estimates": _has("engine.estimates", "criteria_scorecard"),
        "edgar_index_sweep": _has("engine.adapters.edgar_formd", "EdgarFormDAdapter")
                             and hasattr(__import__("engine.adapters.edgar_formd",
                                 fromlist=["_"]).EdgarFormDAdapter, "parse_form_index"),
        "company_news_watch": _has("engine.adapters.company_news", "CompanyNewsAdapter"),
        "companies_house": _has("engine.adapters.companies_house", "CompaniesHouseAdapter"),
    }


def info() -> dict:
    f = features()
    missing = sorted(k for k, v in f.items() if not v)
    return {
        "commit": commit(),
        "branch": branch(),
        "features": f,
        "missing": missing,
        "complete": not missing,
        "note": ("every expected capability is present in the running build"
                 if not missing else
                 "THIS BUILD IS INCOMPLETE — the following are missing from the running "
                 "code, which usually means the deploy did not pick up the latest commit: "
                 + ", ".join(missing)),
    }
