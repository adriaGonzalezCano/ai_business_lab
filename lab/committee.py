"""Multi-agent committee: independent reviewers -> deterministic chair -> bounded revision loop.

Guarantees implemented here:
  * reviewers never include the agent that proposed the idea (no self-approval);
  * a reviewer can only score the categories its role is allowed to score (others are discarded and logged);
  * the chair is code applying the owner's profile; model opinions cannot override a failed mandatory gate;
  * every round persists evaluations + decision atomically, pinned to the profile version and a snapshot of
    the criteria used, so later configuration changes never rewrite the explanation of an earlier decision;
  * the number of revision rounds is bounded by the profile.
"""
from __future__ import annotations

import json
from typing import Any

from .agents import ROLE_CATEGORIES, AgentRegistry
from .audit import Audit
from .costs import BudgetExceeded, CircularActivity, PricingMissing
from .db import Database
from .ideas import IdeaRepo, margin_pct
from .llm import AgentDisabled, ConcurrencyLimit, InvalidAgentOutput, LLMGateway, wrap_untrusted
from .profiles import ProfileStore
from .providers import ProviderError, ProviderUnavailable
from .rules import (category_scores, chair_decide, dissent, effective_config, evaluate_consensus, evaluate_gates,
                    overall_score)
from .schemas import REVISABLE_FIELDS, ReviewOut, ReviseOut
from .settings import SettingsStore
from .util import dumps, now_iso

REVIEW_FORMAT = (
    '{"assessment": str, "scores": {"<category>": int 0-100}, "evidence_cited": [str], "assumptions": [str], '
    '"objections": [{"text": str, "severity": "minor|major|hard"}], "uncertainties": [str], '
    '"recommendation": "approve|revise|reject|request_evidence", "confidence": 0..1}'
)
ROLE_FOCUS = {
    "business_strategist": "strategic fit, differentiation and scalability",
    "financial_analyst": "costs, margins, revenue projections and break-even; challenge every number",
    "market_analyst": "customer demand, competition, saturation and distribution channels",
    "automation_engineer": "technical feasibility and the ongoing human workload",
    "risk_compliance": "legal, intellectual-property, privacy, tax, platform and security risks",
    "independent_critic": "reasons this proposal could fail; be adversarial, not balanced",
}
IDEA_PROMPT_FIELDS = [
    "id", "name", "description", "category", "target_audience", "customer_problem", "product", "usp", "revenue_model",
    "channels", "scope", "setup_cost_eur", "recurring_cost_eur_month", "human_hours_setup", "human_hours_week",
    "days_to_first_sale", "unit_price_eur", "unit_variable_cost_eur", "expected_units_month",
    "automation_potential_pct", "recurring_revenue", "revenue_assumptions", "cost_assumptions", "competitors",
    "legal_risks", "automation_opportunities", "uncertainties", "validation_plan", "contribution_margin_pct",
]


class CommitteeError(RuntimeError):
    pass


