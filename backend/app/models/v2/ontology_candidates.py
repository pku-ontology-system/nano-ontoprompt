"""AI-generated ontology candidates (for example from NVIDIA Auto Ontology) waiting for human review.

A candidate set is one generation or import.  Its items are draft object types and relationships with the
evidence they came from.  Nothing here changes the ontology: only an explicit publish turns the accepted items
into one batch revision through the ontology editor.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


SET_STATUSES = ("generating", "review", "published", "failed", "cancelled")
ITEM_DECISIONS = ("pending", "accepted", "modified", "rejected")


class OntologyCandidateSet(Base):
    __tablename__ = "v2_ontology_candidate_sets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)  # runner | file | example
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="review")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    generator: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    runner_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    document_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    published_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('generating','review','published','failed','cancelled')", name="ck_v2_ontology_candidate_set_status"),
        CheckConstraint("origin IN ('runner','file','example')", name="ck_v2_ontology_candidate_set_origin"),
        Index("ix_v2_ontology_candidate_sets_ontology", "ontology_id", "created_at"),
    )


class OntologyCandidateItem(Base):
    __tablename__ = "v2_ontology_candidate_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    set_id: Mapped[str] = mapped_column(String(36), ForeignKey("v2_ontology_candidate_sets.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # entity_type | relationship
    key: Mapped[str] = mapped_column(String(300), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    basis: Mapped[str] = mapped_column(String(24), nullable=False)  # term | declared_fk | semantic_fk | bridge_table
    draft: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    final: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    decision: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    reject_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    exists_in_ontology: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reviewed_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publish_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("set_id", "key", name="uq_v2_ontology_candidate_item_key"),
        CheckConstraint("kind IN ('entity_type','relationship')", name="ck_v2_ontology_candidate_item_kind"),
        CheckConstraint("decision IN ('pending','accepted','modified','rejected')", name="ck_v2_ontology_candidate_item_decision"),
        Index("ix_v2_ontology_candidate_items_set", "set_id", "ordinal"),
    )
