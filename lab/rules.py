"""Evaluation rules engine — pure functions, no I/O.

A *profile* (stored, versioned, owner-editable) contains:
  weights      score-category weights
  consensus    overall threshold, approve ratio, blocking roles, revision rounds, minimum reviewers
  gates        each gate has mandatory conditions (metric / operator / value) and an optional weighted-score minimum
  category_overrides   per business-category replacements of consensus values and gate score minimums

Mandatory conditions are computed from data (idea numbers, evidence counts, owner settings). A high aggregate
score can never override a failed mandatory condition. Conditions flagged `hard_reject` end the evaluation.
"""
from __future__ import annotations

import copy
import math
import operator
from typing import Any

from .schemas import CATEGORIES

OPS = {"<=": operator.le, "<": operator.lt, ">=": operator.ge, ">": operator.gt, "==": operator.eq, "!=": operator.ne}

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "Default solo-founder profile",
    "weights": {"market_demand": 25, "financial_viability": 25, "automation_potential": 20,
                "feasibility": 15, "risk_compliance": 10, "scalability": 5},
    "consensus": {
        "overall_threshold": 65, "min_approve_ratio": 0.6,
        "blocking_roles": ["financial_analyst", "risk_compliance"],
        "max_revision_rounds": 2, "min_reviewers": 3,
    },
    "gates": [
        {"id": "feasibility", "name": "Gate 1 — Feasibility", "mandatory": True,
         "categories": ["feasibility"], "min_score": 50, "conditions": [
             {"id": "setup_within_budget", "label": "Setup cost within the maximum budget per business",
              "metric": "idea.setup_cost_eur", "op": "<=", "value": {"ref": "settings.max_budget_per_business_eur"},
              "mandatory": True, "hard_reject": True},
             {"id": "workload_ok", "label": "Weekly human workload within the owner's limit",
              "metric": "idea.human_hours_week", "op": "<=", "value": {"ref": "settings.max_human_hours_per_week"},
              "mandatory": True},
             {"id": "validation_window", "label": "First sale expected within the validation window",
              "metric": "idea.days_to_first_sale", "op": "<=", "value": {"ref": "settings.max_days_to_validate"},
              "mandatory": True},
         ]},
        {"id": "market_evidence", "name": "Gate 2 — Market evidence", "mandatory": True,
         "categories": ["market_demand"], "min_score": 50, "conditions": [
             {"id": "enough_evidence", "label": "Enough evidence items above the confidence floor",
              "metric": "evidence.count", "op": ">=", "value": {"ref": "settings.min_evidence_count"}, "mandatory": True},
             {"id": "verified_facts", "label": "Enough verified factual evidence",
              "metric": "evidence.verified_fact_count", "op": ">=", "value": {"ref": "settings.min_verified_facts"},
              "mandatory": True},
             {"id": "cheap_test", "label": "A validation plan exists (demand can be tested cheaply)",
              "metric": "idea.validation_plan_present", "op": "==", "value": True, "mandatory": True},
         ]},
        {"id": "financial", "name": "Gate 3 — Financial viability", "mandatory": True,
         "categories": ["financial_viability"], "min_score": 50, "conditions": [
             {"id": "margin_known", "label": "Unit price and variable cost are stated",
              "metric": "idea.margin_known", "op": "==", "value": True, "mandatory": True},
             {"id": "margin_ok", "label": "Contribution margin at or above the owner's minimum",
              "metric": "idea.contribution_margin_pct", "op": ">=",
              "value": {"ref": "settings.min_contribution_margin_pct"}, "mandatory": True},
             {"id": "costs_stated", "label": "Cost assumptions are stated",
              "metric": "idea.cost_assumptions_present", "op": "==", "value": True, "mandatory": True},
         ]},
        {"id": "operational", "name": "Gate 4 — Operational viability", "mandatory": True,
         "categories": ["automation_potential"], "min_score": 50, "conditions": [
             {"id": "automation_floor", "label": "Automation potential of at least 50%",
              "metric": "idea.automation_potential_pct", "op": ">=", "value": 50, "mandatory": True},
             {"id": "automation_described", "label": "Automation opportunities are described",
              "metric": "idea.automation_opportunities_present", "op": "==", "value": True, "mandatory": True},
             {"id": "automation_preference", "label": "Meets the owner's desired degree of automation (preference)",
              "metric": "idea.automation_potential_pct", "op": ">=", "value": {"ref": "settings.desired_automation_pct"},
              "mandatory": False},
         ]},
        {"id": "risk", "name": "Gate 5 — Risk and compliance", "mandatory": True,
         "categories": ["risk_compliance"], "min_score": 50, "conditions": [
             {"id": "category_allowed", "label": "Business category is not excluded by the owner",
              "metric": "idea.category_excluded", "op": "==", "value": False, "mandatory": True, "hard_reject": True},
             {"id": "legal_identified", "label": "Legal, platform and IP risks are identified in detail",
              "metric": "idea.legal_risks_identified", "op": "==", "value": True, "mandatory": True},
             {"id": "no_hard_objections", "label": "No unresolved hard objection from any reviewer",
              "metric": "review.hard_objections_total", "op": "==", "value": 0, "mandatory": True},
         ]},
    ],
    "category_overrides": {
        "micro_saas": {"consensus": {"overall_threshold": 70}, "gate_min_scores": {"feasibility": 60}},
    },
}


