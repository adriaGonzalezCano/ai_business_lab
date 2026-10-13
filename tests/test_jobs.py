"""Acceptance criteria 10 and 11: safe retries, restarts that keep tasks and decisions, no duplicate work."""
import unittest

from helpers import LabTestCase


class Flaky:
    """A job handler that fails `fail_times` times, then succeeds. Counts executions."""

    def __init__(self, fail_times, exc=RuntimeError):
        self.fail_times, self.exc, self.runs = fail_times, exc, 0

    def __call__(self, payload):
        self.runs += 1
        if self.runs <= self.fail_times:
            raise self.exc(f"attempt {self.runs} failed")
        return {"ok": True, "runs": self.runs}


class Retries(LabTestCase):
    def register(self, handler):
        self.svc.worker.handlers["flaky"] = handler
        return handler

    def test_transient_failures_are_retried_then_succeed(self):  # criterion 10
        h = self.register(Flaky(2))
        jid = self.svc.queue.enqueue("flaky", {})
        results = [self.svc.worker.process_one() for _ in range(3)]
        self.assertEqual(results, ["queued", "queued", "succeeded"])
        job = self.svc.queue.get(jid)
        self.assertEqual((job["status"], job["attempts"], h.runs), ("succeeded", 3, 3))
        self.assertEqual(job["result"]["runs"], 3)

    def test_attempts_are_bounded_then_the_owner_can_retry(self):
        h = self.register(Flaky(10))
        jid = self.svc.queue.enqueue("flaky", {}, max_attempts=2)
        self.svc.worker.process_one()
        self.assertEqual(self.svc.worker.process_one(), "failed")
        self.assertIsNone(self.svc.worker.process_one(), "a failed job must not keep running")
        self.assertEqual(h.runs, 2)
        self.assertIn("RuntimeError", self.svc.queue.get(jid)["error"])

        h.fail_times = 0  # the underlying problem is fixed
        s, job = self.client.post(f"/api/jobs/{jid}/retry")
        self.assertEqual((s, job["status"], job["attempts"]), (200, "queued", 0))
        self.assertEqual(self.svc.worker.process_one(), "succeeded")

    def test_errors_that_retrying_cannot_fix_fail_immediately(self):
        for exc in (ValueError, KeyError, PermissionError):
            h = self.register(Flaky(5, exc))
            jid = self.svc.queue.enqueue("flaky", {})
            self.assertEqual(self.svc.worker.process_one(), "failed", exc)
            self.assertEqual((self.svc.queue.get(jid)["attempts"], h.runs), (1, 1), exc)

    def test_backoff_delays_the_next_attempt(self):
        self.svc.worker.backoff = 3600.0
        h = self.register(Flaky(1))
        self.svc.queue.enqueue("flaky", {})
        self.assertEqual(self.svc.worker.process_one(), "queued")
        self.assertIsNone(self.svc.worker.process_one(), "not due yet")
        self.assertEqual(h.runs, 1)

    def test_queued_jobs_can_be_cancelled_but_running_ones_are_not_touched(self):
        self.register(Flaky(0))
        jid = self.svc.queue.enqueue("flaky", {})
        self.assertEqual(self.client.post(f"/api/jobs/{jid}/cancel")[1]["status"], "cancelled")
        self.assertIsNone(self.svc.worker.process_one())
        jid2 = self.svc.queue.enqueue("flaky", {})
        self.svc.queue.claim_next({"flaky"})
        self.assertEqual(self.client.post(f"/api/jobs/{jid2}/cancel")[1]["status"], "running")


