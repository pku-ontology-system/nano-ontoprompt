"""Event-at-a-time FactoryNet temporal stream service.

The older :mod:`temporal_replay_service` is intentionally left in place for
compatibility with the first demonstrator.  This module is the durable stream
implementation used by the new ``动态演化`` page.  PostgreSQL records events
and temporal facts; FalkorDB receives an idempotent, per-run projection when it
is available.  No event invokes a language model and no event writes the
published ontology schema tables.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from sqlalchemy import event as sqlalchemy_event, func, or_, update
from sqlalchemy.orm import Session

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.models.ontology_revision import OntologyRevision
from app.models.relation import Relation
from app.models.v2.construction import ConstructionRun, EvidenceRef
from app.models.v2.action import OntologyActionType
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.temporal_replay import (
    DataModelSnapshot,
    TemporalFact,
    TemporalGraphProjectionJob,
    TemporalReplay,
    TemporalReplayBatch,
    TemporalStreamEvent,
    TemporalProjectionAttempt,
)
from app.models.v2.temporal_instance_reconciliation import TemporalInstanceReconciliation
from app.models.v2.semantic_core import OntologySemanticResourceVersion
from app.services.storage_service import get_storage_service
from app.services.v2.datasets.factorynet_installer import FACTORYNET_SOURCE_ID, find_factorynet_dataset
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.temporal_error_safety import temporal_error_details
from app.services.v2.temporal_service import (
    FactoryNetIncrementalState,
    TemporalConfig,
    build_factorynet_instances_incremental,
    normalize_temporal_rows,
)


STREAM_ACTIVE_STATUSES = {"created", "queued", "running", "pausing", "paused"}
STREAM_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "published"}
STREAM_CONTROL_STATUSES = STREAM_ACTIVE_STATUSES | {"completed"}
DEFAULT_SPEED = 1.0
MAX_SPEED = 20.0
MAX_GRAPH_NODES = 500
MAX_GRAPH_EDGES = 2000
TEMPORAL_EVENT_HASH_VERSION = 2
DYNAMIC_DISPOSITION_RULE_VERSION = "factorynet_velocity_residual_v1"
DYNAMIC_DISPOSITION_THRESHOLD = 0.5
STREAM_RUN_LEASE_SECONDS = 90
STREAM_HEARTBEAT_SECONDS = 20
STREAM_DISPATCH_LEASE_SECONDS = 90
STREAM_MAX_DISPATCH_ATTEMPTS = 8
STREAM_MAX_PROJECTION_ATTEMPTS = 3

_worker_lock = threading.RLock()
_active_stream_workers: set[str] = set()


class StreamError(ValueError):
    """A user-facing stream error with a stable machine-readable code."""

    def __init__(self, message: str, code: str = "STREAM_INVALID", **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _finite_number(value: Any, field: str = "ordinal") -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StreamError(f"{field} 必须是数值", "INVALID_EVENT") from exc
    if not math.isfinite(number):
        raise StreamError(f"{field} 不能是 NaN 或无穷大", "INVALID_EVENT")
    return number


def _parse_event_time(value: Any) -> datetime:
    try:
        event_time = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            raise ValueError("timezone required")
        _finite_number(event_time.timestamp(), "event_time")
        return event_time
    except (TypeError, ValueError, OverflowError) as exc:
        raise StreamError("event_time 必须是带时区的 ISO 8601 时间", "INVALID_EVENT_TIME") from exc


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _stored_contract_version(record: Any, field: str) -> int:
    value = getattr(record, field, None)
    return int(value) if value is not None else 1


def _event_source_context(replay: TemporalReplay) -> dict[str, Any]:
    config = dict(replay.config or {})
    mapping = config.get("mapping")
    return {
        "source_id": str(replay.source_id or ""),
        "source_mode": str(replay.source_mode or ""),
        "dataset_id": replay.dataset_id,
        "dataset_version_id": replay.dataset_version_id,
        "source_checksum": config.get("source_checksum"),
        "schema_revision_id": replay.schema_revision_id,
        "time_kind": str(replay.time_kind or "ordinal"),
        "mapping": mapping if isinstance(mapping, dict) else None,
    }


def temporal_event_envelope(
    replay: TemporalReplay,
    *,
    event_key: str,
    operation: str,
    episode_id: str,
    entity_key: str,
    ordinal: Any,
    source_sequence: int,
    source_row_id: str,
    payload: dict[str, Any],
    source_ref: dict[str, Any],
) -> dict[str, Any]:
    """Return the normalized event identity used by new idempotency hashes."""
    time_kind = str(replay.time_kind or "ordinal")
    try:
        decimal_ordinal = Decimal(str(ordinal))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StreamError("ordinal 必须是数值", "INVALID_EVENT") from exc
    if not decimal_ordinal.is_finite():
        raise StreamError("ordinal 必须是有限数值", "INVALID_EVENT")
    try:
        # TemporalStreamEvent.ordinal is Numeric(24, 9); hash the value that
        # the durable row can represent so a read-after-write duplicate check
        # produces the same envelope as the original insert.
        ordinal_value = format(
            decimal_ordinal.quantize(
                Decimal("0.000000001"), rounding=ROUND_HALF_UP,
            ),
            "f",
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StreamError("ordinal 超出时序事件支持的精度", "INVALID_EVENT") from exc
    reference = dict(source_ref or {})
    time_value = reference.get("event_time") if time_kind == "event_time" else ordinal_value
    return {
        "hash_version": TEMPORAL_EVENT_HASH_VERSION,
        "source": _event_source_context(replay),
        "event_id": str(event_key),
        "operation": str(operation or "upsert"),
        "episode_id": str(episode_id),
        "entity_key": str(entity_key),
        "source_sequence": int(source_sequence),
        "source_row_id": str(source_row_id or ""),
        "time": {
            "kind": time_kind,
            "value": time_value,
            "ordinal": ordinal_value,
            "timezone": reference.get("time_zone") if time_kind == "event_time" else None,
        },
        "payload_digest": _hash(payload or {}),
        "source_ref": reference,
    }


def temporal_event_envelope_hash(replay: TemporalReplay, **event_fields: Any) -> str:
    return _hash(temporal_event_envelope(replay, **event_fields))


def temporal_event_envelope_matches(
    replay: TemporalReplay,
    existing: TemporalStreamEvent,
    **incoming_fields: Any,
) -> bool:
    """Compare the complete normalized event envelope with persisted fields."""
    stored_fields = {
        "event_key": existing.event_key,
        "operation": existing.operation or "upsert",
        "episode_id": existing.episode_id,
        "entity_key": existing.entity_key,
        "ordinal": existing.ordinal,
        "source_sequence": existing.source_sequence,
        "source_row_id": existing.source_row_id or existing.event_key,
        "payload": existing.payload or {},
        "source_ref": existing.source_ref or {},
    }
    return temporal_event_envelope(replay, **stored_fields) == temporal_event_envelope(
        replay, **incoming_fields)


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def temporal_snapshot_content_digest(
    replay: TemporalReplay,
    events: list[TemporalStreamEvent],
    facts: list[TemporalFact],
    through: float | None = None,
    *,
    manifest: dict[str, Any] | None = None,
) -> str:
    """Hash the authoritative event/fact state covered by a published snapshot."""
    event_digest = [
        {
            "event_id": event.event_key,
            "operation": event.operation or "upsert",
            "episode_id": event.episode_id,
            "entity_key": event.entity_key,
            "ordinal": str(event.ordinal),
            "source_sequence": event.source_sequence,
            "source_row_id": event.source_row_id,
            "source_ref": event.source_ref or {},
            "payload_hash_version": _stored_contract_version(event, "payload_hash_version"),
            "payload_hash": event.payload_hash,
            "payload_digest": _hash(event.payload or {}),
        }
        for event in events
    ]
    fact_digest = [
        {
            "subject_id": fact.subject_id,
            "predicate": fact.predicate,
            "object_id": fact.object_id,
            "object_value": fact.object_value,
            "state_key": fact.state_key,
            "from": str(fact.valid_from_ordinal),
            "to": str(fact.valid_to_ordinal) if fact.valid_to_ordinal is not None else None,
            "status": fact.status,
        }
        for fact in facts
    ]
    content = {
        "ontology_id": replay.ontology_id,
        "source": _event_source_context(replay),
        "schema_revision_id": replay.schema_revision_id,
        "through": _float_or_none(replay.watermark_ordinal) if through is None else through,
        "events": event_digest,
        "facts": fact_digest,
    }
    if manifest is not None:
        content["manifest"] = manifest
    return _hash(content)


def temporal_snapshot_manifest(
    replay: TemporalReplay,
    *,
    watermark_ordinal: Any,
    watermark_sequence: int | None,
    event_count: int,
    node_count: int,
    edge_count: int,
    fact_count: int,
    evidence_count: int,
) -> dict[str, Any]:
    """Build the v2 snapshot manifest included in the content digest."""
    ordinal = None
    if watermark_ordinal is not None:
        try:
            ordinal = format(Decimal(str(watermark_ordinal)), "f")
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise StreamError("snapshot watermark ordinal 无效", "INVALID_SNAPSHOT_MANIFEST") from exc
    return {
        "snapshot_hash_version": 2,
        "ontology_id": replay.ontology_id,
        "schema_revision_id": replay.schema_revision_id,
        "dataset_version_id": replay.dataset_version_id,
        "watermark": {
            "ordinal": ordinal,
            "sequence": int(watermark_sequence) if watermark_sequence is not None else None,
        },
        "counts": {
            "events": int(event_count),
            "nodes": int(node_count),
            "edges": int(edge_count),
            "facts": int(fact_count),
            "evidence": int(evidence_count),
        },
    }


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _event_key(replay_id: str, event_id: str) -> str:
    return f"{replay_id}:{event_id}"


def _episode(row: dict[str, Any]) -> str:
    return str(row.get("episode_id") or row.get("_series_id") or "unknown_episode").strip()


def _source_time(row: dict[str, Any], index: int, column: str = "time_s") -> float:
    value = row.get(column)
    if value in (None, ""):
        raise StreamError(f"缺少 {column}", "INVALID_EVENT", row_index=index)
    return _finite_number(value, column)


def _row_id(row: dict[str, Any], index: int) -> str:
    value = row.get("_source_row_index", row.get("source_row_id", index))
    return str(value)


def _source_order(row: dict[str, Any], fallback: int = 0) -> tuple[int, int | str]:
    """Return a total-order key for numeric or string source row IDs."""
    value = row.get("_source_row_index", row.get("source_row_id", fallback))
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value))


def _serialize_event(event: TemporalStreamEvent) -> dict[str, Any]:
    source_ref = event.source_ref or {}
    error_code, error_message = temporal_error_details(
        event.error, fallback_code="TEMPORAL_EVENT_INVALID",
    )
    return {
        "id": event.id,
        "event_id": event.event_key,
        "event_key": event.event_key,
        "operation": event.operation or "upsert",
        "replay_id": event.replay_id,
        "episode_id": event.episode_id,
        "entity_key": event.entity_key,
        "ordinal": float(event.ordinal) if event.ordinal is not None else None,
        "source_sequence": event.source_sequence,
        "source_row_id": event.source_row_id,
        "payload": event.payload or {},
        "source_ref": source_ref,
        "event_time": source_ref.get("event_time"),
        "ingestion_time": event.ingested_at.isoformat() if event.ingested_at else None,
        "payload_hash": event.payload_hash,
        "payload_hash_version": _stored_contract_version(event, "payload_hash_version"),
        "status": event.status,
        "error": error_message,
        "error_code": error_code,
        "graph_projection_status": event.graph_projection_status or "unknown",
        "graph_projected_at": event.graph_projected_at.isoformat() if event.graph_projected_at else None,
        "retry_generation": int(event.retry_generation or 0),
        "next_attempt_at": event.next_attempt_at.isoformat() if event.next_attempt_at else None,
        "ingested_at": event.ingested_at.isoformat() if event.ingested_at else None,
        "committed_at": event.committed_at.isoformat() if event.committed_at else None,
    }


def serialize_projection_attempt(attempt: TemporalProjectionAttempt) -> dict[str, Any]:
    error_code, error_message = temporal_error_details(
        attempt.error_code or attempt.error_message,
        fallback_code="TEMPORAL_PROJECTION_FAILED",
    )
    return {
        "id": attempt.id,
        "replay_id": attempt.replay_id,
        "event_id": attempt.event_id,
        "attempt_no": attempt.attempt_no,
        "retry_generation": attempt.retry_generation,
        "attempt_type": attempt.attempt_type,
        "status": attempt.status,
        "requested_by": attempt.requested_by,
        "request_key": attempt.request_key,
        "reason": attempt.reason,
        "error_code": error_code,
        "error_message": error_message,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
        "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
        "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
    }


def _serialize_fact(fact: TemporalFact) -> dict[str, Any]:
    return {
        "id": fact.id,
        "replay_id": fact.replay_id,
        "subject_id": fact.subject_id,
        "predicate": fact.predicate,
        "object_id": fact.object_id,
        "object_value": fact.object_value,
        "state_key": fact.state_key or None,
        "valid_from_ordinal": float(fact.valid_from_ordinal) if fact.valid_from_ordinal is not None else None,
        "valid_to_ordinal": float(fact.valid_to_ordinal) if fact.valid_to_ordinal is not None else None,
        "source_event_id": fact.source_event_id,
        "evidence_ref_id": fact.evidence_ref_id,
        "status": fact.status,
        "ingested_at": fact.ingested_at.isoformat() if fact.ingested_at else None,
        "expired_at": fact.expired_at.isoformat() if fact.expired_at else None,
    }


def _serialize_snapshot(snapshot: DataModelSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "ontology_id": snapshot.ontology_id,
        "schema_revision_id": snapshot.schema_revision_id,
        "replay_id": snapshot.replay_id,
        "dataset_version_id": snapshot.dataset_version_id,
        "graph_namespace": snapshot.graph_namespace,
        "through_ordinal": float(snapshot.through_ordinal) if snapshot.through_ordinal is not None else None,
        "watermark_sequence": snapshot.watermark_sequence,
        "event_count": snapshot.event_count,
        "node_count": snapshot.node_count,
        "edge_count": snapshot.edge_count,
        "fact_count": snapshot.fact_count,
        "evidence_count": snapshot.evidence_count,
        "snapshot_hash": snapshot.snapshot_hash,
        "snapshot_hash_version": _stored_contract_version(snapshot, "snapshot_hash_version"),
        "status": snapshot.status,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "published_at": snapshot.published_at.isoformat() if snapshot.published_at else None,
    }


def serialize_stream(replay: TemporalReplay, *, db: Session | None = None) -> dict[str, Any]:
    """Return a JSON-safe stream status payload shared by every endpoint."""
    error_code, error_message = temporal_error_details(
        replay.error, fallback_code="TEMPORAL_STREAM_FAILED",
    )
    live_mode = replay.source_mode == "simulated_live"
    metrics = dict(replay.metrics or {})
    total = int(replay.total_events or replay.selected_rows or 0)
    committed = int(replay.committed_events or replay.normalized_rows or 0)
    if live_mode:
        # A simulated live source deliberately does not expose its future
        # horizon.  ``total_events`` is the number received so far, not a
        # prediction of the file length; the public payload uses the explicit
        # received_events field instead of a progress percentage.
        metrics.setdefault("events_received", committed)
        metrics.pop("events_total", None)
        metrics.pop("queue", None)
        progress: dict[str, Any] = {"stage": _stage(replay), "completed": committed}
        public_total: int | None = None
    else:
        metrics.setdefault("events_total", total)
        metrics.setdefault("events_committed", committed)
        metrics.setdefault("queue", max(0, total - committed))
        progress = {"pct": round(committed / total * 100, 2) if total else 0.0, "completed": committed, "total": total, "stage": _stage(replay)}
        public_total = total
    latest_event = None
    last_received_at = None
    if db is not None:
        latest_event_row = db.query(TemporalStreamEvent).filter(
            TemporalStreamEvent.replay_id == replay.id,
            TemporalStreamEvent.status == "committed",
        ).order_by(TemporalStreamEvent.source_sequence.desc()).first()
        if latest_event_row:
            latest_event = _serialize_event(latest_event_row)
            last_received_at = latest_event_row.committed_at.isoformat() if latest_event_row.committed_at else None
    config = dict(replay.config or {})
    public_config = config
    if live_mode:
        # Keep the internal selection cursor available to the worker, but do
        # not leak a user-selected end boundary (or a source-file horizon) in
        # the blind-stream response.  The UI can only expose ordinals that
        # have already arrived.
        public_config = dict(config)
        selection = dict(public_config.get("selection") or {})
        selection.pop("start_ordinal", None)
        selection.pop("end_ordinal", None)
        public_config["selection"] = selection
    payload: dict[str, Any] = {
        "id": replay.id,
        "run_id": replay.id,
        "replay_id": replay.id,
        "ontology_id": replay.ontology_id,
        "dataset_id": replay.dataset_id,
        "dataset_version_id": replay.dataset_version_id,
        "source_id": replay.source_id,
        "source_mode": replay.source_mode,
        "status": replay.status,
        "schema_revision_id": replay.schema_revision_id,
        "graph_namespace": replay.graph_namespace,
        "time_kind": replay.time_kind,
        "entity_column": replay.entity_column,
        "time_column": replay.time_column,
        "episode_ids": replay.series_ids or [],
        "series_ids": replay.series_ids or [],
        "start_ordinal": None if live_mode else replay.start_time,
        "end_ordinal": None if live_mode else replay.end_time,
        "start_time": None if live_mode else replay.start_time,
        "end_time": None if live_mode else replay.end_time,
        "current_ordinal": replay.current_time,
        "current_time": replay.current_time,
        "current_event_index": replay.current_event_index,
        "current_batch_index": replay.current_batch_index,
        "total_events": public_total,
        "committed_events": committed,
        "total_batches": None if live_mode else replay.total_batches,
        "source_rows": None if live_mode else replay.source_rows,
        "selected_rows": None if live_mode else replay.selected_rows,
        "normalized_rows": replay.normalized_rows,
        "watermark_ordinal": float(replay.watermark_ordinal) if replay.watermark_ordinal is not None else None,
        "watermark_sequence": replay.watermark_sequence,
        "event_interval_ms": replay.event_interval_ms,
        "speed": replay.speed,
        "progress": progress,
        "metrics": metrics,
        "config": public_config,
        "horizon_known": not live_mode,
        "received_events": committed,
        "first_received_ordinal": _float_or_none(config.get("first_received_ordinal")) if live_mode else _float_or_none(replay.start_time),
        "last_received_at": last_received_at,
        "source_exhausted": bool(config.get("source_exhausted", False)) if live_mode else None,
        "state": replay.state or {},
        "pause_requested": bool(replay.pause_requested),
        "step_requested": bool(replay.step_requested),
        "cancel_requested": bool(replay.cancel_requested),
        "base_snapshot_id": replay.base_snapshot_id,
        "published_snapshot_id": replay.published_snapshot_id,
        "published_at": replay.published_at.isoformat() if replay.published_at else None,
        "heartbeat_at": replay.heartbeat_at.isoformat() if replay.heartbeat_at else None,
        "worker_lease_expires_at": replay.worker_lease_expires_at.isoformat() if replay.worker_lease_expires_at else None,
        "next_attempt_at": replay.next_attempt_at.isoformat() if replay.next_attempt_at else None,
        "dispatch_attempts": int(replay.dispatch_attempts or 0),
        "error": error_message,
        "error_code": error_code,
        "created_at": replay.created_at.isoformat() if replay.created_at else None,
        "started_at": replay.started_at.isoformat() if replay.started_at else None,
        "completed_at": replay.completed_at.isoformat() if replay.completed_at else None,
        "updated_at": replay.updated_at.isoformat() if replay.updated_at else None,
    }
    payload["latest_event"] = latest_event
    return payload


def _stage(replay: TemporalReplay) -> str:
    if replay.status == "created":
        return "ready"
    if replay.status in {"queued", "running", "pausing", "paused"}:
        return "event_projection"
    if replay.status == "completed":
        return "awaiting_publish"
    if replay.status == "published":
        return "published"
    return replay.status


def _load_rows(db: Session, version: DatasetVersion) -> list[dict[str, Any]]:
    if not version.storage_uri:
        raise StreamError("数据版本没有可读取对象", "SOURCE_NOT_READY")
    try:
        raw = get_storage_service().get_object(version.storage_uri)
    except Exception as exc:
        raise StreamError(f"源文件无法读取：{exc}", "STORAGE_OBJECT_MISSING") from exc
    from app.routers.v2.temporal import parse_temporal_bytes
    rows = parse_temporal_bytes(raw)
    for index, row in enumerate(rows):
        row.setdefault("_source_row_index", index)
    return rows


def _resolve_source(db: Session, dataset_id: str | None, version_id: str | None) -> tuple[Dataset, DatasetVersion, list[dict[str, Any]]]:
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first() if dataset_id else find_factorynet_dataset(db)
    if not dataset:
        raise StreamError("请先安装 FactoryNet CNC 官方样例", "SOURCE_NOT_INSTALLED")
    manifest = dict(dataset.schema_json or {})
    if manifest.get("source_id") not in {None, FACTORYNET_SOURCE_ID} and dataset.name != "FactoryNet CNC 铣削时序数据":
        raise StreamError("该数据版本不是 FactoryNet CNC 官方样例", "WRONG_SOURCE")
    version = db.query(DatasetVersion).filter(DatasetVersion.id == version_id).first() if version_id else None
    if version is None and dataset.latest_version_id:
        version = db.query(DatasetVersion).filter(DatasetVersion.id == dataset.latest_version_id).first()
    if version is None:
        version = db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version_no.desc()).first()
    if not version:
        raise StreamError("FactoryNet 数据版本不存在", "SOURCE_NOT_READY")
    return dataset, version, _load_rows(db, version)


def _resolve_generic_source(db: Session, source_id: str, dataset_id: str | None, version_id: str | None) -> tuple[Dataset, DatasetVersion, list[dict[str, Any]]]:
    if not dataset_id:
        raise StreamError("文件来源必须显式指定 dataset_id", "SOURCE_NOT_READY")
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or (dataset.schema_json or {}).get("source_id") != source_id:
        raise StreamError("数据集来源标识与 source_id 不一致", "WRONG_SOURCE")
    version = db.get(DatasetVersion, version_id or dataset.latest_version_id) if (version_id or dataset.latest_version_id) else None
    if version is None or version.dataset_id != dataset.id:
        raise StreamError("数据版本不属于指定来源", "SOURCE_NOT_READY")
    return dataset, version, _load_rows(db, version)


def _generic_link_specs(mapping: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize the optional, schema-pinned self-link mappings for a source."""
    configured = mapping.get("links", [])
    if not isinstance(configured, list) or len(configured) > 64:
        raise StreamError("mapping.links 必须是最多 64 项的列表", "INVALID_SOURCE_MAPPING")
    specs: list[dict[str, str]] = []
    seen_fields: set[str] = set()
    for item in configured:
        if not isinstance(item, dict):
            raise StreamError("每个关系映射必须声明 source_field 和 link_type_id", "INVALID_SOURCE_MAPPING")
        source_field = item.get("source_field")
        link_type_id = item.get("link_type_id")
        if (not isinstance(source_field, str) or not source_field.strip() or source_field.strip().startswith("_") or len(source_field) > 80
                or not isinstance(link_type_id, str) or not link_type_id.strip() or len(link_type_id) > 200):
            raise StreamError("关系映射需提供 source_field 和固定版本的 link_type_id", "INVALID_SOURCE_MAPPING")
        source_field = source_field.strip()
        link_type_id = link_type_id.strip()
        if source_field in seen_fields:
            raise StreamError(f"关系映射字段重复：{source_field}", "INVALID_SOURCE_MAPPING")
        seen_fields.add(source_field)
        normalized = {"source_field": source_field, "link_type_id": link_type_id}
        relation_type = item.get("relation_type")
        if relation_type is not None:
            if not isinstance(relation_type, str) or not relation_type.strip() or len(relation_type) > 100:
                raise StreamError("已解析的关系标签无效", "INVALID_SOURCE_MAPPING")
            normalized["relation_type"] = relation_type.strip()
        if item.get("semantic_resource_id") is not None:
            if str(item["semantic_resource_id"]) != link_type_id:
                raise StreamError("关系资源 ID 与固定 link_type_id 不一致", "INVALID_SOURCE_MAPPING")
            normalized["semantic_resource_id"] = link_type_id
        specs.append(normalized)
    return specs


