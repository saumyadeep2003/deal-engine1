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
37. **"Where this search looks" is now permanent, not only mid-run.** The live step
    checklist only existed while a search was running, so at rest the dashboard never
    answered the obvious question — what does this thing actually check? `/api/run/plan`
    returns the same step list with per-step medians from previous runs; the panel shows
    "8 public sources, 21 steps, takes about N — 10 more need paid licences and are
    switched off" with the full list one click away, and is replaced by the live checklist
    while a search runs. (A measured 0.0s step is real data, so the total must not treat it
    as a missing estimate and substitute 20s — that bug inflated the first estimate from
    36s to 276s.)
38. **The AI step was 50 model calls per search; it is now 10 or fewer.** Two calls per
    company (screen, then judge) × 25 companies, each on a reasoning model at 15-30s, plus
    2s pacing = 15-20 minutes while everything else took about a minute. Four changes:
    screening and judgment merged into ONE structured response (the screening question is
    answered from the same context, so asking twice bought only latency); `JUDGE_TOP_N`
    defaults to 10, since only the top few by computed composite ever become Deep Dive
    candidates and everything below still gets full deterministic ranking; the `classify`
    and `chat` tiers point at a small instruct model rather than a reasoning one; and a
    judgment is reused when the company's evidence fingerprint (signal count + newest
    signal id) has not moved since it was made. Measured with a stubbed provider: 10 calls
    cold, 0 on an unchanged re-run, exactly 1 when one company gained a signal.
39. **A tier naming a model the provider does not serve now falls back to the known-good
    model** instead of stubbing the run — config is partner-editable, so a typo there must
    not look like an outage.
40. **The stub marker states the real cause.** `[STUB: no API key …]` printed even when a
    key was set and the provider had merely timed out, which sends the reader hunting for
    the wrong problem. Now: no key, provider-did-not-answer, and provider-failing-repeatedly
    are three distinct messages.
41. **Source Health tells you how to switch a source on**: each licence-gated row shows the
    exact environment variable and what connecting it would add (CONNECTORS.md carries the
    honest availability picture — which vendors are self-serve, which are enterprise sales,
    and which have no public API at all).
42. **"128 calls, 128 stubbed" with no reason was the real bug.** The key was set and every
    call was failing, and the only way to learn why was to read the host's logs. `llm` now
    keeps the provider's own last error and classifies it into a cause a partner can act on
    — key rejected, model not served for this key, out of credits/rate-limited, answered too
    slowly, or could not be reached at all — surfaced through `/api/summary` and stated in
    the dashboard's posture banner. `POST /api/llm/test` (the "Test AI connection" button)
    makes one real call and reports the same verdict on demand, so diagnosis takes a click
    rather than a log hunt. Classification verified against all five error shapes.
43. **Supabase's transaction pooler killed the deploy, and the fallback didn't catch it.**
    Boot failed with `DuplicatePreparedStatement: prepared statement "_pg3_0" already
    exists` — psycopg3 auto-prepares after five identical executions, and `_migrate()`
    issues exactly that shape seven times, while PgBouncer in transaction mode hands each
    statement a different server connection. Fix: `prepare_threshold = None` on connect,
    correct for any pooled connection and harmless on a direct one. Two supporting fixes
    from the same incident: `_migrate()` now runs *inside* the guarded block, so a Postgres
    problem degrades to SQLite with a loud banner instead of exiting (the earlier safety net
    only covered connect + schema); and a dropped pooled connection is detected and
    reconnected once per statement, so a recycled connection costs a reconnect rather than a
    dead dashboard. Verified against real Postgres: 12 identical parameterised queries, a
    forced mid-flight connection close, and a full tracked pipeline run (43 companies,
    7 top picks) with zero tracebacks.
44. **Auditing one brief found four defects, three of them accuracy bugs.** (a) The brief
    claimed "100th percentile of 5 in cohort unclassified|series-b" and, four lines later,
    "No cohort assigned yet" — `_comparables()` bailed on a null sector while `score_all()`
    buckets those companies into an 'unclassified' cohort. Peers now come from the same
    cohort the percentile was computed in, labelled as the catch-all it is. (b) "Product
    traction" listed the funding announcement as traction, because the section ended with a
    dump of all recent signals; funding events are excluded there (they are already under
    Funding history) and the remainder is labelled "Other recent signals (mentions, not
    traction)". (c) A rank drawn from a cohort of five was presented as a Deep Dive with no
    caveat at the recommendation; low-confidence cohorts now carry an explicit "treat as a
    prompt to look, not evidence of relative quality" note. (d) Regenerating exposed the
    worst one: Pangram's commentary carried eight Hacker News comments from 2014-2015 about
    *pangrams the word game*. A company first observed in 2026 cannot have been discussed in
    2015, so commentary older than (first signal − 540 days) is now rejected at harvest and
    pruned from storage on every run — 32 false quotes removed from the existing corpus, the
    two genuine Pangram comments kept. This is the name-collision risk noted in item 12,
    caught in the act by reading real output.
