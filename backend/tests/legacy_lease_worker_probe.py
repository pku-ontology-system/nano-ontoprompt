"""Test-only Celery task used to exercise lease behavior after a real child crash."""
from __future__ import annotations

import time

from sqlalchemy import text

from app.tasks.celery_app import celery_app
from app.tasks import task_claims


# Keep the worker-crash integration check bounded. These values are changed in
# this test worker process only; production task lease settings remain intact.
task_claims.LEGACY_TASK_LEASE_SECONDS = 8
task_claims.LEGACY_TASK_HEARTBEAT_SECONDS = 1


@celery_app.task(
    bind=True,
    name="tests.legacy_lease_crash_probe",
    acks_late=True,
    reject_on_worker_lost=True,
)
def legacy_lease_crash_probe(self, task_id: str):
    import app.models  # noqa: F401
    from app.database import SessionLocal
    from app.models.audit_task import AuditTask

    worker_task_id = str(getattr(self.request, "id", None) or "probe-worker")
    db = SessionLocal()
    lease = None
    try:
        task = task_claims.claim_queued_task(
            db,
            AuditTask,
            task_id,
            progress={"stage": "crash probe claimed"},
            worker_task_id=worker_task_id,
        )
        if task is None:
            return {"claimed": False}

        lease = task_claims.TaskLeaseHeartbeat(
            db, AuditTask, task_id, worker_task_id, int(task.lease_generation)
        ).start()
        # This is an isolated probe marker, not ontology/business output. The
        # test asserts that redelivery after terminal reconciliation adds none.
        db.execute(
            text("INSERT INTO lease_test_probe.lease_test_writes (value) VALUES (:value)"),
            {"value": f"claimed:{task_id}"},
        )
        db.commit()
        while True:
            time.sleep(60)
    finally:
        if lease is not None:
            lease.close()
        db.close()
