import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, JSON, Text, ForeignKey, Integer, Index, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class Pipeline(Base):
    __tablename__ = "v2_pipelines"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(100), nullable=True, default="通用")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    source_dataset_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_datasets.id"), nullable=True)
    route: Mapped[str | None] = mapped_column(String(1), nullable=True)  # A|B|C (legacy, inferred from definition)
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # legacy steps format
    definition: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # new DSL: {nodes: [...], edges: [...]}
    target_curated_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    schedule_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|editing|running|failed|published
    branch: Mapped[str | None] = mapped_column(String(50), nullable=True, default="main")
    version: Mapped[int] = mapped_column(default=1)
    created_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class PipelineVersion(Base):
    __tablename__ = "v2_pipeline_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(String, ForeignKey("v2_pipelines.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    definition: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    definition_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PipelineRun(Base):
    __tablename__ = "v2_pipeline_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(String, ForeignKey("v2_pipelines.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")  # pending|running|success|degraded|failed|cancelled
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    dataset_version_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_dataset_versions.id"), nullable=True)
    pipeline_version_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_pipeline_versions.id", ondelete="SET NULL"), nullable=True
    )
    execution_mode: Mapped[str | None] = mapped_column(String(40), nullable=True)
    execution_manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    execution_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_actor_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    service_principal_id: Mapped[str | None] = mapped_column(
        String(100), ForeignKey("v2_service_principals.id", ondelete="RESTRICT"), nullable=True
    )
    request_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatch_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatch_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dispatch_task_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "pipeline_id", "request_actor_id", "request_key",
            name="uq_v2_pipeline_run_request_key",
        ),
        Index("ix_v2_pipeline_runs_pipeline_version_id", "pipeline_version_id"),
        Index("ix_v2_pipeline_runs_service_principal_id", "service_principal_id"),
        Index("ix_v2_pipeline_runs_execution_digest", "execution_digest"),
        Index("ix_v2_pipeline_runs_dispatch_due", "dispatch_status", "dispatch_next_at"),
    )


class PipelineSchedule(Base):
    """Current durable schedule definition; edits create immutable revisions."""

    __tablename__ = "v2_pipeline_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    pipeline_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_pipelines.id", ondelete="RESTRICT"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    steward_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    service_principal_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("v2_service_principals.id", ondelete="RESTRICT"),
        nullable=False, default="system:pipeline-scheduler"
    )
    pipeline_version_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_pipeline_versions.id", ondelete="RESTRICT"), nullable=False
    )
    cron_expression: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False, default="UTC")
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    misfire_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="skip")
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    overlap_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="forbid")
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("pipeline_id", name="uq_v2_pipeline_schedules_pipeline"),
        CheckConstraint("revision >= 1", name="ck_v2_pipeline_schedule_revision"),
        CheckConstraint("misfire_policy IN ('skip','run_once')", name="ck_v2_pipeline_schedule_misfire"),
        CheckConstraint("overlap_policy IN ('forbid','allow')", name="ck_v2_pipeline_schedule_overlap"),
        CheckConstraint("misfire_grace_seconds >= 0", name="ck_v2_pipeline_schedule_grace"),
        Index("ix_v2_pipeline_schedules_due", "enabled", "next_fire_at"),
    )


class PipelineScheduleRevision(Base):
    """Immutable schedule configuration and version pin for audit and replay."""

    __tablename__ = "v2_pipeline_schedule_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    schedule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_pipeline_schedules.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    pipeline_version_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_pipeline_versions.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("schedule_id", "revision", name="uq_v2_pipeline_schedule_revision"),
        CheckConstraint("revision >= 1", name="ck_v2_pipeline_schedule_revision_no"),
        Index("ix_v2_pipeline_schedule_revisions_version", "pipeline_version_id"),
    )


class PipelineScheduleRun(Base):
    """One idempotent scheduled tick linked to its durable PipelineRun."""

    __tablename__ = "v2_pipeline_schedule_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    schedule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_pipeline_schedules.id", ondelete="RESTRICT"), nullable=False
    )
    schedule_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_pipeline_schedule_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_slot: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    pipeline_run_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_pipeline_runs.id", ondelete="RESTRICT"), nullable=True
    )
    coalesced_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("schedule_revision_id", "local_slot", name="uq_v2_pipeline_schedule_tick"),
        UniqueConstraint("pipeline_run_id", name="uq_v2_pipeline_schedule_run_link"),
        CheckConstraint(
            "status IN ('pending','dispatching','queued','running','success','degraded','failed','skipped','cancelled')",
            name="ck_v2_pipeline_schedule_run_status",
        ),
        Index("ix_v2_pipeline_schedule_runs_due", "status", "scheduled_for"),
        Index("ix_v2_pipeline_schedule_runs_schedule", "schedule_id", "created_at"),
    )
