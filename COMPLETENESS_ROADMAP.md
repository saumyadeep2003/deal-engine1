# Completing the engine: architecture, data, and what to leave out

Nothing in this document is implemented. It is the plan for turning the engine
from "a correct pipeline on thin data" into the thing you actually asked for: an
assistant that can stand behind the sentence *"here is every funding event, every
founder, and every investor move we can legitimately know about."*

The word **legitimately** is doing work in that sentence, and the honest framing
comes first: "complete data from every source" is not a thing anyone sells, let
alone gives away. PitchBook is not complete. Crunchbase is not complete. What is
achievable — and what this plan targets — is *completeness within named
channels*: every US Reg D filing, every disclosed round in the press you watch,
every director a registry names, with each channel's boundary stated so a gap
reads as a gap and never as a zero.

---

## Part 1 — What the engine actually is today, honestly assessed

Six layers: ingestion (12 free adapters + 10 licence-gated) → entity resolution →
deterministic filter → enrichment → scoring (computed → judged → cohort
percentile) → feedback. Around it: the gatekeeper, the workbook, the daily
digest, alerts, chat, coverage reporting, connection tests, build identity.

Where each layer genuinely stands:

| Layer | State | The real weakness |
|---|---|---|
| Ingestion | Strong mechanics | **Keyword-shaped, not complete** — see below |
| Entity resolution | Good algorithm | No canonical registry to resolve against, so junk names (`Text`, `Built`) still get through |
| Filter | Done | Nothing to fix; this is the cost model and it works |
| Enrichment | Working | Depth limited by what free sources expose |
| Scoring | Working | Cohorts are small (many under 20), so ranks are honest but weak |
| Judgement | Working | Coverage advances per run now; quality tracks the model tier |
| Feedback | **Half-built** | 82 partner actions recorded, and *nothing reads them* — the learning loop is a table, not a loop |

### The single biggest architectural flaw

The EDGAR adapter runs **twelve keyword searches** against full-text search:
"artificial intelligence", "robotics", "nuclear"… A Form D whose text doesn't
contain one of your twelve phrases does not exist to the engine. That inverts
your own architecture — the deterministic filter exists precisely so ingestion
can be broad and filtering can be cheap. Discovery keyword-shaped at the source
means your completeness is capped by your vocabulary, and "every funding" is
unreachable by construction.

The fix costs nothing: EDGAR publishes a **daily index of every filing**. Pull
*all* Form Ds each day (a few hundred), parse locally, and let your own filter —
not SEC's search box — decide relevance. Same for the fund-formation
cross-reference against your ~11,500-firm dataset: it becomes exact instead of
keyword-lucky. This one change is the difference between "we search for
filings about AI" and "we see every Reg D raise in America and keep the ones
that matter." **It is the first thing I would build.**

---

## Part 2 — Free and near-free sources that close real gaps

Ordered by what they buy you per unit of effort. Everything here is a documented,
public, permitted interface — the same standard as the existing adapters.

### Tier 1: build these (each closes a named gap)

| Source | Cost | Closes | Effort |
|---|---|---|---|
| **EDGAR daily full index** | Free | The completeness flaw above — every US Reg D filing, not keyword hits | 2–3 days |
| **UK Companies House API** | Free (key, instant) | **Founders.** Directors with names, DOB month/year, nationality, other directorships — real registry data, better than Form D's bare names. Also incorporations = stealth-company signal | 2–3 days |
| **USPTO PatentsView API** | Free | **Moat evidence.** Patent filings per company, inventors (more founder names), citation velocity. Turns "moat: model opinion" into "moat: 4 patents, 2 pending, cited by X" | 2–3 days |
| **Google News RSS per company** | Free | **Tracking, not just discovery.** `news.google.com/rss/search?q="Company Name"+funding` per pipeline company = a standing watch on every tracked name. Today a company is only updated if it happens to reappear in a feed you already read | 1–2 days |
| **PR wires (PRNewswire, Business Wire, GlobeNewswire RSS)** | Free | Funding announcements at the source, hours before aggregators; customer-win press releases | 1 day |
| **OpenCorporates API** | Free tier / cheap | Legal-entity ground truth across 140 registries — the cheap half of the entity-resolution fix (canonical name + jurisdiction + status) | 2–3 days |

### Tier 2: cheap, high leverage for your thesis

