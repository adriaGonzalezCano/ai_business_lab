"""HTTP API (Starlette). Thin: parse -> call a service -> serialise. No business rules live here.

Security model
- Every /api route except /api/health requires `Authorization: Bearer <owner token>`.
- The token is compared in constant time; repeated failures from one client are rate limited.
- Agents have no route to settings, profiles, agent permissions or owner decisions: those routes require the
  owner token, and no agent code path holds it.
- Error messages are passed through secret redaction.
"""
from __future__ import annotations

import hmac
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .app import JOB_COMMITTEE, JOB_RESEARCH, Services
from .committee import CommitteeError
from .costs import BudgetExceeded, CircularActivity, PricingMissing
from .ideas import DuplicateIdea
from .integrations import IntegrationUnavailable
from .llm import AgentDisabled, ConcurrencyLimit, InvalidAgentOutput
from .providers import ProviderError, ProviderUnavailable
from .reports import build_report
from .util import dumps, loads, redact

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
MAX_BODY = 1_000_000
AUTH_WINDOW_S, AUTH_MAX_FAILS = 60.0, 10

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, kind: str = "error"):
        super().__init__(message)
        self.status, self.kind = status, kind


# exception type -> (status, kind)
ERROR_MAP: list[tuple[type[BaseException], int, str]] = [
    (DuplicateIdea, 409, "duplicate_idea"),
    (CommitteeError, 409, "conflict"),
    (BudgetExceeded, 402, "budget_exceeded"),
    (PricingMissing, 409, "pricing_missing"),
    (CircularActivity, 429, "circular_activity"),
    (ConcurrencyLimit, 429, "concurrency_limit"),
    (AgentDisabled, 409, "agent_disabled"),
    (ProviderUnavailable, 503, "provider_unavailable"),
    (IntegrationUnavailable, 503, "integration_unavailable"),
    (ProviderError, 502, "provider_error"),
    (InvalidAgentOutput, 502, "invalid_agent_output"),
    (PermissionError, 403, "forbidden"),
    (KeyError, 404, "not_found"),
    (ValidationError, 422, "validation_error"),
    (ValueError, 422, "validation_error"),
]


def _json(data: Any, status: int = 200) -> Response:
    return Response(dumps(data), status_code=status, media_type="application/json", headers=SECURITY_HEADERS)


def _error(status: int, kind: str, message: str) -> Response:
    return _json({"error": {"type": kind, "message": redact(message)}}, status)


class _AuthLimiter:
    def __init__(self) -> None:
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, client: str) -> bool:
        q, now = self._fails[client], time.monotonic()
        while q and now - q[0] > AUTH_WINDOW_S:
            q.popleft()
        return len(q) >= AUTH_MAX_FAILS

    def fail(self, client: str) -> None:
        self._fails[client].append(time.monotonic())


class Ctx:
    """What a route handler receives."""

    def __init__(self, svc: Services, request: Request, body: Any):
        self.svc, self.request, self.body = svc, request, body
        self.path: dict[str, str] = dict(request.path_params)
        self.query = request.query_params

    def qint(self, name: str, default: int, lo: int = 1, hi: int = 500) -> int:
        try:
            return max(lo, min(hi, int(self.query.get(name, default))))
        except ValueError:
            raise ValueError(f"{name} must be an integer")

    def obj(self) -> dict[str, Any]:
        if not isinstance(self.body, dict):
            raise ValueError("a JSON object body is required")
        return self.body


