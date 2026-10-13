"""Deterministic stand-in provider: free, reproducible, and always labelled synthetic.

It is NOT an analyst. Its scores follow transparent formulas over the idea's numbers so tests and demos
are reproducible. Anything derived from it carries a `synthetic` flag in the database and the UI.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from .base import ProviderRequest, ProviderResult

TAG = "[SYNTHETIC mock review — not real analysis]"


def _tok(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _jitter(*parts: str) -> int:
    h = hashlib.sha256("|".join(parts).encode()).digest()[0]
    return (h % 7) - 3  # -3..+3


def _clip(x: float) -> int:
    return int(max(0, min(100, round(x))))


class MockProvider:
    name = "mock"
    synthetic = True

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    def complete(self, req: ProviderRequest) -> ProviderResult:
        handler = {
            "extract_observations": self._extract,
            "scout_opportunities": self._scout,
            "review": self._review,
            "revise_idea": self._revise,
        }.get(req.task)
        if handler is None:
            out = {"error": f"mock provider has no handler for task {req.task!r}"}
        else:
            out = handler(req.context)
        text = json.dumps(out, ensure_ascii=False)
        return ProviderResult(text=text, input_tokens=_tok(req.system + req.prompt), output_tokens=_tok(text))

    # -- research ------------------------------------------------------
    def _extract(self, ctx: dict) -> dict:
        text = ctx.get("text", "")
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 40 <= len(s.strip()) <= 300]
        key = re.compile(r"\d|price|sell|demand|customer|market|popular|niche|download", re.I)
        picked = [s for s in sentences if key.search(s)][:3] or sentences[:2]
        obs = []
        for s in picked:
            is_fact = bool(re.search(r"\d", s))
            obs.append({
                "statement": f"Source states: {s}"[:600],
                "kind": "fact" if is_fact else "assumption",
                "excerpt": s,
                "confidence": 0.6 if is_fact else 0.4,
            })
        return {"observations": obs}

    def _scout(self, ctx: dict) -> dict:
        topic = ctx.get("topic", "niche")
        obs = [o for o in ctx.get("observations", []) if o.get("verified")][:3]
        return {"ideas": [{
            "name": f"{topic.title()} niche starter kit",
            "description": f"A small catalog of downloadable resources for the '{topic}' niche, sold directly to hobbyists.",
            "category": "digital_products",
            "target_audience": f"Hobbyists and small creators interested in {topic}",
            "customer_problem": f"Existing {topic} resources are scattered, inconsistent in quality or hard to adapt.",
            "product": f"Ten curated, original {topic} templates sold as a bundle plus single items.",
            "usp": "Consistent quality and a clear licence, at a lower price than agency-made packs.",
            "revenue_model": "One-off sales with optional upsell to a premium bundle",
            "channels": ["own landing page", "marketplace listing"],
            "scope": "10 items for the validation test",
            "setup_cost_eur": 40, "recurring_cost_eur_month": 10,
            "human_hours_setup": 10, "human_hours_week": 2,
            "days_to_first_sale": 21,
            "unit_price_eur": 19, "unit_variable_cost_eur": 2.5, "expected_units_month": 20,
            "automation_potential_pct": 70, "recurring_revenue": False,
            "revenue_assumptions": "20 sales/month at EUR 19 is an assumption to be tested, not an observation.",
            "cost_assumptions": "Payment fees about 3-5%; platform commission varies by marketplace.",
            "competitors": "Existing marketplace sellers; differentiation unproven.",
            "legal_risks": "TBD",
            "automation_opportunities": "Delivery, listing copy drafts and basic customer replies can be automated.",
            "uncertainties": ["Will buyers pay this price?", "Is the niche saturated?"],
            "validation_plan": "Publish 3 items and a landing page; success = 10 qualified email sign-ups and 2 sales in 21 days.",
            "evidence_observation_ids": [o["id"] for o in obs],
        }]}

    # -- committee -----------------------------------------------------
    def _review(self, ctx: dict) -> dict:
        role, idea, m = ctx["role"], ctx["idea"], ctx["metrics"]
        allowed = ctx["allowed_categories"]
        settings = ctx.get("settings", {})
        margin = m.get("margin_pct")
        vf, facts, distinct = m.get("verified_facts", 0), m.get("facts", 0), m.get("distinct_sources", 0)
        setup, recurring = idea.get("setup_cost_eur", 0), idea.get("recurring_cost_eur_month", 0)
        hours = idea.get("human_hours_week", 0)
        auto = idea.get("automation_potential_pct")
        auto = 40 if auto is None else auto
        legal = (idea.get("legal_risks") or "").strip()

        raw = {
            "market_demand": 20 + 25 * min(vf, 3) + 5 * min(max(facts - vf, 0), 3) + 5 * min(distinct, 3),
            "financial_viability": 30 if margin is None else margin * 1.15 - (15 if setup > 150 else 0) - (10 if recurring > 30 else 0),
            "automation_potential": auto,
            "feasibility": 100 - min(50, setup / 4) - 6 * hours - (10 if (idea.get("days_to_first_sale") or 0) > 30 else 0),
            "risk_compliance": (80 if len(legal) >= 40 else 35) - 5 * max(0, len(idea.get("uncertainties", [])) - 3),
            "scalability": 25 + (25 if idea.get("recurring_revenue") else 0) + 0.4 * auto,
        }
        penalty = 8 if role == "independent_critic" else 0
        scores = {c: _clip(raw[c] + _jitter(role, idea["id"], c) - penalty) for c in allowed}

        objections = []
        min_margin = settings.get("min_contribution_margin_pct", 60)
        if role == "financial_analyst":
            if margin is None:
                objections.append({"text": "No unit price and variable cost given; contribution margin cannot be assessed.", "severity": "hard"})
            elif margin <= 0:
                objections.append({"text": "Contribution margin is not positive.", "severity": "hard"})
            elif margin < min_margin:
                objections.append({"text": f"Margin {margin:.0f}% is below the owner's minimum of {min_margin:.0f}%.", "severity": "major"})
        if role == "risk_compliance" and len(legal) < 40:
            objections.append({"text": "Legal and platform risks are not identified in enough detail.", "severity": "hard"})
        if role == "automation_engineer" and auto < 50:
            objections.append({"text": "Automation potential looks too low for a one-person business.", "severity": "major"})
        if role == "market_analyst" and facts == 0:
            objections.append({"text": "No factual market evidence is attached.", "severity": "major"})
        if role == "independent_critic":
            objections.append({"text": "Demand at the proposed price is untested; saturation could be higher than assumed.",
                               "severity": "major" if vf == 0 else "minor"})

        avg = sum(scores.values()) / max(1, len(scores))
        if role == "market_analyst" and facts == 0:
            rec = "request_evidence"
        elif any(o["severity"] == "hard" for o in objections):
            rec = "revise"
        elif avg >= 70:
            rec = "approve"
        elif avg >= 50:
            rec = "revise"
        else:
            rec = "reject"
        return {
            "assessment": f"{TAG} {role} scored {scores} from the idea's numbers only.",
            "scores": scores,
            "evidence_cited": [f"{vf} verified facts, {facts} facts total, {distinct} distinct sources"],
            "assumptions": ["Scores follow fixed formulas over the idea's own numbers."],
            "objections": objections,
            "uncertainties": ["Mock review: real demand and competition were not analysed."],
            "recommendation": rec,
            "confidence": 0.3,
        }

    def _revise(self, ctx: dict) -> dict:
        idea, objections = ctx["idea"], ctx.get("objections", [])
        changes: dict = {}
        text = " ".join(o.get("text", "") for o in objections)
        if "Legal and platform risks" in text:
            changes["legal_risks"] = (
                "[MOCK] Marketplace terms on AI-assisted assets, ownership of generated content, consumer-law refund "
                "rights and VAT collection must be confirmed by the owner before any launch."
            )
        if "below the owner's minimum" in text and idea.get("unit_price_eur"):
            changes["unit_price_eur"] = round(float(idea["unit_price_eur"]) * 1.15, 2)
        if idea.get("setup_cost_eur", 0) > 0:
            changes["setup_cost_eur"] = round(float(idea["setup_cost_eur"]) * 0.9, 2)
        changes.setdefault("validation_plan", (idea.get("validation_plan") or "") + " Revised after review: add a pricing test.")
        return {"rationale": "[SYNTHETIC mock revision] Addressed reviewer objections mechanically.", "changes": changes}
