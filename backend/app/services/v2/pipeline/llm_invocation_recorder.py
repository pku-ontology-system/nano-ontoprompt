"""Persist privacy-preserving LLM attempt provenance for Pipeline Runs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4


def _digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PipelineLLMInvocationRecorder:
    """Persist privacy-safe invocation state plus an immutable lifecycle chain."""

    def __init__(
        self,
        db,
        pipeline_run_id: str,
        unavailable_reason: str | None = None,
        model_config_id: str | None = None,
        model_config_digest: str | None = None,
        provider: str | None = None,
        model_name: str | None = None,
    ):
        self.db = db
        self.pipeline_run_id = pipeline_run_id
        self.unavailable_reason = unavailable_reason
        self.model_config_id = model_config_id
        self.model_config_digest = model_config_digest
        self.provider = provider
        self.model_name = model_name
        self.source_dataset_version_id: str | None = None
        self.attempt_number = 0
        self.last_invocation_id: str | None = None

    def set_source(self, dataset_version_id: str | None) -> None:
        self.source_dataset_version_id = dataset_version_id

    def begin(
        self,
        *,
        model_config,
        messages: list[dict],
        phase: str | None,
        prompt_id: str | None,
        prompt_digest: str | None,
    ) -> str | None:
        from sqlalchemy import func

        from app.models.v2.pipeline import PipelineRun
        from app.models.v2.workbench_task import ModelInvocation, PipelineLLMInvocationEvent

        try:
            run = self.db.query(PipelineRun.id).filter(
                PipelineRun.id == self.pipeline_run_id,
            ).with_for_update().first()
            if run is None:
                return None
            latest_attempt = self.db.query(func.max(ModelInvocation.attempt_number)).filter(
                ModelInvocation.pipeline_run_id == self.pipeline_run_id,
            ).scalar() or 0
            self.attempt_number = max(self.attempt_number, int(latest_attempt)) + 1
        except Exception:
            self.db.rollback()
            return None
        models = getattr(model_config, "models", None) or []
        if not isinstance(models, (list, tuple)):
            models = [models]
        model_name = str(models[0]) if models and models[0] else self.model_name or "unavailable"
        invocation = ModelInvocation(
            id=str(uuid4()),
            pipeline_run_id=self.pipeline_run_id,
            source_dataset_version_id=self.source_dataset_version_id,
            attempt_number=self.attempt_number,
            model_config_id=(getattr(model_config, "id", None) if model_config else None) or self.model_config_id,
            model_config_digest=(getattr(model_config, "_model_config_digest", None) if model_config else None) or self.model_config_digest,
            route_alias="pipeline:md_to_structured",
            provider=(getattr(model_config, "provider", None) if model_config else None) or self.provider,
            model_name=model_name,
            status="running",
            phase=phase,
            request_hash=_digest(messages),
            metadata_json={
                "prompt_id": prompt_id,
                "prompt_digest": prompt_digest,
                "provider_model_artifact_reproducibility": "unverified",
                "request_payload_persisted": False,
                "response_payload_persisted": False,
            },
            created_at=datetime.now(timezone.utc),
        )
        try:
            self.db.add(invocation)
            self.db.flush()
            self._append_event(
                invocation,
                PipelineLLMInvocationEvent,
                event_type="started",
                status="running",
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            return None
        self.last_invocation_id = invocation.id
        return invocation.id

    def finish(
        self,
        invocation_id: str | None,
        *,
        status: str,
        response: str | None,
        reason: str | None,
        duration_ms: int,
    ) -> bool:
        from app.models.v2.workbench_task import ModelInvocation, PipelineLLMInvocationEvent

        if not invocation_id:
            return False
        try:
            invocation = self.db.query(ModelInvocation).filter(ModelInvocation.id == invocation_id).first()
            if invocation is None:
                return False
            invocation.status = status
            invocation.error = reason
            invocation.response_hash = _digest(response) if isinstance(response, str) else None
            invocation.duration_ms = max(0, int(duration_ms))
            invocation.completed_at = datetime.now(timezone.utc)
            self._append_event(
                invocation,
                PipelineLLMInvocationEvent,
                event_type="provider_result",
                status=status,
                error_code=reason,
                duration_ms=invocation.duration_ms,
            )
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            return False

    def mark_last_invalid_response(self) -> bool:
        """Attach parse/schema validation failure to the immediately preceding call."""
        from app.models.v2.workbench_task import ModelInvocation, PipelineLLMInvocationEvent

        if not self.last_invocation_id:
            return False
        try:
            invocation = self.db.query(ModelInvocation).filter(
                ModelInvocation.id == self.last_invocation_id,
            ).first()
            if invocation is None:
                return False
            invocation.status = "invalid_response"
            invocation.error = "invalid_response"
            self._append_event(
                invocation,
                PipelineLLMInvocationEvent,
                event_type="response_validation_failed",
                status="invalid_response",
                error_code="invalid_response",
                duration_ms=invocation.duration_ms,
            )
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            return False

    def _append_event(
        self,
        invocation,
        event_model,
        *,
        event_type: str,
        status: str,
        error_code: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Add a digest-chained event in the same transaction as the state update."""
        from sqlalchemy import desc

        from app.models.v2.workbench_task import ModelInvocation

        self.db.query(ModelInvocation.id).filter(
            ModelInvocation.id == invocation.id,
        ).with_for_update().first()
        previous = self.db.query(event_model).filter(
            event_model.invocation_id == invocation.id,
        ).order_by(desc(event_model.sequence)).first()
        sequence = (previous.sequence if previous else 0) + 1
        previous_digest = previous.event_digest if previous else None
        occurred_at = datetime.now(timezone.utc)
        metadata = invocation.metadata_json if isinstance(invocation.metadata_json, dict) else {}
        prompt_id = metadata.get("prompt_id")
        prompt_digest = metadata.get("prompt_digest")
        event_data = {
            "id": str(uuid4()),
            "invocation_id": invocation.id,
            "pipeline_run_id": invocation.pipeline_run_id or self.pipeline_run_id,
            "attempt_number": invocation.attempt_number or self.attempt_number,
            "sequence": sequence,
            "event_type": event_type,
            "status": status,
            "phase": invocation.phase,
            "source_dataset_version_id": invocation.source_dataset_version_id,
            "model_config_id": invocation.model_config_id,
            "model_config_digest": invocation.model_config_digest,
            "prompt_id": prompt_id,
            "prompt_digest": prompt_digest,
            "provider": invocation.provider,
            "model_name": invocation.model_name,
            "request_hash": invocation.request_hash,
            "response_hash": invocation.response_hash,
            "error_code": error_code,
            "duration_ms": duration_ms,
            "previous_digest": previous_digest,
            "occurred_at": occurred_at,
        }
        event_data["event_digest"] = _event_digest(event_data)
        self.db.add(event_model(**event_data))


