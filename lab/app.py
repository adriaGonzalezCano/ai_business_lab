"""Composition root: builds every service once and wires the worker's job handlers.

Nothing here contains business rules; it only connects the pieces so the API, the worker and the tests
all use exactly the same objects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agents import AgentRegistry
from .audit import Audit
from .committee import Committee
from .config import Config
from .costs import CostController
from .db import Database
from .ideas import IdeaRepo
from .integrations import Integrations
from .jobs import JobQueue, Worker
from .llm import LLMGateway
from .profiles import ProfileStore
from .providers import Provider, build_providers
from .research import Fetcher, Research, WebFetcher
from .settings import SettingsStore

JOB_RESEARCH = "research_run"
JOB_COMMITTEE = "committee_run"


@dataclass
class Services:
    config: Config
    db: Database
    audit: Audit
    settings: SettingsStore
    profiles: ProfileStore
    agents: AgentRegistry
    costs: CostController
    gateway: LLMGateway
    ideas: IdeaRepo
    committee: Committee
    research: Research
    queue: JobQueue
    worker: Worker
    integrations: Integrations
    providers: dict[str, Provider]
    startup: dict[str, Any]


def build_services(config: Config, providers: dict[str, Provider] | None = None, fetcher: Fetcher | None = None,
                   backoff_base_seconds: float = 30.0) -> Services:
    db = Database(config.db_path)
    applied = db.migrate()
    audit = Audit(db)
    settings = SettingsStore(db, audit)
    profiles = ProfileStore(db, audit)
    profiles.seed_default()
    agents = AgentRegistry(db, settings, audit)
    agents.seed_defaults()
    costs = CostController(db, settings, audit)
    providers = providers if providers is not None else build_providers(config)
    gateway = LLMGateway(db, settings, agents, costs, providers, audit)
    ideas = IdeaRepo(db, settings, audit)
    committee = Committee(db, settings, agents, gateway, ideas, profiles, audit)
    fetcher = fetcher or WebFetcher(config.user_agent, delay_seconds=float(settings.get("research_fetch_delay_seconds")))
    research = Research(db, settings, agents, gateway, ideas, audit, fetcher)
    queue = JobQueue(db, audit)

    handlers = {
        JOB_RESEARCH: lambda p: research.run_topic(p["topic"], list(p.get("urls", []))),
        JOB_COMMITTEE: lambda p: committee.run(p["idea_id"], actor=p.get("actor", "owner")),
    }
    worker = Worker(queue, handlers, costs, settings, audit, backoff_base_seconds=backoff_base_seconds)

    # Crash recovery: nothing that was in flight is silently lost or silently repeated.
    startup = {
        "migrations_applied": applied,
        "interrupted_runs_marked": gateway.mark_interrupted_runs(),
        "jobs_recovered": queue.recover_running(),
        "stale_reservations_released": costs.sweep_stale_reservations(older_than_minutes=0),
    }
    audit.log("system", "startup", "system", None, startup)
    return Services(config, db, audit, settings, profiles, agents, costs, gateway, ideas, committee, research,
                    queue, worker, Integrations(config), providers, startup)
