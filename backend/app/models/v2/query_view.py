"""Immutable QueryDataView manifests used for repeatable Object Query reads."""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, CheckConstraint, Index, JSON, UniqueConstraint, event
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class QueryDataView(Base):
    __tablename__ = "v2_query_data_views"

    id = Column(String(64), primary_key=True, default=lambda: f"view_{uuid4().hex}")
    ontology_id = Column(String, ForeignKey("ontology_projects.id"), nullable=False, index=True)
    source_manifest_digest = Column(String(128), nullable=False)
    source_snapshot_id = Column(String, ForeignKey('v2_data_model_snapshots.id'), nullable=True)
    base_view_id = Column(String(64), ForeignKey("v2_query_data_views.id"), nullable=True)
    changeset_digest = Column(String(128), nullable=True)
    changeset_version = Column(String(64), nullable=True)
    builder_version = Column(String(64), nullable=False, default="query-view-builder-v1")
    index_version = Column(String(64), nullable=False, default="instance-index-v1")
    metadata_digest = Column(String(128), nullable=False)
    content_digest = Column(String(128), nullable=True)
    graph_key = Column(String(200), nullable=False, unique=True)
    status = Column(String(20), nullable=False, default="building")
    object_count = Column(Integer, nullable=False, default=0)
    edge_count = Column(Integer, nullable=False, default=0)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    retention_until = Column(DateTime(timezone=True), nullable=True)
    refcount = Column(Integer, nullable=False, default=0)
    __table_args__ = (
        CheckConstraint("status IN ('building','validating','ready','failed','expired')", name="ck_query_view_status"),
        CheckConstraint("object_count >= 0 AND edge_count >= 0 AND refcount >= 0", name="ck_query_view_counts"),
    )


class QueryViewRetentionEvent(Base):
    """Durable audit record for physical graph cleanup after a view expires."""

    __tablename__ = "v2_query_view_retention_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    # These identifiers intentionally have no foreign keys: cleanup history must
    # survive deletion of the view row or its ontology.
    ontology_id = Column(String, nullable=False)
    view_id = Column(String(64), nullable=False)
    event_type = Column(String(40), nullable=False, default="graph_deleted")
    reason = Column(String(80), nullable=False, default="retention_expired")
    content_digest = Column(String(128), nullable=True)
    object_count = Column(Integer, nullable=False, default=0)
    edge_count = Column(Integer, nullable=False, default=0)
    retention_until = Column(DateTime(timezone=True), nullable=False)
    details = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("view_id", "event_type", name="uq_v2_query_view_retention_event"),
        CheckConstraint("event_type = 'graph_deleted'", name="ck_v2_query_view_retention_event_type"),
        CheckConstraint("reason = 'retention_expired'", name="ck_v2_query_view_retention_event_reason"),
        CheckConstraint("object_count >= 0 AND edge_count >= 0", name="ck_v2_query_view_retention_event_counts"),
        Index("ix_v2_query_view_retention_events_ontology_created", "ontology_id", "created_at"),
    )


@event.listens_for(QueryViewRetentionEvent, "before_update")
def immutable_query_view_retention_update(mapper, connection, target):
    raise ValueError("Query view retention events are append-only")


@event.listens_for(QueryViewRetentionEvent, "before_delete")
def immutable_query_view_retention_delete(mapper, connection, target):
    raise ValueError("Query view retention events are append-only")
