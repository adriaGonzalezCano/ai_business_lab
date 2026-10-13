"""HTTP API: authentication, input handling, secrets, owner-only authority, reports, integrations."""
import json
import unittest
from pathlib import Path

from helpers import GOOD_IDEA, TOKEN, Client, LabTestCase
from lab.api import create_app
from lab.config import Config
from lab.integrations import IntegrationUnavailable
from lab.util import redact

API_KEY = "sk-ant-api03-SUPERSECRETKEYVALUE1234567890"


class Authentication(LabTestCase):
    def api_routes(self):
        out = []
        for r in self.client.app.routes:
            if r.path.startswith("/api/") and r.path != "/api/health":
                path = r.path.replace("{version:int}", "1").replace("{alert_id:int}", "1")
                for k in ("agent_id", "idea_id", "evidence_id", "job_id"):
                    path = path.replace("{" + k + "}", "x")
                out += [(m, path) for m in r.methods if m != "HEAD"]
        return out

    def test_health_is_public_and_reveals_nothing_else(self):
        s, body = self.client.get("/api/health", token=None)
        self.assertEqual((s, body), (200, {"status": "ok", "db": True}))

    def test_every_other_api_route_requires_the_owner_token(self):
        routes = self.api_routes()
        self.assertGreater(len(routes), 30)
        for method, path in routes:
            for tok in (None, "wrong-token", TOKEN[:-1], TOKEN + "x", ""):
                client = Client(create_app(self.svc))  # fresh app => fresh rate limiter for each probe
                s, body = client.request(method, path, {} if method != "GET" else None, token=tok)
                self.assertEqual(s, 401, (method, path, tok))
                self.assertEqual(body["error"]["type"], "unauthorized")

    def test_token_must_be_in_the_authorization_header_not_the_query_string(self):
        s, _ = self.client.get(f"/api/overview?token={TOKEN}", token=None)
        self.assertEqual(s, 401)

    def test_repeated_failures_are_rate_limited(self):
        for _ in range(10):
            self.assertEqual(self.client.get("/api/overview", token="nope")[0], 401)
        s, body = self.client.get("/api/overview", token="nope")
        self.assertEqual((s, body["error"]["type"]), (429, "rate_limited"))
        self.assertEqual(self.client.get("/api/overview")[0], 429, "guesses are throttled even if one is right")
        self.assertEqual(self.client.get("/api/health", token=None)[0], 200, "health stays available")

    def test_owner_token_is_created_once_with_restricted_permissions_and_reused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            c1 = Config(data_dir=Path(d), owner_token="")
            c1.owner_token = ""
            t1 = c1.ensure_owner_token() if not c1.owner_token else c1.owner_token
            c2 = Config(data_dir=Path(d), owner_token="")
            c2.owner_token = ""
            t2 = c2.ensure_owner_token()
            self.assertGreaterEqual(len(t1), 32)
            self.assertEqual(t1, t2)
            mode = (Path(d) / "owner_token").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)


