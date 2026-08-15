"""LLM layer — z.ai GLM via the OpenAI-compatible endpoint.

- Tier routing (classify / score / brief / chat) from config/models.yaml.
- Token spend logged per funnel stage (llm_usage) so the funnel is demonstrable.
- No key → loud stub: '[STUB: no API key — judgment unavailable]'. Never
  plausible analysis. Stubbed calls are flagged and excluded from composites.
- Structured output treated as unreliable: strip fences → parse → Pydantic
  validate → retry once with the error appended → null + review_queue flag.
- Extraction, never recall: every system prompt pins the model to provided
  context and requires null when the answer is absent.
"""
from __future__ import annotations
import json
import os
import re

from . import db
from .config import models_config

STUB_TEXT = "[STUB: no API key — judgment unavailable]"
# The stub must state the REAL reason. A brief that says "no API key" on a
# deployment that has one sends the reader hunting for the wrong problem.
STUB_PROVIDER_DOWN = "[STUB: AI provider did not answer in time — judgment unavailable]"
STUB_CIRCUIT = ("[STUB: AI provider failing repeatedly — judgment skipped for the rest "
                "of this search]")


def is_stub(text: str | None) -> bool:
    return bool(text) and str(text).startswith("[STUB")


def api_key_env_name() -> str:
    """The env var the configured provider reads — for honest UI/test messages."""
    return models_config()["provider"]["api_key_env"]

EXTRACTION_RULES = (
    "Answer only from the provided context. If the answer is not present, "
    "return null. Do not use prior knowledge about this company. Do not perform "
    "arithmetic — report numbers only as they appear, with their signal ids."
)

_client = None


def api_key() -> str | None:
    return os.environ.get(models_config()["provider"]["api_key_env"])


def stubbed() -> bool:
    return not api_key()


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        cfg = models_config()["provider"]
        # max_retries=0 is deliberate: the SDK's default of 2 silently multiplies
        # every timeout by three (3 x 120s = 6 minutes for ONE call), which turned
        # a slow provider into a search that never finished. Retries belong in
        # complete(), where they are bounded, logged and rate-limit aware.
        _client = OpenAI(base_url=cfg["base_url"], api_key=api_key(), max_retries=0,
                         timeout=models_config()["limits"]["request_timeout_seconds"])
    return _client


# ---- circuit breaker -------------------------------------------------------
# A provider that is down or throttling must cost the run seconds, not hours.
# After N consecutive failures every further call short-circuits to a loud STUB
# for the rest of the process; one success closes it again.
_FAILS = [0]
CIRCUIT_TRIP_AFTER = int(os.environ.get("LLM_CIRCUIT_TRIP_AFTER", "3"))


def circuit_open() -> bool:
    return _FAILS[0] >= CIRCUIT_TRIP_AFTER


def circuit_reset() -> None:
    _FAILS[0] = 0
    _LAST_ERROR.clear()


# ---- the provider's own words, kept where a partner can read them -----------
# "128 calls, 128 stubbed" without a reason forces whoever is on call to go
# reading host logs. The last failure is surfaced through /api/summary instead.
_LAST_ERROR: dict = {}


def last_error() -> dict | None:
    return dict(_LAST_ERROR) or None


def _record_error(stage: str, model: str, exc: Exception) -> None:
    msg = str(exc).strip()
    low = msg.lower()
    kind = type(exc).__name__
    if kind == "APITimeoutError" or "timeout" in low or "timed out" in low:
        hint = ("the provider accepted the request but did not answer within "
                f"{models_config()['limits']['request_timeout_seconds']}s — usually a slow "
                "or overloaded model. Try a smaller model in config/models.yaml.")
    elif kind == "APIConnectionError" or "connection error" in low:
        hint = ("could not reach the provider at all — DNS, blocked outbound network, or a "
                f"wrong base_url ({models_config()['provider']['base_url']}). Note the key "
                "is never checked when the connection itself fails.")
    elif "401" in msg or "unauthor" in low or "invalid api key" in low or "forbidden" in low:
        hint = ("the API key was rejected — it is missing, mistyped, expired or rotated. "
                "Check NVIDIA_API_KEY in the host's environment settings.")
    elif "404" in msg or "not found" in low or "unknown model" in low:
        hint = (f"the provider does not serve '{model}' for this key — change the model in "
                "config/models.yaml or enable it on your provider account.")
    elif "429" in msg or "quota" in low or "rate" in low or "credit" in low:
        hint = ("the account is out of quota/credits or is being rate-limited — check the "
                "provider dashboard for remaining credits.")
    else:
        hint = "unexpected provider error — the exact message is above."
    _LAST_ERROR.update({"stage": stage, "model": model, "type": type(exc).__name__,
                        "message": msg[:300], "hint": hint, "at": db.now_iso()})