# ---------------------------------------------------------------- validation
def validate_profile(cfg: Any) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        raise ValueError("profile must be an object")
    out = copy.deepcopy(cfg)
    w = out.get("weights")
    if not isinstance(w, dict) or not w:
        raise ValueError("weights: required object")
    for k, v in w.items():
        if k not in CATEGORIES:
            raise ValueError(f"weights: unknown category {k!r}")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 or not math.isfinite(v):
            raise ValueError(f"weights.{k}: must be a number >= 0")
    if sum(w.values()) <= 0:
        raise ValueError("weights: sum must be > 0")
    c = out.get("consensus")
    if not isinstance(c, dict):
        raise ValueError("consensus: required object")
    _num(c, "overall_threshold", 0, 100)
    _num(c, "min_approve_ratio", 0, 1)
    _num(c, "max_revision_rounds", 0, 5, integer=True)
    _num(c, "min_reviewers", 1, 10, integer=True)
    if not isinstance(c.get("blocking_roles", []), list):
        raise ValueError("consensus.blocking_roles: must be a list")
    gates = out.get("gates")
    if not isinstance(gates, list) or not gates:
        raise ValueError("gates: at least one gate required")
    seen: set[str] = set()
    for g in gates:
        gid = g.get("id") if isinstance(g, dict) else None
        if not gid or gid in seen:
            raise ValueError("gates: every gate needs a unique id")
        seen.add(gid)
        if not isinstance(g.get("name"), str):
            raise ValueError(f"gate {gid}: name required")
        for cat in g.get("categories", []):
            if cat not in CATEGORIES:
                raise ValueError(f"gate {gid}: unknown category {cat!r}")
        ms = g.get("min_score")
        if ms is not None and (not isinstance(ms, (int, float)) or not 0 <= ms <= 100):
            raise ValueError(f"gate {gid}: min_score must be 0-100")
        cids: set[str] = set()
        for cond in g.get("conditions", []):
            cid = cond.get("id")
            if not cid or cid in cids:
                raise ValueError(f"gate {gid}: condition ids must be unique")
            cids.add(cid)
            if cond.get("op") not in OPS:
                raise ValueError(f"gate {gid}.{cid}: op must be one of {sorted(OPS)}")
            if not isinstance(cond.get("metric"), str):
                raise ValueError(f"gate {gid}.{cid}: metric required")
            v = cond.get("value")
            if not (isinstance(v, (bool, int, float)) or (isinstance(v, dict) and isinstance(v.get("ref"), str))):
                raise ValueError(f"gate {gid}.{cid}: value must be a number, boolean or {{\"ref\": \"path\"}}")
    ov = out.get("category_overrides", {})
    if not isinstance(ov, dict):
        raise ValueError("category_overrides: must be an object")
    out.setdefault("name", "Custom profile")
    return out


def _num(d: dict, key: str, lo: float, hi: float, integer: bool = False) -> None:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValueError(f"consensus.{key}: must be a number between {lo} and {hi}")
    if integer and v != int(v):
        raise ValueError(f"consensus.{key}: must be a whole number")