def create_app(svc: Services) -> Starlette:
    limiter = _AuthLimiter()

    def authorised(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return bool(supplied) and hmac.compare_digest(supplied.encode(), svc.config.owner_token.encode())

    def endpoint(fn: Callable[[Ctx], Any], public: bool = False, status: int = 200):
        async def handler(request: Request) -> Response:
            client = request.client.host if request.client else "unknown"
            if not public:
                if limiter.blocked(client):
                    return _error(429, "rate_limited", "Too many failed sign-in attempts. Wait a minute.")
                if not authorised(request):
                    limiter.fail(client)
                    return _error(401, "unauthorized", "Missing or invalid owner token.")
            body: Any = None
            if request.method in ("POST", "PUT", "PATCH"):
                if int(request.headers.get("content-length") or 0) > MAX_BODY:
                    return _error(413, "too_large", "Request body too large.")
                raw = await request.body()
                if len(raw) > MAX_BODY:
                    return _error(413, "too_large", "Request body too large.")
                if raw:
                    try:
                        body = loads(raw.decode("utf-8"), None)
                    except Exception:  # noqa: BLE001
                        return _error(400, "bad_json", "Body is not valid JSON.")
                    if body is None:
                        return _error(400, "bad_json", "Body is not valid JSON.")
            ctx = Ctx(svc, request, body)
            try:
                result = await run_in_threadpool(fn, ctx)
            except ApiError as e:
                return _error(e.status, e.kind, str(e))
            except Exception as e:  # noqa: BLE001
                for exc_type, code, kind in ERROR_MAP:
                    if isinstance(e, exc_type):
                        msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
                        if isinstance(e, ValidationError):
                            msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
                        return _error(code, kind, str(msg))
                svc.audit.log("system", "unhandled_api_error", "api", request.url.path, {"error": f"{type(e).__name__}: {e}"[:300]})
                return _error(500, "internal_error", "Unexpected server error. See the audit log.")
            return _json(result, status)
        return handler

    # ------------------------------------------------------------------ handlers
    def health(c: Ctx):
        return {"status": "ok", "db": bool(c.svc.db.one("SELECT 1 AS x")["x"])}

    def overview(c: Ctx):
        s = c.svc
        counts = {r["status"]: r["n"] for r in s.db.query("SELECT status, COUNT(*) AS n FROM ideas GROUP BY status")}
        jobs = {r["status"]: r["n"] for r in s.db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        spend = s.costs.summary()
        return {
            "ideas_by_status": counts,
            "jobs_by_status": jobs,
            "needs_owner": [dict(r) for r in s.db.query(
                "SELECT id, name, status, synthetic FROM ideas WHERE status IN ('needs_human_review','needs_evidence') "
                "ORDER BY updated_at DESC LIMIT 20")],
            "ai_spend": spend,
            "providers": {n: dict(zip(("available", "detail"), p.available())) for n, p in s.providers.items()},
            "default_provider": s.settings.get("default_provider"),
            "autonomy_mode": s.settings.get("default_autonomy_mode"),
            "observations": s.db.one("SELECT COUNT(*) AS n FROM market_observations")["n"],
            "businesses": {"active": 0, "note": "Business incubation is Phase 2; no business exists yet."},
            "verified_revenue_eur": 0,
            "startup": s.startup,
        }

    # settings ----------------------------------------------------------------
    def settings_get(c: Ctx):
        return {"settings": c.svc.settings.describe()}

    def settings_put(c: Ctx):
        values = c.obj().get("values")
        if not isinstance(values, dict) or not values:
            raise ValueError("body must be {\"values\": {key: value, ...}}")
        try:
            c.svc.settings.set_many(values)
        except KeyError as e:
            raise ApiError(422, str(e.args[0]), "unknown_setting")
        return {"settings": c.svc.settings.describe()}

    def settings_history(c: Ctx):
        return {"history": c.svc.settings.history(c.query.get("key"), c.qint("limit", 100))}

    # agents ------------------------------------------------------------------
    def agents_list(c: Ctx):
        return {"agents": c.svc.agents.list()}

    def agents_patch(c: Ctx):
        return c.svc.agents.update(c.path["agent_id"], c.obj())

    def runs_list(c: Ctx):
        sql, params = "SELECT * FROM agent_runs", []
        if c.query.get("agent_id"):
            sql, params = sql + " WHERE agent_id=?", [c.query["agent_id"]]
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(c.qint("limit", 100))
        return {"runs": [dict(r) for r in c.svc.db.query(sql, params)]}

    # evaluation profile ---------------------------------------------------------
    def profile_get(c: Ctx):
        version, cfg = c.svc.profiles.active()
        return {"active_version": version, "profile": cfg, "versions": c.svc.profiles.versions()}

    def profile_version(c: Ctx):
        return {"version": int(c.path["version"]), "profile": c.svc.profiles.get(int(c.path["version"]))}

    def profile_put(c: Ctx):
        profile = c.obj().get("profile")
        ver = c.svc.profiles.save_new_version(profile)
        return {"active_version": ver, "profile": c.svc.profiles.get(ver), "versions": c.svc.profiles.versions()}

    # research ---------------------------------------------------------------
    def research_overview(c: Ctx):
        return {"topics": c.svc.research.topics(), "sources": c.svc.research.sources(50),
                "seeds": c.svc.settings.get("research_seeds")}

    def observations_list(c: Ctx):
        return {"observations": c.svc.research.observations(c.query.get("topic"), c.qint("limit", 200))}

    def observation_add(c: Ctx):
        b = c.obj()
        return c.svc.research.add_manual_observation(
            str(b.get("topic", "")), str(b.get("statement", "")), str(b.get("kind", "")),
            b.get("url") or None, str(b.get("excerpt", "")))

    def research_run(c: Ctx):
        b = c.obj()
        topic, urls = str(b.get("topic", "")).strip(), b.get("urls", [])
        if len(topic) < 3:
            raise ValueError("topic must be at least 3 characters")
        if not isinstance(urls, list) or not urls or len(urls) > 10 or not all(
                isinstance(u, str) and u.startswith(("http://", "https://")) for u in urls):
            raise ValueError("urls must be a list of 1-10 http(s) URLs")
        jid, created = c.svc.queue.enqueue_unless_pending(JOB_RESEARCH, {"topic": topic, "urls": urls})
        if created:
            c.svc.audit.log("owner", "research_requested", "job", jid, {"topic": topic, "urls": urls})
        return {"job": c.svc.queue.get(jid), "already_pending": not created}

    # ideas ------------------------------------------------------------------
    def ideas_list(c: Ctx):
        return {"ideas": c.svc.ideas.list(status=c.query.get("status"), category=c.query.get("category"),
                                          q=c.query.get("q"))}

    def idea_create(c: Ctx):
        b = c.obj()
        return c.svc.ideas.create(b.get("idea", b), proposer="owner", allow_duplicate=bool(b.get("allow_duplicate")))

    def idea_detail(c: Ctx):
        return c.svc.ideas.detail(c.path["idea_id"])

    def idea_revise(c: Ctx):
        b = c.obj()
        return c.svc.ideas.revise(c.path["idea_id"], dict(b.get("changes", {})), "owner", str(b.get("reason", "")))

    def idea_evidence_add(c: Ctx):
        b = c.obj()
        return c.svc.ideas.add_evidence(
            c.path["idea_id"], str(b.get("claim", "")), str(b.get("kind", "")), b.get("url") or None,
            None, b.get("excerpt") or None, b.get("observation_id") or None, actor="owner")

    def evidence_verify(c: Ctx):
        b = c.obj()
        return c.svc.ideas.mark_evidence_verified(c.path["evidence_id"], bool(b.get("verified")), "owner",
                                                  str(b.get("note", "")))

    def idea_evaluate(c: Ctx):
        iid = c.path["idea_id"]
        idea = c.svc.ideas.get(iid)
        if idea["status"] in ("approved", "rejected", "archived"):
            raise CommitteeError(f"idea is {idea['status']}; reopen it before re-evaluating")
        if c.svc.costs.is_exhausted():
            raise BudgetExceeded("total", "The monthly AI budget is used up; new evaluations are blocked.")
        jid, created = c.svc.queue.enqueue_unless_pending(JOB_COMMITTEE, {"idea_id": iid, "actor": "owner"})
        if created:
            c.svc.audit.log("owner", "evaluation_requested", "idea", iid, {"job": jid})
        return {"job": c.svc.queue.get(jid), "already_pending": not created}

    def idea_decision(c: Ctx):
        b = c.obj()
        return c.svc.ideas.owner_decision(c.path["idea_id"], str(b.get("decision", "")), str(b.get("reason", "")))

    # jobs ---------------------------------------------------------------------
    def jobs_list(c: Ctx):
        return {"jobs": c.svc.queue.list(c.query.get("status"), c.qint("limit", 100))}

    def job_get(c: Ctx):
        return c.svc.queue.get(c.path["job_id"])

    def job_retry(c: Ctx):
        c.svc.queue.requeue(c.path["job_id"])
        c.svc.audit.log("owner", "job_requeued", "job", c.path["job_id"])
        return c.svc.queue.get(c.path["job_id"])

    def job_cancel(c: Ctx):
        c.svc.queue.cancel(c.path["job_id"])
        c.svc.audit.log("owner", "job_cancelled", "job", c.path["job_id"])
        return c.svc.queue.get(c.path["job_id"])

    def jobs_process(c: Ctx):
        """Run at most one due job now (useful when the background worker is disabled)."""
        return {"result": c.svc.worker.process_one()}

    # costs --------------------------------------------------------------------
    def costs_summary(c: Ctx):
        return c.svc.costs.summary(c.query.get("month"))

    def costs_usage(c: Ctx):
        return {"usage": c.svc.costs.usage_rows(c.qint("limit", 100))}

    def alerts_list(c: Ctx):
        return {"alerts": c.svc.costs.alerts(include_acknowledged=c.query.get("all") == "1")}

    def alert_ack(c: Ctx):
        c.svc.costs.acknowledge_alert(int(c.path["alert_id"]))
        return {"ok": True}

    # misc ---------------------------------------------------------------------
    def report(c: Ctx):
        return build_report(c.svc.db, c.svc.costs, c.qint("days", 1, 1, 90))

    def integrations(c: Ctx):
        return {"integrations": c.svc.integrations.catalog()}

    def audit_list(c: Ctx):
        return {"events": c.svc.audit.recent(c.qint("limit", 100), c.query.get("entity"), c.query.get("entity_id"))}

    # ------------------------------------------------------------------ UI files
    def ui_file(name: str, media: str) -> Response:
        path = UI_DIR / name
        if not path.is_file():
            return _error(404, "not_found", f"UI file missing: ui/{name}")
        return FileResponse(path, media_type=media, headers=SECURITY_HEADERS)

    async def index(request: Request) -> Response:
        return ui_file("index.html", "text/html; charset=utf-8")

    async def static_asset(request: Request) -> Response:
        """Serve ui/<name>.js|.css. The pattern admits only plain file names, so no path can escape ui/."""
        name = request.path_params["name"]
        if not re.fullmatch(r"[a-z0-9_-]+\.(js|css)", name):
            return _error(404, "not_found", "Not found.")
        return ui_file(name, "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8")

    R = lambda path, fn, methods, **kw: Route(path, endpoint(fn, **kw), methods=methods)  # noqa: E731
    routes = [
        Route("/", index),
        R("/api/health", health, ["GET"], public=True),
        R("/api/overview", overview, ["GET"]),
        R("/api/settings", settings_get, ["GET"]), R("/api/settings", settings_put, ["PUT"]),
        R("/api/settings/history", settings_history, ["GET"]),
        R("/api/agents", agents_list, ["GET"]), R("/api/agents/{agent_id}", agents_patch, ["PATCH"]),
        R("/api/runs", runs_list, ["GET"]),
        R("/api/profile", profile_get, ["GET"]), R("/api/profile", profile_put, ["PUT"]),
        R("/api/profile/{version:int}", profile_version, ["GET"]),
        R("/api/research", research_overview, ["GET"]), R("/api/research/run", research_run, ["POST"], status=202),
        R("/api/observations", observations_list, ["GET"]),
        R("/api/observations", observation_add, ["POST"], status=201),
        R("/api/ideas", ideas_list, ["GET"]), R("/api/ideas", idea_create, ["POST"], status=201),
        R("/api/ideas/{idea_id}", idea_detail, ["GET"]),
        R("/api/ideas/{idea_id}/revise", idea_revise, ["POST"]),
        R("/api/ideas/{idea_id}/evidence", idea_evidence_add, ["POST"], status=201),
        R("/api/ideas/{idea_id}/evaluate", idea_evaluate, ["POST"], status=202),
        R("/api/ideas/{idea_id}/decision", idea_decision, ["POST"]),
        R("/api/evidence/{evidence_id}/verify", evidence_verify, ["POST"]),
        R("/api/jobs", jobs_list, ["GET"]), R("/api/jobs/process", jobs_process, ["POST"]),
        R("/api/jobs/{job_id}", job_get, ["GET"]),
        R("/api/jobs/{job_id}/retry", job_retry, ["POST"]), R("/api/jobs/{job_id}/cancel", job_cancel, ["POST"]),
        R("/api/costs/summary", costs_summary, ["GET"]), R("/api/costs/usage", costs_usage, ["GET"]),
        R("/api/alerts", alerts_list, ["GET"]), R("/api/alerts/{alert_id:int}/ack", alert_ack, ["POST"]),
        R("/api/report", report, ["GET"]), R("/api/integrations", integrations, ["GET"]),
        R("/api/audit", audit_list, ["GET"]),
        Route("/{name}", static_asset),  # last: /api/* routes above take precedence
    ]

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        yield
        svc.worker.stop()  # lets an in-flight job finish its current step; queued jobs stay in the database

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.services = svc
    return app
