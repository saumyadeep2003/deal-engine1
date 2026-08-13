# Deal Sourcing & Discovery Engine

A working deal-sourcing pipeline for **Thirdbase / 535 West Capital**: continuous ingest from
real free sources, entity resolution, deterministic filtering, cohort-relative scoring, a
self-maintaining nine-tab Excel pipeline, Mon/Wed/Fri digests, instant alerts, sector
detection, peer tracking, and a conversational interface — runnable end-to-end on a laptop
with **no Docker, no Postgres, no cloud accounts and no paid API keys**.

```bash
# One-shot demo (no install ceremony)
pip install -r requirements.txt
python demo.py                 # narrated end-to-end run, < 3 minutes
python chat.py                 # partner chat REPL
python tests/acceptance.py     # 19 acceptance criteria, after demo.py
python tests/gatekeeper_test.py # 22 anti-hallucination checks (recall + precision)

# Or deploy it as an always-on service with a partner dashboard
./deploy/install.sh            # LaunchAgent + venv + dashboard on :8787  (see DEPLOY.md)
./dealctl status               # funnel, licence posture, source health
./dealctl open                 # the dashboard
python tests/deployment.py     # 12 deployment criteria, against the live service
```

Three documents, three jobs: **README** (this file) is what the system is and why;
**RUNBOOK.md** is running it by hand; **DEPLOY.md** is running it always-on with the
dashboard, email and the live Google Sheet.

## The constraint that shapes everything (a feature, not an apology)

The fund's brief specifies PitchBook, Harmonic, Coresignal, Crunchbase, X and The
Information — all quote-based enterprise contracts. None are available for this build. The
system is therefore **architecturally complete while running entirely on free sources**:

| Real, live, free | What it provides |
|---|---|
| SEC EDGAR full-text search | Real Form D filings: issuer, CIK, dates, offering amounts, related persons |
| Hacker News (Algolia API) | Real funding/launch stories + real engineer commentary |
| RSS (TechCrunch, Axios, Newcomer, Stratechery, Not Boring, The Generalist, The Diff) | Real funding announcements + long-form analysis |
| arXiv API | Real research velocity for sector detection |
| GitHub API | Real stars / velocity |
| Reddit public JSON, careers pages | Commentary + hiring signals (best-effort) |

| Licence-gated (adapter wired, returns `LicenseRequired`) | Unlocks |
|---|---|
| PitchBook | valuations, full cap tables, deal history |
| Coresignal | headcount, 6/12-month growth, LinkedIn GP post feed |
| Harmonic, Crunchbase | firmographics, coverage density |
| X API (paid) | GP watchlist timelines (70 handles configured) |
| Blind, podcast transcripts, Substack threads, The Information | qualitative commentary |

Every licensed adapter implements the **identical `SourceAdapter` interface**, is registered,
scheduled, health-checked and tested. When a contract is signed, set one environment
variable (see `config/sources.yaml`) and rows start flowing — nothing else changes.

**Missing data stays missing.** A field a paid source would fill renders as
`— (requires PitchBook)` / `— (requires Coresignal)` — never interpolated, never estimated
silently, never filled by a model. The workbook is visibly incomplete rather than invisibly
wrong; that discipline is part of what is being demonstrated.

## Accuracy discipline

- **Real data only in outputs.** Every pipeline row traces to a fetchable URL (`signals.url`).
  The Provenance tab maps every Pipeline column to its source and freshness.
- **Signals are immutable.** Company state is derived; everything is reconstructable.
- **No LLM arithmetic.** Tier counts, growth, runway, ratios, percentiles: Python only.
- **Extraction, never recall.** Every prompt pins the model to retrieved context and
  requires null when the answer is absent; every structured-output field is nullable.
- **Citations enforced.** A brief containing a numeric claim without a `[S:signal_id]` /
  `[computed]` citation is rejected by validation, regenerated once, then flagged — never
  published.
- **Gatekeeper: every model sentence is traced before publication** (`engine/gatekeeper.py`).
  Citation *shape* is not evidence — an invented `[S:99999]` satisfies a regex perfectly,
  which is how a fabricated claim can look better sourced than a real one. So each sentence
  a model writes is resolved against the database on three axes: the cited signal must
  **exist and belong to this company**; every figure must **match a stored value** (1%
  tolerance, so `$12.5M` still matches a stored `12500000`); every named investor or firm
  must **appear in this company's evidence** — which is what stops "backed by Sequoia" when
  no such investment row exists. A sentence that fails is removed and replaced with
  `[REMOVED: claim could not be traced to a stored source]`; the rest of the brief still
  publishes, and a rating whose *entire* justification was removed is nulled with it rather
  than left as a bare score. Every removal is stored in `gatekeeper_events` and readable at
  `/api/gatekeeper` — the anti-hallucination claim is meant to be falsifiable, not trusted.
  Applies to briefs, the digest, and chat. Tests: `python tests/gatekeeper_test.py`
  (recall *and* precision — a filter that eats true sentences gets switched off).
- **Stub mode fails loudly.** Without the provider API key (`NVIDIA_API_KEY`), judgment fields read
  `[STUB: no API key — judgment unavailable]` — never plausible fake analysis.
- **Synthetic records** exist only for two mechanism demos (entity-resolution collapse,
  90-day staleness sweep), are named `DEMO-*`, flagged `is_synthetic`, amber-highlighted,
  and confined to the **Demo Cases** tab.

