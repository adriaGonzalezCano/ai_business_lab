# Roadmap

Each milestone ends with: app runs, tests pass, documented functionality usable.

## Phase 1 — Local MVP (this repository state)

- [x] M1.1 Skeleton, migrations, auth, audit, health
- [x] M1.2 Settings engine + agent registry
- [x] M1.3 Cost controller + provider adapters (mock, anthropic)
- [x] M1.4 Ideas, evidence, versions, duplicate detection
- [x] M1.5 Rules engine (gates, scoring, hard rejections) + committee/chair
- [x] M1.6 Research workflow (fetch → extract → verify → scout) + job queue
- [x] M1.7 Dashboard UI + daily report
- [ ] M1.8 First live run with a real provider key (owner action; requires `ANTHROPIC_API_KEY` and model pricing in settings)

## Phase 2 — Business incubation

- M2.1 Approval queue (proposed action, rationale, cost, benefit, risks, approve/reject) and per-business autonomy mode
- M2.2 Business workspace creation from an approved idea (blocked for synthetic-only approvals without override)
- M2.3 Task engine: dependencies, priorities, deadlines, idempotency, verification-of-completion
- M2.4 Validation experiments: hypothesis, thresholds, cost cap, results, decision
- M2.5 Financial ledger (integer cents), per-business budgets, contribution margin / operating profit / pre-tax estimate
- M2.6 Lead-agent planning with per-business spend caps

## Phase 3 — Real operations

- M3.1 First integrations (analytics, one storefront/payment adapter, image generation) with documented restrictions
- M3.2 Asset generation + quality checks; controlled publishing behind approvals
- M3.3 Sales/analytics ingestion; "verified" vs "simulated" sale separation
- M3.4 Observe → diagnose → propose → authorize → execute → measure → keep/revert loop with version history

## Phase 4 — Portfolio

- M4.1 Multiple concurrent businesses, cross-business comparison
- M4.2 Resource allocation suggestions, risk concentration
- M4.3 Learning from outcomes (with correlation ≠ causation guardrails)
- M4.4 Bounded autonomy refinements

## Upgrade path items

- FastAPI + React/TypeScript front end on the existing `/api`
- PostgreSQL migration
- Separate worker process
