import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, Float, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class Relation(Base):
    __tablename__ = "relations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    source_entity: Mapped[str] = mapped_column(String, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    target_entity: Mapped[str] = mapped_column(String, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    # Durable Mapping Link ownership and the exact source snapshots used to
    # produce this PostgreSQL relationship fact. Legacy relations remain NULL.
    source_link_mapping_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_ontology_link_mappings.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    source_link_apply_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("v2_mapping_link_apply_runs.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    source_dataset_version_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    target_dataset_version_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
