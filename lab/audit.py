"""Append-only audit log. Details are redacted before storage."""
from __future__ import annotations

from typing import Any

from .db import Database
from .util import dumps, loads, now_iso, redact


class Audit:
    def __init__(self, db: Database):
        self.db = db

    def log(self, actor: str, action: str, entity: str | None = None, entity_id: str | None = None,
            detail: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO audit_events(actor, action, entity, entity_id, detail_json, created_at) VALUES (?,?,?,?,?,?)",
            (actor, action, entity, entity_id, redact(dumps(detail or {})), now_iso()),
        )

    def recent(self, limit: int = 100, entity: str | None = None, entity_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM audit_events"
        params: list[Any] = []
        if entity:
            sql += " WHERE entity=?"
            params.append(entity)
            if entity_id:
                sql += " AND entity_id=?"
                params.append(entity_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [
            {**dict(r), "detail": loads(r["detail_json"], {})}
            for r in self.db.query(sql, params)
        ]
