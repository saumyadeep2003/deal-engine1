# From demo to production: what it takes, what it costs

Nothing in this document is implemented. It is the plan and the bill.

Costs are researched as of August 2026 and sourced at the end. Where a vendor
refuses to publish (PitchBook, Harmonic, Dealroom, Crunchbase API) the figure is
a reported range, marked as such — a quote is the only real number.

---

## Part 1 — The honest gap

The engine today is a **correct pipeline running on thin data**. That distinction
matters, because the two halves need completely different money.

What is genuinely finished: the funnel, provenance on every figure, cohort-relative
ranking, the gatekeeper, the workbook, the digest, the chat, the self-tests. That
is the hard, unglamorous half and it is done.

What is *not* production is three things, in this order of severity:

**1. It has no front door.** The dashboard is open on a public URL. Anyone who
learns it sees the fund's entire deal flow, every brief, every partner decision,
and can trigger searches that spend your money. For a demo that was a deliberate
convenience. For a fund's live pipeline it is the single most serious defect in
the system, and it is also the cheapest to fix.

**2. The company records are dirty.** Look at the live pipeline and you find
companies called `Text`, `Built`, `leadmagic.io` and `Defense startup Helsing`.
Those are headline fragments that the entity resolver promoted to companies. The
consequence cascades: a junk company gets scored, ranked, counted in a cohort,
possibly briefed, and it dilutes every percentile around it. No amount of extra
data sources fixes this — it is fixed by resolving every candidate against a
canonical registry (Crunchbase / Harmonic company IDs) and refusing to create a
company that cannot be resolved.

**3. Nothing watches it.** If a run dies at 3am, no one is told. If the model
provider starts refusing, the next morning's brief silently reads `[STUB]`. There
is no error tracking, no uptime monitoring, no alert on a failed run, and no
budget ceiling on paid APIs — which matters a great deal more once the APIs cost
money.

Everything else on the wish list — competitor tracking, every source wired, full
per-company detail — is a **data licensing** question, not an engineering one.

---

## Part 2 — Engineering work required

Ordered by what I would actually do first. Estimates assume one experienced
engineer.

| # | Work | Why | Effort |
|---|---|---|---|
| 1 | **Authentication + per-partner accounts** | Fund data is on a public URL. Also required before decisions can be attributed to a named partner rather than "partner" | 2–3 days |
| 2 | **Entity resolution against a canonical registry** | Kills `Text` / `Built` / `Defense startup Helsing`. Requires a deal-database licence to resolve against. Biggest single quality win in the system | 1 week + licence |
| 3 | **Failure alerting + error tracking** (Sentry or equivalent) | A silent 3am failure currently costs you a day | 1 day |
| 4 | **Spend ceilings per source, per day** | With free APIs a runaway loop is embarrassing. With Coresignal credits and Apify compute units it is a bill | 2 days |
| 5 | **Resumable runs / real job queue** | APScheduler runs in-process; a restart orphans a run mid-flight. Fine at 16 min/run, not fine at 90 | 3–4 days |
| 6 | **Historical backfill** | The pipeline only knows what it has seen since it started. A fund wants "who raised in this sector over 24 months", which means bulk-loading history from a deal database | 3 days + licence |
| 7 | **CI on every push** | Four suites exist and run manually. The stale-deploy week we just had is exactly what CI prevents | 1 day |
| 8 | **Secret rotation + audit log** | The NVIDIA key has been pasted in chat and should be rotated regardless | 1 day |
| 9 | **Per-partner digest preferences** | Different partners care about different themes; one digest for everyone is a demo simplification | 2 days |
| 10 | **Deeper commentary ingestion** (podcast transcripts, Substack) | Assignment §2(d) wants qualitative signal for every company; today it reaches a small fraction | 1 week + licences |

**Realistic total: 4–6 weeks** to a system a fund can depend on, of which items
1, 3, 4 and 7 (about a week) remove the risks that actually bite.

---

## Part 3 — Data sources: what each costs and what it buys

### The ones that change what the product *is*

