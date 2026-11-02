"""Persist retry schedule for Action projection delivery."""
from alembic import op
import sqlalchemy as sa


revision = "0042_action_projection_retries"
down_revision = "0041_action_run_case_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_ontology_action_runs",
        sa.Column("projection_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "v2_ontology_action_runs",
        sa.Column("projection_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "v2_ontology_action_runs",
        sa.Column("projection_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE v2_ontology_action_runs "
        "SET projection_next_attempt_at = CURRENT_TIMESTAMP "
        "WHERE projection_status IN ('pending', 'failed')"
    )
    op.create_index(
        "ix_action_run_projection_due",
        "v2_ontology_action_runs",
        ["projection_status", "projection_next_attempt_at"],
    )


def downgrade():
    has_retry_evidence = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_ontology_action_runs "
        "WHERE projection_attempts > 0 "
        "OR projection_next_attempt_at IS NOT NULL "
        "OR projection_last_attempt_at IS NOT NULL LIMIT 1"
    )).first()
    if has_retry_evidence:
        raise RuntimeError(
            "Action projection retry evidence exists; downgrade would discard recovery state"
        )
    op.drop_index("ix_action_run_projection_due", table_name="v2_ontology_action_runs")
    op.drop_column("v2_ontology_action_runs", "projection_last_attempt_at")
    op.drop_column("v2_ontology_action_runs", "projection_next_attempt_at")
    op.drop_column("v2_ontology_action_runs", "projection_attempts")
