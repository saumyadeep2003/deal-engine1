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
own launch/website signals then Clearbit autocomplete, homepage-validated) →
profiles (`profile.py`, site → 4-5 line overview, gatekeeper-checked; HTML fetched
via **pluggable engine** `adapters/fetching.py` — Scrapling when installed, httpx
otherwise) → enrichment → AI judgement
(`judge.py`, context-hash evidence fingerprint, JUDGE_TOP_N budget on companies
lacking judgement, **Deep Dive candidates judged on strong_model first**,
escalate-once-on-empty for everyone else, **a rejection needs two models to
agree before it filters anything**) → cohort-percentile
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

`tests/acceptance.py` 19 (run `python demo.py` first), `tests/deployment.py` 18
(needs the service on :8787 — start it with `python serve.py`, not bare uvicorn,
or D12 fails on a missing scheduler log), `tests/gatekeeper_test.py` 22,
`tests/events_test.py`, `tests/phase1_test.py` 14, `tests/mcp_test.py` 13,
`tests/judge_verification_test.py` 35, `tests/llm_robustness_test.py` 14,
`tests/identity_test.py` 18, `tests/fetching_test.py` 16,
`tests/run_sequence_test.py` 11, `tests/phase2_test.py` 26. MCP server (`mcp_server.py`, stdio for Claude Desktop,
13 tools) exists but user **dropped the hybrid plan — API-only**; keep
`web/api.py` free of mcp imports, `requirements-mcp.txt` separate.

## Docs in repo (read in this order)

`HANDOFF.md` (this) → `BUILD_LOG.md` (74 numbered decisions/bugs — the memory)
→ `ASSIGNMENT_AUDIT.md` (assignment PDF vs build) → `COMPLETENESS_ROADMAP.md`
(3 phases; Phase 1 DONE) → `PRODUCTION_PLAN.md` (costs/licences).

## Immediate next work (user-agreed order)

1. ~~**AI-assessment verification fixes**~~ — **DONE, BUILD_LOG 75.** Deep Dive
   candidates now judged on strong_model first (routing read from the current
   recommendation, one run behind by design); a fast-model "not venture
   relevant" needs strong-model agreement before it sets status=filtered, and an
   unconfirmed rejection keeps the company and files a `review_queue` row of kind
   `unconfirmed_rejection`; the evidence fingerprint is now a hash of the context
   string (`ctx1:…`), so founders/profile/sector changes invalidate it — old
   `count:max_id` keys still honoured while they match, so coverage does not
   collapse on this deploy. **After the first live run, check**: `/api/version`
   markers `strong_model_for_deep_dive` / `verified_rejections` /
   `context_evidence_fingerprint` all true; the judge log line's
   "N on thinkingmachines/inkling" count; and whether inkling actually answers
   real Deep Dive contexts inside the 75s timeout (models.yaml notes it was
   marginal — a timeout degrades to 8b via `fallback_model`, which is the old
   behaviour, not a failure, but if it happens every time the routing is buying
   nothing and the cost belongs elsewhere).
2. **Corroboration scoring** — per claim: "filing + 2 articles" vs "one blog".
3. **Feedback loop** — ~82 partner_actions rows teach nothing today: weekly
   logistic recalibration of scoring weights + recent overrides few-shot into
   judge prompt.
4. ~~Phase 2 free sources~~ — **MOSTLY DONE, BUILD_LOG 84**: PatentsView
   (set free PATENTSVIEW_API_KEY on Render), YC batches, GDELT news sweep,
   podcast show-notes + local whisper transcript script, ATS 3→6 providers.
   Still open from the Phase 2 list: crt.sh stealth watch, Product Hunt
   (needs free dev token), npm downloads. Then Phase 3 (change-alerts
   "open roles -40%", IC memo generator with bear case, weekly self-report).
5. Verify latest deploy picked up: news-watch scoping (Pass companies → monthly
   heartbeat only), domain resolver step, Apollo Deep-Dive-only enrichment
   (note: selects on PREVIOUS run's calls — one-run lag, deliberate).
6. Surface `unconfirmed_rejection` review rows in the dashboard — they are
   written and queryable but nothing in the UI shows them yet, and a queue
   nobody can see is the same as no queue.
8. ~~**Close the domains→profiles one-run lag**~~ — **DONE, BUILD_LOG 81.**
   Pipeline reordered: careers/website collects now run AFTER the domain
   resolver (found → read → profiled in one run), apollo_enrich runs AFTER
   scoring (this run's Deep Dive picks, cache readable by briefs), profiles
   batch prefers companies with read sites, scrapling retries trimmed.
   `tests/run_sequence_test.py` pins the order. NOTE the one-run lag notes in
   items 5/74 are superseded for Apollo.
7. ~~**From reading run 20 on the live box**~~ — **(a)-(c) DONE, BUILD_LOG 77.**
   Uncorroborated single-word identities are held at Watch (never deleted;
   reason in the feature vector; cap lifts when a domain/filing/round/founder
   lands); aggregator-domain names dropped by the filter even from hot rows;
   domains now read from the company's own launch/website signals before
   Clearbit; founders backfilled from old filings' never-fetched XML (60/run,
   newest first). **Still user action:** set `REDDIT_CLIENT_ID/SECRET`
   (commentary 3.5%) and `COMPANIES_HOUSE_API_KEY` (0 UK items) on Render —
   both free. **After next live run check:** the judge line for "N on
   thinkingmachines/inkling" (does the 150s ceiling let it answer real
   contexts?), founder coverage climbing past 75/347, top picks carrying
   descriptions, and `/api/coverage`'s "AI assessment written" rising now
   that tam.assumptions no longer kills judgements.

## HTML fetch engine (Scrapling) — optional, free

`engine/adapters/fetching.py` routes every `http_get` through a chosen transport.
Default httpx; install `requirements-scrapling.txt` to add Scrapling's no-browser
`Fetcher` (browser TLS fingerprints — clears the 403s/JS-shells on company & careers
pages that caused empty briefs), and `scrapling install` + `SCRAPLING_MODE=stealth`
for a real browser (JS render + Cloudflare, local/larger box — NOT the free tier).
`SCRAPLING_MODE`: `auto` (default, respects each source's `fetch_engine`, APIs stay
httpx), `off`, `http`, `stealth`, `dynamic`. Only `company_website` and
`careers_pages` opt in (`fetch_engine: stealth`, downgrades gracefully). Licensed/
ToS-protected sources are NEVER routed here — a fetcher is not a licence (BUILD_LOG
78). Verify with `/api/version` `scrapling_installed` and the connection-test detail
(shows the engine per source). To turn it on in Render: add `pip install -r
requirements-scrapling.txt` to the build command; the browser modes won't fit free.

## Known open issues

- Auth: dashboard is public (top production risk; PRODUCTION_PLAN item 1).
- Entity junk ("Text", "Built") until registry-grade resolution.
- Commentary ~3% until Reddit creds. Apify LinkedIn/X scraping: refused (ToS);
  Coresignal/X API are the licensed routes; Apollo covers headcount+growth+
  funding for Deep Dive. PitchBook-only: valuations/cap tables (estimates.py
  substitutes, labelled). No monitoring/alerting on failed runs yet.