| Source | Price (Aug 2026) | What it unlocks | Verdict |
|---|---|---|---|
| **Coresignal** | Starter **$199/mo** (12k credits) · Pro **$499/mo** (35k) · Growth **$1,000/mo** (150k). Company/employee records 10–20 credits each, job postings 1 credit | Verified headcount and 6/12-month growth. Turns your growth criterion from a hiring proxy into the real measurement. Also LinkedIn GP post feed | **Buy first.** At ~160 companies × 20 credits per refresh = 3.2k credits, weekly refresh needs ~13k/mo — Starter is tight, **Pro is the honest tier** |
| **PitchBook** | Not published. Reported **$12k–$40k/yr** for a solo seat, ~**$7k/yr** per additional seat; API is enterprise custom | Valuations, full cap tables, complete funding history. Turns three of your nine criteria from estimates into measurements | **Only if decisions turn on valuation.** This one line item can exceed everything else combined |
| **Crunchbase** | Pro **$49–99/mo** · Business **$199/mo** · **API: custom quote** | Funding rounds and investor lists far beyond SEC filings. Critically: it is the **registry to resolve entities against** | **Buy second.** The API tier is what fixes the dirty-company problem, so get a quote |
| **Harmonic** | Quote only (console / API / bulk to S3-BigQuery-Snowflake) | 35M companies, 195M people, network mapping. Strong on early stage — where you are looking | Get a quote alongside Crunchbase; they compete |
| **Dealroom** | Quote only | European coverage | Only if the fund invests in Europe |

### GP attention — the assignment's 72 handles

| Route | Price | Notes |
|---|---|---|
| **X API, pay-per-use** | **$0.005** per post read; hard cap 2M reads/mo (≈$10k), then Enterprise at **~$42k/mo** | 72 handles × ~20 posts/day ≈ 43k reads/month ≈ **$216/mo**. Legacy Basic ($200/mo) and Pro ($5,000/mo) are closed to new signups |
| **Third-party X mirrors** | ~**$0.00015** per read (≈33× cheaper) | ~**$7/mo** at your volume. Weigh the terms and the reliability yourself — I would not put a fund's signal chain on an unofficial mirror without reading their contract |
| **Bluesky** (already built) | Free | Real GP signal, smaller investor population. Not a replacement |

### Everything else

| Source | Price | Status |
|---|---|---|
| **Apify** | Free $5 credit · Starter **$29/mo** · Scale **$199/mo** · Business **$999/mo**. Compute unit $0.13–0.20. **Unused budget expires monthly** | Already wired. Starter is enough for the current crawl volume; Scale if you enrich all 160 companies |
| **The Information** | ~$400/yr individual | Scoop-level reporting. Nice, not structural |
| **Reddit API** | Free (OAuth app) | Already coded — needs you to create the app |
| **SEC EDGAR, arXiv, GitHub, HN, RSS, Wayback, ATS boards** | Free | All live |

---

## Part 4 — The model cost, from your own measured usage

Your engine records every call. Measured on the live deployment: **1,334 calls,
667,002 prompt tokens, 230,057 completion tokens across 17 runs** — and that is
with `JUDGE_TOP_N=10`, i.e. analysing 10 companies per run instead of all 160.

Scaling that to full coverage from the per-company rates it actually recorded
(~2.9k prompt + 1.2k completion per company judged):

| | Prompt | Completion |
|---|---|---|
| First full pass (160 companies, 80 profiles, 60 commentary) | ~676k | ~241k |
| Steady state per day (only changed companies re-judged) | ~150k | ~55k |
| **Steady state per month** | **~4.5M** | **~1.65M** |

At that volume, monthly model spend:

| Model | Input | Output | **Monthly** |
|---|---|---|---|
| Claude Haiku 4.5 | $1/M | $5/M | **~$13** |
| Claude Sonnet 5 | $3/M (after 31 Aug) | $15/M | **~$38** |
| Claude Opus 5 | $5/M | $25/M | **~$64** |
| NVIDIA NIM (current) | free tier / credits | | **~$0**, rate-limited |

**The model is not your cost.** Even on the most expensive tier it is under $70 a
month. Batch API would halve it again. Every serious number in this document is a
data licence.

---

## Part 5 — Infrastructure

