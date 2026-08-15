"""Component 02 — SEC Form D watcher. Highest-signal free source.

EDGAR full-text search (efts.sec.gov) for Form D filings since the last
checkpoint, one phrase query per thesis keyword. For top hits we fetch the
filing's primary_doc.xml and parse issuer, CIK, offering amount, date and
related persons — all real, all traceable to a sec.gov URL.

Issuers are cross-referenced against the configured firm list: a known firm
filing a Form D is a fund-formation signal; an unknown issuer is a company
raise that enters the pipeline. The checkpoint only advances on success.
"""
from __future__ import annotations
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote

from .. import db, firms
from ..models import Signal
from .base import BaseAdapter

FTS_URL = ("https://efts.sec.gov/LATEST/search-index?q=%22{query}%22&forms=D"
           "&startdt={start}&enddt={end}")

DEFAULT_QUERIES = [
    "artificial intelligence", "machine learning", "robotics", "cybersecurity",
    "autonomous", "semiconductor", "biotechnology", "aerospace", "defense",
    "voice recognition", "data infrastructure", "nuclear",
]

FUND_NAME_RE = re.compile(
    r"\b(fund|capital partners|ventures? (i{1,3}|iv|v|vi{1,3}|ix|x)\b|,?\s*l\.?p\.?$|"
    r"partners\s+(i{1,3}|iv|v)\b|opportunit(y|ies)|spv|feeder|co-invest|"
    r"a series of|master series|gaingels|business trust|group \d+ llc)", re.I)


def _strip(t: str | None) -> str | None:
    return t.strip() if t else None


