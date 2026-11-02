"""Mapping Apply 异步 Celery 任务"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def _load_mapping_source(db, mapping, dataset_version_id: str | None = None):
    from app.services.v2.dataset_service import DatasetService

    if not mapping.curated_dataset_id:
        raise ValueError("Mapping has no curated dataset")
    datasets = DatasetService(db)
    if dataset_version_id:
        version, rows, manifest = datasets.read_complete_version_snapshot(
            mapping.curated_dataset_id, dataset_version_id,
        )
    else:
        version, rows, manifest = datasets.read_latest_complete_snapshot(
            mapping.curated_dataset_id,
        )
    if not rows:
        raise ValueError(f"Dataset version {version.id} has no rows; mapping was not applied")
    return version, rows, manifest


def apply_approved_mapping(db, mapping_id: str, ontology_id: str,
                           dataset_version_id: str | None,
                           review_id: str | None = None,
                           mapping_review_id: str | None = None,
                           expected_mapping_digest: str | None = None,
                           mapping_apply_run_id: str | None = None):
    """Apply only a dataset snapshot covered by an approved review."""
    from app.services.v2.mapping.mapping_service import MappingService
    from app.models.v2.mapping import OntologyMapping
    from app.models.v2.curated import CuratedDataset
    from app.models.v2.curated import CuratedReview
    from app.models.entity_instance import EntityInstance
    from app.models.v2.dataset import DatasetVersion
    from app.models.v2.mapping_apply_run import MappingApplyRun
    from app.services.v2.mapping_definition_reviews import approved_mapping_review_for_run

    mapping_query = db.query(OntologyMapping).filter(
        OntologyMapping.id == mapping_id,
        OntologyMapping.ontology_id == ontology_id,
    )
    try:
        if db.get_bind().dialect.name == "postgresql":
            mapping_query = mapping_query.with_for_update()
    except Exception:
        pass
    mapping = mapping_query.first()
    if not mapping:
        raise ValueError(f"Mapping {mapping_id} not found in Ontology {ontology_id}")
    if mapping.status == "disabled":
        raise ValueError("Mapping was disabled after the approved run was enqueued")
    if expected_mapping_digest:
        from app.services.v2.mapping_apply_runs import mapping_definition_snapshot

        _, current_mapping_digest = mapping_definition_snapshot(mapping)
        if current_mapping_digest != expected_mapping_digest:
            raise ValueError("Mapping definition changed after the approved run was enqueued")
    mapping_review = approved_mapping_review_for_run(
        db, mapping, mapping_review_id, expected_digest=expected_mapping_digest,
    )

    curated = db.query(CuratedDataset).filter(
        CuratedDataset.id == mapping.curated_dataset_id
    ).first()
    if curated is None or curated.status != "approved":
        raise ValueError("Mapping source dataset must be approved before apply")
    if not dataset_version_id:
        raise ValueError("Mapping apply requires the immutable version pinned by review")

    review_query = db.query(CuratedReview).filter(
        CuratedReview.curated_dataset_id == mapping.curated_dataset_id,
        CuratedReview.status == "approved",
        CuratedReview.dataset_version_id == dataset_version_id,
    )
    if review_id:
        review_query = review_query.filter(CuratedReview.id == review_id)
    review = review_query.order_by(CuratedReview.decided_at.desc()).first()
    if review is None:
        raise ValueError("No approved review authorizes this dataset version")
    if (not review.requested_by or not review.decided_by
            or str(review.requested_by) == str(review.decided_by)):
        raise ValueError("Mapping Apply requires approval by a reviewer different from the requester")
    if review.reviewer_id and str(review.reviewer_id) != str(review.decided_by):
        raise ValueError("Mapping Apply review was not approved by its assigned reviewer")

    if mapping_apply_run_id:
        persisted_run = db.get(MappingApplyRun, mapping_apply_run_id)
        if (persisted_run is None
                or persisted_run.mapping_id != mapping.id
                or persisted_run.review_id != review.id
                or persisted_run.mapping_review_id != mapping_review.id
                or persisted_run.mapping_digest != mapping_review.candidate_digest):
            raise ValueError("Persisted MappingApplyRun does not match both approved review pins")

    version, data, source_manifest = _load_mapping_source(db, mapping, dataset_version_id)
    if mapping_apply_run_id:
        applied_run_version = db.query(DatasetVersion.version_no).join(
            MappingApplyRun,
            MappingApplyRun.dataset_version_id == DatasetVersion.id,
        ).filter(
            MappingApplyRun.mapping_id == mapping.id,
            MappingApplyRun.status == "succeeded",
        ).order_by(DatasetVersion.version_no.desc()).first()
        applied_instance_version = db.query(DatasetVersion.version_no).join(
            EntityInstance,
            EntityInstance.source_version == DatasetVersion.id,
        ).filter(
            EntityInstance.source_mapping_id == mapping.id,
        ).order_by(DatasetVersion.version_no.desc()).first()
        latest_applied_version_no = max(
            [int(item[0]) for item in (applied_run_version, applied_instance_version) if item]
            or [0]
        )
        if int(version.version_no or 0) < latest_applied_version_no:
            raise ValueError(
                f"STALE_MAPPING_SNAPSHOT: version {version.version_no} is older than "
                f"the latest applied version {latest_applied_version_no}"
            )
    from app.services.v2.curated.review_service import ReviewService
    svc = MappingService(db)
    row_identity_column = (mapping.field_mapping or {}).get("__primary_key__")
    if not row_identity_column:
        row_identity_column = svc._choose_pk_col(data)
    data = ReviewService(db).apply_edits_to_snapshot(
        review.id,
        data,
        row_identity_column=str(row_identity_column) if row_identity_column else None,
        expected_dataset_version_id=version.id,
    )

    apply_options = {"source_version_id": version.id, "strict_source_schema": True}
    if mapping_apply_run_id:
        apply_options["mapping_apply_run_id"] = mapping_apply_run_id
    result = svc.apply_mapping(mapping_id, data, **apply_options)
    result["review_id"] = review.id
    result["source_manifest"] = source_manifest
    logger.info(f"Mapping applied: {result}")
    return result


def execute_mapping_apply_run(
    run_id: str,
    *,
    celery_task_id: str | None = None,
    worker_actor_id: str | None = None,
) -> dict:
    from app.database import SessionLocal
    from app.services.v2.mapping_apply_runs import MappingApplyRunService

    with SessionLocal() as db:
        return MappingApplyRunService(db).execute(
            run_id,
            celery_task_id=celery_task_id,
            worker_actor_id=worker_actor_id,
        )


def _reconcile_mapping_apply_runs(batch_size: int = 50) -> dict:
    from app.database import SessionLocal
    from app.services.v2.mapping_apply_runs import MappingApplyRunService

    with SessionLocal() as db:
        return MappingApplyRunService(db).recover_and_dispatch(batch_size=batch_size)


def _reconcile_mapping_row_projection(batch_size: int = 100) -> dict:
    from app.database import SessionLocal
    from app.services.v2.mapping_row_projection import project_mapping_row_changes

    with SessionLocal() as db:
        return project_mapping_row_changes(db, batch_size=batch_size)


from app.tasks.celery_app import celery_app  # noqa: E402


@celery_app.task(
    name="v2.mapping_apply",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def mapping_apply_run_task(self, run_id: str):
    """Execute only a persisted MappingApplyRun row."""
    return execute_mapping_apply_run(
        run_id,
        celery_task_id=self.request.id,
        worker_actor_id=getattr(self.request, "hostname", None),
    )


@celery_app.task(
    name="v2.mapping_apply.reconcile",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_mapping_apply_runs(batch_size: int = 50):
    """Recover unpublished, lost, and expired Mapping Apply run leases."""
    return _reconcile_mapping_apply_runs(batch_size=batch_size)


@celery_app.task(
    name="v2.mapping_rows.reconcile",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_mapping_row_projection(batch_size: int = 100):
    """Retry durable Ontology Mapping row changes not yet projected to FalkorDB."""
    return _reconcile_mapping_row_projection(batch_size=batch_size)
