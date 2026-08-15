"""Hiring signal, assembled from the free sources that answer what Coresignal is bought for.

Coresignal costs what it costs because a fund wants one thing from it: is this
team growing, and in which direction. Two free sources between them answer a
version of that, and this module is the single place the rest of the system asks.

  ats_boards   open roles and function mix from a company's OWN public job board
               (Greenhouse / Lever / Ashby). A leading indicator — more useful
               than a headcount snapshot, and a different measurement, so it is
               reported as `open_roles` and never as headcount.
  wayback_team distinct profile links on archived copies of the company's own
               team page. Low confidence by construction, and it carries its
               caveat wherever it goes.

This exists because both adapters were storing real signals that nothing ever
read: briefs kept printing "Headcount / 6-month growth: — (requires Coresignal)"
for companies whose open roles the engine had collected that morning. Data that
is gathered and never surfaced is worse than data never gathered — it costs the
requests and teaches the reader the system knows less than it does.
"""
from __future__ import annotations

import json

from . import db


def hiring(company_id: int) -> dict:
    """Everything free sources know about this company's growth.

    Always returns a dict. `available` is False when nothing was found, with a
    `reason` — because "we looked and there is no public board" and "we never
    looked" are different facts and a reader deserves to know which."""
    out = {"available": False, "reason": None, "open_roles": None, "change": None,
           "function_mix": None, "observed_at": None, "source": None, "url": None,
           "team_then": None, "team_now": None, "team_window": None,
           "team_confidence": None, "caveat": None}

    rows = db.q("""SELECT observed_at, url, payload_json FROM signals
                   WHERE company_id=? AND kind='hiring'
                   ORDER BY observed_at DESC, id DESC LIMIT 12""", (company_id,))
    boards, archive = [], []
    for r in rows:
        try:
            p = json.loads(r["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if p.get("open_roles") is not None:
            boards.append((r, p))
        elif p.get("platform") == "wayback":
            archive.append((r, p))

    if boards:
        r, p = boards[0]
        out.update(available=True, open_roles=p.get("open_roles"),
                   function_mix=p.get("function_mix"), observed_at=r["observed_at"],
                   source=p.get("provider"), url=r["url"],
                   sample_titles=list(p.get("sample_titles") or [])
                                 if isinstance(p.get("sample_titles"), (list, tuple)) else None)
        # Velocity from the engine's OWN dated observations, not a vendor's claim.
        # One reading is not a trend, and saying so is more useful than a zero.
        prev = next(((rr, pp) for rr, pp in boards[1:]
                     if (rr["observed_at"] or "")[:10] != (r["observed_at"] or "")[:10]), None)
        if prev and isinstance(prev[1].get("open_roles"), int) and isinstance(p.get("open_roles"), int):
            out["change"] = p["open_roles"] - prev[1]["open_roles"]
            out["change_since"] = prev[0]["observed_at"]
        else:
            out["change_reason"] = ("first reading — a trend needs two runs, which is why "
                                    "this is measured rather than asserted")

    if archive:
        r, p = archive[0]
        out.update(available=True, team_then=p.get("people_then"), team_now=p.get("people_now"),
                   team_window=f"{p.get('from_date')} to {p.get('to_date')}",
                   team_confidence=p.get("confidence"), caveat=p.get("caveat"))
        out.setdefault("team_url", r["url"])

    if not out["available"]:
        out["reason"] = ("no public job board found for this company, and its team page "
                         "is not in the Internet Archive")
    return out


def _never_raise(fn):
    """Decorations degrade; they do not take a run step down. The briefs and
    publish steps of run 18 both died on ONE malformed payload reached through a
    renderer here — every other company's brief was lost to it."""
    import functools

    @functools.wraps(fn)
    def wrapped(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:  # noqa: BLE001
            return None
    return wrapped


@_never_raise
def summary_line(company_id: int) -> str | None:
    """One line for a brief or a table cell. None when there is nothing to say —
    the caller then prints the honest licence gap instead of a blank."""
    h = hiring(company_id)
    if not h["available"]:
        return None
    bits = []
    if h["open_roles"] is not None:
        s = f"{h['open_roles']} open role(s) on their own {h['source']} board"
        if h.get("change") is not None:
            s += f" ({h['change']:+d} since {(h.get('change_since') or '')[:10]})"
        elif h.get("change_reason"):
            s += " (first reading)"
        bits.append(s)
    if h.get("function_mix"):
        top = sorted(h["function_mix"].items(), key=lambda kv: -kv[1])[:3]
        bits.append("hiring mostly " + ", ".join(f"{k} ({v})" for k, v in top))
    if h.get("team_now") is not None:
        bits.append(f"team page listed {h['team_then']} people, now {h['team_now']} "
                    f"({h['team_window']}, low confidence)")
    return "; ".join(bits) or None


def cell(company_id: int) -> str:
    """Workbook cell. Falls back to the licence gap, never to a blank."""
    return summary_line(company_id) or "— (requires Coresignal)"


def growth_cell_safe(company_id: int) -> str:
    try:
        return growth_cell(company_id)
    except Exception:  # noqa: BLE001
        return "— (requires Coresignal)"


def growth_cell(company_id: int) -> str:
    h = hiring(company_id)
    if h.get("change") is not None:
        return f"{h['change']:+d} open roles since {(h.get('change_since') or '')[:10]}"
    if h.get("team_now") is not None and h.get("team_then") is not None:
        return (f"{h['team_now'] - h['team_then']:+d} people on their team page "
                f"({h['team_window']}, low confidence)")
    if h["available"]:
        return "first reading — a trend needs a second search"
    return "— (requires Coresignal)"
