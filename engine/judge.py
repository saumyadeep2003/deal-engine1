"""Judged scoring — model judgment on filter survivors only, tier-routed.

Funnel: deterministic filter (free) → cheap classifier (flash tier) →
structured judged scoring (mid tier) → briefs (flagship tier, ~8/day).
Models never do arithmetic; they return labelled judgments with reasoning,
or null. Stub mode returns None — judgment fields render as [STUB], and
composites fall back to computed-only.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
from typing import Optional

from pydantic import BaseModel, field_validator

from . import db, llm
from .config import models_config, thesis


# How many of the top-ranked survivors get the full AI assessment. Everything
# below the cut still gets complete deterministic scoring, ranking and provenance
# — the model is the expensive part, not the pipeline.
JUDGE_TOP_N = int(os.environ.get("JUDGE_TOP_N", "10"))


class Classification(BaseModel):
    is_venture_relevant: Optional[bool] = None
    reason: Optional[str] = None


class TamEstimate(BaseModel):
    value_usd: Optional[float] = None
    assumptions: Optional[list[str]] = None
    confidence: Optional[str] = None      # low | medium | high

    @field_validator("assumptions", mode="before")
    @classmethod
    def _coerce_assumptions(cls, v):
        """The 8b model routinely returns assumptions as one string ('revenue
        run-rate') instead of a list. That shape mismatch was the single biggest
        killer of judgements on the live deployment — 68 review_queue rows, each
        one a whole assessment discarded (and its model spend with it) over a
        distinction a reader does not care about. An analyst handed 'assumptions:
        revenue run-rate' would read it, not reject the memo. Semicolons and
        newlines split into separate assumptions; a bare number is kept as text."""
        if v is None or isinstance(v, list):
            return v
        if isinstance(v, (str, int, float)):
            parts = [p.strip() for p in re.split(r"[;\n]+", str(v)) if p.strip()]
            return parts or None
        return v


class JudgedScore(BaseModel):
    founder_quality: Optional[float] = None       # 0-10
    founder_reasoning: Optional[str] = None
    moat: Optional[float] = None                  # 0-10
    moat_reasoning: Optional[str] = None
    tam: Optional[TamEstimate] = None
    meta_thesis_fit: Optional[float] = None       # 0-10
    meta_thesis_reasoning: Optional[str] = None
    exit_horizon_years: Optional[float] = None
    thesis_narrative: Optional[str] = None        # the paragraph a partner reads


class ScreenedJudgement(JudgedScore):
    """Screening + judgment in ONE response. Two calls per company meant 50 model
    round-trips per search; the screening question ('is this an operating company
    or a fund/SPV?') is answered from the same context the judgment needs, so
    asking twice bought nothing but latency."""
    is_venture_relevant: Optional[bool] = None
    screening_reason: Optional[str] = None


def _context(company_id: int, max_signals: int = 12) -> str:
    """Real retrieved context only — every line carries its signal id."""
    c = db.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    lines = [f"Company: {c['name']} | sector: {c['sub_sector'] or c['sector'] or 'unknown'}"
             f" | HQ: {c['hq'] or 'unknown'} | stage: {c['stage'] or 'unknown'}"]
    for s in db.q("SELECT id, kind, observed_at, url, payload_json, raw FROM signals"
                  " WHERE company_id=? ORDER BY observed_at DESC LIMIT ?",
                  (company_id, max_signals)):
        p = json.loads(s["payload_json"])
        compact = {k: v for k, v in p.items() if v not in (None, [], "", {})}
        lines.append(f"[S:{s['id']}] {s['kind']} @ {s['observed_at'][:10]}"
                     f" {s['url'] or ''} :: {json.dumps(compact)[:500]}")
    for f in db.q("SELECT name, prior_exits, frontier_lab_alum, notes FROM founders"
                  " WHERE company_id=?", (company_id,)):
        lines.append(f"[founder] {f['name']} prior_exits={f['prior_exits']}"
                     f" frontier_lab={f['frontier_lab_alum']} {f['notes'] or ''}")
    return "\n".join(lines)


def classify_company(company_id: int) -> bool | None:
    """Flash-tier binary gate. Returns None when stubbed (nobody removed)."""
    if llm.stubbed():
        return None
    res = llm.complete_json(
        "classify",
        "You are screening deal-sourcing signals for a deep-tech/AI venture fund. "
        "Decide only whether this entity is a venture-investable operating company "
        "(not a fund, SPV, holding vehicle, public company or hobby project).",
        _context(company_id, max_signals=5), Classification, tier="classify")
    return res.is_venture_relevant if res else None


def judge_company(company_id: int) -> dict | None:
    """Mid-tier structured judgment. Null when stubbed or unparseable."""
    res = llm.complete_json(
        "score",
        "You are a venture analyst. Judge ONLY from the provided signals: founder "
        "quality (prior exits, prior company calibre, frontier-lab background, domain "
        "fit, team completeness), moat/defensibility, TAM (with explicit assumptions "
        "and a confidence level — never a bare number), fit with the meta-thesis "
        f"({thesis()['fund']['meta_thesis']}), plausible exit horizon in years, and a "
        "short thesis narrative saying why this could be one of the best deals "
        "available right now (founder, market, investors already in, traction, entry "
        "valuation). Cite signal ids [S:n] for every factual claim.",
        _context(company_id), JudgedScore, tier="score")
    if res is None:
        return None
    out = res.model_dump()
    out["model"] = models_config()["tiers"]["score"]
    return out


JUDGE_PROMPT = (
    "You are a venture analyst at a deep-tech/AI fund. Do TWO things in one answer.\n"
    "(1) SCREEN: is this a venture-investable operating company — not a fund, SPV, "
    "holding vehicle, public company or hobby project? Set is_venture_relevant and "
    "screening_reason. If it is false, leave every other field null and stop.\n"
    "(2) JUDGE (only if relevant): founder quality (prior exits, prior company calibre, "
    "frontier-lab background, domain fit, team completeness), moat/defensibility, TAM "
    "(with explicit assumptions and a confidence level — never a bare number), fit with "
    "the meta-thesis ({meta}), plausible exit horizon in years, and a short thesis "
    "narrative saying why this could be one of the best deals available right now "
    "(founder, market, investors already in, traction, entry valuation). "
    "Cite signal ids [S:n] for every factual claim. Be concise.\n\n"
    "SCORING RULE — this matters more than filling the form: a score must be earned "
    "by evidence in the context above. If the context says nothing about founders, "
    "return null for founder_quality — do NOT award a number and then write 'no team "
    "information available' next to it. A null with a stated reason is a useful answer; "
    "a confident number with no evidence behind it is worse than saying nothing, because "
    "a partner may act on it. The same rule applies to moat, TAM and meta-thesis fit. "
    "Do not treat identifiers (CIK numbers, filing ids) as evidence of quality."
)


JUDGED_CORE = ("founder_quality", "moat", "meta_thesis_fit", "thesis_narrative")

# Set on every judgement that rejects a company. `run_judged_scoring` refuses to
# act on a rejection that does not carry it — see REJECTION_CONFIRM's docstring.
REJECTION_CONFIRM = "rejection_confirmed"


def _is_empty_judgement(out: dict | None) -> bool:
    """Valid JSON with every meaningful field null is NOT an answer. A small model
    will happily return that, and it renders as 'None/10 — n/a' in a brief: output
    that looks like analysis while saying nothing. Worse than a loud [STUB]."""
    if not out or out.get("is_venture_relevant") is False:
        return False                     # a considered rejection is a real answer
    return all(out.get(k) in (None, "", []) for k in JUDGED_CORE)


def strong_model() -> str | None:
    """The verification/escalation target — or None when it is not distinct from
    the routed model, because a model cannot cross-check itself."""
    cfg = models_config()
    strong = cfg.get("strong_model")
    return strong if strong and strong != cfg["tiers"]["score"] else None


def deep_dive_candidates() -> set[int]:
    """Companies whose CURRENT call is Deep Dive — a partner's own override first,
    then the computed recommendation from the most recent scoring pass.

    Knowingly one run behind: recommendations are written by `score_all`, which
    runs AFTER judging, so a company promoted to Deep Dive in this search is
    routed to the strong model in the next one. The alternative is to predict the
    recommendation from the computed composite before the cohort percentiles that
    define it exist, which would route on a number that is not the number the
    partner reads. One run of lag beats routing on a different quantity — and the
    same deliberate lag already governs Apollo enrichment (BUILD_LOG 74)."""
    rows = db.q("""SELECT s.company_id cid, s.recommendation, s.human_override
                   FROM scores s
                   WHERE s.id=(SELECT id FROM scores s2 WHERE s2.company_id=s.company_id
                               ORDER BY scored_at DESC, id DESC LIMIT 1)""")
    return {r["cid"] for r in rows
            if (r["human_override"] or r["recommendation"]) == "Deep Dive"}


def _ask(model: str | None, system: str, ctx: str) -> dict | None:
    """One screening+judging call. `model=None` uses the tier-routed model."""
    res = (llm.complete_json("score", system, ctx, ScreenedJudgement, model_override=model)
           if model else
           llm.complete_json("score", system, ctx, ScreenedJudgement, tier="score"))
    return res.model_dump() if res is not None else None


def assess_company(company_id: int, prefer_strong: bool = False) -> dict | None:
    """One call: screen + judge. Returns the judged dict, or a dict with
    is_venture_relevant False for a rejected entity, or None when stubbed or
    unparseable.

    Two model-routing rules live here, and both exist because the fast model is
    cheap enough to be wrong at scale:

    * `prefer_strong` (set for Deep Dive candidates) judges on the strong model
      FIRST rather than escalating to it on empty output. Escalation-on-empty only
      catches the failure that announces itself — all-nulls. It never catches a
      confident, fluent, wrong assessment, which is the failure that actually
      reaches a partner, because it is indistinguishable from a right one until
      someone checks. The companies a partner will act on are the ones worth the
      slower model, and there are few of them.
    * A rejection is never taken on one fast model's word. `is_venture_relevant:
      false` is the only model output in this system that removes a company from
      the pipeline, so it is confirmed by the strong model before it counts. Both
      must agree; anything else leaves the company where it is and asks a human.
    """
    system = JUDGE_PROMPT.format(meta=thesis()["fund"]["meta_thesis"])
    ctx = _context(company_id)
    strong = strong_model()
    routed = models_config()["tiers"]["score"]

    primary = strong if (prefer_strong and strong) else None
    out = _ask(primary, system, ctx)
    # llm's silent fallback may have answered on a different model than we routed
    # to; the label must say who actually spoke, or the cache's "was this judged
    # by the strong model?" check is answered by the routing instead of the words.
    model_used = (llm.last_model_used() if out is not None else None) or primary or routed

    # escalate once on an empty answer from the fast model (unchanged behaviour)
    if strong and model_used != strong and (out is None or _is_empty_judgement(out)):
        print(f"  ~ empty judgement from {model_used} — escalating once to {strong}")
        out2 = _ask(strong, system, ctx)
        if out2 is not None and not _is_empty_judgement(out2):
            out = out2
            model_used = llm.last_model_used() or strong

    if out is None or _is_empty_judgement(out):
        return None                      # nothing usable — the brief will say [STUB]

    out["model"] = model_used
    out["routed_as"] = "strong (Deep Dive candidate)" if primary else "routed"

    if out.get("is_venture_relevant") is False:
        out = _screen_rejection(out, system, ctx, model_used, strong)
    return out


def _screen_rejection(out: dict, system: str, ctx: str, model_used: str,
                      strong: str | None) -> dict:
    """Second opinion on the one verdict that costs a company its place.

    The strong model's own rejection needs no second model — it IS the second
    opinion. Everything else does, and when the strong model disagrees, is not
    configured, or cannot answer, the rejection is recorded as unconfirmed and
    the company keeps its status. Erring towards keeping a wrong company in the
    pipeline is recoverable by a partner glancing at it; erring towards dropping
    a right one is not, because nobody ever sees what was dropped.

    When the strong model overturns the rejection it does not merely veto it: its
    own answer becomes the judgement, because that answer is a full assessment of
    a company the fast model was about to delete."""
    if model_used == strong:
        out.update({REJECTION_CONFIRM: True, "screening_confirmed_by": strong,
                    "screening_verdicts": {strong: False}})
        return out
    if not strong:
        out.update({REJECTION_CONFIRM: False, "screening_confirmed_by": None,
                    "screening_unconfirmed_reason":
                        "no strong_model distinct from the routed model is configured, "
                        "so this rejection was never cross-checked"})
        return out

    print(f"  ~ {model_used} says not venture-relevant — confirming with {strong}")
    second = _ask(strong, system, ctx)
    answered_by = (llm.last_model_used() if second is not None else None) or strong
    verdicts = {model_used: False, strong: (second or {}).get("is_venture_relevant")}

    if second is None or answered_by != strong:
        # None: the strong model never answered. answered_by != strong: llm's
        # silent fallback answered in its place — which is the same model class
        # that just rejected the company agreeing with itself, not a second
        # opinion. Either way the rejection stays unconfirmed.
        out.update({REJECTION_CONFIRM: False, "screening_confirmed_by": None,
                    "screening_verdicts": verdicts,
                    "screening_unconfirmed_reason":
                        (f"{strong} did not return a usable answer, so the rejection "
                         "stands unverified") if second is None else
                        (f"{strong} did not answer; the fallback model ({answered_by}) "
                         "answered in its place, which is not an independent second "
                         "opinion on its own rejection")})
        return out

    if second.get("is_venture_relevant") is False:
        out.update({REJECTION_CONFIRM: True, "screening_confirmed_by": strong,
                    "screening_verdicts": verdicts,
                    "screening_reason_strong": second.get("screening_reason")})
        return out

    overturned = (f"{model_used} judged this not venture-relevant"
                  f" ({out.get('screening_reason') or 'no reason given'});"
                  f" {strong} disagreed and the pipeline kept the company")
    if _is_empty_judgement(second):
        # It disagreed but had nothing else to say. The veto still counts — an
        # unconfirmed rejection must not filter — but there is no assessment to
        # publish, so the record stays honest about that.
        out.update({REJECTION_CONFIRM: False, "screening_confirmed_by": None,
                    "screening_verdicts": verdicts,
                    "screening_unconfirmed_reason": overturned})
        return out
    second.update({"model": strong, "routed_as": f"strong (overturned {model_used})",
                   REJECTION_CONFIRM: False, "screening_verdicts": verdicts,
                   "screening_overturned": overturned})
    return second


# ---- reuse: a company with no new signals cannot have a new judgment ---------

FINGERPRINT_VERSION = "ctx1"


def _signal_fingerprint(company_id: int) -> str:
    """Identity of the evidence the model is actually shown — a hash of the exact
    context string `_context()` builds.

    It used to be `count:max_id` over the signals table, which answered a narrower
    question than the one being asked: has a new signal ARRIVED? Everything else
    that changes an assessment moved underneath it silently. Founders synced out of
    a filing, a profile written from the company's own website, a sector or stage
    corrected, an enrichment field filled, a signal payload rewritten in place —
    all of it enters the prompt through `_context`, none of it moves a row count or
    a maximum id. The consequence was the worst kind: a company whose founders were
    finally read kept the judgement made when the prompt said nothing about its
    team, and looked freshly assessed while doing so.

    Hashing the context makes the cache key the thing the cache is actually about.
    It costs one extra context build per company per run — pure local reads, next
    to nothing beside a model call — and it is stable, because `_context` contains
    no clock and no random ordering."""
    return (f"{FINGERPRINT_VERSION}:"
            + hashlib.sha256(_context(company_id).encode("utf-8")).hexdigest()[:16])


def _legacy_signal_fingerprint(company_id: int) -> str:
    """The pre-`ctx1` key, kept only so that existing judgements are not all
    invalidated at once by the deploy that fixes this. A judgement stored under the
    old key stays valid while its old key still matches; the next time it is
    re-judged it is written under the new one. Coverage degrades company by
    company as evidence changes, rather than collapsing to zero on restart."""
    r = db.q1("SELECT COUNT(*) n, COALESCE(MAX(id), 0) mx FROM signals WHERE company_id=?",
              (company_id,))
    return f"{r['n']}:{r['mx']}"


def _fingerprint_valid(company_id: int, stored: str | None) -> bool:
    if not stored:
        return False
    if stored.startswith(FINGERPRINT_VERSION + ":"):
        return stored == _signal_fingerprint(company_id)
    return stored == _legacy_signal_fingerprint(company_id)


def _cached_judgement(company_id: int, require_model: str | None = None) -> dict | None:
    """A stored judgement that is still valid, or None — in which case the company
    is re-judged. Three ways to be invalid:

    * the evidence changed (fingerprint mismatch);
    * `require_model` is set — the company is now a Deep Dive candidate — and the
      stored judgement came from a weaker model. Without this, "route Deep Dive
      candidates to the strong model" would apply to whichever companies happened
      to be promoted before they were first judged, and never to the rest;
    * it rejects the company without a confirmed second opinion. Those were written
      before rejections were verified; reusing one re-applies an unverified delete
      every run. Returning None sends it back through the two-model screen instead.
    """
    row = db.q1("""SELECT features_json FROM scores WHERE company_id=?
                   ORDER BY scored_at DESC, id DESC LIMIT 1""", (company_id,))
    if not row or not row["features_json"]:
        return None
    try:
        feats = json.loads(row["features_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    judged = feats.get("judged")
    if not judged or not isinstance(judged, dict):
        return None
    if not _fingerprint_valid(company_id, judged.get("evidence_fingerprint")):
        return None
    if require_model and judged.get("model") != require_model:
        return None
    if judged.get("is_venture_relevant") is False and not judged.get(REJECTION_CONFIRM):
        return None
    return judged


def run_judged_scoring(max_companies: int | None = None, verbose: bool = True,
                       progress_cb=None) -> dict[int, dict]:
    """Judge the top survivors by computed composite. Returns {company_id: judged}.

    progress_cb(i, n, company_name) lets the search runner surface live
    "assessing X (7 of 10)" progress in the dashboard.

    Three things keep this step from dominating a search: only the top N by
    computed composite are judged at all, each takes ONE model call rather than
    two, and a company whose evidence has not changed since its last judgment
    reuses that judgment instead of paying for an identical answer."""
    from .scoring import composite_from_features, computed_features
    cap = max_companies if max_companies is not None else JUDGE_TOP_N
    rows = db.q("SELECT id FROM companies WHERE is_synthetic=0 AND status IN"
                " ('pipeline','hot','watchlist')")
    ranked_all = sorted(((r["id"], composite_from_features(computed_features(r["id"])))
                         for r in rows), key=lambda x: -x[1])

    # The cap used to be applied to the ranking BEFORE checking the cache, which
    # meant the budget was spent on the same top ten every run — and because their
    # judgements were still valid, every one was served from cache. Ten model calls
    # of headroom went unused while a hundred and fifty companies were never judged
    # at all. Coverage was frozen by design, and it looked like a working system.
    #
    # So: reuse every valid judgement (free), then spend the budget on the
    # highest-ranked companies that do NOT have one. Each search now advances
    # coverage by up to `cap` companies, and a company whose evidence changes is
    # re-judged automatically because its fingerprint no longer matches.
    results: dict[int, dict] = {}
    reused = 0
    stub = llm.stubbed()
    # Deep Dive candidates are judged on the strong model, and a stored judgement
    # from a weaker one does not satisfy that — so they are re-judged even though
    # their evidence is unchanged. That is the point: the budget belongs to the
    # companies a partner is going to act on.
    deep = deep_dive_candidates() if not stub else set()
    strong = strong_model()
    pending: list[tuple[int, float]] = []
    if not stub:
        for cid, score in ranked_all:
            cached = _cached_judgement(cid, require_model=strong if cid in deep else None)
            if cached is None:
                # A rejection the two models argued over is not re-argued on the same
                # evidence: it is already in the review queue, it costs two model calls
                # to reproduce, and those calls belong to companies nobody has judged
                # at all. New evidence reopens it, because the fingerprint moves.
                if not _dispute_is_open(cid):
                    pending.append((cid, score))
                continue
            reused += 1
            if cached.get("is_venture_relevant") is False:
                # only ever reached with a confirmed rejection — _cached_judgement
                # refuses to serve an unconfirmed one
                db.execute("UPDATE companies SET status='filtered' WHERE id=?", (cid,))
                continue
            results[cid] = cached
    # highest-ranked first, but a Deep Dive candidate is never queued behind a
    # company nobody has looked at: it is the one whose assessment gets acted on.
    pending.sort(key=lambda p: (0 if p[0] in deep else 1, -p[1]))
    ranked = pending[:cap]
    remaining = max(0, len(pending) - len(ranked))
    strong_calls, unconfirmed, filtered = 0, 0, 0
    for i, (cid, _) in enumerate(ranked, start=1):
        if stub:
            break
        if progress_cb:
            row = db.q1("SELECT name FROM companies WHERE id=?", (cid,))
            try:
                progress_cb(i, len(ranked), row["name"] if row else f"#{cid}")
            except Exception:  # noqa: BLE001 — progress must never break judging
                pass
        # every id here is already known to lack a valid judgement
        judged = assess_company(cid, prefer_strong=cid in deep)
        if not judged:
            continue
        judged["evidence_fingerprint"] = _signal_fingerprint(cid)
        if judged.get("model") == strong:
            strong_calls += 1
        if judged.get("is_venture_relevant") is False:
            if judged.get(REJECTION_CONFIRM):
                db.execute("UPDATE companies SET status='filtered' WHERE id=?", (cid,))
                filtered += 1
                continue
            # An unverified model rejection is the only irreversible unverified
            # action this system can take, so it does not take it. The company
            # keeps its status and a human is told why the two models disagreed.
            unconfirmed += 1
            _flag_unconfirmed_rejection(cid, judged)
            continue
        results[cid] = judged
    if verbose:
        if stub:
            mode = (f"STUB (no {llm.api_key_env_name()} — computed-only scoring, "
                    "judgment fields read [STUB])")
        else:
            covered = len(results)
            mode = (f"{len(ranked)} newly judged ({strong_calls} on {strong or 'n/a'}), "
                    f"{reused} reused unchanged — "
                    f"{covered}/{len(ranked_all)} survivors now carry a judgement"
                    + (f"; {remaining} still waiting (raise JUDGE_TOP_N to go faster)"
                       if remaining else "; full coverage")
                    + (f"; {filtered} rejection(s) confirmed by two models" if filtered else "")
                    + (f"; {unconfirmed} unconfirmed rejection(s) kept in the pipeline for "
                       "review" if unconfirmed else ""))
        print(f"  judged scoring: {mode}")
    return results


UNCONFIRMED_KIND = "unconfirmed_rejection"


def _dispute_is_open(company_id: int) -> bool:
    """True when this company already has a recorded rejection disagreement against
    the evidence it has right now. Fingerprinting the dispute is what makes it
    expire on its own: the moment a new signal, founder or profile changes the
    prompt, the old argument no longer applies and the company is screened again."""
    rows = db.q("SELECT payload_json FROM review_queue WHERE kind=?"
                " ORDER BY id DESC LIMIT 200", (UNCONFIRMED_KIND,))
    fp = None
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if p.get("company_id") != company_id:
            continue
        if fp is None:
            fp = _signal_fingerprint(company_id)
        if p.get("evidence_fingerprint") == fp:
            return True
    return False


def _flag_unconfirmed_rejection(company_id: int, judged: dict) -> None:
    """One review_queue row per disagreement, not one per run: re-flagging the same
    argument every search turns the queue into a log nobody reads."""
    row = db.q1("SELECT name FROM companies WHERE id=?", (company_id,))
    db.insert("review_queue", {"kind": UNCONFIRMED_KIND, "payload_json": json.dumps({
        "company_id": company_id, "name": row["name"] if row else None,
        "evidence_fingerprint": judged.get("evidence_fingerprint"),
        "verdicts": judged.get("screening_verdicts"),
        "fast_model_reason": judged.get("screening_reason"),
        "why_unconfirmed": (judged.get("screening_unconfirmed_reason")
                            or judged.get("screening_overturned")),
        "action_taken": "none — company kept in the pipeline, awaiting a human call",
    }), "created_at": db.now_iso()})
