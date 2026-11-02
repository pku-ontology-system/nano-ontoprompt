"""Execute and reconcile durable Link Mapping Apply runs."""
from __future__ import annotations


def _execute_mapping_link_apply(
    run_id: str, *, celery_task_id: str | None = None, worker_actor_id: str | None = None,
) -> dict:
    from app.database import SessionLocal
    from app.services.v2.mapping_link_apply_runs import MappingLinkApplyRunService

    with SessionLocal() as db:
        return MappingLinkApplyRunService(db).execute(
            run_id,
            celery_task_id=celery_task_id,
            worker_actor_id=worker_actor_id,
        )


def _reconcile_mapping_link_apply_runs(batch_size: int = 50) -> dict:
    from app.database import SessionLocal
    from app.services.v2.mapping_link_apply_runs import MappingLinkApplyRunService

    with SessionLocal() as db:
        return MappingLinkApplyRunService(db).recover_and_dispatch(batch_size=batch_size)


def _execute_mapping_link_projection_replay(
    request_id: str, *, celery_task_id: str | None = None, worker_actor_id: str | None = None,
) -> dict:
    from app.database import SessionLocal
    from app.services.v2.mapping_link_projection import execute_link_projection_replay_request

    with SessionLocal() as db:
        return execute_link_projection_replay_request(
            db,
            request_id,
            celery_task_id=celery_task_id,
            worker_actor_id=worker_actor_id,
        )


def _reconcile_mapping_link_projection_replays(batch_size: int = 50) -> dict:
    from app.database import SessionLocal
    from app.services.v2.mapping_link_projection import reconcile_link_projection_replay_requests

    with SessionLocal() as db:
        return reconcile_link_projection_replay_requests(batch_size=batch_size, db=db)


from app.tasks.celery_app import celery_app  # noqa: E402


@celery_app.task(
    name="v2.mapping_link_apply",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def mapping_link_apply_run_task(self, run_id: str):
    """Execute only a persisted and user-confirmed Link Mapping Apply run."""
    return _execute_mapping_link_apply(
        run_id,
        celery_task_id=self.request.id,
        worker_actor_id=getattr(self.request, "hostname", None),
    )


@celery_app.task(
    name="v2.mapping_link_apply.reconcile",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_mapping_link_apply_runs(batch_size: int = 50):
    """Recover unpublished, lost, and expired Link Mapping Apply runs."""
    return _reconcile_mapping_link_apply_runs(batch_size=batch_size)


@celery_app.task(
    name="v2.mapping_link_projection_replay",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def mapping_link_projection_replay_task(self, request_id: str):
    """Execute one persisted, admin-authorized relation projection replay."""
    return _execute_mapping_link_projection_replay(
        request_id,
        celery_task_id=self.request.id,
        worker_actor_id=getattr(self.request, "hostname", None),
    )


@celery_app.task(
    name="v2.mapping_link_projection_replay.reconcile",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_mapping_link_projection_replays(batch_size: int = 50):
    """Recover and redispatch expired Link projection replay requests."""
    return _reconcile_mapping_link_projection_replays(batch_size=batch_size)
