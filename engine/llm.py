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
    if stubbed():
        _log(stage, model, 0, 0, stub=True)
        return STUB_TEXT
    if circuit_open():
        _log(stage, model, 0, 0, stub=True)
        return STUB_TEXT
    gen = models_config().get("generation", {})
    # Per-stage ceilings: judging needs a short verdict, not an essay. Asking a
    # reasoning model for 8192 tokens makes it think for minutes per company.
    cap = (models_config().get("max_tokens_by_stage", {}).get(stage)
           or gen.get("max_tokens", 8192))
    _pace()
    resp = None
    for attempt in range(4):
        try:
            resp = _get_client().chat.completions.create(
                model=model,
                temperature=gen.get("temperature", 1.0),
                top_p=gen.get("top_p", 0.95),
                max_tokens=cap,
                messages=[{"role": "system", "content": f"{system}\n\n{EXTRACTION_RULES}"},
                          {"role": "user", "content": user}])
            break
        except Exception as exc:  # noqa: BLE001 — provider outage must not kill a job
            msg = str(exc)
            rate_limited = "429" in msg or "rate" in msg.lower() or "quota" in msg.lower()
            if rate_limited and attempt < 3:
                wait = (8, 20, 45)[attempt]
                print(f"  ~ LLM rate-limited ({model}, stage={stage}) — retrying in {wait}s"
                      f" (attempt {attempt + 2}/4)")
                _sleep(wait)
                continue
            _FAILS[0] += 1
            print(f"  ! LLM call failed ({model}, stage={stage}): {type(exc).__name__}:"
                  f" {msg[:120]} — falling back to loud [STUB]"
                  + (f" [circuit open after {_FAILS[0]} failures — remaining AI fields "
                     "in this run will be stubbed rather than waited on]"
                     if circuit_open() else ""))
            _log(stage, model, 0, 0, stub=True)
            return STUB_TEXT
    circuit_reset()
    usage = resp.usage
    _log(stage, model, usage.prompt_tokens if usage else 0,
         usage.completion_tokens if usage else 0, stub=False)
    return resp.choices[0].message.content or ""


def _extract_json(text: str) -> dict | list | None:
    text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def complete_json(stage: str, system: str, user: str, schema_model, tier: str | None = None):
    """Structured completion validated against a Pydantic model.

    Returns a validated model instance, or None (flagged to review_queue) when
    the model cannot produce valid output after one retry — or when stubbed.
    """
    schema_desc = json.dumps(schema_model.model_json_schema(), indent=None)
    sys_full = (f"{system}\n\nRespond with ONLY a JSON object matching this schema "
                f"(all fields nullable — say null rather than guessing):\n{schema_desc}")
    if stubbed():
        _log(stage, "stub", 0, 0, stub=True)
        return None
    raw = complete(stage, sys_full, user, tier=tier)
    if raw == STUB_TEXT:      # provider unreachable — honest null, no review spam
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
            raw = complete(stage, sys_full,
                           f"{user}\n\nYour previous reply failed validation: {err}\n"
                           f"Previous reply: {raw[:800]}\nReturn ONLY corrected JSON.",
                           tier=tier)
    db.insert("review_queue", {"kind": "llm_parse_failure", "payload_json": json.dumps(
        {"stage": stage, "error": err, "raw": raw[:1000]}), "created_at": db.now_iso()})
    return None


def usage_by_stage() -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT stage, model, COUNT(*) calls, SUM(prompt_tokens) prompt_tokens,"
        " SUM(completion_tokens) completion_tokens, SUM(stubbed) stubbed"
        " FROM llm_usage GROUP BY stage, model ORDER BY stage")]
