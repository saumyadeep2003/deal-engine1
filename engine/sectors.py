"""Component 12 — sector detection: signal-to-consensus ratio over a real corpus.

Corpus: arXiv abstracts, HN titles, GitHub repo descriptions (technical signal)
vs mainstream RSS/news (consensus). TF-IDF vectors (numpy) → greedy cosine
clustering → per-cluster velocity, consensus volume, ratio, source diversity.

High ratio = emerging before consensus. Both high = already priced.
Heavy coverage + decelerating signal = contrarian call. A source-diversity
gate stops one prolific account from manufacturing a trend.

Component 15 — on-demand sector scan — lives here too (scan_thesis).
"""
from __future__ import annotations
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import numpy as np

from . import db, llm
from .config import thesis  # noqa: F401  (used by scan_thesis theme expansion)

STOP = set("""the a an and or of to in for with on by from at as is are was be this that it its
we our you your they their new using use used based via can will has have how what why not more
type these than then out up all model models paper approach results show two one 3d method large
towards toward learning data ai code inc llc ltd corp corporation lp series fund
capital partners ventures holdings systems technologies startup company raises
raised launches launch million billion valuation round
san francisco new york boston seattle austin denver chicago angeles diego jose
palo alto menlo park cambridge oakland lewes claymont miami delaware california
future global passes project work show tell ask""".split())

TECH_SOURCES = {"arxiv", "github_trending", "hn"}
CONSENSUS_SOURCES = {"rss_news"}

# A sector is a market, not a vendor. TF-IDF cannot tell the difference: it found
# that many documents mention Cloudflare and duly produced a cluster labelled
# "Cloudflare AI Platform Software", which is a topic, not something a fund can
# invest in. Big-company and product names are therefore barred from labels — they
# may still hold a cluster together, they just cannot name it.
VENDOR_NAMES = set("""
openai anthropic google microsoft amazon aws azure meta nvidia apple ibm oracle
salesforce cloudflare databricks snowflake stripe shopify uber airbnb tesla spacex
github gitlab docker kubernetes hugging huggingface langchain llama gpt claude gemini
mistral cohere perplexity figma notion slack zoom atlassian datadog mongodb redis
postgres postgresql elastic confluent twilio okta crowdstrike palantir intel amd arm
qualcomm broadcom samsung sony netflix spotify tiktok bytedance alibaba tencent baidu
anaconda klaviyo airtable observability cognition
""".split())

# Words that describe an EVENT rather than a market. "acquires" recurring across
# M&A headlines produced a cluster called "Company Acquisition Deals" — real
# clustering, useless as a sector.
EVENT_WORDS = set("""
acquires acquired acquisition acquisitions merger merges ipo layoffs layoff hires
hiring raises raised funding round rounds valuation shutdown shuts closes closed
launches launched announces announced partnership partners appoints names
""".split())


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", text.lower())
    return [w for w in words if w not in STOP]


def _corpus() -> list[dict]:
    """Everything the trend detector reads. Brief §2(b) lists five inputs:
    GP commentary, frontier-lab hiring, founder migration, fund formation, and
    academic / open-source research velocity — so founder_move, hiring and
    fund_formation signals are part of the corpus, not just news and research."""
    labs = [l.lower() for l in thesis().get("frontier_labs", [])]
    docs = []
    rows = db.q("""SELECT s.id, s.kind, s.company_id, s.observed_at, s.url, s.raw,
                          s.payload_json, so.name src
                   FROM signals s JOIN sources so ON s.source_id=so.id
                   WHERE s.kind IN ('research','news','launch','repo','funding_event',
                                    'filing','fund_formation','founder_move',
                                    'customer_win','hiring','commentary','surface')
                   AND s.fetch_mode != 'synthetic_demo'""")
    for r in rows:
        p = json.loads(r["payload_json"])
        if r["kind"] in ("filing", "fund_formation"):
            # Form D is capital-flow signal: issuer + industry + matched keyword
            text = " ".join(str(p.get(k) or "") for k in
                            ("issuer", "industry_group", "matched_keyword", "location"))
        elif r["kind"] == "surface":
            pos = p.get("positioning") or {}
            text = " ".join(str(pos.get(k) or "") for k in
                            ("title", "meta_description", "h1", "h2"))
        else:
            text = " ".join(str(p.get(k) or "") for k in
                            ("title", "abstract", "description", "summary", "story_text",
                             "selftext", "quote"))
        if len(text) < 25:
            continue
        blob = (text + " " + (r["raw"] or "")).lower()
        docs.append({"id": r["id"], "src": r["src"], "url": r["url"],
                     "kind": r["kind"], "company_id": r["company_id"],
                     "observed_at": r["observed_at"], "text": text,
                     "points": p.get("points") or 0,
                     "frontier_lab": any(l in blob for l in labs),
                     "tokens": _tokens(text)})
    return docs


