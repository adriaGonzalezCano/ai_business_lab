"""Typed structured outputs expected from agents. Anything that fails validation is rejected."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EvidenceKind = Literal["fact", "estimate", "assumption", "speculation"]
CATEGORIES = [
    "market_demand", "financial_viability", "automation_potential",
    "feasibility", "risk_compliance", "scalability",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ObservationItem(_Strict):
    statement: str = Field(min_length=5, max_length=600)
    kind: EvidenceKind
    excerpt: str = Field(default="", max_length=600)
    confidence: float = Field(ge=0, le=1)


class ObservationsOut(_Strict):
    observations: list[ObservationItem] = Field(default_factory=list, max_length=10)


class IdeaDraft(_Strict):
    name: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=10, max_length=2000)
    category: str = Field(min_length=2, max_length=60)
    target_audience: str = Field(min_length=3, max_length=500)
    customer_problem: str = Field(min_length=5, max_length=1000)
    product: str = Field(min_length=3, max_length=1000)
    usp: str = Field(default="", max_length=1000)
    revenue_model: str = Field(default="", max_length=500)
    channels: list[str] = Field(default_factory=list, max_length=10)
    scope: str = Field(default="", max_length=1000)
    setup_cost_eur: float = Field(default=0, ge=0, le=1e7)
    recurring_cost_eur_month: float = Field(default=0, ge=0, le=1e7)
    human_hours_setup: float = Field(default=0, ge=0, le=10000)
    human_hours_week: float = Field(default=0, ge=0, le=168)
    days_to_first_sale: int | None = Field(default=None, ge=0, le=3650)
    unit_price_eur: float | None = Field(default=None, ge=0, le=1e6)
    unit_variable_cost_eur: float | None = Field(default=None, ge=0, le=1e6)
    expected_units_month: float | None = Field(default=None, ge=0, le=1e7)
    automation_potential_pct: float | None = Field(default=None, ge=0, le=100)
    recurring_revenue: bool = False
    revenue_assumptions: str = Field(default="", max_length=1500)
    cost_assumptions: str = Field(default="", max_length=1500)
    competitors: str = Field(default="", max_length=1500)
    legal_risks: str = Field(default="", max_length=1500)
    automation_opportunities: str = Field(default="", max_length=1500)
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    validation_plan: str = Field(default="", max_length=1500)

    @field_validator("category")
    @classmethod
    def _norm_cat(cls, v: str) -> str:
        return "_".join(v.lower().replace("-", " ").split())


class ScoutIdea(IdeaDraft):
    evidence_observation_ids: list[str] = Field(default_factory=list, max_length=20)


class ScoutOut(_Strict):
    ideas: list[ScoutIdea] = Field(default_factory=list, max_length=3)


class Objection(_Strict):
    text: str = Field(min_length=3, max_length=800)
    severity: Literal["minor", "major", "hard"]


class ReviewOut(_Strict):
    assessment: str = Field(min_length=5, max_length=2500)
    scores: dict[str, int]
    evidence_cited: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    objections: list[Objection] = Field(default_factory=list, max_length=20)
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    recommendation: Literal["approve", "revise", "reject", "request_evidence"]
    confidence: float = Field(default=0.5, ge=0, le=1)

    @field_validator("scores")
    @classmethod
    def _scores_range(cls, v: dict[str, int]) -> dict[str, int]:
        for k, s in v.items():
            if not 0 <= s <= 100:
                raise ValueError(f"score {k} out of range 0-100")
        return v


# Fields a revision may change. Cost, price and similar numbers are allowed (the proposer may
# change its own proposal) but are re-evaluated from scratch by independent reviewers.
REVISABLE_FIELDS = [
    "description", "product", "usp", "revenue_model", "channels", "scope", "setup_cost_eur",
    "recurring_cost_eur_month", "human_hours_week", "days_to_first_sale", "unit_price_eur",
    "unit_variable_cost_eur", "automation_potential_pct", "revenue_assumptions", "cost_assumptions",
    "competitors", "legal_risks", "automation_opportunities", "uncertainties", "validation_plan",
]


class ReviseOut(_Strict):
    rationale: str = Field(min_length=5, max_length=1500)
    changes: dict[str, object]
