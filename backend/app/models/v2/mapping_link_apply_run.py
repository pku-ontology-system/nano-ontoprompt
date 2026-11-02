"""Durable Link Mapping Apply runs and relationship projection outbox."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


_ACTIVE_RUN_SQL = "status IN ('pending','dispatching','queued','running','retry_wait')"


class MappingLinkApplyRun(Base):
    """A user-confirmed, snapshot-pinned relationship materialization run."""

    __tablename__ = "v2_mapping_link_apply_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_mapping_link_apply_run_idempotency"),
        CheckConstraint(
            "status IN ('pending','dispatching','queued','running','retry_wait','succeeded','failed')",
            name="ck_mapping_link_apply_run_status",
        ),
        CheckConstraint("execution_attempts >= 0", name="ck_mapping_link_apply_run_execution_attempts"),
        CheckConstraint("dispatch_attempts >= 0", name="ck_mapping_link_apply_run_dispatch_attempts"),
        CheckConstraint("request_actor_kind IN ('user','system')", name="ck_mapping_link_apply_run_actor_kind"),
        CheckConstraint(
            "(request_actor_kind = 'user' AND requested_by IS NOT NULL AND request_actor_id = requested_by) "
            "OR (request_actor_kind = 'system' AND request_actor_id <> '')",
            name="ck_mapping_link_apply_run_actor_identity",
        ),
        Index("ix_mapping_link_apply_dispatch_due", "status", "next_attempt_at"),
        Index("ix_mapping_link_apply_dispatch_lease", "status", "dispatch_lease_until"),
        Index("ix_mapping_link_apply_execution_lease", "status", "run_lease_until"),
        Index(
            "uq_mapping_link_apply_active",
            "link_mapping_id",
            unique=True,
            postgresql_where=text(_ACTIVE_RUN_SQL),
            sqlite_where=text(_ACTIVE_RUN_SQL),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    link_mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_link_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    link_review_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_link_definition_reviews.id", ondelete="RESTRICT"), nullable=False,
    )
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False,
    )

    src_dataset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_curated_datasets.id", ondelete="RESTRICT"), nullable=False,
    )
    src_review_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_curated_reviews.id", ondelete="RESTRICT"), nullable=False,
    )
    src_dataset_version_id: Mapped[str] = mapped_column(String(120), nullable=False)
    src_mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    src_mapping_review_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_definition_reviews.id", ondelete="RESTRICT"), nullable=False,
    )
    src_mapping_apply_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_apply_runs.id", ondelete="RESTRICT"), nullable=False,
    )

    tgt_dataset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_curated_datasets.id", ondelete="RESTRICT"), nullable=False,
    )
    tgt_review_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_curated_reviews.id", ondelete="RESTRICT"), nullable=False,
    )
    tgt_dataset_version_id: Mapped[str] = mapped_column(String(120), nullable=False)
    tgt_mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    tgt_mapping_review_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_definition_reviews.id", ondelete="RESTRICT"), nullable=False,
    )
    tgt_mapping_apply_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_apply_runs.id", ondelete="RESTRICT"), nullable=False,
    )

    input_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    preview_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_summary_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String, nullable=True)
    request_actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    request_actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    worker_actor_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    execution_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    celery_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    attempt_events: Mapped[list["MappingLinkApplyRunAttemptEvent"]] = relationship(
        back_populates="run", order_by="MappingLinkApplyRunAttemptEvent.sequence_no", lazy="selectin",
    )


class MappingLinkApplyRunAttemptEvent(Base):
    """Append-only digest chain for each Link Mapping Apply worker attempt."""

    __tablename__ = "v2_mapping_link_apply_run_attempt_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_mapping_link_apply_attempt_sequence"),
        CheckConstraint("sequence_no >= 1", name="ck_mapping_link_apply_attempt_sequence"),
        CheckConstraint("attempt_number >= 1", name="ck_mapping_link_apply_attempt_number"),
        CheckConstraint("digest_version >= 1", name="ck_mapping_link_apply_attempt_digest_version"),
        CheckConstraint(
            "event_type IN ('attempt_started','attempt_succeeded','attempt_failed','attempt_outcome_unknown')",
            name="ck_mapping_link_apply_attempt_event_type",
        ),
        CheckConstraint(
            "status IN ('running','succeeded','retry_wait','failed')",
            name="ck_mapping_link_apply_attempt_status",
        ),
        CheckConstraint("actor_kind IN ('worker','system')", name="ck_mapping_link_apply_attempt_actor_kind"),
        CheckConstraint("actor_id <> ''", name="ck_mapping_link_apply_attempt_actor_id"),
        Index("ix_mapping_link_apply_attempt_run_sequence", "run_id", "sequence_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_link_apply_runs.id", ondelete="RESTRICT"), nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    digest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run: Mapped[MappingLinkApplyRun] = relationship(back_populates="attempt_events")


class MappingLinkRelationChange(Base):
    """Immutable PostgreSQL relationship fact change and graph projection intent."""

    __tablename__ = "v2_mapping_link_relation_changes"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_mapping_link_relation_change_sequence"),
        UniqueConstraint("run_id", "relation_id", name="uq_mapping_link_relation_change_run_relation"),
        CheckConstraint("operation IN ('upsert','delete')", name="ck_mapping_link_relation_change_operation"),
        CheckConstraint("sequence_no >= 1", name="ck_mapping_link_relation_change_sequence"),
        Index("ix_mapping_link_relation_change_link_created", "link_mapping_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_link_apply_runs.id", ondelete="RESTRICT"), nullable=False,
    )
    link_mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_link_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    relation_id: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String(10), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(String, nullable=False)
    target_entity_id: Mapped[str] = mapped_column(String, nullable=False)
    source_dataset_version_id: Mapped[str] = mapped_column(String(120), nullable=False)
    target_dataset_version_id: Mapped[str] = mapped_column(String(120), nullable=False)
    before_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MappingLinkRelationProjectionState(Base):
    """Mutable delivery state for one immutable relationship projection intent."""

    __tablename__ = "v2_mapping_link_relation_projection_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','projecting','projected','dead_letter')",
            name="ck_mapping_link_relation_projection_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_mapping_link_relation_projection_attempts"),
        CheckConstraint("manual_attempts >= 0", name="ck_mapping_link_relation_projection_manual_attempts"),
        Index("ix_mapping_link_relation_projection_due", "status", "next_attempt_at"),
        Index("ix_mapping_link_relation_projection_lease", "status", "lease_until"),
    )

    change_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_link_relation_changes.id", ondelete="RESTRICT"), primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    manual_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    current_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class MappingLinkRelationProjectionEvent(Base):
    """Append-only automatic/manual attempt events for one relation fact."""

    __tablename__ = "v2_mapping_link_relation_projection_events"
    __table_args__ = (
        UniqueConstraint("attempt_id", "event_type", name="uq_mapping_link_projection_attempt_event"),
        UniqueConstraint("change_id", "sequence_no", name="uq_mapping_link_projection_change_sequence"),
        CheckConstraint("sequence_no >= 1", name="ck_mapping_link_projection_event_sequence"),
        CheckConstraint("attempt_number >= 1", name="ck_mapping_link_projection_event_attempt"),
        CheckConstraint("event_type IN ('started','finished')", name="ck_mapping_link_projection_event_type"),
        CheckConstraint("attempt_kind IN ('automatic','manual')", name="ck_mapping_link_projection_attempt_kind"),
        CheckConstraint("actor_kind IN ('system','worker','admin')", name="ck_mapping_link_projection_actor_kind"),
        CheckConstraint(
            "(event_type = 'started' AND outcome IS NULL) OR "
            "(event_type = 'finished' AND outcome IS NOT NULL)",
            name="ck_mapping_link_projection_event_outcome",
        ),
        Index("ix_mapping_link_projection_event_run_sequence", "run_id", "sequence_no"),
        Index("ix_mapping_link_projection_event_change_sequence", "change_id", "sequence_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(12), nullable=False)
    change_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_link_relation_changes.id", ondelete="RESTRICT"), nullable=False,
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_link_apply_runs.id", ondelete="RESTRICT"), nullable=False,
    )
    link_mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_link_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False,
    )
    relation_id: Mapped[str] = mapped_column(String, nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MappingLinkProjectionReplayRequest(Base):
    """Idempotent, authorized manual replay request for a pinned Apply run."""

    __tablename__ = "v2_mapping_link_projection_replay_requests"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "actor_id", "client_request_id",
            name="uq_mapping_link_projection_replay_request",
        ),
        CheckConstraint(
            "status IN ('pending','dispatching','queued','running','retry_wait','completed','partial','failed')",
            name="ck_mapping_link_projection_replay_status",
        ),
        CheckConstraint("selected_count >= 0", name="ck_mapping_link_projection_replay_selected"),
        CheckConstraint("projected_count >= 0", name="ck_mapping_link_projection_replay_projected"),
        CheckConstraint("failed_count >= 0", name="ck_mapping_link_projection_replay_failed"),
        CheckConstraint("pending_count >= 0", name="ck_mapping_link_projection_replay_pending"),
        CheckConstraint("batch_size >= 1 AND batch_size <= 100", name="ck_mapping_link_projection_replay_batch_size"),
        CheckConstraint("dispatch_attempts >= 0", name="ck_mapping_link_projection_replay_dispatch_attempts"),
        Index("ix_mapping_link_projection_replay_run_created", "run_id", "created_at"),
        Index("ix_mapping_link_projection_replay_lease", "status", "lease_until"),
        Index("ix_mapping_link_projection_replay_dispatch", "status", "next_attempt_at"),
        Index("ix_mapping_link_projection_replay_dispatch_lease", "status", "dispatch_lease_until"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_link_apply_runs.id", ondelete="RESTRICT"), nullable=False,
    )
    link_mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_link_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(24), nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_scope_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    celery_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    selected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    projected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dispatch_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@event.listens_for(MappingLinkApplyRunAttemptEvent, "before_update")
def _reject_link_apply_attempt_update(_mapper, _connection, _target):
    raise ValueError("Link Mapping Apply attempt events are append-only")


@event.listens_for(MappingLinkApplyRunAttemptEvent, "before_delete")
def _reject_link_apply_attempt_delete(_mapper, _connection, _target):
    raise ValueError("Link Mapping Apply attempt events are append-only")


@event.listens_for(MappingLinkRelationChange, "before_update")
def _reject_link_relation_change_update(_mapper, _connection, _target):
    raise ValueError("Link relationship changes are append-only")


@event.listens_for(MappingLinkRelationChange, "before_delete")
def _reject_link_relation_change_delete(_mapper, _connection, _target):
    raise ValueError("Link relationship changes are append-only")


@event.listens_for(MappingLinkRelationProjectionEvent, "before_update")
def _reject_link_projection_event_update(_mapper, _connection, _target):
    raise ValueError("Link relation projection events are append-only")


@event.listens_for(MappingLinkRelationProjectionEvent, "before_delete")
def _reject_link_projection_event_delete(_mapper, _connection, _target):
    raise ValueError("Link relation projection events are append-only")
