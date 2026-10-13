"""Versioned evaluation profiles. Editing creates a new version; old versions (and the decisions that
reference them) are never rewritten."""
from __future__ import annotations

from typing import Any

from .audit import Audit
from .db import Database
from .rules import DEFAULT_PROFILE, validate_profile
from .util import dumps, loads, now_iso


class ProfileStore:
    def __init__(self, db: Database, audit: Audit):
        self.db, self.audit = db, audit

    def seed_default(self) -> None:
        if self.db.one("SELECT 1 FROM evaluation_profiles LIMIT 1"):
            return
        self._insert(validate_profile(DEFAULT_PROFILE), "system")

    def _insert(self, cfg: dict[str, Any], actor: str) -> int:
        with self.db.tx():
            ver = (self.db.one("SELECT COALESCE(MAX(version),0)+1 AS v FROM evaluation_profiles")["v"])
            self.db.execute("UPDATE evaluation_profiles SET active=0")
            self.db.execute(
                "INSERT INTO evaluation_profiles(version,name,config_json,active,created_by,created_at) VALUES (?,?,?,1,?,?)",
                (ver, cfg.get("name", "profile"), dumps(cfg), actor, now_iso()))
            self.audit.log(actor, "profile_version_created", "profile", str(ver), {"name": cfg.get("name")})
        return ver

    def active(self) -> tuple[int, dict[str, Any]]:
        r = self.db.one("SELECT version, config_json FROM evaluation_profiles WHERE active=1")
        if r is None:
            self.seed_default()
            r = self.db.one("SELECT version, config_json FROM evaluation_profiles WHERE active=1")
        return r["version"], loads(r["config_json"])

    def get(self, version: int) -> dict[str, Any]:
        r = self.db.one("SELECT config_json FROM evaluation_profiles WHERE version=?", (version,))
        if r is None:
            raise KeyError(f"unknown profile version {version}")
        return loads(r["config_json"])

    def save_new_version(self, cfg: Any, actor: str = "owner") -> int:
        return self._insert(validate_profile(cfg), actor)

    def versions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT version, name, active, created_by, created_at FROM evaluation_profiles ORDER BY version DESC")]
