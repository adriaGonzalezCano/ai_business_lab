"""Acceptance criteria 1, 2, 11 (ideas side): persistence, evidence association, history, restarts."""
import unittest

from helpers import GOOD_IDEA, LabTestCase
from lab.ideas import DuplicateIdea


class IdeaPersistence(LabTestCase):
    def test_idea_is_stored_and_survives_a_restart(self):  # criterion 1 and 11
        idea = self.make_idea()
        self.assertTrue(idea["id"].startswith("idea_"))
        self.assertEqual(idea["status"], "proposed")
        self.boot()  # same database file, brand-new process objects
        again = self.svc.ideas.detail(idea["id"])
        self.assertEqual(again["name"], GOOD_IDEA["name"])
        self.assertEqual(again["version"], 1)
        self.assertEqual([e["event"] for e in again["events"]], ["created"])

    def test_every_documented_field_is_persisted(self):
        idea = self.make_idea()
        for k in ("target_audience", "customer_problem", "product", "usp", "revenue_model", "channels", "scope",
                  "setup_cost_eur", "recurring_cost_eur_month", "human_hours_week", "days_to_first_sale",
                  "revenue_assumptions", "cost_assumptions", "competitors", "legal_risks",
                  "automation_opportunities", "uncertainties", "validation_plan"):
            self.assertEqual(idea[k], GOOD_IDEA[k], k)
        self.assertAlmostEqual(idea["contribution_margin_pct"], (19 - 3) / 19 * 100, places=1)

    def test_invalid_idea_is_rejected_by_the_api_and_stores_nothing(self):
        status, body = self.client.post("/api/ideas", {"name": "x"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["type"], "validation_error")
        self.assertEqual(self.client.get("/api/ideas")[1]["ideas"], [])

    def test_near_duplicate_is_blocked_unless_owner_overrides(self):
        first = self.make_idea()
        with self.assertRaises(DuplicateIdea) as cm:
            self.make_idea(name="Printable budget planners for EU freelancers!")
        self.assertEqual(cm.exception.existing_id, first["id"])
        status, body = self.client.post("/api/ideas", GOOD_IDEA)
        self.assertEqual((status, body["error"]["type"]), (409, "duplicate_idea"))
        status, _ = self.client.post("/api/ideas", {"idea": GOOD_IDEA, "allow_duplicate": True})
        self.assertEqual(status, 201)

    def test_revision_creates_a_new_version_and_keeps_the_old_one(self):
        idea = self.make_idea()
        self.svc.ideas.revise(idea["id"], {"unit_price_eur": 24}, "owner", "price test")
        d = self.svc.ideas.detail(idea["id"])
        self.assertEqual(d["version"], 2)
        self.assertEqual(d["unit_price_eur"], 24)
        self.assertEqual([v["version"] for v in d["versions"]], [1, 2])
        snap = self.db_one("SELECT snapshot_json FROM idea_versions WHERE idea_id=? AND version=1", idea["id"])
        self.assertIn('"unit_price_eur": 19', snap["snapshot_json"])

    def db_one(self, sql, *p):
        return self.svc.db.one(sql, p)


class EvidenceAssociation(LabTestCase):
    def test_evidence_with_source_is_linked_to_the_idea(self):  # criterion 2
        idea = self.make_idea()
        ev = self.svc.ideas.add_evidence(idea["id"], "Competitor lists a similar planner at 19 EUR", "fact",
                                         url="https://example.com/p", excerpt="19 EUR")
        self.assertEqual(ev["idea_id"], idea["id"])
        self.assertFalse(ev["verified"], "owner-entered evidence is not machine-verified by default")
        stats = self.svc.ideas.evidence_stats(idea["id"])
        self.assertEqual((stats["count"], stats["verified_fact_count"]), (1, 0))
        self.svc.ideas.mark_evidence_verified(ev["id"], True, "owner", "opened the page")
        self.assertEqual(self.svc.ideas.evidence_stats(idea["id"])["verified_fact_count"], 1)

    def test_evidence_kinds_are_distinguished(self):
        idea = self.make_idea()
        for kind in ("fact", "estimate", "assumption", "speculation"):
            self.svc.ideas.add_evidence(idea["id"], f"a {kind}", kind)
        self.assertEqual(self.svc.ideas.evidence_stats(idea["id"])["by_kind"],
                         {"assumption": 1, "estimate": 1, "fact": 1, "speculation": 1})
        with self.assertRaises(ValueError):
            self.svc.ideas.add_evidence(idea["id"], "x", "certain_truth")

    def test_evidence_urls_must_be_http(self):
        idea = self.make_idea()
        with self.assertRaises(ValueError):
            self.svc.ideas.add_evidence(idea["id"], "claim", "fact", url="javascript:alert(1)")
        with self.assertRaises(ValueError):
            self.svc.ideas.add_evidence(idea["id"], "claim", "fact", url="file:///etc/passwd")

    def test_evidence_through_the_api(self):
        idea = self.make_idea()
        s, ev = self.client.post(f"/api/ideas/{idea['id']}/evidence",
                                 {"claim": "Searches for the term are growing", "kind": "estimate",
                                  "url": "https://example.com/trends"})
        self.assertEqual(s, 201)
        s, ev2 = self.client.post(f"/api/evidence/{ev['id']}/verify", {"verified": True, "note": "ok"})
        self.assertTrue(ev2["verified"])
        self.assertEqual(self.client.get(f"/api/ideas/{idea['id']}")[1]["evidence"][0]["id"], ev["id"])
        self.assertEqual(self.client.post("/api/ideas/idea_nope/evidence", {"claim": "c", "kind": "fact"})[0], 404)


class OwnerDecision(LabTestCase):
    def test_owner_decision_needs_a_reason_and_is_logged(self):
        idea = self.make_idea()
        s, body = self.client.post(f"/api/ideas/{idea['id']}/decision", {"decision": "reject", "reason": "no"})
        self.assertEqual(s, 422)
        s, body = self.client.post(f"/api/ideas/{idea['id']}/decision",
                                   {"decision": "reject", "reason": "Too crowded a market for me."})
        self.assertEqual((s, body["status"]), (200, "rejected"))
        events = self.svc.ideas.detail(idea["id"])["events"]
        self.assertTrue(any(e["detail"].get("owner_override") for e in events))

    def test_owner_approval_authorises_no_spending(self):
        idea = self.make_idea()
        self.svc.ideas.owner_decision(idea["id"], "approve", "Approved for incubation planning.")
        spent = self.svc.db.one("SELECT COUNT(*) AS n FROM ai_usage")["n"]
        self.assertEqual(spent, 0)
        # Phase 1 has no business workspaces, ledgers or purchase paths at all.
        tables = {r["name"] for r in self.svc.db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({"businesses", "ledger", "transactions"} & tables)


if __name__ == "__main__":
    unittest.main()
