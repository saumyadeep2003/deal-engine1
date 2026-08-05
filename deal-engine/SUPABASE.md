# Supabase — permanent storage for searches, deals and history

Without a database URL the engine stores everything in a local SQLite file. That is
perfect locally, but on free hosting (Render) the file is **wiped on every restart and
deploy** — which is why Deep Dive picks used to vanish overnight. Point the engine at a
Supabase Postgres and everything survives forever: every search, every deal it showed,
every decision you record.

The engine speaks to both backends with the same code — the switch is one environment
variable. (Under the hood, Postgres is taught the SQLite dialect via `db/pg_compat.sql`,
so the tested query behaviour is identical.)

## Setup — about 5 minutes, free, no card

1. Go to <https://supabase.com> → sign in with GitHub → **New project**.
   - Name: anything (`deal-engine`), Region: pick one near your Render region.
   - Set a **database password** and save it somewhere — you need it in step 3.
2. Wait ~1 minute for the project to provision.
3. Get the connection string: project → **Connect** (top bar) → **Connection string** →
   choose **Transaction pooler** (works best with hosted apps) → copy the URI. It looks
   like:
   `postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:6543/postgres`
   Replace `[YOUR-PASSWORD]` with the password from step 1.
4. In **Render** → your service → **Environment** → add:
   - `DATABASE_URL` = that full URI
   Save — the service redeploys.
5. Done. On first boot with the URL set, the engine creates its own tables (plus the
   SQLite-compat functions) automatically. Watch the dashboard's yellow note about
   "results are stored in a local file" disappear — that's the confirmation.

For local runs on your Mac, put the same line in `.env` if you want your laptop and the
hosted engine to share one database (they can — same data, both dashboards live), or
leave it unset locally to keep using the private SQLite file.

## What durability changes in practice

- **Search history is forever.** The Previous Searches panel accumulates across
  restarts, redeploys and git pushes. "What did the search on Tuesday show?" always has
  an answer.
- **Your decisions stick.** Pass/Watch/Top-pick calls recorded in the dashboard survive
  everything and keep winning over the tool's own ranking.
- **Deploys stop being destructive.** A git push no longer means a 10-minute rebuild
  from zero — the engine comes back up with all its data and only fetches what's new.
- **The interviewer sees an instantly-full dashboard** even right after a deploy.

## Free-tier notes (honest)

- Supabase free tier: 500 MB database — years of headroom for this workload (a search
  snapshot is a few KB).
- Free projects are **paused after ~7 days with no activity**; the dashboard would show
  a storage error until you resume it in the Supabase console (one click). If you're
  using the engine at all, this won't trigger.
- The connection string contains the database password: it lives in Render's env
  settings and your local `.env` only — both outside git. Never commit it.

## Verifying

Open the dashboard → the posture banner should have no storage warning, and
`/api/summary` shows `"storage": {"backend": "postgres", "durable": true, …}`.
Run a search, then redeploy the service (push any commit) — after it comes back,
Previous Searches still lists the run. That's the whole point.

## If the service fails to start with "prepared statement already exists"

Supabase's **Transaction pooler** (PgBouncer in transaction mode) gives each
statement a different server connection, so a server-side `PREPARE` from one
statement collides with the next and the app dies at boot with
`DuplicatePreparedStatement: prepared statement "_pg3_0" already exists`.

The engine disables prepared statements on connect (`prepare_threshold = None`),
which is the correct setting for any pooled connection and harmless on a direct
one — so this is already handled. If you ever see it again, that setting is the
thing to check. The Session pooler (port 5432) also avoids it, but the transaction
pooler is the better fit for a service that sleeps and wakes.

The engine also reconnects by itself if the pooler recycles the connection while
the service is idle, so a dashboard left open overnight keeps working.
