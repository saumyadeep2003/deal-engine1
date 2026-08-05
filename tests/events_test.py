"""Precision/recall tests for the founder-move and customer-win classifiers.

These exist because the first version of this classifier passed hand-written
positives and then produced five false positives on the real corpus ("won't"
matching `won`, a bare lab mention counting as a departure). The negatives below
are the actual headlines that broke it — they are regression tests, not examples.

    python tests/events_test.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.events import classify_signal  # noqa: E402

POSITIVE = [
    ("founder_move", "Mira Murati, former OpenAI CTO, launches new AI startup Thinking Machines"),
    ("founder_move", "Ex-DeepMind researchers found Latent Labs to work on protein design"),
    ("founder_move", "Former Anthropic engineer emerges from stealth with Cortex AI"),
    ("founder_move", "Two ex-Google DeepMind scientists spun out of the lab to start Helix"),
    ("customer_win", "Acme Robotics wins contract with Siemens AG to deploy 400 units"),
    ("customer_win", "Zeta selected by Walmart Inc for supply chain automation, $12 million contract"),
    ("customer_win", "Foo signs agreement with Deutsche Bank for fraud detection"),
    ("customer_win", "Nimbus lands a deal with United Airlines for predictive maintenance"),
]

# Real headlines from the ingested corpus that must NOT classify.
NEGATIVE = [
    'Show HN: A "roast my startup" space that won\'t shadowban you for self-promotion',
    "What if more data alone won't solve robotics?",
    "Nvidia Launches Open Secure AI Alliance",
    "Microsoft launches new in-house AI models. Cuts costs up to 89% versus OpenAI",
    "make a statement (a prediction, a claim, a hash of a file) and sign it with a local Ed25519 key",
    "Foobar raises $20M Series A led by Sequoia Capital",
    "Team wins the world championship title",
    "Researchers found that transformer models scale predictably",
    "Startup lands $5M seed round",
    "OpenAI launches a new reasoning model",
    "Anthropic hires more researchers",
    "The company won an award for design",
    "We partner with customers to improve their platform",
]


def main() -> int:
    misses, fps = [], []
    for want, text in POSITIVE:
        kinds = [e["event"] for e in classify_signal("news", text)]
        if want not in kinds:
            misses.append((want, text))
    for text in NEGATIVE:
        got = classify_signal("news", text)
        if got:
            fps.append((text, got))

    print(f"positives detected: {len(POSITIVE) - len(misses)}/{len(POSITIVE)}")
    for want, t in misses:
        print(f"  MISS ({want}): {t}")
    print(f"false positives:    {len(fps)}/{len(NEGATIVE)}")
    for t, g in fps:
        print(f"  FP: {t}\n      -> {g}")

    ok = not misses and not fps
    print("\n" + ("EVENT CLASSIFIER: PASS" if ok else "EVENT CLASSIFIER: FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