45. **The easy test answered the wrong question.** `/api/llm/test` sent "reply OK" and came
    back in 0.8s, which looked like proof the model worked — while every real judgment was
    timing out. A trivial prompt on a reasoning model is nothing like a judging prompt. The
    diagnostic now supports `hard=true` (a judging-sized prompt, the only test that reflects
    the pipeline) and `model=` (probe any model without editing config and redeploying,
    turning a 4-minute deploy-and-hope loop into a 3-second question).
46. **Model routing is now measured, not assumed.** Probed live against the provider with a
    judging-sized prompt: `llama-3.1-8b-instruct` 3.1s (works), `inkling` 43.2s (works on the
    probe, but real company contexts are larger and were exceeding 75s), `llama-3.1-70b` and
    `llama-3.3-70b` both TIMED OUT at 75s — the free tier cannot serve the big models in
    time. Routing moved to 8b for every stage: real, cited, slightly simpler analysis beats
    both a [STUB] and a 13-minute search. The measurements are recorded in models.yaml so the
    next person sees why, and `fallback_model` retries once when the routed model fails for
    any reason (retired, mistyped, or busy) before stubbing. The one-line upgrade path back
    to a larger model is a config edit the day a faster endpoint is available.
47. **A fast model that returns all-nulls is worse than a stub.** With judging routed to 8b,
    the calls succeeded (10 companies, 60s, zero errors) but returned valid JSON with every
    field null — which a brief renders as "Founder quality: None/10 — n/a": output that
    looks like analysis and says nothing, the exact failure the accuracy mandate exists to
    prevent. Three changes: the JSON instruction now demands filled fields and states that
    an all-null response is not a valid answer; `_is_empty_judgement()` detects the case
    (a considered `is_venture_relevant: false` is a real answer, not an empty one); and an
    empty result escalates ONCE to `strong_model` (inkling — slower, but it fills the
    fields). If the escalation is also empty the judgement is dropped, so the brief says
    [STUB] rather than publishing nulls dressed as analysis.
48. **A score without evidence is worse than a null.** The first genuinely-filled judgement
    read "Founder quality: 8.0/10 — no prior exits or team information, but robotics
    experience", and elsewhere treated a CIK number as evidence of being funded. A confident
    number a partner might act on, resting on nothing, is exactly the failure mode this build
    exists to avoid. The judging prompt now carries an explicit scoring rule: a score must be
    earned by cited evidence, absent evidence returns null with a reason, and identifiers
    (CIK, filing ids) are not evidence of quality.
49. **Brief cap raised 8 -> 30.** The cap was sized for slow flagship-tier calls; judging now
    costs ~3s, and a cap of 8 left 22 of 30 pipeline companies with no brief — which reads as
    a broken tool rather than a deliberate budget.
50. **Times display in IST; storage stays UTC.** A database that mixes local times cannot be
    compared, sorted or migrated safely, so `now_iso()` is unchanged and only rendering is
    localised — `db.to_display()` in Python, a matching helper in the dashboard fed by
    `display_tz` from `/api/summary`, so one env var (`DISPLAY_TZ_OFFSET_MIN` /
    `DISPLAY_TZ_LABEL`) moves every timestamp in the product. A fixed offset rather than
    zoneinfo: correct for India (no DST) and immune to containers shipped without tzdata.
    Date-only values render without a clock label, because "25 Jul 2026 IST" reads like a bug.
51. **The brief was reformatted for the person who has to read it.** It opens with the call in
    plain words ("Worth a close look now"), then an At a glance table carrying the six things
    a partner checks first — each cell keeping its [S:n] or [computed] marker, so the table
    cannot become the place where uncited numbers hide. Section headings are now English
    ("Money raised", "Who has backed them", "Signs of traction") instead of schema names, the
    model's judgement is explicitly labelled as opinion rather than measurement, and a new
    "What this brief can't tell you" section states the gaps — including, when true, "no
    founder information found, team quality is unassessed". A reader who knows the shape of
    the hole reads the rest correctly; one who doesn't over-trusts it. (The cohort key's pipe
    character had to be escaped — "robotics|seed" silently split the markdown table cell.)
52. **A formatting improvement that never reaches existing briefs isn't an improvement.**
    After the layout rewrite, a search wrote 0 briefs: existing ones regenerate only when
    their judgement is stubbed, so every stored brief kept the layout it was born with.
    `FORMAT_MARKER` now detects briefs written by an older layout and rewrites them once
    (verified it does not loop: after the rewrite the marker is present, so the trigger
    clears). Rewriting exposed a second trap — regenerating without the judged dict would
    have silently DOWNGRADED briefs that had real analysis back to [STUB], so
    `_stored_judgement()` reads the judgement already persisted on the score row. And the
    acceptance suite caught the third: that fallback initially made key-less briefs stop
    saying [STUB], which breaks the core mandate, so reuse is now scoped to when the engine
    could actually produce a judgement today.
