"""Real email delivery via Resend (the production target named in the architecture doc).

Sends the Mon/Wed/Fri digest and instant alerts. Without `RESEND_API_KEY` it does
NOT pretend to send: it writes the HTML to output/digests|alerts/, logs that
delivery was skipped and why, and records the attempt in `digests`/`alerts_log`
with `delivered=0`. Nothing silently no-ops.

Resend free tier: with no verified domain you send from `onboarding@resend.dev`
and can only deliver to the email address on the Resend account. Verify a domain
to send to partners — that is a config change (`DIGEST_FROM`), not a code change.
"""
from __future__ import annotations
import re
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import db  # noqa: E402
from engine.config import OUTPUT_DIR, env  # noqa: E402

API_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Thirdbase Deal Engine <onboarding@resend.dev>"


def configured() -> bool:
    return bool(env("RESEND_API_KEY"))


RECIPIENT_SETTING = "digest_recipients"
EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[A-Za-z]{2,}$")


def recipients() -> list[str]:
    """Dashboard override wins over the DIGEST_TO env var, so a partner can change
    where the digest lands without a redeploy. Stored in the database, so it
    survives restarts and travels with the Supabase backup."""
    row = db.q1("SELECT value FROM app_settings WHERE key=?", (RECIPIENT_SETTING,))
    raw = (row["value"] if row and row["value"] else None) or env("DIGEST_TO", "")
    return [r.strip() for r in (raw or "").split(",") if r.strip()]


def recipients_source() -> str:
    row = db.q1("SELECT value FROM app_settings WHERE key=?", (RECIPIENT_SETTING,))
    return "dashboard" if row and row["value"] else ("env" if env("DIGEST_TO") else "unset")


def set_recipients(raw: str) -> dict:
    """Validate and store. Rejects anything that is not a plain address list —
    a malformed value here fails silently at send time, hours later."""
    addrs = [a.strip() for a in (raw or "").replace(";", ",").split(",") if a.strip()]
    bad = [a for a in addrs if not EMAIL_RE.match(a)]
    if bad:
        return {"ok": False, "reason": f"not a valid email address: {', '.join(bad[:3])}"}
    if len(addrs) > 5:
        return {"ok": False, "reason": "at most 5 recipients"}
    value = ",".join(addrs)
    db.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
               " updated_at=excluded.updated_at",
               (RECIPIENT_SETTING, value, db.now_iso()))
    return {"ok": True, "to": addrs, "cleared": not addrs}


def status() -> dict:
    to = recipients()
    return {"provider": "resend", "configured": configured(),
            "from": env("DIGEST_FROM", DEFAULT_FROM),
            "to": to,
            "to_source": recipients_source(),
            # the free-tier trap, surfaced where the address is edited rather than
            # discovered when a partner reports "I never got the email"
            "domain_verified_hint": (
                "Resend's free tier only delivers to the address that owns the Resend "
                "account unless you verify a domain — other addresses will be accepted "
                "here and then bounce."),
            "reason": None if configured() and to
            else ("RESEND_API_KEY not set" if not configured() else "no recipient set")}


def send(subject: str, html: str, kind: str = "digest",
         to: list[str] | None = None, verbose: bool = True) -> dict:
    """Returns {delivered, detail}. Never raises on delivery failure — the
    pipeline must not die because an inbox was unreachable."""
    to = to or recipients()
    if not configured() or not to:
        detail = ("RESEND_API_KEY not set" if not configured()
                  else "DIGEST_TO not set (no recipients)")
        if verbose:
            print(f"  email NOT sent ({detail}) — HTML written to output/ instead")
        return {"delivered": False, "detail": detail}
    try:
        r = httpx.post(API_URL, timeout=30,
                       headers={"Authorization": f"Bearer {env('RESEND_API_KEY')}",
                                "Content-Type": "application/json"},
                       json={"from": env("DIGEST_FROM", DEFAULT_FROM), "to": to,
                             "subject": subject, "html": html})
        if r.status_code >= 400:
            detail = f"HTTP {r.status_code}: {r.text[:200]}"
            if verbose:
                print(f"  ! email delivery FAILED — {detail}")
            db.insert("review_queue", {"kind": "email_failure", "created_at": db.now_iso(),
                                       "payload_json": json.dumps({"subject": subject,
                                                                   "error": detail})})
            return {"delivered": False, "detail": detail}
        msg_id = (r.json() or {}).get("id")
        if verbose:
            print(f"  email delivered to {', '.join(to)} (resend id {msg_id})")
        return {"delivered": True, "detail": msg_id}
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        if verbose:
            print(f"  ! email delivery error — {detail}")
        return {"delivered": False, "detail": detail}


def send_digest(path: Path | None = None, verbose: bool = True,
                digest_kind: str = "mwf_digest") -> dict:
    """Send the most recent rendered digest and mark the RIGHT row delivered —
    a full snapshot must not stamp its delivery id onto the scheduled digest's
    record (they are different kinds with different windows)."""
    if path is None:
        files = sorted((OUTPUT_DIR / "digests").glob("digest_*.html"))
        if not files:
            return {"delivered": False, "detail": "no digest rendered yet"}
        path = files[-1]
    label = path.stem.replace("digest_full_", "full snapshot ").replace("digest_", "")
    res = send(f"Thirdbase deal digest — {label}",
               path.read_text(), kind="digest", verbose=verbose)
    row = db.q1("SELECT id FROM digests WHERE kind=? ORDER BY sent_at DESC LIMIT 1",
                (digest_kind,))
    if row:
        db.execute("UPDATE digests SET delivered=?, delivery_detail=? WHERE id=?",
                   (1 if res["delivered"] else 0, res["detail"], row["id"]))
    return res


def send_alert(rule: str, payload: dict, html: str, verbose: bool = True) -> dict:
    subject = {"tier1_coinvest": "⚡ 2+ Tier 1 firms co-investing",
               "thesis_shift": "⚡ Tracked firm investing off-thesis",
               "watched_founder": "⚡ Watched founder started a new company",
               }.get(rule, f"⚡ Deal engine alert: {rule}")
    company = payload.get("company") or payload.get("vehicle") or payload.get("founder") or ""
    return send(f"{subject} — {company}"[:120], html, kind="alert", verbose=verbose)


def self_test(to: str | None = None) -> int:
    """`python -m outputs.email_send --test you@example.com`"""
    st = status()
    print(f"Resend configured: {st['configured']}  from={st['from']}  to={st['to'] or to}")
    if not st["configured"]:
        print("Set RESEND_API_KEY in .env (https://resend.com — free tier, no domain needed)")
        return 1
    res = send("Deal engine — delivery test",
               "<p>If you are reading this, Resend delivery works. "
               "Mon/Wed/Fri digests and instant alerts will arrive here.</p>",
               to=[to] if to else None)
    print("delivered" if res["delivered"] else f"NOT delivered: {res['detail']}")
    return 0 if res["delivered"] else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--test":
        raise SystemExit(self_test(args[1] if len(args) > 1 else None))
    print(json.dumps(status(), indent=2))