def _ensure_factorynet_schema(db: Session, ontology_id: str) -> None:
    """Ensure the deterministic FactoryNet vocabulary exists.

    Existing temporal ontologies already have an approved schema.  Do not
    rewrite their property definitions merely because a new stream run is
    opened; only an empty/partial legacy project is seeded.
    """
    from app.models.entity import Entity
    from app.tasks.v2.temporal_construction import _ensure_factorynet_schema as ensure_schema
    required = {"Machine", "Episode", "Observation", "ProcessPhase", "ToolCondition", "SensorChannel", "InspectionResult"}
    present = {
        str(item.name_en or item.canonical_id or "")
        for item in db.query(Entity).filter(
            Entity.ontology_id == ontology_id,
            Entity.is_source_deleted.is_(False),
        ).all()
    }
    if not required.issubset(present):
        ensure_schema(db, ontology_id)


def ensure_schema_revision(db: Session, project: OntologyProject) -> OntologyRevision:
    """Pin a lightweight immutable schema revision for a stream run.

    Some of the earliest FactoryNet demo ontologies were created before the
    revision service ran and therefore have no ``current_revision_id``.  A
    stream still needs a concrete schema boundary for publish-time conflict
    detection, so create revision 1 locally when necessary.  The snapshot is
    retained in PostgreSQL; later editor/revision flows may attach an object
    storage URI without changing the pinned content.
    """
    if project.current_revision_id:
        current = db.query(OntologyRevision).filter(
            OntologyRevision.id == project.current_revision_id,
            OntologyRevision.ontology_id == project.id,
        ).first()
        if current:
            return current
    current = db.query(OntologyRevision).filter(
        OntologyRevision.ontology_id == project.id,
        OntologyRevision.is_current.is_(True),
    ).order_by(OntologyRevision.revision_no.desc()).first()
    if current:
        project.current_revision_id = current.id
        return current
    from app.services.v2.revision_service import snapshot_ontology
    snapshot = snapshot_ontology(db, project.id)
    digest = hashlib.sha256(_json(snapshot).encode("utf-8")).hexdigest()
    latest = db.query(OntologyRevision.revision_no).filter(
        OntologyRevision.ontology_id == project.id,
    ).order_by(OntologyRevision.revision_no.desc()).first()
    revision_no = int(latest[0]) + 1 if latest else 1
    revision = OntologyRevision(
        id=str(uuid.uuid4()),
        ontology_id=project.id,
        revision_no=revision_no,
        graph_namespace=f"ontology:{project.id}:r{revision_no}",
        snapshot_json=snapshot,
        snapshot_hash=digest,
        summary={
            "entity_count": len(snapshot.get("entities", [])),
            "relation_count": len(snapshot.get("relations", [])),
            "logic_count": len(snapshot.get("logic_rules", [])),
            "instance_count": int(snapshot.get("instance_count", 0)),
            "stream_schema_bootstrap": True,
        },
        status="current",
        is_current=True,
        created_at=_now(),
    )
    db.add(revision)
    project.current_revision_id = revision.id
    project.version = f"r{revision_no}"
    db.flush()
    return revision


def _select_rows(
    rows: list[dict[str, Any]],
    *,
    episode_ids: Iterable[str] | None = None,
    start_ordinal: float | None = None,
    end_ordinal: float | None = None,
    time_column: str = "time_s",
    time_kind: str = "ordinal",
    max_records: int | None = None,
) -> list[tuple[dict[str, Any], float, str]]:
    wanted = {str(value).strip() for value in (episode_ids or []) if str(value).strip()}
    selected: list[tuple[dict[str, Any], float, str]] = []
    for index, original in enumerate(rows):
        row = dict(original)
        episode = _episode(row)
        if wanted and episode not in wanted:
            continue
        try:
            ordinal = (_parse_event_time(row.get(time_column)).timestamp()
                       if time_kind == "event_time" else _source_time(row, index, time_column))
        except StreamError as exc:
            # Do not silently discard a source row.  A dynamic run must either
            # index every selected observation or fail with the exact source
            # row and field so the caller can repair the input.
            raise StreamError(f"源记录 {index} 的 {time_column} 无效：{exc}", "INVALID_EVENT", row_index=index) from exc
        if start_ordinal is not None and ordinal < start_ordinal:
            continue
        if end_ordinal is not None and ordinal > end_ordinal:
            continue
        selected.append((row, ordinal, _row_id(row, index)))
    selected.sort(key=lambda item: (item[1], _episode(item[0]), _source_order(item[0])))
    if max_records is not None:
        selected = selected[: max(1, min(int(max_records), len(selected)))]
    return selected


