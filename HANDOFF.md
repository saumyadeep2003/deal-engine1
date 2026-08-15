# HANDOFF — Deal Sourcing & Discovery Engine (Thirdbase / 535 West Capital)

Paste this file into a new Cowork task (or connect the `deal-engine-deploy` folder
and say "read HANDOFF.md in deal-engine") and the new session has full state.

## Who / where

- User: Saumyadeep Banik (saumya.mimo2003@gmail.com), GitHub `saumyadeep2003`, repo `deal-engine1`, branch `main`.
- Live deployment: **https://deal-engine-dsnp.onrender.com** (Render, auto-deploys on push).
- Repo on the user's Mac: `~/deal-engine-deploy/deal-engine` (Cowork device bridge; connect this folder).
- DB: Supabase Postgres via `DATABASE_URL` (transaction pooler — `prepare_threshold=None` is load-bearing). Local fallback SQLite.
- The user often voice-types; interpret phonetic typos charitably and confirm.

## Delivery workflow (IMPORTANT — learned the hard way)

Never ship via downloaded zips (browser renames created a week of stale deploys).
Correct flow: build + test in the cloud workspace → `tar -czf` of `git ls-files`
→ SendUserFile → `device_commit_files` to `~/deal-engine-deploy/_incoming/` →
`device_bash` untar into the repo → verify sha256 hashes match → user runs
`git add -A && git commit && git push` themselves. Never run git commit/push via
device_bash (leaves un-deletable `.git/index.lock`; user must `rm -f` it).
Always grep shipped artifacts for the NVIDIA key prefix (n v a p i dash, no spaces) = no secrets.
After deploy, verify **/api/version**: commit hash + capability markers
(probed by import). If `complete:false`, the deploy didn't take.

## What the system is

Six-layer funnel, provenance on everything: ingestion (24 registered sources:
EDGAR **daily-index sweep** + keyword FTS, RSS+PR wires, HN, arXiv, GitHub,
Reddit (OAuth-ready), careers, websites, **company_news** Google-News watch,
**companies_house**, ATS boards, Bluesky, Wayback, Apify, **apollo_enrich**,
plus 10 licence-gated stubs) → entity resolution → deterministic filter →
founders from filings (`people.py`) → **domain resolver** (`domains.py`,
Clearbit autocomplete + homepage validation) → profiles (`profile.py`, site →
4-5 line overview, gatekeeper-checked) → enrichment → AI judgement
(`judge.py`, evidence-fingerprint cache, JUDGE_TOP_N budget on companies
lacking judgement, escalate-once-on-empty to strong_model) → cohort-percentile
scoring → briefs (criteria scorecard from `estimates.py`) → commentary →
sectors (TF-IDF clusters, fingerprint-deduped, vendor/event words barred) →
peers → stale (90d flag, never delete) → publish (Excel rebuilds from DB;
Google Sheets batched 2 write calls) → alerts → snapshot.

Key invariants: signals immutable; models never do arithmetic; **gatekeeper**
(`gatekeeper.py`) verifies every model sentence against stored rows (citations
must exist AND belong to the company; numbers match stored values ±1%; named
firms must appear in evidence; offending sentence removed with visible marker;
audit at `/api/gatekeeper`); honest nulls beat invented numbers; licence gaps
say "requires X"; everything partner-facing carries [S:id]/[computed] markers.

LLM: NVIDIA NIM (`NVIDIA_API_KEY`), routing in `config/models.yaml`
(8b for most stages, `thinkingmachines/inkling` strong_model; 70Bs timeout).
~85% of tokens = `score` stage. `llm_usage` logs every call. **User should
rotate the NVIDIA key** (was pasted in chat).

## Env vars on Render (set)

`DATABASE_URL`, `NVIDIA_API_KEY`, `RESEND_API_KEY`, `APIFY_TOKEN`,
`GOOGLE_SERVICE_ACCOUNT_JSON=/etc/secrets/gsa.json`, `APOLLO_API_KEY` (free
plan; auth probe passes; **enrich-endpoint access unverified — first run
answers it**), `JUDGE_TOP_N=40`, `SEARCH_MODE` (check if auto), display TZ=IST.
Not yet set: `REDDIT_CLIENT_ID/SECRET` (free, unblocks commentary),
`COMPANIES_HOUSE_API_KEY` (free, UK founders), Bluesky `handles:` in
config/sources.yaml. Digest: daily 07:00 IST (`thesis.yaml digest.days`).

## Test suites (all must stay green)

`tests/acceptance.py` 19, `tests/deployment.py` 18 (needs uvicorn on :8787),
`tests/gatekeeper_test.py` 22, `tests/events_test.py`, `tests/phase1_test.py` 14,
`tests/mcp_test.py` 13. MCP server (`mcp_server.py`, stdio for Claude Desktop,
13 tools) exists but user **dropped the hybrid plan — API-only**; keep
`web/api.py` free of mcp imports, `requirements-mcp.txt` separate.

## Docs in repo (read in this order)

`HANDOFF.md` (this) → `BUILD_LOG.md` (74 numbered decisions/bugs — the memory)
→ `ASSIGNMENT_AUDIT.md` (assignment PDF vs build) → `COMPLETENESS_ROADMAP.md`
(3 phases; Phase 1 DONE) → `PRODUCTION_PLAN.md` (costs/licences).

## Immediate next work (user-agreed order)

1. **AI-assessment verification fixes** (from last session's audit): route Deep
   Dive candidates to strong_model always (escalation currently fires only on
   EMPTY, never on wrong); require strong-model agreement before a fast-model
   "not venture relevant" sets status=filtered (only irreversible unverified
   model action); evidence fingerprint (`count:max_id`) misses in-place edits.
2. **Corroboration scoring** — per claim: "filing + 2 articles" vs "one blog".
3. **Feedback loop** — ~82 partner_actions rows teach nothing today: weekly
   logistic recalibration of scoring weights + recent overrides few-shot into
   judge prompt.
4. Phase 2 free sources: USPTO PatentsView (moat/inventors), Whisper podcast
   transcripts, crt.sh stealth watch, YC/Product Hunt/npm; then Phase 3
   (change-alerts "open roles -40%", IC memo generator with bear case, weekly
   self-report).
5. Verify latest deploy picked up: news-watch scoping (Pass companies → monthly
   heartbeat only), domain resolver step, Apollo Deep-Dive-only enrichment
   (note: selects on PREVIOUS run's calls — one-run lag, deliberate).

## Known open issues

- Auth: dashboard is public (top production risk; PRODUCTION_PLAN item 1).
- Entity junk ("Text", "Built") until registry-grade resolution.
- Commentary ~3% until Reddit creds. Apify LinkedIn/X scraping: refused (ToS);
  Coresignal/X API are the licensed routes; Apollo covers headcount+growth+
  funding for Deep Dive. PitchBook-only: valuations/cap tables (estimates.py
  substitutes, labelled). No monitoring/alerting on failed runs yet.
