"""Reconcile isolated Scenario graphs left by interrupted leased ActionRuns."""
from app.tasks.celery_app import celery_app


@celery_app.task(
    name="v2.scenario_graph_recovery.reconcile",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_scenario_graph_builds(batch_size: int = 25) -> dict:
    from app.database import SessionLocal
    from app.services.v2.action_runtime import (
        reconcile_expired_scenario_action_graph_builds,
    )

    with SessionLocal() as db:
        return reconcile_expired_scenario_action_graph_builds(
            db,
            batch_size=batch_size,
        )
