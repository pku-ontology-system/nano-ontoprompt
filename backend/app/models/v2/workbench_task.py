"""Persistent workbench task and model-invocation records."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MappingTask(Base):
    __tablename__ = "v2_mapping_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    draft_id: Mapped[str] = mapped_column(String, ForeignKey("v2_construction_drafts.id", ondelete="CASCADE"), nullable=False, index=True)
    source_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    input_manifest_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    model_route: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    progress: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    trace_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_plan_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    transfer_manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False, default="ontology-mapping-v2")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class DataImportTask(Base):
    __tablename__ = "v2_data_import_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_connections.id", ondelete="SET NULL"), nullable=True)
    dataset_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_datasets.id", ondelete="SET NULL"), nullable=True, index=True)
    data_class: Mapped[str] = mapped_column(String(30), nullable=False, default="regular")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    progress: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ModelInvocation(Base):
    __tablename__ = "v2_model_invocations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    draft_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_construction_drafts.id", ondelete="SET NULL"), nullable=True)
    construction_run_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_construction_runs.id", ondelete="SET NULL"), nullable=True)
    audit_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    mapping_task_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_mapping_tasks.id", ondelete="SET NULL"), nullable=True)
    pipeline_run_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_pipeline_runs.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    source_dataset_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_config_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_alias: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(40), nullable=True)
    request_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_pipeline_run_llm_invocation_attempt_number",
            "pipeline_run_id",
            "attempt_number",
            unique=True,
            postgresql_where=text("pipeline_run_id IS NOT NULL AND attempt_number IS NOT NULL"),
            sqlite_where=text("pipeline_run_id IS NOT NULL AND attempt_number IS NOT NULL"),
        ),
    )


class PipelineLLMInvocationEvent(Base):
    """Immutable lifecycle evidence for one Pipeline provider call."""

    __tablename__ = "v2_pipeline_llm_invocation_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    invocation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("v2_model_invocations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    pipeline_run_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_dataset_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model_config_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("invocation_id", "sequence", name="uq_pipeline_llm_invocation_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_pipeline_llm_invocation_event_sequence_positive"),
        CheckConstraint(
            "event_type IN ('started', 'provider_result', 'response_validation_failed')",
            name="ck_pipeline_llm_invocation_event_type",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'empty_response', 'invalid_response')",
            name="ck_pipeline_llm_invocation_event_status",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_pipeline_llm_invocation_attempt_positive"),
        Index("ix_pipeline_llm_events_run_sequence", "pipeline_run_id", "attempt_number", "sequence"),
    )


@event.listens_for(PipelineLLMInvocationEvent, "before_update")
def _reject_pipeline_llm_invocation_event_update(_mapper, _connection, _target):
    raise ValueError("Pipeline LLM invocation events are append-only")


@event.listens_for(PipelineLLMInvocationEvent, "before_delete")
def _reject_pipeline_llm_invocation_event_delete(_mapper, _connection, _target):
    raise ValueError("Pipeline LLM invocation events are append-only")


class ConstructionMappingAttemptEvent(Base):
    """Immutable lifecycle evidence for one Construction MappingTask attempt."""

    __tablename__ = "v2_construction_mapping_attempt_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    mapping_task_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_mapping_tasks.id", ondelete="RESTRICT"), nullable=False,
    )
    invocation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_model_invocations.id", ondelete="RESTRICT"), nullable=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_dataset_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model_config_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    normalizer_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("mapping_task_id", "sequence", name="uq_construction_mapping_event_sequence"),
        CheckConstraint("attempt_number >= 1", name="ck_construction_mapping_event_attempt_positive"),
        CheckConstraint("sequence >= 1", name="ck_construction_mapping_event_sequence_positive"),
        CheckConstraint(
            "event_type IN ('attempt_started', 'invocation_started', 'provider_result', "
            "'response_validation_failed', 'invocation_finalized', 'attempt_result')",
            name="ck_construction_mapping_event_type",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled', 'invalid_response', 'waiting_for_model')",
            name="ck_construction_mapping_event_status",
        ),
        Index("ix_construction_mapping_events_task_sequence", "mapping_task_id", "sequence"),
    )


Index(
    "uq_construction_mapping_attempt_start",
    ConstructionMappingAttemptEvent.mapping_task_id,
    ConstructionMappingAttemptEvent.attempt_number,
    unique=True,
    postgresql_where=text("event_type = 'attempt_started'"),
    sqlite_where=text("event_type = 'attempt_started'"),
)


@event.listens_for(ConstructionMappingAttemptEvent, "before_update")
def _reject_construction_mapping_event_update(_mapper, _connection, _target):
    raise ValueError("Construction MappingTask attempt events are append-only")


@event.listens_for(ConstructionMappingAttemptEvent, "before_delete")
def _reject_construction_mapping_event_delete(_mapper, _connection, _target):
    raise ValueError("Construction MappingTask attempt events are append-only")
