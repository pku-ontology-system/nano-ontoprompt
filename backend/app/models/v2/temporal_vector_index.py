"""Durable build state for replay-scoped temporal vector indexes."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TemporalVectorIndexJob(Base):
    __tablename__ = "v2_temporal_vector_index_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    replay_id: Mapped[str] = mapped_column(String, ForeignKey("v2_temporal_replays.id", ondelete="CASCADE"), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    total_facts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    indexed_facts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_fact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
        UniqueConstraint("replay_id", "source_revision", name="uq_v2_temporal_vector_index_revision"),
        CheckConstraint(
            "status IN ('queued','running','completed','failed','pruned')",
            name="ck_v2_temporal_vector_index_job_status",
        ),
        Index("ix_v2_temporal_vector_index_jobs_dispatch", "status", "next_attempt_at", "updated_at"),
        Index("ix_v2_temporal_vector_index_jobs_replay", "replay_id", "completed_at"),
    )


class TemporalVectorIndexRetentionEvent(Base):
    """Append-only audit record for a retained temporal index generation being pruned."""

    __tablename__ = "v2_temporal_vector_index_retention_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_temporal_vector_index_jobs.id", ondelete="SET NULL"), nullable=True
    )
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False
    )
    replay_id: Mapped[str] = mapped_column(String, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, default="index_pruned")
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        CheckConstraint("event_type = 'index_pruned'", name="ck_v2_temporal_vector_index_retention_event_type"),
        Index("ix_v2_temporal_vector_index_retention_events_replay", "replay_id", "created_at"),
    )
