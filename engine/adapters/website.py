"""Company surface area — brief §5: "Company websites for product positioning,
customer logos, pricing, careers".

Real extraction from real pages, deterministically:
  positioning     <title>, meta description, first <h1>/<h2>
  customer logos  images inside sections whose heading/class/alt says
                  customer/client/trusted-by/partner — the alt text IS the logo name
  pricing         a reachable pricing page, its detected plan names and price tokens
  careers         handled by careers.py (open reqs + engineering/sales/G&A mix)

No model is involved: a model asked "who are their customers" would invent
logos. If a page is unreachable the field is null with a reason.
"""
from __future__ import annotations
import re
from datetime import datetime

from bs4 import BeautifulSoup

from .. import db
from ..models import Signal
from .base import BaseAdapter

CUSTOMER_HINT = re.compile(
    r"(customer|client|trusted[\s-]?by|partners?|logos?|used[\s-]?by|powering|"
    r"backed[\s-]?by|companies)", re.I)
PRICE_RE = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d{2})?(?:\s?(?:/|per\s)\s?(?:mo|month|yr|year|seat|user))?", re.I)
PLAN_RE = re.compile(r"\b(free|starter|basic|pro|professional|team|business|growth|"
                     r"scale|enterprise|custom)\b", re.I)
PRICING_PATHS = ["/pricing", "/plans", "/pricing/", "/price"]
NOISE_ALT = re.compile(r"^(logo|icon|image|img|arrow|star|avatar|menu|close|"
                       r"search|play|check|company logo)?$", re.I)


class WebsiteAdapter(BaseAdapter):
    """Runs on pipeline companies that have a resolved domain (post-filter only —
    scraping before the deterministic filter is the expensive inversion)."""
    name = "company_website"
    interval_minutes = 1440
    max_companies = 10

    def fetch(self, since: datetime) -> list[Signal]:
        rows = db.q("""SELECT id, name, domain FROM companies
                       WHERE domain IS NOT NULL AND domain NOT LIKE '%.example'
                       AND is_synthetic=0 AND status IN ('pipeline','hot','watchlist')
                       ORDER BY last_signal_at DESC LIMIT ?""", (self.max_companies,))
        signals: list[Signal] = []
        for row in rows:
            payload = self.profile_site(row["domain"])
            if not payload:
                continue
            payload["company_id"] = row["id"]
            signals.append(Signal(
                kind="surface", observed_at=db.now_iso(),
                url=f"https://{row['domain']}",
                dedupe_key=f"surface:{row['domain']}:{db.now_iso()[:10]}",
                payload=payload, company_domain=row["domain"], company_name=row["name"],
                fetch_mode=self._last_fetch_mode))
        return signals

    # -- extraction ------------------------------------------------------------

    def profile_site(self, domain: str) -> dict | None:
        try:
            home, mode = self.http_get(f"https://{domain}", retries=0)
        except Exception:  # noqa: BLE001
            return None
        soup = BeautifulSoup(home, "html.parser")
        out: dict = {"positioning": self._positioning(soup),
                     "customer_logos": self._logos(soup),
                     "pricing": self._pricing(domain, soup)}
        return out

    @staticmethod
    def _positioning(soup: BeautifulSoup) -> dict:
        def txt(node):
            return re.sub(r"\s+", " ", node.get_text(" ", strip=True))[:220] if node else None
        meta = soup.find("meta", attrs={"name": "description"}) or \
            soup.find("meta", attrs={"property": "og:description"})
        return {
            "title": txt(soup.title),
            "meta_description": (meta.get("content") or "").strip()[:300] if meta else None,
            "h1": txt(soup.find("h1")),
            "h2": txt(soup.find("h2")),
        }

    @staticmethod
    def _logos(soup: BeautifulSoup) -> dict:
        """Alt text on images inside a customer/trusted-by region. Alt text is
        author-written, so it is evidence, not inference."""
        names: list[str] = []
        regions = []
        for tag in soup.find_all(["section", "div", "aside", "ul"]):
            hint = " ".join(filter(None, [
                " ".join(tag.get("class") or []), tag.get("id") or "",
                tag.get("aria-label") or "",
                (tag.find(["h2", "h3", "h4", "p"]).get_text(" ", strip=True)[:80]
                 if tag.find(["h2", "h3", "h4", "p"]) else "")]))
            if CUSTOMER_HINT.search(hint):
                regions.append(tag)
        for r in regions[:6]:
            for img in r.find_all("img")[:40]:
                alt = (img.get("alt") or "").strip()
                alt = re.sub(r"\s*(logo|icon)\s*$", "", alt, flags=re.I).strip()
                if alt and not NOISE_ALT.match(alt) and len(alt) < 40:
                    names.append(alt)
        seen, uniq = set(), []
        for n in names:
            if n.lower() not in seen:
                seen.add(n.lower())
                uniq.append(n)
        return {"count": len(uniq), "names": uniq[:25],
                "regions_found": len(regions),
                "reason": None if uniq else
                          "no customer/trusted-by region with named logos on the homepage"}

    def _pricing(self, domain: str, home: BeautifulSoup) -> dict:
        # follow an on-page pricing link first, then try conventional paths
        candidates = []
        for a in home.find_all("a", href=True):
            if re.search(r"pricing|plans", a["href"], re.I):
                href = a["href"]
                candidates.append(href if href.startswith("http")
                                  else f"https://{domain}{href if href.startswith('/') else '/' + href}")
        candidates += [f"https://{domain}{p}" for p in PRICING_PATHS]
        for url in dict.fromkeys(candidates):
            try:
                body, _ = self.http_get(url, retries=0)
            except Exception:  # noqa: BLE001
                continue
            text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
            prices = list(dict.fromkeys(PRICE_RE.findall(text)))[:12]
            plans = list(dict.fromkeys(p.title() for p in PLAN_RE.findall(text)))[:8]
            if prices or plans:
                return {"url": url, "public": True, "price_points": prices,
                        "plan_names": plans,
                        "model": "self-serve" if prices else "sales-led (plans, no public price)"}
        return {"url": None, "public": False, "price_points": [], "plan_names": [],
                "reason": "no public pricing page found — often sales-led enterprise pricing"}
