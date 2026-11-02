"""Persisted, auditable Agent runs and their proposed decisions."""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


AGENT_RUN_LEASE_SECONDS = 90


class AgentRun(Base):
    __tablename__ = "v2_agent_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="proposed", nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    session_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("v2_agent_sessions.id"), nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "client_request_id", name="uq_agent_run_session_request"),
        Index("ix_agent_runs_session", "session_id"),
    )


class AgentDefinition(Base):
    """Mutable authoring head for a versioned, ontology-scoped Agent."""

    __tablename__ = "v2_agent_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(
        String, ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draft_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    draft_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        CheckConstraint("status IN ('draft','published','withdrawn')", name="ck_v2_agent_definition_status"),
        CheckConstraint("draft_revision >= 1", name="ck_v2_agent_definition_draft_revision"),
        CheckConstraint("published_version IS NULL OR published_version >= 1", name="ck_v2_agent_definition_published_version"),
        Index("ix_v2_agent_definitions_ontology_status", "ontology_id", "status"),
        Index("ix_v2_agent_definitions_owner", "owner_id", "updated_at"),
    )


class AgentDefinitionVersion(Base):
    """Immutable published Agent configuration; never contains credentials."""

    __tablename__ = "v2_agent_definition_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    published_by: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("definition_id", "version", name="uq_v2_agent_definition_version"),
        CheckConstraint("version >= 1", name="ck_v2_agent_definition_version_no"),
        Index("ix_v2_agent_definition_versions_published", "definition_id", "published_at"),
    )


class AgentDefinitionEvent(Base):
    """Append-only lifecycle audit chain for Agent definitions."""

    __tablename__ = "v2_agent_definition_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("definition_id", "sequence", name="uq_v2_agent_definition_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_v2_agent_definition_event_sequence"),
        CheckConstraint(
            "event_type IN ('definition_created','draft_updated','published','withdrawn','reactivated')",
            name="ck_v2_agent_definition_event_type",
        ),
        Index("ix_v2_agent_definition_events_definition", "definition_id", "created_at"),
    )


class AgentSession(Base):
    __tablename__ = "v2_agent_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id"), nullable=False)
    owner_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="idle")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    permission_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_definition_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("v2_agent_definitions.id", ondelete="RESTRICT"), nullable=True
    )
    agent_definition_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("v2_agent_definition_versions.id", ondelete="RESTRICT"), nullable=True
    )
    agent_definition_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_case_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("v2_decision_cases.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_agent_sessions_ontology", "ontology_id"),
        Index("ix_agent_sessions_owner", "owner_id"),
        Index("ix_agent_sessions_status", "status"),
        Index("ix_agent_sessions_definition", "agent_definition_id", "agent_definition_version_id"),
        CheckConstraint(
            "(agent_definition_id IS NULL AND agent_definition_version_id IS NULL AND agent_definition_digest IS NULL) "
            "OR (agent_definition_id IS NOT NULL AND agent_definition_version_id IS NOT NULL AND agent_definition_digest IS NOT NULL)",
            name="ck_v2_agent_session_definition_pin",
        ),
    )


class AgentSessionParticipant(Base):
    __tablename__ = "v2_agent_session_participants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("v2_agent_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="viewer")
    granted_by: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "user_id", name="uq_agent_session_participant"),
        CheckConstraint("role = 'viewer'", name="ck_agent_session_participant_role"),
        Index("ix_v2_agent_session_participants_session_id", "session_id"),
        Index("ix_v2_agent_session_participants_user_id", "user_id"),
    )


class AgentContextItem(Base):
    __tablename__ = "v2_agent_context_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("v2_agent_sessions.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    permission_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_agent_context_session", "session_id"),)


class AgentEvent(Base):
    __tablename__ = "v2_agent_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("v2_agent_sessions.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_agent_event_sequence"),
        Index("ix_agent_events_session", "session_id"),
    )


class AgentCheckpoint(Base):
    __tablename__ = "v2_agent_checkpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("v2_agent_sessions.id"), nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tool_request_keys: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    pinned_versions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("session_id", "event_sequence", name="uq_agent_checkpoint_sequence"),
        Index("ix_agent_checkpoints_session", "session_id"),
    )
