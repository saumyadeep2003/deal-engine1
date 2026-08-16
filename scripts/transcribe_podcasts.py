#!/usr/bin/env python3
"""Local podcast transcription — the deep half of the podcast_notes source.

Run this on YOUR machine (or any box with a real CPU), never on the 512MB web
instance. It downloads recent episodes from the feeds configured in
config/sources.yaml, transcribes them with faster-whisper
(https://github.com/SYSTRAN/faster-whisper — free, local, no API), finds
tracked-company mentions in the transcript, and stores the surrounding
sentences as commentary signals in the SAME database the deployed engine reads
(set DATABASE_URL to the Supabase string first — otherwise this writes only to
your local SQLite and the dashboard never sees it).

    pip install faster-whisper feedparser httpx
    DATABASE_URL=postgres://... python scripts/transcribe_podcasts.py --max-episodes 4

Everything stored carries its provenance: the episode link, the show, a
'whisper local transcript' marker, and the timestamp window of the mention —
so a partner reading a quote can go listen to the actual minute.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from engine import db, ingest  # noqa: E402
from engine.adapters.podcasts import PodcastsAdapter, parse_feed, tracked_names  # noqa: E402
from engine.config import sources_config  # noqa: E402
from engine.models import Signal  # noqa: E402


def transcribe(audio_url: str, model_size: str = "base") -> list[dict]:
    """[{start, end, text}] segments. Import stays inside the function so the
    engine never needs faster-whisper installed."""
    from faster_whisper import WhisperModel
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        with httpx.stream("GET", audio_url, timeout=120, follow_redirects=True) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                f.write(chunk)
        path = f.name
    model = WhisperModel(model_size, device="auto", compute_type="int8")
    segments, _info = model.transcribe(path, vad_filter=True)
    out = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    Path(path).unlink(missing_ok=True)
    return out


def mentions(segments: list[dict], watch: list[dict], context: int = 2) -> list[dict]:
    hits = []
    for i, seg in enumerate(segments):
        for c in watch:
            if not c["pattern"].search(seg["text"]):
                continue
            lo, hi = max(0, i - context), min(len(segments), i + context + 1)
            quote = " ".join(s["text"] for s in segments[lo:hi]).strip()
            hits.append({"company": c, "quote": quote[:900],
                         "at_seconds": int(seg["start"])})
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-episodes", type=int, default=4,
                    help="episodes to transcribe this run (each takes minutes)")
    ap.add_argument("--model", default="base",
                    help="faster-whisper model size (tiny/base/small/medium)")
    args = ap.parse_args()

    db.connect()
    src = next((s for s in sources_config()["sources"] if s["name"] == "podcast_notes"),
               None)
    feeds = (src or {}).get("feeds") or []
    if not feeds:
        print("no podcast feeds configured under podcast_notes in config/sources.yaml")
        return 1
    watch = tracked_names()
    print(f"listening for {len(watch)} tracked companies across {len(feeds)} shows")

    adapter = PodcastsAdapter(src)
    done = 0
    signals: list[Signal] = []
    for feed in feeds:
        if done >= args.max_episodes:
            break
        try:
            body, _ = adapter.http_get(feed["url"], retries=0)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! feed unreachable: {feed.get('name')}: {exc}")
            continue
        for ep in parse_feed(body, feed.get("name", feed["url"])):
            if done >= args.max_episodes or not ep.get("audio_url"):
                continue
            # skip episodes already transcribed (idempotent by dedupe key prefix)
            if db.q1("SELECT id FROM signals WHERE dedupe_key LIKE ?",
                     (f"podcast_t:{ep['show']}:{ep['title'][:80]}:%",)):
                continue
            print(f"  transcribing: {ep['show']} — {ep['title'][:70]}")
            try:
                segs = transcribe(ep["audio_url"], args.model)
            except Exception as exc:  # noqa: BLE001
                print(f"    ! transcription failed: {exc}")
                continue
            done += 1
            for hit in mentions(segs, watch):
                c = hit["company"]
                signals.append(Signal(
                    kind="commentary", observed_at=ep["published"],
                    url=ep["link"] or feed["url"],
                    dedupe_key=f"podcast_t:{ep['show']}:{ep['title'][:80]}:{c['id']}",
                    payload={"quote": hit["quote"], "episode": ep["title"],
                             "show": ep["show"], "at_seconds": hit["at_seconds"],
                             "via": "whisper local transcript"},
                    raw=hit["quote"], company_name=c["name"],
                    company_domain=c["domain"]))
                print(f"    -> {c['name']} mentioned at ~{hit['at_seconds']}s")
    if signals:
        ingest.register_sources()
        stats = ingest.store_signals("podcast_notes", signals)
        print(f"stored: {stats['new']} new commentary signal(s), "
              f"{stats['duplicate']} already known")
    else:
        print(f"transcribed {done} episode(s); no tracked-company mentions found "
              "(an honest zero — nothing was invented)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
