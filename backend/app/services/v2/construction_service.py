"""Persistence helpers for reproducible ontology construction runs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.v2.construction import ConstructionRun, EvidenceRef


_CONSTRUCTION_RUN_ERROR_MESSAGES = {
    "user_cancelled": "构建任务已取消。",
    "model_unavailable": "模型当前不可用，请检查模型配置后重试。",
    "approval_required": "构建审核或本体基线已失效，任务未发布。",
    "dataset_version_unavailable": "任务固定的数据版本不可用或已变化。",
    "dataset_empty": "数据集没有可用于构建的记录。",
    "temporal_data_invalid": "时序数据未通过有效时间或关系校验。",
    "materialization_failed": "构建结果未能按审核快照发布。",
    "construction_run_failed": "构建任务执行失败，请检查任务状态并重试。",
    "background_fallback": "后台队列不可用，任务已切换为本地执行。",
}


def construction_run_error_code(error: str | None) -> str | None:
    """Convert legacy/free-form errors into a stable, non-sensitive category."""
    if error is None or not str(error).strip():
        return None
    value = str(error).strip()
    if value in _CONSTRUCTION_RUN_ERROR_MESSAGES:
        return value
    lowered = value.casefold()
    if "取消" in value or "cancelled" in lowered or "canceled" in lowered:
        return "user_cancelled"
    if "celery unavailable" in lowered or "temporal_replay_local_fallback" in lowered:
        return "background_fallback"
    if "minimax m3" in lowered or "model_unavailable" in lowered:
        return "model_unavailable"
    if "审批" in value or "审核" in value or "本体基线" in value:
        return "approval_required"
    if (
        "NO_VALID_TEMPORAL_ROWS" in value
        or "NO_GRAPH_RELATIONS" in value
        or "TEMPORAL_REPLAY_NO_VALID_ROWS" in value
        or "TEMPORAL_REPLAY_NO_GRAPH_RELATIONS" in value
        or "时序" in value
        or "时间语义" in value
    ):
        return "temporal_data_invalid"
    if "数据集预览为空" in value or "没有可处理的媒体" in value or "没有可用版本" in value:
        return "dataset_empty"
    if "DatasetVersion" in value or "数据版本" in value or "checksum" in lowered or "schema digest" in lowered:
        return "dataset_version_unavailable"
    if "物化" in value or "发布修订" in value or "revision" in lowered:
        return "materialization_failed"
    if "dataset" in lowered or "数据集" in value:
        return "dataset_version_unavailable"
    return "construction_run_failed"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_run(run: ConstructionRun) -> dict[str, Any]:
    error_code = construction_run_error_code(run.error)
    return {
        "id": run.id,
        "run_id": run.id,
        "ontology_id": run.ontology_id,
        "dataset_id": run.dataset_id,
        "mode": run.mode,
        "status": run.status,
        "model_name": run.model_name,
        "revision_id": run.revision_id,
        "cancel_requested": bool(run.cancel_requested),
        "config": run.config or {},
        "progress": run.progress or {},
        "metrics": run.metrics or {},
        "artifact_uri": run.artifact_uri,
        # Old rows may contain provider/exception text; never expose it through
        # the Construction Run API.
        "error": _CONSTRUCTION_RUN_ERROR_MESSAGES.get(error_code) if error_code else None,
        "error_code": error_code,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "updated_at": _iso(run.updated_at),
    }


def create_run(
    db: Session,
    *,
    ontology_id: str,
    mode: str,
    dataset_id: str | None = None,
    model_name: str | None = None,
    config: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> ConstructionRun:
    run = ConstructionRun(
        id=run_id,
        ontology_id=ontology_id,
        dataset_id=dataset_id,
        mode=mode,
        status="queued",
        model_name=model_name,
        config=config or {},
        progress={"completed": 0, "total": 0},
        metrics={},
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def update_run(
    db: Session,
    run: ConstructionRun,
    *,
    status: str | None = None,
    progress: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    artifact_uri: str | None = None,
    error: str | None = None,
    cancel_requested: bool | None = None,
) -> ConstructionRun:
    if status is not None:
        run.status = status
        now = datetime.now(timezone.utc)
        if status == "running" and run.started_at is None:
            run.started_at = now
        if status in {"completed", "failed", "cancelled"}:
            run.completed_at = now
    if progress is not None:
        run.progress = progress
    if metrics is not None:
        run.metrics = metrics
    if artifact_uri is not None:
        run.artifact_uri = artifact_uri
    if error is not None:
        # Keep only the stable classification in PostgreSQL. The public
        # serializer maps it to a safe message, including for legacy rows.
        run.error = construction_run_error_code(error)
    if cancel_requested is not None:
        run.cancel_requested = cancel_requested
    run.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run


def add_evidence(
    db: Session,
    *,
    run: ConstructionRun,
    assertion_id: str,
    assertion_kind: str = "node",
    extractor: str = "rule",
    source_file: str | None = None,
    source_row: int | None = None,
    source_media_id: str | None = None,
    source_sample_id: str | None = None,
    source_dataset_version: str | None = None,
    revision_id: str | None = None,
    model_name: str | None = None,
    confidence: float | None = None,
    confidence_method: str = "not_calibrated",
    evidence_text: str | None = None,
    content: Any = None,
) -> EvidenceRef:
    digest_input = content if content is not None else evidence_text or assertion_id
    encoded = json.dumps(digest_input, sort_keys=True, default=str).encode()
    ref = EvidenceRef(
        construction_run_id=run.id,
        ontology_id=run.ontology_id,
        assertion_id=assertion_id,
        assertion_kind=assertion_kind,
        source_file=source_file,
        source_row_id=str(source_row) if source_row is not None else None,
        source_media_id=source_media_id,
        source_sample_id=source_sample_id,
        source_version=source_dataset_version,
        revision_id=revision_id or getattr(run, "revision_id", None),
        extractor=extractor,
        model_name=model_name,
        confidence=confidence,
        confidence_method=confidence_method,
        evidence_text=evidence_text,
        content_hash=hashlib.sha256(encoded).hexdigest(),
    )
    db.add(ref)
    db.commit()
    db.refresh(ref)
    return ref


def serialize_evidence(ref: EvidenceRef) -> dict[str, Any]:
    return {
        "id": ref.id,
        "construction_run_id": ref.construction_run_id,
        "ontology_id": ref.ontology_id,
        "assertion_id": ref.assertion_id,
        "assertion_kind": ref.assertion_kind,
        "source_file": ref.source_file,
        "source_row": ref.source_row_id,
        "source_media_id": ref.source_media_id,
        "source_sample_id": ref.source_sample_id,
        "revision_id": ref.revision_id,
        "source_dataset_version": ref.source_version,
        "extractor": ref.extractor,
        "model_name": ref.model_name,
        "confidence": ref.confidence,
        "confidence_method": ref.confidence_method,
        "evidence_text": ref.evidence_text,
        "content_hash": ref.content_hash,
        "created_at": _iso(ref.created_at),
    }
