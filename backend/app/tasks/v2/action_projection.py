"""Retry the durable ActionRun record's pending graph projection."""
from datetime import datetime, timezone
import logging
from types import SimpleNamespace

from sqlalchemy import or_

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="v2.action_projection.reconcile", acks_late=True, reject_on_worker_lost=True)
def reconcile_action_projections(batch_size: int = 50):
    from app.database import SessionLocal
    from app.models.v2.action import OntologyActionRun
    from app.services.v2.decision_cases import (
        ACTION_PROJECTION_MAX_ATTEMPTS,
        DecisionCaseService,
    )

    now = datetime.now(timezone.utc)
    batch_size = min(max(int(batch_size), 1), 200)
    with SessionLocal() as db:
        candidates = [row[0] for row in db.query(OntologyActionRun.id).filter(
            or_(OntologyActionRun.decision_case_id.is_not(None),
                OntologyActionRun.parameters["revert_of"].as_string().is_not(None)),
            OntologyActionRun.status == "completed",
            OntologyActionRun.projection_status.in_(("pending", "failed")),
            OntologyActionRun.projection_attempts < ACTION_PROJECTION_MAX_ATTEMPTS,
            or_(OntologyActionRun.projection_next_attempt_at.is_(None),
                OntologyActionRun.projection_next_attempt_at <= now),
        ).order_by(OntologyActionRun.started_at.asc(), OntologyActionRun.id.asc())
            .limit(batch_size).all()]

    counts = {"synced": 0, "failed": 0, "conflict": 0, "skipped": 0}
    for run_id in candidates:
        with SessionLocal() as db:
            run = db.get(OntologyActionRun, run_id)
            if (run is None or run.status != "completed"
                    or run.projection_status not in {"pending", "failed"}
                    or (run.projection_attempts or 0) >= ACTION_PROJECTION_MAX_ATTEMPTS
                    or (run.projection_next_attempt_at is not None
                        and run.projection_next_attempt_at > now)):
                counts["skipped"] += 1
                continue
            # A retry worker is its own principal. It must not borrow the
            # requester's identity for projection audit events.
            service = DecisionCaseService(db, run.ontology_id,
                                          SimpleNamespace(id=None, role="system"))
            try:
                reconciled = service.sync_projection(run, actor_kind="system")
            except Exception:
                db.rollback()
                log.exception("Action projection retry failed for run %s", run_id)
                counts["failed"] += 1
                continue
            status = reconciled.projection_status or "pending"
            counts[status if status in counts else "skipped"] += 1
    return {"scanned": len(candidates), **counts}
