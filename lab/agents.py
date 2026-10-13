"""Agent role registry. Roles are configuration rows; owners edit them, agents cannot."""
from __future__ import annotations

from typing import Any

from .audit import Audit
from .db import Database
from .settings import SettingsStore
from .util import dumps, loads, now_iso

REVIEWER_ROLES = [
    "business_strategist", "financial_analyst", "market_analyst",
    "automation_engineer", "risk_compliance", "independent_critic",
]

# Which score categories each reviewer role may submit. Anything else is discarded.
ROLE_CATEGORIES: dict[str, list[str]] = {
    "business_strategist": ["market_demand", "scalability"],
    "financial_analyst": ["financial_viability"],
    "market_analyst": ["market_demand"],
    "automation_engineer": ["automation_potential", "feasibility"],
    "risk_compliance": ["risk_compliance"],
    "independent_critic": ["market_demand", "financial_viability", "feasibility"],
}

DEFAULTS: list[dict[str, Any]] = [
    dict(id="market_researcher", name="Market Researcher", kind="research", uses_model=1,
         description="Extracts verifiable observations (needs, niches, pricing, saturation) from fetched sources.",
         permissions=["web_fetch", "write_observation"], max_output_tokens=1500),
    dict(id="opportunity_scout", name="Opportunity Scout", kind="research", uses_model=1,
         description="Turns verified observations into specific, monetizable business opportunities.",
         permissions=["create_idea", "link_evidence"], max_output_tokens=2000),
    dict(id="research_verifier", name="Research Verifier", kind="research", uses_model=0,
         description="Deterministic: checks that every quoted excerpt really appears in the stored source text.",
         permissions=["verify_evidence"], max_output_tokens=0),
    dict(id="idea_generator", name="Idea Generator", kind="generation", uses_model=1,
         description="Improves an existing idea in response to reviewer objections (bounded revision rounds).",
         permissions=["revise_idea"], max_output_tokens=1500),
    dict(id="business_strategist", name="Business Strategist", kind="reviewer", uses_model=1,
         description="Strategic fit, differentiation, scalability.", permissions=["write_evaluation"]),
    dict(id="financial_analyst", name="Financial Analyst", kind="reviewer", uses_model=1,
         description="Challenges costs, margins, revenue and break-even assumptions.", permissions=["write_evaluation"]),
    dict(id="market_analyst", name="Market Analyst", kind="reviewer", uses_model=1,
         description="Customer demand, competition, distribution.", permissions=["write_evaluation"]),
    dict(id="automation_engineer", name="Automation Engineer", kind="reviewer", uses_model=1,
         description="Technical feasibility and ongoing workload.", permissions=["write_evaluation"]),
    dict(id="risk_compliance", name="Risk & Compliance Analyst", kind="reviewer", uses_model=1,
         description="Legal, IP, security and platform risks.", permissions=["write_evaluation"]),
    dict(id="independent_critic", name="Independent Critic", kind="reviewer", uses_model=1,
         description="Actively looks for reasons the proposal will fail.", permissions=["write_evaluation"]),
    dict(id="committee_chair", name="Committee Chair", kind="chair", uses_model=0,
         description="Deterministic: applies the owner's gates and consensus rules; explains the outcome.",
         permissions=["write_decision"], max_output_tokens=0),
]

EDITABLE = {
    "enabled": bool, "provider": (str, type(None)), "model": (str, type(None)),
    "max_iterations": int, "max_tool_calls": int, "max_output_tokens": int,
    "monthly_budget_eur": (int, float, type(None)), "escalation": dict,
}


class AgentRegistry:
    def __init__(self, db: Database, settings: SettingsStore, audit: Audit):
        self.db, self.settings, self.audit = db, settings, audit

    def seed_defaults(self) -> None:
        with self.db.tx():
            for a in DEFAULTS:
                if self.db.one("SELECT 1 FROM agents WHERE id=?", (a["id"],)):
                    continue
                self.db.execute(
                    "INSERT INTO agents(id,name,description,kind,enabled,uses_model,max_iterations,max_tool_calls,"
                    "max_output_tokens,permissions_json,escalation_json,updated_at) VALUES (?,?,?,?,1,?,?,?,?,?,?,?)",
                    (a["id"], a["name"], a["description"], a["kind"], a["uses_model"], 2, 5,
                     a.get("max_output_tokens", 1200), dumps(a.get("permissions", [])),
                     dumps({"escalate_on": "repeated_failure", "approval_required_for": ["spend", "publish"]}),
                     now_iso()),
                )

    @staticmethod
    def _row(r) -> dict[str, Any]:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        d["uses_model"] = bool(d["uses_model"])
        d["permissions"] = loads(d.pop("permissions_json"), [])
        d["escalation"] = loads(d.pop("escalation_json"), {})
        return d

    def list(self) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.query("SELECT * FROM agents ORDER BY kind, id")]

    def get(self, agent_id: str) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if r is None:
            raise KeyError(f"unknown agent: {agent_id}")
        return self._row(r)

    def update(self, agent_id: str, changes: dict[str, Any], actor: str = "owner") -> dict[str, Any]:
        """Owner-only (called from authenticated API routes). Permissions are not editable here by design."""
        self.get(agent_id)
        sets, params = [], []
        for k, v in changes.items():
            if k not in EDITABLE:
                raise ValueError(f"field not editable: {k}")
            if not isinstance(v, EDITABLE[k]) or (k != "enabled" and isinstance(v, bool)):
                raise ValueError(f"{k}: invalid type")
            if k in ("max_iterations", "max_tool_calls") and not (1 <= v <= 20):
                raise ValueError(f"{k}: must be between 1 and 20")
            if k == "max_output_tokens" and not (0 <= v <= 16000):
                raise ValueError("max_output_tokens: must be between 0 and 16000")
            if k == "monthly_budget_eur" and v is not None and v < 0:
                raise ValueError("monthly_budget_eur: must be >= 0")
            if k == "provider" and v is not None and v not in ("mock", "anthropic"):
                raise ValueError("provider: must be mock or anthropic")
            if k == "escalation":
                sets.append("escalation_json=?")
                params.append(dumps(v))
            elif k == "enabled":
                sets.append("enabled=?")
                params.append(1 if v else 0)
            else:
                sets.append(f"{k}=?")
                params.append(v)
        if not sets:
            return self.get(agent_id)
        sets.append("updated_at=?")
        params += [now_iso(), agent_id]
        with self.db.tx():
            self.db.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id=?", params)
            self.audit.log(actor, "agent_updated", "agent", agent_id, changes)
        return self.get(agent_id)

    def resolve_model(self, agent: dict[str, Any]) -> tuple[str, str]:
        provider = agent.get("provider") or self.settings.get("default_provider")
        model = agent.get("model") or self.settings.get("default_model")
        if provider == "mock" and not agent.get("model"):
            model = "mock-1"
        return provider, model

    def require_permission(self, agent_id: str, permission: str) -> None:
        a = self.get(agent_id)
        if not a["enabled"]:
            raise PermissionError(f"agent {agent_id} is disabled")
        if permission not in a["permissions"]:
            raise PermissionError(f"agent {agent_id} lacks permission {permission}")

    def enabled_reviewers(self) -> list[str]:
        out = []
        for role in REVIEWER_ROLES:
            r = self.db.one("SELECT enabled FROM agents WHERE id=?", (role,))
            if r and r["enabled"]:
                out.append(role)
        return out
