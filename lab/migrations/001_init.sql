-- AI Business Lab — initial schema (Phase 1)
-- Timestamps are ISO-8601 UTC strings. JSON columns hold text.

CREATE TABLE owner_settings (
  key         TEXT PRIMARY KEY,
  value_json  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE settings_history (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  key         TEXT NOT NULL,
  old_json    TEXT,
  new_json    TEXT NOT NULL,
  actor       TEXT NOT NULL,
  changed_at  TEXT NOT NULL
);
CREATE INDEX idx_settings_history_key ON settings_history(key, changed_at);

CREATE TABLE agents (
  id                  TEXT PRIMARY KEY,           -- role id, e.g. market_researcher
  name                TEXT NOT NULL,
  description         TEXT NOT NULL,
  kind                TEXT NOT NULL,              -- research | generation | reviewer | chair | system
  enabled             INTEGER NOT NULL DEFAULT 1,
  uses_model          INTEGER NOT NULL DEFAULT 1, -- 0 = deterministic code, no AI cost
  provider            TEXT,                       -- NULL = use default setting
  model               TEXT,
  max_iterations      INTEGER NOT NULL DEFAULT 2,
  max_tool_calls      INTEGER NOT NULL DEFAULT 5,
  max_output_tokens   INTEGER NOT NULL DEFAULT 1500,
  monthly_budget_eur  REAL,                       -- NULL = default per-agent budget setting
  permissions_json    TEXT NOT NULL DEFAULT '[]',
  escalation_json     TEXT NOT NULL DEFAULT '{}',
  updated_at          TEXT NOT NULL
);

CREATE TABLE agent_runs (
  id           TEXT PRIMARY KEY,
  agent_id     TEXT NOT NULL REFERENCES agents(id),
  task         TEXT NOT NULL,
  idea_id      TEXT,
  business_id  TEXT,
  status       TEXT NOT NULL,                     -- running | succeeded | failed | blocked
  detail       TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT
);
CREATE INDEX idx_agent_runs_agent ON agent_runs(agent_id, started_at);
CREATE INDEX idx_agent_runs_idea ON agent_runs(idea_id);

-- Every AI call: reserved -> final (or released). Also holds cache hits at zero cost.
CREATE TABLE ai_usage (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id         TEXT REFERENCES agent_runs(id),
  agent_id       TEXT NOT NULL,
  provider       TEXT NOT NULL,
  model          TEXT NOT NULL,
  task           TEXT NOT NULL,
  idea_id        TEXT,
  business_id    TEXT,
  experiment_id  TEXT,
  prompt_hash    TEXT,
  status         TEXT NOT NULL,                   -- reserved | final | released | cached
  critical       INTEGER NOT NULL DEFAULT 0,
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  reserved_cost  REAL NOT NULL DEFAULT 0,
  estimated_cost REAL NOT NULL DEFAULT 0,         -- computed from configured prices x provider tokens
  actual_cost    REAL,                            -- only if the provider reports it
  month          TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  settled_at     TEXT
);
CREATE INDEX idx_usage_month ON ai_usage(month, status);
CREATE INDEX idx_usage_agent ON ai_usage(agent_id, month);
CREATE INDEX idx_usage_idea ON ai_usage(idea_id);
CREATE INDEX idx_usage_hash ON ai_usage(prompt_hash, created_at);

CREATE TABLE budget_alerts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  scope       TEXT NOT NULL,                      -- total | agent:<id> | business:<id>
  level       TEXT NOT NULL,                      -- warning | hard_stop
  month       TEXT NOT NULL,
  message     TEXT NOT NULL,
  acknowledged INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  UNIQUE(scope, level, month)
);

CREATE TABLE llm_cache (
  key         TEXT PRIMARY KEY,
  response    TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL
);

CREATE TABLE research_sources (
  id            TEXT PRIMARY KEY,
  url           TEXT NOT NULL,
  title         TEXT,
  kind          TEXT NOT NULL DEFAULT 'web',      -- web | manual
  content_text  TEXT,                             -- truncated page text, untrusted
  content_hash  TEXT,
  retrieved_at  TEXT NOT NULL,
  http_status   INTEGER,
  robots_allowed INTEGER,
  note          TEXT,
  UNIQUE(url, content_hash)
);

CREATE TABLE market_observations (
  id            TEXT PRIMARY KEY,
  source_id     TEXT REFERENCES research_sources(id),
  topic         TEXT NOT NULL,
  statement     TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('fact','estimate','assumption','speculation')),
  excerpt       TEXT,
  confidence    REAL NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
  verified      INTEGER NOT NULL DEFAULT 0,       -- excerpt found in stored source text
  verifier_note TEXT,
  synthetic     INTEGER NOT NULL DEFAULT 0,       -- produced by mock provider
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_obs_topic ON market_observations(topic);

