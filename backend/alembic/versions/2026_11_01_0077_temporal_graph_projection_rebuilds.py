"""Track full SQL-to-FalkorDB temporal projection rebuilds."""
from alembic import op
import sqlalchemy as sa


revision = "0077_temporal_graph_projection_rebuilds"
down_revision = "0076_temporal_stream_worker_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v2_temporal_graph_projection_jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("replay_id", sa.String(), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=False),
        sa.Column("graph_namespace", sa.String(length=240), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("expected_nodes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("expected_edges", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("written_nodes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("written_edges", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("verified_nodes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("verified_edges", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("execution_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("celery_task_id", sa.String(length=120), nullable=True),
        sa.Column("worker_token", sa.String(length=80), nullable=True),
        sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued','running','completed','failed')", name="ck_v2_temporal_graph_projection_status"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["replay_id"], ["v2_temporal_replays.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("replay_id", "source_revision", name="uq_v2_temporal_graph_projection_revision"),
    )
    op.create_index(
        "ix_v2_temporal_graph_projection_dispatch",
        "v2_temporal_graph_projection_jobs",
        ["status", "next_attempt_at", "updated_at"],
    )
    op.create_index(
        "ix_v2_temporal_graph_projection_replay",
        "v2_temporal_graph_projection_jobs",
        ["replay_id", "created_at"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_temporal_graph_projection_jobs "
        "WHERE status IN ('running','completed') OR execution_attempts > 0 LIMIT 1"
    )).first():
        raise RuntimeError("Temporal graph rebuilds have started; downgrade would erase their audit history")
    op.drop_index("ix_v2_temporal_graph_projection_replay", table_name="v2_temporal_graph_projection_jobs")
    op.drop_index("ix_v2_temporal_graph_projection_dispatch", table_name="v2_temporal_graph_projection_jobs")
    op.drop_table("v2_temporal_graph_projection_jobs")
