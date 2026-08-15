# Talking to the engine from Claude

The hybrid architecture, running for free. The engine stays exactly as it is —
system of record, deterministic filter, gatekeeper, Postgres — and this adds a
second doorway so a Claude conversation can ask it questions.

Nothing about the hosted service changes. `mcp_server.py` is a separate entry
point; `web/api.py` does not import it, `requirements.txt` does not list its
dependency, and Render never runs it. Delete the file and the deployment does not
notice.

## What you can ask

Thirteen tools, shaped like questions rather than endpoints:

| Ask | Tool |
|---|---|
| "What's worth looking at in defence tech?" | `pipeline_search` |
| "Write me the brief on company 118" | `company_brief` |
| "Prove that — where did the round number come from?" | `company_evidence` |
| "Find companies matching this thesis: …" | `thesis_scan` |
| "What sub-sectors are emerging before consensus?" | `emerging_sectors` |
| "Who's quietly investing in robotics? Who co-invests?" | `investor_activity` |
| "What are engineers saying about them?" | `commentary` |
| "What should I read this week?" | `news_worth_reading` |
| "How much of the pipeline has actually been analysed?" | `coverage_report` |
| "Is everything working?" | `engine_status` |
| "Mark that one Deep Dive" | `record_decision` |
| "Go find new deals" | `start_search` → `search_progress` |

## Setup (5 minutes, no new subscriptions)

**1. Install the SDK**

```bash
cd ~/deal-engine-deploy/deal-engine
pip install -r requirements-mcp.txt
```

**2. Decide which database it reads**

Leave `DATABASE_URL` unset and it reads the local SQLite copy — fine for trying
the tools out. Set it to the same Supabase connection string Render uses and
Claude talks to the **live pipeline**, the same rows the dashboard shows. Put it
in `.env` in the repo (already gitignored); do not paste it into a chat.

**3. Point Claude Desktop at it**

Settings → Developer → Edit Config, and add:

```json
{
  "mcpServers": {
    "deal-engine": {
      "command": "python3",
      "args": ["/Users/saumyadeepbanik/deal-engine-deploy/deal-engine/mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop. The tools appear under the connector icon.

**4. Check it before trusting it**

```bash
DEAL_ENGINE_MCP_SELFTEST=1 python3 mcp_server.py   # prints which database it opened
python3 tests/mcp_test.py                          # 13 checks
```

## What this is not, yet

- **stdio only.** It runs on your Mac, so Claude can reach it when your Mac is
  awake and Claude Desktop is running. A hosted remote MCP server would need
  authentication and a URL — the next step, not this one.
- **No new spend.** Same database, same model key, same everything. The point of
  testing it this way is that the bill does not move.
- **Writes are real.** `record_decision` and `start_search` change live data. A
  search costs model tokens exactly as pressing the dashboard button does.

## Why the tools carry their own caveats

Each tool returns its gaps alongside its answer — `commentary` says that an empty
result means "not found in free sources" rather than "nobody is talking about
them", `pipeline_search` says its ranks are cohort-relative, a sector cluster with
no measured consensus says it is volume rather than a trend.

That is deliberate and it is tested. Everywhere else in this system the discipline
is enforced by code: the gatekeeper drops an unsourced sentence, the brief
validator refuses an unresolvable citation. A conversation has no such mechanism —
the model is free to summarise. Putting the caveat inside the payload is the only
place it survives into the answer.
