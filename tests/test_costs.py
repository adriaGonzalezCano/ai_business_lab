"""Acceptance criteria 8 and 9, plus the cost-controller safeguards from the master prompt."""
import json
import unittest

from helpers import GOOD_IDEA, LabTestCase
from lab.config import Config
from lab.costs import BudgetExceeded, CircularActivity, PricingMissing
from lab.llm import InvalidAgentOutput
from lab.providers import ProviderError, ProviderResult, ProviderUnavailable
from lab.providers.mock import MockProvider
from lab.schemas import ReviewOut

PRICE = {"mock/mock-1": {"input_per_mtok": 100.0, "output_per_mtok": 100.0}}


class CountingProvider(MockProvider):
    """Mock that can misreport tokens or fail, to prove the controller trusts provider usage, not model text."""

    def __init__(self, in_tokens=None, out_tokens=None, fail=False):
        self.in_tokens, self.out_tokens, self.fail, self.calls = in_tokens, out_tokens, fail, 0

    def complete(self, req):
        self.calls += 1
        if self.fail:
            raise ProviderError("boom")
        r = super().complete(req)
        return ProviderResult(r.text, self.in_tokens or r.input_tokens, self.out_tokens or r.output_tokens)


class Base(LabTestCase):
    def authorize(self, worst_eur=0.5, agent="market_analyst", **kw):
        """Reserve a call whose worst-case cost is `worst_eur` under a 1000-token in/out estimate."""
        per_mtok = worst_eur / 2000 * 1e6
        self.set_settings(model_pricing={"mock/mock-1": {"input_per_mtok": per_mtok, "output_per_mtok": per_mtok}})
        return self.svc.costs.authorize(agent_id=agent, provider="mock", model="mock-1", task="review",
                                        est_input_tokens=1000, max_output_tokens=1000, **kw)

    def usage_statuses(self):
        return [r["status"] for r in self.svc.db.query("SELECT status FROM ai_usage ORDER BY id")]


class Attribution(Base):
    def test_every_call_is_attributed_to_agent_task_run_and_idea(self):  # criterion 8
        self.set_settings(model_pricing=PRICE)
        idea = self.make_idea()
        other = self.make_idea(name="Notion templates for solo consultants",
                               description="Notion workspace templates for solo consultants who invoice monthly.",
                               target_audience="Solo consultants", customer_problem="Tracking invoices and clients is messy.")
        self.add_good_evidence(idea["id"])
        self.svc.committee.run(idea["id"])
        rows = self.svc.db.query("SELECT * FROM ai_usage")
        self.assertGreater(len(rows), 5)
        for r in rows:
            self.assertEqual(r["idea_id"], idea["id"])
            self.assertIsNotNone(r["run_id"])
            self.assertEqual(r["status"], "final")
            self.assertGreater(r["estimated_cost"], 0)
        s = self.svc.costs.summary()
        self.assertEqual([x["key"] for x in s["by_idea"]], [idea["id"]])
        self.assertNotIn(other["id"], [x["key"] for x in s["by_idea"]])
        self.assertTrue({"business_strategist", "financial_analyst", "risk_compliance"} <= {x["key"] for x in s["by_agent"]})
        self.assertIn("review", {x["key"] for x in s["by_task"]})
        self.assertAlmostEqual(sum(x["cost"] for x in s["by_agent"]), s["spent_total"], places=4)
        self.assertAlmostEqual(self.svc.costs.spent(idea_id=idea["id"]), s["spent_total"], places=4)

    def test_cost_comes_from_provider_reported_tokens_times_configured_price(self):
        self.set_settings(model_pricing={"mock/mock-1": {"input_per_mtok": 1.0, "output_per_mtok": 5.0}})
        p = CountingProvider(in_tokens=1_000_000, out_tokens=200_000)  # the "model" claims tiny usage in its text
        self.svc.gateway.providers["mock"] = p
        idea = self.make_idea()
        _, meta = self.svc.gateway.complete_json(
            agent_id="market_analyst", task="review", system="s", prompt="p", schema=ReviewOut,
            context={"role": "market_analyst", "idea": {**idea}, "metrics": {}, "allowed_categories": ["market_demand"]},
            idea_id=idea["id"])
        row = self.svc.db.one("SELECT * FROM ai_usage WHERE id=?", (meta["usage_id"],))
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (1_000_000, 200_000))
        self.assertAlmostEqual(row["estimated_cost"], 1.0 + 1.0, places=6)  # 1M*1/1M + 0.2M*5/1M

    def test_spend_is_visible_through_the_api(self):
        self.set_settings(model_pricing=PRICE)
        idea = self.make_idea()
        self.client.post(f"/api/ideas/{idea['id']}/evaluate")
        self.svc.worker.process_one()
        s, body = self.client.get("/api/costs/summary")
        self.assertEqual(s, 200)
        self.assertGreater(body["spent_total"], 0)
        self.assertEqual(body["by_idea"][0]["key"], idea["id"])
        self.assertGreater(len(self.client.get("/api/costs/usage")[1]["usage"]), 0)


