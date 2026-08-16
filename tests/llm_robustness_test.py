"""LLM-robustness tests — the failures found by reading the LIVE deployment's own
diagnostics after run 20, each one a way real model output (or real model spend)
was being thrown away:

* 68 review_queue rows, dominant cause: the 8b model returns `tam.assumptions` as
  a string, the schema wants a list, and the whole judgement — every score, every
  reasoning sentence — is discarded over it.
* 'no parseable JSON found' where the model ECHOED the schema before the answer:
  the greedy brace match swallowed schema + answer together and parsed as nothing,
  so a correct answer sitting in the raw text was rejected.
* every escalation to the strong model timed out at the shared 75s ceiling, fell
  back to 8b, and the answer was then LABELLED as the strong model's — making
  fallback output permanently impersonate the model the cache checks for.
* the alerts step died on Postgres with `syntax error at or near "$2"`:
  `company_id IS ?` is SQLite's null-safe equals and a Postgres syntax error, and
  no sqlite test could ever catch it.

    python tests/llm_robustness_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp()) / "llmr.db"
os.environ["DEAL_ENGINE_DB"] = str(TMP)
os.environ.pop("DATABASE_URL", None)

from engine import db  # noqa: E402
from engine import llm  # noqa: E402
from engine.judge import ScreenedJudgement, TamEstimate  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    db.connect()

    # ---- 1. tam.assumptions arrives as a string ---------------------------
    j = ScreenedJudgement.model_validate({
        "is_venture_relevant": True, "founder_quality": 7.0,
        "tam": {"value_usd": 2e9, "assumptions": "revenue run-rate", "confidence": "low"}})
    check("THE LIVE BUG: a string assumption no longer kills the whole judgement",
          j.tam is not None and j.tam.assumptions == ["revenue run-rate"],
          "this exact payload was review_queue row 364 on the deployment")
    j2 = TamEstimate.model_validate(
        {"assumptions": "bottom-up from 40k warehouses; $50k ACV\n10% penetration"})
    check("semicolons and newlines split into separate assumptions",
          j2.assumptions == ["bottom-up from 40k warehouses", "$50k ACV",
                             "10% penetration"], str(j2.assumptions))
    check("a real list still passes through untouched",
          TamEstimate.model_validate({"assumptions": ["a", "b"]}).assumptions == ["a", "b"],
          "")
    check("null stays null — coercion must not invent an empty-list answer",
          TamEstimate.model_validate({"assumptions": None}).assumptions is None
          and TamEstimate.model_validate({"assumptions": "  "}).assumptions is None, "")

    # ---- 2. schema echo before the answer ---------------------------------
    schema_echo = (
        'Here is the schema I will use:\n'
        '{"$defs": {"TamEstimate": {"properties": {"value_usd": {"type": "number"}}}},'
        ' "properties": {"moat": {"type": "number"}}, "required": []}\n'
        'And my answer:\n'
        '{"is_venture_relevant": true, "moat": 6.5, "thesis_narrative": "cited [S:3]"}')
    out = llm._extract_json(schema_echo)
    check("THE LIVE BUG: an answer after a schema echo is recovered, not rejected",
          out == {"is_venture_relevant": True, "moat": 6.5,
                  "thesis_narrative": "cited [S:3]"}, str(out))
    check("a schema echo alone is still a failure, never returned as an answer",
          llm._extract_json('{"$defs": {}, "properties": {"x": 1}, "required": []}') is None,
          "")
    check("clean JSON still parses exactly as before",
          llm._extract_json('```json\n{"moat": 4.0}\n```') == {"moat": 4.0}, "")
    check("plain prose with no JSON is still None",
          llm._extract_json("I cannot answer this.") is None, "")

    # ---- 3. strong model gets its own timeout, nobody else ----------------
    CONFIG = {"provider": {"api_key_env": "FAKE", "base_url": "x"},
              "tiers": {"score": "fast-8b"}, "strong_model": "strong-big",
              "limits": {"request_timeout_seconds": 75,
                         "strong_model_timeout_seconds": 150}}
    llm.models_config = lambda: CONFIG    # noqa: E731
    check("the strong model gets the longer ceiling",
          llm._strong_timeout_for("strong-big") == 150.0, "")
    check("the fast tier keeps the default ceiling",
          llm._strong_timeout_for("fast-8b") is None, "")
    CONFIG["limits"]["strong_model_timeout_seconds"] = 60
    check("a strong timeout SHORTER than the default is ignored, not honoured",
          llm._strong_timeout_for("strong-big") is None,
          "the setting exists to extend patience, never to cut it")

    # ---- 3b. the empty-answer bug: reasoning model runs out of budget ------
    # Live failure shape: inkling's 300+ score calls returned EMPTY content (its
    # whole 900-token budget went to reasoning), each was logged as SUCCESS, and
    # coverage froze at 22 while every Deep Dive brief read [STUB].
    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)

    class _Usage:
        prompt_tokens, completion_tokens = 100, 900

    class _FakeResp:
        def __init__(self, content):
            self.choices = [_Choice(content)]
            self.usage = _Usage()

    class _FakeClient:
        def __init__(self, script):
            self.script, self.calls = script, []

        def with_options(self, **kw):
            return self

        @property
        def chat(self):
            outer = self

            class _Comp:
                class completions:
                    @staticmethod
                    def create(**kw):
                        outer.calls.append({"model": kw["model"],
                                            "max_tokens": kw["max_tokens"]})
                        return _FakeResp(outer.script.pop(0))
            return _Comp
    CONFIG2 = {"provider": {"api_key_env": "FAKE_LLM_KEY", "base_url": "x"},
               "tiers": {"score": "fast-8b", "classify": "fast-8b",
                         "brief": "fast-8b", "chat": "fast-8b"},
               "fallback_model": "fast-8b", "strong_model": "strong-big",
               "generation": {"temperature": 1.0, "top_p": 0.95},
               "max_tokens_by_stage": {"score": 900},
               "limits": {"request_timeout_seconds": 75,
                          "strong_model_timeout_seconds": 150,
                          "strong_model_max_tokens": 4096}}
    os.environ["FAKE_LLM_KEY"] = "test-key"
    llm.models_config = lambda: CONFIG2   # noqa: E731
    llm.MIN_INTERVAL_S = 0.0

    fake = _FakeClient(["a real answer"])
    llm._client = None
    llm._get_client = lambda: fake        # noqa: E731
    out = llm._raw_complete("strong-big", "score", "sys", "user")
    check("THE LIVE BUG: the strong model gets its own token budget, not the 900 cap",
          fake.calls[0]["max_tokens"] == 4096 and out == "a real answer",
          f"max_tokens={fake.calls[0]['max_tokens']}")

    fake = _FakeClient(["8b answer"])
    llm._get_client = lambda: fake        # noqa: E731
    llm._raw_complete("fast-8b", "score", "sys", "user")
    check("the fast tier keeps the measured 900-token stage cap",
          fake.calls[0]["max_tokens"] == 900, f"max_tokens={fake.calls[0]['max_tokens']}")

    fake = _FakeClient(["", "fallback answer"])   # strong empty -> 8b answers
    llm._get_client = lambda: fake        # noqa: E731
    out = llm._raw_complete("strong-big", "score", "sys", "user")
    check("THE LIVE BUG: an empty answer is NOT success — it falls back to the 8b",
          out == "fallback answer"
          and [c["model"] for c in fake.calls] == ["strong-big", "fast-8b"]
          and llm.last_model_used() == "fast-8b",
          f"calls={[c['model'] for c in fake.calls]}")

    fake = _FakeClient(["", "   "])               # both empty -> loud stub + hint
    llm._get_client = lambda: fake        # noqa: E731
    out = llm._raw_complete("strong-big", "score", "sys", "user")
    check("empty from both models -> a loud stub naming the real cause",
          llm.is_stub(out) and "empty answer" in out
          and "strong_model_max_tokens" in (llm.last_error() or {}).get("hint", ""),
          out[:60])

    # ---- 4. alerts rate-limit query works on both dialects ----------------
    from outputs.alerts import _fire
    fired = _fire("test_rule", None, {"investor": "Test Fund"}, verbose=False)
    check("an investor-level alert (company_id NULL) fires without IS ?",
          fired is True, "this line was a Postgres syntax error on the deployment")
    check("...and is rate-limited on repeat, so NULL still matches NULL",
          _fire("test_rule", None, {"investor": "Test Fund"}, verbose=False) is False, "")
    src = db.insert("sources", {"name": "t", "adapter": "rss_news", "interval_minutes": 60,
                                "requires_license": 0, "health": "ok"})
    cid = db.insert("companies", {"name": "Alert Co", "status": "hot", "is_synthetic": 0,
                                  "created_at": db.now_iso()})
    check("a company-level alert fires and rate-limits by equality",
          _fire("test_rule2", cid, {"x": 1}, verbose=False) is True
          and _fire("test_rule2", cid, {"x": 2}, verbose=False) is False, f"src={src}")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"LLM ROBUSTNESS: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