class InputHandling(LabTestCase):
    def test_malformed_json_is_a_400_not_a_crash(self):
        s, body = self.client.request("POST", "/api/ideas", raw=b"{not json")
        self.assertEqual((s, body["error"]["type"]), (400, "bad_json"))

    def test_oversized_bodies_are_refused(self):
        s, body = self.client.request("POST", "/api/ideas", raw=b'{"a":"' + b"x" * 1_100_000 + b'"}')
        self.assertEqual((s, body["error"]["type"]), (413, "too_large"))

    def test_object_routes_reject_other_json_types(self):
        for body in ([], "text", 5):
            self.assertEqual(self.client.post("/api/ideas", body)[0], 422, body)

    def test_unknown_things_are_404_and_bad_numbers_are_422(self):
        self.assertEqual(self.client.get("/api/ideas/idea_missing")[0], 404)
        self.assertEqual(self.client.get("/api/jobs/job_missing")[0], 404)
        self.assertEqual(self.client.get("/api/profile/999")[0], 404)
        self.assertEqual(self.client.get("/api/runs?limit=abc")[0], 422)
        self.assertEqual(self.client.get("/api/runs?limit=100000")[0], 200, "limits are clamped, not trusted")

    def test_wrong_method_is_405(self):
        self.assertEqual(self.client.request("DELETE", "/api/ideas")[0], 405)

    def test_research_run_validates_urls_and_topic(self):
        for body in ({"topic": "ab", "urls": ["https://e.com"]}, {"topic": "planners", "urls": []},
                     {"topic": "planners", "urls": ["file:///etc/passwd"]}, {"topic": "planners", "urls": "https://e.com"},
                     {"topic": "planners", "urls": ["https://e.com"] * 11}):
            self.assertEqual(self.client.post("/api/research/run", body)[0], 422, body)
        s, body = self.client.post("/api/research/run", {"topic": "planners", "urls": ["https://e.com/a"]})
        self.assertEqual((s, body["job"]["status"]), (202, "queued"))
        again = self.client.post("/api/research/run", {"topic": "planners", "urls": ["https://e.com/a"]})[1]
        self.assertEqual((again["job"]["id"], again["already_pending"]), (body["job"]["id"], True))


class ErrorsAndHeaders(LabTestCase):
    def test_security_headers_on_api_and_ui_and_no_cors(self):
        for path, tok in (("/api/health", None), ("/api/overview", TOKEN), ("/", None)):
            r = self.raw_headers(path, tok)
            self.assertEqual(r.get("x-content-type-options"), "nosniff", path)
            self.assertEqual(r.get("x-frame-options"), "DENY", path)
            self.assertIn("default-src 'self'", r.get("content-security-policy", ""), path)
            self.assertEqual(r.get("cache-control"), "no-store", path)
            self.assertNotIn("access-control-allow-origin", r, path)

    def raw_headers(self, path, token):
        import asyncio
        out = {}

        async def send(m):
            if m["type"] == "http.response.start":
                out.update({k.decode(): v.decode() for k, v in m["headers"]})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        hdrs = [(b"authorization", f"Bearer {token}".encode())] if token else []
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": b"", "headers": hdrs,
                 "client": ("127.0.0.1", 1), "server": ("t", 80), "root_path": ""}
        asyncio.run(self.client.app(scope, receive, send))
        return out

    def test_unexpected_errors_are_generic_and_secrets_are_redacted_in_the_audit_log(self):
        def boom(*a, **k):
            raise RuntimeError(f"db exploded while using api_key={API_KEY}")

        self.svc.ideas.list = boom
        s, body = self.client.get("/api/ideas")
        self.assertEqual((s, body["error"]["type"]), (500, "internal_error"))
        self.assertNotIn("exploded", json.dumps(body))
        self.assertNotIn(API_KEY, json.dumps(self.svc.audit.recent(50)))
        evt = [e for e in self.svc.audit.recent(50) if e["action"] == "unhandled_api_error"]
        self.assertEqual(len(evt), 1)
        self.assertIn("[REDACTED]", evt[0]["detail"]["error"])

    def test_expected_errors_that_mention_secrets_are_redacted_too(self):
        s, body = self.client.put("/api/settings", {"values": {"default_model": f"sk-ant-api03-{'A' * 30}" * 40}})
        self.assertNotIn("sk-ant-api03-AAAA", json.dumps(body))

    def test_redact_helper(self):
        for raw in (f"key {API_KEY} end", "Authorization: Bearer abcdefghijklmnop1234", 'password="hunter2hunter2"',
                    "api_key=abc123456789"):
            self.assertIn("[REDACTED]", redact(raw), raw)
        self.assertEqual(redact("nothing secret here"), "nothing secret here")