class HardStop(Base):
    def setUp(self):
        super().setUp()
        self.set_settings(max_monthly_ai_spend_eur=1.0, ai_critical_reserve_pct=10, ai_budget_warn_pct=70,
                          default_agent_monthly_budget_eur=10.0)

    def test_in_flight_reservations_count_against_the_limit(self):
        first = self.authorize(0.5)
        with self.assertRaises(BudgetExceeded):  # 0.5 reserved + 0.5 > 0.9 ceiling, even though nothing is spent yet
            self.authorize(0.5)
        self.svc.costs.release(first)
        self.assertIsInstance(self.authorize(0.5), int)  # the freed reservation can be used again

    def test_reaching_the_limit_blocks_new_calls_it_does_not_just_warn(self):  # criterion 9
        uid = self.authorize(0.9)  # exactly the non-critical ceiling (1.0 minus the 10% reserve)
        self.svc.costs.settle(uid, 1000, 1000)
        with self.assertRaises(BudgetExceeded) as cm:
            self.authorize(0.05)
        self.assertEqual(cm.exception.scope, "total")
        self.assertIn("hard_stop", [a["level"] for a in self.svc.costs.alerts()])
        s = self.svc.costs.summary()
        self.assertTrue(s["new_work_blocked"])
        self.assertFalse(s["hard_stopped"], "the critical reserve is still available")

    def test_critical_reserve_is_only_usable_by_critical_calls(self):
        uid = self.authorize(0.88)
        self.svc.costs.settle(uid, 1000, 1000)
        self.assertIsInstance(self.authorize(0.05, critical=True), int)
        with self.assertRaises(BudgetExceeded):
            self.authorize(0.2, critical=True)  # even the reserve is capped by the hard limit

    def test_total_never_exceeds_the_limit_under_repeated_calls(self):
        done = 0
        for _ in range(20):
            try:
                self.svc.costs.settle(self.authorize(0.3), 1000, 1000)
                done += 1
            except BudgetExceeded:
                break
        self.assertEqual(done, 3)  # 0.3 x 3 = 0.9 fits; the 4th would pass the 0.9 ceiling
        self.assertLessEqual(self.svc.costs.spent(), 1.0)

    def test_warning_alert_fires_before_the_hard_stop(self):
        self.set_settings(ai_critical_reserve_pct=0)
        self.authorize(0.75)
        levels = [a["level"] for a in self.svc.costs.alerts()]
        self.assertIn("warning", levels)
        self.assertNotIn("hard_stop", levels)

    def test_alerts_can_be_acknowledged(self):
        self.set_settings(ai_critical_reserve_pct=0)
        self.authorize(0.75)
        alert = self.client.get("/api/alerts")[1]["alerts"][0]
        self.assertEqual(self.client.post(f"/api/alerts/{alert['id']}/ack")[0], 200)
        self.assertEqual(self.client.get("/api/alerts")[1]["alerts"], [])
        self.assertEqual(len(self.client.get("/api/alerts?all=1")[1]["alerts"]), 1)

    def test_per_agent_budget_stops_only_that_agent(self):
        self.client.patch("/api/agents/financial_analyst", {"monthly_budget_eur": 0})
        with self.assertRaises(BudgetExceeded) as cm:
            self.authorize(0.1, agent="financial_analyst")
        self.assertEqual(cm.exception.scope, "agent:financial_analyst")
        self.assertIsInstance(self.authorize(0.1, agent="market_analyst"), int)

    def test_business_and_experiment_scopes_are_enforced_when_given(self):
        uid = self.authorize(0.2, business_id="biz_1", business_limit=0.3)
        self.svc.costs.settle(uid, 1000, 1000)
        with self.assertRaises(BudgetExceeded) as cm:
            self.authorize(0.2, business_id="biz_1", business_limit=0.3)
        self.assertEqual(cm.exception.scope, "business:biz_1")
        self.assertIsInstance(self.authorize(0.2, business_id="biz_2", business_limit=0.3), int)

    def test_committee_stops_when_the_budget_runs_out_and_the_idea_is_not_left_hanging(self):
        self.set_settings(model_pricing=PRICE, max_monthly_ai_spend_eur=0.6, ai_critical_reserve_pct=0)
        idea = self.make_idea()
        self.add_good_evidence(idea["id"])
        with self.assertRaises(BudgetExceeded):
            self.svc.committee.run(idea["id"])
        self.assertLessEqual(self.svc.costs.spent(), 0.6)
        self.assertNotIn("reserved", self.usage_statuses())
        self.assertIn(self.svc.ideas.get(idea["id"])["status"], ("proposed", "needs_human_review"))
        self.assertNotEqual(self.svc.ideas.get(idea["id"])["status"], "under_review")

    def test_worker_and_api_refuse_new_work_once_exhausted(self):
        self.set_settings(model_pricing=PRICE, ai_critical_reserve_pct=0)
        idea = self.make_idea()
        self.svc.costs.settle(self.authorize(0.99), 1000, 1000)
        self.set_settings(model_pricing=PRICE)
        self.set_settings(max_monthly_ai_spend_eur=self.svc.costs.spent())
        status, body = self.client.post(f"/api/ideas/{idea['id']}/evaluate")
        self.assertEqual((status, body["error"]["type"]), (402, "budget_exceeded"))
        # Even a job that was queued earlier will not start.
        calls_before = self.svc.db.one("SELECT COUNT(*) AS n FROM ai_usage")["n"]
        jid = self.svc.queue.enqueue("committee_run", {"idea_id": idea["id"]})
        self.assertIsNone(self.svc.worker.process_one())
        self.assertEqual(self.svc.queue.get(jid)["status"], "queued")
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) AS n FROM ai_usage")["n"], calls_before)


