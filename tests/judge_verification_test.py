"""AI-assessment verification tests — the three ways a model verdict could reach a
partner (or delete a company) without anything having checked it.

1. Deep Dive candidates were judged by the fast model and only escalated when the
   answer came back EMPTY. Empty is the failure that announces itself; a fluent,
   confident, wrong assessment is the one that gets acted on, and escalation never
   fired for it. The companies a partner will actually act on are now judged on the
   strong model FIRST.
2. `is_venture_relevant: false` is the only model output in this system that removes
   a company from the pipeline — the single irreversible unverified model action.
   It now requires two models to agree.
3. The judgement cache keyed on `count:max_id` over signals, which only answers "did
   a new signal arrive?". Founders read out of a filing, a profile written from the
   company's own site, a corrected sector — all change the prompt and none move that
   key, so a company kept the judgement made when its prompt said nothing about its
   team, and looked freshly assessed while doing so.

Every model call here is a scripted fake: these are tests of the ROUTING and the
CONSEQUENCES, which is where the bugs were, and a real provider would make them
neither deterministic nor free.

    python tests/judge_verification_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp()) / "judge.db"
os.environ["DEAL_ENGINE_DB"] = str(TMP)
os.environ.pop("DATABASE_URL", None)
os.environ["JUDGE_TOP_N"] = "10"

from engine import db  # noqa: E402
from engine import judge  # noqa: E402
from engine import llm  # noqa: E402

FAST = "fast/model-8b"
STRONG = "strong/model-big"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- fake models

CALLS: list[dict] = []
SCRIPT: list[dict | None] = []
CONFIG = {"provider": {"api_key_env": "FAKE_KEY"},
          "tiers": {"classify": FAST, "score": FAST, "brief": FAST, "chat": FAST},
          "fallback_model": FAST, "strong_model": STRONG,
          "limits": {"request_timeout_seconds": 75}}

GOOD = {"is_venture_relevant": True, "founder_quality": 8.0,
        "founder_reasoning": "two prior exits [S:1]", "moat": 7.0,
        "moat_reasoning": "proprietary dataset [S:1]", "meta_thesis_fit": 8.0,
        "thesis_narrative": "strong seed-stage robotics team [S:1]"}
REJECT = {"is_venture_relevant": False, "screening_reason": "looks like an SPV"}
EMPTY = {"is_venture_relevant": True}


PLAN: dict[str, list] = {}


def _company_of(user: str) -> str:
    """`_context()` always opens with 'Company: NAME | sector: …' — which lets the
    fake answer per company. A single shared queue silently misaligns as soon as one
    company makes two calls and another makes one, and then the test is measuring
    the queue rather than the routing."""
    head = user.split("\n", 1)[0]
    return head[len("Company: "):].split(" | ")[0] if head.startswith("Company: ") else ""


def fake_complete_json(stage, system, user, schema_model, tier=None, model_override=None):
    name = _company_of(user)
    CALLS.append({"company": name, "model": model_override or CONFIG["tiers"]["score"],
                  "override": model_override, "tier": tier})
    queue = PLAN.get(name, SCRIPT)
    payload = queue.pop(0) if queue else None
    return None if payload is None else schema_model.model_validate(payload)


def script(*payloads) -> None:
    """Answers for whichever company is asked next (single-company tests)."""
    CALLS.clear()
    SCRIPT.clear()
    PLAN.clear()
    SCRIPT.extend(payloads)


def plan_for(name: str, *payloads) -> None:
    """Answers for ONE named company; every other company gets nothing back."""
    PLAN[name] = list(payloads)


def models_used() -> list[str]:
    return [c["model"] for c in CALLS]


def install_fakes() -> None:
    judge.models_config = lambda: CONFIG          # noqa: E731
    judge.llm.complete_json = fake_complete_json
    judge.llm.stubbed = lambda: False             # noqa: E731
    judge.llm.api_key_env_name = lambda: "FAKE_KEY"   # noqa: E731


# ---------------------------------------------------------------------- seed

def seed_company(name: str, status: str = "pipeline") -> tuple[int, int]:
    src = db.q1("SELECT id FROM sources WHERE name='test_src'")
    src_id = src["id"] if src else db.insert(
        "sources", {"name": "test_src", "adapter": "rss_news", "interval_minutes": 60,
                    "requires_license": 0, "health": "ok"})
    cid = db.insert("companies", {
        "name": name, "domain": f"{name.lower().replace(' ', '')}.com", "sector": "robotics",
        "sub_sector": "warehouse robotics", "stage": "seed", "hq": "Boston, MA",
        "description": "Autonomous picking arms.", "status": status, "is_synthetic": 0,
        "created_at": db.now_iso(), "last_signal_at": db.now_iso()})
    sid = db.insert("signals", {
        "source_id": src_id, "kind": "funding_event", "observed_at": "2026-08-01T00:00:00Z",
        "fetched_at": db.now_iso(), "fetch_mode": "live",
        "url": f"https://example.com/{cid}", "dedupe_key": f"jv:{cid}", "company_id": cid,
        "payload_json": '{"amount_usd": 12500000, "stage": "seed"}',
        "raw": f"{name} raises $12.5M seed."})
    return cid, sid


def store_judgement(company_id: int, judged: dict, recommendation: str = "Watch",
                    human_override: str | None = None) -> None:
    import json
    db.insert("scores", {
        "company_id": company_id, "composite": 0.5, "percentile": 60.0,
        "cohort_key": "robotics|seed", "cohort_size": 5, "cohort_low_confidence": 1,
        "features_json": json.dumps({"computed": {}, "judged": judged}),
        "recommendation": recommendation, "human_override": human_override,
        "model_version": judged.get("model") or "computed-v1", "prompt_version": "score-p1",
        "scored_at": db.now_iso()})


def status_of(company_id: int) -> str:
    return db.q1("SELECT status FROM companies WHERE id=?", (company_id,))["status"]


# ---------------------------------------------------------------------- tests

def main() -> int:
    db.connect()
    install_fakes()

    # ==== 3. evidence fingerprint =========================================
    cid, sid = seed_company("Helion Substrate")
    fp0 = judge._signal_fingerprint(cid)
    check("fingerprint is versioned, so a stale key can never be mistaken for a fresh one",
          fp0.startswith("ctx1:"), fp0)
    check("fingerprint is stable when nothing changed",
          judge._signal_fingerprint(cid) == fp0, "")

    old0 = judge._legacy_signal_fingerprint(cid)
    db.insert("founders", {"company_id": cid, "name": "Priya Raman", "prior_exits": 1,
                           "frontier_lab_alum": 0})
    fp_founder = judge._signal_fingerprint(cid)
    check("THE BUG: reading founders out of a filing changes the fingerprint",
          fp_founder != fp0,
          "founder quality is the assignment's first criterion — the prompt just changed")
    check("...and the old count:max_id key could not see it at all",
          judge._legacy_signal_fingerprint(cid) == old0,
          "which is why a company kept the judgement made before it had a team")

    db.execute("UPDATE companies SET sub_sector='cold-chain robotics' WHERE id=?", (cid,))
    fp_sector = judge._signal_fingerprint(cid)
    check("a corrected sector changes the fingerprint too (it is in the prompt)",
          fp_sector != fp_founder, "")

    db.execute("UPDATE signals SET payload_json=? WHERE id=?",
               ('{"amount_usd": 40000000, "stage": "series-a"}', sid))
    check("an in-place signal edit changes the fingerprint",
          judge._signal_fingerprint(cid) != fp_sector
          and judge._legacy_signal_fingerprint(cid) == old0,
          "$12.5M seed rewritten to a $40M series-A, same row count, same max id")

    # legacy grandfathering — an existing judgement must not be thrown away by the
    # deploy that fixes this, or coverage collapses to zero on restart
    legacy = judge._legacy_signal_fingerprint(cid)
    check("a legacy count:max_id fingerprint is still accepted while it matches",
          judge._fingerprint_valid(cid, legacy), legacy)
    db.insert("signals", {
        "source_id": db.q1("SELECT id FROM sources WHERE name='test_src'")["id"],
        "kind": "news", "observed_at": "2026-08-02T00:00:00Z", "fetched_at": db.now_iso(),
        "fetch_mode": "live", "url": "https://example.com/new", "dedupe_key": "jv:new",
        "company_id": cid, "payload_json": "{}", "raw": "more news"})
    check("a legacy fingerprint still expires when a signal arrives",
          not judge._fingerprint_valid(cid, legacy), "")
    check("a fingerprint from another company never validates",
          not judge._fingerprint_valid(cid, "ctx1:0000000000000000"), "")

    # ==== 1. Deep Dive routing ============================================
    dd, _ = seed_company("Prime Candidate", status="hot")
    watch, _ = seed_company("Ordinary Co")
    store_judgement(dd, {"model": FAST}, recommendation="Deep Dive")
    store_judgement(watch, {"model": FAST}, recommendation="Watch")
    cands = judge.deep_dive_candidates()
    check("Deep Dive candidates come from the current recommendation",
          dd in cands and watch not in cands, str(sorted(cands)))

    overridden, _ = seed_company("Partner Pick")
    store_judgement(overridden, {"model": FAST}, recommendation="Pass",
                    human_override="Deep Dive")
    check("a partner's own override outranks the computed call",
          overridden in judge.deep_dive_candidates(), "")

    demoted, _ = seed_company("Demoted Co")
    store_judgement(demoted, {"model": FAST}, recommendation="Deep Dive",
                    human_override="Pass")
    check("a partner overriding Deep Dive DOWN removes it from the strong-model list",
          demoted not in judge.deep_dive_candidates(), "")

    script(GOOD)
    out = judge.assess_company(cid, prefer_strong=True)
    check("THE BUG: a Deep Dive candidate goes to the strong model FIRST, not on empty",
          models_used() == [STRONG] and out["model"] == STRONG, str(models_used()))
    check("...and the judgement says how it was routed",
          "Deep Dive" in (out.get("routed_as") or ""), str(out.get("routed_as")))

    script(GOOD)
    out = judge.assess_company(cid)
    check("an ordinary company still costs exactly one fast call",
          models_used() == [FAST] and out["model"] == FAST, str(models_used()))

    script(EMPTY, GOOD)
    out = judge.assess_company(cid)
    check("escalation-on-empty survives unchanged for everyone else",
          models_used() == [FAST, STRONG] and out["model"] == STRONG, str(models_used()))

    # ==== 2. rejections need two models ===================================
    script(REJECT, REJECT)
    out = judge.assess_company(cid)
    check("a fast-model rejection is put to the strong model",
          models_used() == [FAST, STRONG], str(models_used()))
    check("two models agreeing confirms the rejection",
          out["is_venture_relevant"] is False and out[judge.REJECTION_CONFIRM] is True,
          str(out.get(judge.REJECTION_CONFIRM)))

    script(REJECT, GOOD)
    out = judge.assess_company(cid)
    check("THE BUG: the strong model overturning it keeps the company",
          out["is_venture_relevant"] is True and not out[judge.REJECTION_CONFIRM], str(out))
    check("...and the strong model's own answer becomes the judgement, not a bare veto",
          out["model"] == STRONG and out["founder_quality"] == 8.0, str(out.get("model")))
    check("...with both verdicts kept for the audit trail",
          out["screening_verdicts"] == {FAST: False, STRONG: True},
          str(out.get("screening_verdicts")))

    script(REJECT, None)
    out = judge.assess_company(cid)
    check("a strong model that cannot answer does NOT confirm the rejection",
          out["is_venture_relevant"] is False and out[judge.REJECTION_CONFIRM] is False,
          "unverified must fail safe towards keeping the company")

    script(REJECT)
    out = judge.assess_company(cid, prefer_strong=True)
    check("the strong model's own rejection needs no third opinion",
          models_used() == [STRONG] and out[judge.REJECTION_CONFIRM] is True,
          str(models_used()))

    CONFIG["strong_model"] = FAST                  # nothing to cross-check against
    script(REJECT)
    out = judge.assess_company(cid)
    check("with no distinct strong model, a rejection is never confirmed",
          out[judge.REJECTION_CONFIRM] is False and judge.strong_model() is None,
          "a model cannot cross-check itself")
    CONFIG["strong_model"] = STRONG

    # ==== consequences: what run_judged_scoring actually does =============
    keep, _ = seed_company("Kept Despite Doubt")
    store_judgement(keep, {"is_venture_relevant": False, judge.REJECTION_CONFIRM: True,
                           "screening_confirmed_by": STRONG,
                           "evidence_fingerprint": judge._signal_fingerprint(keep)})
    check("a confirmed rejection is still served from cache",
          (judge._cached_judgement(keep) or {}).get(judge.REJECTION_CONFIRM) is True, "")
    script()
    judge.run_judged_scoring(verbose=False)
    check("a confirmed rejection sets status=filtered", status_of(keep) == "filtered",
          status_of(keep))

    unconf, _ = seed_company("Disputed Co")
    store_judgement(unconf, {"is_venture_relevant": False,
                             "evidence_fingerprint": judge._signal_fingerprint(unconf)})
    check("THE BUG: an UNCONFIRMED stored rejection is refused by the cache",
          judge._cached_judgement(unconf) is None,
          "reusing it would re-apply an unverified delete every single run")

    script()
    plan_for("Disputed Co", REJECT, None)          # fast rejects, strong cannot answer
    judge.run_judged_scoring(verbose=False)
    check("an unconfirmed rejection never changes a company's status",
          status_of(unconf) == "pipeline", status_of(unconf))
    q = db.q("SELECT payload_json FROM review_queue WHERE kind='unconfirmed_rejection'")
    check("...it goes to the review queue for a human instead",
          any(f'"company_id": {unconf}' in r["payload_json"] for r in q), f"{len(q)} row(s)")

    n_before = len(q)
    script()
    plan_for("Disputed Co", REJECT, None)
    judge.run_judged_scoring(verbose=False)
    q2 = db.q("SELECT payload_json FROM review_queue WHERE kind='unconfirmed_rejection'")
    asked_again = [c for c in CALLS if c["company"] == "Disputed Co"]
    check("the same dispute is not re-argued on the same evidence (no queue spam, no spend)",
          len(q2) == n_before and not asked_again,
          f"{n_before} -> {len(q2)} row(s), {len(asked_again)} model call(s)")

    db.insert("signals", {
        "source_id": db.q1("SELECT id FROM sources WHERE name='test_src'")["id"],
        "kind": "news", "observed_at": "2026-08-09T00:00:00Z", "fetched_at": db.now_iso(),
        "fetch_mode": "live", "url": "https://example.com/reopen", "dedupe_key": "jv:reopen",
        "company_id": unconf, "payload_json": "{}", "raw": "new evidence"})
    check("new evidence reopens the dispute", not judge._dispute_is_open(unconf),
          "the fingerprint moved, so the old argument no longer applies")

    # ==== cache: a Deep Dive company cannot keep a fast-model judgement ====
    dd2, _ = seed_company("Promoted Co")
    fast_judgement = {**GOOD, "model": FAST,
                      "evidence_fingerprint": judge._signal_fingerprint(dd2)}
    store_judgement(dd2, fast_judgement, recommendation="Deep Dive")
    check("an unchanged judgement is still reused for an ordinary company",
          judge._cached_judgement(dd2) is not None, "")
    check("THE BUG: but a Deep Dive company's fast-model judgement is re-judged",
          judge._cached_judgement(dd2, require_model=STRONG) is None,
          "otherwise 'always strong' would only apply to companies judged after promotion")
    strong_judgement = {**fast_judgement, "model": STRONG}
    store_judgement(dd2, strong_judgement, recommendation="Deep Dive")
    check("...and a strong-model judgement is reused, not paid for twice",
          judge._cached_judgement(dd2, require_model=STRONG) is not None, "")

    # ==== the top-pick gap pass: no Deep Dive brief ships stubbed ==========
    # Scenario from the live box: 250 YC companies reshuffle the ranking; today's
    # Deep Dive picks were not candidates last run, so the main pass never
    # prioritised them and their briefs — the ones a partner opens FIRST — read
    # [STUB]. judge_deep_dive_gaps runs after score_all and closes exactly that.
    newpick, _ = seed_company("Fresh Top Pick", status="hot")
    store_judgement(newpick, {"model": None}, recommendation="Deep Dive")
    db.execute("""UPDATE scores SET features_json='{"computed": {}, "judged": null}'
                  WHERE company_id=?""", (newpick,))
    oldpick, _ = seed_company("Settled Pick", status="hot")
    store_judgement(oldpick, {**GOOD, "model": STRONG,
                              "evidence_fingerprint": judge._signal_fingerprint(oldpick)},
                    recommendation="Deep Dive")
    script()
    plan_for("Fresh Top Pick", GOOD)
    extra = judge.judge_deep_dive_gaps(verbose=False)
    check("THE GAP: a Deep Dive pick that emerged THIS run is assessed post-scoring",
          newpick in extra and extra[newpick]["model"] == STRONG,
          str({k: v.get("model") for k, v in extra.items()}))
    check("...on the strong model, with the fingerprint stamped",
          str(extra[newpick].get("evidence_fingerprint", "")).startswith("ctx1:"), "")
    check("...while an already-judged pick costs nothing",
          oldpick not in extra
          and not [c for c in CALLS if c["company"] == "Settled Pick"],
          "steady state this pass is free")
    check("...and a non-Deep-Dive company is not touched by this pass",
          all(judge.db.q1("SELECT recommendation FROM scores WHERE company_id=?"
                          " ORDER BY id DESC LIMIT 1", (k,))["recommendation"]
              == "Deep Dive" for k in extra), "")

    # ==== the deploy can prove it took ====================================
    from engine import version
    feats = version.features()
    check("/api/version proves all three fixes are in the running build",
          feats.get("strong_model_for_deep_dive") and feats.get("verified_rejections")
          and feats.get("context_evidence_fingerprint"),
          str({k: v for k, v in feats.items() if k in
               ("strong_model_for_deep_dive", "verified_rejections",
                "context_evidence_fingerprint")}))

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"JUDGE VERIFICATION: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
