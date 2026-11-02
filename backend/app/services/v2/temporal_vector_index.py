"""Durable, replay-pinned background builds for large temporal vector indexes."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models.v2.dynamic_ontology import WhatIfRun, WhatIfScenario
from app.models.v2.temporal_replay import TemporalFact, TemporalReplay
from app.models.v2.temporal_vector_index import TemporalVectorIndexJob, TemporalVectorIndexRetentionEvent
from app.services.v2.vector.chroma_service import (
    ChromaService,
    TEMPORAL_INDEX_BATCH_SIZE,
    temporal_replay_index_source_revision,
)

logger = logging.getLogger(__name__)

DISPATCH_LEASE_SECONDS = 90
RUN_LEASE_SECONDS = 180
QUEUED_REDELIVERY_SECONDS = 180
MAX_DISPATCH_ATTEMPTS = 12
MAX_EXECUTION_ATTEMPTS = 5
TEMPORAL_INDEX_RETENTION_DAYS = 30
TEMPORAL_INDEX_KEEP_PER_REPLAY = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def serialize_temporal_vector_index_job(job: TemporalVectorIndexJob) -> dict:
    safe_error_messages = {
        "dispatch_attempts_exhausted": "后台任务队列暂不可用，请稍后重试。",
        "execution_attempts_exhausted": "索引构建多次失败，请稍后重试。",
        "stale_source_revision": "回放在索引期间发生变化，请重新提交检索。",
        "replay_missing": "对应的时序回放已不可用。",
        "index_unavailable": "向量索引服务暂不可用，请稍后重试。",
        "index_write_failed": "向量索引写入失败，请稍后重试。",
        "index_finalize_failed": "向量索引未能发布，请稍后重试。",
        "index_build_failed": "向量索引构建失败，请稍后重试。",
        "worker_lease_expired": "工作进程未及时续租，系统将从上次检查点恢复。",
        "retention_pruned": "历史索引已按保留策略清理；需要时可重新构建。",
    }


def serialize_temporal_vector_index_retention_event(event: TemporalVectorIndexRetentionEvent) -> dict:
    """Expose only the bounded retention facts needed to review a prune."""
    details = event.details if isinstance(event.details, dict) else {}
    return {
        "id": event.id,
        "job_id": event.job_id,
        "ontology_id": event.ontology_id,
        "replay_id": event.replay_id,
        "source_revision": event.source_revision,
        "event_type": event.event_type,
        "reason": event.reason,
        "details": {
            key: details[key]
            for key in ("retention_days", "keep_per_replay", "generation_age_days", "indexed_facts")
            if key in details
        },
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
    return {
        "id": job.id,
        "ontology_id": job.ontology_id,
        "replay_id": job.replay_id,
        "source_revision": job.source_revision,
        "status": job.status,
        "total_facts": int(job.total_facts or 0),
        "indexed_facts": int(job.indexed_facts or 0),
        "error_code": job.error_code,
        "error_message": safe_error_messages.get(job.error_code or "") if job.status in {"failed", "pruned"} else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def request_temporal_vector_index(db, replay: TemporalReplay, requested_by: str | None) -> TemporalVectorIndexJob:
    """Create/reuse one durable job for the current immutable replay revision."""
    revision = temporal_replay_index_source_revision(replay)
    job = db.query(TemporalVectorIndexJob).filter(
        TemporalVectorIndexJob.replay_id == replay.id,
        TemporalVectorIndexJob.source_revision == revision,
    ).with_for_update().first()
    now = _now()
    if job is None:
        total = int(db.query(func.count(TemporalFact.id)).filter(TemporalFact.replay_id == replay.id).scalar() or 0)
        job = TemporalVectorIndexJob(
            id=str(uuid.uuid4()),
            ontology_id=replay.ontology_id,
            replay_id=replay.id,
            source_revision=revision,
            requested_by=requested_by,
            status="queued",
            total_facts=total,
            indexed_facts=0,
        )
        db.add(job)
        try:
            db.commit()
        except Exception:
            db.rollback()
            job = db.query(TemporalVectorIndexJob).filter(
                TemporalVectorIndexJob.replay_id == replay.id,
                TemporalVectorIndexJob.source_revision == revision,
            ).with_for_update().first()
            if job is None:
                raise
    else:
        status_lease = _aware(job.run_lease_until)
        dispatch_lease = _aware(job.dispatch_lease_until)
        retry_at = _aware(job.next_attempt_at)
        if job.status == "completed":
            if ChromaService().temporal_index_revision(replay.id, revision) == revision:
                return job
            job.status = "queued"
            job.indexed_facts = 0
            job.last_fact_id = None
            job.completed_at = None
        elif job.status in {"failed", "pruned"}:
            job.status = "queued"
            job.indexed_facts = 0
            job.last_fact_id = None
            job.execution_attempts = 0
            job.dispatch_attempts = 0
            job.completed_at = None
        elif job.status == "running" and status_lease and status_lease > now:
            return job
        elif job.status == "queued" and (
            (dispatch_lease and dispatch_lease > now) or (retry_at and retry_at > now)
        ):
            return job
        job.requested_by = job.requested_by or requested_by
        job.next_attempt_at = None
        job.dispatch_lease_until = None
        job.run_lease_until = None
        job.worker_token = None
        job.error_code = None
        job.error_message = None
        db.commit()
    dispatch_temporal_vector_index_job(job.id)
    db.refresh(job)
    return job


def record_temporal_vector_index_ready(
    db,
    replay: TemporalReplay,
    fact_count: int,
    requested_by: str | None = None,
) -> TemporalVectorIndexJob:
    """Register a synchronous index build in the same retention ledger as worker builds."""
    revision = temporal_replay_index_source_revision(replay)
    job = db.query(TemporalVectorIndexJob).filter(
        TemporalVectorIndexJob.replay_id == replay.id,
        TemporalVectorIndexJob.source_revision == revision,
    ).with_for_update().first()
    now = _now()
    if job is None:
        job = TemporalVectorIndexJob(
            id=str(uuid.uuid4()),
            ontology_id=replay.ontology_id,
            replay_id=replay.id,
            source_revision=revision,
            requested_by=requested_by,
            status="completed",
            total_facts=max(0, int(fact_count)),
            indexed_facts=max(0, int(fact_count)),
            completed_at=now,
        )
        db.add(job)
        try:
            db.commit()
            db.refresh(job)
            return job
        except IntegrityError:
            db.rollback()
            job = db.query(TemporalVectorIndexJob).filter(
                TemporalVectorIndexJob.replay_id == replay.id,
                TemporalVectorIndexJob.source_revision == revision,
            ).with_for_update().first()
            if job is None:
                raise
    if job.status in {"queued", "running", "completed"}:
        return job
    job.status = "completed"
    job.total_facts = max(0, int(fact_count))
    job.indexed_facts = max(0, int(fact_count))
    job.completed_at = now
    job.worker_token = None
    job.run_lease_until = None
    job.next_attempt_at = None
    job.error_code = None
    job.error_message = None
    job.requested_by = job.requested_by or requested_by
    db.commit()
    db.refresh(job)
    return job


def _vector_index_pins(value: object) -> set[tuple[str, str]]:
    """Find replay/revision pins in saved What-if manifests without trusting their shape."""
    pins: set[tuple[str, str]] = set()
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            replay_id = current.get("replay_id")
            revision = current.get("vector_index_revision")
            if isinstance(replay_id, str) and replay_id and isinstance(revision, str) and revision:
                pins.add((replay_id, revision))
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return pins


def _saved_what_if_vector_pins(db, ontology_ids: set[str]) -> set[tuple[str, str]]:
    if not ontology_ids:
        return set()
    pins: set[tuple[str, str]] = set()
    for (baseline,) in db.query(WhatIfScenario.baseline_json).filter(
        WhatIfScenario.ontology_id.in_(ontology_ids)
    ).yield_per(200):
        pins.update(_vector_index_pins(baseline))
    for (context,) in db.query(WhatIfRun.context_json).filter(
        WhatIfRun.ontology_id.in_(ontology_ids)
    ).yield_per(200):
        pins.update(_vector_index_pins(context))
    return pins


def _register_discovered_generation(db, replay: TemporalReplay, generation: dict) -> None:
    revision = str(generation.get("source_revision") or "")
    if not revision or str(generation.get("replay_id") or "") != str(replay.id):
        return
    job = db.query(TemporalVectorIndexJob).filter(
        TemporalVectorIndexJob.replay_id == replay.id,
        TemporalVectorIndexJob.source_revision == revision,
    ).with_for_update().first()
    if job is not None and job.status in {"queued", "running", "completed"}:
        return
    created_at = now = _now()
    raw_created_at = generation.get("created_at")
    if isinstance(raw_created_at, str) and raw_created_at:
        try:
            parsed = datetime.fromisoformat(raw_created_at.replace("Z", "+00:00"))
            created_at = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            # Older Chroma collections did not record creation time; give them a fresh grace period.
            created_at = now
    fact_count = max(0, int(generation.get("fact_count") or 0))
    if job is None:
        job = TemporalVectorIndexJob(
            id=str(uuid.uuid4()),
            ontology_id=replay.ontology_id,
            replay_id=replay.id,
            source_revision=revision,
            status="completed",
            total_facts=fact_count,
            indexed_facts=fact_count,
            created_at=created_at,
            updated_at=now,
            completed_at=created_at,
        )
        db.add(job)
    else:
        job.status = "completed"
        job.total_facts = fact_count
        job.indexed_facts = fact_count
        job.created_at = created_at
        job.completed_at = created_at
        job.updated_at = now
        job.error_code = None
        job.error_message = None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def dispatch_temporal_vector_index_job(job_id: str) -> dict:
    """Publish queued work with a persisted lease and deterministic task id."""
    now = _now()
    with SessionLocal() as db:
        job = db.query(TemporalVectorIndexJob).filter(
            TemporalVectorIndexJob.id == job_id,
        ).with_for_update().first()
        if not job:
            return {"job_id": job_id, "state": "missing"}
        if job.status != "queued":
            return {"job_id": job_id, "state": job.status}
        lease_until = _aware(job.dispatch_lease_until)
        retry_at = _aware(job.next_attempt_at)
        if (lease_until and lease_until > now) or (retry_at and retry_at > now):
            return {"job_id": job_id, "state": "queued"}
        if job.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS:
            job.status = "failed"
            job.error_code = "dispatch_attempts_exhausted"
            job.error_message = None
            job.completed_at = now
            db.commit()
            return {"job_id": job_id, "state": "failed"}
        job.dispatch_attempts += 1
        attempt = job.dispatch_attempts
        task_id = f"temporal-vector-index-{job.id}-{attempt}"
        job.celery_task_id = task_id
        job.dispatch_lease_until = now + timedelta(seconds=DISPATCH_LEASE_SECONDS)
        job.error_code = None
        job.error_message = None
        db.commit()

    try:
        from app.tasks.v2.temporal_vector_index import run_temporal_vector_index_task

        run_temporal_vector_index_task.apply_async(args=[job_id], task_id=task_id)
    except Exception as exc:
        with SessionLocal() as db:
            job = db.query(TemporalVectorIndexJob).filter(
                TemporalVectorIndexJob.id == job_id,
            ).with_for_update().first()
            if job and job.status == "queued" and job.celery_task_id == task_id:
                now = _now()
                job.dispatch_lease_until = None
                job.error_code = "dispatch_failed"
                job.error_message = None
                if job.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS:
                    job.status = "failed"
                    job.error_code = "dispatch_attempts_exhausted"
                    job.completed_at = now
                else:
                    job.next_attempt_at = now + timedelta(
                        seconds=min(30 * (2 ** min(job.dispatch_attempts - 1, 5)), 900)
                    )
                db.commit()
        logger.warning("Unable to dispatch temporal vector index job %s (exception=%s)", job_id, type(exc).__name__)
        return {"job_id": job_id, "state": "dispatch_failed"}

    with SessionLocal() as db:
        job = db.query(TemporalVectorIndexJob).filter(
            TemporalVectorIndexJob.id == job_id,
        ).with_for_update().first()
        if job and job.status == "queued" and job.celery_task_id == task_id:
            job.dispatch_lease_until = None
            job.next_attempt_at = _now() + timedelta(seconds=QUEUED_REDELIVERY_SECONDS)
            db.commit()
            return {"job_id": job_id, "state": "queued"}
    return {"job_id": job_id, "state": "claimed"}


def _claim_job(job_id: str) -> tuple[str | None, str | None]:
    now = _now()
    with SessionLocal() as db:
        job = db.query(TemporalVectorIndexJob).filter(
            TemporalVectorIndexJob.id == job_id,
        ).with_for_update().first()
        if not job:
            return None, "missing"
        if job.status == "completed":
            return None, "completed"
        if job.status == "failed":
            return None, "failed"
        lease_until = _aware(job.run_lease_until)
        if job.status == "running" and lease_until and lease_until > now:
            return None, "already_running"
        if job.execution_attempts >= MAX_EXECUTION_ATTEMPTS:
            job.status = "failed"
            job.error_code = "execution_attempts_exhausted"
            job.error_message = None
            job.completed_at = now
            db.commit()
            return None, "failed"
        token = str(uuid.uuid4())
        job.status = "running"
        job.execution_attempts += 1
        job.worker_token = token
        job.run_lease_until = now + timedelta(seconds=RUN_LEASE_SECONDS)
        job.dispatch_lease_until = None
        job.next_attempt_at = None
        job.error_code = None
        job.error_message = None
        db.commit()
        return token, None


def _retry_or_fail_job(job_id: str, token: str, code: str, message: str) -> str:
    now = _now()
    with SessionLocal() as db:
        job = db.query(TemporalVectorIndexJob).filter(
            TemporalVectorIndexJob.id == job_id,
        ).with_for_update().first()
        if not job or job.status != "running" or job.worker_token != token:
            return "superseded"
        job.error_code = code
        job.error_message = None
        job.worker_token = None
        job.run_lease_until = None
        if code in {"stale_source_revision", "replay_missing"}:
            job.status = "failed"
            job.completed_at = now
            result = "failed"
        elif job.execution_attempts >= MAX_EXECUTION_ATTEMPTS:
            job.status = "failed"
            job.error_code = "execution_attempts_exhausted"
            job.completed_at = now
            result = "failed"
        else:
            job.status = "queued"
            job.next_attempt_at = now + timedelta(
                seconds=min(20 * (2 ** min(job.execution_attempts - 1, 5)), 600)
            )
            result = "retry_wait"
        db.commit()
        return result


def build_temporal_vector_index(job_id: str) -> dict:
    token, stopped = _claim_job(job_id)
    if token is None:
        return {"job_id": job_id, "state": stopped or "unknown"}
    chroma = ChromaService()
    try:
        with SessionLocal() as db:
            job = db.get(TemporalVectorIndexJob, job_id)
            replay = db.get(TemporalReplay, job.replay_id) if job else None
            if not job or not replay:
                return {"job_id": job_id, "state": _retry_or_fail_job(job_id, token, "replay_missing", "Temporal replay no longer exists")}
            if temporal_replay_index_source_revision(replay) != job.source_revision:
                return {"job_id": job_id, "state": _retry_or_fail_job(job_id, token, "stale_source_revision", "Temporal replay changed before index build")}
            if chroma.temporal_index_revision(replay.id, job.source_revision) == job.source_revision:
                now = _now()
                current = db.query(TemporalVectorIndexJob).filter(
                    TemporalVectorIndexJob.id == job_id,
                    TemporalVectorIndexJob.worker_token == token,
                    TemporalVectorIndexJob.status == "running",
                ).with_for_update().first()
                if current:
                    current.status = "completed"
                    current.indexed_facts = current.total_facts
                    current.worker_token = None
                    current.run_lease_until = None
                    current.completed_at = now
                    db.commit()
                return {"job_id": job_id, "state": "completed"}
            if not chroma.begin_temporal_index_build(replay.id, job.source_revision):
                return {"job_id": job_id, "state": _retry_or_fail_job(job_id, token, "index_unavailable", "Unable to start the Chroma temporal index build")}
            total = int(db.query(func.count(TemporalFact.id)).filter(TemporalFact.replay_id == replay.id).scalar() or 0)
            job.total_facts = total
            db.commit()

        while True:
            stale_source = False
            with SessionLocal() as db:
                job = db.query(TemporalVectorIndexJob).filter(
                    TemporalVectorIndexJob.id == job_id,
                    TemporalVectorIndexJob.status == "running",
                    TemporalVectorIndexJob.worker_token == token,
                ).with_for_update().first()
                if not job:
                    return {"job_id": job_id, "state": "superseded"}
                replay = db.get(TemporalReplay, job.replay_id)
                if not replay or temporal_replay_index_source_revision(replay) != job.source_revision:
                    stale_source = True
                    rows = []
                    batch = []
                else:
                    query = db.query(TemporalFact).filter(TemporalFact.replay_id == replay.id)
                    if job.last_fact_id:
                        query = query.filter(TemporalFact.id > job.last_fact_id)
                    rows = query.order_by(TemporalFact.id.asc()).limit(TEMPORAL_INDEX_BATCH_SIZE).all()
                    if rows:
                        batch = [
                            {
                                "id": row.id,
                                "subject_id": row.subject_id,
                                "predicate": row.predicate,
                                "object_id": row.object_id,
                                "object_value": row.object_value,
                                "state_key": row.state_key,
                                "valid_from_ordinal": float(row.valid_from_ordinal),
                                "valid_to_ordinal": float(row.valid_to_ordinal) if row.valid_to_ordinal is not None else None,
                            }
                            for row in rows
                        ]
                        batch_last_id = str(rows[-1].id)
                        batch_count = len(rows)
                    else:
                        batch = []
                        batch_last_id = None
                        batch_count = 0
                    replay_id = replay.id
                    source_revision = job.source_revision
                    total = int(job.total_facts or 0)
                    if rows:
                        job.run_lease_until = _now() + timedelta(seconds=RUN_LEASE_SECONDS)
                db.commit()
            if stale_source:
                return {"job_id": job_id, "state": _retry_or_fail_job(job_id, token, "stale_source_revision", "Temporal replay changed during index build")}
            if not rows:
                break
            if not chroma.upsert_temporal_fact_batch(replay_id, batch, source_revision):
                return {"job_id": job_id, "state": _retry_or_fail_job(job_id, token, "index_write_failed", "Chroma temporal batch write failed")}
            with SessionLocal() as db:
                job = db.query(TemporalVectorIndexJob).filter(
                    TemporalVectorIndexJob.id == job_id,
                    TemporalVectorIndexJob.status == "running",
                    TemporalVectorIndexJob.worker_token == token,
                ).with_for_update().first()
                if not job:
                    return {"job_id": job_id, "state": "superseded"}
                now = _now()
                job.last_fact_id = batch_last_id
                job.indexed_facts = min(job.total_facts, int(job.indexed_facts or 0) + batch_count)
                job.run_lease_until = now + timedelta(seconds=RUN_LEASE_SECONDS)
                db.commit()

        with SessionLocal() as db:
            replay = db.get(TemporalReplay, replay_id)
            if not replay or temporal_replay_index_source_revision(replay) != source_revision:
                return {"job_id": job_id, "state": _retry_or_fail_job(job_id, token, "stale_source_revision", "Temporal replay changed before index publication")}
        if not chroma.finalize_temporal_index_build(replay_id, source_revision, total):
            return {"job_id": job_id, "state": _retry_or_fail_job(job_id, token, "index_finalize_failed", "Chroma temporal index could not be published")}
        now = _now()
        with SessionLocal() as db:
            job = db.query(TemporalVectorIndexJob).filter(
                TemporalVectorIndexJob.id == job_id,
                TemporalVectorIndexJob.status == "running",
                TemporalVectorIndexJob.worker_token == token,
            ).with_for_update().first()
            if not job:
                return {"job_id": job_id, "state": "superseded"}
            job.status = "completed"
            job.indexed_facts = job.total_facts
            job.worker_token = None
            job.run_lease_until = None
            job.next_attempt_at = None
            job.error_code = None
            job.error_message = None
            job.completed_at = now
            db.commit()
        return {"job_id": job_id, "state": "completed", "indexed_facts": total}
    except Exception as exc:
        logger.warning("Temporal vector index build failed for job %s (exception=%s)", job_id, type(exc).__name__)
        return {
            "job_id": job_id,
            "state": _retry_or_fail_job(job_id, token, "index_build_failed", "Temporal vector index build failed"),
        }


def prune_temporal_vector_index_generations(
    *,
    batch_size: int = 50,
    retention_days: int = TEMPORAL_INDEX_RETENTION_DAYS,
    keep_per_replay: int = TEMPORAL_INDEX_KEEP_PER_REPLAY,
) -> dict:
    """Prune old unpinned Chroma generations while retaining an append-only audit event."""
    batch_size = min(max(int(batch_size), 1), 200)
    retention_days = min(max(int(retention_days), 1), 3650)
    keep_per_replay = min(max(int(keep_per_replay), 1), 20)
    now = _now()
    cutoff = now - timedelta(days=retention_days)
    chroma = ChromaService()
    if not chroma.available:
        return {"state": "unavailable", "scanned": 0, "pruned": 0, "failed": 0}

    discovered = chroma.list_temporal_index_generations()
    with SessionLocal() as db:
        replay_ids = {str(item.get("replay_id") or "") for item in discovered if item.get("replay_id")}
        replays = {
            str(row.id): row
            for row in db.query(TemporalReplay).filter(TemporalReplay.id.in_(replay_ids)).all()
        } if replay_ids else {}
        for generation in discovered:
            replay = replays.get(str(generation.get("replay_id") or ""))
            if replay is not None:
                _register_discovered_generation(db, replay, generation)

        generation_rank = func.row_number().over(
            partition_by=TemporalVectorIndexJob.replay_id,
            order_by=(TemporalVectorIndexJob.completed_at.desc(), TemporalVectorIndexJob.id.desc()),
        ).label("generation_rank")
        ranked = db.query(
            TemporalVectorIndexJob.id.label("id"),
            TemporalVectorIndexJob.replay_id.label("replay_id"),
            TemporalVectorIndexJob.ontology_id.label("ontology_id"),
            TemporalVectorIndexJob.source_revision.label("source_revision"),
            TemporalVectorIndexJob.completed_at.label("completed_at"),
            generation_rank,
        ).filter(
            TemporalVectorIndexJob.status == "completed",
            TemporalVectorIndexJob.completed_at.isnot(None),
        ).subquery()
        candidate_filter = (
            ranked.c.generation_rank > keep_per_replay,
            ranked.c.completed_at <= cutoff,
        )
        ontology_ids = {
            str(ontology_id)
            for (ontology_id,) in db.query(ranked.c.ontology_id).filter(
                ranked.c.generation_rank > keep_per_replay,
                ranked.c.completed_at <= cutoff,
            ).distinct().all()
        }
        pins = _saved_what_if_vector_pins(db, ontology_ids)
        counts = {"scanned": 0, "pruned": 0, "pinned": 0, "current": 0, "failed": 0}
        page_after: tuple[datetime, str] | None = None
        while counts["pruned"] < batch_size:
            page_query = db.query(ranked).filter(*candidate_filter)
            if page_after is not None:
                page_query = page_query.filter(or_(
                    ranked.c.completed_at > page_after[0],
                    and_(ranked.c.completed_at == page_after[0], ranked.c.id > page_after[1]),
                ))
            page = page_query.order_by(
                ranked.c.completed_at.asc(), ranked.c.id.asc()
            ).limit(200).all()
            if not page:
                break

            for candidate in page:
                if counts["pruned"] >= batch_size:
                    break
                page_after = (candidate.completed_at, str(candidate.id))
                counts["scanned"] += 1
                job = db.query(TemporalVectorIndexJob).filter(
                    TemporalVectorIndexJob.id == candidate.id,
                    TemporalVectorIndexJob.status == "completed",
                    TemporalVectorIndexJob.completed_at <= cutoff,
                ).with_for_update().first()
                if job is None:
                    continue
                if (str(job.replay_id), str(job.source_revision)) in pins:
                    counts["pinned"] += 1
                    continue
                replay = db.get(TemporalReplay, job.replay_id)
                if replay is None:
                    continue
                if temporal_replay_index_source_revision(replay) == job.source_revision:
                    counts["current"] += 1
                    continue
                if not chroma.delete_temporal_index(job.replay_id, job.source_revision):
                    counts["failed"] += 1
                    continue

                completed_at = _aware(job.completed_at) or now
                job.status = "pruned"
                job.error_code = "retention_pruned"
                job.error_message = None
                job.worker_token = None
                job.run_lease_until = None
                job.dispatch_lease_until = None
                job.next_attempt_at = None
                db.add(TemporalVectorIndexRetentionEvent(
                    id=str(uuid.uuid4()),
                    job_id=job.id,
                    ontology_id=job.ontology_id,
                    replay_id=job.replay_id,
                    source_revision=job.source_revision,
                    event_type="index_pruned",
                    reason="age_and_generation_retention",
                    details={
                        "retention_days": retention_days,
                        "keep_per_replay": keep_per_replay,
                        "generation_age_days": max(0, int((now - completed_at).total_seconds() // 86400)),
                        "indexed_facts": int(job.indexed_facts or 0),
                    },
                    created_at=now,
                ))
                db.commit()
                counts["pruned"] += 1
    return {"state": "completed", **counts}


def reconcile_temporal_vector_index_jobs(batch_size: int = 50) -> dict:
    """Requeue lost deliveries and workers whose bounded lease has expired."""
    now = _now()
    batch_size = min(max(int(batch_size), 1), 200)
    with SessionLocal() as db:
        expired_running = db.query(TemporalVectorIndexJob).filter(
            TemporalVectorIndexJob.status == "running",
            or_(TemporalVectorIndexJob.run_lease_until.is_(None), TemporalVectorIndexJob.run_lease_until <= now),
        ).order_by(TemporalVectorIndexJob.updated_at.asc()).limit(batch_size).with_for_update().all()
        for job in expired_running:
            job.status = "queued"
            job.worker_token = None
            job.run_lease_until = None
            job.next_attempt_at = now
            job.error_code = "worker_lease_expired"
            job.error_message = None
        db.commit()
        due_ids = [row.id for row in db.query(TemporalVectorIndexJob.id).filter(
            TemporalVectorIndexJob.status == "queued",
            or_(TemporalVectorIndexJob.next_attempt_at.is_(None), TemporalVectorIndexJob.next_attempt_at <= now),
            or_(TemporalVectorIndexJob.dispatch_lease_until.is_(None), TemporalVectorIndexJob.dispatch_lease_until <= now),
        ).order_by(TemporalVectorIndexJob.created_at.asc(), TemporalVectorIndexJob.id.asc()).limit(batch_size).all()]
    states: dict[str, int] = {}
    for job_id in due_ids:
        state = str(dispatch_temporal_vector_index_job(job_id).get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
    return {"scanned": len(due_ids), "expired_workers": len(expired_running), "states": states}
