"""Apollo — measured headcount, growth and funding history, for Deep Dive companies only.

A live test through the user's own Apollo account settled a vendor question: one
enrichment call returned headcount (41), the 6/12/24-month growth series
(+22.9%/+34.4%/+26.5%), a department-level function mix, and a four-round funding
history with investors and news URLs — the exact fields this engine has been
stamping "requires Coresignal" and half of what Crunchbase sells, for 1 credit.

Scope is the deliberate part. Credits are finite and Deep Dive is the fund's own
statement of where attention goes, so this adapter enriches ONLY companies whose
current call is Deep Dive (partner override included), skips anything enriched in
the last 30 days, and never spends more than `max_companies` credits in a run. A
data budget spent evenly across 347 companies is a data budget spent mostly on
companies nobody will read.

Nothing new is invented downstream: funding history is emitted as ordinary
`funding_event` signals, so the EXISTING ingest path builds the rounds and
tier-matches the investors; headcount and growth land in the same
`enrichment_cache` fields the workbook and briefs already read, which means the
"requires Coresignal" cells simply start carrying measured values with an
Apollo provenance note.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from .. import db
from ..enrichment import cache_get, cache_put
from ..models import Signal
from .base import BaseAdapter

API = "https://api.apollo.io/api/v1"
AMT_RE = re.compile(r"([\d.]+)\s*([KMBkmb])?")
SCALE = {"k": 1e3, "m": 1e6, "b": 1e9}

CACHE_FIELD = "apollo_org"          # raw response, 30-day TTL = the credit guard
CACHE_TTL_HOURS = 720


def parse_amount(text: str | None, currency: str | None = "$") -> float | None:
    """'57M' -> 57_000_000. Only USD is trusted into amount_usd — a mislabelled
    currency inflating a round 80x is worse than a missing amount."""
    if not text or (currency or "$") != "$":
        return None
    m = AMT_RE.match(str(text).strip())
    if not m:
        return None
    try:
        return float(m.group(1)) * SCALE.get((m.group(2) or "").lower(), 1.0)
    except ValueError:
        return None


class ApolloEnrichAdapter(BaseAdapter):
    name = "apollo_enrich"
    interval_minutes = 1440
    requires_license = False
    max_companies = 25              # = max credits a single run may spend

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.env_key = self.cfg.get("env_key", "APOLLO_API_KEY")
        self.max_companies = int(self.cfg.get("max_companies", self.max_companies))

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.env_key) or None

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def probe(self) -> dict:
        """Costs zero credits: the auth health endpoint proves the key is real.
        Whether the free plan's key can reach the ENRICH endpoint is a separate
        fact — the first real run answers it, and the health row reports it."""
        if not self.api_key:
            return {"ok": False,
                    "detail": f"{self.env_key} is not set — generate one in Apollo: "
                              "Settings → Integrations → API",
                    "hint": "Free-plan keys may not reach every enrichment endpoint; "
                            "this probe verifies the key, the first run verifies access."}
        res = self.probe_url(API + "/auth/health", expect="")
        try:
            self.http_get(API + f"/auth/health", retries=0, headers=self._headers())
            res["detail"] = "API key accepted by Apollo (auth check, 0 credits spent)"
            res["ok"] = True
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "detail": f"key rejected: {str(exc)[:120]}"}
        return res

    def fetch(self, since: datetime) -> list[Signal]:
        if not self.api_key:
            return []
        rows = db.q("""SELECT c.id, c.name, c.domain FROM companies c
                       JOIN scores s ON s.company_id=c.id
                       WHERE s.id=(SELECT id FROM scores WHERE company_id=c.id
                                   ORDER BY scored_at DESC, id DESC LIMIT 1)
                       AND COALESCE(s.human_override, s.recommendation)='Deep Dive'
                       AND c.is_synthetic=0 AND c.domain IS NOT NULL
                       AND c.domain NOT LIKE '%.example'
                       ORDER BY s.percentile DESC""")
        signals: list[Signal] = []
        spent = 0
        for c in rows:
            if spent >= self.max_companies:
                break
            if cache_get(c["id"], CACHE_FIELD):
                continue            # fresh within 30 days — do not re-spend the credit
            org = self._enrich(c["domain"])
            spent += 1              # count the attempt: a miss may still be billed work
            if not org:
                continue
            signals += self.apply(c["id"], c["name"], c["domain"], org)
        return signals

    def _enrich(self, domain: str) -> dict | None:
        try:
            body, _ = self.http_get(API + f"/organizations/enrich?domain={domain}",
                                    retries=0, headers=self._headers())
            return (json.loads(body) or {}).get("organization") or None
        except Exception as exc:  # noqa: BLE001
            self.record_error(exc)
            return None

    def apply(self, company_id: int, name: str, domain: str, org: dict) -> list[Signal]:
        """Write what Apollo measured into the places the engine already reads."""
        src = f"Apollo (organization enrichment, {domain})"
        cache_put(company_id, CACHE_FIELD,
                  {k: org.get(k) for k in ("estimated_num_employees",
                                           "organization_headcount_six_month_growth",
                                           "organization_headcount_twelve_month_growth",
                                           "founded_year", "total_funding")},
                  src, 0.9, ttl_hours=CACHE_TTL_HOURS)

        # -- the two cells that said "requires Coresignal" ---------------------
        heads = org.get("estimated_num_employees")
        if isinstance(heads, int) and heads > 0:
            cache_put(company_id, "headcount", f"{heads} (Apollo)", src, 0.85)
        g6 = org.get("organization_headcount_six_month_growth")
        g12 = org.get("organization_headcount_twelve_month_growth")
        if isinstance(g6, (int, float)) or isinstance(g12, (int, float)):
            bits = []
            if isinstance(g6, (int, float)):
                bits.append(f"{g6 * 100:+.0f}% 6mo")
            if isinstance(g12, (int, float)):
                bits.append(f"{g12 * 100:+.0f}% 12mo")
            cache_put(company_id, "headcount_growth_6m",
                      " / ".join(bits) + " (Apollo)", src, 0.85)
        mix = org.get("departmental_head_count")
        if isinstance(mix, dict):
            top = {k: v for k, v in sorted(mix.items(), key=lambda kv: -(kv[1] or 0))
                   if v}
            if top:
                cache_put(company_id, "careers_functions", top, src, 0.8)

        # -- company facts, only where the engine had nothing ------------------
        c = db.q1("SELECT description, founded_year FROM companies WHERE id=?",
                  (company_id,))
        desc = (org.get("short_description") or "").strip()
        if desc and not (c and c["description"]):
            db.execute("UPDATE companies SET description=? WHERE id=?",
                       (desc[:300], company_id))
        if org.get("founded_year") and not (c and c["founded_year"]):
            db.execute("UPDATE companies SET founded_year=? WHERE id=?",
                       (org["founded_year"], company_id))

        # -- funding history as ordinary signals: the EXISTING ingest path -----
        # builds the rounds and tier-matches the investors from these payloads.
        signals: list[Signal] = []
        for ev in (org.get("funding_events") or []):
            amount = parse_amount(ev.get("amount"), ev.get("currency"))
            investors = [i.strip() for i in (ev.get("investors") or "").split(",")
                         if i.strip()]
            signals.append(Signal(
                kind="funding_event",
                observed_at=(ev.get("date") or db.now_iso())[:10],
                url=ev.get("news_url") or f"https://{domain}",
                dedupe_key=f"apollo:fund:{ev.get('id') or (domain + str(ev.get('date')))}",
                payload={"title": f"{name} {ev.get('type') or 'round'}"
                                  + (f" ${ev.get('amount')}" if ev.get("amount") else ""),
                         "amount_usd": amount, "stage": ev.get("type"),
                         "lead_investor": investors[0] if investors else None,
                         "investors": investors,
                         "platform": "apollo", "apollo_event_id": ev.get("id")},
                company_name=name, company_domain=domain,
                fetch_mode=self._last_fetch_mode))
        return signals