# ---- pacing: free-tier providers throttle per-minute. Space calls out rather
# than firing the whole funnel back-to-back and eating 429s. ------------------
import time as _t

_last_call = [0.0]
MIN_INTERVAL_S = float(os.environ.get("LLM_MIN_INTERVAL_S", "2.0"))


def _sleep(s: float) -> None:
    _t.sleep(s)


def _pace() -> None:
    gap = _t.time() - _last_call[0]
    if gap < MIN_INTERVAL_S:
        _t.sleep(MIN_INTERVAL_S - gap)
    _last_call[0] = _t.time()


def _log(stage: str, model: str, pt: int, ct: int, stub: bool) -> None:
    db.insert("llm_usage", {"stage": stage, "model": model, "prompt_tokens": pt,
                            "completion_tokens": ct, "stubbed": 1 if stub else 0,
                            "created_at": db.now_iso()})


def complete(stage: str, system: str, user: str, tier: str | None = None) -> str:
    """Plain-text completion. Returns STUB_TEXT when no key is configured."""
    model = models_config()["tiers"][tier or stage if (tier or stage) in models_config()["tiers"] else "score"]
    return _raw_complete(model, stage, system, user)


# The model that actually produced the last successful completion. The silent
# fallback inside _raw_complete (routed model dies -> retry once on the model we
# know answers) is the right behaviour for a partner waiting on a brief, but it
# meant the CALLER could not know which model's words it was storing: a judgement
# produced by the 8b fallback was labelled with the strong model it was routed to.
# That label is load-bearing — the judgement cache re-judges a Deep Dive company
# whose stored judgement came from a weaker model, so a mislabel makes a fallback
# answer permanently impersonate a strong one.
_LAST_MODEL_USED: list = [None]


def last_model_used() -> str | None:
    return _LAST_MODEL_USED[0]


def _strong_timeout_for(model: str):
    """Reasoning models are slow by nature, not broken: the strong model probed at
    43s on a toy context and blows the default 75s on real ones, which turned every
    escalation into a timeout -> fallback -> the fast model answering again — the
    exact loop escalation exists to break. It alone gets a longer leash; the
    default timeout stays where it is for everything else, because a 150s ceiling
    on the 8b tier would just let a dead provider stall the whole search."""
    limits = models_config()["limits"]
    if model and model == models_config().get("strong_model"):
        t = limits.get("strong_model_timeout_seconds")
        if t and float(t) > float(limits["request_timeout_seconds"]):
            return float(t)
    return None


