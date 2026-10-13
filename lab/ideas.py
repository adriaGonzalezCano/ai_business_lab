"""Idea repository: persistent records, versions, evidence, duplicate prevention, decision history."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from .audit import Audit
from .db import Database
from .schemas import REVISABLE_FIELDS, IdeaDraft
from .settings import SettingsStore
from .util import dumps, jaccard, loads, new_id, now_iso, tokenize

STATUSES = {"proposed", "under_review", "revising", "needs_evidence", "needs_human_review",
            "approved", "rejected", "archived"}
EVIDENCE_KINDS = {"fact", "estimate", "assumption", "speculation"}

_JSON_FIELDS = {"channels": "channels_json", "uncertainties": "uncertainties_json"}
_SCALAR = [
    "name", "description", "category", "target_audience", "customer_problem", "product", "usp", "revenue_model",
    "scope", "setup_cost_eur", "recurring_cost_eur_month", "human_hours_setup", "human_hours_week",
    "days_to_first_sale", "unit_price_eur", "unit_variable_cost_eur", "expected_units_month",
    "automation_potential_pct", "recurring_revenue", "revenue_assumptions", "cost_assumptions", "competitors",
    "legal_risks", "automation_opportunities", "validation_plan",
]


class DuplicateIdea(Exception):
    def __init__(self, existing_id: str, similarity: float):
        super().__init__(f"Very similar to existing idea {existing_id} (similarity {similarity:.2f}). Improve that idea instead.")
        self.existing_id, self.similarity = existing_id, similarity


def margin_pct(idea: dict[str, Any]) -> float | None:
    p, vc = idea.get("unit_price_eur"), idea.get("unit_variable_cost_eur")
    if p is None or vc is None or p <= 0:
        return None
    return round((p - vc) / p * 100, 2)


def _fingerprint(d: dict[str, Any]) -> str:
    toks = tokenize(" ".join(str(d.get(k, "")) for k in ("name", "category", "product", "target_audience")))
    return " ".join(sorted(toks))


class IdeaRepo:
    def __init__(self, db: Database, settings: SettingsStore, audit: Audit):
        self.db, self.settings, self.audit = db, settings, audit

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _row(r) -> dict[str, Any]:
        d = dict(r)
        d["channels"] = loads(d.pop("channels_json"), [])
        d["uncertainties"] = loads(d.pop("uncertainties_json"), [])
        d["recurring_revenue"] = bool(d["recurring_revenue"])
        d["synthetic"] = bool(d["synthetic"])
        d.pop("fingerprint", None)
        d["contribution_margin_pct"] = margin_pct(d)
        return d

    def add_event(self, idea_id: str, event: str, actor: str, detail: dict[str, Any] | None = None) -> None:
        self.db.execute("INSERT INTO idea_events(idea_id,event,actor,detail_json,created_at) VALUES (?,?,?,?,?)",
                        (idea_id, event, actor, dumps(detail or {}), now_iso()))

    # -- duplicates ------------------------------------------------------
    def find_duplicate(self, draft: dict[str, Any]) -> tuple[str, float] | None:
        mine = set(_fingerprint(draft).split())
        threshold = float(self.settings.get("duplicate_similarity_threshold"))
        best: tuple[str, float] | None = None
        for r in self.db.query("SELECT id, fingerprint FROM ideas WHERE status!='archived'"):
            sim = jaccard(mine, set(r["fingerprint"].split()))
            if sim >= threshold and (best is None or sim > best[1]):
                best = (r["id"], sim)
        return best

    # -- create / read ---------------------------------------------------
    def create(self, draft: IdeaDraft | dict[str, Any], proposer: str = "owner", synthetic: bool = False,
               parent_id: str | None = None, allow_duplicate: bool = False, actor: str | None = None) -> dict[str, Any]:
        if not isinstance(draft, IdeaDraft):
            draft = IdeaDraft.model_validate(draft)
        data = draft.model_dump()
        dup = None if allow_duplicate else self.find_duplicate(data)
        if dup:
            raise DuplicateIdea(*dup)
        idea_id = new_id("idea")
        now = now_iso()
        with self.db.tx():
            self.db.execute(
                "INSERT INTO ideas(id,fingerprint,name,description,category,target_audience,customer_problem,product,usp,"
                "revenue_model,channels_json,scope,setup_cost_eur,recurring_cost_eur_month,human_hours_setup,"
                "human_hours_week,days_to_first_sale,unit_price_eur,unit_variable_cost_eur,expected_units_month,"
                "automation_potential_pct,recurring_revenue,revenue_assumptions,cost_assumptions,competitors,legal_risks,"
                "automation_opportunities,uncertainties_json,validation_plan,status,proposer_agent,synthetic,"
                "parent_idea_id,version,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'proposed',?,?,?,1,?,?)",
                (idea_id, _fingerprint(data), data["name"], data["description"], data["category"],
                 data["target_audience"], data["customer_problem"], data["product"], data["usp"], data["revenue_model"],
                 dumps(data["channels"]), data["scope"], data["setup_cost_eur"], data["recurring_cost_eur_month"],
                 data["human_hours_setup"], data["human_hours_week"], data["days_to_first_sale"],
                 data["unit_price_eur"], data["unit_variable_cost_eur"], data["expected_units_month"],
                 data["automation_potential_pct"], 1 if data["recurring_revenue"] else 0, data["revenue_assumptions"],
                 data["cost_assumptions"], data["competitors"], data["legal_risks"], data["automation_opportunities"],
                 dumps(data["uncertainties"]), data["validation_plan"], proposer, 1 if synthetic else 0,
                 parent_id, now, now))
            self._snapshot(idea_id, 1, proposer, "created")
            self.add_event(idea_id, "created", actor or proposer, {"synthetic": synthetic})
            self.audit.log(actor or proposer, "idea_created", "idea", idea_id, {"name": data["name"]})
        return self.get(idea_id)

    def get(self, idea_id: str) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM ideas WHERE id=?", (idea_id,))
        if r is None:
            raise KeyError(f"unknown idea: {idea_id}")
        return self._row(r)

    def list(self, status: str | None = None, category: str | None = None, q: str | None = None,
             sort: str = "updated_desc", limit: int = 200) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM ideas WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if category:
            sql += " AND category=?"
            params.append(category)
        if q:
            sql += " AND (name LIKE ? OR description LIKE ? OR product LIKE ?)"
            params += [f"%{q}%"] * 3
        order = {"updated_desc": "updated_at DESC", "created_desc": "created_at DESC", "name": "name COLLATE NOCASE",
                 "setup_cost": "setup_cost_eur ASC"}.get(sort, "updated_at DESC")
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(min(limit, 500))
        out = []
        for r in self.db.query(sql, params):
            d = self._row(r)
            last = self.db.one("SELECT outcome, overall_score, synthetic FROM consensus_decisions WHERE idea_id=? "
                               "ORDER BY id DESC LIMIT 1", (d["id"],))
            d["last_decision"] = dict(last) if last else None
            out.append(d)
        return out

    def detail(self, idea_id: str) -> dict[str, Any]:
        idea = self.get(idea_id)
        idea["versions"] = [
            {"version": r["version"], "changed_by": r["changed_by"], "reason": r["reason"], "created_at": r["created_at"]}
            for r in self.db.query("SELECT * FROM idea_versions WHERE idea_id=? ORDER BY version", (idea_id,))]
        idea["evidence"] = self.evidence(idea_id)
        idea["events"] = [
            {"event": r["event"], "actor": r["actor"], "detail": loads(r["detail_json"], {}), "created_at": r["created_at"]}
            for r in self.db.query("SELECT * FROM idea_events WHERE idea_id=? ORDER BY id", (idea_id,))]
        idea["evaluations"] = [self._eval_row(r) for r in self.db.query(
            "SELECT * FROM idea_evaluations WHERE idea_id=? ORDER BY round, id", (idea_id,))]
        idea["decisions"] = [self._decision_row(r) for r in self.db.query(
            "SELECT * FROM consensus_decisions WHERE idea_id=? ORDER BY round, id", (idea_id,))]
        idea["evidence_stats"] = self.evidence_stats(idea_id)
        return idea

    @staticmethod
    def _eval_row(r) -> dict[str, Any]:
        d = dict(r)
        for src, dst in (("scores_json", "scores"), ("evidence_json", "evidence"), ("assumptions_json", "assumptions"),
                         ("objections_json", "objections"), ("uncertainties_json", "uncertainties"),
                         ("discarded_scores_json", "discarded_scores")):
            d[dst] = loads(d.pop(src), {} if "scores" in dst else [])
        d["synthetic"] = bool(d["synthetic"])
        return d

    @staticmethod
    def _decision_row(r) -> dict[str, Any]:
        d = dict(r)
        for src, dst in (("category_scores_json", "category_scores"), ("gate_results_json", "gate_results"),
                         ("rule_results_json", "rule_results"), ("dissent_json", "dissent"),
                         ("profile_snapshot_json", "profile_snapshot")):
            d[dst] = loads(d.pop(src), None)
        d["synthetic"] = bool(d["synthetic"])
        return d

    # -- versions / revision -----------------------------------------------
    def _snapshot(self, idea_id: str, version: int, by: str, reason: str) -> None:
        snap = self.get(idea_id)
        self.db.execute(
            "INSERT INTO idea_versions(idea_id,version,snapshot_json,changed_by,reason,created_at) VALUES (?,?,?,?,?,?)",
            (idea_id, version, dumps(snap), by, reason, now_iso()))

    def revise(self, idea_id: str, changes: dict[str, Any], by: str, reason: str = "") -> dict[str, Any]:
        """Apply a validated revision as a new version. Only REVISABLE_FIELDS may change."""
        bad = set(changes) - set(REVISABLE_FIELDS)
        if bad:
            raise ValueError(f"fields not revisable: {sorted(bad)}")
        cur = self.get(idea_id)
        merged = {k: cur[k] for k in IdeaDraft.model_fields if k in cur}
        merged.update(changes)
        try:
            draft = IdeaDraft.model_validate(merged)
        except ValidationError as e:
            raise ValueError(f"revision is invalid: {e.errors()[0]['loc']}: {e.errors()[0]['msg']}") from None
        data = draft.model_dump()
        sets, params = [], []
        for k in changes:
            if k in _JSON_FIELDS:
                sets.append(f"{_JSON_FIELDS[k]}=?")
                params.append(dumps(data[k]))
            else:
                sets.append(f"{k}=?")
                params.append(1 if (k == "recurring_revenue" and data[k]) else (0 if k == "recurring_revenue" else data[k]))
        if not sets:
            return cur
        new_version = cur["version"] + 1
        with self.db.tx():
            self.db.execute(f"UPDATE ideas SET {', '.join(sets)}, version=?, updated_at=? WHERE id=?",
                            params + [new_version, now_iso(), idea_id])
            self._snapshot(idea_id, new_version, by, reason or "revised")
            self.add_event(idea_id, "revised", by, {"fields": sorted(changes), "reason": reason, "version": new_version})
            self.audit.log(by, "idea_revised", "idea", idea_id, {"fields": sorted(changes), "version": new_version})
        return self.get(idea_id)

    def set_status(self, idea_id: str, status: str, actor: str, detail: dict[str, Any] | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        with self.db.tx():
            old = self.get(idea_id)["status"]
            self.db.execute("UPDATE ideas SET status=?, updated_at=? WHERE id=?", (status, now_iso(), idea_id))
            self.add_event(idea_id, "status_changed", actor, {"from": old, "to": status, **(detail or {})})
            self.audit.log(actor, "idea_status", "idea", idea_id, {"from": old, "to": status})

    def owner_decision(self, idea_id: str, decision: str, reason: str, actor: str = "owner") -> dict[str, Any]:
        """Owner override. Approving still authorizes no spending (that needs the Phase 2 approval queue)."""
        mapping = {"approve": "approved", "reject": "rejected", "return_to_research": "needs_evidence",
                   "archive": "archived", "reopen": "proposed"}
        if decision not in mapping:
            raise ValueError(f"decision must be one of {sorted(mapping)}")
        if len(reason.strip()) < 10:
            raise ValueError("a reason of at least 10 characters is required")
        self.set_status(idea_id, mapping[decision], actor, {"owner_override": True, "decision": decision, "reason": reason})
        return self.get(idea_id)

    # -- evidence ----------------------------------------------------------
    def add_evidence(self, idea_id: str, claim: str, kind: str, url: str | None = None,
                     retrieved_at: str | None = None, excerpt: str | None = None,
                     observation_id: str | None = None, actor: str = "owner") -> dict[str, Any]:
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"kind must be one of {sorted(EVIDENCE_KINDS)}")
        if not claim or not claim.strip():
            raise ValueError("claim is required")
        self.get(idea_id)
        verified, verifier_note, verified_by = 0, None, None
        if observation_id:
            o = self.db.one("SELECT o.*, s.url AS src_url, s.retrieved_at AS src_ts FROM market_observations o "
                            "LEFT JOIN research_sources s ON s.id=o.source_id WHERE o.id=?", (observation_id,))
            if o is None:
                raise KeyError(f"unknown observation: {observation_id}")
            url, retrieved_at = url or o["src_url"], retrieved_at or o["src_ts"]
            excerpt = excerpt or o["excerpt"]
            verified, verifier_note = o["verified"], o["verifier_note"]
            verified_by = "research_verifier" if verified else None
        if url:
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.netloc:
                raise ValueError("url must be an http(s) URL")
        ev_id = new_id("ev")
        with self.db.tx():
            self.db.execute(
                "INSERT INTO idea_evidence(id,idea_id,observation_id,claim,kind,url,retrieved_at,excerpt,verified,"
                "verified_by,verifier_note,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev_id, idea_id, observation_id, claim.strip(), kind, url, retrieved_at, excerpt, verified,
                 verified_by, verifier_note, now_iso()))
            self.add_event(idea_id, "evidence_added", actor, {"evidence_id": ev_id, "kind": kind, "verified": bool(verified)})
        return self.evidence_one(ev_id)

    def evidence_one(self, ev_id: str) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM idea_evidence WHERE id=?", (ev_id,))
        if r is None:
            raise KeyError(ev_id)
        d = dict(r)
        d["verified"] = bool(d["verified"])
        return d

    def evidence(self, idea_id: str) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query(
                "SELECT e.*, o.confidence AS confidence, o.synthetic AS obs_synthetic FROM idea_evidence e "
                "LEFT JOIN market_observations o ON o.id=e.observation_id WHERE e.idea_id=? ORDER BY e.created_at", (idea_id,)):
            d = dict(r)
            d["verified"] = bool(d["verified"])
            d["obs_synthetic"] = bool(d["obs_synthetic"]) if d["obs_synthetic"] is not None else False
            out.append(d)
        return out

    def mark_evidence_verified(self, ev_id: str, verified: bool, by: str, note: str = "") -> dict[str, Any]:
        """Owner (or the deterministic verifier) records a verification result."""
        ev = self.evidence_one(ev_id)
        with self.db.tx():
            self.db.execute("UPDATE idea_evidence SET verified=?, verified_by=?, verifier_note=? WHERE id=?",
                            (1 if verified else 0, by, note or None, ev_id))
            self.add_event(ev["idea_id"], "evidence_verified" if verified else "evidence_unverified", by,
                           {"evidence_id": ev_id, "note": note})
        return self.evidence_one(ev_id)

    def evidence_stats(self, idea_id: str) -> dict[str, Any]:
        min_conf = float(self.settings.get("min_evidence_confidence"))
        items = self.evidence(idea_id)
        usable = [e for e in items if (e["confidence"] if e["confidence"] is not None else 1.0) >= min_conf]
        facts = [e for e in usable if e["kind"] == "fact"]
        domains = {urlparse(e["url"]).netloc for e in usable if e["url"]}
        return {
            "total": len(items), "count": len(usable),
            "below_confidence": len(items) - len(usable),
            "fact_count": len(facts),
            "verified_count": sum(1 for e in usable if e["verified"]),
            "verified_fact_count": sum(1 for e in facts if e["verified"]),
            "distinct_sources": len(domains),
            "by_kind": {k: sum(1 for e in usable if e["kind"] == k) for k in sorted(EVIDENCE_KINDS)},
            "synthetic_count": sum(1 for e in usable if e["obs_synthetic"]),
        }