53. **Apify wired as a first-class adapter** (`engine/adapters/apify.py`), because the gap
    between free sources and enterprise contracts is exactly where a scraping platform earns
    its keep: ~$40/month against ~$40k/year. Three constraints shaped it. Amounts are parsed
    by the SAME deterministic regex the RSS/HN adapters use, so a scraped headline meets the
    identical evidence bar as a Form D filing and no model is ever asked what a number is.
    Actor runs are snapshot-cached like every other source, so an offline demo replays real
    previous output instead of producing an empty run that reads as "no deals found".
    Self-reported headcount from a company's own About page is stored at confidence 0.5 with
    source `apify:<actor>` — visibly weaker than Coresignal's measured figure — and when the
    site says nothing the field records why. LinkedIn and X Actors are deliberately NOT
    configured: both prohibit scraping in their terms, and the licensed routes for that data
    are already wired. Verified against simulated Actor payloads: a funding headline yields
    amount/stage/lead-investor and a resolved company, a non-funding result yields news with
    no invented company, and no token yields an empty result with an honest health state.
54. **Three defects found by reading one real emailed brief.** (a) The digest's "full brief"
    link was `../briefs/<slug>.md` — a relative *filesystem* path, which resolves to nothing
    in a mail client. Links that leave the app now use `PUBLIC_BASE_URL` (auto-derived from
    `RENDER_EXTERNAL_HOSTNAME` when hosted) and point at `/api/brief/<id>`, which now renders
    real HTML instead of raw markdown in a `<pre>` — a partner clicking from email gets a
    document, and a company with no brief yet gets an explanation rather than a bare 404.
    (b) Apify's `enrich_company()` existed but was never called by the pipeline, so headcount
    stayed `— requires Coresignal` no matter what was wired; enrichment now runs it over the
    top-ranked companies with a domain, capped at `APIFY_ENRICH_MAX` and logged when capped.
    (c) Worst: scraped search results were becoming *companies*. A live run created pipeline
    entries called `axios.com`, `linkedin.com`, `instagram.com` and "Best Defence Tech
    Startups (2026)", which then appeared as **comparables in a real brief**. A company name
    is now extracted only when the headline actually says a company raised money, and bare
    domains, publisher names and listicles are rejected — 11/11 on the real bad cases. A
    signal with no resolvable company is still stored with its URL: evidence without an
    entity is honest, an invented entity is not.
55. **The digest recipient is editable from the dashboard**, because "change who gets the
    email" should not require a redeploy and an env var. Stored in a new `app_settings`
    table rather than in `config/*.yaml`: YAML is the fund's stated intent and belongs in
    git, while a recipient is runtime state that must survive a restart and travel with the
    Supabase backup. The override wins over `DIGEST_TO`; clearing it falls back to the env
    var rather than to nothing. Addresses are validated on save (max 5) — a malformed value
    would otherwise fail silently at send time, hours later. The UI states the Resend
    free-tier trap *where the address is typed*, not in a doc: without a verified domain
    only the Resend account owner actually receives mail, so any other address is accepted
    here and then bounces. And the note shows the saved address even when sending is
    switched off, because otherwise a partner cannot tell whether their change was stored.
