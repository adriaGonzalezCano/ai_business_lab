"""Shared test fixtures: a throw-away lab, an ASGI client that needs no extra packages, a fake fetcher."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.api import create_app  # noqa: E402
from lab.app import Services, build_services  # noqa: E402
from lab.config import Config  # noqa: E402
from lab.research import FetchResult  # noqa: E402

TOKEN = "test-owner-token-0123456789abcdef"

# A complete, well-formed idea: every field the gates look at is present.
GOOD_IDEA: dict[str, Any] = dict(
    name="Printable budget planners for EU freelancers",
    description="A bundle of printable PDF budget planners designed for freelancers with irregular income.",
    category="digital_products",
    target_audience="Freelancers in the EU with irregular monthly income",
    customer_problem="Irregular income makes monthly budgeting with generic templates unreliable.",
    product="PDF planner bundle (monthly, quarterly, tax-reserve sheets)",
    usp="Built around variable income and quarterly tax reserves",
    revenue_model="One-off sales of the bundle",
    channels=["etsy", "own_site"],
    scope="Initial catalog: 3 planners",
    setup_cost_eur=60, recurring_cost_eur_month=10, human_hours_setup=12, human_hours_week=2,
    days_to_first_sale=21, unit_price_eur=19, unit_variable_cost_eur=3, expected_units_month=40,
    automation_potential_pct=70, recurring_revenue=False,
    revenue_assumptions="Assumption (not observed): 40 sales per month after 3 months.",
    cost_assumptions="Platform fee ~6.5% + payment fee; design software 10 EUR/month.",
    competitors="Generic budget printables; few target irregular income.",
    legal_risks="Own designs only; check Etsy digital goods policy; EU VAT on digital goods.",
    automation_opportunities="Template generation and listing copy can be scripted.",
    uncertainties=["Real demand for the niche is unproven"],
    validation_plan="List 3 products, run for 30 days, success = at least 5 sales.",
)


class FakeFetcher:
    """Serves canned pages; records what was requested. Never touches the network."""

    def __init__(self, pages: dict[str, str] | None = None):
        self.pages = pages or {}
        self.requested: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.requested.append(url)
        if url not in self.pages:
            return FetchResult(url=url, status=404, title="", text="", error="not found", robots_allowed=True)
        return FetchResult(url=url, status=200, title="Test page", text=self.pages[url], robots_allowed=True)


class Client:
    """Minimal ASGI test client."""

    def __init__(self, app: Any, token: str | None = TOKEN):
        self.app, self.token = app, token

    def request(self, method: str, path: str, body: Any = None, token: str | None = "default",
                raw: bytes | None = None) -> tuple[int, Any]:
        tok = self.token if token == "default" else token
        path, _, qs = path.partition("?")
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
        if tok:
            headers.append((b"authorization", f"Bearer {tok}".encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": qs.encode(), "headers": headers,
                 "client": ("127.0.0.1", 5000), "server": ("testserver", 80), "root_path": ""}
        out: dict[str, Any] = {"status": None, "body": b""}
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(msg: dict[str, Any]) -> None:
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        asyncio.run(self.app(scope, receive, send))
        text = out["body"].decode() or "null"
        try:
            return out["status"], json.loads(text)
        except json.JSONDecodeError:
            return out["status"], text

    def get(self, path: str, **kw: Any) -> tuple[int, Any]:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        return self.request("POST", path, body if body is not None else {}, **kw)

    def put(self, path: str, body: Any, **kw: Any) -> tuple[int, Any]:
        return self.request("PUT", path, body, **kw)

    def patch(self, path: str, body: Any, **kw: Any) -> tuple[int, Any]:
        return self.request("PATCH", path, body, **kw)


class LabTestCase(unittest.TestCase):
    """Fresh database in a temp dir for every test; worker thread never started."""

    fetcher_pages: dict[str, str] = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = Config(data_dir=Path(self._tmp.name), owner_token=TOKEN, run_worker=False)
        self.fetcher = FakeFetcher(dict(self.fetcher_pages))
        self.boot()

    def boot(self) -> Services:
        """(Re)build all services on the same database file: this is what a restart looks like."""
        old = getattr(self, "svc", None)
        if old is not None:
            old.db.close()  # a restart drops the old process's connections
        self.svc = build_services(self.config, fetcher=self.fetcher, backoff_base_seconds=0.0)
        self.client = Client(create_app(self.svc))
        return self.svc

    def tearDown(self) -> None:
        self.svc.db.close()

    # convenience ---------------------------------------------------------
    def make_idea(self, **over: Any) -> dict[str, Any]:
        return self.svc.ideas.create({**GOOD_IDEA, **over}, proposer="owner")

    def add_good_evidence(self, idea_id: str, n: int = 3) -> None:
        """Owner-entered, owner-verified facts so evidence gates can pass in tests."""
        for i in range(n):
            ev = self.svc.ideas.add_evidence(idea_id, f"Observed competitor listing #{i} priced 15-25 EUR", "fact",
                                             url=f"https://example.com/listing-{i}", excerpt="price 19 EUR")
            self.svc.ideas.mark_evidence_verified(ev["id"], True, "owner", "checked by owner")

    def set_settings(self, **values: Any) -> None:
        self.svc.settings.set_many(values)
