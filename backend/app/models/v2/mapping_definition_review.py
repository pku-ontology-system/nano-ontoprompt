"""Immutable Mapping definition candidates and independent review decisions."""
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


class MappingDefinitionReview(Base):
    """A frozen Mapping candidate; approval publishes this exact revision."""

    __tablename__ = "v2_mapping_definition_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    mapping_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_ontology_mappings.id", ondelete="RESTRICT"), nullable=False,
    )
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    candidate_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending_review")
    submitted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review','approved','rejected','changes_requested')",
            name="ck_v2_mapping_definition_review_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approved','rejected','changes_requested')",
            name="ck_v2_mapping_definition_review_decision",
        ),
        CheckConstraint("revision_no >= 1", name="ck_v2_mapping_definition_review_revision_positive"),
        UniqueConstraint("mapping_id", "revision_no", name="uq_v2_mapping_definition_review_revision"),
        Index("ix_v2_mapping_definition_review_status_created", "status", "created_at"),
    )


class MappingDefinitionReviewEvent(Base):
    """Append-only actor and decision history for Mapping candidate reviews."""

    __tablename__ = "v2_mapping_definition_review_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    review_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_mapping_definition_reviews.id", ondelete="RESTRICT"), nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    detail_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        CheckConstraint("sequence_no >= 1", name="ck_v2_mapping_definition_review_event_sequence_positive"),
        CheckConstraint("actor_kind IN ('user','system')", name="ck_v2_mapping_definition_review_event_actor_kind"),
        CheckConstraint(
            "event_type IN ('submitted','approved','rejected','changes_requested')",
            name="ck_v2_mapping_definition_review_event_type",
        ),
        CheckConstraint(
            "status IN ('pending_review','approved','rejected','changes_requested')",
            name="ck_v2_mapping_definition_review_event_status",
        ),
        UniqueConstraint("review_id", "sequence_no", name="uq_v2_mapping_definition_review_event_sequence"),
        Index("ix_v2_mapping_definition_review_events_review", "review_id", "sequence_no"),
    )


@event.listens_for(MappingDefinitionReviewEvent, "before_update")
def _reject_mapping_review_event_update(_mapper, _connection, _target):
    raise ValueError("Mapping definition review events are append-only")


@event.listens_for(MappingDefinitionReviewEvent, "before_delete")
def _reject_mapping_review_event_delete(_mapper, _connection, _target):
    raise ValueError("Mapping definition review events are append-only")