# ---------------------------------------------------------------- evaluation
def _dig(metrics: dict[str, Any], path: str) -> Any:
    cur: Any = metrics
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def effective_config(profile: dict[str, Any], category: str) -> dict[str, Any]:
    """Profile with the idea category's overrides applied."""
    cfg = copy.deepcopy(profile)
    ov = (profile.get("category_overrides") or {}).get(category) or {}
    cfg["consensus"].update(ov.get("consensus", {}))
    for g in cfg["gates"]:
        if g["id"] in ov.get("gate_min_scores", {}):
            g["min_score"] = ov["gate_min_scores"][g["id"]]
    cfg["_applied_override"] = category if ov else None
    return cfg


def category_scores(reviews: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, float]]:
    """Mean score per category over reviewers, plus the spread (max-min) as a disagreement indicator."""
    buckets: dict[str, list[float]] = {}
    for r in reviews:
        for cat, s in r["scores"].items():
            buckets.setdefault(cat, []).append(float(s))
    means = {c: round(sum(v) / len(v), 2) for c, v in buckets.items()}
    spread = {c: round(max(v) - min(v), 2) for c, v in buckets.items()}
    return means, spread


def overall_score(weights: dict[str, float], scores: dict[str, float]) -> float:
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    return round(sum(w * scores.get(cat, 0.0) for cat, w in weights.items()) / total, 2)


