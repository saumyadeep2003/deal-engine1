"""Store a fetched body into an adapter's snapshot cache (same format http_get writes).

Usage: python3 scripts/cache_put.py <adapter_name> <url> <body_file>
Strips markdown code fences if present. Used to seed the cache with real
payloads fetched out-of-band on network-restricted machines.
"""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.config import CACHE_DIR  # noqa: E402

adapter, url, body_file = sys.argv[1], sys.argv[2], sys.argv[3]
body = Path(body_file).read_text()
m = re.match(r"^\s*```[a-zA-Z]*\n(.*)\n```\s*$", body, re.S)
if m:
    body = m.group(1)
h = hashlib.sha1(url.encode()).hexdigest()[:16]
d = CACHE_DIR / adapter
d.mkdir(parents=True, exist_ok=True)
(d / f"{h}.json").write_text(json.dumps({
    "url": url,
    "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "body": body}))
print(f"cached {adapter} <- {url} ({len(body)} bytes)")