def _event_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _event_payload(event) -> dict:
    return {
        "id": event.id,
        "invocation_id": event.invocation_id,
        "pipeline_run_id": event.pipeline_run_id,
        "attempt_number": event.attempt_number,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "status": event.status,
        "phase": event.phase,
        "source_dataset_version_id": event.source_dataset_version_id,
        "model_config_id": event.model_config_id,
        "model_config_digest": event.model_config_digest,
        "prompt_id": event.prompt_id,
        "prompt_digest": event.prompt_digest,
        "provider": event.provider,
        "model_name": event.model_name,
        "request_hash": event.request_hash,
        "response_hash": event.response_hash,
        "error_code": event.error_code,
        "duration_ms": event.duration_ms,
        "previous_digest": event.previous_digest,
        "occurred_at": _event_timestamp(event.occurred_at),
    }


def _event_digest(event) -> str:
    payload = _event_payload(event) if hasattr(event, "event_digest") else {
        **event,
        "occurred_at": _event_timestamp(event["occurred_at"]),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def verify_pipeline_invocation_event_chain(events) -> bool:
    """Verify sequence, predecessor links, and canonical event digests."""
    previous_digest = None
    for expected_sequence, item in enumerate(events, start=1):
        if item.sequence != expected_sequence or item.previous_digest != previous_digest:
            return False
        if item.event_digest != _event_digest(item):
            return False
        previous_digest = item.event_digest
    return bool(events)
