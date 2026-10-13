"""Research room: fetch -> extract observations -> verify excerpts in code -> scout opportunities.

Safety rules baked in:
  * the fetcher only reads http(s), refuses private/loopback/link-local addresses (SSRF), follows redirects
    manually with re-validation, honours robots.txt (fail closed), identifies itself, rate-limits per host;
  * fetched text is untrusted data: it is wrapped before it reaches a model;
  * the Research Verifier is code: an observation is `verified` only if its quoted excerpt really occurs in
    the stored page text. Unverified 'fact' claims are downgraded to 'assumption' with confidence 0.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Protocol
from urllib.parse import urljoin, urlparse

from .agents import AgentRegistry
from .audit import Audit
from .db import Database
from .ideas import DuplicateIdea, IdeaRepo
from .llm import LLMGateway, wrap_untrusted
from .schemas import ObservationsOut, ScoutOut
from .settings import SettingsStore
from .util import new_id, now_iso

MAX_BYTES = 1_000_000
MAX_TEXT = 60_000


@dataclass
class FetchResult:
    url: str
    status: int | None
    title: str
    text: str
    robots_allowed: bool
    error: str | None = None


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResult: ...


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP and tag != "head":
            self._skip += 1
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and tag != "head" and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    p = _TextExtractor()
    p.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return re.sub(r"\s+", " ", p.title).strip(), text


def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # type: ignore[override]
        return None


class WebFetcher:
    def __init__(self, user_agent: str, delay_seconds: float = 2.0, timeout: float = 15.0,
                 opener: urllib.request.OpenerDirector | None = None, host_check: Callable[[str], bool] = _host_is_public):
        self.ua, self.delay, self.timeout = user_agent, delay_seconds, timeout
        self._opener = opener or urllib.request.build_opener(_NoRedirect)
        self._host_check = host_check
        self._robots: dict[str, urllib.robotparser.RobotFileParser | bool] = {}
        self._last: dict[str, float] = {}

    def _get(self, url: str) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "text/html,text/plain;q=0.9"})
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, b""

    def _robots_allows(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            try:
                status, _, body = self._get(origin + "/robots.txt")
            except (urllib.error.URLError, OSError, TimeoutError):
                self._robots[origin] = False  # fail closed
            else:
                if status in (404, 410):
                    self._robots[origin] = True
                elif status != 200:
                    self._robots[origin] = False
                else:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(body.decode("utf-8", "replace").splitlines())
                    self._robots[origin] = rp
        rule = self._robots[origin]
        if isinstance(rule, bool):
            return rule
        return rule.can_fetch(self.ua, url)

    def _pace(self, host: str) -> None:
        wait = self._last.get(host, 0) + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def fetch(self, url: str) -> FetchResult:
        cur = url
        for _ in range(4):
            p = urlparse(cur)
            if p.scheme not in ("http", "https") or not p.hostname:
                return FetchResult(cur, None, "", "", False, "only http(s) URLs are allowed")
            if not self._host_check(p.hostname):
                return FetchResult(cur, None, "", "", False, "host resolves to a non-public address; refused")
            try:
                if not self._robots_allows(cur):
                    return FetchResult(cur, None, "", "", False, "disallowed by robots.txt (or robots.txt unreadable)")
                self._pace(p.netloc)
                status, headers, body = self._get(cur)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                return FetchResult(cur, None, "", "", True, f"network error: {e}")
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                cur = urljoin(cur, headers["location"])
                continue
            if status != 200:
                return FetchResult(cur, status, "", "", True, f"HTTP {status}")
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in ("text/html", "text/plain", "application/xhtml+xml"):
                return FetchResult(cur, status, "", "", True, f"unsupported content type {ctype or 'unknown'}")
            if len(body) > MAX_BYTES:
                body = body[:MAX_BYTES]
            raw = body.decode("utf-8", "replace")
            title, text = html_to_text(raw) if ctype != "text/plain" else ("", raw)
            return FetchResult(cur, status, title, text[:MAX_TEXT], True)
        return FetchResult(cur, None, "", "", True, "too many redirects")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class Research:
    def __init__(self, db: Database, settings: SettingsStore, agents: AgentRegistry, gateway: LLMGateway,
                 ideas: IdeaRepo, audit: Audit, fetcher: Fetcher):
        self.db, self.settings, self.agents, self.gateway = db, settings, agents, gateway
        self.ideas, self.audit, self.fetcher = ideas, audit, fetcher

    # -- sources -----------------------------------------------------------
    def fetch_source(self, url: str, actor: str = "market_researcher") -> dict[str, Any]:
        self.agents.require_permission("market_researcher", "web_fetch")
        res = self.fetcher.fetch(url)
        if res.error or not res.text.strip():
            self.audit.log(actor, "fetch_failed", "source", None, {"url": url, "error": res.error or "empty page"})
            return {"ok": False, "url": url, "error": res.error or "page had no readable text",
                    "robots_allowed": res.robots_allowed}
        h = hashlib.sha256(res.text.encode()).hexdigest()
        with self.db.tx():
            row = self.db.one("SELECT id FROM research_sources WHERE url=? AND content_hash=?", (res.url, h))
            if row:
                sid, new = row["id"], False
            else:
                sid, new = new_id("src"), True
                self.db.execute(
                    "INSERT INTO research_sources(id,url,title,kind,content_text,content_hash,retrieved_at,http_status,"
                    "robots_allowed) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, res.url, res.title, "web", res.text, h, now_iso(), res.status, 1))
        return {"ok": True, "source_id": sid, "url": res.url, "title": res.title, "new": new, "chars": len(res.text)}

    def sources(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT id,url,title,kind,content_hash,retrieved_at,http_status,note,LENGTH(content_text) AS chars "
            "FROM research_sources ORDER BY retrieved_at DESC LIMIT ?", (min(limit, 500),))]

    # -- extraction + programmatic verification ---------------------------------
    def extract(self, source_id: str, topic: str, run_id: str | None = None) -> list[dict[str, Any]]:
        src = self.db.one("SELECT * FROM research_sources WHERE id=?", (source_id,))
        if src is None:
            raise KeyError(f"unknown source {source_id}")
        system = ("Role: market_researcher. From the source text, extract at most 6 market observations relevant to the "
                  "topic: customer needs, pricing, demand signals, competitor positioning, saturation, complaints. "
                  "For every observation give an EXACT short excerpt copied verbatim from the source (<= 300 chars). "
                  "kind: 'fact' only if the source states it; 'estimate' if it gives a rough figure; 'assumption' or "
                  "'speculation' otherwise. confidence 0-1 reflects how directly the source supports it. JSON: "
                  '{"observations": [{"statement": str, "kind": "fact|estimate|assumption|speculation", '
                  '"excerpt": str, "confidence": 0..1}]}')
        prompt = f"Topic: {topic}\nSource URL: {src['url']}\n" + wrap_untrusted(src["url"], src["content_text"] or "")
        out, meta = self.gateway.complete_json(
            agent_id="market_researcher", task="extract_observations", system=system, prompt=prompt,
            schema=ObservationsOut, context={"text": src["content_text"] or "", "topic": topic},
            run_id=run_id, cacheable=True)
        verifier_on = bool(self.db.one("SELECT enabled FROM agents WHERE id='research_verifier'")["enabled"])
        haystack = _norm(src["content_text"] or "")
        created = []
        with self.db.tx():
            for o in out.observations:
                ex = _norm(o.excerpt)
                found = bool(ex) and len(ex) >= 15 and ex in haystack
                verified = found and verifier_on
                kind, conf = o.kind, o.confidence
                note = "excerpt found verbatim in stored source text" if verified else (
                    "verifier disabled" if (found and not verifier_on) else "excerpt NOT found in source text")
                if not verified:
                    conf = 0.0
                    if kind == "fact":
                        kind = "assumption"  # an unverifiable 'fact' must not count as a fact
                if self.db.one("SELECT 1 FROM market_observations WHERE source_id=? AND statement=?",
                               (source_id, o.statement)):
                    continue
                oid = new_id("obs")
                self.db.execute(
                    "INSERT INTO market_observations(id,source_id,topic,statement,kind,excerpt,confidence,verified,"
                    "verifier_note,synthetic,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, source_id, topic, o.statement, kind, o.excerpt, conf, 1 if verified else 0, note,
                     1 if meta["synthetic"] else 0, now_iso()))
                created.append(self.observation(oid))
        return created

    def observation(self, obs_id: str) -> dict[str, Any]:
        r = self.db.one("SELECT o.*, s.url AS url, s.title AS source_title FROM market_observations o "
                        "LEFT JOIN research_sources s ON s.id=o.source_id WHERE o.id=?", (obs_id,))
        d = dict(r)
        d["verified"], d["synthetic"] = bool(d["verified"]), bool(d["synthetic"])
        return d

    def observations(self, topic: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql, params = ("SELECT o.id FROM market_observations o WHERE 1=1"), []
        if topic:
            sql += " AND o.topic=?"
            params.append(topic)
        sql += " ORDER BY o.created_at DESC LIMIT ?"
        params.append(min(limit, 500))
        return [self.observation(r["id"]) for r in self.db.query(sql, params)]

    def add_manual_observation(self, topic: str, statement: str, kind: str, url: str | None = None,
                               excerpt: str = "", actor: str = "owner") -> dict[str, Any]:
        if kind not in ("fact", "estimate", "assumption", "speculation"):
            raise ValueError("invalid kind")
        if len(statement.strip()) < 5:
            raise ValueError("statement is too short")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        sid = None
        if url:
            sid = new_id("src")
            self.db.execute("INSERT INTO research_sources(id,url,title,kind,retrieved_at,note) VALUES (?,?,?,?,?,?)",
                            (sid, url, "", "manual", now_iso(), "entered by owner; page text not stored"))
        oid = new_id("obs")
        self.db.execute(
            "INSERT INTO market_observations(id,source_id,topic,statement,kind,excerpt,confidence,verified,verifier_note,"
            "synthetic,created_at) VALUES (?,?,?,?,?,?,?,0,?,0,?)",
            (oid, sid, topic.strip(), statement.strip(), kind, excerpt, 0.5,
             "entered by the owner; not machine-verified", now_iso()))
        self.audit.log(actor, "observation_added", "observation", oid, {"topic": topic})
        return self.observation(oid)

    def topics(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT topic, COUNT(*) AS observations, SUM(verified) AS verified, MAX(created_at) AS last_at "
            "FROM market_observations GROUP BY topic ORDER BY last_at DESC")]

    # -- scouting ---------------------------------------------------------------
    def scout(self, topic: str, observation_ids: list[str], run_id: str | None = None) -> dict[str, Any]:
        self.agents.require_permission("opportunity_scout", "create_idea")
        obs = [self.observation(i) for i in observation_ids]
        if not obs:
            return {"created": [], "duplicates": [], "note": "no observations to scout from"}
        settings = self.settings.all()
        system = ("Role: opportunity_scout. Propose at most 2 specific, monetizable business opportunities grounded ONLY in "
                  "the observations. Each needs a target customer, problem, product, USP, pricing (unit_price_eur and "
                  "unit_variable_cost_eur), channels, costs, human hours, validation plan, risks, uncertainties, and the "
                  "ids of the observations it relies on (evidence_observation_ids). Never invent demand figures; label "
                  "projections as assumptions. Owner constraints: "
                  + str({k: settings[k] for k in ("markets", "languages", "preferred_categories", "excluded_categories",
                                                  "max_budget_per_business_eur", "max_human_hours_per_week")})
                  + ". JSON: {\"ideas\": [ {name, description, category, target_audience, customer_problem, product, usp, "
                    "revenue_model, channels, scope, setup_cost_eur, recurring_cost_eur_month, human_hours_setup, "
                    "human_hours_week, days_to_first_sale, unit_price_eur, unit_variable_cost_eur, expected_units_month, "
                    "automation_potential_pct, recurring_revenue, revenue_assumptions, cost_assumptions, competitors, "
                    "legal_risks, automation_opportunities, uncertainties, validation_plan, evidence_observation_ids} ]}")
        lines = [f"[{o['id']}] ({o['kind']}, {'verified' if o['verified'] else 'UNVERIFIED'}, conf {o['confidence']}) "
                 f"{o['statement']}" for o in obs]
        prompt = f"Topic: {topic}\n" + wrap_untrusted("observations", "\n".join(lines))
        out, meta = self.gateway.complete_json(
            agent_id="opportunity_scout", task="scout_opportunities", system=system, prompt=prompt, schema=ScoutOut,
            context={"topic": topic, "observations": [{"id": o["id"], "verified": o["verified"]} for o in obs]},
            run_id=run_id)
        allowed_ids = {o["id"] for o in obs}
        created, duplicates = [], []
        for draft in out.ideas:
            data = draft.model_dump(exclude={"evidence_observation_ids"})
            try:
                idea = self.ideas.create(data, proposer="opportunity_scout", synthetic=meta["synthetic"],
                                         actor="opportunity_scout")
            except DuplicateIdea as d:
                duplicates.append({"name": draft.name, "existing_id": d.existing_id, "similarity": round(d.similarity, 2)})
                continue
            for oid in draft.evidence_observation_ids:
                if oid in allowed_ids:  # the scout cannot cite observations it was not given
                    o = self.observation(oid)
                    self.ideas.add_evidence(idea["id"], o["statement"], o["kind"], observation_id=oid, actor="opportunity_scout")
            created.append(self.ideas.get(idea["id"]))
        return {"created": created, "duplicates": duplicates}

    # -- pipeline -----------------------------------------------------------------
    def run_topic(self, topic: str, urls: list[str]) -> dict[str, Any]:
        summary: dict[str, Any] = {"topic": topic, "sources": [], "observations": 0, "verified": 0,
                                   "ideas_created": [], "duplicates": [], "errors": []}
        obs_ids: list[str] = []
        with self.gateway.run("market_researcher", "research_topic") as run_id:
            for url in urls:
                f = self.fetch_source(url)
                summary["sources"].append({k: f.get(k) for k in ("url", "ok", "error", "source_id")})
                if not f["ok"]:
                    summary["errors"].append(f"{url}: {f['error']}")
                    continue
                for o in self.extract(f["source_id"], topic, run_id=run_id):
                    obs_ids.append(o["id"])
                    summary["observations"] += 1
                    summary["verified"] += 1 if o["verified"] else 0
        if obs_ids:
            with self.gateway.run("opportunity_scout", "scout_topic") as run_id:
                res = self.scout(topic, obs_ids, run_id=run_id)
            summary["ideas_created"] = [i["id"] for i in res["created"]]
            summary["duplicates"] = res["duplicates"]
        self.audit.log("market_researcher", "research_run", "topic", topic,
                       {k: summary[k] for k in ("observations", "verified", "ideas_created")})
        return summary
