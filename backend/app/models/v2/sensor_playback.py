"""Sensor playback: recorded trajectories replayed as if they arrived live, one governed rule evaluation per step."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SensorPlayback(Base):
    __tablename__ = "v2_sensor_playbacks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(60), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    series_property: Mapped[str] = mapped_column(String(100), nullable=False)
    # [{"asset_id", "object_id", "name", "unit"}]: which ontology object each recorded engine drives.
    mapping: Mapped[list] = mapped_column(JSON, nullable=False)
    base_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    step_cycles: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    latest_view_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    etag: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (Index("ix_v2_sensor_playbacks_ontology", "ontology_id", "created_at"),)


class SensorPlaybackTick(Base):
    """One step: the data view pinned for it, the rule evaluation on that view and the alerts it opened."""
    __tablename__ = "v2_sensor_playback_ticks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    playback_id: Mapped[str] = mapped_column(String(36), ForeignKey("v2_sensor_playbacks.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    view_id: Mapped[str] = mapped_column(String(64), nullable=False)  # views expire on their own schedule, so no foreign key
    evaluation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    new_alert_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("playback_id", "sequence", name="uq_v2_sensor_playback_tick_sequence"),)
