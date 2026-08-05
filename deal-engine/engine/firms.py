"""The firm dataset — brief §5: "SEC Form D filings (cross-referenced against our
existing dataset of ~11,500 firms for new fund announcements)".

The fund holds that dataset; this module is the loader and the index. Drop a CSV
at `config/firms.csv` (or point `FIRM_DATASET` at one) and every Form D issuer is
matched against it. Without that file the engine falls back to the ~90 firms in
`config/thesis.yaml`, and `coverage()` says so plainly rather than implying the
full dataset is loaded.

Accepted CSV shapes — the loader sniffs the header:
    name
    firm_name,tier
    Firm Name,Tier,AUM
Any column named name/firm/firm_name/investor/company is used as the firm name;
a tier/tier_number column is honoured if present.
"""
from __future__ import annotations
import csv
import os
import re
from functools import lru_cache
from pathlib import Path

from .config import CONFIG_DIR, thesis

NAME_COLS = ("name", "firm", "firm_name", "investor", "investor_name", "company", "fund")
TIER_COLS = ("tier", "tier_number", "firm_tier")

# Legal/структural noise that must not distinguish two records of the same firm.
_SUFFIX_RE = re.compile(
    r"\b(l\.?p\.?|llc|llp|ltd|limited|inc|incorporated|corp|corporation|gmbh|bv|ab|"
    r"holdings?|management|managements|partners|partnership|capital|ventures?|"
    r"venture|advisors?|advisers?|group|fund[s]?|associates|co)\b\.?", re.I)


def normalize_firm(name: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"[&]", " and ", n)
    n = re.sub(r"[^\w\s]", " ", n)
    prev = None
    while prev != n:                      # strip stacked suffixes
        prev = n
        n = _SUFFIX_RE.sub(" ", n).strip()
        n = re.sub(r"\s+", " ", n)
    return n or name.strip().lower()


def dataset_path() -> Path | None:
    env = os.environ.get("FIRM_DATASET")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    p = CONFIG_DIR / "firms.csv"
    return p if p.exists() else None


@lru_cache(maxsize=1)
def firm_index() -> dict[str, dict]:
    """{normalized_name: {"name","tier","source"}} — dataset first, config always."""
    index: dict[str, dict] = {}

    # config tiers are authoritative for tier assignment
    tiers = thesis()["investor_tiers"]
    for tier_num, key in ((1, "tier1"), (2, "tier2"), (3, "tier3")):
        for name in tiers.get(key, []):
            index[normalize_firm(name)] = {"name": name, "tier": tier_num, "source": "thesis.yaml"}

    path = dataset_path()
    if path:
        with open(path, newline="", encoding="utf-8-sig") as f:
            sample = f.read(4096)
            f.seek(0)
            has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
            reader = csv.DictReader(f) if has_header else None
            if reader and reader.fieldnames:
                lower = {(c or "").strip().lower(): c for c in reader.fieldnames}
                name_col = next((lower[c] for c in NAME_COLS if c in lower), reader.fieldnames[0])
                tier_col = next((lower[c] for c in TIER_COLS if c in lower), None)
                for row in reader:
                    raw = (row.get(name_col) or "").strip()
                    if not raw:
                        continue
                    key = normalize_firm(raw)
                    tier = None
                    if tier_col:
                        try:
                            tier = int(str(row.get(tier_col)).strip())
                        except (TypeError, ValueError):
                            tier = None
                    if key in index:
                        # config tier wins; dataset only adds the canonical name
                        continue
                    index[key] = {"name": raw, "tier": tier, "source": path.name}
            else:
                f.seek(0)
                for row in csv.reader(f):
                    if row and row[0].strip():
                        key = normalize_firm(row[0])
                        index.setdefault(key, {"name": row[0].strip(), "tier": None,
                                               "source": path.name})
    return index


def match(issuer: str) -> dict | None:
    """Exact-after-normalisation, then two bounded fallbacks.

    A false positive here mislabels a company raise as a peer-firm fund
    formation, so both fallbacks are deliberately narrow:

    - multi-word / long keys match only on a word boundary, so 'Index' cannot
      match 'Indexed Bio' and 'Sequoia Capital' still matches
      'Sequoia Capital Global Growth';
    - short keys ('GV', 'IVP', '8VC', 'NEA') match only as a standalone token,
      so 'GV 2026 LP' matches while 'EGV Arigon' does not.
    """
    if not issuer:
        return None
    idx = firm_index()
    norm = normalize_firm(issuer)
    if not norm:
        return None
    if norm in idx:
        return idx[norm]

    tokens = set(norm.split())
    best: tuple[int, dict] | None = None
    for key, rec in idx.items():
        if len(key) < 5:
            if key in tokens:                      # standalone-token match only
                score = len(key)
            else:
                continue
        elif re.search(rf"(?<![\w]){re.escape(key)}(?![\w])", norm):
            score = len(key)                       # prefer the longest match
        else:
            continue
        if best is None or score > best[0]:
            best = (score, rec)
    return best[1] if best else None


def coverage() -> dict:
    """Honest reporting for the dashboard and the README."""
    path = dataset_path()
    idx = firm_index()
    from_dataset = sum(1 for r in idx.values() if r["source"] not in ("thesis.yaml",))
    return {
        "total_firms": len(idx),
        "from_dataset": from_dataset,
        "from_config": len(idx) - from_dataset,
        "dataset_path": str(path) if path else None,
        "note": None if path else
                ("no firm dataset loaded — matching against the "
                 f"{len(idx)} firms in config/thesis.yaml only. Drop the fund's "
                 "~11,500-firm export at config/firms.csv (or set FIRM_DATASET) "
                 "to widen coverage; no code change needed."),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(coverage(), indent=2))