class SecretsNeverLeave(LabTestCase):
    def setUp(self):
        super().setUp()
        self.config.anthropic_api_key = API_KEY
        self.boot()

    def test_no_api_response_contains_the_provider_key_or_the_owner_token(self):
        idea = self.make_idea()
        self.client.post(f"/api/ideas/{idea['id']}/evaluate")
        blobs = []
        for path in ("/api/overview", "/api/integrations", "/api/settings", "/api/agents", "/api/audit?limit=500",
                     "/api/report", "/api/costs/summary", "/api/jobs", "/api/runs", f"/api/ideas/{idea['id']}"):
            s, body = self.client.get(path)
            self.assertEqual(s, 200, path)
            blobs.append(json.dumps(body))
        text = "\n".join(blobs)
        self.assertNotIn(API_KEY, text)
        self.assertNotIn("SUPERSECRET", text)
        self.assertNotIn(TOKEN, text)

    def test_integration_status_reports_presence_of_a_key_not_the_key(self):
        anth = next(i for i in self.client.get("/api/integrations")[1]["integrations"] if i["id"] == "model_anthropic")
        self.assertEqual(anth["status"], "available")
        self.assertNotIn("sk-", json.dumps(anth))


class OwnerAuthority(LabTestCase):
    def test_agent_permissions_cannot_be_edited_even_by_the_owner_api(self):
        before = self.svc.agents.get("market_analyst")["permissions"]
        for field in ("permissions", "id", "kind", "uses_model"):
            s, body = self.client.patch("/api/agents/market_analyst", {field: ["web_fetch"] if field == "permissions" else "x"})
            self.assertEqual(s, 422, field)
        self.assertEqual(self.svc.agents.get("market_analyst")["permissions"], before)

    def test_agent_updates_are_validated_and_audited(self):
        for bad in ({"max_iterations": 0}, {"max_iterations": 99}, {"enabled": "yes"}, {"monthly_budget_eur": -1},
                    {"provider": "openai"}, {"max_output_tokens": 10**6}, {"enabled": True, "model": 5}):
            self.assertEqual(self.client.patch("/api/agents/market_analyst", bad)[0], 422, bad)
        s, a = self.client.patch("/api/agents/market_analyst", {"max_iterations": 3, "monthly_budget_eur": 1.5})
        self.assertEqual((s, a["max_iterations"], a["monthly_budget_eur"]), (200, 3, 1.5))
        self.assertTrue(any(e["action"] == "agent_updated" and e["actor"] == "owner" for e in self.svc.audit.recent(20)))
        self.assertEqual(self.client.patch("/api/agents/nobody", {"enabled": False})[0], 404)

    def test_settings_validation_is_atomic_and_every_change_is_recorded(self):
        before = self.client.get("/api/settings")[1]["settings"]
        s, body = self.client.put("/api/settings", {"values": {"max_monthly_ai_spend_eur": 5, "default_provider": "nope"}})
        self.assertEqual(s, 422)
        self.assertEqual(self.client.get("/api/settings")[1]["settings"], before, "one bad value changes nothing")
        for bad in ({"max_monthly_ai_spend_eur": -1}, {"max_monthly_ai_spend_eur": "lots"}, {"not_a_setting": 1},
                    {"ai_budget_warn_pct": 500}, {"research_seeds": "x"}, {"model_pricing": {"bad key": {}}}):
            self.assertEqual(self.client.put("/api/settings", {"values": bad})[0], 422, bad)
        s, body = self.client.put("/api/settings", {"values": {"max_monthly_ai_spend_eur": 5, "target_monthly_profit_eur": 300}})
        self.assertEqual(s, 200)
        by_key = {x["key"]: x["value"] for x in body["settings"]}
        self.assertEqual((by_key["max_monthly_ai_spend_eur"], by_key["target_monthly_profit_eur"]), (5, 300))
        hist = self.client.get("/api/settings/history?key=max_monthly_ai_spend_eur")[1]["history"]
        self.assertEqual((hist[0]["old"], hist[0]["new"], hist[0]["actor"]), (20.0, 5, "owner"))

    def test_every_required_owner_setting_exists_and_is_editable(self):
        keys = {s["key"] for s in self.client.get("/api/settings")[1]["settings"]}
        required = {"max_monthly_ai_spend_eur", "default_agent_monthly_budget_eur", "max_budget_per_business_eur",
                    "max_validation_experiment_budget_eur", "max_cumulative_investment_before_approval_eur",
                    "target_monthly_profit_eur", "min_contribution_margin_pct", "max_days_to_validate",
                    "min_cash_reserve_eur", "currency", "preferred_categories", "excluded_categories",
                    "max_operational_complexity", "max_human_hours_per_week", "desired_automation_pct",
                    "revenue_preference", "profitability_horizon_months", "markets", "languages",
                    "acceptable_risk_level", "min_evidence_count", "default_provider", "default_model",
                    "max_concurrent_agent_runs", "research_frequency_hours", "default_autonomy_mode"}
        self.assertEqual(required - keys, set())
        self.assertEqual(self.svc.settings.get("currency"), "EUR")

    def test_agents_have_no_code_path_to_owner_actions(self):
        """Owner-only mutations are reachable only through token-protected routes."""
        owner_only = [("PUT", "/api/settings", {"values": {"max_monthly_ai_spend_eur": 1000}}),
                      ("PATCH", "/api/agents/market_analyst", {"enabled": False}),
                      ("PUT", "/api/profile", {"profile": {}}),
                      ("POST", "/api/ideas/x/decision", {"decision": "approve", "reason": "because I said so"})]
        for method, path, body in owner_only:
            self.assertEqual(self.client.request(method, path, body, token="agent-guess")[0], 401, path)
        self.assertEqual(self.svc.settings.get("max_monthly_ai_spend_eur"), 20.0)


