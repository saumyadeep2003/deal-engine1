# Sharing the LIVE system — GitHub + Render (engine) + Vercel (frontend)

The interviewer gets **one Vercel URL that runs the complete flow live**: real ingest,
scoring, chat, on-demand thesis scans, provenance, decisions, briefs, the downloadable
workbook. Under the hood:

```
interviewer's browser ──> your-app.vercel.app        (frontend, static, instant)
                              │  /api/*  proxied by vercel.json rewrites
                              ▼
                          your-service.onrender.com  (the full engine: FastAPI +
                                                      APScheduler + SQLite + live ingest)
```

Why the split: Vercel is serverless and cannot run a background scheduler or keep a
database — the engine needs a real process. Render's free tier runs one. The rewrite proxy
means the browser only ever talks to the Vercel origin — no CORS, one URL to share.

Total setup: ~15 minutes, two free accounts (both sign in with GitHub), no card.

---

## Step 1 — push to GitHub (~3 min)

The repo is already committed locally.

1. <https://github.com/new> → name `deal-engine1` → **public** (Render free + Vercel free
   both deploy public repos without extra permissions; private also works) → do NOT
   initialise with a README.
2. From the `deal-engine/` folder:

```bash
git remote add origin https://github.com/YOUR_USERNAME/deal-engine1.git
git push -u origin main
```

Auth = your GitHub username + a Personal Access Token (Settings → Developer settings →
Tokens (classic) → `repo` scope), or `gh auth login`.

## Step 2 — deploy the engine on Render (~5 min)

1. <https://render.com> → sign up with GitHub (free, no card).
2. **New → Blueprint** → select your `deal-engine1` repo. Render reads `render.yaml` and
   proposes the `deal-engine` web service on the **free** plan. Approve.
3. It will ask for the env vars marked `sync: false`:
   - `NVIDIA_API_KEY` → paste your key (this turns on live LLM judgment for the demo)
   - `RESEND_API_KEY`, `DIGEST_TO` → leave blank unless you want hosted email
4. Deploy. First build ~3–5 min. Your URL appears as
   `https://deal-engine-XXXX.onrender.com` — **copy it**.
5. Open `https://deal-engine-XXXX.onrender.com/healthz` → `{"ok": true}`.

What happens on first boot (and after any restart): the disk starts empty, the engine
detects that, seeds itself, and runs a full **live** ingest+score pipeline in the
background — EDGAR, Hacker News, RSS, arXiv and GitHub are all fetched live from Render
(unrestricted network), so the interviewer sees fresher, fuller data than any snapshot.
The dashboard fills in over the first few minutes.

## Step 3 — deploy the frontend on Vercel (~4 min)

1. Point the proxy at your actual Render URL and commit:

```bash
python scripts/build_frontend.py --backend https://deal-engine-XXXX.onrender.com
git add frontend && git commit -m "frontend: point proxy at Render backend" && git push
```

2. <https://vercel.com/new> → sign in with GitHub → Import `deal-engine1`.
3. One setting matters: **Root Directory → `frontend`**. Framework: *Other*. No build
   command. Deploy.
4. You get `https://deal-engine-YOURNAME.vercel.app`. Open it, click around once.

`…/snapshot/` on the same Vercel URL serves the static baked demo as a belt-and-braces
fallback if Render is ever down.

## The free-tier honesty notes (also shown in the UI)

- **Cold start**: Render free sleeps after ~15 min idle. The first visitor sees a
  "engine backend is waking up" banner for up to a minute; the dashboard retries
  automatically. If you know when the interviewer will look, open the URL yourself a few
  minutes earlier and it will be warm.
- **Ephemeral disk**: a Render restart wipes the database; the engine reseeds and re-ingests
  itself with live data automatically. Partner decisions recorded on the hosted demo can
  therefore be lost on restart — the dashboard's provenance drawer and `partner_actions`
  still demonstrate the mechanism. (Persistent disk is one paid tier away, or the local
  install, or the systemd VPS unit — all documented in DEPLOY.md.)
- **Open access + rate budget**: there is no login (deliberate, for a frictionless demo).
  Expensive operations (chat, scans, refresh, briefs) have hourly caps far above human use
  so a crawler can't drain the NVIDIA quota; past the cap the API answers with an honest
  429, never a fake result.
- Delete the deployments after the interview (Render → service → Settings → Delete;
  Vercel → project → Settings → Delete) and rotate the NVIDIA key.

## What to send the interviewer

> **Live demo (complete flow, real data ingested live):**
> https://deal-engine-YOURNAME.vercel.app
> First load may take ~1 min if the free-tier backend is asleep — the page says so and
> retries itself. Try: click any pipeline row for one-click provenance, ask the chat
> "who's quietly investing in robotics?", run an on-demand thesis scan, download the
> nine-tab workbook.
>
> **Code:** https://github.com/YOU/deal-engine1 — `python demo.py` runs the full narrated
> pipeline locally in ~3 minutes with no keys; `tests/` verifies every claim in the brief
> (19 pipeline + 12 deployment checks).

## Keeping it in sync

Every `git push` redeploys both: Render rebuilds the engine, Vercel redeploys the frontend.
Refresh the fallback snapshot occasionally with
`python scripts/export_static_demo.py && python scripts/build_frontend.py --backend … && git push`.

## Security

- No keys in the repo — verified by grep before packaging; `.env` is gitignored. The
  NVIDIA key lives only in Render's env settings.
- The hosted data is public-source material (SEC filings, HN posts) plus computed scores.
- `X-Robots-Tag: noindex` is set on the Vercel frontend so the demo stays out of search.
