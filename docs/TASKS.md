# Work plan: small chunks

Each chunk is sized to finish inside one short session (a free-plan session is enough): one goal, a few files,
one check at the end. Do them in order inside a track; tracks U (UI) and D (docs) are independent of each other.

How to resume: read `docs/PROGRESS.md`, take the first unchecked chunk below, do only that chunk, run the check,
tick the box, add one line to PROGRESS.md, commit. Do not start the next chunk in the same session if you are
running low on usage; stop after committing.

Run tests: `python3 -I -m unittest discover -s tests` (from the repo root). Expect everything to pass before and after.

## Track U: user interface (vanilla JS, no build step; files in `ui/`)

Conventions for all UI chunks: build DOM with `Lab.h()` and `textContent` only (page and agent text is untrusted, never use
`innerHTML`); no inline `<script>`/`style=` (the CSP forbids them; use classes and `el.style.setProperty`); sentence case,
status shown as words plus colour; every button calls a real API route; every list has an empty state that says what to do.

- [x] **U0 Shell and styles**: `ui/index.html`, `ui/styles.css`, safe static-file route in `lab/api.py`. (done, committed)
- [ ] **U1 Core** (`ui/core.js`, `ui/main.js`): `Lab.h`/`Lab.svg` helpers, `Lab.api()` with bearer token in sessionStorage,
      login screen, hash router, room rail, top-bar AI-spend meter, toasts, dialog helper, polling helper.
      Check: with the server running, signing in with a wrong token shows an error, the right one shows the shell; routes
      without a view show a clear "not built" message.
- [ ] **U2 Overview and floor plan** (`ui/view-overview.js`): SVG floor plan of the seven rooms with live figures, agent dots
      (idle / working / disabled from `/api/agents` and `/api/runs`), "needs you" list, recent decisions, job status.
      Rooms for Phase 2 are drawn dashed and explained, not linked. Narrow screens get a list instead of the SVG.
- [ ] **U3 Research room** (`ui/view-research.js`): start a research run (topic + URLs), "process next job" button,
      observations table with topic filter and verified/synthetic marks, manual observation form, sources list.
- [ ] **U4 Ideas list and create** (`ui/view-ideas.js`): filterable, sortable table; "New idea" dialog with all fields
      (required ones first, the rest under "More detail"); duplicate (409) message links to the existing idea.
- [ ] **U5 Idea detail** (`ui/view-idea-detail.js`): facts, evidence list with verify toggle and add form, score breakdown vs
      weights, gate-by-gate conditions (pass/fail/unknown), consensus rules, objections, dissent, timeline, versions;
      actions: Evaluate (poll the job), owner decision with required reason, revise price/cost fields.
- [ ] **U6 Committee room** (`ui/view-committee.js`): latest decision per idea, reviewer roster with enable toggles,
      what is waiting for the owner.
- [ ] **U7 Analytics room** (`ui/view-analytics.js`): spend meter with warning tick, by agent / idea / task tables,
      alerts with acknowledge, usage log, daily and weekly report (completed / proposed / failed / forecasts / verified).
- [ ] **U8 Control room: settings and agents** (`ui/view-control.js`): settings by group with typed inputs, per-field reset,
      atomic save, change history; agents table (enable, provider, model, budget, iterations), permissions read-only.
- [ ] **U9 Control room: profile, jobs, integrations, audit** (`ui/view-control-more.js`): evaluation-profile editor
      (weights, consensus, per-gate minimum score, condition operator/value, mandatory and hard-reject flags, new version,
      view old versions); jobs with retry/cancel; integration catalogue with setup guides; audit log with filter.
- [ ] **U10 Browser check**: script `tests/ui_check.py` (Playwright, optional, not part of unittest) that signs in, visits every
      room, submits one form per room, fails on console errors; take screenshots, fix layout and accessibility problems found
      (keyboard focus, contrast, 360px width).

After U1 add the script tags for any new file to `ui/index.html` (U5 and U9 add `view-idea-detail.js` and `view-control-more.js`).

## Track D: documentation (can run any time)

- [ ] **D1 README and `.env.example`**: what it is, install (Python 3.11+, `pip install starlette uvicorn pydantic`), run
      (`python -m lab.server`), where the owner token lives, first run with the mock provider, how to switch to Anthropic
      (key, model pricing, provider setting), run tests, troubleshooting (Windows PATH, port in use, token lost).
- [ ] **D2 `docs/DATA_MODEL.md` and `docs/SECURITY.md`**: tables with purpose and key constraints (from
      `lab/migrations/001_init.sql`), what is pinned for history; threat model, controls, known limits (single process, DNS
      rebinding window, provider caps).
- [ ] **D3 Accuracy pass**: compare `docs/COST_MODEL.md`, `docs/PRODUCT_REQUIREMENTS.md`, `docs/ARCHITECTURE.md` with the code;
      correct `docs/ROADMAP.md` checkboxes (M1.7 is only done after U1 to U9).

## Track G: release

- [ ] **G1 Final check and handoff**: tests pass, the server starts, `docs/PROGRESS.md` is current, everything committed.
      Pushing needs the Claude GitHub App on the repository (owner action); otherwise the owner pulls the files another way.

## Later phases (not started; each phase becomes its own chunk list when reached)

Phase 2 incubation, Phase 3 real operations, Phase 4 portfolio. See `docs/ROADMAP.md`.
