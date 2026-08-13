# The assignment, line by line, against what actually runs

Every row is checked against the live engine, not against intent. Where something
is partial the limit is named, because a capability with an unstated cap is
indistinguishable from a broken one.

Measured on the hosted deployment: 2,019 signals, 537 companies resolved, 160
surviving the thesis filter.

---

## 1. The tool

| Assignment | Status | Evidence / limit |
|---|---|---|
| 1(a) Single tool automating web sourcing | **Working** | 12 free sources live, 10 licensed adapters wired and reporting `LicenseRequired` |
| 1(b) Excel workbook, self-maintaining | **Working** | 11 tabs, rebuilt from the database on every download so it cannot go stale |
| 1(b) Mon/Wed/Fri email alerts | **Changed on request** | Now every morning at 07:00 IST — `digest.days` in `config/thesis.yaml` |
| 1(b) Add / update / remove / re-score, no manual work | **Working** | Adds on signal, re-scores each run, flags 90-day stale for partner review, never deletes |
| 1(c) Holds its own in conversation | **Working** | `/api/chat`; answers cite urls and dates and refuse when the evidence is absent |

## 2. Core intelligence

| Assignment | Status | Evidence / limit |
|---|---|---|
| 2(a) Knows a great deal from a noisy announcement | **Working** | Deterministic filter, then judgement over stored signals only. Ranked by percentile **within a (sector, stage) cohort**, never in isolation |
| 2(a) Articulates why *this* company | **Working, capped** | `JUDGE_TOP_N` per search, now advancing coverage instead of re-judging the same ten. Set to 40 to cover the pipeline in four searches |
| 2(b) Sector of tomorrow, before consensus | **Working, rebuilt** | TF-IDF → cosine clustering → technical velocity ÷ mainstream consensus. Deduplicated per cluster, vendor and event words barred from labels, unmeasured consensus ranked below real leads |
| 2(b) From GP commentary, frontier-lab hiring, founder migration, fund formation, research velocity | **4 of 5** | All present except GP commentary at scale — X requires the paid API; Bluesky is the free substitute and has a smaller investor population |
| 2(c) 3–5 curated news items with a one-line why | **Working** | Deterministic relevance for every item, model rationale for the top slice, gatekeeper-checked against the article |
| 2(d) Commentary for **every** company | **Partial** | HN live; Reddit needs `REDDIT_CLIENT_ID`/`SECRET` (free) because anonymous Reddit is blocked from hosted IPs. X, Blind, podcasts, Substack are licence-gated |

## 3. What the tool does

| Assignment | Status | Evidence / limit |
|---|---|---|
| 3(a) Continuous sourcing | **Working** | Manual-trigger by default so a demo is never mid-run; `SEARCH_MODE=auto` schedules it |
| 3(a) Funding, hiring, launches, founder moves, customer wins | **Working** | All five are distinct signal kinds; the event classifier tests 8/8 positives with 0 false positives |
| 3(a) Not-yet-on-mainstream-lists | **Working** | Form D full-text search surfaces filings before press coverage |
| 3(a) Intelligent de-duplication | **Working** | Domain → LinkedIn → UUID → scoped fuzzy name; ≥0.85 auto-merges reversibly, 0.60–0.85 goes to review |
| 3(b) On-demand thesis scan | **Working** | `/api/scan` |
| 3(b) Proactive sector flags + best deals within | **Working** | Companies sourced inside each cluster, now requiring 3 shared terms rather than 1 |
| 3(b) Contrarian angle | **Working** | Heavy coverage + decelerating technical signal |
| 3(c) Peer set tracking | **Working** | Form D fund formations + observed rounds |
| 3(c) Co-investor heatmap | **Working** | Workbook tab |
| 3(c) Thesis-shift flags | **Working** | Against `stated_focus` in config |
| 3(d) Company intelligence briefs | **Working, expanded** | Now opens with what the company does and its products, from its own website; adds a criteria scorecard |
| 3(e) Self-maintaining pipeline | **Working** | Stale at 90 days, flagged for partner review, never auto-removed |

