"""Durable, idempotent runs for review-triggered Ontology Mapping Apply."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import event, text
from sqlalchemy import (
    DateTime,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MappingApplyRun(Base):
    __tablename__ = "v2_mapping_apply_runs"
    __table_args__ = (
        UniqueConstraint("mapping_id", "review_id", name="uq_mapping_apply_run_mapping_review"),
        UniqueConstraint("idempotency_key", name="uq_mapping_apply_run_idempotency_key"),
        Index("ix_mapping_apply_run_dispatch_due", "status", "next_attempt_at"),
        Index("ix_mapping_apply_run_dispatch_lease", "status", "dispatch_lease_until"),
        Index("ix_mapping_apply_run_execution_lease", "status", "run_lease_until"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False,
    )
    curated_dataset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_curated_datasets.id", ondelete="RESTRICT"), nullable=False,
    )
    review_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_curated_reviews.id", ondelete="RESTRICT"), nullable=False,
    )
    # Keep the immutable version identifier as a durable reference while a
    # run is retained. The source object's lifecycle is managed separately.
    dataset_version_id: Mapped[str] = mapped_column(String(120), nullable=False)
    mapping_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    mapping_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    execution_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    provenance: Mapped["MappingApplyRunProvenance | None"] = relationship(
        back_populates="run", uselist=False, lazy="joined", cascade="all, delete-orphan",
    )
    attempt_events: Mapped[list["MappingApplyRunAttemptEvent"]] = relationship(
        back_populates="run",
        order_by="MappingApplyRunAttemptEvent.sequence_no",
        lazy="selectin",
    )

    @property
    def mapping_review_id(self) -> str | None:
        return self.provenance.mapping_review_id if self.provenance else None

    @property
    def requested_by(self) -> str | None:
        return self.provenance.requested_by if self.provenance else None

    @property
    def request_actor_kind(self) -> str | None:
        return self.provenance.request_actor_kind if self.provenance else None

    @property
    def request_actor_id(self) -> str | None:
        return self.provenance.request_actor_id if self.provenance else None

    @property
    def worker_actor_id(self) -> str | None:
        return self.provenance.worker_actor_id if self.provenance else None


class MappingApplyRunProvenance(Base):
    """Provenance extension that avoids rebuilding referenced run tables."""

    __tablename__ = "v2_mapping_apply_run_provenance"

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_apply_runs.id", ondelete="RESTRICT"), primary_key=True,
    )
    mapping_review_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_definition_reviews.id", ondelete="RESTRICT"), nullable=False,
    )
    requested_by: Mapped[str | None] = mapped_column(String, nullable=True)
    request_actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    request_actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    # The most recent execution worker; per-attempt history is kept separately.
    worker_actor_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )

    run: Mapped[MappingApplyRun] = relationship(back_populates="provenance")

    __table_args__ = (
        CheckConstraint("request_actor_kind IN ('user','system')", name="ck_mapping_apply_provenance_actor_kind"),
        CheckConstraint(
            "(request_actor_kind = 'user' AND requested_by IS NOT NULL AND request_actor_id = requested_by) "
            "OR (request_actor_kind = 'system' AND request_actor_id <> '')",
            name="ck_mapping_apply_provenance_actor_identity",
        ),
    )


class MappingApplyRunAttemptEvent(Base):
    """Append-only, digest-chained evidence for Mapping Apply worker attempts."""

    __tablename__ = "v2_mapping_apply_run_attempt_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_apply_runs.id", ondelete="RESTRICT"), nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    digest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    run: Mapped[MappingApplyRun] = relationship(back_populates="attempt_events")

    __table_args__ = (
        UniqueConstraint("run_id", "sequence_no", name="uq_mapping_apply_attempt_event_sequence"),
        CheckConstraint("sequence_no >= 1", name="ck_mapping_apply_attempt_sequence_positive"),
        CheckConstraint("attempt_number >= 1", name="ck_mapping_apply_attempt_number_positive"),
        CheckConstraint("digest_version >= 1", name="ck_mapping_apply_attempt_digest_version_positive"),
        CheckConstraint(
            "event_type IN ('attempt_started','attempt_succeeded','attempt_failed','attempt_outcome_unknown')",
            name="ck_mapping_apply_attempt_event_type",
        ),
        CheckConstraint(
            "status IN ('running','succeeded','retry_wait','failed')",
            name="ck_mapping_apply_attempt_status",
        ),
        CheckConstraint("actor_kind IN ('worker','system')", name="ck_mapping_apply_attempt_actor_kind"),
        CheckConstraint("actor_id <> ''", name="ck_mapping_apply_attempt_actor_id"),
        Index("ix_mapping_apply_attempt_run_sequence", "run_id", "sequence_no"),
    )


Index(
    "uq_mapping_apply_attempt_start",
    MappingApplyRunAttemptEvent.run_id,
    MappingApplyRunAttemptEvent.attempt_number,
    unique=True,
    postgresql_where=text("event_type = 'attempt_started'"),
    sqlite_where=text("event_type = 'attempt_started'"),
)
Index(
    "uq_mapping_apply_attempt_terminal",
    MappingApplyRunAttemptEvent.run_id,
    MappingApplyRunAttemptEvent.attempt_number,
    unique=True,
    postgresql_where=text("event_type <> 'attempt_started'"),
    sqlite_where=text("event_type <> 'attempt_started'"),
)


@event.listens_for(MappingApplyRunAttemptEvent, "before_update")
def _reject_mapping_apply_attempt_event_update(_mapper, _connection, _target):
    raise ValueError("Mapping Apply attempt events are append-only")


@event.listens_for(MappingApplyRunAttemptEvent, "before_delete")
def _reject_mapping_apply_attempt_event_delete(_mapper, _connection, _target):
    raise ValueError("Mapping Apply attempt events are append-only")
