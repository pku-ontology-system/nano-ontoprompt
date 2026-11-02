"""Make Scenario merge reconciliation attempts idempotent."""
from alembic import op
import sqlalchemy as sa


revision = "0053_scenario_merge_reconciliation_idempotency"
down_revision = "0052_action_manual_projection_budget"
branch_labels = None
depends_on = None


def upgrade():
    duplicate = op.get_bind().execute(sa.text(
        "SELECT scenario_id, request_id FROM v2_scenario_audits "
        "WHERE operation = 'merge_reconcile' AND request_id IS NOT NULL "
        "GROUP BY scenario_id, request_id HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError(
            "Duplicate Scenario merge reconciliation request IDs exist; reconcile before migration"
        )
    op.create_index(
        "uq_v2_scenario_merge_reconcile_request",
        "v2_scenario_audits",
        ["scenario_id", "request_id"],
        unique=True,
        postgresql_where=sa.text("operation = 'merge_reconcile' AND request_id IS NOT NULL"),
        sqlite_where=sa.text("operation = 'merge_reconcile' AND request_id IS NOT NULL"),
    )


def downgrade():
    has_reconciliation_attempts = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_scenario_audits "
        "WHERE operation = 'merge_reconcile' AND request_id IS NOT NULL LIMIT 1"
    )).first()
    if has_reconciliation_attempts:
        raise RuntimeError(
            "Scenario merge reconciliation evidence exists; downgrade would remove idempotency"
        )
    op.drop_index(
        "uq_v2_scenario_merge_reconcile_request", table_name="v2_scenario_audits"
    )
