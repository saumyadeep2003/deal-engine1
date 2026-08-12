# Connecting the paid data sources

Every source in this engine is already wired end to end: adapter, health tracking,
provenance, and the workbook columns that depend on it. What separates a live source
from a switched-off one is **one environment variable**. Nothing needs to be coded.

Set the variable in Render (Environment tab) or in your local `.env`, restart, and the
source starts contributing on the next search. The dashboard's Source Health table shows
the variable name next to every switched-off source, and the fields that depend on it
stop reading `— (requires X)`.

## The honest availability picture

This matters more than the mechanics: **most of these are not self-serve.** Six of the
ten require a sales conversation and an annual contract, typically in the thousands to
tens of thousands of dollars. Two have no public API at all. Presenting them as "just add
a key" would be dishonest, so here is the real state of each.

### Buyable today, by yourself, with a card

**X (Twitter) — `X_BEARER_TOKEN`.** The Basic tier at <https://developer.x.com> is around
$100–200/month and is enough to poll the 70 GP handles in `config/thesis.yaml`. This is
the single highest-value connection for this engine: what a tracked GP posts about is the
earliest sector signal that exists, well before a filing. If you connect exactly one paid
source, make it this one.

**Crunchbase — `CRUNCHBASE_API_KEY`.** API access sits on the paid tiers (roughly
$500+/month at time of writing, quoted annually). It adds funding rounds and investor
lists beyond what SEC Form D discloses. Useful, but overlaps heavily with what the engine
already gets free from filings and news.

**Podcast transcripts — `PODCAST_API_KEY`.** Depends on the provider you pick; several
transcript APIs have usage-based pricing in the tens of dollars per month. Adds investor
commentary from podcast appearances.

### Enterprise sales only

**PitchBook — `PITCHBOOK_API_KEY`** (full funding history, valuations, complete cap
tables), **Coresignal — `CORESIGNAL_API_KEY`** (headcount and 6-month hiring growth, which
feeds the runway estimate), **Harmonic — `HARMONIC_API_KEY`** (company and founder graph
with strong early-stage coverage), **Dealroom — `DEALROOM_API_KEY`** (European coverage),
and **The Information — `THEINFORMATION_API_KEY`** (scoop-level reporting). These are
annual contracts negotiated with a salesperson; a fund typically already has PitchBook and
would simply hand you the key.

### No public API exists

**Blind — `BLIND_API_KEY`** and **Substack threads — `SUBSTACK_API_KEY`** have no
official API. The adapters exist so that the day a licensed data partner or an approved
scraping arrangement appears, the plumbing is done. Until then they honestly report
`LicenseRequired` rather than scraping in violation of terms of service.

## What is already live and free

SEC EDGAR Form D filings, RSS news (TechCrunch, Bloomberg, FT, Reuters and others), Hacker
News, arXiv, GitHub, Reddit, company careers pages and company websites. Eight sources,
no keys, no cost — and they are where every number in the current dashboard comes from.

## Two keys that are worth setting and are cheap or free

**`NVIDIA_API_KEY`** switches on the AI judgment (founder quality, moat, TAM, thesis
narrative). Without it those fields read `[STUB: no API key — judgment unavailable]`.
Free tier at <https://build.nvidia.com>.

**`RESEND_API_KEY`** switches on digest email delivery. Free tier at <https://resend.com>
is 100 emails/day. Note the free-tier restriction documented in DEPLOY.md: without a
verified domain, Resend only delivers to your own account address.

**`DATABASE_URL`** is not a data source but belongs on any list of things to set — see
SUPABASE.md. Without it, hosted deployments lose their history on every restart.

## How to verify a connection worked

After setting a variable and restarting, open the dashboard and look at Source Health.
The source should move from 🔒 *needs paid subscription* to ✔ *working* with a recent
"Last check" time and a rising item count. If it stays switched off, the variable name
is wrong or empty — the table prints the exact name the engine looks for.

The columns that were showing `— (requires Coresignal)` or `— (requires PitchBook)` will
fill in on the next search. Nothing else changes: the same filter, the same scoring, the
same provenance rules. More evidence in, better-supported judgments out.

## Apify — scraped coverage without an enterprise contract

`APIFY_TOKEN`. Sign up at <https://apify.com>, then **Settings → Integrations → API token**.
The free tier's monthly credit runs a small demo; the paid Starter plan (~$39–49/month)
covers real daily use. Set the token and the source switches itself on — no code change.

What it adds once connected:

- **Funding discovery beyond SEC filings.** A search Actor runs one query per fund theme
  from `config/thesis.yaml`, and each real result becomes a signal with its URL. Amounts and
  stages are extracted by the *same deterministic regex* the RSS and Hacker News adapters
  use — a scraped headline is held to exactly the evidence standard as a Form D filing, and
  a model is never asked what a number is.
- **Self-reported team size and pricing pages**, by crawling the company's own website.
  Stored as `self_reported_headcount` with confidence 0.5 and source `apify:<actor>`, because
  a company's About page is weaker evidence than Coresignal's measured headcount. When the
  site says nothing, the field records *why* rather than staying mysteriously blank.

Configure Actors in `config/sources.yaml` under the `apify` entry — Actor ids, queries and
page limits are data, so trying a different Store Actor never touches Python.

### What is deliberately NOT wired, and why

LinkedIn and X Actors exist on the Apify Store, and they would fill the headcount and
GP-attention gaps cheaply. They are **intentionally excluded**: both platforms prohibit
scraping in their terms of service. For a personal project that is your call to make; for a
tool handed to a fund — an entity with compliance obligations — it is a liability that costs
more than the subscription it saves. The licensed routes for exactly that data (Coresignal,
the official X API) are already wired in `licensed.py` and need only their env var.

### Verifying

`POST /api/apify/test` (or the dashboard) makes one cheap call to Apify's `users/me` and
reports the account and plan, or names the failure — token rejected, or unreachable.
