"""SQLite access: per-thread connections, explicit transactions, versioned migrations."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .util import now_iso

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()

    # -- connections ---------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            if self.path != ":memory:":
                c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
            self._local.depth = 0
            with self._all_lock:
                self._all.append(c)
        return c

    def close(self) -> None:
        """Close every thread's connection (the API's worker threads each hold one)."""
        with self._all_lock:
            conns, self._all = self._all, []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction (BEGIN IMMEDIATE). Re-entrant: inner calls join the outer one."""
        c = self.conn()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            c.execute("BEGIN IMMEDIATE")
        self._local.depth = depth + 1
        try:
            yield c
        except BaseException:
            self._local.depth -= 1
            if self._local.depth == 0:
                c.execute("ROLLBACK")
            raise
        else:
            self._local.depth -= 1
            if self._local.depth == 0:
                c.execute("COMMIT")

    # -- convenience ---------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        if getattr(self._local, "depth", 0) > 0:
            return self.conn().execute(sql, params)
        with self.tx() as c:
            return c.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    # -- migrations ----------------------------------------------------
    def migrate(self) -> list[str]:
        """Apply pending .sql migrations in filename order. Returns the versions applied."""
        c = self.conn()
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        done = {r["version"] for r in c.execute("SELECT version FROM schema_migrations")}
        applied: list[str] = []
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if f.stem in done:
                continue
            script = f.read_text(encoding="utf-8")
            try:
                c.executescript("BEGIN IMMEDIATE;\n" + script)
                c.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (f.stem, now_iso())
                )
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
            applied.append(f.stem)
        return applied
