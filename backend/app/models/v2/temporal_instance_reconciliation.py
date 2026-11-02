"""Append-only audit records for temporal observation instance reconciliation."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TemporalInstanceReconciliation(Base):
    __tablename__ = "v2_temporal_instance_reconciliations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False
    )
    replay_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_temporal_replays.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "replay_id", "idempotency_key",
            name="uq_v2_temporal_instance_reconciliation_request_key",
        ),
        Index("ix_v2_temporal_instance_reconciliations_ontology_created", "ontology_id", "created_at"),
    )
