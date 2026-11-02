"""Isolated regression checks for legacy Celery task lease fencing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Boolean, DateTime, Integer, JSON, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.tasks.task_claims import (
    LEGACY_TASK_LEASE_SECONDS,
    TaskLeaseHeartbeat,
    TaskLeaseLost,
    claim_queued_task,
    reconcile_expired_task_leases,
)


class LeaseTestBase(DeclarativeBase):
    pass


class LeaseTask(LeaseTestBase):
    __tablename__ = "lease_test_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    progress: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    worker_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LeaseTestWrite(LeaseTestBase):
    __tablename__ = "lease_test_writes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


@pytest.fixture
def lease_session():
    engine = create_engine("sqlite:///:memory:")
    LeaseTestBase.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session, factory
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def postgres_lease_factory():
    """Use only a dedicated disposable database supplied by the caller."""
    url = os.getenv("LEASE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set LEASE_TEST_POSTGRES_URL to run isolated PostgreSQL lease checks")
    engine = create_engine(url, pool_size=8, max_overflow=0)
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS lease_test_probe"))
    test_engine = engine.execution_options(
        schema_translate_map={None: "lease_test_probe"}
    )
    LeaseTestBase.metadata.create_all(bind=test_engine)
    factory = sessionmaker(bind=test_engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        LeaseTestBase.metadata.drop_all(bind=test_engine)
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS lease_test_probe CASCADE"))
        engine.dispose()


def _task(task_id: str, *, status: str = "queued", **values) -> LeaseTask:
    now = datetime.now(timezone.utc)
    attributes = {
        "id": task_id,
        "status": status,
        "progress": {},
        "lease_generation": 0,
        "cancel_requested": False,
        "updated_at": now,
    }
    attributes.update(values)
    return LeaseTask(**attributes)


def test_claim_is_atomic_and_duplicate_delivery_is_noop(lease_session):
    db, _ = lease_session
    db.add(_task("claim-me"))
    db.commit()

    claimed = claim_queued_task(
        db, LeaseTask, "claim-me", progress={"stage": "start"}, worker_task_id="worker-a"
    )
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.worker_task_id == "worker-a"
    assert claimed.lease_generation == 1
    assert claimed.lease_expires_at > datetime.now(timezone.utc).replace(tzinfo=None)

    assert claim_queued_task(
        db, LeaseTask, "claim-me", progress={"stage": "duplicate"}, worker_task_id="worker-b"
    ) is None
    db.refresh(claimed)
    assert claimed.worker_task_id == "worker-a"
    assert claimed.lease_generation == 1


def test_owned_commit_renews_lease_and_persists_business_write(lease_session):
    db, _ = lease_session
    db.add(_task("live"))
    db.commit()
    claimed = claim_queued_task(
        db, LeaseTask, "live", progress={"stage": "start"}, worker_task_id="worker-a"
    )
    original_expiry = claimed.lease_expires_at
    lease = TaskLeaseHeartbeat(db, LeaseTask, "live", "worker-a", 1).start()
    try:
        db.add(LeaseTestWrite(value="accepted"))
        db.commit()
        db.refresh(claimed)
        assert claimed.heartbeat_at is not None
        assert claimed.lease_expires_at > original_expiry
        assert db.query(LeaseTestWrite).count() == 1
        assert (claimed.lease_expires_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() <= LEGACY_TASK_LEASE_SECONDS
    finally:
        lease.close()


def test_stale_owner_commit_is_fenced_from_business_writes(lease_session):
    db, _ = lease_session
    db.add(_task(
        "reassigned",
        status="running",
        worker_task_id="worker-new",
        lease_generation=2,
        heartbeat_at=datetime.now(timezone.utc),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=LEGACY_TASK_LEASE_SECONDS),
    ))
    db.commit()
    stale_lease = TaskLeaseHeartbeat(db, LeaseTask, "reassigned", "worker-old", 1).start()
    try:
        db.add(LeaseTestWrite(value="must-not-commit"))
        with pytest.raises(TaskLeaseLost):
            db.commit()
        db.rollback()
        assert db.query(LeaseTestWrite).count() == 0
    finally:
        stale_lease.close()


@pytest.mark.parametrize(
    ("cancel_requested", "expected_status"),
    [(False, "failed"), (True, "cancelled")],
)
def test_expired_lease_is_terminal_and_never_reclaimed(lease_session, cancel_requested, expected_status):
    db, _ = lease_session
    db.add(_task(
        "expired",
        status="running",
        worker_task_id="worker-dead",
        lease_generation=3,
        heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=3),
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        cancel_requested=cancel_requested,
    ))
    db.commit()

    assert reconcile_expired_task_leases(db, LeaseTask) == 1
    task = db.query(LeaseTask).filter_by(id="expired").one()
    assert task.status == expected_status
    assert task.worker_task_id is None
    assert task.lease_expires_at is None
    assert reconcile_expired_task_leases(db, LeaseTask) == 0
    assert claim_queued_task(
        db, LeaseTask, "expired", progress={}, worker_task_id="worker-replay"
    ) is None


def test_postgres_concurrent_deliveries_have_one_claim_winner(postgres_lease_factory):
    factory = postgres_lease_factory
    with factory() as db:
        db.add(_task("pg-race"))
        db.commit()

    start = Barrier(2)

    def claim(worker_id: str):
        with factory() as db:
            start.wait(timeout=5)
            task = claim_queued_task(
                db, LeaseTask, "pg-race", progress={"stage": "start"}, worker_task_id=worker_id
            )
            return task.worker_task_id if task else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))
    assert sum(result is not None for result in results) == 1
    with factory() as db:
        task = db.query(LeaseTask).filter_by(id="pg-race").one()
        assert task.status == "running"
        assert task.worker_task_id in {"worker-a", "worker-b"}
        assert task.lease_generation == 1


def test_postgres_stale_worker_transaction_is_rolled_back(postgres_lease_factory):
    factory = postgres_lease_factory
    with factory() as db:
        db.add(_task(
            "pg-fenced",
            status="running",
            worker_task_id="worker-current",
            lease_generation=2,
            heartbeat_at=datetime.now(timezone.utc),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=LEGACY_TASK_LEASE_SECONDS),
        ))
        db.commit()

    with factory() as db:
        stale_lease = TaskLeaseHeartbeat(db, LeaseTask, "pg-fenced", "worker-stale", 1).start()
        try:
            db.add(LeaseTestWrite(value="must-rollback"))
            with pytest.raises(TaskLeaseLost):
                db.commit()
            db.rollback()
        finally:
            stale_lease.close()

    with factory() as db:
        assert db.query(LeaseTestWrite).count() == 0
        task = db.query(LeaseTask).filter_by(id="pg-fenced").one()
        assert task.status == "running"
        assert task.worker_task_id == "worker-current"
        assert task.lease_generation == 2


def test_postgres_concurrent_reconciliation_transitions_expired_row_once(postgres_lease_factory):
    factory = postgres_lease_factory
    with factory() as db:
        db.add(_task(
            "pg-expired",
            status="running",
            worker_task_id="worker-dead",
            lease_generation=4,
            heartbeat_at=datetime.now(timezone.utc) - timedelta(minutes=3),
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))
        db.commit()

    start = Barrier(2)

    def reconcile():
        with factory() as db:
            start.wait(timeout=5)
            return reconcile_expired_task_leases(db, LeaseTask)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reconcile(), range(2)))
    assert sum(results) == 1
    with factory() as db:
        task = db.query(LeaseTask).filter_by(id="pg-expired").one()
        assert task.status == "failed"
        assert task.worker_task_id is None
        assert task.lease_expires_at is None


def test_celery_child_crash_is_reconciled_without_replay(postgres_lease_factory):
    """Kill a real prefork task child against disposable PostgreSQL and Redis."""
    postgres_url = os.getenv("LEASE_TEST_POSTGRES_URL")
    redis_url = os.getenv("LEASE_TEST_REDIS_URL")
    if not postgres_url or not redis_url:
        pytest.skip("set LEASE_TEST_POSTGRES_URL and LEASE_TEST_REDIS_URL for worker crash check")

    from app.database import SessionLocal
    from app.models.audit_task import AuditTask
    from app.models.ontology import OntologyProject
    from app.models.user import User
    from app.tasks.celery_app import celery_app
    from app.tasks.legacy_task_recovery import reconcile_expired_legacy_tasks

    task_id = str(uuid4())
    user_id = str(uuid4())
    ontology_id = str(uuid4())
    worker_name = f"lease-crash-{uuid4().hex[:8]}@%h"
    worker = None
    worker_log = tempfile.TemporaryFile()

    with SessionLocal() as db:
        db.add(User(
            id=user_id,
            username=f"lease-crash-{user_id[:8]}",
            email=f"{user_id}@lease-test.invalid",
            password_hash="unused-test-hash",
            role="admin",
        ))
        db.flush()
        db.add(OntologyProject(
            id=ontology_id,
            name="Lease crash probe",
            domain="test",
            created_by=user_id,
        ))
        db.flush()
        db.add(AuditTask(
            id=task_id,
            ontology_id=ontology_id,
            model_name="test-only",
            status="queued",
            progress={},
            cancel_requested=False,
        ))
        db.commit()

    worker_env = os.environ.copy()
    worker_env["DATABASE_URL"] = postgres_url
    worker_env["REDIS_URL"] = redis_url
    worker_env["PYTHONPATH"] = os.pathsep.join(
        ["/app/tests", "/app", worker_env.get("PYTHONPATH", "")]
    )
    command = [
        sys.executable, "-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker",
        "--pool=solo", "--concurrency=1", "--queues=lease_crash_test",
        f"--hostname={worker_name}", "--loglevel=INFO", "--include=legacy_lease_worker_probe",
        "--without-gossip", "--without-mingle",
    ]
    worker = subprocess.Popen(
        command,
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=worker_env,
        stdout=worker_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if worker.poll() is not None:
                worker_log.flush()
                worker_log.seek(0)
                logs = worker_log.read().decode(errors="replace")[-8000:]
                pytest.fail(f"Celery worker exited before becoming ready: {worker.returncode}; log={logs}")
            try:
                replies = celery_app.control.inspect(timeout=1).ping() or {}
            except Exception:
                replies = {}
            if replies:
                break
            time.sleep(0.2)
        else:
            pytest.fail("isolated Celery worker did not become ready")

        celery_app.send_task(
            "tests.legacy_lease_crash_probe",
            args=[task_id],
            task_id=task_id,
            queue="lease_crash_test",
        )

        def claimed_and_marked() -> bool:
            with SessionLocal() as db:
                task = db.query(AuditTask).filter_by(id=task_id).one()
                marker_count = db.execute(
                    text("SELECT count(*) FROM lease_test_probe.lease_test_writes WHERE value=:value"),
                    {"value": f"claimed:{task_id}"},
                ).scalar_one()
                return task.status == "running" and bool(task.worker_task_id) and marker_count == 1

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not claimed_and_marked():
            if worker.poll() is not None:
                pytest.fail(f"Celery worker exited while processing probe: {worker.returncode}")
            time.sleep(0.1)
        if not claimed_and_marked():
            worker_log.flush()
            worker_log.seek(0)
            logs = worker_log.read().decode(errors="replace")[-8000:]
            with SessionLocal() as db:
                task = db.query(AuditTask).filter_by(id=task_id).one()
                status = (task.status, task.worker_task_id, task.error)
            pytest.fail(f"worker did not claim and commit the probe marker; task={status}; log={logs}")

        # Kill the isolated worker process itself. The DB and Redis endpoints
        # are disposable; no live demo process or task is targeted.
        os.killpg(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)
        assert worker.returncode == -signal.SIGKILL

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                task = db.query(AuditTask).filter_by(id=task_id).one()
                expiry = task.lease_expires_at
                if expiry is not None and expiry <= datetime.now(timezone.utc):
                    break
            time.sleep(0.2)
        else:
            pytest.fail("task lease kept renewing after the Celery child was killed")

        recovered = reconcile_expired_legacy_tasks.apply(kwargs={"batch_size": 100}, throw=True).get()
        assert recovered == {"audits": 1, "extractions": 0}

        with SessionLocal() as db:
            task = db.query(AuditTask).filter_by(id=task_id).one()
            assert task.status == "failed"
            assert task.worker_task_id is None
            assert task.lease_expires_at is None
            marker_count = db.execute(
                text("SELECT count(*) FROM lease_test_probe.lease_test_writes WHERE value=:value"),
                {"value": f"claimed:{task_id}"},
            ).scalar_one()
            assert marker_count == 1
    finally:
        if worker is not None and worker.poll() is None:
            try:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                worker.wait(timeout=5)
        with SessionLocal() as db:
            db.query(AuditTask).filter_by(id=task_id).delete(synchronize_session=False)
            db.query(OntologyProject).filter_by(id=ontology_id).delete(synchronize_session=False)
            db.query(User).filter_by(id=user_id).delete(synchronize_session=False)
            db.commit()
        worker_log.close()