| Source | Cost | Closes | Effort |
|---|---|---|---|
| **Podcast transcripts via open-source Whisper** | ~$0 (compute only) | The assignment's podcast-commentary requirement. RSS gives you the audio of 20VC, Invest Like the Best, All-In; Whisper (open weights) transcribes locally; your existing commentary pipeline ingests the text. This is the licence-gated `podcasts` adapter, un-gated by doing the work yourself | ~1 week |
| **Certificate-transparency logs (crt.sh)** | Free | **Stealth discovery.** New TLS certs for new domains are visible the day they're issued — companies surface here before they have a website worth reading. Filter by thesis-adjacent naming + cross-reference later signals | 2–3 days |
| **Y Combinator public directory** | Free | Every YC batch company with founders and one-liners — a clean, structured seed-stage feed that also improves entity resolution | 1 day |
| **Product Hunt API** | Free (GraphQL) | Launch signals for the AI-native-stack theme | 1–2 days |
| **npm / PyPI download stats** | Free | Traction for devtools companies — real adoption numbers, not stars | 1–2 days |
| **Substack public posts via RSS** | Free | The *essays* half of the Substack requirement (comment threads stay out of reach honestly) | 1 day |
| **More newsletters as feeds** (Fortune Term Sheet, StrictlyVC, Axios Pro Rata full) | Free | Deal-flow coverage density; these are where mid-size rounds get announced | half a day |

### Tier 3: keep on the list, lower priority

Mastodon (marginal investor population), Hacker News *comment threads* per
company (you read stories; the threads carry the sentiment), GitHub org member
counts (team-size proxy), Common Crawl (web-scale but heavy), international
registries beyond the UK (France INPI, Singapore ACRA — free-ish, add when the
fund looks there).

### What NOT to include — and it matters that this list is explicit

**LinkedIn scraping** (via Apify or anyone): prohibited by ToS, litigated, and a
liability a fund should not carry. Coresignal is the licensed route; until then
the gap stays a stated gap. **X scraping**: same reasoning; the API or nothing.
**Paywalled content** (The Information, PitchBook web): subscriber agreements
forbid it. **Google SERP scraping beyond your existing bounded Apify actor**:
fragile, adversarial, and it breaks mid-demo. Every one of these is data you
could technically get; the engine's credibility rests on being able to say where
every row came from, and "scraped against terms" poisons that answer.

---

## Part 3 — Engine improvements (no new data, just using what you have)

**1. Close the feedback loop — this is the "fine-tune" you actually want.**
You have 82 partner actions recorded against feature vectors, and nothing reads
them. Real model fine-tuning at this scale is the wrong tool (dozens of examples,
weeks of drift, opaque results). What works at this scale:

- *Weight recalibration*: logistic regression from feature vectors → partner
  decisions, refreshed weekly, writes new weights into `thesis.yaml` scoring
  with the old ones archived. ~2 days, fully inspectable.
- *Few-shot judgement*: inject the 5 most recent partner overrides (with their
  reasoning notes) into the judge prompt as calibration examples — "the partners
  passed on X despite tier-1 backing because…". Zero training, immediate effect.
- Revisit actual fine-tuning only when you have ~500+ decisions.

**2. Corroboration scoring.** You have multi-source signals per company; nothing
computes "this round is confirmed by a filing AND two articles" versus "one blog
mentioned it". A `corroboration` field (sources × kinds per claim) is cheap and
changes how much a partner trusts a row.

**3. IC memo generator.** The brief is a screening document. The next artefact up
is the investment-committee memo: brief + criteria scorecard + comparables + the
bear case (a second model pass prompted to argue *against*, gatekeeper-checked).
One endpoint, one template, big perceived-value jump for "AI fund manager".

**4. Round-stage inference.** Many Form Ds don't state a stage; amount +
company age + prior rounds imply one. Label it inferred, and cohorts get denser —
which directly strengthens every percentile.

**5. Alert digest for *changes*, not just news.** "Company X's open roles dropped
40%", "first tier-1 appeared on Y's cap table", "Z went quiet after weekly
signals". The signals table already holds the history; nothing diffs it over
time. This is the daily insight layer an assistant is judged on.

**6. Weekly self-evaluation.** The engine emails you what *it* did: coverage
deltas, gatekeeper catches, sources degraded, judgements reversed by partners.
An assistant that reports its own misses is one you can safely stop supervising.

---

## Part 4 — Order of work

Phase 1, ~2 weeks: EDGAR daily index, Google News per-company watch, PR wires,
Companies House. *Outcome: discovery is complete-within-channel and every
tracked company has a standing watch.*

Phase 2, ~2 weeks: PatentsView, corroboration scoring, recalibration + few-shot
feedback, round-stage inference. *Outcome: scoring gets sharper from data you
already hold.*

Phase 3, ~2 weeks: Whisper podcast pipeline, crt.sh stealth watch, YC/Product
Hunt/npm, change-alerts, IC memos, weekly self-report. *Outcome: the qualitative
layer and the assistant behaviours.*

Total: roughly six weeks of build, near-zero new recurring cost (Whisper compute
and OpenCorporates being the only candidates for actual spend, both small). The
paid-data decision from PRODUCTION_PLAN.md sits orthogonal to all of it — this
plan is what "maximum completeness without licences" looks like, and it moves
founders, moat, tracking and commentary from thin to genuinely covered.