def _tfidf(docs: list[dict]) -> tuple[np.ndarray, list[str]]:
    df: Counter = Counter()
    for d in docs:
        df.update(set(d["tokens"]))
    vocab = [w for w, c in df.items() if 2 <= c <= len(docs) * 0.4]
    idx = {w: i for i, w in enumerate(vocab)}
    mat = np.zeros((len(docs), len(vocab)))
    for i, d in enumerate(docs):
        tf = Counter(d["tokens"])
        for w, n in tf.items():
            j = idx.get(w)
            if j is not None:
                mat[i, j] = (1 + math.log(n)) * math.log(len(docs) / df[w])
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return mat / norms, vocab


def _greedy_clusters(sim: np.ndarray, threshold: float = 0.18,
                     min_size: int = 4) -> list[list[int]]:
    n = sim.shape[0]
    unassigned = set(range(n))
    clusters = []
    order = np.argsort(-sim.sum(axis=1))       # densest docs seed first
    for seed in order:
        if seed not in unassigned:
            continue
        members = [i for i in unassigned if sim[seed, i] >= threshold] + [int(seed)]
        members = sorted(set(members))
        if len(members) >= min_size:
            clusters.append(members)
            unassigned -= set(members)
    return clusters


def _label(docs: list[dict], members: list[int], mat: np.ndarray, vocab: list[str]) -> str:
    centroid = mat[members].mean(axis=0)
    # prefer multi-character, non-numeric terms that appear in several docs of the
    # cluster — a single doc's rare word makes a misleading label
    counts: Counter = Counter()
    editorial: Counter = Counter()      # occurrences OUTSIDE pure filings
    for m in members:
        toks = set(docs[m]["tokens"])
        counts.update(toks)
        if docs[m]["kind"] not in ("filing", "fund_formation"):
            editorial.update(toks)

    def thematic(term: str) -> bool:
        # A theme name is a word humans write about, not an SPV's paperwork.
        # Vehicle codes ('cgf2021'), sponsor names and registered-agent towns
        # appear only inside filings, so require at least one editorial mention.
        if len(term) <= 3 or any(ch.isdigit() for ch in term):
            return False
        if term in VENDOR_NAMES or term in EVENT_WORDS:
            return False          # a vendor is not a market; an event is not a sector
        if counts[term] < max(2, len(members) // 4):
            return False
        return editorial[term] > 0

    ranked = [vocab[i] for i in np.argsort(-centroid) if thematic(vocab[i])]
    if not ranked:   # an all-filings cluster: fall back to frequency, digits excluded
        ranked = [vocab[i] for i in np.argsort(-centroid)
                  if len(vocab[i]) > 3 and not any(ch.isdigit() for ch in vocab[i])
                  and counts[vocab[i]] >= max(2, len(members) // 4)]
    top = ranked[:4] or [vocab[i] for i in np.argsort(-centroid)[:4]]
    if not llm.stubbed():
        name = llm.complete("sector_label",
                            "Name this technology sub-sector in <=5 words based only on the "
                            "provided keywords and titles.",
                            f"keywords: {top}\ntitles: "
                            + "; ".join(docs[m]["text"][:80] for m in members[:6]),
                            tier="classify").strip()
        name = _clean_label(name)
        if name and not name.startswith("[STUB"):
            return name[:60]
    return " / ".join(top)


def _clean_label(name: str) -> str:
    """Models hand back quoted, trailing-punctuation strings. `"Company Acquisition
    Deals"` was stored, quotes and all, and rendered that way on the dashboard."""
    name = (name or "").strip().strip('"\'').strip()
    name = re.sub(r"^(the|a|an)\s+", "", name, flags=re.I).strip(" .:-")
    return name


def _fingerprint(terms: list[str]) -> str:
    """Identity of a cluster across runs: its defining terms, order-independent.

    Without this, every search inserted a fresh row for the same cluster — 526
    rows accumulated, the dashboard showed the same trend two or three times under
    slightly different model-written names, and "trends we are spotting" became a
    log rather than a view."""
    return "|".join(sorted(t.lower() for t in terms[:8]))


def detect_sectors(verbose: bool = True) -> int:
    docs = _corpus()
    if len(docs) < 12:
        if verbose:
            print(f"  sector detection: corpus too thin ({len(docs)} docs) — honest empty state")
        return 0
    mat, vocab = _tfidf(docs)
    sim = mat @ mat.T
    clusters = _greedy_clusters(sim)
    now = datetime.now(timezone.utc)
    n_saved = 0
    for ci, members in enumerate(clusters):
        srcs = {docs[m]["src"] for m in members}
        if len(srcs) < 2:
            continue  # source-diversity gate: one prolific source can't make a trend

        def _dt(iso):
            try:
                d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                return now - timedelta(days=90)

        recent = [m for m in members if _dt(docs[m]["observed_at"]) > now - timedelta(days=30)]
        prior = [m for m in members if now - timedelta(days=60) < _dt(docs[m]["observed_at"])
                 <= now - timedelta(days=30)]
        def is_consensus(m: int) -> bool:
            # mainstream coverage, or an HN story that hit broad attention (real points)
            return docs[m]["src"] in CONSENSUS_SOURCES or docs[m]["points"] >= 50

        tech_recent = sum(1 for m in recent if docs[m]["src"] in TECH_SOURCES
                          and not is_consensus(m))
        consensus = sum(1 for m in members if is_consensus(m))
        velocity = tech_recent
        ratio = velocity / (consensus + 1)
        decelerating = len(recent) < len(prior)
        contrarian = consensus >= 3 and decelerating
        label = _label(docs, members, mat, vocab)
        evidence = [{"signal_id": docs[m]["id"], "url": docs[m]["url"],
                     "src": docs[m]["src"], "title": docs[m]["text"][:90]}
                    for m in members[:8]]
        # brief §2(b) trend inputs: talent flow = founder moves + frontier-lab
        # mentions inside this cluster (capital and coverage are already counted)
        talent = sum(1 for m in members
                     if docs[m]["kind"] in ("founder_move", "hiring")
                     or docs[m].get("frontier_lab"))
        # "Signal running ahead of consensus" is only meaningful if consensus was
        # measured. With zero mainstream documents the ratio collapses to raw
        # volume, and the top of the board fills with whatever was noisiest this
        # week. Such a cluster is kept but reported as unmeasured, not ranked as
        # an emerging sector.
        consensus_measured = consensus > 0
        # the cluster's defining terms, kept for auditability and for sourcing
        centroid = mat[members].mean(axis=0)
        terms = [vocab[i] for i in np.argsort(-centroid)[:12] if len(vocab[i]) > 3]
        # §2(b) "…and then go find the best companies in them"
        found = source_inside_cluster(terms, members, docs)
        thesis_md = ("Contrarian: heavy coverage with decelerating technical signal."
                     if contrarian else
                     "Emerging: technical signal running ahead of mainstream coverage."
                     if consensus_measured else
                     "Technical activity clustered here, but no mainstream coverage was "
                     "found to compare it against — this is volume, not yet a lead "
                     "indicator.")
        row = {"label": label, "cluster_id": ci, "signal_velocity": velocity,
               "consensus_volume": consensus,
               # unmeasured consensus must not out-rank a real lead
               "ratio": round(ratio, 3) if consensus_measured else 0.0,
               "source_diversity": len(srcs), "evidence_json": json.dumps(evidence),
               "thesis_md": thesis_md,
               "detected_at": db.now_iso(), "is_contrarian": 1 if contrarian else 0,
               "companies_json": json.dumps(found), "talent_flow": talent,
               "terms_json": json.dumps(terms)}
        # One row per cluster identity: update it in place so the table is the
        # current picture rather than an append-only log of every run.
        fp = _fingerprint(terms)
        prior = db.q1("SELECT id FROM sectors_emerging WHERE fingerprint=?", (fp,))
        if prior:
            db.execute("""UPDATE sectors_emerging SET label=?, signal_velocity=?,
                          consensus_volume=?, ratio=?, source_diversity=?, evidence_json=?,
                          thesis_md=?, detected_at=?, is_contrarian=?, companies_json=?,
                          talent_flow=?, terms_json=? WHERE id=?""",
                       (row["label"], row["signal_velocity"], row["consensus_volume"],
                        row["ratio"], row["source_diversity"], row["evidence_json"],
                        row["thesis_md"], row["detected_at"], row["is_contrarian"],
                        row["companies_json"], row["talent_flow"], row["terms_json"],
                        prior["id"]))
        else:
            db.insert("sectors_emerging", {**row, "fingerprint": fp})
        n_saved += 1
    if verbose and n_saved == 0:
        print(f"  sector detection: {len(clusters)} raw cluster(s), none passed the"
              " source-diversity gate — honest empty state (corpus grows with arXiv/RSS)")
    if verbose:
        rows = db.q("SELECT label, ratio, is_contrarian FROM sectors_emerging"
                    " ORDER BY id DESC LIMIT ?", (n_saved,))
        for r in rows:
            tag = " [CONTRARIAN]" if r["is_contrarian"] else ""
            print(f"  cluster: {r['label'][:50]:52s} signal/consensus={r['ratio']}{tag}")
    return n_saved


# How many of a cluster's defining terms a company must share before it is
# claimed as a member of that sector. Three is strict enough to stop coincidence
# and loose enough to survive different wording.
MIN_TERM_OVERLAP = 3


def source_inside_cluster(terms: list[str], members: list[int],
                          docs: list[dict], limit: int = 5) -> list[dict]:
    """Brief §2(b): having detected an emerging cluster, go find the best
    companies *in it*.

    Two evidence paths, both real:
      1. companies whose own signals are in the cluster (direct membership);
      2. pipeline companies whose text matches the cluster's defining terms.
    Ranked by cohort percentile, so "best" means the same thing it means
    everywhere else in the system — position within a (sector, stage) cohort.
    """
    stem_terms = {_stem(t) for t in terms}
    direct: dict[int, int] = {}
    for m in members:
        cid = docs[m].get("company_id")
        if cid:
            direct[cid] = direct.get(cid, 0) + 1

    scored: list[dict] = []
    for c in db.q("""SELECT c.id, c.name, c.sector, c.sub_sector, c.stage, c.market_rank,
                            s.percentile, s.cohort_size,
                            COALESCE(s.human_override, s.recommendation) rec
                     FROM companies c LEFT JOIN scores s ON s.company_id=c.id
                       AND s.id=(SELECT id FROM scores WHERE company_id=c.id
                                 ORDER BY scored_at DESC, id DESC LIMIT 1)
                     WHERE c.is_synthetic=0
                     AND c.status IN ('hot','watchlist','pipeline')"""):
        text = " ".join(filter(None, [c["name"], c["sector"], c["sub_sector"]]))
        for s in db.q("SELECT payload_json FROM signals WHERE company_id=? LIMIT 6", (c["id"],)):
            text += " " + s["payload_json"][:300]
        overlap = len(stem_terms & _stems(text))
        in_cluster = direct.get(c["id"], 0)
        # A single shared stem is not membership. One term of overlap put a
        # robotics company under a Cloudflare AI cluster, which is the kind of
        # association that makes a partner distrust the whole panel. Direct
        # membership (the company's own signal is IN the cluster) still counts on
        # its own, because that is evidence rather than word overlap.
        if not in_cluster and overlap < MIN_TERM_OVERLAP:
            continue
        scored.append({
            "company_id": c["id"], "company": c["name"],
            "sector": c["sub_sector"] or c["sector"],
            "stage": c["stage"] or "unknown",
            "percentile": c["percentile"], "cohort_size": c["cohort_size"],
            "recommendation": c["rec"],
            "evidence": ("signal inside the cluster" if in_cluster
                         else f"matches {overlap} cluster term(s)"),
            "_rank": (in_cluster * 3 + overlap) * (1 + (c["percentile"] or 50) / 100),
        })
    scored.sort(key=lambda x: -x["_rank"])
    for s in scored:
        s.pop("_rank", None)
    return scored[:limit]


def _stem(t: str) -> str:
    """Crude stemmer so 'robots' / 'robotic' / 'robotics' collide. Deliberately
    simple — a real one is a production concern, not a correctness one here."""
    for suf in ("ications", "ication", "ities", "ing", "ics", "ies", "ical", "ed", "es", "s"):
        if len(t) - len(suf) >= 4 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def _stems(text: str) -> set[str]:
    return {_stem(t) for t in _tokens(text)}


def scan_thesis(prose: str, limit: int = 10) -> list[dict]:
    """Component 15 — partner describes a thesis in prose → ranked companies."""
    q_tokens = _stems(prose)
    # theme keywords for the matched theme widen recall beyond literal wording
    key, _label = None, None
    try:
        from .filters import match_theme
        key, _label = match_theme(prose)
        if key:
            for t in thesis()["themes"]:
                if t["key"] == key:
                    q_tokens |= _stems(" ".join(t["keywords"]))
                    break
    except Exception:  # noqa: BLE001
        pass
    out = []
    for c in db.q("""SELECT c.*, s.percentile FROM companies c
                     LEFT JOIN scores s ON s.company_id=c.id AND s.id=(
                       SELECT id FROM scores WHERE company_id=c.id
                       ORDER BY scored_at DESC, id DESC LIMIT 1)
                     WHERE c.is_synthetic=0 AND c.status IN ('hot','watchlist','pipeline')"""):
        text = " ".join(filter(None, [c["name"], c["description"], c["sector"],
                                      c["sub_sector"]]))
        sigs = db.q("SELECT payload_json FROM signals WHERE company_id=? LIMIT 5", (c["id"],))
        text += " " + " ".join(s["payload_json"][:300] for s in sigs)
        matched = q_tokens & _stems(text)
        overlap = len(matched)
        if key and c["sector"] == key:
            overlap += 2          # same thesis theme is strong evidence
        if overlap:
            out.append({"company": c["name"], "sector": c["sub_sector"] or c["sector"],
                        "overlap": overlap, "matched_terms": sorted(matched)[:6],
                        "percentile": c["percentile"],
                        "relevance": overlap * (1 + (c["percentile"] or 50) / 100)})
    return sorted(out, key=lambda x: -x["relevance"])[:limit]