def create_stream_run(
    db: Session,
    ontology_id: str,
    *,
    dataset_id: str | None = None,
    dataset_version_id: str | None = None,
    episode_ids: list[str] | None = None,
    start_ordinal: float | None = None,
    end_ordinal: float | None = None,
    speed: float = DEFAULT_SPEED,
    time_column: str = "time_s",
    entity_column: str = "episode_id",
    source_mode: str = "file_replay",
    source_id: str = FACTORYNET_SOURCE_ID,
    config: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> TemporalReplay:
    """Create a queued event index; no instance graph is written yet."""
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise StreamError("本体不存在", "ONTOLOGY_NOT_FOUND")
    if str(getattr(project, "data_class", "regular")) != "temporal":
        raise StreamError("动态演化只能用于时序本体", "DATA_CLASS_MISMATCH")
    if source_mode not in {"file_replay", "push", "simulated_live"}:
        raise StreamError("source_mode 必须是 file_replay、push 或 simulated_live", "INVALID_SOURCE_MODE")
    generic_source = source_id != FACTORYNET_SOURCE_ID
    config_for_run = dict(config or {})
    if generic_source:
        raw_mapping = config_for_run.get("mapping")
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 120:
            raise StreamError("source_id 必须是非空来源标识", "INVALID_SOURCE_ID")
        if source_mode not in {"push", "file_replay"} or not isinstance(raw_mapping, dict) or not isinstance(raw_mapping.get("entity_type"), str) or not raw_mapping["entity_type"].strip() or not isinstance(raw_mapping.get("properties"), dict) or not raw_mapping["properties"] or (source_mode == "file_replay" and not isinstance(raw_mapping.get("entity_key_field"), str)):
            raise StreamError("通用来源需显式声明 mapping.entity_type、mapping.properties，文件来源还需 entity_key_field", "INVALID_SOURCE_MAPPING")
        mapping = dict(raw_mapping)
        if any(not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip() or target.startswith("_") for source, target in mapping["properties"].items()):
            raise StreamError("属性映射必须使用非空字段名，且目标字段不能以 _ 开头", "INVALID_SOURCE_MAPPING")
        revision = db.query(OntologyRevision).filter(OntologyRevision.id == project.current_revision_id, OntologyRevision.ontology_id == project.id).first() if project.current_revision_id else None
        if revision is None:
            raise StreamError("通用来源需要已发布的 schema revision", "SCHEMA_REVISION_REQUIRED")
        revision_snapshot = revision.snapshot_json or {}
        entities = revision_snapshot.get("entities") or []
        mapped_entity = next((entity for entity in entities if isinstance(entity, dict)
                              and mapping["entity_type"] in {entity.get("id"), entity.get("name_en"), entity.get("name_cn"), entity.get("canonical_id")}), None)
        if mapped_entity is None:
            raise StreamError("mapping.entity_type 不属于固定 schema revision", "INVALID_SOURCE_MAPPING")
        link_specs = _generic_link_specs(mapping)
        if set(mapping["properties"]).intersection(spec["source_field"] for spec in link_specs):
            raise StreamError("同一源字段不能同时映射为属性和关系", "INVALID_SOURCE_MAPPING")
        mapped_entity_id = str(mapped_entity.get("id") or "")
        mapped_entity_row = db.query(Entity).filter(
            Entity.ontology_id == project.id,
            Entity.id == mapped_entity_id,
            Entity.is_source_deleted.is_(False),
        ).first()
        if not link_specs:
            config_for_run["mapping"] = mapping
        if link_specs and (mapped_entity_row is None or not mapped_entity_row.semantic_resource_id):
            raise StreamError("关系映射需要有固定语义资源 ID 的映射对象类型", "INVALID_SOURCE_MAPPING")
        resolved_link_specs: list[dict[str, str]] = []
        for spec in link_specs:
            link_version = db.query(OntologySemanticResourceVersion).filter(
                OntologySemanticResourceVersion.ontology_id == project.id,
                OntologySemanticResourceVersion.revision_id == revision.id,
                OntologySemanticResourceVersion.resource_id == spec["link_type_id"],
                OntologySemanticResourceVersion.kind == "link_type",
            ).first()
            expected_type_resource_id = str(mapped_entity_row.semantic_resource_id) if mapped_entity_row else ""
            if (link_version is None
                    or link_version.source_resource_id != expected_type_resource_id
                    or link_version.target_resource_id != expected_type_resource_id):
                raise StreamError(
                    f"关系资源 {spec['link_type_id']} 不属于当前 schema revision 的 {mapping['entity_type']} 自关系",
                    "INVALID_SOURCE_MAPPING",
                )
            if not str(link_version.api_name or "").strip() or len(str(link_version.api_name)) > 160:
                raise StreamError(f"关系资源 {spec['link_type_id']} 的 API 名称不符合 TemporalFact 长度限制", "INVALID_SOURCE_MAPPING")
            resolved_link_specs.append({
                **spec,
                "relation_type": str(link_version.api_name),
                "semantic_resource_id": str(link_version.resource_id),
            })
        if resolved_link_specs:
            mapping["links"] = resolved_link_specs
            config_for_run["mapping"] = mapping
        if config_for_run.get("late_event_policy", "reject") != "reject":
            raise StreamError("当前仅支持拒绝迟到事件", "UNSUPPORTED_LATE_EVENT_POLICY")
        if config_for_run.get("time_kind", "ordinal") not in {"ordinal", "event_time"}:
            raise StreamError("time_kind 必须是 ordinal 或 event_time", "INVALID_TIME_KIND")
    speed = _finite_number(speed, "speed")
    if speed <= 0 or speed > MAX_SPEED:
        raise StreamError(f"speed 必须在 0 和 {MAX_SPEED} 之间", "INVALID_SPEED")
    if generic_source and source_mode == "push" and (dataset_id or dataset_version_id):
        raise StreamError("通用 push 来源不能附带文件数据版本", "INVALID_SOURCE_MAPPING")
    if source_mode == "push":
        if not dataset_id and not dataset_version_id:
            dataset = None
            version = None
            rows: list[dict[str, Any]] = []
        else:
            dataset, version, rows = _resolve_source(db, dataset_id, dataset_version_id)
    else:
        dataset, version, rows = (_resolve_generic_source(db, source_id, dataset_id, dataset_version_id)
                                  if generic_source else _resolve_source(db, dataset_id, dataset_version_id))
    if generic_source and source_mode == "file_replay":
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get(mapping["entity_key_field"]) in (None, ""):
                raise StreamError(f"源记录 {index} 缺少实体键", "INVALID_EVENT", row_index=index)
            row.setdefault("episode_id", str(row.get(entity_column) or row[mapping["entity_key_field"]]))
    available_episodes = {_episode(row) for row in rows}
    requested_episode_ids = {str(value).strip() for value in (episode_ids or []) if str(value).strip()}
    unknown = sorted(requested_episode_ids - available_episodes)
    # A push stream may introduce an episode that is not present in the
    # optional FactoryNet catalog.  File replay selections remain strict, but
    # rejecting a push-only episode here would make the documented event
    # contract unusable for a fresh sensor/source integration.
    if unknown and source_mode != "push":
        raise StreamError(f"不存在的 episode_id：{unknown[0]}", "UNKNOWN_EPISODE", episode_id=unknown[0])
    # A file replay is intentionally scoped to one complete episode unless the
    # caller explicitly selects episodes.  This keeps the first dynamic demo
    # small enough to inspect while still preserving every event in that
    # episode (the optional max_records limit is applied afterwards).
    effective_episode_ids = [str(value).strip() for value in (episode_ids or []) if str(value).strip()]
    if source_mode in {"file_replay", "simulated_live"} and not effective_episode_ids:
        candidates = _select_rows(
            rows,
            episode_ids=None,
            start_ordinal=start_ordinal,
            end_ordinal=end_ordinal,
            time_column=time_column,
            time_kind=(config or {}).get("time_kind", "ordinal"),
            max_records=None,
        )
        episode_order: list[str] = []
        for candidate, _ordinal, _source_row_id in candidates:
            episode = _episode(candidate)
            if episode not in episode_order:
                episode_order.append(episode)
        if episode_order:
            effective_episode_ids = [episode_order[0]]
    selected = _select_rows(
        rows,
        episode_ids=effective_episode_ids,
        start_ordinal=start_ordinal,
        end_ordinal=end_ordinal,
        time_column=time_column,
        time_kind=(config or {}).get("time_kind", "ordinal"),
        max_records=(config or {}).get("max_records"),
    )
    if source_mode in {"file_replay", "simulated_live"} and not selected:
        raise StreamError("所选 episode 或 Ordinal 范围没有可用记录", "NO_EVENTS")
    selected_episodes = sorted({ _episode(row) for row, _, _ in selected })
    # The live demo needs the first episode to be known, but must not reveal or
    # persist its future start/end horizon.  The source cursor will discover
    # the first actual Ordinal only when the first event arrives.
    if source_mode == "simulated_live":
        selected_episodes = [effective_episode_ids[0]] if effective_episode_ids else selected_episodes[:1]
    if source_mode != "simulated_live" and start_ordinal is None and selected:
        start_ordinal = selected[0][1]
    if source_mode != "simulated_live" and end_ordinal is None and selected:
        end_ordinal = selected[-1][1]
    replay_id = str(uuid.uuid4())
    namespace = f"stream_{uuid.uuid4().hex}"
    cfg = dict(config_for_run)
    explicit_interval = cfg.get("explicit_event_interval_ms") or cfg.get("event_interval_ms")
    try:
        explicit_interval = max(1, min(int(explicit_interval), 60000)) if explicit_interval is not None else None
    except (TypeError, ValueError):
        explicit_interval = None
    cfg.update({
        "source_id": source_id,
        "source_file": (dict(dataset.schema_json or {}).get("filename") if dataset else None) or ("FactoryNet CNC" if not generic_source else None),
        "source_checksum": version.checksum if version else None,
        "current_state_dimensions": cfg.get("current_state_dimensions") or ["phase", "tool_condition", "inspection"],
        "event_order": "ordinal,source_sequence",
        "selection": {
            "episode_ids": selected_episodes,
            "start_ordinal": start_ordinal if source_mode != "simulated_live" else (config or {}).get("start_ordinal", start_ordinal),
            "end_ordinal": end_ordinal if source_mode != "simulated_live" else (config or {}).get("end_ordinal", end_ordinal),
            "time_column": time_column,
        },
    })
    if source_mode == "simulated_live":
        cfg.update({
            "horizon_known": False,
            "source_cursor": 0,
            "source_exhausted": False,
            "first_received_ordinal": None,
        })
    live_mode = source_mode == "simulated_live"
    replay = TemporalReplay(
        id=replay_id,
        ontology_id=ontology_id,
        dataset_id=dataset.id if dataset else dataset_id,
        dataset_version_id=version.id if version else dataset_version_id,
        source_id=source_id,
        source_mode=source_mode,
        schema_revision_id=project.current_revision_id,
        graph_namespace=namespace,
        status="created",
        time_kind=cfg.get("time_kind", "ordinal"),
        entity_column=entity_column,
        time_column=time_column,
        series_ids=selected_episodes or [str(v) for v in (episode_ids or [])],
        start_time=start_ordinal,
        end_time=end_ordinal,
        window_seconds=1.0,
        speed=speed,
        event_interval_ms=explicit_interval or max(1, int(round(1000 / speed))),
        current_time=None,
        current_batch_index=-1,
        total_batches=0 if live_mode else len(selected),
        source_rows=0 if live_mode else len(rows),
        selected_rows=0 if live_mode else len(selected),
        normalized_rows=0,
        total_events=0 if live_mode else len(selected),
        committed_events=0,
        current_event_index=-1,
        base_snapshot_id=project.current_data_snapshot_id,
        metrics={"events_received": 0, **({} if live_mode else {"events_total": len(selected), "events_committed": 0, "queue": len(selected)}), "nodes_written": 0, "edges_written": 0, "facts_written": 0, "state_transitions": 0, "model_calls": 0},
        config=cfg,
        state={"builder": {}, "episodes": {}, "latest_event": None},
        error=None,
        pause_requested=False,
        step_requested=False,
        cancel_requested=False,
    )
    db.add(replay)
    db.flush()
    if not generic_source:
        _ensure_factorynet_schema(db, ontology_id)
        schema_revision = ensure_schema_revision(db, project)
        replay.schema_revision_id = schema_revision.id
        db.flush()
    if not live_mode:
        for source_sequence, (row, ordinal, source_row_id) in enumerate(selected):
            episode = _episode(row)
            event_id = f"{source_id}:{episode}:{source_row_id}"
            payload = {key: value for key, value in row.items() if not str(key).startswith("_")}
            entity_key = (str(row[mapping["entity_key_field"]]) if generic_source
                          else str(row.get("entity_key") or row.get("machine_type") or episode))
            temporal_source_ref: dict[str, str] = {}
            if generic_source and replay.time_kind == "event_time":
                event_time = _parse_event_time(row[time_column])
                temporal_source_ref = {
                    "event_time": event_time.isoformat(),
                    "time_zone": str(event_time.tzinfo),
                }
            source_ref = {"dataset_id": replay.dataset_id,
                          "dataset_version_id": replay.dataset_version_id,
                          "source_row_id": source_row_id,
                          "source_file": cfg.get("source_file"),
                          **temporal_source_ref}
            payload_hash = temporal_event_envelope_hash(
                replay, event_key=event_id, operation="upsert", episode_id=episode,
                entity_key=entity_key, ordinal=ordinal, source_sequence=source_sequence,
                source_row_id=source_row_id, payload=payload, source_ref=source_ref,
            )
            db.add(TemporalStreamEvent(
                id=str(uuid.uuid4()),
                replay_id=replay.id,
                event_key=event_id,
                episode_id=episode,
                entity_key=entity_key,
                ordinal=Decimal(str(ordinal)),
                source_sequence=source_sequence,
                source_row_id=source_row_id,
                payload=payload,
                source_ref=source_ref,
                payload_hash=payload_hash,
                payload_hash_version=TEMPORAL_EVENT_HASH_VERSION,
                status="queued",
            ))
    replay.schema_revision_id = project.current_revision_id
    db.commit()
    db.refresh(replay)
    return replay


def _ensure_construction_run(db: Session, replay: TemporalReplay) -> ConstructionRun:
    run_id = (replay.config or {}).get("construction_run_id")
    run = db.query(ConstructionRun).filter(ConstructionRun.id == run_id).first() if run_id else None
    if run:
        return run
    run = ConstructionRun(
        id=str(uuid.uuid4()), ontology_id=replay.ontology_id, dataset_id=replay.dataset_id,
        mode="temporal_stream", status="running", revision_id=replay.schema_revision_id,
        config={"replay_id": replay.id, "graph_namespace": replay.graph_namespace},
        progress={"stage": "动态事件投影", "completed": replay.committed_events, "total": replay.total_events}, metrics={},
    )
    db.add(run)
    replay.config = {**(replay.config or {}), "construction_run_id": run.id}
    db.flush()
    return run


def _evidence_for_event(db: Session, replay: TemporalReplay, event: TemporalStreamEvent, payload: dict[str, Any]) -> EvidenceRef:
    run = _ensure_construction_run(db, replay)
    evidence_id = f"stream-evidence:{replay.id}:{event.id}"
    existing = db.query(EvidenceRef).filter(EvidenceRef.id == evidence_id).first()
    if existing:
        return existing
    text = _json(payload)[:8000]
    evidence = EvidenceRef(
        id=evidence_id, construction_run_id=run.id, ontology_id=replay.ontology_id,
        assertion_id=f"event:{event.event_key}", assertion_kind="node",
        source_dataset_id=replay.dataset_id,
        source_version=str((replay.config or {}).get("source_checksum") or replay.dataset_version_id or ""),
        source_file=(replay.config or {}).get("source_file"), source_row_id=event.source_row_id,
        revision_id=replay.schema_revision_id, extractor="rule", confidence=1.0,
        confidence_method="factorynet_temporal_stream" if replay.source_id == FACTORYNET_SOURCE_ID else "mapped_temporal_stream", evidence_text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    db.add(evidence)
    db.flush()
    return evidence


def _maybe_create_dynamic_incident(db: Session, replay: TemporalReplay,
                                   event: TemporalStreamEvent, evidence: EvidenceRef,
                                   payload: dict[str, Any], ordinal: float) -> EntityInstance | None:
    """Persist a reviewable business object; never rewrite the source event."""
    try:
        feedback = _finite_number(payload.get("feedback_vel_0"), "feedback_vel_0")
        setpoint = _finite_number(payload.get("setpoint_vel_0"), "setpoint_vel_0")
    except StreamError:
        return None
    residual = abs(feedback - setpoint)
    if residual < DYNAMIC_DISPOSITION_THRESHOLD:
        return None
    incident_id = f"dynamic-incident:{replay.id}:{event.id}"
    existing = db.get(EntityInstance, incident_id)
    if existing:
        return existing
    type_id = f"{replay.ontology_id}:type:DynamicIncident"
    action_id = f"{replay.ontology_id}:action:factorynet_disposition_v1"
    if db.get(Entity, type_id) is None:
        db.add(Entity(id=type_id, ontology_id=replay.ontology_id,
                      name_cn="动态事件处置事项", name_en="DynamicIncident", type="EntityType",
                      properties={"property_definitions": [
                          {"id": name, "name": name, "type": "string"} for name in
                          ("event_key", "run_id", "episode_id", "source_row_id", "ordinal",
                           "residual", "threshold", "rule_version", "disposition_status")]}))
        db.flush()
    parameters = [{"name": "target", "type": "object", "object_type": "DynamicIncident", "required": True},
                  {"name": "decision", "type": "string", "choices":
                   ["confirmed", "ignored", "in_progress", "resolved"], "required": True},
                  {"name": "reason", "type": "string", "required": True}]
    effects = [{"op": "set_property", "target_parameter": "target", "property": "disposition_status",
                "value_parameter": "decision"},
               {"op": "set_property", "target_parameter": "target", "property": "disposition_reason",
                "value_parameter": "reason"}]
    action = db.get(OntologyActionType, action_id)
    if action is None:
        project = db.get(OntologyProject, replay.ontology_id)
        db.add(OntologyActionType(id=action_id, ontology_id=replay.ontology_id,
            name="动态事件人工处置", target_entity_type="DynamicIncident",
            action_category="decision_case", parameters=parameters, effects=effects,
            submission_criteria=[{"type": "required_target"}],
            permission_rules=[{"role": "admin"}, {"role": "editor"}],
            side_effects=[], enabled=True, status="published", version=1,
            created_by=project.created_by if project else None))
    elif action.parameters != parameters or action.effects != effects:
        raise StreamError("动态处置 Action 定义与当前规则不一致", "ACTION_VERSION_CONFLICT")
    incident_evidence_id = f"stream-incident-evidence:{replay.id}:{event.id}"
    if db.get(EvidenceRef, incident_evidence_id) is None:
        db.add(EvidenceRef(id=incident_evidence_id,
            construction_run_id=evidence.construction_run_id, ontology_id=replay.ontology_id,
            assertion_id=incident_id, assertion_kind="node",
            source_dataset_id=evidence.source_dataset_id, source_version=evidence.source_version,
            source_file=evidence.source_file, source_row_id=evidence.source_row_id,
            revision_id=evidence.revision_id, extractor="rule", confidence=1.0,
            confidence_method=DYNAMIC_DISPOSITION_RULE_VERSION,
            evidence_text=evidence.evidence_text, content_hash=evidence.content_hash))
    incident = EntityInstance(id=incident_id, ontology_id=replay.ontology_id,
        entity_id=type_id, row_identity=incident_id, source_version=evidence.source_version,
        row_data={"event_key": event.event_key, "event_id": event.id, "run_id": replay.id,
                  "episode_id": event.episode_id, "source_row_id": event.source_row_id,
                  "event_time": (event.source_ref or {}).get("event_time"),
                  "ingestion_time": event.ingested_at.isoformat() if event.ingested_at else None,
                  "ordinal": ordinal, "source_cursor": event.source_sequence,
                  "feedback_vel_0": feedback, "setpoint_vel_0": setpoint,
                  "residual": round(residual, 6), "threshold": DYNAMIC_DISPOSITION_THRESHOLD,
                  "rule_version": DYNAMIC_DISPOSITION_RULE_VERSION,
                  "source_ref": event.source_ref or {},
                  "evidence_ref_id": incident_evidence_id,
                  "disposition_status": "pending_confirmation"})
    db.add(incident)
    return incident


def _factorynet_observation_instance_id(ontology_id: str, episode_id: str, source_row_id: Any) -> str:
    """Return the stable Object instance ID shared by replay and live ingestion."""
    identity = f"{episode_id}:{source_row_id}"
    digest = hashlib.sha256(f"{ontology_id}:Observation:{identity}".encode("utf-8")).hexdigest()[:20]
    return f"temporal:{ontology_id}:Observation:{digest}"


def _materialize_factorynet_observation(
    db: Session,
    replay: TemporalReplay,
    event: TemporalStreamEvent,
    row: dict[str, Any],
    source_sequence: int,
) -> EntityInstance:
    """Upsert the canonical Object row for a streamed FactoryNet observation."""
    entity_id = f"schema:{replay.ontology_id}:Observation"
    entity = db.get(Entity, entity_id)
    if entity is None:
        raise StreamError(
            "当前本体版本缺少 Observation 对象类型，无法落库观测实例",
            "OBSERVATION_OBJECT_TYPE_MISSING",
        )
    source_row_id = event.source_row_id or source_sequence
    identity = f"{event.episode_id}:{source_row_id}"
    instance_id = _factorynet_observation_instance_id(replay.ontology_id, event.episode_id, source_row_id)
    item = db.get(EntityInstance, instance_id)
    row_data = {key: value for key, value in row.items() if key != "_replay_event_seq"}
    source_version = str((replay.config or {}).get("source_checksum") or replay.dataset_version_id or "factorynet")[:120]
    if item is None:
        item = EntityInstance(
            id=instance_id,
            ontology_id=replay.ontology_id,
            entity_id=entity_id,
            row_identity=identity[:200],
            row_data=row_data,
            revision_id=replay.schema_revision_id,
            object_type_resource_id=entity.semantic_resource_id,
            source_version=source_version,
        )
        db.add(item)
    else:
        item.entity_id = entity_id
        item.row_identity = identity[:200]
        item.row_data = row_data
        item.revision_id = item.revision_id or replay.schema_revision_id
        item.object_type_resource_id = entity.semantic_resource_id or item.object_type_resource_id
        item.source_version = source_version
        item.is_deleted = False
        item.deleted_at = None
    return item


def _normalize_factorynet_reconciliation_event(
    replay: TemporalReplay,
    event: TemporalStreamEvent,
) -> dict[str, Any] | None:
    """Rebuild the same source-row payload used by live event projection."""
    payload = dict(event.payload or {})
    if not payload:
        return None
    sequence = int(event.source_sequence or 0)
    payload.setdefault("episode_id", event.episode_id)
    payload.setdefault("_source_row_index", event.source_row_id or sequence)
    time_column = replay.time_column or "time_s"
    payload[time_column] = payload.get(time_column, float(event.ordinal))
    normalized, issues = normalize_temporal_rows(
        [payload], TemporalConfig(time_kind="ordinal", sequence_column=time_column)
    )
    if issues or not normalized:
        return None
    return normalized[0]


def _factorynet_reconciliation_page(
    db: Session,
    replay: TemporalReplay,
    *,
    offset: int,
    limit: int,
) -> tuple[int, list[TemporalStreamEvent], dict[str, tuple[TemporalStreamEvent, dict[str, Any]]], set[str], int]:
    total = db.query(TemporalStreamEvent).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
    ).count()
    events = db.query(TemporalStreamEvent).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
    ).order_by(
        TemporalStreamEvent.ordinal.asc(),
        TemporalStreamEvent.source_sequence.asc(),
        TemporalStreamEvent.id.asc(),
    ).offset(offset).limit(limit).all()
    candidates: dict[str, tuple[TemporalStreamEvent, dict[str, Any]]] = {}
    invalid_events = 0
    for event in events:
        row = _normalize_factorynet_reconciliation_event(replay, event)
        if row is None:
            invalid_events += 1
            continue
        instance_id = _factorynet_observation_instance_id(
            replay.ontology_id,
            event.episode_id,
            event.source_row_id or event.source_sequence,
        )
        # Events are ordered by source time, so a repeated source identity is
        # materialized from its latest observation in this page.
        candidates[instance_id] = (event, row)
    candidate_ids = list(candidates)
    existing_ids: set[str] = set()
    for start in range(0, len(candidate_ids), 1000):
        chunk = candidate_ids[start:start + 1000]
        existing_ids.update(
            row_id for (row_id,) in db.query(EntityInstance.id)
            .filter(EntityInstance.ontology_id == replay.ontology_id, EntityInstance.id.in_(chunk))
            .all()
        )
    return total, events, candidates, existing_ids, invalid_events


def _require_reconcilable_factorynet_run(replay: TemporalReplay) -> None:
    if replay.source_id != FACTORYNET_SOURCE_ID:
        raise StreamError("当前对账入口只支持 FactoryNet Observation", "RECONCILIATION_SOURCE_UNSUPPORTED")
    if replay.status not in STREAM_TERMINAL_STATUSES:
        raise StreamError("动态运行仍可能接收事件，请在运行终止后再对账", "RUN_NOT_TERMINAL")


def preview_factorynet_observation_reconciliation(
    db: Session,
    replay: TemporalReplay,
    *,
    offset: int = 0,
    limit: int = 5000,
) -> dict[str, Any]:
    _require_reconcilable_factorynet_run(replay)
    total, events, candidates, existing_ids, invalid_events = _factorynet_reconciliation_page(
        db, replay, offset=offset, limit=limit
    )
    return {
        "run_id": replay.id,
        "status": replay.status,
        "offset": offset,
        "limit": limit,
        "total_committed_events": total,
        "scanned_events": len(events),
        "unique_observations": len(candidates),
        "existing_instances": len(existing_ids),
        "missing_instances": max(0, len(candidates) - len(existing_ids)),
        "invalid_events": invalid_events,
        "has_more": offset + len(events) < total,
        "next_offset": offset + len(events) if offset + len(events) < total else None,
        "schema_revision_id": replay.schema_revision_id,
        "dataset_version_id": replay.dataset_version_id,
    }


