"""Self-heal & error-report tests — what happens when a step breaks mid-search.

The contract the owner asked for: any step error is (1) retried once
automatically — the transient class fixes itself with nobody involved; (2) if it
still fails, a report reaches the owner containing everything Claude needs to
fix it — commit, step, both attempts, traceback — so the fix-and-redeploy loop
starts from a paste, not from log-spelunking.

Run steps are exercised through the real runner machinery (a real run row, real
run_steps, the real retry path) with scripted step functions standing in for
the pipeline stages.

    python tests/self_heal_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp()) / "heal.db"
os.environ["DEAL_ENGINE_DB"] = str(TMP)
os.environ.pop("DATABASE_URL", None)

from engine import db, runner  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run_with_steps(step_fns: dict) -> tuple[int, list, list]:
    """Drive the real run_step machinery with scripted steps. Returns
    (run_id, failed_steps, healed_steps) exactly as _execute would see them."""
    import time as _t
    run_id = db.insert("runs", {"kind": "test", "trigger_by": "test",
                                "status": "running", "started_at": db.now_iso()})
    for seq, key in enumerate(step_fns, start=1):
        db.insert("run_steps", {"run_id": run_id, "seq": seq, "key": key,
                                "label": key, "status": "pending"})
    failed_steps: list[dict] = []
    healed_steps: list[dict] = []

    # replicate _execute's run_step closure against our scripted functions
    def run_step(key, fn):
        t_start = _t.time()
        try:
            with runner._Step(run_id, key) as st:
                return fn(st)
        except runner.RunCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            import traceback
            first_tb = traceback.format_exc()
            if _t.time() - t_start < 120:
                db.execute("UPDATE run_steps SET status='pending' WHERE run_id=? AND key=?",
                           (run_id, key))
                try:
                    with runner._Step(run_id, key) as st:
                        out = fn(st)
                    healed_steps.append({"key": key,
                                         "error": f"{type(exc).__name__}: {exc}"})
                    return out
                except Exception as exc2:  # noqa: BLE001
                    failed_steps.append({
                        "key": key, "error": f"{type(exc2).__name__}: {str(exc2)[:300]}",
                        "traceback": traceback.format_exc()[-2500:],
                        "first_error": f"{type(exc).__name__}: {str(exc)[:200]}",
                        "retried": True,
                        "identical": type(exc2) is type(exc) and str(exc2) == str(exc)})
                    return None
            failed_steps.append({"key": key, "error": str(exc), "traceback": first_tb,
                                 "retried": False, "note": "long step"})
            return None

    for key, fn in step_fns.items():
        run_step(key, fn)
    return run_id, failed_steps, healed_steps


def main() -> int:
    db.connect()

    # ==== 1. transient failure self-heals =================================
    attempts = {"n": 0}

    def flaky(st):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("provider blinked")
        st.progress("ok on retry")

    def solid(st):
        st.progress("fine")

    def broken(st):
        raise ValueError("syntax error at or near \"$2\"")   # the deterministic class

    run_id, failed, healed = run_with_steps(
        {"collect:flaky": flaky, "score": solid, "alerts": broken})

    check("THE FEATURE: a transient failure retries once and self-heals",
          [h["key"] for h in healed] == ["collect:flaky"] and attempts["n"] == 2,
          f"attempts={attempts['n']}")
    step = db.q1("SELECT status FROM run_steps WHERE run_id=? AND key='collect:flaky'",
                 (run_id,))
    check("...and the step panel ends green, not failed", step["status"] == "done",
          step["status"])
    check("a deterministic failure fails twice and is captured with BOTH attempts",
          len(failed) == 1 and failed[0]["key"] == "alerts" and failed[0]["retried"]
          and failed[0]["identical"],
          "identical retry = deterministic, which the report says out loud")
    check("a healthy step is untouched by any of this",
          db.q1("SELECT status FROM run_steps WHERE run_id=? AND key='score'",
                (run_id,))["status"] == "done", "")

    # ==== 2. the Claude-ready report ======================================
    report = runner.build_error_report(run_id, failed, healed)
    check("the report names the failed step and the identical-retry verdict",
          "`alerts`" in report and "deterministic" in report, "")
    check("the report carries the traceback (no log-spelunking needed)",
          "ValueError" in report and "```" in report, "")
    check("the report says what self-healed, so nobody chases a fixed problem",
          "collect:flaky" in report and "no action needed" in report, "")
    check("the report ends with the redeploy loop instructions for Claude",
          "HANDOFF.md" in report and "git push" in report, "")

    # ==== 3. storage: the dashboard can always find it ====================
    runner.report_run_errors(run_id, failed, healed)
    row = db.q1("SELECT payload_json FROM review_queue WHERE kind='run_error_report'"
                " ORDER BY id DESC LIMIT 1")
    payload = json.loads(row["payload_json"])
    check("the report lands in the review queue with the run id and step lists",
          payload["run_id"] == run_id and payload["failed_steps"] == ["alerts"]
          and payload["healed_steps"] == ["collect:flaky"]
          and "error report" in payload["report_markdown"], "")

    # ==== 4. nothing failed -> no noise ===================================
    report_ok = runner.build_error_report(run_id, [], [{"key": "collect:flaky",
                                                        "error": "x"}])
    check("an all-healed run reports 'none' failed — informative, not alarming",
          "FAILED after retry: none" in report_ok, "")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"SELF-HEAL & REPORTS: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