## 4. Investment criteria

Every brief now carries a scorecard answering all nine. A criterion with no
evidence says so — zero would read as a judgement.

| Criterion | Status | How |
|---|---|---|
| 60/40 dominant-tech / tactical split | **Working** | Applied at the ranking layer |
| Entry valuation | **Estimated** | Round size ÷ the stage's ordinary dilution band, as a range, marked an estimate |
| 40%+ YoY growth | **Proxy only** | Change in open roles on the company's own board. Labelled hiring appetite, **not** revenue growth |
| ~3 year runway | **Estimated** | Round size ÷ (team × per-head burn), as a range; refuses without team evidence |
| 3–4 Tier-1 investors | **Working** | Observed investments × the config tier list |
| Moat / defensibility | **Working** | Model judgement, gatekeeper-verified |
| TAM > $1B | **Model estimate** | With stated assumptions; never presented as measured |
| 3–5 year exit horizon | **Model judgement** | |
| Rank vs same sector and stage | **Working** | Cohort percentile, low-confidence flagged under 20 |

## 5. Sources — assignment table vs what runs

| Category | Assignment asks for | Running |
|---|---|---|
| Deal databases | PitchBook, Crunchbase, Harmonic, Dealroom | Wired, all four licence-gated. **Apify** substitutes partially |
| Regulatory filings | SEC Form D vs ~11,500-firm dataset | **Live.** Detail XML now parsed for 150 filings/run (was 15 — the cause of 0% founder coverage) |
| GP and firm signals | X accounts, LinkedIn via Coresignal | X licence-gated (72 handles configured); **Bluesky** is the free substitute |
| Long-form analysis | Stratechery, The Information, Newcomer, Generalist, Not Boring, Byrne Hobart | **Live** except The Information (paywalled) |
| News feeds | TechCrunch, Axios, Bloomberg, Reuters, FT | **Live** where RSS exists |
| Investor/operator commentary | X, Reddit, HN, Blind, podcasts, Substack | HN live; **Reddit needs free credentials**; the rest licence-gated |
| Technical signals | GitHub stars/contributors/velocity, arXiv | **Live, all three** |
| Hiring signals | Coresignal headcount, careers pages, function mix | Coresignal gated; **ATS boards + careers pages + Wayback team history live** |
| Company surface area | Websites: positioning, logos, pricing, careers | **Live** — now 80 companies/run, up from 10 |
| Press releases | RSS + web search | **Live** |

## 6. Output

| Assignment | Status |
|---|---|
| 9 workbook tabs | **All present** plus Provenance and Demo Cases |
| Pipeline's 17 columns | **All present**; description now from the company's own site |
| Digest: 3–5 deals, 1–2 sector calls, 3–5 news, peer moves, links to briefs | **Working** |
| Immediate alerts | **Working** — 2+ Tier-1 co-investment, off-thesis move, watched founder |
| Conversational interface | **Working** |

---

## What still needs you

1. **`JUDGE_TOP_N=40`** in Render — the single dial that decides how fast every company gets analysed.
2. **Reddit credentials** — free `script` app at reddit.com/prefs/apps → `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`. Without these, commentary stays HN-only.
3. **Bluesky handles** — `bluesky.handles` in `config/sources.yaml`. The thesis search works without them; watched-GP attention does not.
4. **`SEARCH_MODE=auto`** — required for the engine to search by itself overnight and have something new in the 7am brief.

## What money would buy, and nothing else will

| Spend | Unlocks |
|---|---|
| Coresignal | Verified headcount and 6/12-month growth — turns the growth proxy into the real criterion |
| PitchBook | Valuations, full cap tables, complete funding history — turns three estimates into measurements |
| X API | GP attention at the scale the assignment describes |
| The Information | Scoop-level reporting ahead of the free feeds |