def apply_factorynet_observation_reconciliation(
    db: Session,
    replay: TemporalReplay,
    *,
    requested_by: str,
    idempotency_key: str,
    offset: int = 0,
    limit: int = 5000,
) -> dict[str, Any]:
    """Idempotently materialize a bounded, audited page of terminal-run observations."""
    locked = db.query(TemporalReplay).filter(TemporalReplay.id == replay.id).with_for_update().first()
    if locked is None:
        raise StreamError("动态运行不存在", "RUN_NOT_FOUND")
    _require_reconcilable_factorynet_run(locked)
    request_json = {
        "offset": offset,
        "limit": limit,
        "schema_revision_id": locked.schema_revision_id,
        "dataset_version_id": locked.dataset_version_id,
    }
    prior = db.query(TemporalInstanceReconciliation).filter(
        TemporalInstanceReconciliation.replay_id == locked.id,
        TemporalInstanceReconciliation.idempotency_key == idempotency_key,
    ).first()
    if prior is not None:
        if prior.request_json != request_json:
            raise StreamError("幂等键已用于不同的对账页", "IDEMPOTENCY_KEY_REUSED")
        return {**(prior.result_json or {}), "idempotent": True}

    total, events, candidates, existing_ids, invalid_events = _factorynet_reconciliation_page(
        db, locked, offset=offset, limit=limit
    )
    entity_id = f"schema:{locked.ontology_id}:Observation"
    entity = db.get(Entity, entity_id)
    if candidates and entity is None:
        raise StreamError(
            "当前本体版本缺少 Observation 对象类型，无法执行历史对账",
            "OBSERVATION_OBJECT_TYPE_MISSING",
        )
    candidate_ids = list(candidates)
    existing_rows = db.query(EntityInstance).filter(
        EntityInstance.ontology_id == locked.ontology_id,
        EntityInstance.id.in_(candidate_ids),
    ).all() if candidate_ids else []
    existing_by_id = {row.id: row for row in existing_rows}
    created = 0
    updated = 0
    for instance_id, (event, row_data) in candidates.items():
        current = existing_by_id.get(instance_id)
        before = None if current is None else (
            current.entity_id, current.row_identity, current.row_data,
            current.revision_id, current.object_type_resource_id,
            current.source_version, current.is_deleted,
        )
        materialized = _materialize_factorynet_observation(
            db, locked, event, row_data, int(event.source_sequence or 0)
        )
        if current is None:
            created += 1
        else:
            after = (
                materialized.entity_id, materialized.row_identity, materialized.row_data,
                materialized.revision_id, materialized.object_type_resource_id,
                materialized.source_version, materialized.is_deleted,
            )
            if before != after:
                updated += 1

    reconciliation_id = str(uuid.uuid4())
    result = {
        "reconciliation_id": reconciliation_id,
        "run_id": locked.id,
        "status": "applied",
        "offset": offset,
        "limit": limit,
        "total_committed_events": total,
        "scanned_events": len(events),
        "unique_observations": len(candidates),
        "existing_instances_before": len(existing_ids),
        "created_instances": created,
        "updated_instances": updated,
        "invalid_events": invalid_events,
        "schema_revision_id": locked.schema_revision_id,
        "dataset_version_id": locked.dataset_version_id,
        "has_more": offset + len(events) < total,
        "next_offset": offset + len(events) if offset + len(events) < total else None,
        "idempotent": False,
    }
    db.add(TemporalInstanceReconciliation(
        id=reconciliation_id,
        ontology_id=locked.ontology_id,
        replay_id=locked.id,
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        request_json=request_json,
        result_json={key: value for key, value in result.items() if key != "idempotent"},
    ))
    db.commit()
    return result


def _fact_id(replay_id: str, subject: str, predicate: str, obj: str | None, state_key: str, valid_from: Any) -> str:
    return "fact:" + hashlib.sha256(_json([replay_id, subject, predicate, obj, state_key, str(valid_from)]).encode("utf-8")).hexdigest()


def _close_state_fact(db: Session, replay: TemporalReplay, subject_id: str, state_key: str, ordinal: float, graph: FalkorDBService | None) -> TemporalFact | None:
    existing = db.query(TemporalFact).filter(
        TemporalFact.replay_id == replay.id,
        TemporalFact.subject_id == subject_id,
        TemporalFact.state_key == state_key,
        TemporalFact.valid_to_ordinal.is_(None),
        TemporalFact.status == "active",
    ).order_by(TemporalFact.valid_from_ordinal.desc()).first()
    if not existing:
        return None
    existing.valid_to_ordinal = Decimal(str(ordinal))
    existing.expired_at = _now()
    existing.status = "expired"
    if graph and getattr(graph, "available", False):
        try:
            graph.expire_relation_by_fact_id(replay.ontology_id, existing.id, ordinal, graph_namespace=replay.graph_namespace)
        except TypeError:
            try:
                graph.expire_relation_by_fact_id(replay.ontology_id, existing.id, ordinal)
            except Exception:
                pass
        except Exception:
            pass
    return existing


def _add_fact(
    db: Session,
    replay: TemporalReplay,
    *,
    subject_id: str,
    predicate: str,
    object_id: str | None,
    object_value: Any = None,
    state_key: str = "",
    ordinal: float,
    event: TemporalStreamEvent,
    evidence: EvidenceRef,
    graph: FalkorDBService | None,
) -> TemporalFact:
    fact_key = _fact_id(replay.id, subject_id, predicate, object_id, state_key, ordinal if state_key == "" else event.id)
    existing = db.get(TemporalFact, fact_key)
    if existing:
        return existing
    fact = TemporalFact(
        id=fact_key, replay_id=replay.id, subject_id=subject_id, predicate=predicate,
        object_id=object_id, object_value=object_value if object_value is not None else None,
        state_key=state_key, valid_from_ordinal=Decimal(str(ordinal)), valid_to_ordinal=None,
        source_event_id=event.id, evidence_ref_id=evidence.id, status="active",
    )
    db.add(fact)
    db.flush()
    return fact


def _graph_upsert_instances(graph: Any, ontology_id: str, nodes: list[dict[str, Any]], namespace: str) -> int:
    if not graph or not getattr(graph, "available", False):
        return 0
    try:
        return int(graph.upsert_instances(ontology_id, nodes, graph_namespace=namespace) or 0)
    except TypeError:
        return int(graph.upsert_instances(ontology_id, nodes) or 0)


def _graph_upsert_relations(graph: Any, ontology_id: str, edges: list[dict[str, Any]], namespace: str) -> int:
    if not graph or not getattr(graph, "available", False):
        return 0
    try:
        return int(graph.upsert_relations(ontology_id, edges, graph_namespace=namespace) or 0)
    except TypeError:
        return int(graph.upsert_relations(ontology_id, edges) or 0)


def _state_object(row: dict[str, Any], kind: str) -> tuple[str, str] | None:
    values = {
        "phase": ("ctx_process_phase", "FactoryNet:ProcessPhase:"),
        "tool_condition": ("ctx_tool_condition", "FactoryNet:ToolCondition:"),
        "inspection": ("ctx_passed_visual_inspection", "FactoryNet:InspectionResult:"),
    }
    source, prefix = values[kind]
    value = row.get(source)
    if value in (None, ""):
        return None
    text = str(value).strip()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return prefix + digest, text


def _process_generic_event(db: Session, replay: TemporalReplay, event: TemporalStreamEvent, *, commit: bool) -> dict[str, Any]:
    """Project an explicitly mapped push event, including temporal tombstones."""
    mapping = dict((replay.config or {}).get("mapping") or {})
    property_map = mapping.get("properties") or {}
    if not isinstance(property_map, dict) or not property_map:
        raise StreamError("通用来源需要 mapping.properties", "INVALID_SOURCE_MAPPING")
    payload = dict(event.payload or {})
    operation = str(event.operation or "upsert")
    if operation not in {"upsert", "delete"}:
        raise StreamError("operation 必须是 upsert 或 delete", "UNSUPPORTED_OPERATION")
    values = {str(target): payload[source] for source, target in property_map.items() if source in payload}
    link_updates: list[tuple[str, str, str | None]] = []
    if operation == "upsert":
        for spec in _generic_link_specs(mapping):
            source_field = spec["source_field"]
            relation_type = spec.get("relation_type")
            link_type_id = spec.get("semantic_resource_id")
            if not relation_type or not link_type_id:
                raise StreamError("关系映射缺少固定版本的语义关系资源", "INVALID_SOURCE_MAPPING")
            if source_field not in payload:
                continue
            raw_target = payload[source_field]
            if raw_target in (None, ""):
                target_id = None
            elif isinstance(raw_target, (str, int)) and not isinstance(raw_target, bool):
                target_id = str(raw_target).strip() or None
            else:
                raise StreamError(f"关系字段 {source_field} 必须是单个对象 ID 或空值", "INVALID_EVENT")
            link_updates.append((source_field, relation_type, target_id))
    if operation == "upsert" and not values and not link_updates:
        raise StreamError("事件没有匹配映射的属性", "INVALID_EVENT")
    ordinal = _finite_number(event.ordinal)
    sequence = int(event.source_sequence)
    graph = FalkorDBService()
    namespace = replay.graph_namespace or f"stream_{replay.id.replace('-', '')}"
    entity_id = str(event.entity_key)
    entity_type = str(mapping["entity_type"])
    evidence = _evidence_for_event(db, replay, event, payload)
    facts: list[TemporalFact] = []
    if operation == "delete":
        active_state_keys = [str(key) for (key,) in db.query(TemporalFact.state_key).filter(
            TemporalFact.replay_id == replay.id,
            TemporalFact.subject_id == entity_id,
            TemporalFact.status == "active",
            TemporalFact.valid_to_ordinal.is_(None),
            TemporalFact.state_key != "",
        ).distinct().all()]
        for state_key in active_state_keys:
            _close_state_fact(db, replay, entity_id, state_key, ordinal, graph)
        tombstone = _add_fact(db, replay, subject_id=entity_id, predicate="DELETED",
                              object_id=None, object_value={"value": True}, state_key="__deleted__",
                              ordinal=ordinal, event=event, evidence=evidence, graph=graph)
        facts.append(tombstone)
        node_values = {"_deleted": True, "_deleted_at_ordinal": ordinal}
    else:
        # A later upsert can recreate a deleted object.  Closing the tombstone
        # keeps as-of reads before the new event intact while restoring the
        # object to the current projection.
        _close_state_fact(db, replay, entity_id, "__deleted__", ordinal, graph)
        for key, value in values.items():
            _close_state_fact(db, replay, entity_id, key, ordinal, graph)
            facts.append(_add_fact(db, replay, subject_id=entity_id, predicate=key,
                                   object_id=None, object_value={"value": value}, state_key=key,
                                   ordinal=ordinal, event=event, evidence=evidence, graph=graph))
        for source_field, relation_type, target_id in link_updates:
            state_key = f"link:{source_field}"
            _close_state_fact(db, replay, entity_id, state_key, ordinal, graph)
            if target_id is not None:
                facts.append(_add_fact(
                    db, replay, subject_id=entity_id, predicate=relation_type,
                    object_id=target_id, object_value=None, state_key=state_key,
                    ordinal=ordinal, event=event, evidence=evidence, graph=graph,
                ))
        node_values = {**values, "_deleted": False}
    node = {"id": entity_id, "entity_type": entity_type, "properties": {
        **node_values, "_replay_id": replay.id, "_event_id": event.id,
        "event_ordinal": ordinal, "_graph_namespace": namespace,
    }}
    nodes_written = _graph_upsert_instances(graph, replay.ontology_id, [node], namespace)
    # Generic relationships are reconstructed from PostgreSQL facts on reads.
    # FalkorDB is only a projection, so retry the incoming edges here too: a
    # source event can precede the event that materializes its target object.
    active_relationships = db.query(TemporalFact).filter(
        TemporalFact.replay_id == replay.id,
        ((TemporalFact.subject_id == entity_id) | (TemporalFact.object_id == entity_id)),
        TemporalFact.object_id.is_not(None),
        TemporalFact.valid_from_ordinal <= Decimal(str(ordinal)),
        (TemporalFact.valid_to_ordinal.is_(None)) | (TemporalFact.valid_to_ordinal > Decimal(str(ordinal))),
        TemporalFact.status == "active",
    ).order_by(TemporalFact.valid_from_ordinal.asc(), TemporalFact.id.asc()).all()
    link_resource_by_state_key = {
        f"link:{spec['source_field']}": spec["semantic_resource_id"]
        for spec in _generic_link_specs(mapping)
        if spec.get("semantic_resource_id")
    }
    graph_edges = []
    for fact in active_relationships:
        if not fact.object_id:
            continue
        edge_properties = {
            "_fact_id": fact.id,
            "_event_id": fact.source_event_id,
            "evidence_ref_id": fact.evidence_ref_id,
            "event_ordinal": float(fact.valid_from_ordinal),
            "valid_from_ordinal": float(fact.valid_from_ordinal),
            "_replay_id": replay.id,
            "_graph_namespace": namespace,
        }
        semantic_resource_id = link_resource_by_state_key.get(fact.state_key)
        if semantic_resource_id:
            edge_properties["semantic_resource_id"] = semantic_resource_id
        graph_edges.append({"id": fact.id, "source": fact.subject_id,
                            "target": fact.object_id, "type": fact.predicate,
                            "properties": edge_properties})
    edges_written = _graph_upsert_relations(graph, replay.ontology_id, graph_edges, namespace)
    replay.state = {**(replay.state or {}), "latest_event": {
        "id": event.id, "event_key": event.event_key, "ordinal": ordinal,
        "source_sequence": sequence, "entity_key": entity_id,
    }}
    replay.current_time = ordinal
    replay.watermark_ordinal = Decimal(str(ordinal))
    replay.watermark_sequence = sequence
    replay.current_event_index = sequence
    replay.committed_events = int(replay.committed_events or 0) + 1
    replay.normalized_rows = int(replay.normalized_rows or 0) + 1
    metrics = dict(replay.metrics or {})
    metrics.update({"events_committed": replay.committed_events,
                    "queue": max(0, int(replay.total_events or 0) - replay.committed_events),
                    "nodes_written": int(metrics.get("nodes_written", 0)) + nodes_written,
                    "edges_written": int(metrics.get("edges_written", 0)) + edges_written,
                    "facts_written": int(metrics.get("facts_written", 0)) + len(facts),
                    "last_event_id": event.id, "last_event_ordinal": ordinal})
    replay.metrics = metrics
    event.status = "committed"
    event.graph_projection_status = "projected" if graph and graph.available else "pending"
    event.graph_projected_at = _now() if graph and graph.available else None
    event.committed_at = _now()
    replay.updated_at = _now()
    if commit:
        db.commit()
        db.refresh(replay)
    return {"event": _serialize_event(event), "facts": [_serialize_fact(item) for item in facts],
            "nodes_written": nodes_written, "edges_written": edges_written, "state_transitions": len(facts)}


