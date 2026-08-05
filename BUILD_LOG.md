# BUILD_LOG — decisions, stubs, and what changes with a data budget

Chronological log of judgment calls made while building. Part of the deliverable.

## Environment & data-access decisions

1. **Build sandbox had no direct egress** to sec.gov / hn.algolia.com / RSS hosts
   (corporate proxy 403). Decision: adapters are live-first with a snapshot-cache fallback
   (`data/cache/`), where every snapshot is a verbatim body of a real fetch of the same URL,
   and every stored signal records `fetch_mode` (`live` | `cached_snapshot`). Rows are
   therefore real regardless of where the demo runs. Nothing in the cache is hand-authored.
2. **EDGAR full-text search matches issuer names more than prose** — Form D XML carries
   little descriptive text, so keyword queries ("robotics", "artificial intelligence")
   effectively surface *named* companies and vehicles. Accepted: it is precise, real and
   high-signal; RSS/HN cover thesis language the filings don't contain.
3. **Form D `primary_doc.xml` detail fetches** (offering amounts, related persons) are a
   second request per filing, capped at 15/run to keep the demo fast. When the detail fetch
   is unavailable, amount fields stay null with a reason — never estimated from the FTS hit.

## Schema & code decisions

4. Added operational tables beyond the specified data model: `checkpoints` (EDGAR
   high-water mark that only advances on success), `llm_usage` (per-stage token log),
   `alerts_log` (dedupe + rate limiting). All noted here per instructions.
5. **Pipeline tab columns**: the brief's enumerated list contains 17 columns while its prose
   says 16. The enumerated list is treated as authoritative and implemented exactly, in
   order (`outputs/excel.py::PIPELINE_COLS`).
6. **Fuzzy-match suffix stripping** treats `Systems`, `Labs`, `Technologies`, `AI` as legal
   noise ("Harmonic AI" ≡ "Harmonic, Inc."). Risk of over-merge is bounded by the 0.85
   auto-merge threshold, sector/geo penalties, and the review queue band (0.60–0.85), and
   every merge is reversible (snapshot in `company_aliases.merged_from`).
7. **Synthetic namespace guard**: any name starting `DEMO` is quarantined — it can never
   fuzzy-merge with, or be created as, a real record.
8. **SPV/fund detection** is a name heuristic (fund/LP/SPV/"a series of"/Gaingels/…) plus
   the Form D `Pooled Investment Fund` industry code when the XML is available. Remaining
   leakage (e.g. an SPV named like an operating company) is exactly what the flash-tier
   classifier stage removes when a key is present.
9. **Public-company leakage** from EDGAR display names is filtered by the `(TICKER)` suffix
   rule + config blocklist.
10. **Funding-amount window** (`min/max_offering_usd`) applies to Form D offerings only. A
    disclosed $3.5B venture round from news is a deal, not a fund.
11. **HN stories with "raises $X"** are parsed into `funding_event` signals with the same
    deterministic regex as RSS — a model never extracts amounts.
12. **Commentary misattribution risk** (name collision on generic names like "Warp") is
    mitigated by a generic-name blocklist and legal-suffix stripping; with licensed data the
    right fix is domain-anchored matching. Quotes are always real text with real URLs.
13. **Vector search**: `sqlite-vec` skipped in favour of TF-IDF + cosine in numpy — zero
    native-extension risk on a clean demo machine; pgvector is the production target.
14. **Relevance scoring for news** is deterministic (theme match × source weight + HN
    points). The one-line "why it matters" is model judgment and stubs loudly without a key.

## Stubbed (deliberately) and why

- **Feedback recalibration**: `partner_actions` writes are live (workbook edits land there
  with the feature vector); weight re-fitting from those actions is a documented next step.
- **Licensed adapters** (PitchBook, Coresignal, Harmonic, Crunchbase, X, Blind, podcasts,
  Substack, The Information): full interface, registry, health, and (for PitchBook) a
  response parser written against documented shapes; they return `LicenseRequired` with no
  key. This is the assignment's core constraint made explicit.
- **Email/Slack delivery**: digests render to HTML files; Resend/Slack Bolt are config-level
  swaps in production.

## What changes with a real data budget

- Coresignal fills headcount/growth → YoY growth and runway compute with stated assumptions
  (fully-loaded $220k/head burn) instead of rendering `— (requires Coresignal)`.
- PitchBook/Crunchbase densify the `investments` join table → co-investor heatmap and Tier
  1/2/3 counts move from sparse-but-real to comprehensive; valuation-vs-cohort-median
  becomes computable per (sector, stage).
- X + LinkedIn (Coresignal) feed the GP-watchlist corpus → sector detection gains its
  strongest leading indicator (GP attention), and founder-migration tracking becomes real.
- Volume rises from ~10² to ~10³-10⁴ signals/day → the 80–90% deterministic-filter share and
  the funnel economics (5,000 → 600 → 120 → 25 → 8) become directly observable; with the
  snapshot corpus (EDGAR+HN only) the filter removes ~70% because EDGAR is already
  high-precision. Noisier sources raise the removal share, not lower it.