class Idempotency(LabTestCase):
    def test_same_idempotency_key_never_creates_a_second_job(self):
        a = self.svc.queue.enqueue("flaky", {}, idempotency_key="k1")
        b = self.svc.queue.enqueue("flaky", {}, idempotency_key="k1")
        self.assertEqual(a, b)
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) AS n FROM jobs")["n"], 1)

    def test_double_clicking_evaluate_does_not_pay_twice(self):
        idea = self.make_idea()
        s1, b1 = self.client.post(f"/api/ideas/{idea['id']}/evaluate")
        s2, b2 = self.client.post(f"/api/ideas/{idea['id']}/evaluate")
        self.assertEqual((s1, s2), (202, 202))
        self.assertEqual(b1["job"]["id"], b2["job"]["id"])
        self.assertEqual((b1["already_pending"], b2["already_pending"]), (False, True))
        self.svc.worker.process_one()
        self.assertIsNone(self.svc.worker.process_one(), "only one evaluation was queued")
        rounds = self.svc.db.one("SELECT COUNT(DISTINCT round) AS n FROM consensus_decisions")["n"]
        self.assertGreaterEqual(rounds, 1)

    def test_scheduled_research_is_enqueued_once_per_window(self):
        self.set_settings(research_schedule_enabled=True, research_frequency_hours=24,
                          research_seeds=[{"topic": "printables", "urls": ["https://example.com/a"]}])
        first = self.svc.worker.enqueue_scheduled_research()
        second = self.svc.worker.enqueue_scheduled_research()
        self.assertEqual(len(first), 1)
        self.assertEqual(first, second)
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) AS n FROM jobs")["n"], 1)

    def test_scheduling_is_off_by_default(self):
        self.set_settings(research_seeds=[{"topic": "printables", "urls": ["https://example.com/a"]}])
        self.assertEqual(self.svc.worker.enqueue_scheduled_research(), [])


class Restarts(LabTestCase):
    def test_decisions_and_ideas_survive_a_restart(self):  # criterion 11
        idea = self.make_idea()
        self.add_good_evidence(idea["id"])
        self.svc.committee.run(idea["id"])
        before = self.svc.ideas.detail(idea["id"])
        self.boot()
        after = self.svc.ideas.detail(idea["id"])
        self.assertEqual(after["status"], before["status"])
        self.assertEqual([d["id"] for d in after["decisions"]], [d["id"] for d in before["decisions"]])
        self.assertEqual(len(after["evaluations"]), len(before["evaluations"]))
        self.assertEqual(after["decisions"][-1]["explanation"], before["decisions"][-1]["explanation"])

    def test_queued_jobs_survive_and_run_after_a_restart(self):
        idea = self.make_idea()
        jid = self.client.post(f"/api/ideas/{idea['id']}/evaluate")[1]["job"]["id"]
        self.boot()
        self.assertEqual(self.svc.queue.get(jid)["status"], "queued")
        self.assertEqual(self.svc.worker.process_one(), "succeeded")

    def test_job_interrupted_by_a_crash_is_requeued_not_lost_or_duplicated(self):
        self.svc.worker.handlers["flaky"] = Flaky(0)
        jid = self.svc.queue.enqueue("flaky", {})
        self.svc.queue.claim_next({"flaky"})  # worker takes it ... and the process dies
        self.assertEqual(self.svc.queue.get(jid)["status"], "running")
        self.boot()
        self.assertEqual(self.svc.startup["jobs_recovered"], 1)
        self.assertEqual(self.svc.queue.get(jid)["status"], "queued")
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) AS n FROM jobs")["n"], 1)

    def test_job_that_used_up_its_attempts_before_the_crash_is_failed_not_retried_forever(self):
        jid = self.svc.queue.enqueue("flaky", {}, max_attempts=1)
        self.svc.queue.claim_next({"flaky"})
        self.boot()
        self.assertEqual(self.svc.queue.get(jid)["status"], "failed")

    def test_agent_runs_left_running_by_a_crash_are_marked_failed(self):
        with self.svc.gateway.run("market_analyst", "review") as run_id:
            self.svc.db.execute("UPDATE agent_runs SET status='running' WHERE id=?", (run_id,))
        self.svc.db.execute("UPDATE agent_runs SET status='running', finished_at=NULL WHERE id=?", (run_id,))
        self.boot()
        row = self.svc.db.one("SELECT status, detail FROM agent_runs WHERE id=?", (run_id,))
        self.assertEqual(row["status"], "failed")
        self.assertIn("restart", row["detail"])

    def test_settings_and_agent_config_survive_a_restart(self):
        self.set_settings(max_monthly_ai_spend_eur=7.5)
        self.client.patch("/api/agents/market_analyst", {"enabled": False})
        self.boot()
        self.assertEqual(self.svc.settings.get("max_monthly_ai_spend_eur"), 7.5)
        self.assertFalse(self.svc.agents.get("market_analyst")["enabled"])
        self.assertEqual(len(self.svc.agents.list()), 11, "seeding must not duplicate or reset agents")


if __name__ == "__main__":
    unittest.main()
