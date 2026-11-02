"""PRD v1.1 Ontology Action Type & Action Run 模型"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class OntologyActionType(Base):
    """动作类型 — 可执行业务操作"""
    __tablename__ = "v2_ontology_action_types"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_entity_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    action_category: Mapped[str] = mapped_column(String(50), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    submission_criteria: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    effects: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    side_effects: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    permission_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    backed_by_function: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state_machine_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_ontology_state_machines.id", ondelete="SET NULL"), nullable=True)
    transition_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                                                 onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("state_machine_id", "transition_key", name="uq_action_state_transition"),)


class OntologyActionRun(Base):
    """动作执行记录 — 审计与回滚"""
    __tablename__ = "v2_ontology_action_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    action_type_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_ontology_action_types.id"), nullable=True)
    ontology_id: Mapped[str] = mapped_column(String, ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False)
    target_object_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    execution_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    before_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    side_effect_results: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Keep requester, reviewer and executor distinct. Legacy runs remain NULL
    # where the original request or approval actor cannot be proven.
    requested_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    executed_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # New reviewed runs use these fields; historical runs remain readable
    # without inventing approval or provenance that was never recorded.
    decision_case_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_scope: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permission_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_etag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Scenario Action retries use a fenced, renewable claim. Historical and
    # non-Scenario runs keep attempt=0 with no lease fields.
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    execution_lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A leased Scenario revision records its isolated graph key before the
    # external write. A reconciler can then remove an unreferenced graph if
    # the process exits before the SQL publication transaction commits.
    scenario_build_graph_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scenario_build_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    projection_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    projection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    projection_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Manual operator retries are accounted separately and never replenish the
    # automatic retry budget stored in projection_attempts.
    projection_manual_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    projection_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projection_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("decision_case_id", name="uq_action_run_decision_case"),
        Index("ix_action_runs_decision_case", "decision_case_id"),
        Index("ix_action_run_projection_due", "projection_status", "projection_next_attempt_at"),
        Index("ix_action_run_execution_lease", "status", "execution_lease_until"),
        CheckConstraint("execution_attempt >= 0", name="ck_action_run_execution_attempt_nonnegative"),
        CheckConstraint(
            "(execution_lease_token IS NULL AND execution_lease_until IS NULL) OR "
            "(execution_lease_token IS NOT NULL AND execution_lease_until IS NOT NULL)",
            name="ck_action_run_execution_lease_pair",
        ),
        CheckConstraint(
            "(scenario_build_graph_key IS NULL AND scenario_build_revision IS NULL) OR "
            "(scenario_build_graph_key IS NOT NULL AND scenario_build_revision IS NOT NULL "
            "AND scenario_build_revision >= 1)",
            name="ck_action_run_scenario_build_pair",
        ),
        Index(
            "ix_action_run_scenario_build_recovery",
            "scenario_build_graph_key", "status",
        ),
        Index(
            "uq_action_run_request_scope",
            "ontology_id", "executed_by", "idempotency_scope", "client_request_id",
            unique=True,
            postgresql_where=text("idempotency_scope IS NOT NULL AND client_request_id IS NOT NULL"),
            sqlite_where=text("idempotency_scope IS NOT NULL AND client_request_id IS NOT NULL"),
        ),
    )


class OntologyActionRunEvent(Base):
    """Append-only, digest-chained lifecycle evidence for an ActionRun."""
    __tablename__ = "v2_ontology_action_run_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    action_run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("v2_ontology_action_runs.id", name="fk_action_run_event_run"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permission_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("action_run_id", "sequence", name="uq_action_run_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_action_run_event_sequence_positive"),
        CheckConstraint(
            "event_type IN ('created', 'status_changed')",
            name="ck_action_run_event_type",
        ),
        CheckConstraint(
            "actor_kind IN ('user', 'agent', 'worker', 'system')",
            name="ck_action_run_event_actor_kind",
        ),
        Index("ix_action_run_events_run_sequence", "action_run_id", "sequence"),
    )


@event.listens_for(OntologyActionRunEvent, "before_update")
def _reject_action_run_event_update(_mapper, _connection, _target):
    raise ValueError("ActionRun lifecycle events are append-only")


@event.listens_for(OntologyActionRunEvent, "before_delete")
def _reject_action_run_event_delete(_mapper, _connection, _target):
    raise ValueError("ActionRun lifecycle events are append-only")


class OntologyActionRunClaimEvent(Base):
    """Append-only digest chain for execution lease claims and their outcomes."""
    __tablename__ = "v2_ontology_action_run_claim_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    action_run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("v2_ontology_action_runs.id", name="fk_action_run_claim_event_run"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("action_run_id", "sequence", name="uq_action_run_claim_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_action_run_claim_event_sequence_positive"),
        CheckConstraint("attempt_number >= 1", name="ck_action_run_claim_event_attempt_positive"),
        CheckConstraint(
            "event_type IN ('claim_acquired', 'lease_renewed', 'attempt_expired', "
            "'attempt_completed', 'attempt_failed', 'attempt_reconciliation_required', "
            "'graph_write_started', 'graph_orphan_deleted', 'graph_cleanup_failed', "
            "'graph_reference_preserved')",
            name="ck_action_run_claim_event_type",
        ),
        CheckConstraint(
            "actor_kind IN ('user', 'agent', 'worker', 'system')",
            name="ck_action_run_claim_event_actor_kind",
        ),
        Index("ix_action_run_claim_events_run_sequence", "action_run_id", "sequence"),
    )


@event.listens_for(OntologyActionRunClaimEvent, "before_update")
def _reject_action_run_claim_event_update(_mapper, _connection, _target):
    raise ValueError("ActionRun execution claim events are append-only")


@event.listens_for(OntologyActionRunClaimEvent, "before_delete")
def _reject_action_run_claim_event_delete(_mapper, _connection, _target):
    raise ValueError("ActionRun execution claim events are append-only")
