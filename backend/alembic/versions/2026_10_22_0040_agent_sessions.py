"""Persist Agent sessions, typed context, events, checkpoints and run linkage."""
from alembic import op
import sqlalchemy as sa

revision = "0040_agent_sessions"
down_revision = "0039_action_request_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v2_agent_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ontology_id", sa.String(), sa.ForeignKey("ontology_projects.id"), nullable=False),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("metadata_revision_id", sa.String(), nullable=True),
        sa.Column("permission_digest", sa.String(64), nullable=True),
        sa.Column("pending_case_id", sa.String(36), sa.ForeignKey("v2_decision_cases.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_sessions_ontology", "v2_agent_sessions", ["ontology_id"])
    op.create_index("ix_agent_sessions_owner", "v2_agent_sessions", ["owner_id"])
    op.create_index("ix_agent_sessions_status", "v2_agent_sessions", ["status"])
    op.create_table(
        "v2_agent_context_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("v2_agent_sessions.id"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("ref_id", sa.String(200), nullable=False),
        sa.Column("source_version", sa.String(200), nullable=True),
        sa.Column("permission_digest", sa.String(64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_context_session", "v2_agent_context_items", ["session_id"])
    op.create_table(
        "v2_agent_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("v2_agent_sessions.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("actor_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("trace_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_agent_event_sequence"),
    )
    op.create_index("ix_agent_events_session", "v2_agent_events", ["session_id"])
    op.create_table(
        "v2_agent_checkpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("v2_agent_sessions.id"), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("tool_request_keys", sa.JSON(), nullable=False),
        sa.Column("pinned_versions", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "event_sequence", name="uq_agent_checkpoint_sequence"),
    )
    op.create_index("ix_agent_checkpoints_session", "v2_agent_checkpoints", ["session_id"])
    op.add_column("v2_agent_runs", sa.Column("session_id", sa.String(36), sa.ForeignKey("v2_agent_sessions.id"), nullable=True))
    op.add_column("v2_agent_runs", sa.Column("client_request_id", sa.String(120), nullable=True))
    op.add_column("v2_agent_runs", sa.Column("input_digest", sa.String(64), nullable=True))
    op.add_column("v2_agent_runs", sa.Column("output_digest", sa.String(64), nullable=True))
    op.add_column("v2_agent_runs", sa.Column("trace_id", sa.String(36), nullable=True))
    op.add_column("v2_agent_runs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_agent_runs_session", "v2_agent_runs", ["session_id"])
    op.create_unique_constraint("uq_agent_run_session_request", "v2_agent_runs", ["session_id", "client_request_id"])


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM v2_agent_sessions LIMIT 1")).first():
        raise RuntimeError("Agent session audit facts exist; downgrade would delete them")
    op.drop_constraint("uq_agent_run_session_request", "v2_agent_runs", type_="unique")
    op.drop_index("ix_agent_runs_session", table_name="v2_agent_runs")
    for name in ("completed_at", "trace_id", "output_digest", "input_digest", "client_request_id", "session_id"):
        op.drop_column("v2_agent_runs", name)
    op.drop_index("ix_agent_checkpoints_session", table_name="v2_agent_checkpoints")
    op.drop_table("v2_agent_checkpoints")
    op.drop_index("ix_agent_events_session", table_name="v2_agent_events")
    op.drop_table("v2_agent_events")
    op.drop_index("ix_agent_context_session", table_name="v2_agent_context_items")
    op.drop_table("v2_agent_context_items")
    op.drop_index("ix_agent_sessions_status", table_name="v2_agent_sessions")
    op.drop_index("ix_agent_sessions_owner", table_name="v2_agent_sessions")
    op.drop_index("ix_agent_sessions_ontology", table_name="v2_agent_sessions")
    op.drop_table("v2_agent_sessions")
