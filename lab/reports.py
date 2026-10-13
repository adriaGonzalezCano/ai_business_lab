"""Daily / weekly report. Separates completed, proposed, failed, forecast and verified-outcome items.
Nothing is padded: empty sections say so."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .costs import CostController
from .db import Database
from .util import loads, now_iso


def build_report(db: Database, costs: CostController, days: int = 1) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    def rows(sql: str, *p: Any) -> list[dict]:
        return [dict(r) for r in db.query(sql, (*p,))]
    decisions = rows("SELECT d.idea_id, i.name, d.round, d.outcome, d.overall_score, d.synthetic, d.created_at FROM "
                     "consensus_decisions d JOIN ideas i ON i.id=d.idea_id WHERE d.created_at>=? ORDER BY d.id DESC", since)
    created = rows("SELECT id, name, status, synthetic, created_at FROM ideas WHERE created_at>=? ORDER BY created_at DESC", since)
    failed_jobs = rows("SELECT id, kind, error, updated_at FROM jobs WHERE status='failed' AND updated_at>=?", since)
    failed_runs = rows("SELECT id, agent_id, task, detail, finished_at FROM agent_runs WHERE status IN ('failed','blocked') "
                       "AND finished_at>=?", since)
    needs_owner = rows("SELECT id, name, status FROM ideas WHERE status IN ('needs_human_review','needs_evidence') ORDER BY updated_at DESC")
    obs = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(verified),0) AS v FROM market_observations WHERE created_at>=?", (since,))
    spend = costs.summary()
    approved_synthetic = [d for d in decisions if d["outcome"] == "approved" and d["synthetic"]]
    return {
        "generated_at": now_iso(), "window_days": days,
        "completed": {
            "ideas_created": created,
            "committee_decisions": decisions,
            "observations_recorded": {"total": obs["n"], "machine_verified": obs["v"]},
        },
        "proposed": {"awaiting_owner": needs_owner,
                     "note": "Ideas listed here need your decision or more evidence. Nothing is purchased or published automatically."},
        "failed": {"jobs": failed_jobs, "agent_runs": failed_runs},
        "forecasts": {"note": "Phase 1 produces no revenue forecasts. Idea estimates are assumptions, not income."},
        "verified_business_outcomes": {"sales": 0, "revenue_eur": 0,
                                       "note": "No business has been launched; there are no verified sales or revenue."},
        "ai_spend": {k: spend[k] for k in ("month", "currency", "limit", "spent_total", "pct_used", "new_work_blocked")},
        "warnings": ([f"{len(approved_synthetic)} approval(s) in this window rest on synthetic mock reviews."]
                     if approved_synthetic else []) + [a["message"] for a in spend["alerts"]],
    }