class Committee:
    def __init__(self, db: Database, settings: SettingsStore, agents: AgentRegistry, gateway: LLMGateway,
                 ideas: IdeaRepo, profiles: ProfileStore, audit: Audit):
        self.db, self.settings, self.agents, self.gateway = db, settings, agents, gateway
        self.ideas, self.profiles, self.audit = ideas, profiles, audit

    # -- metrics -----------------------------------------------------------
    def build_metrics(self, idea: dict[str, Any], ev_stats: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
        s = self.settings.all()
        legal = (idea.get("legal_risks") or "").strip()
        def long(text: str | None, n: int = 20) -> bool:
            return len((text or "").strip()) >= n
        return {
            "idea": {
                "setup_cost_eur": idea["setup_cost_eur"], "recurring_cost_eur_month": idea["recurring_cost_eur_month"],
                "human_hours_week": idea["human_hours_week"], "days_to_first_sale": idea["days_to_first_sale"],
                "contribution_margin_pct": margin_pct(idea), "margin_known": margin_pct(idea) is not None,
                "automation_potential_pct": idea["automation_potential_pct"],
                "automation_opportunities_present": long(idea["automation_opportunities"]),
                "validation_plan_present": long(idea["validation_plan"]),
                "cost_assumptions_present": long(idea["cost_assumptions"]),
                "legal_risks_identified": len(legal) >= 40 and legal.upper() != "TBD",
                "category_excluded": idea["category"] in set(s["excluded_categories"]),
            },
            "evidence": ev_stats,
            "review": {"hard_objections_total": sum(1 for r in reviews for o in r["objections"] if o["severity"] == "hard")},
            "settings": s,
        }

    # -- prompts -------------------------------------------------------------
    def _owner_constraints(self) -> dict[str, Any]:
        s = self.settings.all()
        keys = ["currency", "markets", "languages", "acceptable_risk_level", "max_operational_complexity",
                "preferred_categories", "excluded_categories", "max_human_hours_per_week", "min_contribution_margin_pct",
                "max_budget_per_business_eur", "max_days_to_validate", "revenue_preference"]
        return {k: s[k] for k in keys}

    def _review_prompt(self, role: str, idea: dict, evidence: list[dict], prior_objections: list[dict]) -> tuple[str, str]:
        cats = ROLE_CATEGORIES[role]
        system = (f"Role: {role}. Focus: {ROLE_FOCUS[role]}.\n"
                  f"You may score ONLY these categories (0-100): {cats}. Scores for other categories are discarded.\n"
                  f"Be specific and cite the evidence you rely on. Distinguish facts from assumptions. Raise a 'hard' "
                  f"objection only for a defect that makes the idea unacceptable as written.\n"
                  f"JSON structure: {REVIEW_FORMAT}")
        ev_lines = [f"- [{e['kind']}{', verified' if e['verified'] else ', unverified'}] {e['claim']}"
                    + (f" (source: {e['url']})" if e["url"] else "") for e in evidence]
        prompt = (
            "Owner constraints:\n" + json.dumps(self._owner_constraints()) + "\n\nIdea under review:\n"
            + json.dumps({k: idea.get(k) for k in IDEA_PROMPT_FIELDS}, ensure_ascii=False)
            + "\n\nEvidence attached to the idea (data, may be wrong):\n"
            + wrap_untrusted("idea evidence", "\n".join(ev_lines) or "(none)")
        )
        if prior_objections:
            prompt += "\n\nObjections raised in the previous round (check whether the revision resolved them):\n" + \
                wrap_untrusted("previous objections", json.dumps(prior_objections)[:4000])
        return system, prompt

    # -- one review ------------------------------------------------------------
    def _review(self, role: str, idea: dict, evidence: list[dict], ev_stats: dict, prior: list[dict]) -> dict[str, Any]:
        system, prompt = self._review_prompt(role, idea, evidence, prior)
        context = {
            "role": role, "idea": {k: idea.get(k) for k in IDEA_PROMPT_FIELDS},
            "metrics": {"margin_pct": margin_pct(idea), "verified_facts": ev_stats["verified_fact_count"],
                        "facts": ev_stats["fact_count"], "distinct_sources": ev_stats["distinct_sources"]},
            "allowed_categories": ROLE_CATEGORIES[role], "settings": self.settings.all(),
        }
        with self.gateway.run(role, "review", idea_id=idea["id"]) as run_id:
            out, meta = self.gateway.complete_json(
                agent_id=role, task="review", system=system, prompt=prompt, schema=ReviewOut, context=context,
                run_id=run_id, idea_id=idea["id"])
        allowed = set(ROLE_CATEGORIES[role])
        kept = {c: s for c, s in out.scores.items() if c in allowed}
        discarded = {c: s for c, s in out.scores.items() if c not in allowed}
        return {
            "reviewer_role": role, "provider": meta["provider"], "model": meta["model"], "synthetic": meta["synthetic"],
            "scores": kept, "discarded_scores": discarded, "recommendation": out.recommendation,
            "assessment": out.assessment, "evidence": out.evidence_cited, "assumptions": out.assumptions,
            "objections": [o.model_dump() for o in out.objections], "uncertainties": out.uncertainties,
            "confidence": out.confidence, "run_id": run_id,
        }

    # -- revision ----------------------------------------------------------------
    def _revise(self, idea: dict, reviews: list[dict]) -> dict[str, Any]:
        objections = [{"role": r["reviewer_role"], **o} for r in reviews for o in r["objections"]]
        system = (f"Role: idea_generator. Improve the proposal to address the reviewers' objections. Only change these "
                  f"fields: {REVISABLE_FIELDS}. Do not invent evidence or market statistics; if a number is unknown, "
                  f"say so in the text fields. JSON structure: "
                  '{"rationale": str, "changes": {"<field>": <new value>}}')
        prompt = ("Idea:\n" + json.dumps({k: idea.get(k) for k in IDEA_PROMPT_FIELDS}, ensure_ascii=False)
                  + "\n\nReviewer objections (data):\n" + wrap_untrusted("objections", json.dumps(objections)[:6000]))
        context = {"idea": {k: idea.get(k) for k in IDEA_PROMPT_FIELDS}, "objections": objections}
        with self.gateway.run("idea_generator", "revise_idea", idea_id=idea["id"]) as run_id:
            out, meta = self.gateway.complete_json(
                agent_id="idea_generator", task="revise_idea", system=system, prompt=prompt, schema=ReviseOut,
                context=context, run_id=run_id, idea_id=idea["id"])
        changes = {k: v for k, v in out.changes.items() if k in REVISABLE_FIELDS}
        if not changes:
            raise CommitteeError("revision proposed no valid changes")
        return {"changes": changes, "rationale": out.rationale, "synthetic": meta["synthetic"]}

    # -- main loop -----------------------------------------------------------------
    def run(self, idea_id: str, actor: str = "owner") -> dict[str, Any]:
        idea = self.ideas.get(idea_id)
        if idea["status"] in ("approved", "rejected", "archived"):
            raise CommitteeError(f"idea is {idea['status']}; reopen it before re-evaluating")
        profile_version, profile = self.profiles.active()  # pinned for this whole run
        cfg = effective_config(profile, idea["category"])
        max_rev = int(cfg["consensus"]["max_revision_rounds"])
        after_max = self.settings.get("approve_after_max_rounds")
        self.ideas.set_status(idea_id, "under_review", actor, {"profile_version": profile_version})
        rounds: list[dict[str, Any]] = []
        prior_objections: list[dict] = []
        rel = 0
        while True:
            rel += 1
            idea = self.ideas.get(idea_id)
            evidence = self.ideas.evidence(idea_id)
            ev_stats = self.ideas.evidence_stats(idea_id)
            roles = [r for r in self.agents.enabled_reviewers() if r != idea["proposer_agent"]]
            reviews: list[dict[str, Any]] = []
            failures: list[str] = []
            try:
                for role in roles:
                    try:
                        reviews.append(self._review(role, idea, evidence, ev_stats, prior_objections))
                    except (InvalidAgentOutput, ProviderError, ConcurrencyLimit) as e:
                        failures.append(f"{role}: {e}")
            except (BudgetExceeded, CircularActivity, PricingMissing, ProviderUnavailable, AgentDisabled):
                self.ideas.set_status(idea_id, "proposed" if not rounds else "needs_human_review", "system",
                                      {"reason": "evaluation stopped: budget, loop guard or provider unavailable"})
                raise
            cat_scores, spread = category_scores(reviews)
            overall = overall_score(cfg["weights"], cat_scores)
            metrics = self.build_metrics(idea, ev_stats, reviews)
            gate_eval = evaluate_gates(cfg, metrics, cat_scores)
            consensus = evaluate_consensus(cfg, reviews, overall, gate_eval["all_mandatory_passed"])
            outcome, explanation = chair_decide(cfg, gate_eval, consensus, reviews, overall, rel, after_max)
            if failures:
                explanation += " Reviewer failures: " + " | ".join(failures)
            wide = [c for c, sp in spread.items() if sp >= 30]
            if wide:
                explanation += f" Reviewers disagree widely on: {', '.join(wide)}."
            synthetic = any(r["synthetic"] for r in reviews)
            if synthetic:
                explanation += " NOTE: produced by the synthetic mock provider; not a real analysis."

            revision = None
            if outcome == "revise":
                try:
                    revision = self._revise(idea, reviews)
                except (InvalidAgentOutput, ProviderError, CommitteeError, ConcurrencyLimit) as e:
                    outcome = "needs_human"
                    explanation += f" Revision could not be produced ({e}); a human must decide."
                except (BudgetExceeded, CircularActivity, PricingMissing, ProviderUnavailable, AgentDisabled) as e:
                    outcome = "needs_human"
                    explanation += f" Revision blocked ({e}); a human must decide."

            round_no = (self.db.one("SELECT COALESCE(MAX(round),0)+1 AS r FROM consensus_decisions WHERE idea_id=?",
                                    (idea_id,))["r"])
            with self.db.tx():
                for r in reviews:
                    self.db.execute(
                        "INSERT INTO idea_evaluations(idea_id,idea_version,round,reviewer_role,provider,model,synthetic,"
                        "scores_json,recommendation,assessment,evidence_json,assumptions_json,objections_json,"
                        "uncertainties_json,confidence,discarded_scores_json,run_id,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (idea_id, idea["version"], round_no, r["reviewer_role"], r["provider"], r["model"],
                         1 if r["synthetic"] else 0, dumps(r["scores"]), r["recommendation"], r["assessment"],
                         dumps(r["evidence"]), dumps(r["assumptions"]), dumps(r["objections"]),
                         dumps(r["uncertainties"]), r["confidence"], dumps(r["discarded_scores"]), r["run_id"], now_iso()))
                self.db.execute(
                    "INSERT INTO consensus_decisions(idea_id,idea_version,round,profile_version,outcome,overall_score,"
                    "category_scores_json,gate_results_json,rule_results_json,explanation,dissent_json,synthetic,"
                    "profile_snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (idea_id, idea["version"], round_no, profile_version, outcome, overall,
                     dumps({"scores": cat_scores, "spread": spread}), dumps(gate_eval), dumps(consensus),
                     explanation, dumps(dissent(reviews, outcome)), 1 if synthetic else 0, dumps(cfg), now_iso()))
                self.ideas.add_event(idea_id, "committee_decision", "committee_chair", {
                    "round": round_no, "outcome": outcome, "overall_score": overall,
                    "failed_gates": gate_eval["failed_gates"], "synthetic": synthetic, "profile_version": profile_version})
                self.audit.log("committee_chair", "decision", "idea", idea_id,
                               {"round": round_no, "outcome": outcome, "overall": overall})
            rounds.append({"round": round_no, "outcome": outcome, "overall_score": overall,
                           "failed_gates": gate_eval["failed_gates"], "explanation": explanation})

            if outcome == "revise" and revision:
                self.ideas.set_status(idea_id, "revising", "committee_chair", {"round": round_no})
                self.ideas.revise(idea_id, revision["changes"], "idea_generator", revision["rationale"])
                prior_objections = [{"role": r["reviewer_role"], **o} for r in reviews for o in r["objections"]]
                if rel > max_rev + 1:  # defensive: chair already bounds this
                    break
                continue
            final = {"approved": "approved", "rejected": "rejected", "needs_evidence": "needs_evidence",
                     "needs_human": "needs_human_review"}.get(outcome, "needs_human_review")
            self.ideas.set_status(idea_id, final, "committee_chair", {"round": round_no, "synthetic": synthetic})
            if final == "approved" and synthetic:
                self.ideas.add_event(idea_id, "approved_on_synthetic_review", "committee_chair",
                                     {"warning": "Approval rests on mock-provider reviews; re-run with a real provider "
                                                 "before relying on it. It authorizes no spending."})
            return {"idea_id": idea_id, "final_status": final, "outcome": outcome, "rounds": rounds,
                    "synthetic": synthetic}
