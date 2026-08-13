# Deploying — always-on on your Mac

One command installs a background service that runs the whole engine on schedule and
serves a partner dashboard at `http://127.0.0.1:8787`.

```bash
cd deal-engine
./deploy/install.sh
```

That script: finds a Python 3.11+, creates `.venv`, installs dependencies, creates `.env`
from `.env.example`, seeds the database on first run, renders and loads a **LaunchAgent**,
then waits until the service answers. Re-run it any time — it's idempotent.

Then:

```bash
./dealctl status     # funnel counts, licence posture, source health, last run
./dealctl open       # open the dashboard
./dealctl logs       # follow both log streams
./dealctl run        # trigger a full pipeline run out of band
./dealctl doctor     # environment + imports + live-source connectivity
```

## What runs, and when

One process (`serve.py`) hosts both the scheduler and the web app, so there is one SQLite
file, one log stream, one thing to start and stop. Job ids map 1:1 onto the n8n workflow
names in the production architecture.

**Search mode.** `SEARCH_MODE=manual` (the hosted default) disables every scheduled
search: nothing runs until someone presses **Run a new search** on the dashboard, which
also caps model-API spend. Only housekeeping (source health, log rotation) stays
scheduled. `SEARCH_MODE=auto` restores the full self-updating schedule below.

| Job | Cadence |
|---|---|
| `01_news_rss_ingest+02_sec_form_d` | every 60 min |
| `03_resolution+04_enrichment+05_scoring+06_briefs` | every 120 min |
| `07_commentary_harvester` | every 240 min |
| `08_peer_set_tracker` | every 240 min |
| `09_excel_writer+gsheets_sync` | every 120 min |
| `10_digest_assembly+email` | **Mon/Wed/Fri 07:00 local** |
| `11_instant_alerts` | every 30 min |
| `12_sector_detection` | every 12 h |
| `13_error_handler+14_source_health` | every 60 min |
| `log_rotation` | every 12 h |

## Sleep, and why a digest is never silently skipped

A laptop is not a server: closing the lid suspends the process, and macOS resumes it on
wake. Three things make that safe rather than lossy:

1. Jobs are registered with `coalesce=True` and a **6-hour misfire grace**, so runs missed
   during sleep collapse into one run on wake instead of being dropped.
2. `serve.py` runs a **digest catch-up** on every startup: if today is a digest day, the
   hour has passed, and no digest was sent, it sends immediately.
3. The LaunchAgent has `KeepAlive`, so a crash restarts the service within 30 seconds.

If you want it genuinely continuous, keep the lid open and set System Settings → Lock
Screen → "Turn display off on power adapter when inactive" to Never, or run
`caffeinate -s` while it matters. If it must run with the laptop shut, that's the VPS
path — `deploy/dealengine.service` is the systemd equivalent, same code, no changes.

## Four credentials (all optional)

Everything below is unset by default and the system runs fully without any of it, stating
plainly what is missing rather than faking it. Fill in `.env`, then `./dealctl restart`.

### 1. Model judgment — `NVIDIA_API_KEY`

Without it every judgment field reads `[STUB: no API key — judgment unavailable]`.
Computed scoring, cohort percentiles, tier counts, briefs' observed sections, the funnel and
the workbook are all unaffected — they never involved a model.

### 2. Email — `RESEND_API_KEY`

Sign up at <https://resend.com> (free tier: 100 emails/day, no card). Create an API key,
paste it, set `DIGEST_TO`.

```bash
./dealctl email-test          # sends a one-line test to DIGEST_TO
```

Free-tier caveat worth knowing before you demo it: with **no verified domain** you send
from `onboarding@resend.dev` and Resend only delivers to the address on your own account.
To email partners, verify a domain in Resend and change `DIGEST_FROM`. That is a config
change, not a code change.

Until a key is present, digests still render to `output/digests/` and the dashboard says so
in the banner. A delivery failure is recorded on the `digests` row (`delivered`,
`delivery_detail`) and pushed to `review_queue` — it never looks like a success.

### 3. Live Google Sheet — `GOOGLE_SERVICE_ACCOUNT_JSON`

Five minutes, once:

1. <https://console.cloud.google.com> → new project (any name).
2. APIs & Services → Library → enable **Google Sheets API** and **Google Drive API**.
3. APIs & Services → Credentials → Create credentials → **Service account** → create.
4. Open the service account → Keys → Add key → **JSON** → download it.
5. Move it somewhere private and point `.env` at it:
   `GOOGLE_SERVICE_ACCOUNT_JSON=/Users/you/.config/deal-engine-sa.json`
6. **Recommended:** create a sheet yourself at <https://sheets.new>, press Share and give
   the service account's `client_email` (it's in the JSON) **Editor** access, then set
   `GSHEET_ID` to the id in the sheet's URL — the part between `/d/` and `/edit`.
   Alternatively let it create the sheet (`GSHEET_TITLE` + `GSHEET_SHARE_WITH=you@gmail.com`).

