"""Atomic claims for persisted business work launched through Celery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import threading
from typing import Any

from sqlalchemy import event, update
from sqlalchemy.orm import Session


log = logging.getLogger(__name__)
LEGACY_TASK_LEASE_SECONDS = 90
LEGACY_TASK_HEARTBEAT_SECONDS = 20


def celery_dispatch_enabled() -> bool:
    """Whether this API process is configured to dispatch durable Celery work."""
    return os.getenv("CELERY_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def claim_queued_task(
    db: Session,
    model: type,
    task_id: str,
    *,
    progress: dict[str, Any],
    worker_task_id: str,
) -> Any | None:
    """Atomically transition one persisted task from queued to running.

    Celery deliveries are at-least-once. The conditional SQL UPDATE makes the
    business task row the idempotency boundary: only one delivery can claim a
    queued row, while retries of running or terminal rows become no-ops.
    """
    task_id_column = getattr(model, "id")
    query = db.query(model).filter(
        task_id_column == task_id,
        getattr(model, "status") == "queued",
    )
    cancel_requested = getattr(model, "cancel_requested", None)
    if cancel_requested is not None:
        query = query.filter(cancel_requested.is_(False))

    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "status": "running",
        "progress": progress,
        "worker_task_id": worker_task_id,
        "lease_generation": getattr(model, "lease_generation") + 1,
        "heartbeat_at": now,
        "lease_expires_at": now + timedelta(seconds=LEGACY_TASK_LEASE_SECONDS),
    }
    if hasattr(model, "updated_at"):
        values["updated_at"] = datetime.now(timezone.utc)

    claimed = query.update(values, synchronize_session=False)
    db.commit()
    if claimed != 1:
        return None
    return db.query(model).filter(task_id_column == task_id).first()


class TaskLeaseLost(RuntimeError):
    """Raised when a worker no longer owns the persisted task lease."""


class TaskLeaseHeartbeat:
    """Renew a task lease and fence every business-session commit."""

    def __init__(self, db: Session, model: type, task_id: str,
                 worker_task_id: str, generation: int):
        self.db = db
        self.model = model
        self.task_id = task_id
        self.worker_task_id = worker_task_id
        self.generation = generation
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._before_commit = self._fence_commit

    def start(self) -> "TaskLeaseHeartbeat":
        event.listen(self.db, "before_commit", self._before_commit)
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"task-lease-{self.task_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _where(self, now: datetime):
        return (
            self.model.id == self.task_id,
            self.model.status == "running",
            self.model.worker_task_id == self.worker_task_id,
            self.model.lease_generation == self.generation,
            self.model.lease_expires_at > now,
        )

    def _fence_commit(self, session: Session) -> None:
        now = datetime.now(timezone.utc)
        # Check and renew on the business transaction's connection before ORM
        # flush, so a stale worker cannot commit pending ontology or audit data.
        result = session.connection().execute(
            update(self.model).where(*self._where(now)).values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=LEGACY_TASK_LEASE_SECONDS),
            )
        )
        if result.rowcount != 1:
            raise TaskLeaseLost(f"Task {self.task_id} lease is no longer owned by this worker")

    def _heartbeat_loop(self) -> None:
        from app.database import SessionLocal

        while not self._stop.wait(LEGACY_TASK_HEARTBEAT_SECONDS):
            now = datetime.now(timezone.utc)
            try:
                with SessionLocal() as heartbeat_db:
                    renewed = heartbeat_db.query(self.model).filter(
                        *self._where(now)
                    ).update({
                        "heartbeat_at": now,
                        "lease_expires_at": now + timedelta(seconds=LEGACY_TASK_LEASE_SECONDS),
                    }, synchronize_session=False)
                    heartbeat_db.commit()
                if renewed != 1:
                    return
            except Exception:
                # A later beat may recover a transient outage. After expiry,
                # neither this update nor a worker commit can reclaim the lease.
                log.exception("Could not renew legacy task lease %s", self.task_id)

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        event.remove(self.db, "before_commit", self._before_commit)


def release_task_lease(task: Any) -> None:
    task.worker_task_id = None
    task.heartbeat_at = None
    task.lease_expires_at = None


def fail_claimed_task(db: Session, model: type, task_id: str, *,
                      worker_task_id: str, generation: int, error: str) -> bool:
    """Persist a failure only while this worker still owns a live lease."""
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "status": "failed",
        "worker_task_id": None,
        "heartbeat_at": None,
        "lease_expires_at": None,
    }
    if hasattr(model, "error"):
        values["error"] = error
    if hasattr(model, "progress"):
        values["progress"] = {"stage": "failed", "pct": 0}
    if hasattr(model, "updated_at"):
        values["updated_at"] = now
    changed = db.query(model).filter(
        model.id == task_id,
        model.status == "running",
        model.worker_task_id == worker_task_id,
        model.lease_generation == generation,
        model.lease_expires_at > now,
    ).update(values, synchronize_session=False)
    db.commit()
    return changed == 1


def reconcile_expired_task_leases(db: Session, model: type, *, batch_size: int = 100) -> int:
    """Fail expired legacy tasks without replaying potentially paid or partial work."""
    from sqlalchemy import case

    now = datetime.now(timezone.utc)
    query = db.query(model.id, model.worker_task_id, model.lease_generation).filter(
        model.status == "running",
        model.lease_expires_at.isnot(None),
        model.lease_expires_at <= now,
    ).order_by(model.lease_expires_at.asc(), model.id.asc()).limit(max(1, min(batch_size, 500)))
    expired = query.with_for_update(skip_locked=True).all()
    reconciled = 0
    for task_id, worker_task_id, generation in expired:
        conditions = [
            model.id == task_id,
            model.status == "running",
            model.worker_task_id == worker_task_id,
            model.lease_generation == generation,
            model.lease_expires_at <= now,
        ]
        cancel_requested = getattr(model, "cancel_requested", None)
        status_value: Any = "failed"
        error_value: Any = (
            "Worker lease expired; task was stopped without automatic replay. "
            "Inspect the task and any partial output before retrying."
        )
        if cancel_requested is not None:
            status_value = case((cancel_requested.is_(True), "cancelled"), else_="failed")
            error_value = case(
                (cancel_requested.is_(True), "Worker lease expired after cancellation was requested"),
                else_=error_value,
            )
        values: dict[str, Any] = {
            "status": status_value,
            "error": error_value,
            "progress": {"stage": "worker lease expired", "pct": 0},
            "worker_task_id": None,
            "heartbeat_at": None,
            "lease_expires_at": None,
        }
        if hasattr(model, "updated_at"):
            values["updated_at"] = now
        changed = db.query(model).filter(*conditions).update(values, synchronize_session=False)
        if changed == 1:
            reconciled += 1
    db.commit()
    return reconciled


def fail_queued_task_dispatch(
    db: Session,
    model: type,
    task_id: str,
    *,
    error: str,
) -> bool:
    """Fail a task only if no worker has atomically claimed its queued row."""
    task_id_column = getattr(model, "id")
    query = db.query(model).filter(
        task_id_column == task_id,
        getattr(model, "status") == "queued",
    )
    values: dict[str, Any] = {"status": "failed"}
    if hasattr(model, "error"):
        values["error"] = error
    if hasattr(model, "progress"):
        values["progress"] = {"stage": "dispatch failed", "pct": 0}
    if hasattr(model, "updated_at"):
        values["updated_at"] = datetime.now(timezone.utc)
    failed = query.update(values, synchronize_session=False)
    db.commit()
    return failed == 1