def _eval_condition(cond: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    actual = _dig(metrics, cond["metric"])
    exp = cond["value"]
    if isinstance(exp, dict):
        exp = _dig(metrics, exp["ref"])
    if actual is None or exp is None:
        status, note = "unknown", "no data available for this metric"
    else:
        try:
            ok = OPS[cond["op"]](actual, exp)
            status, note = ("pass" if ok else "fail"), ""
        except TypeError:
            status, note = "unknown", "values are not comparable"
    return {
        "id": cond["id"], "label": cond.get("label", cond["id"]), "metric": cond["metric"], "op": cond["op"],
        "expected": exp, "actual": actual, "status": status, "note": note,
        "mandatory": bool(cond.get("mandatory", True)), "hard_reject": bool(cond.get("hard_reject", False)),
    }


def evaluate_gates(cfg: dict[str, Any], metrics: dict[str, Any], cat_scores: dict[str, float]) -> dict[str, Any]:
    gates_out, hard = [], []
    for g in cfg["gates"]:
        conds = [_eval_condition(c, metrics) for c in g.get("conditions", [])]
        failed_mandatory = [c for c in conds if c["mandatory"] and c["status"] != "pass"]
        for c in conds:
            if c["hard_reject"] and c["status"] == "fail":
                hard.append({"gate": g["id"], "condition": c["id"], "label": c["label"]})
        cats = g.get("categories", [])
        gate_score, score_ok, score_note = None, True, ""
        if cats:
            present = [cat_scores[c] for c in cats if c in cat_scores]
            gate_score = round(sum(present) / len(present), 2) if present else None
            ms = g.get("min_score")
            if ms is not None:
                if gate_score is None:
                    score_ok, score_note = False, "no reviewer scored this gate's categories"
                elif gate_score < ms:
                    score_ok, score_note = False, f"score {gate_score} is below the minimum {ms}"
        passed = not failed_mandatory and score_ok
        gates_out.append({
            "id": g["id"], "name": g["name"], "mandatory": bool(g.get("mandatory", True)), "passed": passed,
            "score": gate_score, "min_score": g.get("min_score"), "score_ok": score_ok, "score_note": score_note,
            "conditions": conds, "failed_mandatory": [c["id"] for c in failed_mandatory],
        })
    blocking = [g for g in gates_out if g["mandatory"] and not g["passed"]]
    return {"gates": gates_out, "all_mandatory_passed": not blocking, "failed_gates": [g["id"] for g in blocking],
            "hard_rejections": hard}


def evaluate_consensus(cfg: dict[str, Any], reviews: list[dict[str, Any]], overall: float,
                       gates_ok: bool) -> dict[str, Any]:
    c = cfg["consensus"]
    n = len(reviews)
    approvals = sum(1 for r in reviews if r["recommendation"] == "approve")
    ratio = approvals / n if n else 0.0
    blocking_hard = [
        {"role": r["reviewer_role"], "objection": o["text"]}
        for r in reviews if r["reviewer_role"] in c.get("blocking_roles", [])
        for o in r["objections"] if o["severity"] == "hard"
    ]
    rules = [
        {"id": "mandatory_gates", "label": "All mandatory gates pass", "ok": gates_ok},
        {"id": "min_reviewers", "label": f"At least {c['min_reviewers']} independent reviewers", "ok": n >= c["min_reviewers"],
         "actual": n},
        {"id": "approve_ratio", "label": f"At least {c['min_approve_ratio']:.0%} of reviewers approve",
         "ok": ratio >= c["min_approve_ratio"], "actual": round(ratio, 2)},
        {"id": "blocking_roles", "label": "No unresolved hard objection from blocking roles "
         + ", ".join(c.get("blocking_roles", [])), "ok": not blocking_hard, "details": blocking_hard},
        {"id": "overall_threshold", "label": f"Overall score at least {c['overall_threshold']}",
         "ok": overall >= c["overall_threshold"], "actual": overall},
    ]
    return {"rules": rules, "all_ok": all(r["ok"] for r in rules), "approvals": approvals, "reviewers": n}


def chair_decide(cfg: dict[str, Any], gate_eval: dict[str, Any], consensus: dict[str, Any],
                 reviews: list[dict[str, Any]], overall: float, round_no: int, after_max: str) -> tuple[str, str]:
    """Deterministic chair. Returns (outcome, explanation). Never averages its way past a failed mandatory gate."""
    c = cfg["consensus"]
    max_rounds = int(c["max_revision_rounds"])
    lines: list[str] = []
    failed_rules = [r for r in consensus["rules"] if not r["ok"]]
    n = len(reviews)

    if consensus["reviewers"] < c["min_reviewers"]:
        return "needs_human", (f"Only {consensus['reviewers']} independent reviewer(s) available; "
                               f"{c['min_reviewers']} required. A human must review.")

    if gate_eval["hard_rejections"]:
        why = "; ".join(h["label"] for h in gate_eval["hard_rejections"])
        return "rejected", f"Hard rejection (no revision can fix this): {why}."

    if consensus["all_ok"]:
        return "approved", (f"All mandatory gates passed, {consensus['approvals']}/{n} reviewers approve, "
                            f"overall score {overall} >= {c['overall_threshold']}. Approval makes the idea "
                            "eligible for incubation; it authorizes no spending.")

    for g in gate_eval["gates"]:
        if g["mandatory"] and not g["passed"]:
            bad = [x["label"] + (f" (actual {x['actual']}, needed {x['op']} {x['expected']})" if x["status"] == "fail"
                                 else " (no data)") for x in g["conditions"] if x["mandatory"] and x["status"] != "pass"]
            if not g["score_ok"]:
                bad.append(g["score_note"])
            lines.append(f"{g['name']} failed: " + "; ".join(bad))
    for r in failed_rules:
        if r["id"] != "mandatory_gates":
            lines.append(f"Consensus rule not met: {r['label']}" + (f" (actual {r.get('actual')})" if "actual" in r else ""))

    rejects = sum(1 for r in reviews if r["recommendation"] == "reject")
    if n and rejects / n > 0.5 and overall < c["overall_threshold"]:
        return "rejected", "Most reviewers recommend rejection and the overall score is below threshold. " + " | ".join(lines)

    evidence_only = bool(gate_eval["failed_gates"]) and all(
        x["metric"].startswith("evidence.") for g in gate_eval["gates"] if g["mandatory"] and not g["passed"]
        for x in g["conditions"] if x["mandatory"] and x["status"] != "pass"
    ) and all(g["score_ok"] for g in gate_eval["gates"] if g["mandatory"] and not g["passed"])
    if evidence_only:
        return "needs_evidence", "Only the evidence requirements are unmet; more verified research is needed. " + " | ".join(lines)

    if round_no <= max_rounds:
        return "revise", f"Revision requested (round {round_no} of {max_rounds + 1}). " + " | ".join(lines)

    outcome = {"needs_human": "needs_human", "reject": "rejected", "return_to_research": "needs_evidence"}[after_max]
    return outcome, f"Consensus not reached after {round_no} rounds ({after_max}). " + " | ".join(lines)


def dissent(reviews: list[dict[str, Any]], outcome: str) -> list[dict[str, Any]]:
    """Reviewers whose recommendation differs from the chair's outcome are recorded as dissent."""
    expected = {"approved": "approve", "rejected": "reject", "needs_evidence": "request_evidence", "revise": "revise"}.get(outcome)
    out = []
    for r in reviews:
        if expected and r["recommendation"] != expected:
            out.append({"role": r["reviewer_role"], "recommendation": r["recommendation"],
                        "assessment": r["assessment"][:400], "objections": r["objections"]})
    return out
