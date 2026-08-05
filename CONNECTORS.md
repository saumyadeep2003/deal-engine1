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
