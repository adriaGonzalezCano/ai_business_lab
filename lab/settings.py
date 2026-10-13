"""Owner settings: definitions, validation, persistence, change history.

Every spec §4 setting is a row in DEFINITIONS. Values live in the DB, so they are editable without
touching source code. Defaults suit a solo founder with a small budget and never enable spending.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .audit import Audit
from .db import Database
from .util import dumps, loads, now_iso


@dataclass(frozen=True)
class SettingDef:
    key: str
    group: str            # financial | strategic | agents | system
    label: str
    type: str             # number | int | bool | string | enum | list | json
    default: Any
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key, "group": self.group, "label": self.label, "type": self.type,
            "default": self.default, "help": self.help, "min": self.min, "max": self.max,
            "choices": list(self.choices), "unit": self.unit,
        }


def _d(*a, **k) -> SettingDef:
    return SettingDef(*a, **k)


DEFINITIONS: list[SettingDef] = [
    # ---- Financial -------------------------------------------------
    _d("currency", "financial", "Currency", "string", "EUR", "ISO currency code used for display and limits."),
    _d("max_monthly_ai_spend_eur", "financial", "Max total monthly AI spend", "number", 20.0,
       "Hard cap for all AI calls in a calendar month (UTC).", min=0, max=100000, unit="EUR"),
    _d("default_agent_monthly_budget_eur", "financial", "Default per-agent monthly AI budget", "number", 5.0,
       "Used when an agent has no budget of its own.", min=0, max=100000, unit="EUR"),
    _d("max_budget_per_business_eur", "financial", "Max budget per proposed business", "number", 200.0,
       "Setup cost above this fails the feasibility gate.", min=0, max=1e7, unit="EUR"),
    _d("max_validation_experiment_budget_eur", "financial", "Max budget per validation experiment", "number", 50.0,
       "Applied to experiments in Phase 2.", min=0, max=1e7, unit="EUR"),
    _d("max_cumulative_investment_before_approval_eur", "financial", "Max cumulative investment before human approval",
       "number", 100.0, "Above this, spending needs explicit owner approval.", min=0, max=1e7, unit="EUR"),
    _d("target_monthly_profit_eur", "financial", "Target monthly profit", "number", 500.0,
       "A goal, never a promise.", min=0, max=1e7, unit="EUR"),
    _d("min_contribution_margin_pct", "financial", "Minimum acceptable contribution margin", "number", 60.0,
       "(price - variable cost) / price.", min=0, max=100, unit="%"),
    _d("max_days_to_validate", "financial", "Max acceptable time to validate a business", "int", 30,
       "Maximum days to first evidence of demand.", min=1, max=3650, unit="days"),
    _d("min_cash_reserve_eur", "financial", "Minimum cash reserve", "number", 200.0,
       "Cash that must remain untouched.", min=0, max=1e7, unit="EUR"),
    _d("ai_budget_warn_pct", "financial", "AI budget warning threshold", "number", 70.0,
       "Owner alert when spend reaches this share of a budget.", min=1, max=100, unit="%"),
    _d("ai_critical_reserve_pct", "financial", "AI budget reserve for critical operations", "number", 10.0,
       "Share of the budget only calls flagged critical may use.", min=0, max=50, unit="%"),
    _d("model_pricing", "financial", "Model pricing (per 1M tokens)", "json",
       {"mock/mock-1": {"input_per_mtok": 0.0, "output_per_mtok": 0.0}},
       "Map 'provider/model' -> {input_per_mtok, output_per_mtok} in your currency. "
       "A model with no price here cannot be called."),
    # ---- Strategic -------------------------------------------------
    _d("preferred_categories", "strategic", "Preferred business categories", "list",
       ["digital_products", "templates", "micro_saas", "datasets", "education"],
       "Used to prioritize research and as context for agents."),
    _d("excluded_categories", "strategic", "Categories to exclude", "list",
       ["adult", "gambling", "crypto_trading", "medical_advice", "financial_advice", "weapons"],
       "Ideas in these categories are hard-rejected."),
    _d("max_operational_complexity", "strategic", "Max operational complexity (1-5)", "int", 3,
       "1 = trivial, 5 = needs a team.", min=1, max=5),
    _d("max_human_hours_per_week", "strategic", "Max acceptable human workload", "number", 5.0,
       "Ongoing hours per week for one business.", min=0, max=168, unit="h/week"),
    _d("desired_automation_pct", "strategic", "Desired degree of automation", "number", 70.0,
       "Share of recurring work that should be automatable.", min=0, max=100, unit="%"),
    _d("revenue_preference", "strategic", "Recurring vs one-off revenue", "enum", "balanced",
       "Preference used when ranking ideas.", choices=("recurring", "balanced", "one_off")),
    _d("profitability_horizon_months", "strategic", "Preferred time horizon for profitability", "int", 6,
       "", min=1, max=120, unit="months"),
    _d("markets", "strategic", "Geographic markets", "list", ["EU"], ""),
    _d("languages", "strategic", "Languages", "list", ["en", "es"], ""),
    _d("acceptable_risk_level", "strategic", "Acceptable risk level", "enum", "low",
       "Highest risk level an idea may carry.", choices=("low", "medium", "high")),
    _d("min_evidence_count", "strategic", "Minimum evidence items per idea", "int", 3,
       "", min=0, max=100),
    _d("min_verified_facts", "strategic", "Minimum verified factual evidence items", "int", 1,
       "Facts whose excerpt was verified against the stored source.", min=0, max=100),
    _d("min_evidence_confidence", "strategic", "Minimum evidence confidence", "number", 0.5,
       "Observations below this cannot support an idea.", min=0, max=1),
    _d("duplicate_similarity_threshold", "strategic", "Duplicate idea similarity threshold", "number", 0.6,
       "Jaccard similarity above which a new idea is treated as a duplicate.", min=0.1, max=1),
    # ---- Agents & system ------------------------------------------
    _d("default_autonomy_mode", "agents", "Default autonomy mode", "enum", "advisory",
       "advisory = agents propose only; supervised/bounded arrive in Phase 2.",
       choices=("advisory", "supervised", "bounded")),
    _d("default_provider", "agents", "Default model provider", "enum", "mock",
       "mock = free, deterministic, synthetic output. anthropic requires ANTHROPIC_API_KEY.",
       choices=("mock", "anthropic")),
    _d("default_model", "agents", "Default model", "string", "mock-1",
       "Model id used by agents that have no model of their own."),
    _d("max_concurrent_agent_runs", "agents", "Concurrency limit", "int", 2, "", min=1, max=32),
    _d("max_identical_prompts", "agents", "Loop detection: identical prompts allowed per hour", "int", 3,
       "Repeating the same prompt more often than this is blocked.", min=1, max=50),
    _d("cache_ttl_hours", "agents", "Response cache lifetime", "int", 168, "", min=0, max=8760, unit="h"),
    _d("research_schedule_enabled", "agents", "Scheduled research enabled", "bool", False,
       "When on, research runs automatically over the seeds below."),
    _d("research_frequency_hours", "agents", "Scheduled research frequency", "int", 24, "", min=1, max=720, unit="h"),
    _d("research_seeds", "agents", "Research seeds", "json", [],
       "List of {topic, urls[]}. The fetcher only reads URLs listed here or passed explicitly."),
    _d("research_fetch_delay_seconds", "agents", "Delay between fetches", "number", 2.0, "", min=0, max=60, unit="s"),
    _d("approve_after_max_rounds", "agents", "When revision rounds run out", "enum", "needs_human",
       "What the chair does if consensus is still impossible.",
       choices=("needs_human", "reject", "return_to_research")),
]

BY_KEY = {d.key: d for d in DEFINITIONS}


def validate(defn: SettingDef, value: Any) -> Any:
    t = defn.type
    if t in ("number", "int"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{defn.key}: must be a number")
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{defn.key}: must be finite")
        if t == "int":
            if v != int(v):
                raise ValueError(f"{defn.key}: must be a whole number")
            v = int(v)
        if defn.min is not None and v < defn.min:
            raise ValueError(f"{defn.key}: must be >= {defn.min}")
        if defn.max is not None and v > defn.max:
            raise ValueError(f"{defn.key}: must be <= {defn.max}")
        return v
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{defn.key}: must be true or false")
        return value
    if t == "string":
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"{defn.key}: must be a non-empty string (max 200 chars)")
        return value.strip()
    if t == "enum":
        if value not in defn.choices:
            raise ValueError(f"{defn.key}: must be one of {list(defn.choices)}")
        return value
    if t == "list":
        if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
            raise ValueError(f"{defn.key}: must be a list of non-empty strings")
        return [x.strip() for x in value][:100]
    if t == "json":
        if defn.key == "model_pricing":
            return _validate_pricing(value)
        if defn.key == "research_seeds":
            return _validate_seeds(value)
        return value
    raise ValueError(f"{defn.key}: unsupported type {t}")


def _validate_pricing(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("model_pricing: must be an object")
    out: dict[str, dict[str, float]] = {}
    for k, v in value.items():
        if not isinstance(k, str) or "/" not in k:
            raise ValueError("model_pricing: keys must look like 'provider/model'")
        if not isinstance(v, dict) or set(v) - {"input_per_mtok", "output_per_mtok"}:
            raise ValueError(f"model_pricing[{k}]: expected input_per_mtok and output_per_mtok")
        try:
            pi, po = float(v["input_per_mtok"]), float(v["output_per_mtok"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"model_pricing[{k}]: both prices must be numbers") from None
        if pi < 0 or po < 0:
            raise ValueError(f"model_pricing[{k}]: prices must be >= 0")
        out[k] = {"input_per_mtok": pi, "output_per_mtok": po}
    return out


def _validate_seeds(value: Any) -> list:
    if not isinstance(value, list):
        raise ValueError("research_seeds: must be a list")
    out = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("topic"), str) or not item["topic"].strip():
            raise ValueError("research_seeds: each item needs a topic")
        urls = item.get("urls", [])
        if not isinstance(urls, list) or not all(isinstance(u, str) and u.startswith(("http://", "https://")) for u in urls):
            raise ValueError("research_seeds: urls must be http(s) URLs")
        out.append({"topic": item["topic"].strip(), "urls": urls[:20]})
    return out[:50]


class SettingsStore:
    def __init__(self, db: Database, audit: Audit):
        self.db = db
        self.audit = audit

    def get(self, key: str) -> Any:
        defn = BY_KEY[key]
        row = self.db.one("SELECT value_json FROM owner_settings WHERE key=?", (key,))
        if row is None:
            return defn.default
        return loads(row["value_json"], defn.default)

    def all(self) -> dict[str, Any]:
        stored = {r["key"]: loads(r["value_json"]) for r in self.db.query("SELECT key, value_json FROM owner_settings")}
        return {d.key: stored.get(d.key, d.default) for d in DEFINITIONS}

    def describe(self) -> list[dict[str, Any]]:
        vals = self.all()
        return [{**d.public(), "value": vals[d.key]} for d in DEFINITIONS]

    def set(self, key: str, value: Any, actor: str = "owner") -> Any:
        if key not in BY_KEY:
            raise KeyError(f"unknown setting: {key}")
        clean = validate(BY_KEY[key], value)
        with self.db.tx():
            old = self.db.one("SELECT value_json FROM owner_settings WHERE key=?", (key,))
            old_json = old["value_json"] if old else dumps(BY_KEY[key].default)
            new_json = dumps(clean)
            if old_json != new_json:
                self.db.execute(
                    "INSERT INTO owner_settings(key, value_json, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                    (key, new_json, now_iso()),
                )
                self.db.execute(
                    "INSERT INTO settings_history(key, old_json, new_json, actor, changed_at) VALUES (?,?,?,?,?)",
                    (key, old_json, new_json, actor, now_iso()),
                )
                self.audit.log(actor, "setting_changed", "setting", key, {"old": loads(old_json), "new": clean})
        return clean

    def set_many(self, values: dict[str, Any], actor: str = "owner") -> dict[str, Any]:
        # validate everything first so a bad value changes nothing
        cleaned = {}
        for k, v in values.items():
            if k not in BY_KEY:
                raise KeyError(f"unknown setting: {k}")
            cleaned[k] = validate(BY_KEY[k], v)
        with self.db.tx():
            for k, v in cleaned.items():
                self.set(k, v, actor)
        return cleaned

    def history(self, key: str | None = None, limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM settings_history", []
        if key:
            sql += " WHERE key=?"
            params.append(key)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [
            {"key": r["key"], "old": loads(r["old_json"]), "new": loads(r["new_json"]),
             "actor": r["actor"], "changed_at": r["changed_at"]}
            for r in self.db.query(sql, params)
        ]
