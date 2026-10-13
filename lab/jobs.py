"""Persistent job queue: idempotency keys, bounded retries with backoff, recovery after restarts, and a worker.

Handlers must be safe to run twice (the queue gives at-least-once execution; idempotency keys prevent
duplicate *enqueueing*, and handlers use stable keys for anything they create).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .audit import Audit
from .costs import BudgetExceeded, CircularActivity, CostController, PricingMissing
from .db import Database
from .llm import AgentDisabled
from .providers import ProviderUnavailable
from .settings import SettingsStore
from .util import dumps, loads, new_id, now_iso

# Errors that retrying cannot fix: the job fails immediately and the owner decides.
NON_RETRYABLE = (BudgetExceeded, CircularActivity, PricingMissing, ProviderUnavailable, AgentDisabled,
                 PermissionError, ValueError, KeyError)


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class JobQueue:
    def __init__(self, db: Database, audit: Audit):
        self.db, self.audit = db, audit

    def enqueue(self, kind: str, payload: dict[str, Any] | None = None, idempotency_key: str | None = None,
                max_attempts: int = 3, delay_seconds: float = 0) -> str:
        """Returns the job id. If idempotency_key already exists, returns the existing job id (no duplicate)."""
        with self.db.tx():
            if idempotency_key:
                r = self.db.one("SELECT id FROM jobs WHERE idempotency_key=?", (idempotency_key,))
                if r:
                    return r["id"]
            jid = new_id("job")
            run_after = _ts(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
            self.db.execute(
                "INSERT INTO jobs(id,kind,payload_json,idempotency_key,status,max_attempts,run_after,created_at,updated_at) "
                "VALUES (?,?,?,?,'queued',?,?,?,?)",
                (jid, kind, dumps(payload or {}), idempotency_key, max_attempts, run_after, now_iso(), now_iso()))
        return jid

    def enqueue_unless_pending(self, kind: str, payload: dict[str, Any], max_attempts: int = 3) -> tuple[str, bool]:
        """Owner-triggered work: a double click must not pay for the same job twice.

        Returns (job_id, created). If an identical job is still queued or running, that job is returned."""
        body = dumps(payload)
        with self.db.tx():
            row = self.db.one("SELECT id FROM jobs WHERE kind=? AND payload_json=? AND status IN ('queued','running')",
                              (kind, body))
            if row:
                return row["id"], False
            return self.enqueue(kind, payload, max_attempts=max_attempts), True

    def claim_next(self, kinds: set[str] | None = None) -> dict[str, Any] | None:
        with self.db.tx():
            rows = self.db.query("SELECT * FROM jobs WHERE status='queued' AND run_after<=? ORDER BY run_after, created_at",
                                 (now_iso(),))
            for r in rows:
                if kinds is not None and r["kind"] not in kinds:
                    continue
                self.db.execute("UPDATE jobs SET status='running', attempts=attempts+1, locked_at=?, updated_at=? WHERE id=?",
                                (now_iso(), now_iso(), r["id"]))
                return self.get(r["id"])
        return None

    def get(self, job_id: str) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if r is None:
            raise KeyError(job_id)
        d = dict(r)
        d["payload"] = loads(d.pop("payload_json"), {})
        d["result"] = loads(d.pop("result_json"), None)
        return d

    def complete(self, job_id: str, result: dict[str, Any] | None = None) -> None:
        self.db.execute("UPDATE jobs SET status='succeeded', result_json=?, error=NULL, locked_at=NULL, updated_at=? WHERE id=?",
                        (dumps(result or {}), now_iso(), job_id))

    def fail(self, job_id: str, error: str, retryable: bool = True, backoff_base_seconds: float = 30.0) -> str:
        """Returns the new status: 'queued' (will retry) or 'failed'."""
        with self.db.tx():
            j = self.get(job_id)
            if retryable and j["attempts"] < j["max_attempts"]:
                delay = backoff_base_seconds * (2 ** (j["attempts"] - 1))
                run_after = _ts(datetime.now(timezone.utc) + timedelta(seconds=delay))
                self.db.execute("UPDATE jobs SET status='queued', error=?, run_after=?, locked_at=NULL, updated_at=? WHERE id=?",
                                (error[:500], run_after, now_iso(), job_id))
                return "queued"
            self.db.execute("UPDATE jobs SET status='failed', error=?, locked_at=NULL, updated_at=? WHERE id=?",
                            (error[:500], now_iso(), job_id))
            self.audit.log("system", "job_failed", "job", job_id, {"kind": j["kind"], "error": error[:300]})
            return "failed"

    def recover_running(self, older_than_minutes: float | None = None) -> int:
        """Jobs left 'running' by a crash go back to the queue (or fail if attempts are exhausted)."""
        sql = "SELECT id, attempts, max_attempts FROM jobs WHERE status='running'"
        params: list[Any] = []
        if older_than_minutes is not None:
            sql += " AND locked_at<?"
            params.append(_ts(datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)))
        n = 0
        with self.db.tx():
            for r in self.db.query(sql, params):
                status = "queued" if r["attempts"] < r["max_attempts"] else "failed"
                self.db.execute("UPDATE jobs SET status=?, error='recovered after restart', locked_at=NULL, updated_at=? WHERE id=?",
                                (status, now_iso(), r["id"]))
                n += 1
        return n

    def requeue(self, job_id: str) -> None:
        """Owner action: give a failed/cancelled job another go (attempt counter reset)."""
        self.db.execute("UPDATE jobs SET status='queued', attempts=0, error=NULL, run_after=?, updated_at=? "
                        "WHERE id=? AND status IN ('failed','cancelled')", (now_iso(), now_iso(), job_id))

    def cancel(self, job_id: str) -> None:
        self.db.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status='queued'", (now_iso(), job_id))

    def list(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, params = "SELECT id FROM jobs", []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(min(limit, 500))
        return [self.get(r["id"]) for r in self.db.query(sql, params)]


class Worker:
    """Single background thread. `process_one` is also called directly by tests."""

    def __init__(self, queue: JobQueue, handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
                 costs: CostController, settings: SettingsStore, audit: Audit, backoff_base_seconds: float = 30.0):
        self.queue, self.handlers, self.costs, self.settings, self.audit = queue, handlers, costs, settings, audit
        self.backoff = backoff_base_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def process_one(self) -> str | None:
        """Run at most one due job. Returns the job's resulting status, or None if nothing was due."""
        if self.costs.is_exhausted():
            return None  # nonessential work stops when the AI budget (minus critical reserve) is used up
        job = self.queue.claim_next(set(self.handlers))
        if job is None:
            return None
        try:
            result = self.handlers[job["kind"]](job["payload"])
        except NON_RETRYABLE as e:
            return self.queue.fail(job["id"], f"{type(e).__name__}: {e}", retryable=False)
        except Exception as e:  # noqa: BLE001 - any other error is retried with backoff
            return self.queue.fail(job["id"], f"{type(e).__name__}: {e}", retryable=True,
                                   backoff_base_seconds=self.backoff)
        self.queue.complete(job["id"], result)
        return "succeeded"

    def enqueue_scheduled_research(self) -> list[str]:
        if not self.settings.get("research_schedule_enabled"):
            return []
        freq = int(self.settings.get("research_frequency_hours"))
        bucket = int(time.time() // (freq * 3600))
        ids = []
        for seed in self.settings.get("research_seeds"):
            if seed["urls"]:
                ids.append(self.queue.enqueue("research_run", {"topic": seed["topic"], "urls": seed["urls"]},
                                              idempotency_key=f"sched:{seed['topic']}:{bucket}"))
        return ids

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.enqueue_scheduled_research()
                if self.process_one() is None:
                    self._stop.wait(3.0)
            except Exception as e:  # noqa: BLE001 - the worker must never die
                self.audit.log("system", "worker_error", "worker", None, {"error": str(e)[:300]})
                self._stop.wait(5.0)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="lab-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
