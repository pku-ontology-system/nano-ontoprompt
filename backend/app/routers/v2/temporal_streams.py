"""HTTP API for FactoryNet's event-at-a-time dynamic evolution demo."""
from __future__ import annotations

import asyncio
import json
import math
from decimal import Decimal
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_user, get_db, require_editor
from app.models.ontology import OntologyProject
from app.models.entity_instance import EntityInstance
from app.models.user import User
from app.models.v2.temporal_replay import TemporalFact, TemporalProjectionAttempt, TemporalReplay, TemporalStreamEvent
from app.models.v2.temporal_instance_reconciliation import TemporalInstanceReconciliation
from app.models.v2.temporal_vector_index import TemporalVectorIndexJob, TemporalVectorIndexRetentionEvent
from app.services.v2.temporal_stream_service import (
    StreamError,
    create_stream_run,
    dispatch_stream,
    ingest_push_event,
    publish_stream_run,
    serialize_stream,
    _serialize_fact,
    stream_facts,
    stream_graph,
    update_stream_control,
    _factorynet_observation_instance_id,
    apply_factorynet_observation_reconciliation,
    preview_factorynet_observation_reconciliation,
    authorize_temporal_event_retry,
    serialize_projection_attempt,
    temporal_graph_projection_health,
)
from app.services.v2.temporal_error_safety import temporal_error_payload
from app.services.v2.decision_cases import CaseError, DecisionCaseService
from app.services.v2.temporal_vector_index import (
    record_temporal_vector_index_ready,
    request_temporal_vector_index,
    serialize_temporal_vector_index_job,
    serialize_temporal_vector_index_retention_event,
)
from app.services.v2.temporal_graph_projection import (
    request_temporal_graph_projection_rebuild,
    serialize_temporal_graph_projection_job,
)
from app.services.v2.dataset_authorization import require_dataset_access
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError


router = APIRouter(dependencies=[Depends(get_current_user)])


class TemporalStreamCreate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    dataset_id: str | None = None
    dataset_version_id: str | None = None
    source_id: str = "factorynet_cnc"
    source_mode: str = Field(default="file_replay", pattern="^(file_replay|push|simulated_live)$")
    episode_ids: list[str] = Field(default_factory=list, max_length=200)
    # The singular form is accepted by the browser's compact form and older
    # integrations; it is folded into episode_ids below.
    episode_id: str | None = None
    series_ids: list[str] = Field(default_factory=list, max_length=200)
    start_ordinal: float | None = None
    end_ordinal: float | None = None
    start_time: float | None = None
    end_time: float | None = None
    speed: float = Field(default=1.0, gt=0, le=20)
    event_interval_ms: int | None = Field(default=None, ge=1, le=60000)
    time_column: str = Field(default="time_s", min_length=1, max_length=200)
    entity_column: str = Field(default="episode_id", min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)


class TemporalStreamControl(BaseModel):
    action: str = Field(pattern="^(start|pause|resume|step|cancel)$")
    speed: float | None = Field(default=None, gt=0, le=20)


class TemporalStreamEventBody(BaseModel):
    event_id: str = Field(min_length=1, max_length=300)
    operation: str = Field(default="upsert", pattern="^(upsert|delete)$")
    episode_id: str = Field(min_length=1, max_length=200)
    entity_key: str = Field(min_length=1, max_length=200)
    ordinal: float | None = None
    event_time: str | None = None
    source_sequence: int
    payload: dict[str, Any] = Field(default_factory=dict)
    source_ref: dict[str, Any] = Field(default_factory=dict)


class TemporalSemanticSearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    at: float | None = None
    n_results: int = Field(default=10, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("搜索词不能为空")
        return normalized

    @field_validator("at")
    @classmethod
    def require_finite_time(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("时间点必须是有限数值")
        return value


class TemporalInstanceReconciliationBody(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=120)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5000, ge=1, le=5000)


class TemporalStreamEventRetryBody(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=120)
    reason: str = Field(min_length=8, max_length=1000)

    @field_validator("idempotency_key", "reason")
    @classmethod
    def reject_blank_retry_fields(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("人工重放需要幂等键和具体原因")
        return normalized


def _require_ontology(db: Session, ontology_id: str, user: User) -> OntologyProject:
    ontology = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if ontology is None:
        raise HTTPException(404, "本体不存在")
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise HTTPException(404, "本体不存在或不可访问") from None
    return ontology


def _get_replay(db: Session, run_id: str, user: User) -> TemporalReplay:
    replay = db.query(TemporalReplay).filter(TemporalReplay.id == run_id).first()
    if not replay:
        raise HTTPException(404, "动态运行不存在")
    _require_ontology(db, replay.ontology_id, user)
    return replay


def _raise_stream(exc: StreamError) -> None:
    status_code = 409 if exc.code in {
        "SCHEMA_REVISION_CONFLICT", "LATE_EVENT_NOT_SUPPORTED", "EVENT_PAYLOAD_CONFLICT",
        "CURRENT_STATE_CONFLICT", "RUN_NOT_COMPLETED", "EVENTS_INCOMPLETE", "RUN_PUBLISHED",
        "RUN_CANCELLED", "RUN_NOT_TERMINAL", "IDEMPOTENCY_KEY_REUSED", "SOURCE_MODE_MISMATCH",
        "DATA_CLASS_MISMATCH", "EVENT_RUN_MISMATCH", "SNAPSHOT_STALE",
        "EVENT_WATERMARK_MISMATCH",
        "MANUAL_REPLAY_REQUIRED", "RUN_ALREADY_ACTIVE", "RETRY_REASON_REQUIRED",
        "INVALID_RETRY_REQUEST_KEY", "EVENT_NOT_TERMINAL_FAILED", "EVENT_NOT_FOUND",
    } else 424 if exc.code in {"STORAGE_OBJECT_MISSING", "SOURCE_NOT_READY"} else 422
    detail = temporal_error_payload(exc)
    raise HTTPException(status_code, detail=detail) from exc


@router.get("/ontologies/{ontology_id}/temporal-streams")
def list_streams(ontology_id: str, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_ontology(db, ontology_id, user)
    rows = db.query(TemporalReplay).filter(TemporalReplay.ontology_id == ontology_id).order_by(TemporalReplay.created_at.desc()).limit(limit).all()
    return {"runs": [serialize_stream(item, db=db) for item in rows], "count": len(rows)}


@router.post("/ontologies/{ontology_id}/temporal-streams", status_code=status.HTTP_201_CREATED)
def create_stream(ontology_id: str, body: TemporalStreamCreate, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _require_ontology(db, ontology_id, user)
    dataset = None
    if body.dataset_id:
        dataset = require_dataset_access(db, body.dataset_id, user)
    elif body.dataset_version_id:
        from app.models.v2.dataset import DatasetVersion
        version = db.query(DatasetVersion).filter(DatasetVersion.id == body.dataset_version_id).first()
        if not version:
            raise HTTPException(404, "数据版本不存在或不可访问")
        dataset = require_dataset_access(db, version.dataset_id, user)
    elif body.source_id == "factorynet_cnc":
        from app.services.v2.datasets.factorynet_installer import find_factorynet_dataset
        dataset = find_factorynet_dataset(db)
        if dataset:
            require_dataset_access(db, dataset, user)
    if body.dataset_version_id and dataset is not None:
        from app.models.v2.dataset import DatasetVersion
        version = db.query(DatasetVersion).filter(
            DatasetVersion.id == body.dataset_version_id,
            DatasetVersion.dataset_id == dataset.id,
        ).first()
        if not version:
            raise HTTPException(404, "数据版本不存在或不属于该数据集")
    episodes = list(body.episode_ids or body.series_ids or [])
    if body.episode_id:
        episodes = [body.episode_id] if not episodes else [*episodes, body.episode_id]
    config = dict(body.config or {})
    if body.event_interval_ms:
        config["event_interval_ms"] = body.event_interval_ms
        # ``speed`` remains the visible events/second value; the service uses
        # the explicit interval when supplied for a deterministic demo.
        config["explicit_event_interval_ms"] = body.event_interval_ms
    if body.start_ordinal is None and body.start_time is not None:
        start_ordinal = body.start_time
    else:
        start_ordinal = body.start_ordinal
    if body.end_ordinal is None and body.end_time is not None:
        end_ordinal = body.end_time
    else:
        end_ordinal = body.end_ordinal
    try:
        replay = create_stream_run(
            db, ontology_id,
            dataset_id=body.dataset_id,
            dataset_version_id=body.dataset_version_id,
            episode_ids=episodes,
            start_ordinal=start_ordinal,
            end_ordinal=end_ordinal,
            speed=body.speed,
            time_column=body.time_column,
            entity_column=body.entity_column,
            source_mode=body.source_mode,
            source_id=body.source_id,
            config=config,
            created_by=user.id,
        )
    except StreamError as exc:
        _raise_stream(exc)
    return serialize_stream(replay, db=db)


@router.get("/temporal-streams/{run_id}")
def get_stream(run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return serialize_stream(_get_replay(db, run_id, user), db=db)


@router.get("/temporal-streams/{run_id}/incidents")
def list_stream_incidents(run_id: str, limit: int = Query(50, ge=1, le=200),
                          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    replay = _get_replay(db, run_id, user)
    type_id = f"{replay.ontology_id}:type:DynamicIncident"
    rows = db.query(EntityInstance).filter_by(ontology_id=replay.ontology_id,
                                               entity_id=type_id).filter(
        EntityInstance.row_data["run_id"].as_string() == replay.id).order_by(
        EntityInstance.created_at.desc()).limit(limit).all()
    service = DecisionCaseService(db, replay.ontology_id, user)
    incidents = []
    for row in rows:
        try:
            state = service.target_state(row.id)
        except CaseError:
            continue
        incidents.append({"object_id": row.id, "object_type": state["object_type"],
                          "properties": state["properties"], "etag": state["etag"],
                          "source_version": state["source_version"]})
        if len(incidents) >= limit:
            break
    return {"run_id": replay.id, "ontology_id": replay.ontology_id,
            "incidents": incidents, "count": len(incidents)}


@router.post("/temporal-streams/{run_id}/control")
def control_stream(run_id: str, body: TemporalStreamControl, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    replay = _get_replay(db, run_id, user)
    try:
        replay, should_dispatch = update_stream_control(db, replay, body.action, body.speed)
    except StreamError as exc:
        _raise_stream(exc)
    if should_dispatch:
        dispatch_stream(replay.id)
    return serialize_stream(replay, db=db)


@router.get("/temporal-streams/{run_id}/events")
def list_events(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000), status_filter: str | None = Query(None, alias="status"), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    replay = _get_replay(db, run_id, user)
    query = db.query(TemporalStreamEvent).filter(TemporalStreamEvent.replay_id == replay.id)
    if status_filter:
        query = query.filter(TemporalStreamEvent.status == status_filter)
    total = query.count()
    rows = query.order_by(TemporalStreamEvent.ordinal.asc(), TemporalStreamEvent.source_sequence.asc()).offset(offset).limit(limit).all()
    from app.services.v2.temporal_stream_service import _serialize_event
    return {"run_id": run_id, "events": [_serialize_event(row) for row in rows], "total": total, "offset": offset, "limit": limit, "next_offset": offset + len(rows) if offset + len(rows) < total else None}


@router.get("/temporal-streams/{run_id}/attempts")
def list_stream_attempts(
    run_id: str,
    event_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    replay = _get_replay(db, run_id, user)
    query = db.query(TemporalProjectionAttempt).filter(TemporalProjectionAttempt.replay_id == replay.id)
    if event_id:
        matching_ids = db.query(TemporalStreamEvent.id).filter(
            TemporalStreamEvent.replay_id == replay.id,
            or_(TemporalStreamEvent.id == event_id, TemporalStreamEvent.event_key == event_id),
        )
        query = query.filter(TemporalProjectionAttempt.event_id.in_(matching_ids))
    total = query.count()
    rows = query.order_by(
        TemporalProjectionAttempt.created_at.desc(),
        TemporalProjectionAttempt.attempt_no.desc(),
    ).offset(offset).limit(limit).all()
    return {
        "run_id": replay.id,
        "attempts": [serialize_projection_attempt(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/temporal-streams/{run_id}/projection-health")
def get_stream_projection_health(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    replay = _get_replay(db, run_id, user)
    return temporal_graph_projection_health(db, replay)


@router.post("/temporal-streams/{run_id}/projection-rebuild", status_code=status.HTTP_202_ACCEPTED)
def rebuild_stream_graph_projection(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_editor),
):
    replay = _get_replay(db, run_id, user)
    try:
        job = request_temporal_graph_projection_rebuild(db, replay, requested_by=user.id)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=temporal_error_payload(exc, fallback_code="GRAPH_REBUILD_NOT_ALLOWED"),
        ) from exc
    return {"job": serialize_temporal_graph_projection_job(job)}


@router.post("/temporal-streams/{run_id}/events/{event_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_failed_stream_event(
    run_id: str,
    event_id: str,
    body: TemporalStreamEventRetryBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_editor),
):
    replay = _get_replay(db, run_id, user)
    try:
        attempt, idempotent = authorize_temporal_event_retry(
            db,
            replay,
            event_id,
            requested_by=user.id,
            reason=body.reason,
            request_key=body.idempotency_key,
        )
    except StreamError as exc:
        _raise_stream(exc)
    dispatch_state = dispatch_stream(replay.id)
    db.refresh(replay)
    return {
        "run_id": replay.id,
        "attempt": serialize_projection_attempt(attempt),
        "idempotent": idempotent,
        "dispatch_state": dispatch_state,
        "run": serialize_stream(replay, db=db),
    }


@router.post("/temporal-streams/{run_id}/events", status_code=status.HTTP_202_ACCEPTED)
def push_event(run_id: str, body: TemporalStreamEventBody, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    replay = _get_replay(db, run_id, user)
    try:
        event, idempotent = ingest_push_event(db, replay, body.model_dump())
    except StreamError as exc:
        _raise_stream(exc)
    from app.services.v2.temporal_stream_service import _serialize_event
    return {"event": _serialize_event(event), "idempotent": idempotent, "run": serialize_stream(replay, db=db)}


@router.get("/temporal-streams/{run_id}/facts")
def get_facts(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000), at: float | None = None, state_key: str | None = None, relation_state: str = Query("all", pattern="^(all|current)$"), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    replay = _get_replay(db, run_id, user)
    return stream_facts(db, replay, offset=offset, limit=limit, at=at, state_key=state_key, relation_state=relation_state)


@router.get("/temporal-streams/{run_id}/instances/reconciliation")
def preview_observation_instance_reconciliation(
    run_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(5000, ge=1, le=5000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Read-only count of stable Observation Object instances for one terminal replay page."""
    replay = _get_replay(db, run_id, user)
    try:
        return preview_factorynet_observation_reconciliation(db, replay, offset=offset, limit=limit)
    except StreamError as exc:
        _raise_stream(exc)


@router.post("/temporal-streams/{run_id}/instances/reconciliation")
def apply_observation_instance_reconciliation(
    run_id: str,
    body: TemporalInstanceReconciliationBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_editor),
):
    """Explicitly reconcile one page; its append-only audit row makes retries safe."""
    replay = _get_replay(db, run_id, user)
    try:
        return apply_factorynet_observation_reconciliation(
            db,
            replay,
            requested_by=user.id,
            idempotency_key=body.idempotency_key,
            offset=body.offset,
            limit=body.limit,
        )
    except StreamError as exc:
        _raise_stream(exc)


@router.get("/temporal-streams/{run_id}/instances/reconciliations")
def list_observation_instance_reconciliations(
    run_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    replay = _get_replay(db, run_id, user)
    rows = db.query(TemporalInstanceReconciliation).filter(
        TemporalInstanceReconciliation.replay_id == replay.id
    ).order_by(TemporalInstanceReconciliation.created_at.desc()).limit(limit).all()
    return {
        "run_id": replay.id,
        "reconciliations": [
            {
                "id": row.id,
                "requested_by": row.requested_by,
                "request": row.request_json,
                "result": row.result_json,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.post("/temporal-streams/{run_id}/search/semantic")
def semantic_search_temporal_stream(
    run_id: str,
    body: TemporalSemanticSearchBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Vector-recall temporal facts, then revalidate their interval in PostgreSQL."""
    replay = _get_replay(db, run_id, user)
    point = body.at
    if point is None:
        point = replay.current_time
    if point is None:
        point = float(replay.watermark_ordinal) if replay.watermark_ordinal is not None else replay.end_time
    if point is None:
        raise HTTPException(status_code=409, detail={"code": "time_context_required", "message": "该运行还没有可查询的时间水位"})
    point = float(point)
    if not math.isfinite(point):
        raise HTTPException(status_code=422, detail={"code": "invalid_time_context", "message": "时间点必须是有限数值"})

    from app.services.v2.vector.chroma_service import ChromaService, MAX_TEMPORAL_INDEX_FACTS, temporal_replay_index_source_revision

    chroma = ChromaService()
    if not chroma.available:
        raise HTTPException(status_code=503, detail={"code": "temporal_search_unavailable", "message": "时序语义检索服务暂不可用"})

    revision = temporal_replay_index_source_revision(replay)
    fact_count = int(db.query(func.count(TemporalFact.id)).filter(TemporalFact.replay_id == replay.id).scalar() or 0)
    if chroma.temporal_index_revision(replay.id, revision) != revision:
        if fact_count > MAX_TEMPORAL_INDEX_FACTS:
            job = request_temporal_vector_index(db, replay, str(user.id))
            return JSONResponse(status_code=202, content={
                "status": "indexing",
                "code": "temporal_index_building",
                "message": "该回放超过同步索引上限，已提交后台分批索引；完成后请重新检索",
                "status_path": f"/api/v2/temporal-streams/{replay.id}/vector-index-jobs/{job.id}",
                "job": serialize_temporal_vector_index_job(job),
            })
        fact_rows = db.query(TemporalFact).filter(
            TemporalFact.replay_id == replay.id
        ).order_by(TemporalFact.id.asc()).all()
        fact_payloads = [
            {
                "id": fact.id,
                "subject_id": fact.subject_id,
                "predicate": fact.predicate,
                "object_id": fact.object_id,
                "object_value": fact.object_value,
                "state_key": fact.state_key,
                "valid_from_ordinal": float(fact.valid_from_ordinal),
                "valid_to_ordinal": float(fact.valid_to_ordinal) if fact.valid_to_ordinal is not None else None,
            }
            for fact in fact_rows
        ]
        if not chroma.upsert_temporal_facts(replay.id, fact_payloads, revision):
            raise HTTPException(status_code=503, detail={"code": "temporal_index_build_failed", "message": "时序向量索引未能更新，请稍后重试"})

    db.refresh(replay)
    if (
        temporal_replay_index_source_revision(replay) != revision
        or chroma.temporal_index_revision(replay.id, revision) != revision
    ):
        raise HTTPException(status_code=409, detail={"code": "temporal_index_stale", "message": "时序数据在索引期间发生变化，请重试本次查询"})
    record_temporal_vector_index_ready(db, replay, fact_count, str(user.id))

    candidate_limit = min(100, body.n_results * 5)
    try:
        candidates = chroma.temporal_semantic_search(replay.id, body.query, point, candidate_limit, source_revision=revision)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "temporal_search_failed", "message": "时序语义检索执行失败，请稍后重试"}) from exc
    candidate_ids = [item["fact_id"] for item in candidates]
    current_facts: dict[str, TemporalFact] = {}
    if candidate_ids:
        point_decimal = Decimal(str(point))
        rows = db.query(TemporalFact).filter(
            TemporalFact.replay_id == replay.id,
            TemporalFact.id.in_(candidate_ids),
            TemporalFact.valid_from_ordinal <= point_decimal,
            or_(TemporalFact.valid_to_ordinal.is_(None), TemporalFact.valid_to_ordinal > point_decimal),
        ).all()
        current_facts = {row.id: row for row in rows}

    event_ids = {fact.source_event_id for fact in current_facts.values() if fact.source_event_id}
    events_by_id: dict[str, TemporalStreamEvent] = {}
    if event_ids:
        events_by_id = {
            event.id: event
            for event in db.query(TemporalStreamEvent).filter(TemporalStreamEvent.id.in_(event_ids)).all()
        }
    object_ref_by_subject: dict[str, str] = {}
    if replay.source_id == "factorynet_cnc":
        object_ref_by_subject = {
            f"FactoryNet:Observation:{event.episode_id}:{event.source_row_id or event.source_sequence}": _factorynet_observation_instance_id(
                replay.ontology_id,
                event.episode_id,
                event.source_row_id or event.source_sequence,
            )
            for event in events_by_id.values()
        }
    materialized_object_refs: set[str] = set()
    object_ref_ids = list(set(object_ref_by_subject.values()))
    for start in range(0, len(object_ref_ids), 1000):
        chunk = object_ref_ids[start:start + 1000]
        materialized_object_refs.update(
            row_id for (row_id,) in db.query(EntityInstance.id)
            .filter(EntityInstance.ontology_id == replay.ontology_id, EntityInstance.id.in_(chunk))
            .all()
        )

    grouped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        fact = current_facts.get(candidate["fact_id"])
        if fact is None:
            continue
        result = grouped.get(fact.subject_id)
        if result is None:
            if len(grouped) >= body.n_results:
                continue
            result = {"subject_id": fact.subject_id, "score": candidate["score"], "matched_facts": []}
            grouped[fact.subject_id] = result
        event_object_ref = object_ref_by_subject.get(fact.subject_id)
        if event_object_ref and event_object_ref in materialized_object_refs:
            result["object_ref_id"] = event_object_ref
        result["score"] = max(float(result["score"]), float(candidate["score"]))
        if len(result["matched_facts"]) < 3:
            result["matched_facts"].append(_serialize_fact(fact))

    return {
        "run_id": replay.id,
        "ontology_id": replay.ontology_id,
        "at": point,
        "time_kind": replay.time_kind,
        "watermark_ordinal": float(replay.watermark_ordinal) if replay.watermark_ordinal is not None else None,
        "published_snapshot_id": replay.published_snapshot_id,
        "index_revision": revision,
        "results": list(grouped.values()),
    }


@router.get("/temporal-streams/{run_id}/vector-index-jobs/{job_id}")
def temporal_vector_index_job_status(
    run_id: str,
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    replay = _get_replay(db, run_id, user)
    job = db.query(TemporalVectorIndexJob).filter(
        TemporalVectorIndexJob.id == job_id,
        TemporalVectorIndexJob.replay_id == replay.id,
        TemporalVectorIndexJob.ontology_id == replay.ontology_id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="时序向量索引任务不存在")
    return serialize_temporal_vector_index_job(job)


@router.get("/temporal-streams/{run_id}/vector-index-retention-events")
def temporal_vector_index_retention_events(
    run_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    replay = _get_replay(db, run_id, user)
    rows = db.query(TemporalVectorIndexRetentionEvent).filter(
        TemporalVectorIndexRetentionEvent.replay_id == replay.id,
        TemporalVectorIndexRetentionEvent.ontology_id == replay.ontology_id,
    ).order_by(
        TemporalVectorIndexRetentionEvent.created_at.desc(),
        TemporalVectorIndexRetentionEvent.id.desc(),
    ).limit(limit).all()
    return {
        "events": [serialize_temporal_vector_index_retention_event(row) for row in rows],
        "count": len(rows),
        "limit": limit,
    }


@router.get("/temporal-streams/{run_id}/graph")
def get_stream_graph(
    run_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    entity_type: str | None = None,
    episode_id: str | None = None,
    at: float | None = None,
    mode: str = Query("cumulative", pattern="^(cumulative|window)$"),
    relation_state: str = Query("all", pattern="^(all|current)$"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    replay = _get_replay(db, run_id, user)
    return stream_graph(
        db,
        replay,
        offset=offset,
        limit=limit,
        entity_type=entity_type,
        episode_id=episode_id,
        at=at,
        mode=mode,
        relation_state=relation_state,
    )


async def _event_stream(run_id: str, request: Request) -> AsyncIterator[str]:
    """Polling-to-SSE bridge with event IDs suitable for reconnects.

    The database remains authoritative, so a browser reconnect can send its
    last source sequence and receive only the committed events after that
    point.  This avoids duplicate visual commits while still working when a
    Redis/Celery broker is unavailable in a local demonstration.
    """
    try:
        last_sequence = int(request.headers.get("last-event-id") or -1)
    except (TypeError, ValueError):
        last_sequence = -1
    last_revision = ""
    for _ in range(3600):  # one hour maximum per browser connection
        if await request.is_disconnected():
            break
        db = SessionLocal()
        try:
            replay = db.query(TemporalReplay).filter(TemporalReplay.id == run_id).first()
            if not replay:
                yield f"event: error\ndata: {json.dumps({'code': 'RUN_NOT_FOUND', 'message': '动态运行不存在'}, ensure_ascii=False)}\n\n"
                break
            payload = serialize_stream(replay, db=db)
            committed_events = db.query(TemporalStreamEvent).filter(
                TemporalStreamEvent.replay_id == run_id,
                TemporalStreamEvent.status == "committed",
                TemporalStreamEvent.source_sequence > last_sequence,
            ).order_by(TemporalStreamEvent.source_sequence.asc()).all()
            from app.services.v2.temporal_stream_service import _serialize_event, _serialize_fact
            for committed_event in committed_events:
                expired_facts = db.query(TemporalFact).filter(
                    TemporalFact.replay_id == run_id,
                    TemporalFact.valid_to_ordinal == committed_event.ordinal,
                ).order_by(TemporalFact.id.asc()).all()
                packet = {
                    "event": _serialize_event(committed_event),
                    "facts_expired": [_serialize_fact(item) for item in expired_facts],
                    "run": payload,
                }
                last_sequence = int(committed_event.source_sequence)
                yield f"id: {last_sequence}\nevent: event_committed\ndata: {json.dumps(packet, ensure_ascii=False, default=str)}\n\n"
            revision = f"{payload.get('updated_at')}:{payload.get('committed_events')}:{payload.get('status')}"
            if revision != last_revision:
                last_revision = revision
                event_name = "completed" if replay.status in {"completed", "published"} else "update"
                yield f"id: {max(last_sequence, int(payload.get('committed_events', 0)) - 1)}\nevent: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            if replay.status in {"completed", "failed", "cancelled", "published"}:
                break
        finally:
            db.close()
        await asyncio.sleep(0.8)


@router.get("/temporal-streams/{run_id}/event-stream")
async def event_stream(run_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_replay(db, run_id, user)
    return StreamingResponse(_event_stream(run_id, request), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/temporal-streams/{run_id}/publish")
def publish(run_id: str, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    replay = _get_replay(db, run_id, user)
    try:
        return publish_stream_run(db, replay, created_by=user.id)
    except StreamError as exc:
        _raise_stream(exc)


__all__ = ["router"]
