"""Acceptance criteria 3, 4, 5: editable criteria, mandatory gates, traceable committee decisions."""
import copy
import unittest

from helpers import LabTestCase
from lab import rules
from lab.agents import REVIEWER_ROLES


def _review(role, rec="approve", scores=None, hard=False):
    return {"reviewer_role": role, "recommendation": rec, "scores": scores or {"market_demand": 100},
            "assessment": "x", "objections": [{"text": "bad", "severity": "hard"}] if hard else []}


class PureRules(unittest.TestCase):
    """The rules engine is pure; test it without any database."""

    def setUp(self):
        self.cfg = rules.effective_config(copy.deepcopy(rules.DEFAULT_PROFILE), "digital_products")
        self.scores = {c: 100.0 for c in self.cfg["weights"]}
        self.metrics_ok = {
            "idea": {"setup_cost_eur": 50, "human_hours_week": 2, "days_to_first_sale": 10, "validation_plan_present": True,
                     "margin_known": True, "contribution_margin_pct": 80, "cost_assumptions_present": True,
                     "automation_potential_pct": 90, "automation_opportunities_present": True,
                     "category_excluded": False, "legal_risks_identified": True},
            "evidence": {"count": 5, "verified_fact_count": 3},
            "review": {"hard_objections_total": 0},
            "settings": {"max_budget_per_business_eur": 300, "max_human_hours_per_week": 10, "max_days_to_validate": 45,
                         "min_evidence_count": 3, "min_verified_facts": 1, "min_contribution_margin_pct": 40,
                         "desired_automation_pct": 60},
        }

    def decide(self, metrics, reviews=None, overall=None):
        reviews = reviews or [_review(r) for r in REVIEWER_ROLES]
        overall = rules.overall_score(self.cfg["weights"], self.scores) if overall is None else overall
        g = rules.evaluate_gates(self.cfg, metrics, self.scores)
        c = rules.evaluate_consensus(self.cfg, reviews, overall, g["all_mandatory_passed"])
        return g, rules.chair_decide(self.cfg, g, c, reviews, overall, 1, "needs_human")

    def test_everything_in_order_is_approved(self):
        g, (outcome, why) = self.decide(self.metrics_ok)
        self.assertTrue(g["all_mandatory_passed"])
        self.assertEqual(outcome, "approved")
        self.assertIn("authorizes no spending", why)

    def test_perfect_scores_cannot_override_a_failed_mandatory_condition(self):  # criterion 4
        for metric_path, bad in [(("evidence", "verified_fact_count"), 0), (("idea", "margin_known"), False),
                                 (("idea", "legal_risks_identified"), False), (("idea", "human_hours_week"), 99)]:
            m = copy.deepcopy(self.metrics_ok)
            m[metric_path[0]][metric_path[1]] = bad
            g, (outcome, _) = self.decide(m, overall=100.0)
            self.assertFalse(g["all_mandatory_passed"], metric_path)
            self.assertNotEqual(outcome, "approved", metric_path)

    def test_missing_data_counts_as_failure_not_as_a_pass(self):
        m = copy.deepcopy(self.metrics_ok)
        m["idea"]["contribution_margin_pct"] = None
        g, (outcome, _) = self.decide(m)
        cond = next(c for gate in g["gates"] for c in gate["conditions"] if c["id"] == "margin_ok")
        self.assertEqual(cond["status"], "unknown")
        self.assertNotEqual(outcome, "approved")

    def test_hard_rejection_ends_evaluation(self):
        m = copy.deepcopy(self.metrics_ok)
        m["idea"]["category_excluded"] = True
        g, (outcome, why) = self.decide(m)
        self.assertEqual(outcome, "rejected")
        self.assertTrue(g["hard_rejections"])
        self.assertIn("no revision can fix", why)

    def test_hard_objection_from_a_blocking_role_prevents_approval(self):
        reviews = [_review(r) for r in REVIEWER_ROLES]
        reviews[1] = _review("financial_analyst", "approve", hard=True)
        _, (outcome, why) = self.decide(self.metrics_ok, reviews)
        self.assertNotEqual(outcome, "approved")
        self.assertIn("financial_analyst", why)

    def test_chair_does_not_just_average_or_follow_the_majority(self):
        reviews = [_review(r) for r in REVIEWER_ROLES[:5]] + [_review("risk_compliance", "reject", hard=True)]
        _, (outcome, _) = self.decide(self.metrics_ok, reviews)
        self.assertNotEqual(outcome, "approved", "5/6 approvals must not outvote a hard risk objection")

    def test_revision_rounds_are_bounded(self):
        m = copy.deepcopy(self.metrics_ok)
        m["idea"]["cost_assumptions_present"] = False
        g = rules.evaluate_gates(self.cfg, m, self.scores)
        reviews = [_review(r, "revise") for r in REVIEWER_ROLES]
        c = rules.evaluate_consensus(self.cfg, reviews, 80.0, g["all_mandatory_passed"])
        max_rounds = self.cfg["consensus"]["max_revision_rounds"]
        self.assertEqual(rules.chair_decide(self.cfg, g, c, reviews, 80.0, max_rounds, "needs_human")[0], "revise")
        self.assertEqual(rules.chair_decide(self.cfg, g, c, reviews, 80.0, max_rounds + 1, "needs_human")[0], "needs_human")
        self.assertEqual(rules.chair_decide(self.cfg, g, c, reviews, 80.0, max_rounds + 1, "reject")[0], "rejected")

    def test_too_few_reviewers_means_human_review(self):
        reviews = [_review("market_analyst"), _review("financial_analyst")]
        _, (outcome, _) = self.decide(self.metrics_ok, reviews)
        self.assertEqual(outcome, "needs_human")

    def test_category_override_changes_thresholds(self):
        saas = rules.effective_config(rules.DEFAULT_PROFILE, "micro_saas")
        self.assertEqual(saas["consensus"]["overall_threshold"], 70)
        self.assertEqual(next(g for g in saas["gates"] if g["id"] == "feasibility")["min_score"], 60)
        self.assertEqual(self.cfg["consensus"]["overall_threshold"], 65)

    def test_dissent_is_recorded(self):
        reviews = [_review("market_analyst", "approve"), _review("risk_compliance", "reject")]
        d = rules.dissent(reviews, "approved")
        self.assertEqual([x["role"] for x in d], ["risk_compliance"])