class IntegrationsAndReports(LabTestCase):
    def test_unconfigured_integrations_are_unavailable_with_a_setup_guide_and_never_simulate_success(self):
        items = self.client.get("/api/integrations")[1]["integrations"]
        by_id = {i["id"]: i for i in items}
        for i in items:
            for field in ("capabilities", "auth", "permissions_required", "pricing_and_limits", "platform_restrictions",
                          "failure_handling", "requires_human_approval", "setup_guide"):
                self.assertIn(field, i, (i["id"], field))
        for k in ("storefront", "payments", "image_generation", "analytics", "email_support", "accounting_export"):
            self.assertEqual(by_id[k]["status"], "not_built", k)
            with self.assertRaises(IntegrationUnavailable):
                self.svc.integrations.require(k)
        self.assertEqual(by_id["model_anthropic"]["status"], "unconfigured")
        self.assertTrue(by_id["payments"]["requires_human_approval"])
        self.assertEqual(by_id["web_research"]["status"], "available")

    def test_empty_report_pads_nothing(self):
        r = self.client.get("/api/report")[1]
        self.assertEqual(r["completed"]["ideas_created"], [])
        self.assertEqual(r["completed"]["committee_decisions"], [])
        self.assertEqual(r["verified_business_outcomes"]["sales"], 0)
        self.assertEqual(r["verified_business_outcomes"]["revenue_eur"], 0)
        self.assertEqual(r["warnings"], [])
        self.assertIn("not income", r["forecasts"]["note"])

    def test_report_separates_completed_proposed_and_failed_and_warns_about_synthetic_approvals(self):
        approved = self.make_idea()
        self.add_good_evidence(approved["id"])
        self.svc.committee.run(approved["id"])
        waiting = self.make_idea(name="Notion templates for solo consultants",
                                 description="Notion workspace templates for consultants who invoice monthly.",
                                 target_audience="Solo consultants", customer_problem="Tracking invoices is messy.")
        self.svc.committee.run(waiting["id"])  # no evidence -> not approved
        self.svc.queue.enqueue("unknown_kind", {}, max_attempts=1)
        self.svc.worker.handlers["boom"] = lambda p: (_ for _ in ()).throw(ValueError("bad payload"))
        jid = self.svc.queue.enqueue("boom", {})
        self.svc.worker.process_one()
        r = self.client.get("/api/report?days=1")[1]
        self.assertEqual({d["idea_id"] for d in r["completed"]["committee_decisions"]}, {approved["id"], waiting["id"]})
        self.assertEqual([i["id"] for i in r["proposed"]["awaiting_owner"]], [waiting["id"]],
                         "the unapproved idea waits for the owner; the approved one does not")
        self.assertEqual([j["id"] for j in r["failed"]["jobs"]], [jid])
        self.assertTrue(any("synthetic" in w for w in r["warnings"]))
        self.assertEqual(r["verified_business_outcomes"], {"sales": 0, "revenue_eur": 0, "note": r["verified_business_outcomes"]["note"]})
        self.assertEqual(self.client.get("/api/report?days=0")[1]["window_days"], 1)

    def test_simulated_results_are_never_presented_as_verified_revenue(self):  # criterion 13 (Phase 1 scope)
        idea = self.make_idea()
        self.add_good_evidence(idea["id"])
        self.svc.committee.run(idea["id"])
        ov = self.client.get("/api/overview")[1]
        self.assertEqual(ov["verified_revenue_eur"], 0)
        self.assertEqual(ov["businesses"]["active"], 0)
        self.assertTrue(self.svc.ideas.detail(idea["id"])["decisions"][-1]["synthetic"],
                        "the approval itself is labelled synthetic")
        tables = {r["name"] for r in self.svc.db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({"sales", "orders", "revenue"} & tables, "Phase 1 has nowhere to record a sale, real or fake")

    def test_overview_reflects_backend_state(self):  # criterion 15 (API side)
        a = self.make_idea()
        self.client.post(f"/api/ideas/{a['id']}/evaluate")
        ov = self.client.get("/api/overview")[1]
        self.assertEqual(ov["ideas_by_status"], {"proposed": 1})
        self.assertEqual(ov["jobs_by_status"], {"queued": 1})
        self.svc.worker.process_one()
        ov = self.client.get("/api/overview")[1]
        self.assertEqual(sum(ov["ideas_by_status"].values()), 1)
        self.assertNotIn("proposed", ov["ideas_by_status"])
        self.assertEqual(ov["jobs_by_status"], {"succeeded": 1})
        self.assertEqual(ov["providers"]["mock"]["available"], True)
        self.assertEqual(ov["providers"]["anthropic"]["available"], False)


class EndToEndThroughTheApi(LabTestCase):
    fetcher_pages = {"https://example.com/market":
                     "Printable budget planners sell for 19 EUR and customers ask for irregular income support. " * 3}

    def test_research_to_decision_using_only_http(self):
        s, body = self.client.post("/api/research/run", {"topic": "budget planners", "urls": ["https://example.com/market"]})
        self.assertEqual(s, 202)
        self.assertEqual(self.client.post("/api/jobs/process")[1]["result"], "succeeded")
        job = self.client.get(f"/api/jobs/{body['job']['id']}")[1]
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(job["result"]["ideas_created"])
        idea_id = job["result"]["ideas_created"][0]

        s, ev = self.client.get(f"/api/observations?topic=budget%20planners")
        self.assertTrue(ev["observations"])
        self.client.post(f"/api/ideas/{idea_id}/evaluate")
        self.assertEqual(self.client.post("/api/jobs/process")[1]["result"], "succeeded")
        detail = self.client.get(f"/api/ideas/{idea_id}")[1]
        self.assertTrue(detail["decisions"])
        self.assertTrue(detail["evaluations"])
        self.assertIn(detail["status"], ("approved", "rejected", "needs_evidence", "needs_human_review"))
        self.assertGreater(self.client.get("/api/costs/summary")[1]["by_idea"][0]["calls"], 0)
        self.assertTrue(self.client.get("/api/audit?entity=idea&entity_id=" + idea_id)[1]["events"])


if __name__ == "__main__":
    unittest.main()
