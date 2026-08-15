"""MCP server tests — every tool answers, and none of them can quietly lie.

Two things are checked, and the second matters more.

Recall: every tool returns valid JSON and does not raise. A tool that throws
inside an MCP session surfaces to the model as an opaque failure, and the model's
usual recovery is to answer from memory — which is the exact failure the whole
engine is built to prevent.

Discipline: the tools that could be quoted out of context carry their own
caveats. `commentary` must say that an empty result means "not found in free
sources" rather than "nobody is talking about them". `pipeline_search` must say
its ranks are cohort-relative. Those sentences are the only thing standing
between a chat answer and a confident summary of nothing.

    python tests/mcp_test.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server as srv  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def call(name: str, **kwargs) -> str:
    res = await srv.mcp.call_tool(name, kwargs)
    body = res[0] if isinstance(res, tuple) else res
    return body[0].text if isinstance(body, list) and body else str(body)


async def main() -> int:
    tools = await srv.mcp.list_tools()
    names = {t.name for t in tools}
    check("every engine capability is exposed as a tool", len(tools) >= 13,
          f"{len(tools)} tools")

    # Descriptions are the model's only guide to picking correctly. A tool with a
    # thin description is a tool that gets called for the wrong question.
    thin = [t.name for t in tools if len(t.description or "") < 80]
    check("every tool description explains when to use it", not thin, str(thin))

    read_only = {
        "pipeline_search": {"limit": 3},
        "coverage_report": {},
        "emerging_sectors": {"limit": 2},
        "thesis_scan": {"description": "robotics warehouse automation", "limit": 3},
        "engine_status": {},
        "news_worth_reading": {"limit": 3},
        "investor_activity": {"limit": 5},
        "search_progress": {},
    }
    bad = []
    for name, kw in read_only.items():
        try:
            txt = await call(name, **kw)
            json.loads(txt)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{name}: {type(e).__name__}: {e}")
    check("every read tool returns valid JSON and never raises", not bad, "; ".join(bad))

    # A tool handed a nonsense id must answer, not explode: the model needs a
    # sentence it can relay, not a stack trace it will paper over.
    for name, kw in (("company_evidence", {"company_id": 999999}),
                     ("commentary", {"company_id": 999999}),
                     ("record_decision", {"company_id": 999999, "action": "Watch"})):
        try:
            txt = await call(name, **kw)
            payload = json.loads(txt)
            ok = "error" in payload or payload.get("ok") is False or payload
        except Exception as e:  # noqa: BLE001
            ok = False
            txt = f"{type(e).__name__}: {e}"
        check(f"{name} answers gracefully for a missing company", bool(ok), str(txt)[:80])

    # Invalid input must be refused rather than written.
    txt = await call("record_decision", company_id=1, action="Maybe")
    check("record_decision refuses an action outside Pass/Watch/Deep Dive",
          "error" in json.loads(txt), txt[:80])

    # --- the caveats that keep a chat answer honest -------------------------
    txt = await call("commentary", company_id=1)
    check("commentary states that empty means 'not in free sources'",
          "free sources" in txt.lower() and "nobody" in txt.lower(), "")

    txt = await call("pipeline_search", limit=2)
    check("pipeline_search states its ranks are cohort-relative",
          "cohort" in txt.lower(), "")

    txt = await call("emerging_sectors", limit=2)
    payload = json.loads(txt)
    zero_ratio_labelled = all(
        ("not" in (c.get("reading") or "").lower()
         or "no mainstream" in (c.get("reading") or "").lower())
        for c in payload.get("clusters", []) if c.get("ratio") == 0)
    check("a sector with unmeasured consensus is not sold as an emerging trend",
          zero_ratio_labelled, "")

    txt = await call("coverage_report")
    cov = json.loads(txt)
    check("coverage names the cap limiting every stage",
          all(s.get("cap") for s in cov.get("stages", [])),
          f"{len(cov.get('stages', []))} stages")

    # --- the hosted API must be entirely unaffected -------------------------
    import web.api as api
    check("the FastAPI app still imports and is untouched by the MCP layer",
          hasattr(api, "app"), "")
    src = (ROOT / "web" / "api.py").read_text()
    check("the web layer does not import the MCP server",
          "mcp_server" not in src, "")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"MCP: {passed}/{len(RESULTS)} passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAILING: {name} — {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