56. **Three free adapters replace what the paid vendors were bought for.** Rather than
    scraping X and LinkedIn (prohibited, and Actors break mid-demo), the substitutes are
    sources that exist to be read. `ats_boards`: Greenhouse/Lever/Ashby public endpoints
    give open roles and function mix — the Coresignal question ("is this team growing, in
    which functions") answered by a *leading* indicator rather than a headcount snapshot,
    and named `open_roles` so it is never confused for one. Velocity is computed from the
    engine's OWN dated observations (one immutable signal per company per day), so a trend
    is something the system measured, and a first reading honestly says "a trend needs two
    runs". `bluesky`: the AT Protocol public appview needs no key and no permission, and
    the watchlist is handle-based exactly like the X adapter, so an X budget later changes
    nothing structural — with the honest caveat that Bluesky's investor population is
    smaller, so this is real GP signal, not equal coverage. `wayback_team`: distinct profile
    links on *archived copies of a company's own team page* give team growth with two
    citable URLs; confidence 0.4 and the caveat travels with the number, because a redesign
    skews it. Testing caught two bugs: "ML Research Scientist" classified as engineering
    (function order now puts research first) and `www.` domains yielding no board slug.
57. **Gatekeeper: the citation validator was checking punctuation, not truth.** The existing
    `validate_brief()` asked whether a numeric claim carried a citation *marker*. Handed the
    sentence "backed by Sequoia and Benchmark [S:99999]" it returned zero violations —
    signal 99999 does not exist, no such investment row exists, and the fabrication passed
    precisely *because* it was well formatted. A hallucination that cites is more dangerous
    than one that doesn't. `engine/gatekeeper.py` resolves every model sentence against the
    database instead: the cited signal must exist AND belong to this company (borrowing a
    real id from another company is the failure a shape check can never see), every figure
    must match a stored value to 1% (so `$12.5M` still matches a stored `12500000` — the
    engine's own rounding must not read as invention), and every named firm must appear in
    this company's evidence, checked against the `investors` table plus a built-in list of
    well-known funds, since firms get invented far more often than they get misremembered
    and carry the most weight with a reader.
    Enforcement is per sentence: the offending sentence is dropped and replaced with a
    visible marker, and the rest publishes. Killing a whole brief over one bad clause pushes
    people back to reading raw signals, which is worse. Three details came out of testing.
    (a) The audit footer originally quoted the removed text back — which put the invented
    figure and its fake citation straight back on the page, where the citation validator
    then correctly failed the brief; the footer now names categories only and the wording
    lives in `gatekeeper_events`. (b) A rating whose *entire* justification was removed used
    to render as "7/10 — [REMOVED…]": a confident number with its reasoning deleted, which
    is the exact failure mode the module exists to stop, so the score is now nulled with its
    reasoning. (c) Precision is tested as hard as recall — labelled opinion (`8/10`, "6
    years"), honest statements of absence, and true sourced sentences must pass untouched,
    because a filter that eats true sentences gets switched off within a week.
    Deliberately NOT policed: labelled judgement. "Founder quality: 7/10" in a section
    headed "What the AI makes of it" is an opinion the page already marks as one — the job
    is stopping invented facts, not stopping the model from having a view. Removals are
    counted on the dashboard and readable in full at `/api/gatekeeper`, so "nothing
    unsourced is published" is falsifiable rather than asserted. 22 tests.
58. **Two integrations that reported success while failing.** The Excel download answered
    `{"detail":"workbook not generated yet"}` on the hosted engine while the dashboard above
    it showed 498 companies and 43 briefs. The rows were never missing — the database is
    durable (Supabase) but `output/` is not, so the workbook, a pure build artefact written
    by the pipeline and served straight off disk, vanished on every deploy, restart and idle
    spin-down. The fix is to stop treating the file as an output and start treating it as a
    cache of the database: `ensure_workbook()` rebuilds it when it is absent, or when a score
    has been written since it was made, so the download can never disagree with the dashboard
    above it. Locked in as a regression: D11 now **deletes the file first** and requires the
    download to work anyway.
    Google Sheets had the same shape of bug one level up. `status()` reported
    `configured: true` because a key file was found at `/etc/secrets/gsa.json` — and every
    sync had been failing for a day with `[403] Google Drive API has not been used in project
    819252726443`. Credentials being *found* and Google *accepting* them are different facts,
    and only the first was being reported. Three unrelated problems all present as a bare 403
    — an API switched off in the Cloud project, a sheet never shared with the robot account,
    and a service account having no Drive storage of its own so it cannot *create* a sheet —
    so `diagnose()` maps each to its cause, its fix and a direct console link (pulling the
    project number out of Google's own error text, since that is the number the link needs).
    The dashboard now shows a configured-but-failing sheet as failing, with the robot's
    `client_email` — the address nobody can find when they need it — printed where the
    problem is described. A **Test Google Sheet** button runs one real sync on demand, the
    same pattern as Test AI connection. Two smaller fixes fell out: `GSHEET_ID` now takes the
    Sheets-only path (opening by title goes through Drive, which is the API that was off),
    and credentials may be given as a file path, inline JSON, or base64 — "put this JSON
    somewhere on disk first" is exactly where a working key turns into an unconfigured
    integration on hosts that only offer environment variables.
59. **Three complaints, one cause: the running service was not the code.** The user reported
    no Sheets test button, a broken Excel download and dead email links. All three were true
    and none was a code fault: the hosted build predated the fixes. The repository folder on
    the Mac still held the 6 August tree — `engine/gatekeeper.py` and every new adapter
    absent, `briefs.py` back at its original version — while commits carrying the *right
    messages* were being pushed on top of it. A stale zip was being re-applied (browsers do
    not overwrite a download; they add " (1)"), so each push re-committed the same old tree
    under a new name. Nothing in the product could distinguish "this feature is broken" from
    "this feature was never deployed", which is the distinction that has to come first, so
    `/api/version` now reports the build's commit and probes each capability **by import** —
    a marker is true because the code answered, not because a constant claims it. An
    incomplete build says so at the top of the dashboard in the loudest terms on the page,
    because every other number on it is meaningless if that one is wrong. Delivery moved off
    the zip entirely: files are written to the Mac directly and verified by hash.
60. **Connections: test buttons for everything, because passive health lies by omission.**
    A source read "ok" if the last scheduled run happened to succeed and an integration read
    "configured" if a credential was found — both inferences from history, and both wrong at
    least once here. `engine/connections.py` catalogues every dependency (each model in
    models.yaml individually, the keyed services, all 22 sources) and tests each with one
    real request on demand. Details that matter: models are probed with a judging-sized
    prompt, since a two-word ping once passed in 0.8s while every real judgement timed out;
    licensed sources report `license_required` as a PASS, because an adapter waiting on a
    contract is not broken; adapters answering from the offline snapshot cache say so rather
    than claiming to be live; the email test does NOT send mail, because a diagnostic that
    spams a partner's inbox on every press is its own bug; and Test-everything runs
    sequentially, since firing twenty live calls at once to make a dashboard feel responsive
    is how you get rate-limited by the sources you depend on. Expensive adapters override
    `probe()` — ats_boards asks the three providers for a deliberately nonsense slug, so a
    clean 404 proves reachability without depending on any company's board existing today.
61. **An email link that dead-ends teaches a partner the engine is broken.** The digest links
    every top pick, but briefs are capped per day, so most links hit
    `{"detail":"no validated brief for this company yet"}` — raw JSON, which reads as a blank
    page. The engine held plenty on those companies; it simply had not written the page. The
    brief is now generated on arrival: the daily cap governs what the engine spends
    unprompted, and someone who clicked a link has asked. If generation fails, the page shows
    every stored, sourced piece of evidence rather than an apology.
62. **Two faults the connections panel exposed within minutes of going live — both mine.**
    First, the panel's 29 probes were all charged to the `llm_test` budget (20/hour), which
    exists to stop a crawler draining the model quota. One press of "Test everything" spent
    the hour and the remaining rows answered "rate limited": a diagnostic that breaks when
    used as intended is worse than no diagnostic. Budgets are now priced by what a probe
    actually costs — a model probe spends tokens and stays tightly capped; a source probe is
    an ordinary HTTP request and gets its own generous allowance.
    Second, the very first *successful* Google Sheets sync returned
    `[429] Quota exceeded for 'Write requests per minute per user'`. Eleven tabs were costing
    five write calls each — clear, resize, update, freeze, format — which is fifty-five
    requests against a sixty-per-minute quota, and over a minute of round trips. The whole
    workbook is one payload and is now sent as one: a batched clear plus a batched update, so
    a refresh is two write calls no matter how many tabs the workbook grows to. Resize only
    fires when the data outgrew the grid, header styling is applied when a tab is born rather
    than re-sent every sync, and a 429 is retried with backoff because a per-minute quota is
    a "wait", not a "no" — failing the sync and telling the user their credentials are broken
    would have been the third wrong diagnosis of the same afternoon.
63. **The engine was collecting hiring data and throwing it away.** `ats_boards` had been
    storing real `open_roles` signals for days, and `hiring_velocity()` / `team_trend()` were
    dead code — nothing read them. Every brief still printed "Headcount / 6-month growth: —
    (requires Coresignal)" for companies whose job board the engine had read that morning,
    and the workbook's Headcount column did the same. Data gathered and never surfaced is
    worse than data never gathered: it costs the requests and teaches the reader the system
    knows less than it does. `engine/hiring.py` is now the single place the rest of the
    system asks, and it feeds the At-a-glance table, the team section, the "what this brief
    can't tell you" list (which no longer claims hiring is missing when it is not) and the
    two Excel columns. The licence gap is still stated — open roles are a *leading* indicator
    and not a headcount, and the wording says so wherever the number appears.
64. **Coverage was frozen at ten companies and it looked like a working system.**
    `run_judged_scoring` took the top ten by composite, then checked the cache — and because
    those ten already had valid judgements, all ten were served from cache. Ten model calls
    of headroom went unused every single search while a hundred and fifty companies were
    never judged at all. The fix inverts the order: reuse every valid judgement first (free),
    then spend the budget on the highest-ranked companies that do NOT have one. Each search
    now advances coverage by up to `JUDGE_TOP_N`, re-judging happens automatically when a
    company's evidence fingerprint changes, and the log line reports coverage as a fraction
    with the number still waiting.
65. **A cap nobody can see is indistinguishable from a bug.** `/api/coverage` reports, per
    stage, how many of the companies that COULD have something actually do — and names the
    setting that limits it. "Ten companies have an AI assessment" reads as broken; "ten of a
    hundred and sixty, capped by JUDGE_TOP_N=10, advancing by ten per search" reads as a
    dial. One detail mattered more than it looks: the per-company counts have to be scoped to
    companies still in the pipeline, because evidence rows outlive the companies they belong
    to — unscoped, enrichment reported "43 of 36 = 119%", and a single impossible number
    discredits every other row in the table beside it.
66. **Bluesky returned zero for its whole first run, and the reason was in the query.** It
    searched the theme LABEL — "Robotics & Physical AI funding round" — a phrase written for
    a fund's own documents that nobody has ever posted on a social network. It now searches
    the short keywords each theme already carries for the deterministic filter, which are the
    words people actually use ("robotics raised", "synthetic data seed round"). ats_boards
    went 25 -> 60 companies and wayback 10 -> 25: both are free, key-free endpoints where the
    only cost is wall-clock, and the old caps were why hiring data existed for a handful of
    companies and "requires Coresignal" for everyone else.
67. **Sector detection rebuilt after reading its own output.** 526 rows had accumulated because
    every run inserted every cluster and nothing ever deduplicated: the same trend appeared
    two or three times under slightly different model-written names. Clusters now carry a
    fingerprint (their defining terms, order-independent) and are updated in place, so the
    table is the current picture rather than an append-only log. Three quality fixes came out
    of looking at what it had actually produced. The top cluster was "Cloudflare AI Platform
    Software" — TF-IDF had found documents that mention a vendor, which is a topic and not a
    market, so vendor and product names are now barred from labels (they may still hold a
    cluster together, they just cannot name it). "Company Acquisition Deals" was an *event*
    type recurring across M&A headlines, so event words are barred too. And a cluster with
    zero mainstream documents was ranking top on a ratio that had collapsed to raw volume;
    consensus must now be measured for a ratio to be reported at all, otherwise the row says
    so and sorts below every real lead. Company-to-cluster matching went from one shared stem
    to three — one term had put a robotics company under a Cloudflare AI heading, which is the
    kind of association that makes a partner distrust the whole panel.
68. **The description column was quoting the article that found the company.** `companies.
    description` took whatever `summary` arrived with the creating signal, so a company first
    seen inside a funding round-up was described by the round-up: leadmagic.io read *"19
    Series A Cybersecurity Startups That Raised $626M · Escape · Qevlar AI…"* in a
    partner-facing column. Listicle summaries are now rejected at creation, and
    `engine/profile.py` rebuilds the field from the one source that can only be about this
    company — its own website. The model writes two or three sentences and a product list
    from the scraped text, then the gatekeeper checks every sentence back against that text
    and drops product names the site never prints. No site read, no profile: a named absence
    beats a paragraph assembled from press coverage, because a partner reading "what they do"
    is entitled to assume it came from the company.
69. **Founder coverage was 0% because of a cap set for a budget that does not exist.** Form D
    detail XML is the only place a filing names its people — and `max_detail_fetches` was 15
    per run, so 205 of 220 filings never had theirs read. The consequence was not cosmetic:
    `judge._context()` builds its evidence from the `founders` table, so every company was
    assessed for founder quality — the assignment's first criterion — with **zero founder
    evidence in the prompt**. SEC asks for a descriptive User-Agent and under 10 requests a
    second; it does not charge. The cap is now 150 and configurable, and `engine/people.py`
    syncs related persons into `founders` before the judge runs, recording what the filing
    actually said: a director is not promoted to founder because it would read better, and a
    fund's officers are never written onto a startup's team.
70. **Three of the nine investment criteria were permanently blank.** Valuation, growth and
    runway all read "requires PitchBook" — true of the *measured* figures and useless as an
    answer. `engine/estimates.py` computes each from what is observable, and every estimate
    carries three things it must never be separated from: a range instead of a point, the
    arithmetic that produced it, and the word estimate. Valuation is the round size over the
    stage's ordinary dilution band. Growth is the change in open roles, labelled hiring
    appetite and explicitly *not* the revenue growth the criterion asks for. Runway refuses
    outright without team evidence, because dividing a real round size by an invented
    headcount produces something indistinguishable from a real estimate. Two rendering bugs
    surfaced immediately: the growth target printed as "≥ 0.4% YoY" (the config stores a
    fraction) and the tier-1 target printed as "[3, 4]" — both in the row a partner reads to
    decide whether a company clears the bar.
71. **The engine as an MCP server — the hybrid, tested without spending anything.** The
    request was to try the hybrid architecture while leaving the hosted API untouched and
    buying nothing, so `mcp_server.py` is a second entry point onto the same engine rather
    than a second service: `web/api.py` does not import it, `requirements.txt` does not carry
    its dependency (it lives in `requirements-mcp.txt`), and Render never runs it. Deleting
    the file would not change the deployment — verified by diffing every deployed path after
    the change and finding it empty.
    Two decisions make it more than a REST mirror with different punctuation. The tools are
    shaped like *questions* — `investor_activity`, `thesis_scan`, `emerging_sectors` — rather
    than like endpoints, because a model choosing between thirteen verbs it understands picks
    correctly far more often than one composing thirty routes. And every payload carries its
    own caveats: `commentary` states that an empty result means "not found in free sources"
    rather than "nobody is talking about them", `pipeline_search` states that its ranks are
    cohort-relative, a cluster with unmeasured consensus states it is volume rather than a
    trend. That is deliberate. Everywhere else the discipline is enforced by code — the
    gatekeeper drops an unsourced sentence, the validator refuses an unresolvable citation —
    but a conversation has no such mechanism, and the model is free to summarise. Putting the
    caveat inside the payload is the only place it survives into the answer, and the tests
    assert those sentences are present.
    One bug caught by the suite: `search_progress` queried a `step` column that does not
    exist (`run_steps` uses `key`/`seq`), which surfaces to a model as an opaque tool failure
    — and a model's usual recovery from an opaque failure is to answer from memory, which is
    precisely the failure mode this system exists to prevent.
72. **Phase 1 of the completeness roadmap: discovery stops being keyword-shaped.** Four
    changes, one theme — the engine now sees channels whole instead of sampling them.
    (a) The EDGAR adapter sweeps the DAILY FORM INDEX: every Form D filed, parsed locally,
    with the engine's own deterministic filter deciding relevance. The twelve keyword
    searches survive as a safety net (they reach further back than the index window) and
    `dedupe_key` makes the overlap harmless. This closes the architectural inversion where
    a filing that didn't contain one of our phrases did not exist to the system.
    (b) `company_news`: a standing Google News RSS watch per tracked company — tracking,
    not re-discovery. Names are quoted plus funding-context terms, and generic names
    ("Text", "Built") are refused outright, because wrong news attributed to a tracked
    company misleads a partner worse than a missed article.
    (c) Press-release wires (PRNewswire, GlobeNewswire, Business Wire) join the news feeds —
    funding announcements at the source, hours ahead of aggregators.
    (d) `companies_house`: the UK statutory registry, free API key, officers with roles and
    appointment dates. Two disciplines: registry matches are exact-normalised only (fuzzy
    matching a five-million-entity registry is how a stranger's board lands on a pipeline
    company), and officers are emitted in the `related_persons` shape so
    `people.sync_from_filings()` ingests them with no new code path — one pipeline for
    people regardless of which registry named them.
73. **Run 18's briefs and publish steps both died on one line, and the line was mine.** The
    website adapter stores `customer_logos` as a dict (`{"names": [...], "evidence": ...}`);
    `profile.source_text` sliced it — `p["customer_logos"][:10]` — which on Render's Python
    3.12 raises `KeyError: slice(None, 10, None)` (slices became hashable in 3.12, so a dict
    lookup fails instead of a TypeError). The profiles step survived because backfill
    swallows per-company errors — reporting "0 written" — while briefs and publish, which
    reach the same code without that guard, took the whole step down. Two fixes with
    different jobs: the shape is now read correctly (and defensively), and the public
    renderers in `hiring`/`profile` no longer raise at all — a decoration on a brief must
    degrade to the honest gap, not cost a run its remaining 160 briefs. Regression test
    seeds the exact live payload shape and runs both failing paths.
74. **Apollo enrichment, scoped to Deep Dive only.** A live test through the user's own
    Apollo account returned, for one credit, the exact fields this engine stamps "requires
    Coresignal" — headcount, the 6/12/24-month growth series, a department-level function
    mix — plus a four-round funding history with investors and news URLs. The adapter
    enriches ONLY companies whose current call is Deep Dive (partner override included),
    skips anything enriched within 30 days, and hard-caps credits per run: a data budget
    spent evenly across 347 companies is a budget spent mostly on companies nobody reads.
    Nothing new downstream: funding history is emitted as ordinary funding_event signals so
    the existing ingest path builds rounds and tier-matches investors; headcount and growth
    land in the same enrichment_cache fields the workbook and briefs already read. Only USD
    amounts are trusted into amount_usd, and the probe uses the zero-credit auth endpoint —
    whether a free-plan key reaches the enrich endpoint is answered by the first run, not
    assumed.
75. **Three ways a model verdict reached a partner — or deleted a company — with nothing
    having checked it.** All three were found by auditing the AI-assessment path rather
    than by anything failing, which is the point: each one produced output that looked
    exactly like output from a working system.
    (a) *Escalation only fired on the failure that announces itself.* A Deep Dive
    candidate was judged by the 8b model and escalated to `strong_model` only when the
    answer came back with every field null. All-nulls is loud and easy to catch. A
    fluent, confident, wrong assessment is neither, and it is the one a partner acts on —
    escalation had never once fired for it, because there is nothing in the response to
    fire on. Deep Dive candidates now go to the strong model FIRST. Candidacy is read
    from the current recommendation (a partner's own override outranking the computed
    call), which puts the routing one run behind — `score_all` writes recommendations
    after judging — and that is deliberate: predicting the recommendation from the
    computed composite before the cohort percentiles that define it exist would route on
    a number that is not the number the partner reads. The same deliberate lag already
    governs Apollo enrichment (74). A stored fast-model judgement no longer satisfies a
    Deep Dive company either, or "always strong" would have applied only to companies
    promoted before they were first judged and to no one else.
    (b) *One cheap model could delete a company on its own say-so.* `is_venture_relevant:
    false` sets `status='filtered'`, and it was the only irreversible unverified model
    action in the system — every other model output is a number or a sentence that a
    reader can discount, while this one removes the company from every view before anyone
    sees it. It now needs two models to agree. The strong model's own rejection stands
    (it IS the second opinion); a fast-model rejection is put to the strong model, and
    if that one disagrees, cannot answer, or is not configured, the rejection is recorded
    unconfirmed, the company keeps its status, and a review_queue row says who said what.
    When the strong model overturns a rejection its own answer becomes the judgement
    rather than a bare veto — it has just produced a full assessment of a company the
    fast model was about to delete. The failure direction is chosen: a wrong company left
    in the pipeline costs a partner a glance, a right one dropped costs everything and is
    invisible, because nobody ever sees what was filtered. Disputes are fingerprinted
    against the evidence that produced them, so the same argument is not re-run (or
    re-queued) every search, and new evidence reopens it automatically.
    (c) *The judgement cache was keyed on a question narrower than the one being asked.*
    `count:max_id` over the signals table answers "has a new signal arrived?" — but the
    prompt is built by `_context()`, which also carries founders, the company's sector,
    stage and HQ, and the payload of every signal. Founders synced out of a Form D (69),
    a profile written from the company's own website (68), a corrected sector, an
    in-place payload edit: all of them change what the model is shown and none of them
    move a row count or a maximum id. So the company whose founders were finally read
    kept the judgement made when its prompt said nothing about its team, and displayed it
    as a current assessment. The key is now a hash of the exact context string, which is
    the thing the cache is actually about — one extra local context build per company per
    run, against a model call. Old `count:max_id` keys are still honoured while they
    match, so the deploy that fixes this does not invalidate every stored judgement at
    once and collapse coverage to zero on restart; each company upgrades to the new key
    the next time it is judged.
    `tests/judge_verification_test.py` (35 checks) scripts the models per company rather
    than through one shared queue — a single queue silently misaligns the moment one
    company makes two calls and another makes one, and then the test is measuring the
    queue instead of the routing. Three new `/api/version` markers so a deploy can prove
    it took: `strong_model_for_deep_dive`, `verified_rejections`,
    `context_evidence_fingerprint`.
76. **Run 20 read back from the live deployment: four ways real output was being thrown
    away.** The user's complaint was "briefs are missing information" — and the deployed
    service's own diagnostics named every cause. (a) 68 review_queue rows, dominant
    pattern: the 8b model returns `tam.assumptions` as one string ('revenue run-rate'),
    the schema wants a list, and the ENTIRE judgement — every score, every reasoning
    sentence, the model spend that produced it — was discarded over a distinction no
    reader cares about. A `field_validator(mode="before")` now coerces string to list
    (splitting on semicolons/newlines); nulls stay null. This was the single biggest
    reason AI-assessment coverage sat at 22/347 while every search spent a full
    JUDGE_TOP_N=40 budget: the same top-ranked companies failed the same way every run
    and were re-attempted every run. (b) 'no parseable JSON found' where the model echoed
    the SCHEMA before the answer: the greedy first-to-last-brace match swallowed
    schema+answer together and parsed as nothing. _extract_json now falls back to
    scanning balanced objects and returns the last one that parses and is not
    schema-shaped; a pure schema echo now fails fast into the retry instead of
    'validating' into an all-null judgement. (c) Every escalation to inkling timed out at
    the shared 75s ceiling (38 stubbed score calls on the live box), fell back to 8b —
    and the answer was then LABELLED inkling, because the silent fallback inside
    _raw_complete was invisible to the caller. Two fixes: `strong_model_timeout_seconds:
    150` gives the reasoning model alone a longer leash (a shorter value than the default
    is ignored — the setting extends patience, never cuts it), and llm.last_model_used()
    lets the judge label a judgement with the model that actually spoke. The label is
    load-bearing twice over: the cache re-judges a Deep Dive company whose judgement
    came from a weaker model, and a rejection 'confirmed' by the fallback answering in
    the strong model's place is the rejecting model class agreeing with itself — that
    now counts as unconfirmed, and says so. (d) The alerts step died on Postgres with
    `syntax error at or near "$2"`: `company_id IS ?` is SQLite's null-safe equals and a
    Postgres syntax error — invisible to every sqlite-backed test, fatal on the Supabase
    deployment. It is now two dialect-free queries branched on None.
    `tests/llm_robustness_test.py` (14 checks) pins all four, including the live
    review_queue payload verbatim.
