"""Analysis after an ontology exists: lineage events (OpenLineage-shaped) and the runs of the data-health and entity-resolution engines."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LineageEvent(Base):
    """One OpenLineage run event as received. The lineage graph is derived from these on read, so nothing here is edited."""
    __tablename__ = "v2_lineage_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_namespace: Mapped[str] = mapped_column(String(200), nullable=False)
    job_name: Mapped[str] = mapped_column(String(400), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(60), nullable=False, default="api")  # api, demo:marquez, pipeline
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_v2_lineage_events_job", "job_namespace", "job_name", "event_time"),
        Index("ix_v2_lineage_events_run", "run_id"),
        Index("ix_v2_lineage_events_source", "source"),
    )


class AnalysisRun(Base):
    """A finished run of the health engine, the resolution engine or the pipeline that chains them."""
    __tablename__ = "v2_analysis_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # health, resolution, pipeline
    dataset_id: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (Index("ix_v2_analysis_runs_kind", "kind", "dataset_id", "created_at"),)
