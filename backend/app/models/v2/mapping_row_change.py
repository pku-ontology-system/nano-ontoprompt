"""Append-only row changes and retryable graph projection outbox for Mapping."""
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
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MappingRowChange(Base):
    __tablename__ = "v2_mapping_row_changes"
    __table_args__ = (
        UniqueConstraint("mapping_apply_run_id", "object_id", name="uq_mapping_row_change_run_object"),
        CheckConstraint("operation IN ('upsert', 'delete')", name="ck_mapping_row_change_operation"),
        CheckConstraint(
            "projection_status IN ('pending', 'projecting', 'projected', 'dead_letter')",
            name="ck_mapping_row_change_projection_status",
        ),
        Index("ix_mapping_row_change_projection_due", "projection_status", "projection_next_attempt_at"),
        Index("ix_mapping_row_change_mapping_created", "mapping_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    mapping_apply_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_apply_runs.id", ondelete="RESTRICT"), nullable=False,
    )
    mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False,
    )
    object_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    row_identity: Mapped[str] = mapped_column(String(200), nullable=False)
    dataset_version_id: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(10), nullable=False)
    before_row_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_row_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    projection_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    projection_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    projection_manual_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    projection_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projection_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projection_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projection_current_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    projection_projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )


class MappingRowProjectionEvent(Base):
    """Append-only start/finish events for one row projection attempt."""

    __tablename__ = "v2_mapping_row_projection_events"
    __table_args__ = (
        CheckConstraint("event_type IN ('started', 'finished')", name="ck_mapping_row_projection_event_type"),
        CheckConstraint("attempt_kind IN ('automatic', 'manual')", name="ck_mapping_row_projection_attempt_kind"),
        UniqueConstraint("attempt_id", "event_type", name="uq_mapping_row_projection_attempt_event"),
        Index("ix_mapping_row_projection_event_run_created", "mapping_apply_run_id", "created_at"),
        Index(
            "uq_mapping_row_projection_manual_request",
            "row_change_id", "actor_id", "client_request_id",
            unique=True,
            postgresql_where=text("event_type = 'started' AND client_request_id IS NOT NULL"),
            sqlite_where=text("event_type = 'started' AND client_request_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(12), nullable=False)
    row_change_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_row_changes.id", ondelete="RESTRICT"), nullable=False,
    )
    mapping_apply_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_apply_runs.id", ondelete="RESTRICT"), nullable=False,
    )
    mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False,
    )
    object_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )


@event.listens_for(MappingRowProjectionEvent, "before_update")
def _reject_mapping_row_projection_event_update(_mapper, _connection, _target):
    raise ValueError("Mapping row projection events are append-only")


@event.listens_for(MappingRowProjectionEvent, "before_delete")
def _reject_mapping_row_projection_event_delete(_mapper, _connection, _target):
    raise ValueError("Mapping row projection events are append-only")