def process_one_event(db: Session, replay: TemporalReplay, event: TemporalStreamEvent, *, commit: bool = True) -> dict[str, Any]:
    """Project exactly one observation and migrate its current-state facts."""
    if event.replay_id != replay.id:
        raise StreamError("事件不属于当前动态运行", "EVENT_RUN_MISMATCH")
    if event.status == "committed":
        return {"event": _serialize_event(event), "idempotent": True, "facts": []}
    ordinal = _finite_number(event.ordinal)
    watermark = _float_or_none(replay.watermark_ordinal)
    sequence = int(event.source_sequence or 0)
    if watermark is not None:
        current_pair = (watermark, int(replay.watermark_sequence or -1))
        if (ordinal, sequence) <= current_pair:
            raise StreamError("事件早于或等于当前水位，不能在本版本重算", "LATE_EVENT_NOT_SUPPORTED", watermark_ordinal=watermark, watermark_sequence=replay.watermark_sequence)
    if replay.source_id != FACTORYNET_SOURCE_ID:
        return _process_generic_event(db, replay, event, commit=commit)
    if str(event.operation or "upsert") != "upsert":
        raise StreamError("FactoryNet observation events only support upsert", "UNSUPPORTED_OPERATION")
    payload = dict(event.payload or {})
    payload.setdefault("episode_id", event.episode_id)
    payload.setdefault("_source_row_index", event.source_row_id or sequence)
    time_column = replay.time_column or "time_s"
    payload[time_column] = payload.get(time_column, ordinal)
    normalized, issues = normalize_temporal_rows([payload], TemporalConfig(time_kind="ordinal", sequence_column=time_column))
    if issues or not normalized:
        message = issues[0].get("error") if issues else "记录没有有效 Ordinal"
        event.status = "failed"
        event.error = "INVALID_EVENT"
        replay.status = "failed"
        replay.error = "INVALID_EVENT"
        if commit:
            db.commit()
        raise StreamError(str(message), "INVALID_EVENT")
    row = normalized[0]
    state_payload = dict(replay.state or {})
    builder = FactoryNetIncrementalState.from_dict(state_payload.get("builder") or {})
    row["_replay_event_seq"] = builder.sequence_by_episode.get(event.episode_id, 0)
    nodes, structural_edges, builder = build_factorynet_instances_incremental([row], builder, sequence_column=time_column)
    episode_node = f"FactoryNet:Episode:{event.episode_id}"
    observation_node = f"FactoryNet:Observation:{event.episode_id}:{event.source_row_id or sequence}"
    observation_instance = _materialize_factorynet_observation(db, replay, event, row, sequence)
    graph = FalkorDBService()
    evidence = _evidence_for_event(db, replay, event, payload)
    incident = _maybe_create_dynamic_incident(db, replay, event, evidence, payload, ordinal)
    namespace = replay.graph_namespace or f"stream_{replay.id.replace('-', '')}"
    graph_nodes: list[dict[str, Any]] = []
    for node in nodes:
        props = dict(node.get("properties") or {})
        props.update({"_replay_id": replay.id, "_event_id": event.id, "event_ordinal": ordinal, "_graph_namespace": namespace})
        if str(node.get("id")) == observation_node:
            props["object_ref_id"] = observation_instance.id
            if observation_instance.object_type_resource_id:
                props["object_type_resource_id"] = observation_instance.object_type_resource_id
        graph_nodes.append({**node, "properties": props})
    graph_edges: list[dict[str, Any]] = []
    facts: list[TemporalFact] = []
    for edge in structural_edges:
        props = dict(edge.get("properties") or {})
        props.update({"_replay_id": replay.id, "_event_id": event.id, "event_ordinal": ordinal, "valid_from_ordinal": ordinal, "_graph_namespace": namespace})
        fact = _add_fact(db, replay, subject_id=str(edge["source"]), predicate=str(edge.get("type") or "RELATED"), object_id=str(edge["target"]), ordinal=ordinal, event=event, evidence=evidence, graph=graph)
        props["_fact_id"] = fact.id
        graph_edges.append({**edge, "properties": props})
        facts.append(fact)

    if incident is not None:
        incident_props = {key: value for key, value in (incident.row_data or {}).items()
                          if value is None or isinstance(value, (str, int, float, bool))}
        graph_nodes.append({"id": incident.id, "entity_type": "DynamicIncident",
                            "properties": {**incident_props, "_replay_id": replay.id,
                                           "_event_id": event.id, "event_ordinal": ordinal,
                                           "_graph_namespace": namespace}})
        triggered = _add_fact(db, replay, subject_id=observation_node,
                              predicate="TRIGGERS_INCIDENT", object_id=incident.id,
                              ordinal=ordinal, event=event, evidence=evidence, graph=graph)
        facts.append(triggered)
        graph_edges.append({"source": observation_node, "target": incident.id,
                            "type": "TRIGGERS_INCIDENT", "properties": {
                                "_fact_id": triggered.id, "_event_id": event.id,
                                "event_ordinal": ordinal, "valid_from_ordinal": ordinal,
                                "_replay_id": replay.id, "_graph_namespace": namespace}})
    state_map = dict(state_payload.get("episodes") or {})
    episode_state = dict(state_map.get(event.episode_id) or {})
    # LATEST_OBSERVATION is a new fact for each submitted event.
    previous_latest = _close_state_fact(db, replay, episode_node, "latest_observation", ordinal, graph)
    latest = _add_fact(db, replay, subject_id=episode_node, predicate="LATEST_OBSERVATION", object_id=observation_node, state_key="latest_observation", ordinal=ordinal, event=event, evidence=evidence, graph=graph)
    latest_props = {"_fact_id": latest.id, "_event_id": event.id, "event_ordinal": ordinal, "valid_from_ordinal": ordinal, "_replay_id": replay.id, "_graph_namespace": namespace}
    graph_edges.append({"source": episode_node, "target": observation_node, "type": "LATEST_OBSERVATION", "properties": latest_props})
    episode_state["latest_observation"] = {"value": observation_node, "fact_id": latest.id, "ordinal": ordinal}
    if previous_latest:
        episode_state["latest_previous_fact_id"] = previous_latest.id

    transitions = 0
    transition_config = {"phase": ("CURRENT_PHASE", "phase"), "tool_condition": ("CURRENT_TOOL_CONDITION", "tool_condition"), "inspection": ("CURRENT_INSPECTION", "inspection")}
    for state_key, (predicate, label) in transition_config.items():
        value_pair = _state_object(row, state_key)
        if value_pair is None:
            continue
        object_id, display_value = value_pair
        prior = dict(episode_state.get(state_key) or {})
        if prior.get("object_id") == object_id:
            # The fact remains valid; only the latest event evidence changes.
            continue
        _close_state_fact(db, replay, episode_node, state_key, ordinal, graph)
        current = _add_fact(db, replay, subject_id=episode_node, predicate=predicate, object_id=object_id, object_value={"value": display_value}, state_key=state_key, ordinal=ordinal, event=event, evidence=evidence, graph=graph)
        graph_edges.append({"source": episode_node, "target": object_id, "type": predicate, "properties": {"_fact_id": current.id, "_event_id": event.id, "event_ordinal": ordinal, "valid_from_ordinal": ordinal, "_replay_id": replay.id, "_graph_namespace": namespace, "value": display_value}})
        episode_state[state_key] = {"object_id": object_id, "value": display_value, "fact_id": current.id, "ordinal": ordinal}
        transitions += 1

    graph_nodes_written = _graph_upsert_instances(graph, replay.ontology_id, graph_nodes, namespace)
    graph_edges_written = _graph_upsert_relations(graph, replay.ontology_id, graph_edges, namespace)
    state_payload["builder"] = builder.to_dict()
    state_map[event.episode_id] = episode_state
    state_payload["episodes"] = state_map
    state_payload["latest_event"] = {"id": event.id, "event_key": event.event_key, "episode_id": event.episode_id, "ordinal": ordinal, "source_sequence": sequence, "payload": payload}
    replay.state = state_payload
    if replay.source_mode == "simulated_live":
        live_config = dict(replay.config or {})
        if live_config.get("first_received_ordinal") is None:
            live_config["first_received_ordinal"] = ordinal
        replay.config = live_config
    replay.current_time = ordinal
    replay.watermark_ordinal = Decimal(str(ordinal))
    replay.watermark_sequence = sequence
    replay.current_event_index = sequence
    replay.current_batch_index = sequence
    replay.committed_events = int(replay.committed_events or 0) + 1
    replay.normalized_rows = int(replay.normalized_rows or 0) + 1
    metrics = dict(replay.metrics or {})
    metrics.update({"events_total": replay.total_events, "events_committed": replay.committed_events, "events_received": replay.committed_events, "queue": max(0, replay.total_events - replay.committed_events), "nodes_written": int(metrics.get("nodes_written", 0)) + graph_nodes_written, "edges_written": int(metrics.get("edges_written", 0)) + graph_edges_written, "facts_written": int(metrics.get("facts_written", 0)) + len(facts) + 1 + transitions, "state_transitions": int(metrics.get("state_transitions", 0)) + transitions, "last_event_id": event.id, "last_event_ordinal": ordinal, "last_payload_hash": event.payload_hash})
    replay.metrics = metrics
    event.status = "committed"
    event.graph_projection_status = "projected" if graph and graph.available else "pending"
    event.graph_projected_at = _now() if graph and graph.available else None
    event.committed_at = _now()
    event.error = None
    # Keep the legacy batch checkpoint truthful when this event came from the
    # compatibility replay endpoint.  The dynamic worker remains event based,
    # but old clients can still inspect which batch is complete.
    source_ref = dict(event.source_ref or {})
    batch_value = source_ref.get("batch_no")
    if batch_value not in (None, ""):
        try:
            batch_no = int(batch_value)
        except (TypeError, ValueError):
            batch_no = None
        if batch_no is not None:
            batch = db.query(TemporalReplayBatch).filter(
                TemporalReplayBatch.replay_id == replay.id,
                TemporalReplayBatch.batch_no == batch_no,
            ).first()
            if batch:
                batch.status = "running"
                batch.normalized_rows = int(batch.normalized_rows or 0) + 1
                batch.nodes_written = int(batch.nodes_written or 0) + graph_nodes_written
                batch.edges_written = int(batch.edges_written or 0) + graph_edges_written
                batch.started_at = batch.started_at or _now()
                batch_event_rows = db.query(TemporalStreamEvent).filter(
                    TemporalStreamEvent.replay_id == replay.id,
                ).all()
                batch_events = [
                    item for item in batch_event_rows
                    if int((item.source_ref or {}).get("batch_no", -1)) == batch_no
                ]
                if batch_events and all(item.status == "committed" for item in batch_events):
                    batch.status = "completed"
                    batch.completed_at = _now()
                replay.current_batch_index = batch_no
                metrics["committed_batches"] = db.query(TemporalReplayBatch).filter(
                    TemporalReplayBatch.replay_id == replay.id,
                    TemporalReplayBatch.status == "completed",
                ).count()
    replay.updated_at = _now()
    if commit:
        db.commit()
        db.refresh(replay)
    return {"event": _serialize_event(event), "facts": [_serialize_fact(item) for item in facts], "nodes_written": graph_nodes_written, "edges_written": graph_edges_written, "state_transitions": transitions}


def _next_event(db: Session, replay_id: str) -> TemporalStreamEvent | None:
    return db.query(TemporalStreamEvent).filter(
        TemporalStreamEvent.replay_id == replay_id,
        TemporalStreamEvent.status == "queued",
    ).order_by(
        TemporalStreamEvent.ordinal.asc(),
        TemporalStreamEvent.source_sequence.asc(),
        TemporalStreamEvent.id.asc(),
    ).first()


def _next_simulated_live_event(db: Session, replay: TemporalReplay) -> TemporalStreamEvent | None:
    """Materialize only the next source row for the blind live demo.

    The source file is read by the producer, but future rows are never written
    to the event table or returned by the API.  A JSON cursor is sufficient for
    this deterministic local demo and survives worker restarts without adding a
    migration just for a source-specific offset.
    """
    if replay.source_mode != "simulated_live":
        return None
    config = dict(replay.config or {})
    if config.get("source_exhausted"):
        return None
    _dataset, version, rows = _resolve_source(db, replay.dataset_id, replay.dataset_version_id)
    selection = dict(config.get("selection") or {})
    episode_ids = [str(item) for item in (selection.get("episode_ids") or replay.series_ids or []) if str(item).strip()]
    candidates = _select_rows(
        rows,
        episode_ids=episode_ids,
        start_ordinal=_float_or_none(selection.get("start_ordinal")),
        end_ordinal=_float_or_none(selection.get("end_ordinal")),
        time_column=replay.time_column or "time_s",
        max_records=None,
    )
    cursor = max(0, int(config.get("source_cursor") or 0))
    if cursor >= len(candidates):
        config["source_exhausted"] = True
        replay.config = config
        replay.updated_at = _now()
        return None
    row, ordinal, source_row_id = candidates[cursor]
    episode = _episode(row)
    payload = {key: value for key, value in row.items() if not str(key).startswith("_")}
    entity_key = str(row.get("entity_key") or row.get("machine_type") or episode)
    event_key = f"factorynet:{episode}:{source_row_id}"
    source_ref = {
        "dataset_id": replay.dataset_id,
        "dataset_version_id": replay.dataset_version_id,
        "source_row_id": source_row_id,
        "source_file": config.get("source_file"),
        "source_mode": "simulated_live",
    }
    payload_hash = temporal_event_envelope_hash(
        replay, event_key=event_key, operation="upsert", episode_id=episode,
        entity_key=entity_key, ordinal=ordinal, source_sequence=cursor,
        source_row_id=source_row_id, payload=payload, source_ref=source_ref,
    )
    event = TemporalStreamEvent(
        id=str(uuid.uuid4()),
        replay_id=replay.id,
        event_key=event_key,
        episode_id=episode,
        entity_key=entity_key,
        ordinal=Decimal(str(ordinal)),
        source_sequence=cursor,
        source_row_id=source_row_id,
        payload=payload,
        source_ref=source_ref,
        payload_hash=payload_hash,
        payload_hash_version=TEMPORAL_EVENT_HASH_VERSION,
        status="queued",
    )
    db.add(event)
    db.flush()
    # The cursor is advanced in the same transaction as the event and facts by
    # run_temporal_stream after process_one_event succeeds.
    config["source_cursor_pending"] = cursor + 1
    replay.config = config
    replay.total_events = int(replay.total_events or 0) + 1
    replay.selected_rows = int(replay.selected_rows or 0) + 1
    replay.total_batches = int(replay.total_batches or 0) + 1
    if replay.start_time is None:
        replay.start_time = ordinal
    replay.updated_at = _now()
    return event


class TemporalStreamLeaseLost(RuntimeError):
    """Raised when a stream worker no longer owns its durable lease."""


class _TemporalStreamHeartbeat:
    def __init__(self, db: Session, replay_id: str, token: str, generation: int):
        self.db = db
        self.replay_id = replay_id
        self.token = token
        self.generation = generation
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._before_commit = self._fence_commit

    def _where(self, now: datetime):
        return (
            TemporalReplay.id == self.replay_id,
            TemporalReplay.status.in_(("running", "pausing")),
            TemporalReplay.worker_token == self.token,
            TemporalReplay.worker_generation == self.generation,
            TemporalReplay.worker_lease_expires_at > now,
        )

    def _renew_on_connection(self, db: Session) -> None:
        if self._lost.is_set():
            raise TemporalStreamLeaseLost(f"Temporal stream {self.replay_id} lease was lost")
        now = _now()
        result = db.connection().execute(
            update(TemporalReplay).where(*self._where(now)).values(
                heartbeat_at=now,
                worker_lease_expires_at=now + timedelta(seconds=STREAM_RUN_LEASE_SECONDS),
            )
        )
        if result.rowcount != 1:
            self._lost.set()
            raise TemporalStreamLeaseLost(f"Temporal stream {self.replay_id} lease was lost")

    def _fence_commit(self, session: Session) -> None:
        self._renew_on_connection(session)

    def start(self) -> "_TemporalStreamHeartbeat":
        sqlalchemy_event.listen(self.db, "before_commit", self._before_commit)
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"temporal-stream-heartbeat-{self.replay_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def assert_owned(self) -> None:
        self._renew_on_connection(self.db)

    def _heartbeat_loop(self) -> None:
        from app.database import SessionLocal

        while not self._stop.wait(STREAM_HEARTBEAT_SECONDS):
            now = _now()
            try:
                with SessionLocal() as heartbeat_db:
                    renewed = heartbeat_db.query(TemporalReplay).filter(
                        *self._where(now)
                    ).update({
                        "heartbeat_at": now,
                        "worker_lease_expires_at": now + timedelta(seconds=STREAM_RUN_LEASE_SECONDS),
                    }, synchronize_session=False)
                    heartbeat_db.commit()
                if renewed != 1:
                    self._lost.set()
                    return
            except Exception:
                # The business transaction's next commit performs a fencing
                # update; a transient heartbeat outage cannot authorize stale work.
                continue

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            sqlalchemy_event.remove(self.db, "before_commit", self._before_commit)
        except Exception:
            pass


def _release_stream_lease(replay: TemporalReplay) -> None:
    replay.worker_token = None
    replay.heartbeat_at = None
    replay.worker_lease_expires_at = None


def _claim_stream_worker(replay_id: str) -> tuple[str | None, int | None, str | None]:
    from app.database import SessionLocal

    now = _now()
    with SessionLocal() as db:
        replay = db.query(TemporalReplay).filter(
            TemporalReplay.id == replay_id,
        ).with_for_update().first()
        if replay is None:
            return None, None, "missing"
        if replay.status in {"published", "completed", "failed", "cancelled"}:
            return None, None, replay.status
        lease_until = _aware(replay.worker_lease_expires_at)
        if replay.status in {"running", "pausing"} and lease_until and lease_until > now:
            return None, None, "already_running"
        if replay.status not in {"created", "queued", "paused", "running", "pausing"}:
            return None, None, replay.status
        retry_at = _aware(replay.next_attempt_at)
        if retry_at and retry_at > now:
            return None, None, "retry_wait"

        old_token = replay.worker_token
        if old_token:
            abandoned = db.query(TemporalProjectionAttempt).filter(
                TemporalProjectionAttempt.replay_id == replay.id,
                TemporalProjectionAttempt.worker_token == old_token,
                TemporalProjectionAttempt.status == "started",
            ).with_for_update().all()
            for attempt in abandoned:
                attempt.status = "abandoned"
                attempt.error_code = "worker_lease_expired"
                attempt.error_message = "The worker lease expired before this projection committed"
                attempt.finished_at = now
                event_row = db.get(TemporalStreamEvent, attempt.event_id)
                if event_row and event_row.status == "processing":
                    event_row.status = "queued"
        token = str(uuid.uuid4())
        replay.worker_generation = int(replay.worker_generation or 0) + 1
        replay.worker_token = token
        replay.heartbeat_at = now
        replay.worker_lease_expires_at = now + timedelta(seconds=STREAM_RUN_LEASE_SECONDS)
        replay.dispatch_lease_until = None
        replay.next_attempt_at = None
        replay.status = "running"
        replay.started_at = replay.started_at or now
        replay.updated_at = now
        db.commit()
        return token, int(replay.worker_generation), None


def _start_projection_attempt(
    db: Session,
    replay: TemporalReplay,
    event_row: TemporalStreamEvent,
    token: str,
) -> TemporalProjectionAttempt | None:
    db.flush()
    attempts = db.query(TemporalProjectionAttempt).filter(
        TemporalProjectionAttempt.event_id == event_row.id,
        TemporalProjectionAttempt.retry_generation == int(event_row.retry_generation or 0),
    ).order_by(TemporalProjectionAttempt.attempt_no.asc()).with_for_update().all()
    authorized = next((item for item in attempts if item.status == "authorized" and item.attempt_type == "manual"), None)
    if authorized is not None:
        attempt = authorized
        attempt.status = "started"
        attempt.worker_token = token
        attempt.started_at = _now()
        attempt.error_code = None
        attempt.error_message = None
    else:
        used = sum(1 for item in attempts if item.status != "authorized")
        if used >= STREAM_MAX_PROJECTION_ATTEMPTS:
            latest = next((item for item in reversed(attempts) if item.error_message), None)
            message = latest.error_message if latest else "事件投影多次失败，请检查错误码和数据版本。"
            event_row.status = "failed"
            event_row.error = "PROJECTION_ATTEMPTS_EXHAUSTED"
            replay.status = "failed"
            replay.error = "PROJECTION_ATTEMPTS_EXHAUSTED"
            replay.completed_at = _now()
            _release_stream_lease(replay)
            replay.updated_at = _now()
            db.commit()
            return None
        attempt_no = int(db.query(func.max(TemporalProjectionAttempt.attempt_no)).filter(
            TemporalProjectionAttempt.event_id == event_row.id,
        ).scalar() or 0) + 1
        attempt = TemporalProjectionAttempt(
            id=str(uuid.uuid4()), replay_id=replay.id, event_id=event_row.id,
            attempt_no=attempt_no, retry_generation=int(event_row.retry_generation or 0),
            attempt_type="automatic", status="started", worker_token=token,
            created_at=_now(), started_at=_now(),
        )
        db.add(attempt)
    event_row.status = "processing"
    event_row.error = None
    event_row.next_attempt_at = None
    replay.next_attempt_at = None
    replay.status = "running"
    replay.error = None
    replay.updated_at = _now()
    db.commit()
    return attempt


def _record_projection_failure(
    replay_id: str,
    event_id: str,
    attempt_id: str,
    token: str,
    exc: Exception,
) -> str:
    from app.database import SessionLocal

    now = _now()
    raw_code = exc.code if isinstance(exc, StreamError) else "projection_error"
    code, message = temporal_error_details(raw_code, fallback_code="TEMPORAL_PROJECTION_FAILED")
    with SessionLocal() as db:
        replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).with_for_update().first()
        event_row = db.query(TemporalStreamEvent).filter(
            TemporalStreamEvent.id == event_id,
            TemporalStreamEvent.replay_id == replay_id,
        ).with_for_update().first()
        attempt = db.query(TemporalProjectionAttempt).filter(
            TemporalProjectionAttempt.id == attempt_id,
            TemporalProjectionAttempt.worker_token == token,
            TemporalProjectionAttempt.status == "started",
        ).with_for_update().first()
        if (replay is None or event_row is None or attempt is None
                or replay.status not in {"running", "pausing"}
                or replay.worker_token != token
                or not _aware(replay.worker_lease_expires_at)
                or _aware(replay.worker_lease_expires_at) <= now):
            return "superseded"
        attempt.status = "failed"
        attempt.error_code = code
        attempt.error_message = message
        attempt.finished_at = now
        db.flush()
        generation_count = db.query(func.count(TemporalProjectionAttempt.id)).filter(
            TemporalProjectionAttempt.event_id == event_row.id,
            TemporalProjectionAttempt.retry_generation == int(event_row.retry_generation or 0),
            TemporalProjectionAttempt.status != "authorized",
        ).scalar() or 0
        retryable = not isinstance(exc, StreamError) or raw_code in {"STORAGE_OBJECT_MISSING", "SOURCE_NOT_READY"}
        if retryable and int(generation_count) < STREAM_MAX_PROJECTION_ATTEMPTS:
            delay_seconds = min(5 * (2 ** max(0, int(generation_count) - 1)), 60)
            retry_at = now + timedelta(seconds=delay_seconds)
            event_row.status = "queued"
            event_row.error = code
            event_row.next_attempt_at = retry_at
            replay.status = "queued"
            replay.error = code
            replay.next_attempt_at = retry_at
            replay.completed_at = None
            result = "retry_wait"
        else:
            event_row.status = "failed"
            event_row.error = code
            event_row.next_attempt_at = None
            replay.status = "failed"
            replay.error = code
            replay.next_attempt_at = None
            replay.completed_at = now
            result = "failed"
        _release_stream_lease(replay)
        replay.updated_at = now
        db.commit()
        return result


def _record_stream_failure(replay_id: str, token: str, exc: Exception) -> str:
    from app.database import SessionLocal

    now = _now()
    raw_code = exc.code if isinstance(exc, StreamError) else "stream_worker_error"
    code, _ = temporal_error_details(raw_code, fallback_code="TEMPORAL_STREAM_FAILED")
    with SessionLocal() as db:
        replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).with_for_update().first()
        if (replay is None or replay.worker_token != token
                or replay.status not in {"running", "pausing"}
                or not _aware(replay.worker_lease_expires_at)
                or _aware(replay.worker_lease_expires_at) <= now):
            return "superseded"
        replay.status = "failed"
        replay.error = code
        replay.completed_at = now
        replay.next_attempt_at = None
        _release_stream_lease(replay)
        replay.updated_at = now
        db.commit()
        return "failed"


