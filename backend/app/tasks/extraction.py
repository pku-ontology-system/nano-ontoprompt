"""Retired direct-write extraction worker."""
from app.tasks.celery_app import celery_app


@celery_app.task(bind=True)
def run_extraction(self, task_id: str):
    """Fail queued legacy tasks closed without calling a model or writing ontology rows."""
    import uuid
    import app.models  # noqa: F401 - ensure task table mappings are registered
    from app.database import SessionLocal
    from app.models.extraction_task import ExtractionTask
    from app.models.entity import Entity
    from app.models.ontology import OntologyProject
    from app.tasks.task_claims import claim_queued_task, fail_claimed_task

    db = SessionLocal()
    worker_task_id = str(getattr(self.request, "id", None) or uuid.uuid4())
    try:
        task = claim_queued_task(
            db,
            ExtractionTask,
            task_id,
            progress={"stage": "legacy route retired", "pct": 0},
            worker_task_id=worker_task_id,
        )
        if not task:
            return
        failed = fail_claimed_task(
            db,
            ExtractionTask,
            task_id,
            worker_task_id=worker_task_id,
            generation=int(task.lease_generation or 0),
            error=(
                "Legacy direct-write extraction is retired. No model call or ontology write was performed; "
                "use the versioned Construction workflow."
            ),
        )
        if failed:
            another_active_task = db.query(ExtractionTask.id).filter(
                ExtractionTask.ontology_id == task.ontology_id,
                ExtractionTask.id != task.id,
                ExtractionTask.status.in_(("queued", "running")),
            ).first()
            project = db.query(OntologyProject).filter_by(id=task.ontology_id).first()
            if not another_active_task and project and project.status == "creating":
                has_entities = db.query(Entity.id).filter_by(ontology_id=task.ontology_id).first()
                project.status = "created" if project.current_revision_id or has_entities else "draft"
                db.commit()
    finally:
        db.close()
