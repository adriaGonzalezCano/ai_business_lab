# Progress / handoff log

Read this first when resuming ("continue"). Update it at the end of every work session.

## Environment notes

- Cloud workspace could NOT reach PyPI (egress proxy 403, organization policy). Do not try to route around it.
- Available there: Python 3.13, pydantic 2, starlette 1.x, uvicorn, stdlib. NOT available: fastapi, sqlalchemy, pytest, alembic, httpx.
- Therefore: backend = Starlette + pydantic + stdlib sqlite3; tests = `unittest` (also run by pytest); API tests use a tiny ASGI client in `tests/helpers.py` (no httpx).
- Run tests: `python3 -I -m unittest discover -s tests` from the repo root. Use `-W error::ResourceWarning` to catch leaked DB connections.
- Starlette 1.x has no `on_startup/on_shutdown`; the app uses `lifespan`.

## Status (update below)

| Step | State |
|---|---|
| Repo attached (public, `main`) | done |
| Design docs (PRD, architecture, cost model, roadmap) | done (ROADMAP checkboxes corrected below) |
| Backend domain modules (`lab/*.py`) | done, import cleanly |
| `lab/app.py` (composition root), `lab/api.py`, `lab/server.py` | done |
| Tests: ideas, rules/committee, costs | done, passing |
| Tests: jobs/restart, research, API security/flow, reports | done (123 passing) |
| UI (`ui/`) | not started |
| README / DATA_MODEL / SECURITY / .env.example | not started |

## Decisions and findings

- The mock provider scores from fixed formulas over the idea's numbers (e.g. feasibility = 100 - setup/4 - 6*hours). It is not owner-setting aware; tests that need a setting to matter assert on gate *conditions*, not mock scores.
- Bug fixed: `sweep_stale_reservations` skipped reservations made in the same second as a restart (`<` vs `<=`).
- `Database.close()` now closes connections of all threads (needed for Windows temp-dir cleanup).
- Approval by the committee on mock reviews is flagged `synthetic` in decisions, evaluations, events and reports.

## Next actions

1. (done) Backend tests: 123 passing.
2. Build `ui/` (index.html, app.js, styles.css) served by `lab/api.py`; every control wired to `/api`.
3. Write README, `.env.example`, DATA_MODEL, SECURITY; finish COST_MODEL accuracy check.
4. Correct ROADMAP checkboxes to match reality (M1.7 UI is not done until `ui/` exists).
5. Commit; push may be refused (see below).

## Git

- Local commits are made in `/home/claude/ai_business_lab`. Earlier pushes from the cloud session were refused because the Claude GitHub App was not installed for the repo. If a push fails, tell the owner; do not work around it.