| Item | Price | Note |
|---|---|---|
| Render Pro | **$25/mo** | Unlimited services, 25GB bandwidth. Needed for a service that does not sleep |
| Render Postgres | **$7/mo** starter → $175/mo Pro Plus | Or keep Supabase |
| Supabase Pro | **$25/mo** | What you use now; daily backups and no pausing |
| Resend | Free to 3k emails/mo | One daily digest to a few partners never leaves the free tier |
| Sentry | Free tier | Sufficient at this scale |
| **Infrastructure total** | **~$50–60/mo** | |

---

## Part 6 — Three budgets

### A. Lean production — **~$300/month**
Coresignal Pro ($499 — or Starter $199 if you refresh fortnightly), Apify Starter
($29), X via third-party (~$7), infrastructure (~$55), model (~$15).
*Call it $300–$800/mo depending on the Coresignal tier.*

**Gets you:** verified headcount and growth, GP attention from the 72 handles,
web-scraped enrichment, daily briefs. **Does not get you:** valuations, cap
tables, clean entity resolution.

### B. Serious — **~$1,200/month + a Crunchbase API quote**
Everything above plus Coresignal Pro ($499), Crunchbase Business or API,
Apify Scale ($199), official X API (~$216), The Information ($33/mo).

**Gets you:** the entity-resolution fix, real investor-by-round data — which is
what actually answers *"which VCs are investing in this company"* properly rather
than inferring it from Form D. **This is the tier I would argue for.**

### C. Full institutional — **$30k–$60k/year**
Everything above plus PitchBook (**$12k–$40k/yr**) and/or Harmonic (quote).

**Gets you:** measured valuations, complete cap tables, full funding history —
three criteria stop being estimates. Justified only if the fund's decisions
actually turn on entry valuation, which for a seed/Series-A thesis they often
do not.

### Engineering, separately
4–6 weeks of build. At contractor rates that is a real number and it does not
recur; the licences do.

---

## Part 7 — What I would do, in order

1. **Lock the front door.** Auth, this week, before anything else. It costs two
   days and removes the only defect that could actually hurt the fund.
2. **Rotate the NVIDIA key.** It has been in a chat window.
3. **Get quotes from Crunchbase (API) and Harmonic in parallel.** They compete,
   and one of them is the answer to dirty entity resolution. Do not buy PitchBook
   until you have both quotes in hand.
4. **Buy Coresignal Pro.** It is the cheapest line item that converts a criterion
   from proxy to measurement, and hiring data is the leading indicator you will
   actually act on.
5. **Wire X properly** once you have decided official vs mirror. 72 handles is
   the assignment's single largest content gap.
6. **Then the remaining engineering** — alerting, spend caps, CI, resumable runs.
7. **Decide on PitchBook last**, with real usage data on how often a valuation
   actually changed a decision.

---

## Part 8 — What I need from you before building any of it

1. **Who logs in?** Just you, or named partners with their own accounts and
   attributed decisions? This changes the auth design substantially.
2. **Crunchbase or Harmonic?** I would get both quotes; if you have a preference
   or an existing relationship, that decides the entity-resolution work.
3. **Official X API or a third-party mirror?** ~$216/mo versus ~$7/mo, with a
   terms-of-service and reliability trade-off that is genuinely your call, not
   mine.
4. **How far back should history go?** Backfill depth is a direct cost multiplier
   on any deal-database licence.
5. **Is this for the fund, or is it a product you intend to sell?** Multi-tenancy,
   data-redistribution rights and per-seat licence terms all change if it is the
   second — most data licences explicitly forbid redistribution.

---

## Sources

- [Coresignal pricing](https://coresignal.com/pricing/)
- [PitchBook pricing breakdown](https://easyvc.ai/vs/pitchbook-pricing-in-2026-costs-best-alternative/)
- [Crunchbase pricing](https://easyvc.ai/vs/crunchbase-pricing/)
- [Harmonic pricing](https://harmonic.ai/pricing)
- [X API cost breakdown 2026](https://twitterapi.io/blog/x-api-cost-breakdown-2026)
- [Apify pricing 2026](https://scrapegraphai.com/blog/apify-pricing)
- [Claude API pricing, August 2026](https://benchlm.ai/anthropic/api-pricing)
- [Render pricing 2026](https://costbench.com/software/developer-tools/render/)