class CommitteeRuns(LabTestCase):
    def evaluate(self, idea_id):
        return self.svc.committee.run(idea_id)

    def test_decision_is_fully_traceable(self):  # criterion 5
        idea = self.make_idea()
        self.add_good_evidence(idea["id"])
        res = self.evaluate(idea["id"])
        d = self.svc.ideas.detail(idea["id"])
        self.assertEqual(len(d["decisions"]), len(res["rounds"]))
        dec = d["decisions"][-1]
        self.assertTrue(dec["explanation"])
        self.assertIsNotNone(dec["profile_version"])
        self.assertIn("gates", dec["gate_results"])
        self.assertTrue(dec["rule_results"]["rules"])
        self.assertIn("weights", dec["profile_snapshot"])
        roles = {e["reviewer_role"] for e in d["evaluations"] if e["round"] == dec["round"]}
        self.assertEqual(roles, set(REVIEWER_ROLES))
        for e in d["evaluations"]:
            self.assertIn(e["recommendation"], ("approve", "revise", "reject", "request_evidence"))
            self.assertTrue(e["assessment"])
            self.assertIsInstance(e["objections"], list)

    def test_idea_without_evidence_is_not_approved(self):  # criterion 4, end to end
        idea = self.make_idea()
        res = self.evaluate(idea["id"])
        self.assertNotEqual(res["outcome"], "approved")
        self.assertNotEqual(self.svc.ideas.get(idea["id"])["status"], "approved")
        first = self.svc.ideas.detail(idea["id"])["decisions"][0]
        self.assertIn("market_evidence", first["gate_results"]["failed_gates"])

    def test_excluded_category_is_rejected_outright(self):
        self.set_settings(excluded_categories=["digital_products"])
        idea = self.make_idea()
        self.add_good_evidence(idea["id"])
        self.assertEqual(self.evaluate(idea["id"])["outcome"], "rejected")

    def test_setup_cost_over_budget_is_a_hard_rejection(self):
        idea = self.make_idea(setup_cost_eur=5000)
        self.add_good_evidence(idea["id"])
        self.assertEqual(self.evaluate(idea["id"])["outcome"], "rejected")

    def test_mock_based_approval_is_labelled_synthetic_everywhere(self):
        idea = self.make_idea()
        self.add_good_evidence(idea["id"])
        res = self.evaluate(idea["id"])
        self.assertEqual(res["outcome"], "approved")
        self.assertTrue(res["synthetic"])
        d = self.svc.ideas.detail(idea["id"])
        self.assertTrue(d["decisions"][-1]["synthetic"])
        self.assertIn("synthetic", d["decisions"][-1]["explanation"].lower())
        self.assertTrue(all(e["synthetic"] for e in d["evaluations"]))
        self.assertIn("approved_on_synthetic_review", [e["event"] for e in d["events"]])

    def test_proposer_never_reviews_its_own_idea(self):
        idea = self.svc.ideas.create({**__import__("helpers").GOOD_IDEA}, proposer="financial_analyst")
        self.add_good_evidence(idea["id"])
        self.evaluate(idea["id"])
        roles = {e["reviewer_role"] for e in self.svc.ideas.detail(idea["id"])["evaluations"]}
        self.assertNotIn("financial_analyst", roles)

    def test_disabled_reviewer_is_skipped_and_too_few_reviewers_escalate_to_a_human(self):
        for role in REVIEWER_ROLES[:4]:
            self.client.patch(f"/api/agents/{role}", {"enabled": False})
        idea = self.make_idea()
        self.add_good_evidence(idea["id"])
        res = self.evaluate(idea["id"])
        self.assertEqual(res["outcome"], "needs_human")
        self.assertEqual(self.svc.ideas.get(idea["id"])["status"], "needs_human_review")

    def test_revision_loop_is_bounded_and_ends_with_a_human(self):
        idea = self.make_idea(cost_assumptions="", legal_risks="", automation_opportunities="")
        self.add_good_evidence(idea["id"])
        res = self.evaluate(idea["id"])
        max_rounds = self.svc.profiles.active()[1]["consensus"]["max_revision_rounds"]
        self.assertLessEqual(len(res["rounds"]), max_rounds + 1)
        self.assertEqual(res["final_status"], "needs_human_review")

    def test_finished_ideas_cannot_be_re_evaluated_until_reopened(self):
        idea = self.make_idea()
        self.svc.ideas.owner_decision(idea["id"], "reject", "Not interested in this niche.")
        status, body = self.client.post(f"/api/ideas/{idea['id']}/evaluate")
        self.assertEqual((status, body["error"]["type"]), (409, "conflict"))