class Safeguards(Base):
    def test_identical_prompts_are_blocked_as_a_loop(self):
        self.set_settings(max_identical_prompts=2)
        for _ in range(2):
            self.authorize(0.01, prompt_hash_="same")
        with self.assertRaises(CircularActivity):
            self.authorize(0.01, prompt_hash_="same")
        self.assertIsInstance(self.authorize(0.01, prompt_hash_="different"), int)

    def test_unpriced_real_model_is_refused_until_the_owner_sets_a_price(self):
        with self.assertRaises(PricingMissing):
            self.svc.costs.authorize(agent_id="market_analyst", provider="anthropic", model="some-model", task="review",
                                     est_input_tokens=10, max_output_tokens=10)
        self.assertEqual(self.usage_statuses(), [])

    def test_unconfigured_provider_makes_no_call_and_leaves_no_reservation(self):
        self.set_settings(default_provider="anthropic", default_model="some-model",
                          model_pricing={"anthropic/some-model": {"input_per_mtok": 3, "output_per_mtok": 15}})
        self.assertEqual(self.config.anthropic_api_key, "")
        with self.assertRaises(ProviderUnavailable) as cm:
            self.svc.gateway.complete_json(agent_id="market_analyst", task="review", system="s", prompt="p", schema=ReviewOut)
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))
        self.assertEqual(self.usage_statuses(), [])

    def test_provider_failure_releases_the_reservation(self):
        self.svc.gateway.providers["mock"] = CountingProvider(fail=True)
        with self.assertRaises(ProviderError):
            self.svc.gateway.complete_json(agent_id="market_analyst", task="review", system="s", prompt="p", schema=ReviewOut)
        self.assertEqual(self.usage_statuses(), ["released"])
        self.assertEqual(self.svc.costs.spent(), 0)

    def test_garbage_output_is_retried_a_bounded_number_of_times(self):
        class Garbage(MockProvider):
            calls = 0

            def complete(self, req):
                Garbage.calls += 1
                return ProviderResult("not json at all", 10, 5)

        self.svc.gateway.providers["mock"] = Garbage()
        self.client.patch("/api/agents/market_analyst", {"max_iterations": 2})
        with self.assertRaises(InvalidAgentOutput):
            self.svc.gateway.complete_json(agent_id="market_analyst", task="review", system="s", prompt="p", schema=ReviewOut)
        self.assertEqual(Garbage.calls, 2)

    def test_a_disabled_agent_cannot_run(self):
        self.client.patch("/api/agents/market_analyst", {"enabled": False})
        from lab.llm import AgentDisabled
        with self.assertRaises(AgentDisabled):
            self.svc.gateway.complete_json(agent_id="market_analyst", task="review", system="s", prompt="p", schema=ReviewOut)

    def test_stale_reservations_from_a_crash_are_released_on_startup(self):
        self.authorize(0.5)
        self.assertEqual(self.usage_statuses(), ["reserved"])
        self.boot()
        self.assertEqual(self.usage_statuses(), ["released"])
        self.assertEqual(self.svc.startup["stale_reservations_released"], 1)
        self.assertEqual(self.svc.costs.summary()["reserved_in_flight"], 0)


if __name__ == "__main__":
    unittest.main()
