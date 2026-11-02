"""Independent review decisions for ontology construction candidates."""
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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConstructionReview(Base):
    __tablename__ = "v2_construction_reviews"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    draft_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_construction_drafts.id", ondelete="RESTRICT"), nullable=False,
    )
    dataset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_datasets.id", ondelete="RESTRICT"), nullable=False,
    )
    dataset_version_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_dataset_versions.id", ondelete="RESTRICT"), nullable=False,
    )
    ontology_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="SET NULL"), nullable=True,
    )
    base_revision_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("ontology_revisions.id", ondelete="SET NULL"), nullable=True,
    )
    mapping_task_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_mapping_tasks.id", ondelete="SET NULL"), nullable=True,
    )
    construction_run_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_construction_runs.id", ondelete="SET NULL"), nullable=True,
    )
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    impact_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending_review")
    submitted_by: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    reviewed_by: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review','approved','rejected','changes_requested')",
            name="ck_v2_construction_reviews_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approved','rejected','changes_requested')",
            name="ck_v2_construction_reviews_decision",
        ),
        Index("ix_v2_construction_reviews_status_created", "status", "created_at"),
        Index("ix_v2_construction_reviews_draft_created", "draft_id", "created_at"),
        Index("ix_v2_construction_reviews_dataset", "dataset_id"),
    )


class ConstructionReviewEvent(Base):
    """Append-only record of candidate review and materialization lifecycle."""

    __tablename__ = "v2_construction_review_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    review_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_construction_reviews.id", ondelete="RESTRICT"), nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    detail_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        CheckConstraint("sequence_no >= 1", name="ck_v2_construction_review_event_sequence_positive"),
        CheckConstraint(
            "event_type IN ('submitted','approved','rejected','changes_requested','dispatch_failed','materialized')",
            name="ck_v2_construction_review_event_type",
        ),
        CheckConstraint(
            "status IN ('pending_review','approved','rejected','changes_requested')",
            name="ck_v2_construction_review_event_status",
        ),
        UniqueConstraint("review_id", "sequence_no", name="uq_v2_construction_review_event_sequence"),
        Index("ix_v2_construction_review_events_review", "review_id", "sequence_no"),
    )


@event.listens_for(ConstructionReviewEvent, "before_update")
def _review_event_before_update(mapper, connection, target):  # noqa: ARG001
    raise ValueError("Construction review events are append-only")


@event.listens_for(ConstructionReviewEvent, "before_delete")
def _review_event_before_delete(mapper, connection, target):  # noqa: ARG001
    raise ValueError("Construction review events are append-only")