## Architecture (six layers, ordered by cost)

```
1 INGESTION        one adapter per source → common Signal shape (engine/adapters/)
2 ENTITY RESOLUTION signals → canonical companies (engine/resolution.py)
3 DETERMINISTIC     free rules remove the bulk before anything costs money (engine/filters.py)
4 ENRICHMENT        cached, survivors only (engine/enrichment.py)
5 SCORING           computed → judged → percentile within (sector, stage) cohort (engine/scoring.py)
6 FEEDBACK          partner actions logged against the feature vector (partner_actions)
```

**Entity resolution** — canonical key is the normalised domain; fallbacks: LinkedIn URL →
external UUID → fuzzy name scoped by sector/geo. ≥0.85 auto-merges (logged, reversible via
`resolution.unmerge`); 0.60–0.85 goes to `review_queue`; below creates a new record. A wrong
merge is worse than a duplicate.

**Relative ranking** — the primary output is the percentile within a `(sector, stage)`
cohort, never a bare score. Cohorts under 20 members are flagged low-confidence. The 60/40
dominant-tech/tactical split is applied at the ranking layer (`scoring.apply_focus_split`).

**Feedback loop** — every workbook edit and partner decision writes a `partner_actions` row
with the feature vector that produced the recommendation. Recalibration itself is stubbed
(documented in BUILD_LOG.md); the table and write path are live.

**The funnel is the cost model** — raw signals → deterministic filter → flash-tier
classifier → mid-tier structured scoring → ~8 flagship-tier briefs/day. Token spend is
logged per stage (`llm_usage`) and printed by `demo.py`.

**Nothing fails silently** — every adapter heartbeats (`sources.last_ok_at`); any source
quiet beyond 2× its interval, or erroring repeatedly, raises an alert (component 14).

## LLM configuration

Any OpenAI-compatible endpoint via `config/models.yaml` (every model name lives there,
never inline). Currently: **NVIDIA NIM** (`integrate.api.nvidia.com`) serving Thinking
Machines' **Inkling** across all four tiers; point the `classify` tier at a cheaper model
when one is available and the funnel economics improve with zero code change. Structured
output is treated as unreliable regardless of provider: fence-stripping → JSON parse →
Pydantic validation → one retry with the error appended → null + review flag.

```bash
export NVIDIA_API_KEY=...   # optional; without it the pipeline runs fully, judgment stubs loudly
```

## Outputs

- `output/deal_pipeline.xlsx` — Pipeline, Hot Deals, Watchlist, Sector of Tomorrow,
  Peer Set Activity, Co-investor Heatmap, News Worth Reading, Investor Commentary, Stale,
  plus Provenance and Demo Cases. Two-way sync: recommendation edits in the workbook are
  read back before regeneration — the human value wins and is logged.
- **Live Google Sheet mirror** (optional) — the same nine tabs, same two-way sync contract,
  so partners can read and edit from anywhere. One renderer, two destinations.
- `output/digests/` — Mon/Wed/Fri HTML digest, hard caps per section, honest empty sections.
  **Emailed via Resend** when configured; delivery success/failure is recorded on the
  `digests` row, never assumed.
- `output/alerts/` — instant alerts: 2+ Tier-1 co-invest, off-thesis move by a tracked firm,
  watched founder starts a company. Deterministic rules, rate-limited, deduplicated, emailed.
- `output/briefs/` — citation-validated company briefs (on-demand + auto above threshold).
- **Partner dashboard** at `http://127.0.0.1:8787` — funnel, sector ratios, the full
  pipeline, one-click provenance per company, on-demand thesis scan, chat, and a posture
  banner that always states what is stubbed or licence-gated.

## Configuration is the thesis

`config/thesis.yaml`: 11 investment themes with keywords, Tier 1/2/3 firm lists, stated
focus per firm (thesis-shift detection), GP watchlist (70 X handles), watched founders,
filter thresholds, scoring weights, digest caps. Partners edit YAML, not Python.

## Production deltas (documented, deliberate)

| This build | Production target |
|---|---|
| SQLite **or Postgres via `DATABASE_URL`** (Supabase — see `SUPABASE.md`) | PostgreSQL + pgvector |
| TF-IDF + cosine (numpy) | pgvector embeddings |
| APScheduler in one process, job names mapped 1:1 | n8n queue mode + Redis |
| **Resend (wired and live)** | same |
| **FastAPI + vanilla-JS dashboard, localhost** | same app behind SSO |
| **macOS LaunchAgent** (`deploy/`) | the included systemd unit on a VPS |
| CLI REPL + `/api/chat` | Slack Bolt |
| httpx + feedparser + BS4 | + Apify + Playwright |
| openpyxl + **live Google Sheet mirror** | Microsoft Graph API |

## Offline verification note

Adapters are live-first. When the demo box has no egress (e.g. a sandboxed environment),
`http_get` falls back to a snapshot cache of **real previously-fetched payloads** for the
same URL (`data/cache/`), and every signal records `fetch_mode` (`live` vs
`cached_snapshot`) so provenance is never ambiguous. On an open network the cache is
refreshed automatically. Snapshots ship with the repo so `python demo.py` produces a real
workbook anywhere; nothing in the cache is hand-written.
