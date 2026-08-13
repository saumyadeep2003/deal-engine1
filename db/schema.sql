-- Deal Sourcing & Discovery Engine — SQLite schema
-- Signals are immutable: never UPDATE a signals row. Company state is derived.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  adapter TEXT NOT NULL,
  interval_minutes INTEGER NOT NULL DEFAULT 60,
  requires_license INTEGER NOT NULL DEFAULT 0,
  license_vendor TEXT,
  last_ok_at TEXT,
  last_attempt_at TEXT,
  health TEXT NOT NULL DEFAULT 'unknown',   -- ok | degraded | license_required | down | unknown
  error_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  company_id INTEGER REFERENCES companies(id),
  kind TEXT NOT NULL,            -- funding_event | filing | launch | news | research | repo | commentary | hiring | fund_formation
  observed_at TEXT NOT NULL,     -- when the underlying event was observed/published (ISO)
  fetched_at TEXT NOT NULL,      -- when we fetched it
  fetch_mode TEXT NOT NULL DEFAULT 'live',  -- live | cached_snapshot
  payload_json TEXT NOT NULL,    -- parsed fields
  raw TEXT,                      -- raw excerpt for provenance
  url TEXT,                      -- real, fetchable source URL
  dedupe_key TEXT UNIQUE         -- source-scoped id to make ingest idempotent
);
CREATE INDEX IF NOT EXISTS idx_signals_company ON signals(company_id);
CREATE INDEX IF NOT EXISTS idx_signals_kind ON signals(kind);
CREATE INDEX IF NOT EXISTS idx_signals_observed ON signals(observed_at);

CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  domain TEXT UNIQUE,            -- canonical key when known
  name TEXT NOT NULL,
  description TEXT,
  sector TEXT,
  sub_sector TEXT,
  stage TEXT,                    -- pre-seed | seed | series-a | series-b | growth | unknown
  country TEXT,
  founded_year INTEGER,
  hq TEXT,
  last_signal_at TEXT,
  status TEXT NOT NULL DEFAULT 'candidate',  -- candidate | filtered | pipeline | watchlist | hot | stale_review | removed
  market_rank INTEGER,
  is_synthetic INTEGER NOT NULL DEFAULT 0,   -- demo-only records; NEVER shown in real tabs
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_aliases (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  alias TEXT NOT NULL,
  alias_type TEXT NOT NULL,      -- name | domain | linkedin | uuid | mention
  merged_from TEXT,              -- JSON snapshot of merged company record (reversibility)
  confidence REAL,
  merged_at TEXT,
  source_signal_id INTEGER REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS idx_alias_alias ON company_aliases(alias);

CREATE TABLE IF NOT EXISTS founders (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),
  name TEXT NOT NULL,
  linkedin_url TEXT,
  prior_companies_json TEXT,
  prior_exits INTEGER,
  frontier_lab_alum INTEGER,
  founder_score REAL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS funding_rounds (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  stage TEXT,
  amount_usd REAL,
  valuation_usd REAL,
  announced_at TEXT,
  lead_investor_id INTEGER REFERENCES investors(id),
  source_signal_id INTEGER REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS investors (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  tier INTEGER,                  -- 1 | 2 | 3 | NULL unknown
  aum_usd REAL,
  sector_distribution_json TEXT, -- historical distribution, derived from investments
  stated_focus_json TEXT         -- from config; basis for thesis-shift detection
);

CREATE TABLE IF NOT EXISTS investments (
  id INTEGER PRIMARY KEY,
  investor_id INTEGER NOT NULL REFERENCES investors(id),
  company_id INTEGER NOT NULL REFERENCES companies(id),
  round_id INTEGER REFERENCES funding_rounds(id),
  is_lead INTEGER NOT NULL DEFAULT 0,
  announced_at TEXT,
  source_signal_id INTEGER REFERENCES signals(id),
  UNIQUE(investor_id, company_id, round_id)
);

CREATE TABLE IF NOT EXISTS enrichment_cache (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  field TEXT NOT NULL,
  value_json TEXT,               -- null value + reason when unavailable
  unavailable_reason TEXT,       -- e.g. 'requires Coresignal'
  fetched_at TEXT NOT NULL,
  ttl_hours INTEGER NOT NULL DEFAULT 168,
  source TEXT NOT NULL,
  confidence REAL,
  UNIQUE(company_id, field)
);

CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  composite REAL,
  percentile REAL,               -- within (sector, stage) cohort — the primary output
  cohort_key TEXT,
  cohort_size INTEGER,
  cohort_low_confidence INTEGER NOT NULL DEFAULT 0,
  features_json TEXT NOT NULL,   -- full feature vector incl. nulls + reasons
  recommendation TEXT,           -- Pass | Watch | Deep Dive
  human_override TEXT,           -- partner-edited recommendation, always wins
  model_version TEXT,
  prompt_version TEXT,
  scored_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_company ON scores(company_id);

CREATE TABLE IF NOT EXISTS commentary (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),
  platform TEXT NOT NULL,
  author TEXT,
  author_credibility TEXT,
  sentiment TEXT,                -- positive | negative | mixed | neutral | [STUB]
  themes_json TEXT,
  quote TEXT,
  url TEXT,
  observed_at TEXT
);

CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  content_md TEXT NOT NULL,
  recommendation TEXT,
  generated_at TEXT NOT NULL,
  trigger TEXT NOT NULL,         -- on_demand | auto_threshold
  validated INTEGER NOT NULL DEFAULT 0,
  validation_notes TEXT
);

