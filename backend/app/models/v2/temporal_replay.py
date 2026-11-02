"""Durable checkpoints for the temporal data-arrival simulation.

The replay is intentionally separate from :class:`ConstructionRun`: a normal
construction represents one atomic build, while a replay is a user-visible
sequence of committed batches that can be paused, resumed, or continued with
another forward segment.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.database import Base


class TemporalReplay(Base):
    __tablename__ = "v2_temporal_replays"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    dataset_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_datasets.id", ondelete="SET NULL"), nullable=True)
    dataset_version_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_dataset_versions.id", ondelete="SET NULL"), nullable=True)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False, default="factorynet_cnc")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="created")
    time_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="ordinal")
    entity_column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    time_column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    series_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    start_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    window_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    speed: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    current_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_batch_index: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    total_batches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normalized_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pause_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    step_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    worker_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    worker_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dispatch_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Dynamic data-model evolution fields.  The legacy replay columns remain
    # intact so existing URLs and records can still be read.  A replay is now
    # also a durable stream run with its own graph projection namespace.
    source_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="file_replay")
    schema_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    graph_namespace: Mapped[str | None] = mapped_column(String(240), nullable=True)
    event_interval_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    current_event_index: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    total_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    committed_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    watermark_ordinal: Mapped[float | None] = mapped_column(Numeric(24, 9), nullable=True)
    watermark_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Current data-model snapshot observed when this run was created.  Publish
    # uses it as the expected value for the atomic current-snapshot switch.
    base_snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    published_snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Explicit aliases keep integrations that use the more verbose JSON field
    # names source-compatible without creating duplicate database columns.
    config_json = synonym("config")
    metrics_json = synonym("metrics")
    state_json = synonym("state")

    __table_args__ = (
        Index("ix_v2_temporal_replays_ontology_id", "ontology_id"),
        Index("ix_v2_temporal_replays_status", "status"),
        Index("ix_v2_temporal_replays_dispatch", "status", "next_attempt_at", "dispatch_lease_until"),
    )


class TemporalReplayBatch(Base):
    __tablename__ = "v2_temporal_replay_batches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    replay_id: Mapped[str] = mapped_column(String, ForeignKey("v2_temporal_replays.id", ondelete="CASCADE"), nullable=False)
    batch_no: Mapped[int] = mapped_column(Integer, nullable=False)
    time_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_to: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    source_row_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normalized_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nodes_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edges_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latest_rows: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    latest_rows_json = synonym("latest_rows")

    __table_args__ = (
        UniqueConstraint("replay_id", "batch_no", name="uq_v2_temporal_replay_batch_no"),
        Index("ix_v2_temporal_replay_batches_replay_id", "replay_id"),
    )


class TemporalStreamEvent(Base):
    """One source observation in a dynamic temporal run.

    FactoryNet replay events reference a source row instead of duplicating the
    complete payload.  Push events may keep a small structured payload.  The
    unique run/event key makes retries idempotent while the ordinal watermark
    keeps the first release deterministic.
    """

    __tablename__ = "v2_temporal_stream_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    replay_id: Mapped[str] = mapped_column(String, ForeignKey("v2_temporal_replays.id", ondelete="CASCADE"), nullable=False)
    event_key: Mapped[str] = mapped_column(String(300), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False, default="upsert", server_default="upsert")
    episode_id: Mapped[str] = mapped_column(String(200), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(200), nullable=False)
    ordinal: Mapped[float] = mapped_column(Numeric(24, 9), nullable=False)
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_row_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_ref: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_hash_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_projection_status: Mapped[str | None] = mapped_column(String(20), nullable=True, default="pending")
    graph_projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("replay_id", "event_key", name="uq_v2_temporal_stream_event_key"),
        Index("ix_v2_temporal_stream_events_replay_id", "replay_id"),
        Index("ix_v2_temporal_stream_events_watermark", "replay_id", "ordinal", "source_sequence"),
        Index("ix_v2_temporal_stream_events_retry", "replay_id", "status", "next_attempt_at"),
        Index("ix_v2_temporal_stream_events_graph_projection", "replay_id", "graph_projection_status"),
    )


class TemporalProjectionAttempt(Base):
    """Durable lifecycle and authorization record for one event projection try."""

    __tablename__ = "v2_temporal_projection_attempts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    replay_id: Mapped[str] = mapped_column(String, ForeignKey("v2_temporal_replays.id", ondelete="RESTRICT"), nullable=False)
    event_id: Mapped[str] = mapped_column(String, ForeignKey("v2_temporal_stream_events.id", ondelete="RESTRICT"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    attempt_type: Mapped[str] = mapped_column(String(20), nullable=False, default="automatic", server_default="automatic")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="started", server_default="started")
    worker_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    request_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("event_id", "attempt_no", name="uq_v2_temporal_projection_attempt_no"),
        UniqueConstraint("replay_id", "request_key", name="uq_v2_temporal_projection_request_key"),
        CheckConstraint("attempt_type IN ('automatic','manual')", name="ck_v2_temporal_projection_attempt_type"),
        CheckConstraint("status IN ('authorized','started','committed','failed','abandoned')", name="ck_v2_temporal_projection_attempt_status"),
        Index("ix_v2_temporal_projection_attempts_replay", "replay_id", "created_at"),
        Index("ix_v2_temporal_projection_attempts_event", "event_id", "attempt_no"),
    )


class TemporalGraphProjectionJob(Base):
    """Durable full rebuild and content verification of one replay graph."""

    __tablename__ = "v2_temporal_graph_projection_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    replay_id: Mapped[str] = mapped_column(String, ForeignKey("v2_temporal_replays.id", ondelete="CASCADE"), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_namespace: Mapped[str] = mapped_column(String(240), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    expected_nodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_edges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    written_nodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    written_edges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_nodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_edges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    execution_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    celery_task_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    worker_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    dispatch_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("replay_id", "source_revision", name="uq_v2_temporal_graph_projection_revision"),
        CheckConstraint("status IN ('queued','running','completed','failed')", name="ck_v2_temporal_graph_projection_status"),
        Index("ix_v2_temporal_graph_projection_dispatch", "status", "next_attempt_at", "updated_at"),
        Index("ix_v2_temporal_graph_projection_replay", "replay_id", "created_at"),
    )


class TemporalFact(Base):
    """Versioned temporal fact projected from one or more stream events."""

    __tablename__ = "v2_temporal_facts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    replay_id: Mapped[str] = mapped_column(String, ForeignKey("v2_temporal_replays.id", ondelete="CASCADE"), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(300), nullable=False)
    predicate: Mapped[str] = mapped_column(String(160), nullable=False)
    object_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    object_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state_key: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    valid_from_ordinal: Mapped[float] = mapped_column(Numeric(24, 9), nullable=False)
    valid_to_ordinal: Mapped[float | None] = mapped_column(Numeric(24, 9), nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_temporal_stream_events.id", ondelete="SET NULL"), nullable=True)
    evidence_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_v2_temporal_facts_replay_id", "replay_id"),
        Index("ix_v2_temporal_facts_state", "replay_id", "state_key", "subject_id"),
        Index("ix_v2_temporal_facts_interval", "replay_id", "valid_from_ordinal", "valid_to_ordinal"),
    )


class DataModelSnapshot(Base):
    """Immutable published data-model projection for an ontology."""

    __tablename__ = "v2_data_model_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    schema_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    replay_id: Mapped[str] = mapped_column(String, ForeignKey("v2_temporal_replays.id", ondelete="RESTRICT"), nullable=False)
    dataset_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    graph_namespace: Mapped[str] = mapped_column(String(240), nullable=False)
    through_ordinal: Mapped[float | None] = mapped_column(Numeric(24, 9), nullable=True)
    watermark_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_hash_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="published")
    created_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_v2_data_model_snapshots_ontology_id", "ontology_id"),
        Index("ix_v2_data_model_snapshots_replay_id", "replay_id"),
    )


__all__ = ["TemporalReplay", "TemporalReplayBatch", "TemporalStreamEvent", "TemporalProjectionAttempt", "TemporalGraphProjectionJob", "TemporalFact", "DataModelSnapshot"]
