"""Celery entry points for durable SQL-to-FalkorDB projection rebuilds."""
from __future__ import annotations

from app.tasks.celery_app import celery_app


@celery_app.task(name="v2.temporal_graph_projection")
def run_temporal_graph_projection_task(job_id: str) -> dict:
    from app.services.v2.temporal_graph_projection import build_temporal_graph_projection

    return build_temporal_graph_projection(job_id)


@celery_app.task(name="v2.temporal_graph_projection.reconcile")
def reconcile_temporal_graph_projection_task(batch_size: int = 50) -> dict:
    from app.services.v2.temporal_graph_projection import reconcile_temporal_graph_projection_jobs

    return reconcile_temporal_graph_projection_jobs(batch_size=batch_size)