def _raw_complete(model: str, stage: str, system: str, user: str) -> str:
    """The call itself, with an explicit model — so a diagnostic can probe any model
    without editing config and redeploying."""
    if stubbed():
        _log(stage, model, 0, 0, stub=True)
        return STUB_TEXT
    if circuit_open():
        _log(stage, model, 0, 0, stub=True)
        return STUB_CIRCUIT
    gen = models_config().get("generation", {})
    # Per-stage ceilings: judging needs a short verdict, not an essay. Asking a
    # reasoning model for 8192 tokens makes it think for minutes per company.
    cap = (models_config().get("max_tokens_by_stage", {}).get(stage)
           or gen.get("max_tokens", 8192))
    _pace()
    resp = None
    _tried_fallback = False
    for attempt in range(4):
        try:
            client = _get_client()
            slow = _strong_timeout_for(model)
            if slow:
                client = client.with_options(timeout=slow)
            resp = client.chat.completions.create(
                model=model,
                temperature=gen.get("temperature", 1.0),
                top_p=gen.get("top_p", 0.95),
                max_tokens=cap,
                messages=[{"role": "system", "content": f"{system}\n\n{EXTRACTION_RULES}"},
                          {"role": "user", "content": user}])
            break
        except Exception as exc:  # noqa: BLE001 — provider outage must not kill a job
            msg = str(exc)
            # Any failure of the routed model — retired, mistyped, or simply too slow
            # on this tier — degrades to the model we know answers, rather than
            # stubbing. Measured: the 70B models time out here while 8b returns in ~3s,
            # so "the big model is busy" must not cost the partner their analysis.
            fallback = models_config().get("fallback_model") or models_config()["tiers"].get("score")
            if fallback and model != fallback and not _tried_fallback:
                _tried_fallback = True
                print(f"  ~ model '{model}' failed for stage={stage} ({type(exc).__name__})"
                      f" — retrying once with '{fallback}'")
                model = fallback
                continue
            rate_limited = "429" in msg or "rate" in msg.lower() or "quota" in msg.lower()
            if rate_limited and attempt < 3:
                wait = (8, 20, 45)[attempt]
                print(f"  ~ LLM rate-limited ({model}, stage={stage}) — retrying in {wait}s"
                      f" (attempt {attempt + 2}/4)")
                _sleep(wait)
                continue
            _FAILS[0] += 1
            _record_error(stage, model, exc)
            print(f"  ! LLM call failed ({model}, stage={stage}): {type(exc).__name__}:"
                  f" {msg[:120]} — falling back to loud [STUB]"
                  + (f" [circuit open after {_FAILS[0]} failures — remaining AI fields "
                     "in this run will be stubbed rather than waited on]"
                     if circuit_open() else ""))
            _log(stage, model, 0, 0, stub=True)
            return STUB_PROVIDER_DOWN     # a key IS set — say what actually happened
    circuit_reset()
    _LAST_MODEL_USED[0] = model          # post-fallback: the model that ANSWERED
    usage = resp.usage
    _log(stage, model, usage.prompt_tokens if usage else 0,
         usage.completion_tokens if usage else 0, stub=False)
    return resp.choices[0].message.content or ""


def self_test(model_override: str | None = None, hard: bool = False) -> dict:
    """One real call, so 'why is everything stubbed?' is answerable from the dashboard
    instead of the host's logs. Never raises.

    `model_override` probes a specific model without a deploy — the difference between
    "change config, push, wait 4 minutes, hope" and "try three models in 30 seconds".
    `hard=True` sends a judging-sized prompt: a trivial "reply OK" passed in 0.8s while
    every real judgment timed out, so the easy test was answering the wrong question."""
    key_env = api_key_env_name()
    if stubbed():
        return {"ok": False, "reason": f"{key_env} is not set in this environment",
                "key_env": key_env, "key_present": False}
    model = model_override or models_config()["tiers"]["score"]
    circuit_reset()                      # a test should try, not inherit a tripped circuit
    t0 = _t.time()
    if hard:
        out = _raw_complete(model, "selftest",
                            "You are a venture analyst. Judge the company below on founder "
                            "quality, moat and TAM with explicit assumptions, then give a "
                            "short thesis narrative. Cite signal ids [S:n].",
                            "Company: Testco | sector: robotics | stage: seed\n"
                            "[S:1] funding_event @ 2026-08-01 :: {\"title\": \"Testco raises "
                            "$20M Series A led by a tier-1 fund for warehouse robots\"}\n"
                            "[S:2] news @ 2026-08-02 :: {\"title\": \"Testco signs pilot with "
                            "a national logistics operator\"}")
    else:
        out = _raw_complete(model, "selftest", "You are a connection test.",
                            "Reply with the word OK.")
    took = round(_t.time() - t0, 1)
    if is_stub(out):
        err = last_error() or {}
        return {"ok": False, "key_env": key_env, "key_present": True, "model": model,
                "seconds": took, "reason": err.get("hint", "the call did not succeed"),
                "provider_message": err.get("message"), "error_type": err.get("type")}
    return {"ok": True, "key_env": key_env, "key_present": True, "model": model,
            "seconds": took, "reply": (out or "").strip()[:80]}