def run_temporal_stream(replay_id: str) -> dict[str, Any]:
    """Process a stream under a persisted, heartbeated, fenced worker lease."""
    with _worker_lock:
        if replay_id in _active_stream_workers:
            return {"run_id": replay_id, "status": "already_running"}
        _active_stream_workers.add(replay_id)
    from app.database import SessionLocal

    db = SessionLocal()
    lease: _TemporalStreamHeartbeat | None = None
    try:
        token, generation, stopped = _claim_stream_worker(replay_id)
        if token is None:
            replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).first()
            return serialize_stream(replay, db=db) if replay else {"run_id": replay_id, "status": stopped or "missing"}
        lease = _TemporalStreamHeartbeat(db, replay_id, token, int(generation or 0)).start()
        replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).first()
        if replay is None:
            return {"run_id": replay_id, "status": "missing"}
        while True:
            db.refresh(replay)
            if replay.cancel_requested or replay.pause_requested:
                db.rollback()
                replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).first()
                if replay is None:
                    return {"run_id": replay_id, "status": "missing"}
                if replay.cancel_requested:
                    replay.status = "cancelled"
                    replay.error = "TEMPORAL_REPLAY_CANCELLED"
                    replay.completed_at = _now()
                else:
                    replay.status = "paused"
                _release_stream_lease(replay)
                replay.updated_at = _now()
                db.commit()
                return serialize_stream(replay, db=db)

            # A failed simulated-live event must be retried before another
            # source row is materialized; otherwise its pending cursor repeats.
            stream_event = _next_event(db, replay.id)
            event_row = stream_event or (
                _next_simulated_live_event(db, replay)
                if replay.source_mode == "simulated_live" else None
            )
            if event_row is None:
                if replay.source_mode == "push":
                    replay.status = "paused"
                    replay.error = "TEMPORAL_REPLAY_WAITING_FOR_EVENT"
                    replay.completed_at = None
                else:
                    replay.status = "completed"
                    replay.current_event_index = max(-1, int(replay.total_events or 0) - 1)
                    replay.current_batch_index = replay.current_event_index
                    replay.completed_at = _now()
                _release_stream_lease(replay)
                replay.updated_at = _now()
                db.commit()
                return serialize_stream(replay, db=db)

            retry_at = _aware(event_row.next_attempt_at)
            if retry_at and retry_at > _now():
                replay.status = "queued"
                replay.next_attempt_at = retry_at
                _release_stream_lease(replay)
                replay.updated_at = _now()
                db.commit()
                return serialize_stream(replay, db=db)

            with db.no_autoflush:
                flags = db.query(
                    TemporalReplay.pause_requested,
                    TemporalReplay.cancel_requested,
                ).filter(TemporalReplay.id == replay_id).first()
            if flags and (bool(flags[0]) or bool(flags[1])):
                db.rollback()
                replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).first()
                if replay is None:
                    return {"run_id": replay_id, "status": "missing"}
                replay.status = "cancelled" if replay.cancel_requested else "paused"
                if replay.cancel_requested:
                    replay.error = "TEMPORAL_REPLAY_CANCELLED"
                    replay.completed_at = _now()
                _release_stream_lease(replay)
                replay.updated_at = _now()
                db.commit()
                return serialize_stream(replay, db=db)

            replay.status = "running"
            attempt = _start_projection_attempt(db, replay, event_row, token)
            if attempt is None:
                db.refresh(replay)
                return serialize_stream(replay, db=db)
            try:
                lease.assert_owned()
                process_one_event(db, replay, event_row, commit=False)
                if replay.source_mode == "simulated_live":
                    config = dict(replay.config or {})
                    pending_cursor = config.pop("source_cursor_pending", None)
                    if pending_cursor is not None:
                        config["source_cursor"] = int(pending_cursor)
                    replay.config = config
                attempt.status = "committed"
                attempt.finished_at = _now()
                attempt.error_code = None
                attempt.error_message = None
                db.commit()
                db.refresh(replay)
            except TemporalStreamLeaseLost:
                db.rollback()
                return {"run_id": replay_id, "status": "superseded"}
            except Exception as exc:
                db.rollback()
                lease.close()
                lease = None
                _record_projection_failure(replay_id, event_row.id, attempt.id, token, exc)
                db.expire_all()
                replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).first()
                return serialize_stream(replay, db=db) if replay else {
                    "run_id": replay_id, "status": "failed",
                    "error": "时序流任务未能完成，请检查错误码并重试。",
                    "error_code": "TEMPORAL_STREAM_FAILED",
                }

            db.refresh(replay)
            if replay.step_requested:
                replay.step_requested = False
                replay.pause_requested = True
                replay.status = "paused"
                _release_stream_lease(replay)
                replay.updated_at = _now()
                db.commit()
                return serialize_stream(replay, db=db)
            if replay.pause_requested:
                replay.status = "paused"
                _release_stream_lease(replay)
                replay.updated_at = _now()
                db.commit()
                return serialize_stream(replay, db=db)
            delay = max(0.01, min(60.0, float(replay.event_interval_ms or 1000) / 1000.0))
            if delay:
                time.sleep(delay)
    except TemporalStreamLeaseLost:
        db.rollback()
        return {"run_id": replay_id, "status": "superseded"}
    except Exception as exc:
        db.rollback()
        if lease is not None:
            lease.close()
            lease = None
        if "token" in locals() and token:
            _record_stream_failure(replay_id, token, exc)
        replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).first()
        return serialize_stream(replay, db=db) if replay else {
            "run_id": replay_id, "status": "failed",
            "error": "时序流任务未能完成，请检查错误码并重试。",
            "error_code": "TEMPORAL_STREAM_FAILED",
        }
    finally:
        if lease is not None:
            lease.close()
        db.close()
        with _worker_lock:
            _active_stream_workers.discard(replay_id)


def dispatch_stream(replay_id: str) -> str:
    """Dispatch a queued run with a persisted lease and bounded backoff."""
    from app.database import SessionLocal

    now = _now()
    with SessionLocal() as db:
        replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).with_for_update().first()
        if replay is None:
            return "missing"
        if replay.status != "queued":
            return str(replay.status)
        retry_at = _aware(replay.next_attempt_at)
        dispatch_lease = _aware(replay.dispatch_lease_until)
        if retry_at and retry_at > now:
            return "retry_wait"
        if dispatch_lease and dispatch_lease > now:
            return "dispatch_pending"
        if int(replay.dispatch_attempts or 0) >= STREAM_MAX_DISPATCH_ATTEMPTS:
            replay.status = "failed"
            replay.error = "dispatch_attempts_exhausted"
            replay.completed_at = now
            replay.updated_at = now
            db.commit()
            return "failed"
        replay.dispatch_attempts = int(replay.dispatch_attempts or 0) + 1
        attempt_no = replay.dispatch_attempts
        task_id = f"temporal-stream-{replay.id}-{attempt_no}"
        replay.dispatch_lease_until = now + timedelta(seconds=STREAM_DISPATCH_LEASE_SECONDS)
        replay.updated_at = now
        db.commit()

    try:
        if os.getenv("CELERY_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
            from app.tasks.v2.temporal_stream import run_temporal_stream_task
            run_temporal_stream_task.apply_async(args=[replay_id], task_id=task_id)
            return "celery"
        thread = threading.Thread(
            target=run_temporal_stream,
            args=(replay_id,),
            name=f"temporal-stream-{replay_id[:8]}",
            daemon=True,
        )
        thread.start()
        return "thread"
    except Exception as exc:
        with SessionLocal() as db:
            replay = db.query(TemporalReplay).filter(TemporalReplay.id == replay_id).with_for_update().first()
            if replay and replay.status == "queued" and int(replay.dispatch_attempts or 0) == attempt_no:
                replay.dispatch_lease_until = None
                if attempt_no >= STREAM_MAX_DISPATCH_ATTEMPTS:
                    replay.status = "failed"
                    replay.error = "dispatch_attempts_exhausted"
                    replay.completed_at = _now()
                    state = "failed"
                else:
                    replay.error = "dispatch_failed"
                    replay.next_attempt_at = _now() + timedelta(seconds=min(10 * (2 ** (attempt_no - 1)), 300))
                    state = "dispatch_failed"
                replay.updated_at = _now()
                db.commit()
                return state
        return "superseded"


def reconcile_temporal_stream_workers(batch_size: int = 50) -> dict[str, Any]:
    """Recover expired workers and redeliver queued runs whose retry is due."""
    from app.database import SessionLocal

    batch_size = max(1, min(int(batch_size), 200))
    now = _now()
    with SessionLocal() as db:
        expired = db.query(TemporalReplay).filter(
            TemporalReplay.status.in_(("running", "pausing")),
            or_(TemporalReplay.worker_lease_expires_at.is_(None), TemporalReplay.worker_lease_expires_at <= now),
        ).order_by(TemporalReplay.updated_at.asc(), TemporalReplay.id.asc()).limit(batch_size).with_for_update().all()
        for replay in expired:
            old_token = replay.worker_token
            if old_token:
                attempts = db.query(TemporalProjectionAttempt).filter(
                    TemporalProjectionAttempt.replay_id == replay.id,
                    TemporalProjectionAttempt.worker_token == old_token,
                    TemporalProjectionAttempt.status == "started",
                ).with_for_update().all()
                for attempt in attempts:
                    attempt.status = "abandoned"
                    attempt.error_code, attempt.error_message = temporal_error_details("worker_lease_expired")
                    attempt.finished_at = now
                    event_row = db.get(TemporalStreamEvent, attempt.event_id)
                    if event_row and event_row.status == "processing":
                        event_row.status = "queued"
            if replay.cancel_requested:
                replay.status = "cancelled"
                replay.error = "TEMPORAL_REPLAY_CANCELLED"
                replay.completed_at = now
            elif replay.pause_requested:
                replay.status = "paused"
            else:
                replay.status = "queued"
                replay.next_attempt_at = now
            _release_stream_lease(replay)
            replay.updated_at = now
        db.commit()
        due_ids = [row.id for row in db.query(TemporalReplay.id).filter(
            TemporalReplay.status == "queued",
            or_(TemporalReplay.next_attempt_at.is_(None), TemporalReplay.next_attempt_at <= now),
            or_(TemporalReplay.dispatch_lease_until.is_(None), TemporalReplay.dispatch_lease_until <= now),
        ).order_by(TemporalReplay.created_at.asc(), TemporalReplay.id.asc()).limit(batch_size).all()]
    states: Counter[str] = Counter()
    for replay_id in due_ids:
        states[dispatch_stream(replay_id)] += 1
    return {"expired_workers": len(expired), "due_runs": len(due_ids), "dispatch_states": dict(states)}


def temporal_graph_projection_health(db: Session, replay: TemporalReplay) -> dict[str, Any]:
    """Compare a replay's SQL fact envelope with its isolated graph counts."""
    subject_ids = db.query(TemporalFact.subject_id.label("node_id")).filter(
        TemporalFact.replay_id == replay.id,
    )
    object_ids = db.query(TemporalFact.object_id.label("node_id")).filter(
        TemporalFact.replay_id == replay.id,
        TemporalFact.object_id.is_not(None),
    )
    node_ids = subject_ids.union(object_ids).subquery()
    expected_nodes = int(db.query(func.count()).select_from(node_ids).scalar() or 0)
    expected_edges = int(db.query(func.count(TemporalFact.id)).filter(
        TemporalFact.replay_id == replay.id,
        TemporalFact.object_id.is_not(None),
    ).scalar() or 0)
    committed_events = int(db.query(func.count(TemporalStreamEvent.id)).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
    ).scalar() or 0)
    projected_events = int(db.query(func.count(TemporalStreamEvent.id)).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
        TemporalStreamEvent.graph_projection_status == "projected",
    ).scalar() or 0)
    pending_events = max(0, committed_events - projected_events)

    graph = FalkorDBService()
    actual = graph.projection_counts(replay.ontology_id, replay.graph_namespace) if graph.available else None
    latest_rebuild = db.query(TemporalGraphProjectionJob).filter(
        TemporalGraphProjectionJob.replay_id == replay.id,
    ).order_by(TemporalGraphProjectionJob.created_at.desc()).first()
    if not graph.available:
        status = "unavailable"
    elif latest_rebuild and latest_rebuild.status in {"queued", "running"}:
        status = "rebuilding"
    elif latest_rebuild and latest_rebuild.status == "failed":
        status = "rebuild_failed"
    elif actual is None:
        status = "missing_graph" if expected_nodes or expected_edges else "empty"
    elif int(actual.get("nodes", 0)) == expected_nodes and int(actual.get("edges", 0)) == expected_edges and pending_events == 0:
        status = "content_verified" if latest_rebuild and latest_rebuild.status == "completed" and latest_rebuild.content_digest else "count_match"
    else:
        status = "lag_or_count_mismatch"
    rebuild_payload = None
    if latest_rebuild:
        from app.services.v2.temporal_graph_projection import serialize_temporal_graph_projection_job

        rebuild_payload = serialize_temporal_graph_projection_job(latest_rebuild)
    return {
        "run_id": replay.id,
        "source_of_truth": "postgresql_temporal_facts",
        "graph_namespace": replay.graph_namespace,
        "status": status,
        "committed_events": committed_events,
        "events_marked_projected": projected_events,
        "events_pending_or_unknown": pending_events,
        "sql_expected_nodes": expected_nodes,
        "sql_expected_edges": expected_edges,
        "graph_counts": actual,
        "count_match_is_not_content_proof": True,
        "last_rebuild": rebuild_payload,
        "last_rebuild_content_verified": bool(latest_rebuild and latest_rebuild.status == "completed" and latest_rebuild.content_digest),
        "checked_at": _now().isoformat(),
    }


def update_stream_control(db: Session, replay: TemporalReplay, action: str, speed: float | None = None) -> tuple[TemporalReplay, bool]:
    replay = db.query(TemporalReplay).filter(
        TemporalReplay.id == replay.id,
    ).populate_existing().with_for_update().first()
    if replay is None:
        raise StreamError("动态运行不存在", "RUN_NOT_FOUND")
    if action not in {"start", "pause", "resume", "step", "cancel"}:
        raise StreamError("action 必须是 start、pause、resume、step 或 cancel", "INVALID_CONTROL")
    if speed is not None:
        speed = _finite_number(speed, "speed")
        if speed <= 0 or speed > MAX_SPEED:
            raise StreamError(f"speed 必须在 0 和 {MAX_SPEED} 之间", "INVALID_SPEED")
        replay.speed = speed
        replay.event_interval_ms = max(1, int(round(1000 / speed)))
    if replay.status == "published" and action != "cancel":
        raise StreamError("已发布运行不能继续控制", "RUN_PUBLISHED")
    if action in {"start", "resume", "step"} and replay.status in {"running", "pausing"}:
        lease_until = _aware(replay.worker_lease_expires_at)
        if lease_until and lease_until > _now():
            raise StreamError("当前运行已有持久 worker 正在处理", "RUN_ALREADY_ACTIVE")
    if action in {"start", "resume", "step"} and db.query(TemporalStreamEvent.id).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "failed",
    ).first():
        raise StreamError("存在失败事件；请通过带原因和幂等键的人工重放接口重新授权", "MANUAL_REPLAY_REQUIRED")
    if action == "start":
        replay.cancel_requested = False; replay.pause_requested = False; replay.step_requested = False; replay.status = "queued"
        replay.dispatch_attempts = 0
        replay.dispatch_lease_until = None
        replay.next_attempt_at = None
        replay.error = None
        dispatch = True
    elif action == "resume":
        replay.cancel_requested = False; replay.pause_requested = False; replay.step_requested = False; replay.status = "queued"
        replay.dispatch_lease_until = None
        replay.next_attempt_at = None
        replay.error = None
        dispatch = True
    elif action == "step":
        replay.cancel_requested = False; replay.pause_requested = False; replay.step_requested = True; replay.status = "queued"
        replay.dispatch_lease_until = None
        replay.next_attempt_at = None
        dispatch = True
    elif action == "pause":
        was_active = replay.status in {"running", "pausing"}
        replay.pause_requested = True
        replay.status = "pausing" if was_active else "paused"
        if not was_active:
            replay.dispatch_lease_until = None
        dispatch = False
    else:
        was_active = replay.status in {"running", "pausing"}
        replay.cancel_requested = True; replay.pause_requested = False; replay.step_requested = False
        replay.status = "pausing" if was_active else "cancelled"
        if not was_active:
            _release_stream_lease(replay)
            replay.dispatch_lease_until = None
            replay.completed_at = _now()
        dispatch = False
    replay.updated_at = _now()
    db.commit(); db.refresh(replay)
    return replay, dispatch


def authorize_temporal_event_retry(
    db: Session,
    replay: TemporalReplay,
    event_ref: str,
    *,
    requested_by: str,
    reason: str,
    request_key: str,
) -> tuple[TemporalProjectionAttempt, bool]:
    """Authorize one auditable manual retry for a terminally failed event."""
    normalized_reason = str(reason or "").strip()
    normalized_key = str(request_key or "").strip()
    if len(normalized_reason) < 8:
        raise StreamError("人工重放原因至少需要 8 个字符", "RETRY_REASON_REQUIRED")
    if not normalized_key or len(normalized_key) > 120:
        raise StreamError("人工重放幂等键长度必须在 1 到 120 之间", "INVALID_RETRY_REQUEST_KEY")

    locked_replay = db.query(TemporalReplay).filter(
        TemporalReplay.id == replay.id,
    ).populate_existing().with_for_update().first()
    if locked_replay is None:
        raise StreamError("动态运行不存在", "RUN_NOT_FOUND")
    event_row = db.query(TemporalStreamEvent).filter(
        TemporalStreamEvent.replay_id == locked_replay.id,
        or_(TemporalStreamEvent.id == event_ref, TemporalStreamEvent.event_key == event_ref),
    ).with_for_update().first()
    if event_row is None:
        raise StreamError("动态事件不存在", "EVENT_NOT_FOUND")

    existing = db.query(TemporalProjectionAttempt).filter(
        TemporalProjectionAttempt.replay_id == locked_replay.id,
        TemporalProjectionAttempt.request_key == normalized_key,
    ).first()
    if existing is not None:
        if (existing.event_id != event_row.id
                or existing.attempt_type != "manual"
                or existing.reason != normalized_reason
                or existing.requested_by != requested_by):
            raise StreamError("幂等键已用于其他重放请求", "IDEMPOTENCY_KEY_REUSED")
        return existing, True
    if event_row.status != "failed":
        raise StreamError("只有终态失败事件可以人工重放", "EVENT_NOT_TERMINAL_FAILED")
    if locked_replay.status in {"published", "cancelled"}:
        raise StreamError("已发布或已取消的运行不能人工重放", "RUN_PUBLISHED")

    attempt_no = int(db.query(func.max(TemporalProjectionAttempt.attempt_no)).filter(
        TemporalProjectionAttempt.event_id == event_row.id,
    ).scalar() or 0) + 1
    retry_generation = int(event_row.retry_generation or 0) + 1
    now = _now()
    attempt = TemporalProjectionAttempt(
        id=str(uuid.uuid4()),
        replay_id=locked_replay.id,
        event_id=event_row.id,
        attempt_no=attempt_no,
        retry_generation=retry_generation,
        attempt_type="manual",
        status="authorized",
        request_key=normalized_key,
        requested_by=requested_by,
        reason=normalized_reason,
        created_at=now,
    )
    db.add(attempt)
    event_row.retry_generation = retry_generation
    event_row.status = "queued"
    event_row.error = None
    event_row.next_attempt_at = None
    locked_replay.status = "queued"
    locked_replay.pause_requested = False
    locked_replay.cancel_requested = False
    locked_replay.step_requested = False
    locked_replay.error = None
    locked_replay.completed_at = None
    locked_replay.dispatch_attempts = 0
    locked_replay.dispatch_lease_until = None
    locked_replay.next_attempt_at = None
    locked_replay.updated_at = now
    db.commit()
    db.refresh(attempt)
    return attempt, False


