# Product Requirements — AI Business Lab

Status: living document. Phase 1 (local MVP) is implemented; later phases are specified in `ROADMAP.md`.

## 1. Critical analysis of the concept

**What is strong**

- A *gated, evidence-first pipeline* (research → idea → committee → validation experiment → operate) is the right shape. It forces every dollar through a cheap test before it is spent.
- Treating the owner as the authority for spend, publication and irreversible actions is the correct control model for an agent system.
- Separating revenue / contribution margin / operating profit / net profit prevents the most common self-deception in side businesses.

**What is risky or unrealistic, and how the design responds**

| Risk | Why it matters | Design response |
|---|---|---|
| LLM "market research" is easily hallucinated. Agents will happily invent demand figures. | Every downstream decision inherits the error. | Observations must cite a fetched source. A *programmatic* verifier checks that the quoted excerpt really appears in the stored page text. Unverifiable claims get confidence 0 and cannot satisfy the evidence gate. |
| A committee of LLM roles is *not* independent — same model, same biases. Agreement is weak evidence. | "Consensus" can be theatre. | Chair is deterministic code applying owner-set rules, not a vote. Mandatory conditions are computed from data (evidence counts, margin, budget), not from model opinion. Reviewers can only score the categories their role is allowed to score. Evaluations produced by the mock provider are flagged `synthetic`. |
| The most-discussed "passive income" ideas (generic digital art, templates, micro-SaaS) are crowded; edge is thin. | The system could confidently greenlight low-margin, high-saturation ideas. | Saturation and differentiation are explicit evidence items; the financial gate uses explicit price / variable-cost numbers; every business starts with a small validation experiment (Phase 2). |
| Platform terms: many marketplaces restrict automated listing, scraping, or AI-generated assets. | Account bans, legal exposure. | Risk & compliance gate is mandatory. Integrations are adapters that report *unavailable* until configured; nothing simulates success. Research respects `robots.txt`. |
| Cost runaway from agent loops. | Real money. | Central cost controller: reserve-before-call, per-agent / total / per-business caps, warning threshold, hard stop, identical-prompt loop detection, bounded revision rounds. |
| "Minimal intervention" vs. real income: most validated businesses need ongoing human judgement (customer support, quality, taxes). | Setting wrong expectations. | The system tracks human hours; profit per human hour is a first-class metric (Phase 2+). No ROI is ever promised. |
| Tax / legal obligations (VAT/IVA, self-employment registration, EU consumer law) vary by country. | The owner is in the EU. | Out of scope for automation; the compliance gate requires *identified* obligations, and reports label figures "pre-tax estimate". The system is not legal or tax advice. |

**Verdict.** Feasible as a *decision-support and operations tool*. It cannot guarantee income. The honest value proposition is: cheaper, faster, better-documented screening of ideas and disciplined small experiments.

## 2. Missing decisions and the assumptions adopted

| # | Open question | Assumption adopted (editable in settings where noted) |
|---|---|---|
| 1 | Owner profile | Solo founder, EUR, small budget. Defaults: €20/month AI spend, €200 per business, €50 per experiment. (settings) |
| 2 | Default model provider | `mock` (zero cost, deterministic) until the owner configures a real provider. Never spend by default. |
| 3 | Real provider for MVP | Anthropic Messages API adapter. Others plug in via the same interface. |
| 4 | Model pricing | Not hard-coded. Owner enters per-model prices in settings; a model with no price configured **cannot be called** (budget could not be enforced). |
| 5 | Database | SQLite (WAL) for local single-owner use; schema is plain SQL with versioned migrations and no SQLite-only features in the domain layer, so PostgreSQL is a documented migration. |
| 6 | Web stack | See ARCHITECTURE: Starlette + pydantic + stdlib sqlite3, vanilla-JS UI with no build step. React/TypeScript + FastAPI is the documented upgrade path. (Reason: dependency-light, runs anywhere, testable in restricted environments.) |
| 7 | Auth | Single owner, bearer token from `LAB_OWNER_TOKEN` (auto-generated on first run if unset). |
| 8 | Autonomy | Default mode is Advisory. Modes stored per-setting now, per-business in Phase 2. |
| 9 | Money precision | Phase 1 stores EUR as floats rounded to 6 decimals for AI-usage estimates. The Phase 2 ledger will use integer cents. |
| 10 | Web research scope | Owner supplies seed URLs / topics; fetcher respects `robots.txt`, identifies itself, rate-limits, and treats all content as untrusted data. No logins, no circumvention. |
| 11 | What "approved" means | Approval makes an idea *eligible* for incubation. It authorizes no spending. Incubation (Phase 2) needs an explicit owner authorization. Ideas approved only by synthetic (mock) review are flagged and cannot be incubated without an explicit override. |
| 12 | Languages / markets | Defaults EU, `en` + `es`; stored as settings and fed to agents as constraints. |

## 3. Phase 1 MVP scope (implemented)

1. Application skeleton, versioned SQL migrations, health check, owner-token auth, audit log.
2. Owner control panel: every setting in spec §4 is defined, validated, persisted, history-tracked and editable from the UI.
3. Agent registry: roles, enable/disable, provider/model, iteration & token limits, per-agent budget.
4. Cost controller: pricing from config, reserve → settle → release accounting, warning + hard stop, loop detection, response cache.
5. Provider adapters: `mock` (deterministic) and `anthropic` (real, needs `ANTHROPIC_API_KEY`).
6. Research room: robots-aware fetcher, observation extraction, programmatic excerpt verification, opportunity scouting, duplicate detection, persistent history, schedulable job.
7. Idea room: full idea record, versions, evidence, event history, duplicate prevention.
8. Evaluation gates: editable, versioned profile (criteria, thresholds, weights, hard rejections, per-category overrides). Mandatory conditions are separate from weighted scoring.
9. Committee: six reviewer roles + deterministic chair, bounded revision rounds, dissent recorded, self-approval blocked, traceable decisions pinned to the profile version used.
10. Job queue with idempotency keys, retries, crash recovery.
11. Dashboard UI wired to the API; daily report.

Explicitly **not** in Phase 1: business workspaces, task engine, experiments, approval queue, ledger, real marketplace integrations (they appear in the UI as labelled "not built yet", not as fake controls).

## 4. Acceptance criteria (Phase 1)

Mapped to spec §24 — each is covered by an automated test in `tests/`:

| Spec item | Test |
|---|---|
| 1 Idea stored persistently | `test_ideas.py::test_idea_persists_across_reopen` |
| 2 Evidence associated with idea | `test_ideas.py::test_evidence_attached` |
| 3 Criteria change without code | `test_rules.py::test_profile_edit_changes_outcome` |
| 4 Failed mandatory gate blocks approval | `test_rules.py::test_failed_mandatory_gate_blocks_high_score` |
| 5 Traceable committee decision | `test_committee.py::test_decision_is_traceable` |
| 8 AI spend attributed correctly | `test_costs.py::test_usage_attributed_to_agent_and_idea` |
| 9 Hard spending stop | `test_costs.py::test_hard_stop_blocks_new_calls` |
| 10 Safe retry | `test_jobs.py::test_retry_and_idempotency` |
| 11 Restart preserves state | `test_jobs.py::test_restart_recovers_running_jobs` |
| 12 Unapproved external action cannot run | Phase 2 (approval queue); Phase 1 verifies unconfigured integrations refuse (`test_integrations.py`) |
| 15 UI reflects backend | `test_api.py` (API contract the UI consumes) |

Items 6, 7, 13, 14 belong to Phase 2.