def _extract_json(text: str) -> dict | list | None:
    """Pull the answer object out of whatever the model wrapped it in.

    The greedy first-brace-to-last-brace match fails on one observed and common
    small-model habit: echoing the JSON SCHEMA it was shown before (or instead of)
    the answer. Then the greedy span covers schema + prose + answer and parses as
    nothing, and a real answer sitting right there was thrown away — 'no parseable
    JSON found' in review_queue with the answer visible in the raw. So on failure,
    walk every balanced {...} block and return the LAST one that parses: the answer
    follows the echo, and a schema echo is recognisable (it carries "$defs"/
    "properties"/"type" keys) and is never returned as an answer."""
    looks_like_schema = lambda d: isinstance(d, dict) and (  # noqa: E731
        "$defs" in d or "$schema" in d
        or ("properties" in d and ("type" in d or "required" in d)))
    text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    if m:
        try:
            got = json.loads(m.group(0))
            # A pure schema echo is syntactically valid JSON — returning it would
            # 'validate' into an all-null judgement downstream. Failing here turns
            # it into the retry-with-error it deserves.
            if not looks_like_schema(got):
                return got
        except json.JSONDecodeError:
            pass
    candidates: list = []
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    candidates.append(json.loads(text[start:i + 1]))
                except json.JSONDecodeError:
                    pass
                start = None
    for cand in reversed(candidates):
        if isinstance(cand, dict) and not looks_like_schema(cand):
            return cand
    return None


def complete_json(stage: str, system: str, user: str, schema_model, tier: str | None = None,
                  model_override: str | None = None):
    """Structured completion validated against a Pydantic model.

    Returns a validated model instance, or None (flagged to review_queue) when
    the model cannot produce valid output after one retry — or when stubbed.
    `model_override` lets a caller escalate to a stronger model when the routed
    one returns syntactically valid but empty output.
    """
    schema_desc = json.dumps(schema_model.model_json_schema(), indent=None)
    sys_full = (f"{system}\n\nRespond with ONLY a JSON object matching this schema:\n"
                f"{schema_desc}\n\nFill in every field you can support from the provided "
                "context — a number where the schema asks for a number, prose where it asks "
                "for prose. Use null ONLY when the context genuinely gives you nothing to go "
                "on; a response of all-nulls is not a valid answer.")
    if stubbed():
        _log(stage, "stub", 0, 0, stub=True)
        return None
    raw = (_raw_complete(model_override, stage, sys_full, user) if model_override
           else complete(stage, sys_full, user, tier=tier))
    if is_stub(raw):          # provider unreachable — honest null, no review spam
        return None
    for attempt in range(2):
        data = _extract_json(raw)
        if data is not None:
            try:
                return schema_model.model_validate(data)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:400]
        else:
            err = "no parseable JSON found"
        if attempt == 0:
            retry_user = (f"{user}\n\nYour previous reply failed validation: {err}\n"
                          f"Previous reply: {raw[:800]}\nReturn ONLY corrected JSON.")
            raw = (_raw_complete(model_override, stage, sys_full, retry_user) if model_override
                   else complete(stage, sys_full, retry_user, tier=tier))
    db.insert("review_queue", {"kind": "llm_parse_failure", "payload_json": json.dumps(
        {"stage": stage, "error": err, "raw": raw[:1000]}), "created_at": db.now_iso()})
    return None


def usage_by_stage() -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT stage, model, COUNT(*) calls, SUM(prompt_tokens) prompt_tokens,"
        " SUM(completion_tokens) completion_tokens, SUM(stubbed) stubbed"
        " FROM llm_usage GROUP BY stage, model ORDER BY stage")]