def ingest_push_event(db: Session, replay: TemporalReplay, body: dict[str, Any]) -> tuple[TemporalStreamEvent, bool]:
    locked_replay = db.query(TemporalReplay).filter(
        TemporalReplay.id == replay.id,
    ).populate_existing().with_for_update().first()
    if locked_replay is None:
        raise StreamError("动态运行不存在", "RUN_NOT_FOUND")
    replay = locked_replay
    if replay.source_mode != "push":
        raise StreamError("文件回放运行不能接收推送事件", "SOURCE_MODE_MISMATCH")
    if replay.status in {"published", "cancelled"}:
        raise StreamError("当前运行已结束，不能接收新事件", "RUN_PUBLISHED" if replay.status == "published" else "RUN_CANCELLED")
    operation = str(body.get("operation") or "upsert")
    if operation not in {"upsert", "delete"}:
        raise StreamError("operation 必须是 upsert 或 delete", "UNSUPPORTED_OPERATION")
    if replay.source_id == FACTORYNET_SOURCE_ID and operation != "upsert":
        raise StreamError("FactoryNet observation events only support upsert", "UNSUPPORTED_OPERATION")
    required = ["event_id", "episode_id", "entity_key", "source_sequence", "event_time" if replay.time_kind == "event_time" else "ordinal"]
    missing = [key for key in required if body.get(key) in (None, "")]
    if missing:
        raise StreamError(f"缺少事件字段：{', '.join(missing)}", "INVALID_EVENT")
    event_id = str(body["event_id"])
    payload_value = body.get("payload", {})
    if payload_value is None:
        payload_value = {}
    if not isinstance(payload_value, dict):
        raise StreamError("payload 必须是对象", "INVALID_EVENT")
    payload = payload_value
    source_ref_value = body.get("source_ref", {})
    if source_ref_value is None:
        source_ref_value = {}
    if not isinstance(source_ref_value, dict):
        raise StreamError("source_ref 必须是对象", "INVALID_EVENT")
    source_ref = dict(source_ref_value)
    if replay.time_kind == "event_time":
        event_time = _parse_event_time(body["event_time"])
        ordinal = event_time.timestamp()
    else:
        ordinal = _finite_number(body["ordinal"])
    try:
        raw_source_sequence = body["source_sequence"]
        sequence_decimal = Decimal(str(raw_source_sequence))
        if (isinstance(raw_source_sequence, bool)
                or not sequence_decimal.is_finite()
                or sequence_decimal != sequence_decimal.to_integral_value()):
            raise ValueError("source_sequence must be an integer")
        source_sequence = int(sequence_decimal)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StreamError("source_sequence 必须是整数", "INVALID_EVENT") from exc
    if source_sequence < 0:
        raise StreamError("source_sequence 不能小于 0", "INVALID_EVENT")
    episode_id = str(body["episode_id"])
    entity_key = str(body["entity_key"])
    source_row_id = str(source_ref.get("source_row_id") or event_id)
    source_ref["source_row_id"] = source_row_id
    if replay.time_kind == "event_time":
        source_ref["event_time"] = event_time.isoformat()
        source_ref["time_zone"] = str(event_time.tzinfo)
    event_fields = {
        "event_key": event_id,
        "operation": operation,
        "episode_id": episode_id,
        "entity_key": entity_key,
        "ordinal": ordinal,
        "source_sequence": source_sequence,
        "source_row_id": source_row_id,
        "payload": payload,
        "source_ref": source_ref,
    }
    payload_hash = temporal_event_envelope_hash(replay, **event_fields)
    existing = db.query(TemporalStreamEvent).filter(TemporalStreamEvent.replay_id == replay.id, TemporalStreamEvent.event_key == event_id).first()
    if existing:
        existing_version = _stored_contract_version(existing, "payload_hash_version")
        existing_fields = {
            "event_key": existing.event_key,
            "operation": existing.operation or "upsert",
            "episode_id": existing.episode_id,
            "entity_key": existing.entity_key,
            "ordinal": existing.ordinal,
            "source_sequence": existing.source_sequence,
            "source_row_id": existing.source_row_id or existing.event_key,
            "payload": existing.payload or {},
            "source_ref": existing.source_ref or {},
        }
        # v1 push rows stored the caller's source_ref verbatim (plus normalized
        # event_time/time_zone), before source_row_id was copied into it. Keep
        # that persisted representation for compatibility comparisons.
        legacy_source_ref = dict(source_ref_value)
        if replay.time_kind == "event_time":
            legacy_source_ref["event_time"] = event_time.isoformat()
            legacy_source_ref["time_zone"] = str(event_time.tzinfo)
        legacy_event_fields = {**event_fields, "source_ref": legacy_source_ref}
        if existing_version == 1:
            exact_envelope_match = temporal_event_envelope_matches(
                replay, existing, **legacy_event_fields,
            )
            stored_hash_valid = True
        elif existing_version == TEMPORAL_EVENT_HASH_VERSION:
            exact_envelope_match = temporal_event_envelope_matches(
                replay, existing, **event_fields,
            )
            stored_hash_valid = temporal_event_envelope_hash(
                replay, **existing_fields,
            ) == existing.payload_hash
        else:
            exact_envelope_match = False
            stored_hash_valid = False
        if exact_envelope_match and stored_hash_valid:
            return existing, True
        raise StreamError("相同 event_id 的完整事件 envelope 不同", "EVENT_PAYLOAD_CONFLICT", event_id=event_id)
    watermark = _float_or_none(replay.watermark_ordinal)
    if watermark is not None and (ordinal, source_sequence) <= (watermark, int(replay.watermark_sequence or -1)):
        raise StreamError("事件早于或等于当前水位，不能在本版本重算", "LATE_EVENT_NOT_SUPPORTED", watermark_ordinal=watermark, watermark_sequence=replay.watermark_sequence)
    event = TemporalStreamEvent(
        id=str(uuid.uuid4()), replay_id=replay.id, event_key=event_id,
        operation=operation,
        episode_id=episode_id, entity_key=entity_key,
        ordinal=Decimal(str(ordinal)), source_sequence=source_sequence,
        source_row_id=source_row_id,
        payload=payload, source_ref=source_ref, payload_hash=payload_hash,
        payload_hash_version=TEMPORAL_EVENT_HASH_VERSION, status="queued",
    )
    db.add(event)
    replay.total_events = int(replay.total_events or 0) + 1
    replay.selected_rows = int(replay.selected_rows or 0) + 1
    replay.total_batches = replay.total_events
    if replay.start_time is None or ordinal < float(replay.start_time):
        replay.start_time = ordinal
    if replay.end_time is None or ordinal > float(replay.end_time):
        replay.end_time = ordinal
    stream_episodes = [str(item) for item in (replay.series_ids or [])]
    if str(body["episode_id"]) not in stream_episodes:
        stream_episodes.append(str(body["episode_id"]))
        replay.series_ids = stream_episodes
    replay.metrics = {**(replay.metrics or {}), "events_total": replay.total_events, "queue": replay.total_events - int(replay.committed_events or 0)}
    replay.status = "queued" if replay.status not in {"running", "pausing"} else replay.status
    replay.error = None
    db.commit(); db.refresh(event); db.refresh(replay)
    dispatch_stream(replay.id)
    return event, False


def _sql_stream_graph(
    db: Session,
    replay: TemporalReplay,
    *,
    limit: int,
    offset: int,
    at: float | None,
    mode: str,
    relation_state: str,
    entity_type: str | None = None,
    episode_id: str | None = None,
) -> dict[str, Any]:
    if replay.source_id != FACTORYNET_SOURCE_ID:
        facts = db.query(TemporalFact).filter(TemporalFact.replay_id == replay.id).all()
        if at is not None:
            point = Decimal(str(at))
            facts = [fact for fact in facts if fact.valid_from_ordinal <= point
                     and (fact.valid_to_ordinal is None or fact.valid_to_ordinal > point)]
        elif relation_state == "current":
            facts = [fact for fact in facts if fact.status == "active"]
        if episode_id:
            event_ids = {event.id for event in db.query(TemporalStreamEvent).filter(
                TemporalStreamEvent.replay_id == replay.id,
                TemporalStreamEvent.episode_id == episode_id).all()}
            facts = [fact for fact in facts if fact.source_event_id in event_ids]
        by_entity: dict[str, dict[str, Any]] = {}
        for fact in sorted(facts, key=lambda item: (item.valid_from_ordinal, item.id)):
            node = by_entity.setdefault(fact.subject_id, {"id": fact.subject_id,
                "entity_type": (replay.config or {}).get("mapping", {}).get("entity_type"),
                "labels": ["Instance"], "properties": {}, "node_kind": "instance"})
            if isinstance(fact.object_value, dict):
                if fact.state_key == "__deleted__":
                    node["properties"]["_deleted"] = bool(fact.object_value.get("value"))
                else:
                    node["properties"][fact.predicate] = fact.object_value.get("value")
        nodes = list(by_entity.values())
        if relation_state == "current":
            nodes = [node for node in nodes if not node["properties"].get("_deleted", False)]
        if entity_type:
            nodes = [node for node in nodes if node["entity_type"] == entity_type]
        total = len(nodes)
        page = nodes[offset:offset + limit]
        visible_node_ids = {str(node["id"]) for node in nodes}
        page_node_ids = {str(node["id"]) for node in page}
        link_resource_by_state_key = {
            f"link:{spec['source_field']}": spec.get("semantic_resource_id") or spec.get("link_type_id")
            for spec in _generic_link_specs(dict((replay.config or {}).get("mapping") or {}))
        }
        all_edges = []
        for fact in sorted(facts, key=lambda item: (item.valid_from_ordinal, item.id)):
            if not fact.object_id or fact.subject_id not in visible_node_ids or fact.object_id not in visible_node_ids:
                continue
            edge_properties = {
                "_fact_id": fact.id,
                "_event_id": fact.source_event_id,
                "evidence_ref_id": fact.evidence_ref_id,
                "event_ordinal": float(fact.valid_from_ordinal),
                "valid_from_ordinal": float(fact.valid_from_ordinal),
                "valid_to_ordinal": float(fact.valid_to_ordinal) if fact.valid_to_ordinal is not None else None,
                "_replay_id": replay.id,
                "_graph_namespace": replay.graph_namespace,
            }
            semantic_resource_id = link_resource_by_state_key.get(fact.state_key)
            if semantic_resource_id:
                edge_properties["semantic_resource_id"] = semantic_resource_id
            all_edges.append({
                "id": fact.id,
                "source": fact.subject_id,
                "target": fact.object_id,
                "type": fact.predicate,
                "label": fact.predicate,
                "properties": edge_properties,
                "valid_from_ordinal": float(fact.valid_from_ordinal),
                "valid_to_ordinal": float(fact.valid_to_ordinal) if fact.valid_to_ordinal is not None else None,
                "edge_kind": "instance",
            })
        page_edges = [edge for edge in all_edges
                      if str(edge["source"]) in page_node_ids and str(edge["target"]) in page_node_ids]
        return {"available": True, "graph_backend": "postgres-temporal-facts",
                "graph_namespace": replay.graph_namespace, "nodes": page, "edges": page_edges,
                "type_counts": dict(Counter(str(node.get("entity_type") or "Entity") for node in nodes)),
                "total_instances": total, "total_edges": len(all_edges), "returned": len(page),
                "offset": offset, "next_offset": offset + len(page) if offset + len(page) < total else None,
                "sample_limit": limit, "relation_state": relation_state, "at": at, "mode": mode}
    events = db.query(TemporalStreamEvent).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
    ).order_by(
        TemporalStreamEvent.ordinal.asc(),
        TemporalStreamEvent.source_sequence.asc(),
        TemporalStreamEvent.id.asc(),
    ).all()
    if episode_id:
        events = [event for event in events if str(event.episode_id) == str(episode_id)]
    observation_instance_ids = {
        _factorynet_observation_instance_id(
            replay.ontology_id,
            event.episode_id,
            event.source_row_id or event.source_sequence,
        )
        for event in events
    }
    materialized_observation_ids: set[str] = set()
    observation_id_list = list(observation_instance_ids)
    for start in range(0, len(observation_id_list), 1000):
        chunk = observation_id_list[start:start + 1000]
        materialized_observation_ids.update(
            row_id for (row_id,) in db.query(EntityInstance.id)
            .filter(EntityInstance.ontology_id == replay.ontology_id, EntityInstance.id.in_(chunk))
            .all()
        )
    facts_q = db.query(TemporalFact).filter(TemporalFact.replay_id == replay.id)
    if at is not None:
        facts_q = facts_q.filter(TemporalFact.valid_from_ordinal <= Decimal(str(at))).filter((TemporalFact.valid_to_ordinal.is_(None)) | (TemporalFact.valid_to_ordinal > Decimal(str(at))))
        if mode == "window":
            # Structural facts are shown for the event at the cursor; state
            # facts remain visible while their validity interval covers it.
            facts_q = facts_q.filter(
                (TemporalFact.state_key != "")
                | (TemporalFact.valid_from_ordinal == Decimal(str(at)))
            )
    if relation_state == "current":
        # With an explicit historical position, "current" means the fact
        # valid at that position (an expired fact may still be current then).
        # Without a position it means the present active interval.
        if at is None:
            facts_q = facts_q.filter(TemporalFact.status == "active", TemporalFact.valid_to_ordinal.is_(None))
    facts = facts_q.order_by(TemporalFact.valid_from_ordinal.asc()).all()
    # Every stream fact is sourced from one event.  Restricting by the event
    # set keeps the PostgreSQL fallback semantically identical to FalkorDB's
    # episode filter, including state facts generated by a selected episode.
    event_ids = {event.id for event in events}
    facts = [fact for fact in facts if fact.source_event_id in event_ids]
    event_map = {event.id: event for event in events}
    node_map: dict[str, dict[str, Any]] = {}
    for event in events:
        ordinal = float(event.ordinal)
        if at is not None:
            if mode == "window" and not math.isclose(ordinal, float(at), rel_tol=0.0, abs_tol=1e-9):
                continue
            if mode == "cumulative" and ordinal > at:
                continue
        source_row_id = event.source_row_id or event.source_sequence
        obs_id = f"FactoryNet:Observation:{event.episode_id}:{source_row_id}"
        properties = {**(event.payload or {}), "episode_id": event.episode_id, "source_row_id": event.source_row_id}
        object_ref_id = _factorynet_observation_instance_id(replay.ontology_id, event.episode_id, source_row_id)
        if object_ref_id in materialized_observation_ids:
            properties["object_ref_id"] = object_ref_id
        node_map.setdefault(obs_id, {"id": obs_id, "entity_type": "Observation", "labels": ["Instance"], "event_seq": event.source_sequence, "event_ordinal": ordinal, "properties": properties, "node_kind": "instance"})
        ep_id = f"FactoryNet:Episode:{event.episode_id}"
        node_map.setdefault(ep_id, {"id": ep_id, "entity_type": "Episode", "labels": ["Instance"], "properties": {"episode_id": event.episode_id}, "node_kind": "instance"})
    edge_rows: list[dict[str, Any]] = []
    for fact in facts:
        if fact.subject_id not in node_map:
            node_map[fact.subject_id] = {"id": fact.subject_id, "entity_type": fact.subject_id.split(":")[1] if ":" in fact.subject_id else "Entity", "labels": ["Instance"], "properties": {}, "node_kind": "instance"}
        if fact.object_id and fact.object_id not in node_map:
            node_map[fact.object_id] = {"id": fact.object_id, "entity_type": fact.object_id.split(":")[1] if ":" in fact.object_id else "Entity", "labels": ["Instance"], "properties": {"value": (fact.object_value or {}).get("value") if isinstance(fact.object_value, dict) else fact.object_value}, "node_kind": "instance"}
        edge_rows.append({"id": fact.id, "source": fact.subject_id, "target": fact.object_id, "type": fact.predicate, "label": fact.predicate, "edge_kind": "instance", "properties": {"state_key": fact.state_key, "valid_from_ordinal": float(fact.valid_from_ordinal), "valid_to_ordinal": float(fact.valid_to_ordinal) if fact.valid_to_ordinal is not None else None, "source_event_id": fact.source_event_id, "evidence_ref_id": fact.evidence_ref_id}})
    nodes = list(node_map.values())
    if entity_type:
        nodes = [node for node in nodes if str(node.get("entity_type") or "") == str(entity_type)]
    total = len(nodes)
    page = nodes[offset: offset + limit]
    ids = {node["id"] for node in page}
    edges = [edge for edge in edge_rows if edge["source"] in ids and edge["target"] in ids]
    return {"available": True, "graph_backend": "postgres-temporal-facts", "graph_namespace": replay.graph_namespace, "nodes": page, "edges": edges, "type_counts": dict(Counter(str(node.get("entity_type") or "Entity") for node in nodes)), "total_instances": total, "total_edges": len(edge_rows), "returned": len(page), "offset": offset, "next_offset": offset + len(page) if offset + len(page) < total else None, "sample_limit": limit, "relation_state": relation_state, "at": at, "mode": mode}