CREATE TABLE ideas (
  id                    TEXT PRIMARY KEY,
  fingerprint           TEXT NOT NULL,
  name                  TEXT NOT NULL,
  description           TEXT NOT NULL,
  category              TEXT NOT NULL,
  target_audience       TEXT NOT NULL,
  customer_problem      TEXT NOT NULL,
  product               TEXT NOT NULL,
  usp                   TEXT NOT NULL DEFAULT '',
  revenue_model         TEXT NOT NULL DEFAULT '',
  channels_json         TEXT NOT NULL DEFAULT '[]',
  scope                 TEXT NOT NULL DEFAULT '',
  setup_cost_eur        REAL NOT NULL DEFAULT 0 CHECK (setup_cost_eur >= 0),
  recurring_cost_eur_month REAL NOT NULL DEFAULT 0 CHECK (recurring_cost_eur_month >= 0),
  human_hours_setup     REAL NOT NULL DEFAULT 0 CHECK (human_hours_setup >= 0),
  human_hours_week      REAL NOT NULL DEFAULT 0 CHECK (human_hours_week >= 0),
  days_to_first_sale    INTEGER,
  unit_price_eur        REAL,
  unit_variable_cost_eur REAL,
  expected_units_month  REAL,
  automation_potential_pct REAL,
  recurring_revenue     INTEGER NOT NULL DEFAULT 0,
  revenue_assumptions   TEXT NOT NULL DEFAULT '',
  cost_assumptions      TEXT NOT NULL DEFAULT '',
  competitors           TEXT NOT NULL DEFAULT '',
  legal_risks           TEXT NOT NULL DEFAULT '',
  automation_opportunities TEXT NOT NULL DEFAULT '',
  uncertainties_json    TEXT NOT NULL DEFAULT '[]',
  validation_plan       TEXT NOT NULL DEFAULT '',
  status                TEXT NOT NULL,            -- proposed | under_review | revising | needs_evidence | needs_human_review | approved | rejected | archived
  proposer_agent        TEXT NOT NULL,
  synthetic             INTEGER NOT NULL DEFAULT 0,
  parent_idea_id        TEXT REFERENCES ideas(id),
  version               INTEGER NOT NULL DEFAULT 1,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL
);
CREATE INDEX idx_ideas_status ON ideas(status);
CREATE INDEX idx_ideas_category ON ideas(category);

CREATE TABLE idea_versions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id       TEXT NOT NULL REFERENCES ideas(id),
  version       INTEGER NOT NULL,
  snapshot_json TEXT NOT NULL,
  changed_by    TEXT NOT NULL,
  reason        TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(idea_id, version)
);

CREATE TABLE idea_evidence (
  id              TEXT PRIMARY KEY,
  idea_id         TEXT NOT NULL REFERENCES ideas(id),
  observation_id  TEXT REFERENCES market_observations(id),
  claim           TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('fact','estimate','assumption','speculation')),
  url             TEXT,
  retrieved_at    TEXT,
  excerpt         TEXT,
  verified        INTEGER NOT NULL DEFAULT 0,
  verified_by     TEXT,
  verifier_note   TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX idx_evidence_idea ON idea_evidence(idea_id);

CREATE TABLE idea_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id     TEXT NOT NULL REFERENCES ideas(id),
  event       TEXT NOT NULL,
  actor       TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_idea_events ON idea_events(idea_id, id);

-- Evaluation criteria are versioned so past decisions keep their original explanation.
CREATE TABLE evaluation_profiles (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  version     INTEGER NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  config_json TEXT NOT NULL,
  active      INTEGER NOT NULL DEFAULT 0,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE idea_evaluations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id         TEXT NOT NULL REFERENCES ideas(id),
  idea_version    INTEGER NOT NULL,
  round           INTEGER NOT NULL,
  reviewer_role   TEXT NOT NULL,
  provider        TEXT NOT NULL,
  model           TEXT NOT NULL,
  synthetic       INTEGER NOT NULL DEFAULT 0,
  scores_json     TEXT NOT NULL,
  recommendation  TEXT NOT NULL CHECK (recommendation IN ('approve','revise','reject','request_evidence')),
  assessment      TEXT NOT NULL,
  evidence_json   TEXT NOT NULL DEFAULT '[]',
  assumptions_json TEXT NOT NULL DEFAULT '[]',
  objections_json TEXT NOT NULL DEFAULT '[]',
  uncertainties_json TEXT NOT NULL DEFAULT '[]',
  confidence      REAL,
  discarded_scores_json TEXT NOT NULL DEFAULT '{}',
  run_id          TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX idx_eval_idea ON idea_evaluations(idea_id, round);

CREATE TABLE consensus_decisions (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  idea_id          TEXT NOT NULL REFERENCES ideas(id),
  idea_version     INTEGER NOT NULL,
  round            INTEGER NOT NULL,
  profile_version  INTEGER NOT NULL,
  outcome          TEXT NOT NULL,                 -- approved | revise | rejected | needs_evidence | needs_human
  overall_score    REAL,
  category_scores_json TEXT NOT NULL,
  gate_results_json TEXT NOT NULL,
  rule_results_json TEXT NOT NULL,
  explanation      TEXT NOT NULL,
  dissent_json     TEXT NOT NULL DEFAULT '[]',
  synthetic        INTEGER NOT NULL DEFAULT 0,
  profile_snapshot_json TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE INDEX idx_decisions_idea ON consensus_decisions(idea_id, round);

CREATE TABLE jobs (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,
  payload_json     TEXT NOT NULL DEFAULT '{}',
  idempotency_key  TEXT UNIQUE,
  status           TEXT NOT NULL,                 -- queued | running | succeeded | failed | cancelled
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 3,
  run_after        TEXT NOT NULL,
  locked_at        TEXT,
  result_json      TEXT,
  error            TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX idx_jobs_status ON jobs(status, run_after);

CREATE TABLE audit_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  entity      TEXT,
  entity_id   TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_audit_created ON audit_events(created_at);
CREATE INDEX idx_audit_entity ON audit_events(entity, entity_id);