## Deployment pass (always-on service, dashboard, email, Google Sheet)

15. **One process, not two.** The scheduler and the web app share a single process
    (`serve.py`, FastAPI lifespan starts APScheduler). One SQLite file, one log stream, one
    thing to start/stop. `run.py` is kept as the headless scheduler-only entry point.
16. **LaunchAgent, not LaunchDaemon.** The engine runs as the logged-in user because the
    data, the `.env` and the outputs belong to that user, not to root.
17. **Sleep is a correctness problem, not an ops detail.** A laptop suspends. Jobs use
    `coalesce=True` + a 6-hour misfire grace, and startup runs a digest catch-up (today is a
    digest day, the hour has passed, nothing sent → send now). Without this a closed lid at
    07:00 Monday would silently skip a digest, which is exactly the class of failure the
    brief's "nothing fails silently" rule targets.
18. **Delivery is verified, not assumed.** `digests` and `alerts_log` gained `delivered` +
    `delivery_detail`; a Resend failure lands in `review_queue`. Added via an additive
    `ALTER TABLE` migration in `db.connect()`, since `CREATE TABLE IF NOT EXISTS` cannot add
    columns to an existing database.
19. **Resend free-tier restriction documented, not hidden.** With no verified domain you can
    only deliver to the Resend account's own address. Sending to partners is a domain
    verification plus a `DIGEST_FROM` change — noted in DEPLOY.md rather than discovered on
    demo day.
20. **Google Sheet mirrors the generated workbook** rather than re-querying the DB. One
    renderer, two destinations — a second query path would be a second source of truth and
    would eventually disagree with the .xlsx.
21. **`dealctl` instead of a menu-bar app.** A rumps/pyobjc menu-bar item would need
    macOS-specific permissions and could not be tested in this build environment. A CLI
    plus the dashboard's own health panel gives the same visibility and is verifiable.
    Noted as a deliberate substitution.
22. **Dashboard visuals were validated, not eyeballed.** Ordinal single-hue ramps for the
    ordered forms (funnel stages, recommendation mix) run through the palette validator in
    both modes; the dark ramp is deliberately reversed so the small deep-funnel stages get
    the light, legible steps on a dark surface. Sectors are *nominal*, so every sector bar
    gets one hue — a value-ramp there would double-encode bar length as colour.
23. **Rendering the dashboard caught three real bugs** that reading the code did not:
    `[object Object]` in table cells (a cell-shape branch that only handled `{node}`),
    junk company names from headline regexes ("A", "Natural") reaching the pipeline, and
    the synthetic demo's `demo_src_*` sources polluting the health panel. Screenshot-and-look
    is now part of the build loop, not a nicety.
24. **A mislabeled metric was caught the same way.** The dashboard's "Filter removed" was
    company-level (35.9%) sitting next to a signal-level funnel, which read as if the
    documented ≥80% filter had regressed. Both numbers are now computed and labelled
    separately (`signals_filtered_pct` 82.3%, `companies_filtered_pct` 35.9%).
25. **Deployment has its own acceptance suite** (`tests/deployment.py`, 12 checks) because
    the pipeline suite cannot see the web layer, the scheduler, or whether email and sheets
    degrade honestly. Both suites pass: 19/19 and 12/12.

## Doc-audit round (line-by-line against the fund's brief)