def _attach_factorynet_object_refs(
    db: Session,
    replay: TemporalReplay,
    graph_data: dict[str, Any],
) -> dict[str, Any]:
    """Enrich the rebuildable graph projection from authoritative Object rows."""
    nodes = graph_data.get("nodes") or []
    node_ref_ids: dict[int, str] = {}
    for index, node in enumerate(nodes):
        if str(node.get("entity_type") or "").lower() != "observation":
            continue
        props = node.get("properties") or {}
        episode_id = props.get("episode_id")
        source_row_id = props.get("source_row_id") or props.get("_source_row_index")
        if episode_id is None or source_row_id is None:
            prefix = "FactoryNet:Observation:"
            node_id = str(node.get("id") or "")
            if not node_id.startswith(prefix):
                continue
            identity = node_id[len(prefix):]
            episode_id, separator, source_row_id = identity.rpartition(":")
            if not separator or not episode_id:
                continue
        node_ref_ids[index] = _factorynet_observation_instance_id(
            replay.ontology_id, str(episode_id), source_row_id
        )
    ref_ids = list(set(node_ref_ids.values()))
    instances: dict[str, EntityInstance] = {}
    for start in range(0, len(ref_ids), 1000):
        chunk = ref_ids[start:start + 1000]
        instances.update({
            item.id: item
            for item in db.query(EntityInstance).filter(
                EntityInstance.ontology_id == replay.ontology_id,
                EntityInstance.id.in_(chunk),
            ).all()
        })
    for index, object_ref_id in node_ref_ids.items():
        instance = instances.get(object_ref_id)
        if instance is None:
            continue
        node = nodes[index]
        props = dict(node.get("properties") or {})
        props["object_ref_id"] = instance.id
        if instance.object_type_resource_id:
            props["object_type_resource_id"] = instance.object_type_resource_id
        node["properties"] = props
    graph_data["nodes"] = nodes
    return graph_data


def stream_graph(
    db: Session,
    replay: TemporalReplay,
    *,
    limit: int = 200,
    offset: int = 0,
    at: float | None = None,
    mode: str = "cumulative",
    relation_state: str = "all",
    entity_type: str | None = None,
    episode_id: str | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), MAX_GRAPH_NODES)); offset = max(0, int(offset))
    if replay.source_id != FACTORYNET_SOURCE_ID:
        # The graph projection holds the latest properties. Historical reads
        # must use the fact intervals, even when FalkorDB is available.
        return {**_sql_stream_graph(db, replay, limit=limit, offset=offset, at=at,
                                    mode=mode, relation_state=relation_state,
                                    entity_type=entity_type, episode_id=episode_id),
                "run_id": replay.id, "ontology_id": replay.ontology_id}
    latest_rebuild = db.query(TemporalGraphProjectionJob).filter(
        TemporalGraphProjectionJob.replay_id == replay.id,
    ).order_by(TemporalGraphProjectionJob.created_at.desc()).first()
    if latest_rebuild and latest_rebuild.status != "completed":
        return {
            **_sql_stream_graph(
                db,
                replay,
                limit=limit,
                offset=offset,
                at=at,
                mode=mode,
                relation_state=relation_state,
                entity_type=entity_type,
                episode_id=episode_id,
            ),
            "run_id": replay.id,
            "ontology_id": replay.ontology_id,
            "graph_namespace": replay.graph_namespace,
            "at": at,
            "mode": mode,
            "graph_recovered": True,
            "graph_projection_rebuild_status": latest_rebuild.status,
        }
    graph = FalkorDBService()
    if getattr(graph, "available", False):
        try:
            data = graph.get_graph_data(replay.ontology_id, limit=limit, offset=offset, entity_type=entity_type, episode_id=episode_id, seq_to=None, relation_state=relation_state, replay_id=replay.id, at=at, graph_namespace=replay.graph_namespace, mode=mode)
            if data.get("nodes") or not int(replay.committed_events or 0):
                data = _attach_factorynet_object_refs(db, replay, data)
                return {**data, "run_id": replay.id, "ontology_id": replay.ontology_id, "graph_namespace": replay.graph_namespace, "at": at, "mode": mode}
            # FalkorDB is a projection, not the source of truth.  A restart
            # or a lost graph volume can leave it empty while PostgreSQL still
            # contains committed events and facts.  Use the deterministic SQL
            # projection instead of showing a misleading empty canvas.
            return {
                **_sql_stream_graph(
                    db,
                    replay,
                    limit=limit,
                    offset=offset,
                    at=at,
                    mode=mode,
                    relation_state=relation_state,
                    entity_type=entity_type,
                    episode_id=episode_id,
                ),
                "run_id": replay.id,
                "ontology_id": replay.ontology_id,
                "graph_namespace": replay.graph_namespace,
                "at": at,
                "mode": mode,
                "graph_recovered": True,
            }
        except TypeError:
            data = graph.get_graph_data(replay.ontology_id, limit=limit, offset=offset, entity_type=entity_type, episode_id=episode_id, relation_state=relation_state, replay_id=replay.id, at=at)
            if data.get("nodes") or not int(replay.committed_events or 0):
                data = _attach_factorynet_object_refs(db, replay, data)
                return {**data, "run_id": replay.id, "ontology_id": replay.ontology_id, "graph_namespace": replay.graph_namespace, "at": at, "mode": mode}
            return {
                **_sql_stream_graph(
                    db,
                    replay,
                    limit=limit,
                    offset=offset,
                    at=at,
                    mode=mode,
                    relation_state=relation_state,
                    entity_type=entity_type,
                    episode_id=episode_id,
                ),
                "run_id": replay.id,
                "ontology_id": replay.ontology_id,
                "graph_namespace": replay.graph_namespace,
                "at": at,
                "mode": mode,
                "graph_recovered": True,
            }
        except Exception:
            pass
    return {
        **_sql_stream_graph(
            db,
            replay,
            limit=limit,
            offset=offset,
            at=at,
            mode=mode,
            relation_state=relation_state,
            entity_type=entity_type,
            episode_id=episode_id,
        ),
        "run_id": replay.id,
        "ontology_id": replay.ontology_id,
    }


def stream_facts(db: Session, replay: TemporalReplay, *, limit: int = 200, offset: int = 0, at: float | None = None, state_key: str | None = None, relation_state: str = "all") -> dict[str, Any]:
    query = db.query(TemporalFact).filter(TemporalFact.replay_id == replay.id)
    if state_key:
        query = query.filter(TemporalFact.state_key == state_key)
    if at is not None:
        point = Decimal(str(at)); query = query.filter(TemporalFact.valid_from_ordinal <= point).filter((TemporalFact.valid_to_ordinal.is_(None)) | (TemporalFact.valid_to_ordinal > point))
    if relation_state == "current" and at is None:
        query = query.filter(TemporalFact.status == "active", TemporalFact.valid_to_ordinal.is_(None))
    total = query.count(); rows = query.order_by(TemporalFact.valid_from_ordinal.asc(), TemporalFact.created_at.asc()).offset(max(0, offset)).limit(max(1, min(limit, 1000))).all()
    return {"run_id": replay.id, "facts": [_serialize_fact(row) for row in rows], "total": total, "offset": offset, "limit": limit, "next_offset": offset + len(rows) if offset + len(rows) < total else None, "at": at, "relation_state": relation_state}


def _validate_publish(db: Session, replay: TemporalReplay) -> list[str]:
    """Run deterministic gates before an immutable snapshot is published."""
    problems: list[str] = []
    event_query = db.query(TemporalStreamEvent).filter(TemporalStreamEvent.replay_id == replay.id)
    total_events = event_query.count()
    if total_events != int(replay.total_events or 0):
        problems.append(f"事件索引数量 {total_events} 与运行总数 {replay.total_events or 0} 不一致")
    pending = event_query.filter(TemporalStreamEvent.status != "committed").count()
    if pending:
        problems.append(f"仍有 {pending} 条事件未提交")

    facts = db.query(TemporalFact).filter(TemporalFact.replay_id == replay.id).all()
    for fact in facts:
        if fact.valid_to_ordinal is not None and fact.valid_to_ordinal < fact.valid_from_ordinal:
            problems.append(f"事实 {fact.id} 的有效区间反向")
        if fact.status == "active" and fact.valid_to_ordinal is not None:
            problems.append(f"当前事实 {fact.id} 仍带有失效水位")
        if fact.status == "expired" and fact.valid_to_ordinal is None:
            problems.append(f"历史事实 {fact.id} 缺少失效水位")
        if not fact.source_event_id or not fact.evidence_ref_id:
            problems.append(f"事实 {fact.id} 缺少来源事件或 EvidenceRef")
    duplicate_states = db.query(
        TemporalFact.subject_id,
        TemporalFact.state_key,
        func.count(TemporalFact.id),
    ).filter(
        TemporalFact.replay_id == replay.id,
        TemporalFact.state_key != "",
        TemporalFact.status == "active",
        TemporalFact.valid_to_ordinal.is_(None),
    ).group_by(
        TemporalFact.subject_id,
        TemporalFact.state_key,
    ).having(func.count(TemporalFact.id) > 1).all()
    if duplicate_states:
        problems.append("当前状态存在重复有效事实")

    observations = [fact for fact in facts if fact.predicate == "HAS_OBSERVATION"]
    event_episodes = {
        str(episode)
        for (episode,) in db.query(TemporalStreamEvent.episode_id).filter(
            TemporalStreamEvent.replay_id == replay.id,
            TemporalStreamEvent.status == "committed",
        ).distinct().all()
    }
    observation_episodes = {
        str(fact.subject_id).split(":", 2)[-1]
        for fact in observations
        if str(fact.subject_id).startswith("FactoryNet:Episode:")
    }
    if replay.source_id == FACTORYNET_SOURCE_ID and event_episodes - observation_episodes:
        problems.append("存在已提交事件没有对应 HAS_OBSERVATION 事实")
    for episode in observation_episodes:
        count = sum(1 for fact in observations if fact.subject_id == f"FactoryNet:Episode:{episode}")
        # NEXT_OBSERVATION is per episode; count only the edges whose source
        # observation belongs to this episode.
        next_count = sum(
            1 for fact in facts
            if fact.predicate == "NEXT_OBSERVATION"
            and str(fact.subject_id).startswith(f"FactoryNet:Observation:{episode}:")
        )
        if count > 0 and next_count != max(0, count - 1):
            problems.append(f"episode {episode} 的 Observation 顺序不连续")

    run_id = (replay.config or {}).get("construction_run_id")
    missing_evidence = db.query(TemporalFact.id).filter(
        TemporalFact.replay_id == replay.id,
        ~TemporalFact.evidence_ref_id.in_(
            db.query(EvidenceRef.id).filter(EvidenceRef.construction_run_id == run_id)
        ) if run_id else TemporalFact.evidence_ref_id.is_(None),
    ).count()
    if missing_evidence:
        problems.append(f"有 {missing_evidence} 条事实无法定位到本次 EvidenceRef")
    return problems


def publish_stream_run(db: Session, replay: TemporalReplay, *, created_by: str | None = None) -> dict[str, Any]:
    locked_replay = db.query(TemporalReplay).filter(
        TemporalReplay.id == replay.id,
    ).populate_existing().with_for_update().first()
    if locked_replay is None:
        raise StreamError("动态运行不存在", "RUN_NOT_FOUND")
    replay = locked_replay
    if replay.status == "published" and replay.published_snapshot_id:
        snapshot = db.query(DataModelSnapshot).filter(DataModelSnapshot.id == replay.published_snapshot_id).first()
        return {"run": serialize_stream(replay, db=db), "snapshot": _serialize_snapshot(snapshot) if snapshot else None, "already_published": True}
    if replay.status != "completed":
        raise StreamError("只有全部事件处理完成后才能发布快照", "RUN_NOT_COMPLETED")
    committed = db.query(TemporalStreamEvent).filter(TemporalStreamEvent.replay_id == replay.id, TemporalStreamEvent.status == "committed").count()
    if committed != int(replay.total_events or 0) or committed != int(replay.committed_events or 0):
        raise StreamError("仍有事件未提交，不能发布", "EVENTS_INCOMPLETE", committed=committed, total=replay.total_events)
    project = db.query(OntologyProject).filter(OntologyProject.id == replay.ontology_id).first()
    if not project:
        raise StreamError("本体不存在", "ONTOLOGY_NOT_FOUND")
    if replay.schema_revision_id != project.current_revision_id:
        raise StreamError("运行期间本体结构已变化，请基于新修订重新验证", "SCHEMA_REVISION_CONFLICT", run_revision_id=replay.schema_revision_id, current_revision_id=project.current_revision_id)
    expected_snapshot_id = replay.base_snapshot_id
    validation_problems = _validate_publish(db, replay)
    if validation_problems:
        code = "CURRENT_STATE_CONFLICT" if any("当前状态" in item for item in validation_problems) else "STREAM_VALIDATION_FAILED"
        raise StreamError("；".join(validation_problems[:8]), code, problems=validation_problems[:20])
    facts = db.query(TemporalFact).filter(TemporalFact.replay_id == replay.id).order_by(
        TemporalFact.valid_from_ordinal.asc(),
        TemporalFact.subject_id.asc(),
        TemporalFact.predicate.asc(),
        TemporalFact.object_id.asc(),
        TemporalFact.state_key.asc(),
        TemporalFact.id.asc(),
    ).all()
    through = _float_or_none(replay.watermark_ordinal)
    latest_event = db.query(TemporalStreamEvent).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
    ).order_by(
        TemporalStreamEvent.ordinal.desc(),
        TemporalStreamEvent.source_sequence.desc(),
        TemporalStreamEvent.id.desc(),
    ).first()
    if committed and (
        latest_event is None
        or replay.watermark_ordinal is None
        or replay.watermark_sequence is None
        or Decimal(str(latest_event.ordinal)) != Decimal(str(replay.watermark_ordinal))
        or int(latest_event.source_sequence) != int(replay.watermark_sequence)
    ):
        raise StreamError("回放 watermark 与最后一条已提交事件不一致", "EVENT_WATERMARK_MISMATCH")
    graph = stream_graph(db, replay, limit=MAX_GRAPH_NODES, offset=0, at=None, relation_state="all")
    node_count = int(graph.get("total_instances") or len(graph.get("nodes") or []))
    edge_count = int(graph.get("total_edges") or len(graph.get("edges") or []))
    evidence_count = db.query(EvidenceRef).filter(EvidenceRef.construction_run_id == (replay.config or {}).get("construction_run_id")).count()
    # Snapshot identity describes the materialized content, not the temporary
    # run UUID or graph namespace.  Excluding those run-scoped identifiers is
    # what makes a file replay and an equivalent push sequence produce the
    # same hash while still keeping both projections isolated at runtime.
    ordered_events = db.query(TemporalStreamEvent).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
    ).order_by(
        TemporalStreamEvent.ordinal.asc(),
        TemporalStreamEvent.source_sequence.asc(),
        TemporalStreamEvent.id.asc(),
    ).all()
    manifest = temporal_snapshot_manifest(
        replay,
        watermark_ordinal=replay.watermark_ordinal,
        watermark_sequence=replay.watermark_sequence,
        event_count=committed,
        node_count=node_count,
        edge_count=edge_count,
        fact_count=len(facts),
        evidence_count=evidence_count,
    )
    snapshot_hash = temporal_snapshot_content_digest(
        replay, ordered_events, facts, through, manifest=manifest,
    )
    snapshot = DataModelSnapshot(
        id=str(uuid.uuid4()),
        ontology_id=replay.ontology_id,
        schema_revision_id=replay.schema_revision_id,
        replay_id=replay.id,
        dataset_version_id=replay.dataset_version_id,
        graph_namespace=replay.graph_namespace or f"stream_{replay.id.replace('-', '')}",
        through_ordinal=Decimal(str(through)) if through is not None else None,
        watermark_sequence=replay.watermark_sequence,
        event_count=committed,
        node_count=node_count,
        edge_count=edge_count,
        fact_count=len(facts),
        evidence_count=evidence_count,
        snapshot_hash=snapshot_hash,
        snapshot_hash_version=2,
        status="published",
        created_by=created_by,
    )
    db.add(snapshot)
    db.flush()

    # The run pins the project snapshot that existed when the run was created.
    # The conditional UPDATE is the atomic compare-and-swap: only one publisher
    # based on that version can advance the project's current snapshot.
    pointer_filter = (
        OntologyProject.current_data_snapshot_id.is_(None)
        if expected_snapshot_id is None
        else OntologyProject.current_data_snapshot_id == expected_snapshot_id
    )
    project_update = db.query(OntologyProject).filter(
        OntologyProject.id == replay.ontology_id,
        pointer_filter,
    )
    revision_filter = (
        OntologyProject.current_revision_id.is_(None)
        if replay.schema_revision_id is None
        else OntologyProject.current_revision_id == replay.schema_revision_id
    )
    project_update = project_update.filter(revision_filter)
    switched = project_update.update(
        {
            OntologyProject.current_data_snapshot_id: snapshot.id,
            OntologyProject.updated_at: _now(),
        },
        synchronize_session=False,
    )
    if switched != 1:
        current_project = db.query(OntologyProject).filter(
            OntologyProject.id == replay.ontology_id,
        ).populate_existing().first()
        if current_project is None:
            db.rollback()
            raise StreamError("本体不存在", "ONTOLOGY_NOT_FOUND")
        if current_project.current_revision_id != replay.schema_revision_id:
            current_revision_id = current_project.current_revision_id
            db.rollback()
            raise StreamError(
                "发布期间本体结构已变化，请基于新修订重新运行",
                "SCHEMA_REVISION_CONFLICT",
                run_revision_id=replay.schema_revision_id,
                current_revision_id=current_revision_id,
            )
        current_snapshot_id = current_project.current_data_snapshot_id
        db.rollback()
        raise StreamError(
            "项目当前快照已更新；此运行基于旧版本，不能覆盖较新的快照",
            "SNAPSHOT_STALE",
            expected_base_snapshot_id=expected_snapshot_id,
            current_snapshot_id=current_snapshot_id,
        )
    replay.published_snapshot_id = snapshot.id
    replay.published_at = _now()
    replay.status = "published"
    replay.updated_at = _now()
    db.commit(); db.refresh(snapshot); db.refresh(replay)
    audit_task_id = None
    try:
        from app.services.v2.audit_runner import queue_local_audit
        audit = queue_local_audit(db, ontology_id=replay.ontology_id, revision_id=replay.schema_revision_id or project.current_revision_id, construction_run_id=(replay.config or {}).get("construction_run_id"))
        audit_task_id = getattr(audit, "id", None)
    except Exception:
        # Publishing remains successful when Ollama is not configured.  The
        # audit page can be used to retry once the local model is available.
        audit_task_id = None
    return {"run": serialize_stream(replay, db=db), "snapshot": _serialize_snapshot(snapshot), "audit_task_id": audit_task_id, "already_published": False}


def stream_snapshot(db: Session, replay: TemporalReplay) -> DataModelSnapshot | None:
    if not replay.published_snapshot_id:
        return None
    return db.query(DataModelSnapshot).filter(DataModelSnapshot.id == replay.published_snapshot_id).first()


__all__ = [
    "StreamError", "STREAM_ACTIVE_STATUSES", "STREAM_TERMINAL_STATUSES", "create_stream_run",
    "process_one_event", "run_temporal_stream", "dispatch_stream", "update_stream_control",
    "ingest_push_event", "serialize_stream", "stream_graph", "stream_facts", "publish_stream_run",
    "stream_snapshot", "temporal_snapshot_content_digest", "temporal_snapshot_manifest",
]
