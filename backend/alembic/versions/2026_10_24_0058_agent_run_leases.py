"""Add fenced worker leases and acknowledged cancellation to Agent runs."""
from alembic import op
import sqlalchemy as sa


revision = "0058_agent_run_leases"
down_revision = "0057_action_run_request_key_length"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v2_agent_runs", sa.Column("worker_task_id", sa.String(length=200), nullable=True))
    op.add_column(
        "v2_agent_runs",
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("v2_agent_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_agent_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_agent_runs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_agent_runs", sa.Column("cancel_acknowledged_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_v2_agent_runs_lease_expires_at", "v2_agent_runs", ["lease_expires_at"])


def downgrade():
    has_lease_history = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_agent_runs WHERE lease_generation > 0 "
        "OR cancel_requested_at IS NOT NULL OR cancel_acknowledged_at IS NOT NULL LIMIT 1"
    )).first()
    if has_lease_history:
        raise RuntimeError(
            "Agent worker lease or cancellation history exists; downgrade would discard execution audit"
        )
    op.drop_index("ix_v2_agent_runs_lease_expires_at", table_name="v2_agent_runs")
    op.drop_column("v2_agent_runs", "cancel_acknowledged_at")
    op.drop_column("v2_agent_runs", "cancel_requested_at")
    op.drop_column("v2_agent_runs", "lease_expires_at")
    op.drop_column("v2_agent_runs", "heartbeat_at")
    op.drop_column("v2_agent_runs", "lease_generation")
    op.drop_column("v2_agent_runs", "worker_task_id")
