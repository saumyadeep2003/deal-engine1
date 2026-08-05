"""List primary_doc.xml URLs for the newest Form D filings found in the cached
FTS snapshots — mirrors EdgarFormDAdapter's selection so the cache matches the
adapter's requests exactly."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.config import CACHE_DIR  # noqa: E402

hits, seen = [], set()
for f in (CACHE_DIR / "edgar_formd").glob("*.json"):
    snap = json.loads(f.read_text())
    try:
        data = json.loads(snap["body"])
    except Exception:
        continue
    for h in data.get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        adsh = src.get("adsh")
        if not adsh or adsh in seen or not src.get("ciks"):
            continue
        seen.add(adsh)
        hits.append(src)
hits.sort(key=lambda s: s.get("file_date", ""), reverse=True)
for src in hits[:15]:
    cik = int(src["ciks"][0])
    adsh = src["adsh"]
    print(f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/primary_doc.xml")
