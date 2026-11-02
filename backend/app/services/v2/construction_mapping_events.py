"""Append-only, privacy-safe Construction MappingTask attempt evidence."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from uuid import uuid4


def _canonical_digest(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def append_mapping_attempt_event(
    db,
    task,
    *,
    attempt_number: int,
    event_type: str,
    status: str,
    invocation=None,
    phase: str | None = None,
    error_code: str | None = None,
    duration_ms: int | None = None,
    response_hash: str | None = None,
):
    """Append an event in the caller's transaction, serialized on its task row."""
    from sqlalchemy import desc

    from app.models.v2.workbench_task import ConstructionMappingAttemptEvent, MappingTask

    locked = db.query(MappingTask.id).filter(MappingTask.id == task.id).with_for_update().first()
    if locked is None:
        raise ValueError("MappingTask disappeared while recording its attempt")
    previous = db.query(ConstructionMappingAttemptEvent).filter(
        ConstructionMappingAttemptEvent.mapping_task_id == task.id,
    ).order_by(desc(ConstructionMappingAttemptEvent.sequence)).first()
    manifest = task.input_manifest_json if isinstance(task.input_manifest_json, dict) else {}
    model_pin = manifest.get("model_config") if isinstance(manifest.get("model_config"), dict) else {}
    llm_contract = manifest.get("llm_contract") if isinstance(manifest.get("llm_contract"), dict) else {}
    metadata = invocation.metadata_json if invocation is not None and isinstance(invocation.metadata_json, dict) else {}
    occurred_at = datetime.now(timezone.utc)
    event_data = {
        "id": str(uuid4()),
        "mapping_task_id": task.id,
        "invocation_id": invocation.id if invocation is not None else None,
        "attempt_number": int(attempt_number),
        "sequence": (previous.sequence if previous else 0) + 1,
        "event_type": event_type,
        "status": status,
        "phase": phase or (invocation.phase if invocation is not None else None),
        "source_dataset_version_id": task.source_version_id,
        "model_config_id": metadata.get("model_config_id") or model_pin.get("config_id"),
        "model_config_digest": metadata.get("model_config_digest") or model_pin.get("config_digest"),
        "prompt_digest": metadata.get("prompt_digest") or _prompt_digest(llm_contract, phase),
        "parser_version": llm_contract.get("parser_version"),
        "normalizer_version": llm_contract.get("normalizer_version"),
        "provider": invocation.provider if invocation is not None else model_pin.get("provider"),
        "model_name": invocation.model_name if invocation is not None else _pinned_model_name(model_pin),
        "request_hash": invocation.request_hash if invocation is not None else None,
        "response_hash": response_hash if response_hash is not None else (
            invocation.response_hash if invocation is not None else None
        ),
        "error_code": error_code,
        "duration_ms": duration_ms,
        "previous_digest": previous.event_digest if previous else None,
        "occurred_at": occurred_at,
    }
    event_data["event_digest"] = _canonical_digest({
        **event_data,
        "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(timespec="microseconds"),
    })
    db.add(ConstructionMappingAttemptEvent(**event_data))


def next_mapping_attempt_number(db, task) -> int:
    from sqlalchemy import func

    from app.models.v2.workbench_task import ConstructionMappingAttemptEvent

    latest = db.query(func.max(ConstructionMappingAttemptEvent.attempt_number)).filter(
        ConstructionMappingAttemptEvent.mapping_task_id == task.id,
        ConstructionMappingAttemptEvent.event_type == "attempt_started",
    ).scalar()
    return int(latest or 0) + 1


def _prompt_digest(contract: dict, phase: str | None) -> str | None:
    if phase == "source_analysis":
        return contract.get("source_analysis_prompt_digest")
    if phase == "ontology_mapping":
        return contract.get("ontology_mapping_prompt_digest")
    return None


def _pinned_model_name(model_pin: dict) -> str | None:
    models = model_pin.get("models")
    return str(models[0]) if isinstance(models, list) and models else None


def verify_mapping_attempt_event_chain(events: Iterable) -> bool:
    previous_digest = None
    for expected_sequence, item in enumerate(events, start=1):
        if item.sequence != expected_sequence or item.previous_digest != previous_digest:
            return False
        occurred_at = item.occurred_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        payload = {
            "id": item.id,
            "mapping_task_id": item.mapping_task_id,
            "invocation_id": item.invocation_id,
            "attempt_number": item.attempt_number,
            "sequence": item.sequence,
            "event_type": item.event_type,
            "status": item.status,
            "phase": item.phase,
            "source_dataset_version_id": item.source_dataset_version_id,
            "model_config_id": item.model_config_id,
            "model_config_digest": item.model_config_digest,
            "prompt_digest": item.prompt_digest,
            "parser_version": item.parser_version,
            "normalizer_version": item.normalizer_version,
            "provider": item.provider,
            "model_name": item.model_name,
            "request_hash": item.request_hash,
            "response_hash": item.response_hash,
            "error_code": item.error_code,
            "duration_ms": item.duration_ms,
            "previous_digest": item.previous_digest,
            "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        }
        if _canonical_digest(payload) != item.event_digest:
            return False
        previous_digest = item.event_digest
    return True
