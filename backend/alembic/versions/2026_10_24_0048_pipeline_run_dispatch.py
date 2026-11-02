"""Persist Pipeline Run idempotency keys and recoverable dispatch state."""
from alembic import op
import sqlalchemy as sa


revision = "0048_pipeline_run_dispatch"
down_revision = "0047_pipeline_execution_manifests"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v2_pipeline_runs", sa.Column("request_key", sa.String(length=128), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("request_actor_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_v2_pipeline_runs_request_actor", "v2_pipeline_runs", "users",
        ["request_actor_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column("v2_pipeline_runs", sa.Column("request_fingerprint", sa.String(length=64), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("dispatch_status", sa.String(length=24), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("dispatch_attempts", sa.Integer(), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("dispatch_next_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("dispatch_claim_token", sa.String(length=36), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("dispatch_task_id", sa.String(length=100), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("dispatch_error", sa.Text(), nullable=True))

    op.execute(sa.text(
        "UPDATE v2_pipeline_runs SET dispatch_status = "
        "CASE WHEN status = 'pending' THEN 'pending' ELSE 'legacy' END, "
        "dispatch_attempts = 0 WHERE dispatch_status IS NULL"
    ))
    op.alter_column(
        "v2_pipeline_runs", "dispatch_status", existing_type=sa.String(length=24),
        nullable=False, server_default="pending",
    )
    op.alter_column(
        "v2_pipeline_runs", "dispatch_attempts", existing_type=sa.Integer(),
        nullable=False, server_default="0",
    )
    op.create_unique_constraint(
        "uq_v2_pipeline_run_request_key", "v2_pipeline_runs",
        ["pipeline_id", "request_actor_id", "request_key"],
    )
    op.create_index(
        "ix_v2_pipeline_runs_dispatch_due", "v2_pipeline_runs",
        ["dispatch_status", "dispatch_next_at"],
    )


def downgrade():
    bind = op.get_bind()
    has_dispatch_evidence = bind.execute(sa.text(
        "SELECT 1 FROM v2_pipeline_runs WHERE request_key IS NOT NULL "
        "OR request_actor_id IS NOT NULL OR request_fingerprint IS NOT NULL "
        "OR (dispatch_status IS NOT NULL AND dispatch_status <> 'legacy') "
        "OR dispatch_attempts > 0 OR dispatch_next_at IS NOT NULL "
        "OR dispatch_lease_until IS NOT NULL OR dispatch_claim_token IS NOT NULL "
        "OR dispatch_task_id IS NOT NULL OR dispatched_at IS NOT NULL "
        "OR dispatch_error IS NOT NULL LIMIT 1"
    )).first()
    if has_dispatch_evidence:
        raise RuntimeError("Pipeline Run dispatch/idempotency evidence exists; downgrade would discard recovery state")

    op.drop_index("ix_v2_pipeline_runs_dispatch_due", table_name="v2_pipeline_runs")
    op.drop_constraint("uq_v2_pipeline_run_request_key", "v2_pipeline_runs", type_="unique")
    op.drop_column("v2_pipeline_runs", "dispatch_error")
    op.drop_column("v2_pipeline_runs", "dispatched_at")
    op.drop_column("v2_pipeline_runs", "dispatch_task_id")
    op.drop_column("v2_pipeline_runs", "dispatch_claim_token")
    op.drop_column("v2_pipeline_runs", "dispatch_lease_until")
    op.drop_column("v2_pipeline_runs", "dispatch_next_at")
    op.drop_column("v2_pipeline_runs", "dispatch_attempts")
    op.drop_column("v2_pipeline_runs", "dispatch_status")
    op.drop_column("v2_pipeline_runs", "request_fingerprint")
    op.drop_column("v2_pipeline_runs", "request_key")
    op.drop_constraint("fk_v2_pipeline_runs_request_actor", "v2_pipeline_runs", type_="foreignkey")
    op.drop_column("v2_pipeline_runs", "request_actor_id")