class EditableCriteria(LabTestCase):
    def get_profile(self):
        return self.client.get("/api/profile")[1]

    def test_criteria_change_without_touching_source_code(self):  # criterion 3
        idea = self.make_idea()
        self.add_good_evidence(idea["id"])
        before = self.evaluate_and_get(idea["id"])
        self.assertEqual(before["outcome"], "approved")

        cur = self.get_profile()
        edited = copy.deepcopy(cur["profile"])
        edited["name"] = "Strict profile"
        edited["consensus"]["overall_threshold"] = 99
        status, body = self.client.put("/api/profile", {"profile": edited})
        self.assertEqual(status, 200)
        self.assertEqual(body["active_version"], cur["active_version"] + 1)

        self.svc.ideas.owner_decision(idea["id"], "reopen", "Re-checking under stricter rules.")
        after = self.evaluate_and_get(idea["id"])
        self.assertNotEqual(after["outcome"], "approved")

        # The earlier decision still explains itself with the rules that applied at the time.
        decisions = self.svc.ideas.detail(idea["id"])["decisions"]
        first, last = decisions[0], decisions[-1]
        self.assertEqual(first["profile_version"], cur["active_version"])
        self.assertEqual(first["profile_snapshot"]["consensus"]["overall_threshold"], 65)
        self.assertEqual(last["profile_version"], cur["active_version"] + 1)
        self.assertEqual(last["profile_snapshot"]["consensus"]["overall_threshold"], 99)
        self.assertEqual(self.client.get(f"/api/profile/{cur['active_version']}")[1]["profile"]["consensus"]["overall_threshold"], 65)

    def evaluate_and_get(self, idea_id):
        return self.svc.committee.run(idea_id)

    def test_invalid_profiles_are_refused_and_change_nothing(self):
        cur = self.get_profile()
        bad = copy.deepcopy(cur["profile"])
        bad["gates"][0]["conditions"][0]["op"] = "~="
        self.assertEqual(self.client.put("/api/profile", {"profile": bad})[0], 422)
        bad2 = copy.deepcopy(cur["profile"])
        bad2["weights"] = {"market_demand": -5}
        self.assertEqual(self.client.put("/api/profile", {"profile": bad2})[0], 422)
        self.assertEqual(self.get_profile()["active_version"], cur["active_version"])

    def workload_condition(self, idea_id):
        last = self.svc.ideas.detail(idea_id)["decisions"][-1]
        gate = next(g for g in last["gate_results"]["gates"] if g["id"] == "feasibility")
        return next(c for c in gate["conditions"] if c["id"] == "workload_ok"), last

    def test_owner_settings_feed_the_gates(self):
        idea = self.make_idea(human_hours_week=5)
        self.add_good_evidence(idea["id"])
        self.set_settings(max_human_hours_per_week=3)
        self.svc.committee.run(idea["id"])
        cond, dec = self.workload_condition(idea["id"])
        self.assertEqual((cond["status"], cond["expected"], cond["actual"]), ("fail", 3, 5))
        self.assertNotEqual(dec["outcome"], "approved")

        self.svc.ideas.owner_decision(idea["id"], "reopen", "Owner raised the workload limit.")
        self.set_settings(max_human_hours_per_week=10)
        res = self.svc.committee.run(idea["id"])
        cond, dec = self.workload_condition(idea["id"])
        self.assertEqual((cond["status"], cond["expected"]), ("pass", 10))
        self.assertEqual(res["outcome"], "approved")


if __name__ == "__main__":
    unittest.main()
