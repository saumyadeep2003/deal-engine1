"""Every external thing this engine depends on, and a button that proves it works.

The dashboard could already report health, but only *passively*: a source was
"ok" because the last scheduled run happened to succeed, and an integration was
"configured" because a credential was found on disk. Both are inferences from
history, and both were wrong at least once in this project's life — a Google
Sheet that had been failing for a day still read as connected, because nobody
had asked it a question since.

So this module answers a different question: not "did it work at some point"
but "does it work right now, when I press this". Each target makes one real
request and reports what actually came back — status, elapsed time, and the
provider's own message when it refuses.

Three groups, because they fail for different reasons and the fix differs:

  models        every model named in config/models.yaml, probed individually
                with a judging-sized prompt. A model that answers "OK" in 0.8s
                and then times out on real work is the failure this project
                already hit once; the probe is sized to catch it.
  integrations  the keyed services — Apify, email, Google Sheets, the database.
  sources       every data adapter. Licensed ones report `license_required`,
                which is a correct answer and not a failure.

A test is never allowed to raise. A diagnostic that crashes teaches nothing.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from . import db, llm
from .config import models_config, sources_config

# Which stage each model serves, so a failure reads as a consequence ("judging
# stops") rather than a name.
STAGE_MEANING = {
    "classify": "the cheap screening pass on every new company",
    "score": "the judgement written into every brief",
    "brief": "brief prose",
    "chat": "the partner chat box",
    "fallback_model": "the backup used when the routed model returns nothing",
    "strong_model": "the escalation used when the fast model answers emptily",
}


def _models() -> list[dict]:
    cfg = models_config()
    seen: dict[str, list[str]] = {}
    for stage, model in (cfg.get("tiers") or {}).items():
        seen.setdefault(model, []).append(stage)
    for key in ("fallback_model", "strong_model"):
        if cfg.get(key):
            seen.setdefault(cfg[key], []).append(key)
    out = []
    for model, roles in seen.items():
        out.append({
            "id": f"model:{model}",
            "group": "models",
            "label": model,
            "detail": "used for " + ", ".join(STAGE_MEANING.get(r, r) for r in roles),
            "needs": llm.api_key_env_name(),
            "configured": not llm.stubbed(),
        })
    return sorted(out, key=lambda x: x["label"])


def _integrations() -> list[dict]:
    from outputs import email_send, gsheets
    import os
    apify_key = bool(os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN"))
    store = db.backend_info()
    return [
        {"id": "integration:database", "group": "integrations", "label": "Database",
         "detail": f"{store.get('backend')} — "
                   + ("survives restarts" if store.get("durable")
                      else "local file, resets on restart"),
         "needs": "DATABASE_URL", "configured": store.get("backend") == "postgres"},
        {"id": "integration:llm_provider", "group": "integrations", "label": "AI provider key",
         "detail": "the account the models are billed to",
         "needs": llm.api_key_env_name(), "configured": not llm.stubbed()},
        {"id": "integration:apify", "group": "integrations", "label": "Apify",
         "detail": "web-scraped funding mentions, team size and pricing pages",
         "needs": "APIFY_TOKEN", "configured": apify_key},
        {"id": "integration:email", "group": "integrations", "label": "Email (Resend)",
         "detail": "delivers the digest to "
                   + (", ".join(email_send.recipients()) or "nobody yet"),
         "needs": "RESEND_API_KEY", "configured": email_send.status().get("configured", False)},
        {"id": "integration:sheets", "group": "integrations", "label": "Google Sheet",
         "detail": "live mirror of the workbook partners can edit",
         "needs": "GOOGLE_SERVICE_ACCOUNT_JSON", "configured": gsheets.configured()},
    ]


def _sources() -> list[dict]:
    out = []
    for src in sources_config()["sources"]:
        name = src["name"]
        row = db.q1("SELECT health, last_ok_at, error_count FROM sources WHERE name=?", (name,))
        out.append({
            "id": f"source:{name}", "group": "sources", "label": name,
            "detail": (f"licensed — {src.get('license_vendor')}"
                       if src.get("requires_license") else "free, live"),
            "requires_license": bool(src.get("requires_license")),
            "needs": src.get("env_key"),
            "configured": not src.get("requires_license"),
            "last_ok_at": row["last_ok_at"] if row else None,
            "health": row["health"] if row else "unknown",
        })
    return out


def catalogue() -> dict:
    return {"models": _models(), "integrations": _integrations(), "sources": _sources(),
            "note": "Each Test makes one real request now. Nothing here is inferred "
                    "from the last scheduled run."}


# ------------------------------------------------------------------ testing --

def test(target: str) -> dict:
    """Run one target. Always returns a verdict; never raises."""
    t0 = time.time()
    try:
        kind, _, ref = target.partition(":")
        if kind == "model":
            res = _test_model(ref)
        elif kind == "integration":
            res = _test_integration(ref)
        elif kind == "source":
            res = _test_source(ref)
        else:
            res = {"ok": False, "detail": f"unknown target '{target}'"}
    except Exception as exc:  # noqa: BLE001 — a diagnostic that crashes teaches nothing
        res = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:400]}
    res["target"] = target
    # setdefault is not enough: the shaping layer sets the key to None when the
    # probe did not time itself, and a dashboard row reading "Nones" is a bug
    # the user sees before any real failure does.
    if not res.get("seconds"):
        res["seconds"] = round(time.time() - t0, 1)
    res["tested_at"] = db.now_iso()
    return res


def _test_model(model: str) -> dict:
    """A judging-sized prompt, not 'reply OK'.

    This distinction is not academic: an earlier version of the self-test sent
    two words, passed in 0.8 seconds, and reported healthy while every real
    judgement in the pipeline was timing out at 75 seconds. The probe has to
    resemble the work."""
    if llm.stubbed():
        return {"ok": False, "detail": f"no API key set ({llm.api_key_env_name()})",
                "hint": "Add the key in the host's environment settings and redeploy."}
    r = llm.self_test(model_override=model, hard=True)
    return {"ok": bool(r.get("ok")),
            "detail": (f"answered in {r.get('seconds')}s" if r.get("ok")
                       else (r.get("reason") or "no answer")),
            "provider_message": r.get("provider_message"),
            "seconds": r.get("seconds")}


def _test_integration(ref: str) -> dict:
    if ref == "database":
        info = db.backend_info()
        one = db.q1("SELECT COUNT(*) c FROM companies")
        return {"ok": bool(one is not None),
                "detail": f"{info.get('backend')} answered — {one['c']} companies stored"
                          + ("" if info.get("durable") else " (local file: resets on restart)"),
                "hint": None if info.get("durable")
                        else "Set DATABASE_URL to a Supabase connection string (SUPABASE.md)."}
    if ref == "llm_provider":
        return _test_model(models_config()["tiers"]["classify"])
    if ref == "apify":
        from engine.adapters import apify
        return _shape(apify.self_test())
    if ref == "email":
        from outputs import email_send
        st = email_send.status()
        if not st.get("configured"):
            return {"ok": False, "detail": st.get("reason") or "no API key",
                    "hint": "Set RESEND_API_KEY."}
        # Deliberately NOT sending mail: a diagnostic that spams the partner's
        # inbox every time someone presses a button is its own bug. This checks
        # the key is accepted and a recipient exists.
        return {"ok": bool(email_send.recipients()),
                "detail": "key present; digest goes to "
                          + (", ".join(email_send.recipients()) or "nobody"),
                "hint": None if email_send.recipients()
                        else "Set a recipient in the box above the dashboard.",
                "note": st.get("domain_verified_hint")}
    if ref == "sheets":
        from outputs import gsheets
        if not gsheets.configured():
            return {"ok": False, "detail": "no service-account credentials found",
                    "hint": "Set GOOGLE_SERVICE_ACCOUNT_JSON (see DEPLOY.md §3)."}
        res = gsheets.sync(verbose=False)
        ok = res.get("status") == "ok"
        d = gsheets.diagnose(res.get("detail")) if not ok else None
        return {"ok": ok,
                "detail": (f"{res.get('tabs_written')} tabs written — {res.get('spreadsheet_url')}"
                           if ok else (d["cause"] if d else str(res.get("detail"))[:300])),
                "hint": (d["fix"] + (f" ({d['url']})" if d.get("url") else "")) if d else None,
                "extra": {"service_account_email": gsheets.service_account_email()}}
    return {"ok": False, "detail": f"unknown integration '{ref}'"}


def _test_source(name: str) -> dict:
    from .ingest import load_adapters
    ads = load_adapters(only=[name])
    if not ads:
        return {"ok": False, "detail": f"'{name}' is not registered in config/sources.yaml"}
    ad = ads[0]
    if getattr(ad, "requires_license", False):
        # Not a failure. The adapter is wired, tested and scheduled; it is waiting
        # on a contract. Reporting that as "down" would be the dishonest reading.
        return {"ok": True, "skipped": True,
                "detail": f"licence-gated ({ad.cfg.get('license_vendor') or 'vendor'}) — "
                          "adapter wired and scheduled, returns LicenseRequired without a key",
                "hint": f"Set {ad.cfg.get('env_key')} to switch it on."}
    res = ad.probe()
    return _shape(res)


def _shape(res: dict) -> dict:
    """Adapters and integrations each report in their own words; the UI needs one
    shape."""
    if not isinstance(res, dict):
        return {"ok": False, "detail": str(res)[:300]}
    ok = res.get("ok")
    if ok is None:
        ok = res.get("status") in ("ok", "success", True)
    return {"ok": bool(ok),
            "detail": str(res.get("detail") or res.get("reason") or res.get("message") or "")[:400],
            "hint": res.get("hint"),
            "fetch_mode": res.get("fetch_mode"),
            "seconds": res.get("seconds")}


def test_all(group: str | None = None) -> list[dict]:
    """Every target, or one group. Sequential on purpose — these are live calls to
    other people's services, and firing twenty at once to make a dashboard feel
    snappy is how you get rate-limited by the sources you depend on."""
    cat = catalogue()
    targets = []
    for g in ("models", "integrations", "sources"):
        if group in (None, g):
            targets += [t["id"] for t in cat[g]]
    return [test(t) for t in targets]


def since_window(days: int = 2) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
