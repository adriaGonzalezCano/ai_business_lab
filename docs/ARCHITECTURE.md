# Architecture

## Stack decision

| Layer | Choice | Why | Upgrade path |
|---|---|---|---|
| API | Starlette (ASGI) + pydantic v2, served by uvicorn | Small, fast, typed request/response models. | Swap in FastAPI: route handlers are thin and call framework-free services. |
| Domain | Plain Python modules (`lab/*.py`) | No framework coupling; unit-testable without a server. | — |
| Storage | SQLite (WAL, foreign keys on) via stdlib `sqlite3`, versioned `.sql` migrations | Zero install, one file to back up. | PostgreSQL: SQL is portable; replace `lab/db.py`. |
| Jobs | DB-backed queue + one worker thread | Survives restarts, no broker. | Dedicated worker process / Celery-style. |
| UI | Static HTML + vanilla JS + CSS, no build step | Works anywhere; every control calls the real API. | React + TypeScript consuming the same `/api`. |
| AI | Provider adapters (`mock`, `anthropic`) behind one interface | Cost control and tests do not depend on a vendor. | Add adapters (OpenAI-compatible, local). |

The master prompt prefers FastAPI + React. This build ran in an environment where PyPI was blocked, so the MVP was built on the packages that were available (Starlette, pydantic, uvicorn) and the standard library. Nothing in the domain layer depends on that choice.

## Module map

```
lab/
  config.py        env-driven runtime config (paths, token, provider keys)
  db.py            connections, transactions, migrations
  migrations/      001_init.sql ... (applied in order, recorded)
  util.py          ids, timestamps, secret redaction
  settings.py      owner-setting definitions, validation, persistence, history
  agents.py        agent role registry + per-agent config
  audit.py         append-only audit events
  providers/       base interface, mock, anthropic
  costs.py         CostController (pricing, reserve/settle/release, limits, alerts)
  llm.py           LLMGateway: budget check -> provider -> settle -> validate JSON
  ideas.py         idea repository, versions, evidence, duplicates, events
  rules.py         condition evaluator, gates, scoring (pure functions)
  committee.py     reviewers, deterministic chair, bounded revision loop
  research.py      fetcher (robots-aware), extraction, verification, scouting
  jobs.py          persistent queue, idempotency, retries, recovery
  integrations.py  capability registry; unconfigured => unavailable
  reports.py       daily report
  api.py           HTTP routes + auth
  server.py        entrypoint
ui/                index.html, app.js, styles.css
tests/             unittest-compatible (also runs under pytest)
```

Dependency direction: `api → services → db`. Services never import `api`. `rules.py` imports nothing from the rest of the lab (pure).

## Key flows

**Evaluate an idea**

1. `POST /api/ideas/{id}/evaluate` enqueues/runs `committee.run`.
2. For each independent reviewer role → `LLMGateway.complete_json` → `CostController.authorize` (reserve worst-case cost; reject if any limit would be crossed) → provider call → `settle` with *provider-reported* tokens.
3. Reviewer JSON is validated by pydantic; scores outside the role's allowed categories are discarded.
4. `rules.evaluate` builds a metrics dict (idea numbers, evidence counts, category scores, owner settings), evaluates every gate condition, applies hard rejections.
5. Chair (deterministic) combines gate results + consensus rules → `approved | revise | rejected | needs_evidence | needs_human`.
6. `revise` triggers the generator agent (bounded by `max_revision_rounds`), producing a new idea version, then a fresh round.
7. A `consensus_decisions` row pins the profile version, gate results, rule results, dissent and explanation.

**Cost control** — see `COST_MODEL.md` and `SECURITY.md`. Limits are enforced *before* a call is made; a hard stop applies to new calls, and in-flight reservations count against the budget so concurrent calls cannot jointly overshoot.

## Separation of propose / authorize / execute / verify

- Propose: agents write ideas, evaluations, (later) action proposals.
- Authorize: only owner-authenticated API routes change settings, profile, agent permissions or approve overrides. Agents have no code path to those.
- Execute: integrations adapters; unconfigured ones raise `IntegrationUnavailable`.
- Verify: research excerpts are checked against stored page text by code, not by a model.

## Known limitations

- SQLite single-writer; fine for one owner, not for multi-user.
- Anthropic offers no hard account-level spend cap through the API; the cap here is application-level and only covers calls made through this app. Set a limit in your provider console as well.
- Costs are estimates from configured prices × provider-reported tokens; reconcile with the provider invoice.