class EdgarFormDAdapter(BaseAdapter):
    name = "edgar_formd"
    interval_minutes = 360
    # The detail XML is the ONLY place a Form D names its people, its offering
    # amount and its incorporation year. At 15 per run, 205 of 220 filings never
    # had theirs read — which is why "Founders identified" sat at 0 of 160 while
    # the filings that name them were already in the database. SEC asks for a
    # descriptive User-Agent and under 10 requests/second; it does not charge, and
    # it does not ration. The old cap was protecting a budget that does not exist.
    max_detail_fetches = 150   # per run, overridable in config/sources.yaml

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.queries = self.cfg.get("queries", DEFAULT_QUERIES)
        self.max_detail_fetches = int(self.cfg.get("max_detail_fetches",
                                                   self.max_detail_fetches))

    # -- fetch -----------------------------------------------------------------

    def fetch(self, since: datetime) -> list[Signal]:
        checkpoint = db.checkpoint_get(self.name) or since.date().isoformat()
        end = datetime.utcnow().date().isoformat()
        signals: list[Signal] = []
        seen_adsh: set[str] = set()
        hits: list[dict] = []

        # -- 1. THE INDEX SWEEP: every Form D, not keyword hits ----------------
        # For its first months this adapter only ran twelve keyword searches
        # against full-text search, which quietly inverted the engine's own
        # architecture: a Form D whose text didn't contain one of our phrases
        # did not exist to the system, so "completeness" was capped by
        # vocabulary. EDGAR publishes a daily index of EVERY filing; sweeping it
        # and letting our own deterministic filter decide relevance is what the
        # funnel was designed for. The keyword search below survives as a safety
        # net (it reaches back further than the index window); dedupe_key makes
        # the overlap harmless.
        for entry in self._index_sweep(checkpoint, end):
            if entry["adsh"] not in seen_adsh:
                seen_adsh.add(entry["adsh"])
                hits.append({"src": entry, "keyword": "(daily index — all Form Ds)",
                             "mode": self._last_fetch_mode})

        any_success = bool(hits)
        for kw in self.queries:
            url = FTS_URL.format(query=quote(kw), start=checkpoint, end=end)
            try:
                body, mode = self.http_get(url)
                data = json.loads(body)
                any_success = True
            except Exception as exc:  # noqa: BLE001
                self.record_error(exc)
                continue
            for h in data.get("hits", {}).get("hits", []):
                src = h.get("_source", {})
                adsh = src.get("adsh")
                if not adsh or adsh in seen_adsh:
                    continue
                seen_adsh.add(adsh)
                hits.append({"src": src, "keyword": kw, "mode": mode})

        # fetch primary_doc.xml details for the newest N filings
        hits.sort(key=lambda h: h["src"].get("file_date", ""), reverse=True)
        max_date = checkpoint
        for i, h in enumerate(hits):
            src, kw = h["src"], h["keyword"]
            adsh = src["adsh"]
            cik = int(src["ciks"][0]) if src.get("ciks") else None
            file_date = src.get("file_date", "")
            max_date = max(max_date, file_date)
            issuer = re.sub(r"\s*\(CIK \d+\)\s*$", "", (src.get("display_names") or ["?"])[0]).strip()
            index_url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                         f"{adsh.replace('-', '')}/{adsh}-index.htm")
            detail: dict = {}
            if cik and i < self.max_detail_fetches:
                detail = self._fetch_detail(cik, adsh)

            is_fund = detail.get("is_fund")
            if is_fund is None:
                is_fund = bool(FUND_NAME_RE.search(issuer))
            known_firm = self._match_known_firm(issuer)
            kind = "fund_formation" if (is_fund or known_firm) else "filing"

            payload = {
                "issuer": issuer, "cik": cik, "accession": adsh,
                "file_date": file_date, "form": src.get("form"),
                "matched_keyword": kw,
                "state": (src.get("biz_states") or [None])[0],
                "location": (src.get("biz_locations") or [None])[0],
                "known_firm": known_firm,
                **detail,
            }
            signals.append(Signal(
                kind=kind, observed_at=file_date or db.now_iso(),
                url=index_url, dedupe_key=f"edgar:{adsh}",
                payload=payload, company_name=None if kind == "fund_formation" else issuer,
                fetch_mode=h["mode"]))

        if any_success:
            db.checkpoint_set(self.name, max_date)  # advances only on success
        elif not signals:
            raise RuntimeError("EDGAR full-text search unreachable and no snapshot available")
        return signals

    # -- daily index sweep -----------------------------------------------------

    INDEX_URL = ("https://www.sec.gov/Archives/edgar/daily-index/"
                 "{year}/QTR{q}/form.{ymd}.idx")
    max_index_days = 5      # per run; the checkpoint carries continuity between runs

    def _index_sweep(self, checkpoint: str, end: str) -> list[dict]:
        """Every Form D filed since the checkpoint, from the daily form index.

        The index is plain text, one line per filing, published nightly. Weekends
        and market holidays have no file — a 404 there is a calendar fact, not an
        error, and must not mark the source degraded."""
        from datetime import date, timedelta
        try:
            start = date.fromisoformat(checkpoint[:10])
            stop = date.fromisoformat(end[:10])
        except ValueError:
            return []
        days = int(self.cfg.get("max_index_days", self.max_index_days))
        first = max(start, stop - timedelta(days=days - 1))
        out: list[dict] = []
        d = first
        while d <= stop:
            ymd = d.strftime("%Y%m%d")
            url = self.INDEX_URL.format(year=d.year, q=(d.month - 1) // 3 + 1, ymd=ymd)
            try:
                body, _ = self.http_get(url, retries=0)
            except Exception:  # noqa: BLE001 — no file on a non-trading day
                d += timedelta(days=1)
                continue
            out.extend(self.parse_form_index(body, d.isoformat()))
            d += timedelta(days=1)
        return out

    @staticmethod
    def parse_form_index(text: str, file_date: str) -> list[dict]:
        """Form D lines out of a daily form.idx. Shaped like an FTS hit so the
        one processing path downstream serves both discovery routes."""
        out: list[dict] = []
        for line in (text or "").splitlines():
            # 'D    <company name>   <CIK>   <date>   edgar/data/<cik>/<adsh>.txt'
            if not (line.startswith("D ") or line.startswith("D/A ")):
                continue
            m = re.search(r"edgar/data/(\d+)/([\d-]{18,22})\.txt\s*$", line)
            if not m:
                continue
            cik, adsh = int(m.group(1)), m.group(2)
            form = line.split()[0]
            name = re.sub(r"\s{2,}.*$", "", line[len(form):].strip()).strip()
            if not name:
                continue
            out.append({"adsh": adsh, "ciks": [cik], "file_date": file_date,
                        "form": form, "display_names": [name],
                        "biz_states": [], "biz_locations": []})
        return out

    # -- detail parse ----------------------------------------------------------

    def _fetch_detail(self, cik: int, adsh: str) -> dict:
        url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
               f"{adsh.replace('-', '')}/primary_doc.xml")
        try:
            body, mode = self.http_get(url)
        except Exception:  # noqa: BLE001
            return {}
        return self.parse_form_d_xml(body, url)

    @staticmethod
    def parse_form_d_xml(xml_text: str, url: str) -> dict:
        try:
            root = ET.fromstring(xml_text.strip())
        except ET.ParseError:
            return {}

        def find(path: str) -> str | None:
            el = root.find(path)
            return _strip(el.text) if el is not None else None

        def money(path: str) -> float | None:
            v = find(path)
            if v is None or v.lower() == "indefinite":
                return None
            try:
                return float(v)
            except ValueError:
                return None

        industry = find(".//offeringData/industryGroup/industryGroupType")
        related = []
        for rp in root.findall(".//relatedPersonsList/relatedPersonInfo"):
            first = rp.findtext("relatedPersonName/firstName") or ""
            last = rp.findtext("relatedPersonName/lastName") or ""
            titles = [_strip(t.text) for t in rp.findall(".//relationshipClarification") if t.text]
            name = f"{first} {last}".strip()
            if name:
                related.append({"name": name, "titles": titles})
        return {
            "entity_name": find(".//primaryIssuer/entityName"),
            "entity_type": find(".//primaryIssuer/entityType"),
            "year_of_incorporation": find(".//primaryIssuer/yearOfInc/value"),
            "industry_group": industry,
            "is_fund": industry == "Pooled Investment Fund" or None,
            "total_offering_usd": money(".//offeringData/offeringSalesAmounts/totalOfferingAmount"),
            "total_sold_usd": money(".//offeringData/offeringSalesAmounts/totalAmountSold"),
            "date_of_first_sale": find(".//offeringData/typeOfFiling/dateOfFirstSale/value"),
            "related_persons": related,
            "detail_url": url,
        }

    # -- firm cross-reference --------------------------------------------------

    @staticmethod
    def _match_known_firm(issuer: str) -> str | None:
        """Cross-reference the issuer against the firm dataset (brief §5).

        engine/firms.py loads the fund's ~11,500-firm export when present and
        falls back to the configured tier lists otherwise — see firms.coverage().
        """
        rec = firms.match(issuer)
        return rec["name"] if rec else None
