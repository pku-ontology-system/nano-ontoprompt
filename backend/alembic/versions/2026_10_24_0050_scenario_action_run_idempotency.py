"""Add scoped request idempotency for Scenario Action Runs."""
from alembic import op
import sqlalchemy as sa


revision = "0050_scenario_action_run_idempotency"
down_revision = "0049_scenario_merge_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_ontology_action_runs",
        sa.Column("idempotency_scope", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "uq_action_run_request_scope",
        "v2_ontology_action_runs",
        ["ontology_id", "executed_by", "idempotency_scope", "client_request_id"],
        unique=True,
        postgresql_where=sa.text(
            "idempotency_scope IS NOT NULL AND client_request_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "idempotency_scope IS NOT NULL AND client_request_id IS NOT NULL"
        ),
    )


def downgrade():
    has_scoped_runs = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_ontology_action_runs "
        "WHERE idempotency_scope IS NOT NULL LIMIT 1"
    )).first()
    if has_scoped_runs:
        raise RuntimeError(
            "Scenario Action request evidence exists; downgrade would remove its idempotency guard"
        )
    op.drop_index("uq_action_run_request_scope", table_name="v2_ontology_action_runs")
    op.drop_column("v2_ontology_action_runs", "idempotency_scope")
