"""Celery entry points for durable temporal vector index builds."""
from __future__ import annotations

from app.tasks.celery_app import celery_app


@celery_app.task(name="v2.temporal_vector_index")
def run_temporal_vector_index_task(job_id: str) -> dict:
    from app.services.v2.temporal_vector_index import build_temporal_vector_index

    return build_temporal_vector_index(job_id)


@celery_app.task(name="v2.temporal_vector_index.reconcile")
def reconcile_temporal_vector_index_task(batch_size: int = 50) -> dict:
    from app.services.v2.temporal_vector_index import reconcile_temporal_vector_index_jobs

    return reconcile_temporal_vector_index_jobs(batch_size=batch_size)


@celery_app.task(name="v2.temporal_vector_index.retention")
def prune_temporal_vector_index_retention_task(batch_size: int = 50) -> dict:
    from app.services.v2.temporal_vector_index import prune_temporal_vector_index_generations

    return prune_temporal_vector_index_generations(batch_size=batch_size)