**On Render** there is no `.env` file: add the key as a **Secret File**
(Environment → Secret Files → filename `gsa.json`, paste the JSON) and set
`GOOGLE_SERVICE_ACCOUNT_JSON=/etc/secrets/gsa.json`. If a host offers only environment
variables, paste the JSON *itself* into `GOOGLE_SERVICE_ACCOUNT_JSON`, or base64 it into
`GOOGLE_SERVICE_ACCOUNT_JSON_B64` — all three forms are accepted.

```bash
./dealctl sheets              # sync now, prints the sheet URL
```

Press **📗 Test Google Sheet** on the dashboard to run one real sync and see what Google
actually said. Three different problems all present as a bare `[403]` and the button tells
them apart:

| What you see | What it means | Fix |
|---|---|---|
| *"Google Drive API has not been used in project N… or it is disabled"* | The API is off in the Cloud project — step 2 was skipped or done in a different project | Enable **both** Sheets and Drive APIs, wait a minute, test again. Or set `GSHEET_ID`, which never touches Drive. |
| *"The caller does not have permission"* | The sheet was never shared with the robot account | Share it as **Editor** with the `client_email` the dashboard shows |
| *"storageQuotaExceeded"* | A service account has no Drive of its own, so it cannot *create* a sheet | Make the sheet yourself and use `GSHEET_ID` |

Because credentials being *found* and Google *accepting* them are different facts, the
dashboard reports both: a configured-but-failing sheet shows the cause, the fix and the
robot's address in the posture banner rather than reading as connected.

The sheet is a mirror of the same workbook renderer — one source of truth, two
destinations. The two-way sync contract is identical: the **Recommendation** column is read
back into the database before every push, the human value wins, and the disagreement is
logged to `partner_actions`. Without credentials the local `.xlsx` simply remains the only
copy and `sheet_sync` records `not_configured`.

### 4. Permanent storage — `DATABASE_URL`

Unset: a local SQLite file (`data/engine.db`) — perfect on a laptop, but **wiped on every
restart on ephemeral hosts** like Render's free tier. Set it to a Supabase Postgres
connection string and every search, deal and decision survives restarts and redeploys.
Full 5-minute setup: **`SUPABASE.md`**. The dashboard's posture banner and
`/api/summary` → `"storage"` always state which backend is live and whether it's durable.

## The dashboard

`http://127.0.0.1:8787` — bound to localhost on purpose: this is fund data on a laptop, not
a public site. It shows the funnel, the recommendation mix, emerging sectors with their
signal-to-consensus ratios, the full pipeline table, peer activity, the co-investor
heatmap, commentary, news, the stale queue and source health. Clicking any pipeline row
opens **provenance** — every signal with its real URL, fetch mode and date, plus the stored
feature vector, model version and prompt version. From there a partner can record
Pass / Watch / Deep Dive, which writes the feedback loop.

A banner across the top always states the current posture: stubbed judgment, unconfigured
email, unconfigured sheets. The tool is never quietly less capable than it looks.

Every chart has a table view, works in light and dark, keeps identity off colour alone, and
uses palettes validated with the dataviz validator (ordinal single-hue ramps; sectors are
nominal so they get one hue, not a value-ramp).

If you want to reach it from your phone on the same Wi-Fi, set `DEAL_ENGINE_HOST=0.0.0.0`
— but understand you are then exposing an unauthenticated dashboard to your local network.
Tailscale is the better answer.

## Verifying the deployment

```bash
python tests/acceptance.py     # 19 pipeline criteria (run after a pipeline run)
python tests/gatekeeper_test.py # 22 anti-hallucination checks (standalone, seeds its own DB)
python tests/deployment.py     # 13 deployment criteria (run against the live service)
```

The deployment suite checks the things the pipeline suite cannot: every endpoint answers,
the dashboard's funnel agrees with the database, licensed adapters report as licence-gated
rather than broken, email and sheets state *why* they aren't sending, the stub posture is
exposed through the API, chat answers the brief's three questions with citations, the
on-demand scan ranks companies, a partner decision persists and lands in `partner_actions`,
provenance returns real URLs, the workbook downloads as a real `.xlsx`, the scheduler is
live in-process honouring the configured search mode, and every search run is tracked
with live per-source steps and saved history (`/api/run/current`, `/api/runs`).

## Troubleshooting

| Symptom | Check |
|---|---|
| `install.sh` says Python 3.11+ needed | `brew install python@3.12`, re-run |
| Service won't answer | `tail -40 logs/engine.err.log` |
| `dealctl status` says not loaded | `./deploy/install.sh` again |
| Port already in use | change `DEAL_ENGINE_PORT` in `.env`, `./dealctl restart` |
| Sources show `degraded` | `./dealctl doctor` — the connectivity section tests each live source |
| Digest didn't arrive | `./dealctl email-test`; check `DIGEST_TO` and the Resend free-tier restriction above |
| Everything is empty after install | `./dealctl run`, wait ~60s, refresh |

## Removing it

```bash
./deploy/uninstall.sh    # unloads the service; keeps data/, output/ and .env
```
