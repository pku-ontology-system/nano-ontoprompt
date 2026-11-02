"""Converge expired legacy Audit and Extraction worker leases."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.tasks.celery_app import celery_app


log = logging.getLogger(__name__)


@celery_app.task(
    name="legacy_tasks.reconcile",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_expired_legacy_tasks(batch_size: int = 100) -> dict[str, int]:
    """Make abandoned tasks visible without automatically repeating LLM work."""
    import app.models  # noqa: F401
    from app.database import SessionLocal
    from app.models.audit_task import AuditTask
    from app.models.extraction_task import ExtractionTask
    from app.models.v2.workbench_task import ModelInvocation
    from app.tasks.task_claims import reconcile_expired_task_leases

    batch_size = max(1, min(int(batch_size), 500))
    with SessionLocal() as db:
        audits = reconcile_expired_task_leases(db, AuditTask, batch_size=batch_size)
        extractions = reconcile_expired_task_leases(db, ExtractionTask, batch_size=batch_size)

        expired_audit_ids = db.query(AuditTask.id).filter(
            AuditTask.status.in_(("failed", "cancelled")),
            AuditTask.error.like("Worker lease expired%"),
        ).subquery()
        db.query(ModelInvocation).filter(
            ModelInvocation.audit_task_id.in_(expired_audit_ids),
            ModelInvocation.status == "running",
        ).update({
            "status": "failed",
            "error": "Audit worker lease expired before a result was committed",
            "completed_at": datetime.now(timezone.utc),
        }, synchronize_session=False)
        db.commit()

    if audits or extractions:
        log.warning("Reconciled expired legacy task leases: audits=%s extractions=%s", audits, extractions)
    return {"audits": audits, "extractions": extractions}
