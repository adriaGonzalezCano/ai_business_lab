"""Central AI cost controller.

Flow for every model call:  authorize (reserve worst case) -> provider call -> settle (provider-reported
tokens x configured prices) | release (call failed before billing).

Because the reservation happens inside one IMMEDIATE transaction and counts against the budget until it
is settled, concurrent calls cannot jointly overshoot a limit. The only possible overshoot is input-token
estimation error (the estimate is deliberately conservative: 1 token per 3 characters).

LIMITATION: this caps calls made through this application only. Providers such as Anthropic do not offer a
hard per-key spend cap through the API, so also set a limit in the provider console.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import Audit
from .db import Database
from .settings import SettingsStore
from .util import month_key, now_iso, round_money


class BudgetExceeded(RuntimeError):
    def __init__(self, scope: str, message: str):
        super().__init__(message)
        self.scope = scope


class CircularActivity(RuntimeError):
    pass


class PricingMissing(RuntimeError):
    pass


def estimate_tokens(text: str) -> int:
    """Conservative (over-)estimate of input tokens: 1 token per 3 characters."""
    return max(1, math.ceil(len(text) / 3))


def prompt_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


_COST_SQL = (
    "COALESCE(SUM(CASE status WHEN 'final' THEN COALESCE(actual_cost, estimated_cost) "
    "WHEN 'reserved' THEN reserved_cost ELSE 0 END), 0)"
)


@dataclass
class Scope:
    name: str
    column: str | None
    value: str | None
    limit: float


class CostController:
    def __init__(self, db: Database, settings: SettingsStore, audit: Audit):
        self.db, self.settings, self.audit = db, settings, audit

    # -- pricing -------------------------------------------------------
    def price(self, provider: str, model: str) -> tuple[float, float]:
        table = self.settings.get("model_pricing")
        entry = table.get(f"{provider}/{model}")
        if entry is None:
            if provider == "mock":
                return 0.0, 0.0
            raise PricingMissing(
                f"No price configured for {provider}/{model}. Add it to the 'model_pricing' setting "
                "(per 1M tokens) so spending can be limited; calls are refused until then."
            )
        return float(entry["input_per_mtok"]), float(entry["output_per_mtok"])

    def cost_of(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
        pin, pout = self.price(provider, model)
        return round_money(input_tokens * pin / 1e6 + output_tokens * pout / 1e6)

    # -- spend queries -------------------------------------------------
    def spent(self, month: str | None = None, **filters: str) -> float:
        sql = f"SELECT {_COST_SQL} AS c FROM ai_usage WHERE month=?"
        params: list[Any] = [month or month_key()]
        for col, val in filters.items():
            if col not in ("agent_id", "business_id", "experiment_id", "idea_id", "task"):
                raise ValueError(col)
            sql += f" AND {col}=?"
            params.append(val)
        return round_money(self.db.one(sql, params)["c"])

    def _agent_limit(self, agent_id: str) -> float:
        r = self.db.one("SELECT monthly_budget_eur FROM agents WHERE id=?", (agent_id,))
        if r is not None and r["monthly_budget_eur"] is not None:
            return float(r["monthly_budget_eur"])
        return float(self.settings.get("default_agent_monthly_budget_eur"))

    # -- authorize / settle / release ------------------------------------
    def authorize(self, *, agent_id: str, provider: str, model: str, task: str, est_input_tokens: int,
                  max_output_tokens: int, run_id: str | None = None, idea_id: str | None = None,
                  business_id: str | None = None, experiment_id: str | None = None,
                  prompt_hash_: str | None = None, critical: bool = False,
                  business_limit: float | None = None, experiment_limit: float | None = None) -> int:
        pin, pout = self.price(provider, model)
        worst = round_money(est_input_tokens * pin / 1e6 + max_output_tokens * pout / 1e6)
        month = month_key()
        reserve_pct = float(self.settings.get("ai_critical_reserve_pct"))
        warn_pct = float(self.settings.get("ai_budget_warn_pct"))
        max_identical = int(self.settings.get("max_identical_prompts"))

        scopes = [
            Scope("total", None, None, float(self.settings.get("max_monthly_ai_spend_eur"))),
            Scope(f"agent:{agent_id}", "agent_id", agent_id, self._agent_limit(agent_id)),
        ]
        if business_id and business_limit is not None:
            scopes.append(Scope(f"business:{business_id}", "business_id", business_id, business_limit))
        if experiment_id and experiment_limit is not None:
            scopes.append(Scope(f"experiment:{experiment_id}", "experiment_id", experiment_id, experiment_limit))

        refusal: tuple[str, str, str] | None = None  # (kind, scope, message)
        warnings: list[tuple[str, float]] = []
        usage_id = 0
        with self.db.tx():
            if prompt_hash_ and max_identical > 0:
                since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
                n = self.db.one(
                    "SELECT COUNT(*) AS n FROM ai_usage WHERE prompt_hash=? AND status IN ('reserved','final') "
                    "AND created_at>=?", (prompt_hash_, since))["n"]
                if n >= max_identical:
                    refusal = ("loop", "loop", f"Identical prompt already sent {n} times in the last hour "
                               f"(limit {max_identical}); blocked as a possible loop.")
            if refusal is None:
                for sc in scopes:
                    where, params = "month=?", [month]
                    if sc.column:
                        where += f" AND {sc.column}=?"
                        params.append(sc.value)
                    cur = self.db.one(f"SELECT {_COST_SQL} AS c FROM ai_usage WHERE {where}", params)["c"]
                    projected = cur + worst
                    ceiling = sc.limit if critical else sc.limit * (1 - reserve_pct / 100)
                    if projected > ceiling + 1e-12:
                        refusal = ("hard_stop", sc.name,
                                   f"AI budget limit reached for {sc.name}: spent+reserved {cur:.4f} + this call "
                                   f"{worst:.4f} would exceed {'limit' if critical else 'limit minus critical reserve'} "
                                   f"{ceiling:.4f}.")
                        break
                    if sc.limit > 0 and projected / sc.limit * 100 >= warn_pct:
                        warnings.append((sc.name, projected / sc.limit * 100))
            if refusal is None:
                cur = self.db.execute(
                    "INSERT INTO ai_usage(run_id,agent_id,provider,model,task,idea_id,business_id,experiment_id,"
                    "prompt_hash,status,critical,reserved_cost,month,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'reserved',?,?,?,?)",
                    (run_id, agent_id, provider, model, task, idea_id, business_id, experiment_id,
                     prompt_hash_, 1 if critical else 0, worst, month, now_iso()),
                )
                usage_id = int(cur.lastrowid)
                for scope_name, pct in warnings:
                    self._alert(scope_name, "warning", month,
                                f"AI spend for {scope_name} reached {pct:.0f}% of its budget.")
        if refusal is not None:
            kind, scope_name, message = refusal
            if kind == "hard_stop":
                self._alert(scope_name, "hard_stop", month, message)
                raise BudgetExceeded(scope_name, message)
            raise CircularActivity(message)
        return usage_id

    def settle(self, usage_id: int, input_tokens: int, output_tokens: int, actual_cost: float | None = None) -> float:
        row = self.db.one("SELECT provider, model, status FROM ai_usage WHERE id=?", (usage_id,))
        if row is None or row["status"] != "reserved":
            raise ValueError(f"usage {usage_id} is not a pending reservation")
        est = self.cost_of(row["provider"], row["model"], input_tokens, output_tokens)
        self.db.execute(
            "UPDATE ai_usage SET status='final', input_tokens=?, output_tokens=?, estimated_cost=?, actual_cost=?, "
            "settled_at=? WHERE id=?",
            (input_tokens, output_tokens, est, actual_cost, now_iso(), usage_id),
        )
        return est

    def release(self, usage_id: int) -> None:
        """Call failed before the provider billed anything: free the reservation (idempotent)."""
        self.db.execute("UPDATE ai_usage SET status='released', settled_at=? WHERE id=? AND status='reserved'",
                        (now_iso(), usage_id))

    def record_cache_hit(self, *, agent_id: str, provider: str, model: str, task: str,
                         idea_id: str | None = None, business_id: str | None = None,
                         run_id: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO ai_usage(run_id,agent_id,provider,model,task,idea_id,business_id,status,month,created_at,settled_at)"
            " VALUES (?,?,?,?,?,?,?,'cached',?,?,?)",
            (run_id, agent_id, provider, model, task, idea_id, business_id, month_key(), now_iso(), now_iso()),
        )

    def sweep_stale_reservations(self, older_than_minutes: int = 30) -> int:
        """After a crash, reservations never settled are released so they stop blocking the budget."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat(timespec="seconds")
        cur = self.db.execute(
            # '<=' because timestamps have one-second resolution: with a 0-minute window (startup) a reservation
            # made in the same second must still be released.
            "UPDATE ai_usage SET status='released', settled_at=? WHERE status='reserved' AND created_at<=?",
            (now_iso(), cutoff))
        return cur.rowcount

    # -- alerts & reporting ----------------------------------------------
    def _alert(self, scope: str, level: str, month: str, message: str) -> None:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO budget_alerts(scope, level, month, message, created_at) VALUES (?,?,?,?,?)",
            (scope, level, month, message, now_iso()))
        if cur.rowcount:
            self.audit.log("system", f"budget_{level}", "budget", scope, {"message": message})

    def alerts(self, include_acknowledged: bool = False) -> list[dict]:
        sql = "SELECT * FROM budget_alerts"
        if not include_acknowledged:
            sql += " WHERE acknowledged=0"
        return [dict(r) for r in self.db.query(sql + " ORDER BY id DESC LIMIT 100")]

    def acknowledge_alert(self, alert_id: int) -> None:
        self.db.execute("UPDATE budget_alerts SET acknowledged=1 WHERE id=?", (alert_id,))

    def is_exhausted(self, critical: bool = False) -> bool:
        """True when new non-essential work should not start (total budget minus reserve is used)."""
        limit = float(self.settings.get("max_monthly_ai_spend_eur"))
        reserve = float(self.settings.get("ai_critical_reserve_pct"))
        ceiling = limit if critical else limit * (1 - reserve / 100)
        return self.spent() >= ceiling - 1e-12

    def summary(self, month: str | None = None) -> dict[str, Any]:
        month = month or month_key()
        limit = float(self.settings.get("max_monthly_ai_spend_eur"))
        total = self.spent(month)
        def group(col: str) -> list[dict]:
            rows = self.db.query(
                f"SELECT {col} AS k, {_COST_SQL} AS cost, "
                "SUM(CASE WHEN status='final' THEN 1 ELSE 0 END) AS calls, "
                "SUM(CASE WHEN status='cached' THEN 1 ELSE 0 END) AS cache_hits, "
                "COALESCE(SUM(input_tokens),0) AS in_tok, COALESCE(SUM(output_tokens),0) AS out_tok "
                f"FROM ai_usage WHERE month=? AND {col} IS NOT NULL GROUP BY {col} ORDER BY cost DESC", (month,))
            return [{"key": r["k"], "cost": round_money(r["cost"]), "calls": r["calls"],
                     "cache_hits": r["cache_hits"], "input_tokens": r["in_tok"], "output_tokens": r["out_tok"]}
                    for r in rows]
        reserved = round_money(self.db.one(
            "SELECT COALESCE(SUM(reserved_cost),0) AS c FROM ai_usage WHERE month=? AND status='reserved'", (month,))["c"])
        return {
            "month": month, "currency": self.settings.get("currency"), "limit": limit,
            "spent_total": total, "reserved_in_flight": reserved,
            "pct_used": round(total / limit * 100, 1) if limit > 0 else None,
            "warn_pct": float(self.settings.get("ai_budget_warn_pct")),
            "hard_stopped": self.is_exhausted(critical=True),
            "new_work_blocked": self.is_exhausted(),
            "by_agent": group("agent_id"), "by_idea": group("idea_id"),
            "by_business": group("business_id"), "by_task": group("task"),
            "alerts": self.alerts(),
        }

    def usage_rows(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM ai_usage ORDER BY id DESC LIMIT ?", (min(limit, 500),))]