26. **Audited the build against the original brief and found ten gaps**, four of them
    things earlier docs implied were done. All ten closed in this round:
    GitHub contributor count + commit velocity (Link-header trick + /stats/participation,
    202-aware); the ~11,500-firm dataset loader (`engine/firms.py`, CSV drop-in at
    config/firms.csv, honest `coverage()` when absent); sourcing *inside* emerging clusters
    (§2b "then go find the best companies in them" — Sector of Tomorrow now lists them with
    cohort percentiles); company surface area (positioning/customer logos from author-written
    alt text/pricing pages — `adapters/website.py`); customer-win + founder-move signal
    kinds (`engine/events.py`); frontier-lab talent-flow trend input; Bloomberg/Reuters/FT/
    Dealroom feeds; a Dealroom licensed adapter; `gavinsbaker` on the watchlist; Index
    Ventures moved to Tier 1 (named in the brief's peer set).
27. **The event classifier is regex with stored evidence spans, and it earned its tests.**
    The first version passed hand-written positives, then produced five false positives on
    the real corpus ("won't" matched `won`; a bare "…versus OpenAI" counted as a departure).
    The real failing headlines are now regression tests (`tests/events_test.py`: 8/8
    positives, 0/13 false positives). On the current corpus it honestly reports 0 founder
    moves and 0 customer wins — the earlier five were all noise, and zero is the correct
    number, which is rather the point of the accuracy mandate.
28. **Firm matching is precision-first in both directions**: short keys ('GV', 'IVP', '8VC')
    match only as standalone tokens so 'EGV Arigon' cannot match GV; long keys match on word
    boundaries so 'Index' cannot match 'Indexed Bio' while 'Sequoia Capital Global Growth'
    still resolves. Config tiers win over dataset tiers on conflict.

## Durable-storage + manual-search round (Supabase, tracked runs, run history)

29. **One db layer, two backends.** `DATABASE_URL` set → Postgres (Supabase); unset →
    SQLite, zero code changes elsewhere. Rather than rewriting ~200 queries, Postgres is
    taught the SQLite dialect: `db/pg_compat.sql` defines `julianday()`, `datetime(x, modifier)`
    and a `group_concat` aggregate, and `engine/db.py` translates `?` placeholders to `%s`
    (escaping literal `%` first — `LIKE 'demo\_%'` burned one round). The full pipeline,
    workbook, digest, alerts and chat were proven identical on local Postgres 16 before
    shipping. Portability traps that did need query changes: `MAX(a,b)` scalar (SQLite-only),
    `HAVING` on a select alias, GROUP BY strictness, and — the subtle one — `date('now')`,
    which PG parses as a *cast to the date type* that shadows any compat function; those
    call sites now compute day-strings in Python. `backend_info()` is exposed through
    `/api/summary` so the dashboard can state durable-vs-ephemeral instead of implying it.
30. **Searches are now first-class, tracked objects** (`engine/runner.py`; `runs`,
    `run_steps`, `run_results` tables). Pressing the button creates a run with a plain-English
    step checklist (one step per free source, then filter/score/briefs/etc.), each step
    writing status, item counts and seconds as it goes; the dashboard polls
    `/api/run/current` and renders a live checklist with an ETA computed from the *median of
    previous runs' step durations* (first run says so instead of guessing). On completion the
    top-of-funnel results are **frozen** into `run_results` with cohort ranks and an `is_new`
    flag, so "what did Tuesday's search show" has a permanent answer even after re-ranking —
    the exact complaint that started this round (Deep Dive picks vanishing overnight).
    `recover_interrupted()` marks phantom `running` rows failed on boot rather than leaving
    them stuck.
31. **`SEARCH_MODE=manual` is the hosted default**: the scheduler registers only
    housekeeping; ingest+scoring runs *only* from the dashboard button (or `auto` restores
    the brief's hourly schedule). This is both a spend cap on the model API and the
    behaviour the user asked for verbatim. Bootstrap still seeds an empty DB but explicitly
    does not search.
32. The old `_run_refresh` subprocess (progress = a tail of stdout at the end) is replaced by
    the in-process tracked runner above — the limitation entry below is retired.

## Known limitations (honest)

- Stage is often `unknown` from Form D (the form doesn't state a round name) → cohorts are
  `(sector, unknown)`; cohort sizes < 20 are flagged low-confidence rather than hidden.
- Sector-of-Tomorrow needs corpus breadth; with a thin corpus it renders an honest empty
  state rather than a fabricated trend (source-diversity gate ≥ 2).
- Founder table fills only from Form D related-persons and (with keys) enrichment — no
  invented founder data.
- The dashboard is **unauthenticated and bound to 127.0.0.1**. That is the right trade for a
  single-partner laptop tool; exposing it to a network needs auth in front of it (SSO or
  Tailscale), which is deliberately out of scope rather than half-done.
- Searches run in one background thread (`engine/runner.py`) — a second button press while
  one is running is refused with 409 rather than queued. Right for a single-team tool;
  a multi-tenant deployment would want a real job queue.
- On a laptop the service pauses during sleep. Catch-up covers a missed digest, but a 5-day
  holiday with the lid shut means five days of no ingest. That is the VPS path
  (`deploy/dealengine.service`, same code) and the honest reason to pay $5/month.

## Live-hosting round (why a search hung, and what now bounds it)

33. **The first hosted search stalled on one company for ten minutes.** The cause was not
    the provider being slow but a hidden multiplier: the OpenAI SDK defaults to
    `max_retries=2`, so the configured 120-second timeout was really 360 seconds per call,
    and `complete()`'s own rate-limit loop could stack four of those. One judged company
    could therefore cost 20+ minutes, and a 25-company pass would never finish. The client
    is now created with `max_retries=0` — retries belong in `complete()`, where they are
    bounded, logged and 429-aware — and the timeout is 75s, which is the real per-call
    ceiling.
34. **Reasoning models bill you in time for `max_tokens`.** Inkling given 8192 tokens will
    think for minutes; judging needs a verdict, not an essay. `max_tokens_by_stage` now caps
    classify at 700, score at 900, chat at 1200 and briefs at 2000.
35. **Circuit breaker.** After 3 consecutive provider failures every further call
    short-circuits to the loud `[STUB]` for the rest of the process, so a provider outage
    costs a run seconds instead of hours; one success closes it again. The stub text is
    unchanged — a degraded run still says so on every affected field rather than quietly
    presenting weaker output as normal.
36. **Searches are stoppable.** `POST /api/run/cancel` (a Stop button on the live step
    panel) halts at the next step boundary — or the next company inside the judging step —
    keeps everything already collected, and records the run as `cancelled`, not `failed`.
    Unfinished steps show "not run — search was stopped". `_clear_stale_running()` also
    means a crashed worker can never leave a phantom run blocking the button.