CREATE TABLE IF NOT EXISTS sectors_emerging (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL,
  cluster_id INTEGER,
  signal_velocity REAL,
  consensus_volume REAL,
  ratio REAL,
  source_diversity INTEGER,
  evidence_json TEXT,            -- doc ids + urls
  thesis_md TEXT,
  detected_at TEXT NOT NULL,
  is_contrarian INTEGER NOT NULL DEFAULT 0,
  companies_json TEXT,           -- best companies sourced INSIDE this cluster (§2b)
  talent_flow INTEGER NOT NULL DEFAULT 0,  -- founder-move / frontier-lab docs in cluster
  terms_json TEXT                -- the cluster's defining terms, for auditability
);

CREATE TABLE IF NOT EXISTS news_items (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT,
  source TEXT,
  published_at TEXT,
  why_it_matters TEXT,
  relevance_score REAL,
  signal_id INTEGER REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS peer_events (
  id INTEGER PRIMARY KEY,
  investor_id INTEGER NOT NULL REFERENCES investors(id),
  company_id INTEGER REFERENCES companies(id),
  event_type TEXT NOT NULL,      -- investment | fund_formation
  is_thesis_shift INTEGER NOT NULL DEFAULT 0,
  deviation_score REAL,
  observed_at TEXT,
  source_signal_id INTEGER REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS partner_actions (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  partner TEXT,
  action TEXT NOT NULL,          -- pass | watch | deep_dive | override | keep | remove
  score_at_time REAL,
  features_at_time_json TEXT,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,            -- merge | stale | brief_validation | llm_parse_failure
  payload_json TEXT NOT NULL,
  confidence REAL,
  status TEXT NOT NULL DEFAULT 'open',  -- open | resolved
  resolved_by TEXT,
  resolved_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS digests (
  id INTEGER PRIMARY KEY,
  sent_at TEXT NOT NULL,
  kind TEXT NOT NULL,            -- mwf_digest | instant_alert
  contents_json TEXT NOT NULL,
  item_count INTEGER NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0,      -- was it actually emailed?
  delivery_detail TEXT                       -- provider id, or why not
);

-- Operational tables (build additions; noted in BUILD_LOG.md)

CREATE TABLE IF NOT EXISTS checkpoints (
  source_name TEXT PRIMARY KEY,
  checkpoint TEXT NOT NULL,      -- ISO date/time high-water mark; only advances on success
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY,
  stage TEXT NOT NULL,           -- classify | score | brief | digest | chat | sector_label
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  stubbed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts_log (
  id INTEGER PRIMARY KEY,
  rule TEXT NOT NULL,            -- tier1_coinvest | thesis_shift | watched_founder
  company_id INTEGER REFERENCES companies(id),
  dedupe_key TEXT UNIQUE,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0,      -- was it actually emailed?
  delivery_detail TEXT                       -- provider id, or why not
);

-- Search runs: every search is a first-class record with live step progress
-- and a frozen snapshot of what it showed — so "what did Tuesday's search
-- find?" stays answerable forever, independent of later re-ranking.

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,             -- full | quick
  trigger_by TEXT NOT NULL,       -- manual | scheduled | boot
  status TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
  started_at TEXT NOT NULL,
  finished_at TEXT,
  seconds REAL,
  stats_json TEXT,                -- funnel counts, new companies, etc.
  error TEXT
);

CREATE TABLE IF NOT EXISTS run_steps (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  seq INTEGER NOT NULL,
  key TEXT NOT NULL,              -- e.g. collect:edgar_formd, judge, briefs
  label TEXT NOT NULL,            -- plain-English, shown in the dashboard
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | skipped
  started_at TEXT,
  finished_at TEXT,
  seconds REAL,
  items INTEGER,                  -- e.g. signals fetched, companies judged
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps(run_id);

CREATE TABLE IF NOT EXISTS run_results (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  company_id INTEGER,
  name TEXT NOT NULL,
  sector TEXT,
  stage TEXT,
  recommendation TEXT,            -- as shown by THIS search (frozen)
  percentile REAL,
  cohort_size INTEGER,
  rank_in_cohort INTEGER,
  is_new INTEGER NOT NULL DEFAULT 0,   -- first time this company ever appeared
  captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_results_run ON run_results(run_id);

CREATE TABLE IF NOT EXISTS sheet_sync (
  id INTEGER PRIMARY KEY,
  spreadsheet_id TEXT,
  spreadsheet_url TEXT,
  tabs_written INTEGER,
  edits_pulled INTEGER,
  status TEXT NOT NULL,          -- ok | not_configured | error
  detail TEXT,
  synced_at TEXT NOT NULL
);

-- Small key/value store for settings a partner can change from the dashboard
-- (e.g. the digest recipient). Deliberately separate from config/*.yaml: YAML is
-- the fund's stated intent and belongs in git; this is runtime state and belongs
-- in the database, so it survives a restart and follows the Supabase backup.
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL
);
