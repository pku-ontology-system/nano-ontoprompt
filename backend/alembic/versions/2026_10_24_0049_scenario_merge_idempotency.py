"""Make Scenario merge requests durable and idempotent."""
from alembic import op
import sqlalchemy as sa


revision = "0049_scenario_merge_idempotency"
down_revision = "0048_pipeline_run_dispatch"
branch_labels = None
depends_on = None


def upgrade():
    duplicate = op.get_bind().execute(sa.text(
        "SELECT scenario_id, request_id FROM v2_scenario_audits "
        "WHERE operation = 'merge' AND request_id IS NOT NULL "
        "GROUP BY scenario_id, request_id HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError(
            "Duplicate Scenario merge request IDs exist; reconcile before adding the idempotency index"
        )

    op.create_index(
        "uq_v2_scenario_merge_request",
        "v2_scenario_audits",
        ["scenario_id", "request_id"],
        unique=True,
        postgresql_where=sa.text("operation = 'merge' AND request_id IS NOT NULL"),
        sqlite_where=sa.text("operation = 'merge' AND request_id IS NOT NULL"),
    )


def downgrade():
    has_merge_requests = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_scenario_audits "
        "WHERE operation = 'merge' AND request_id IS NOT NULL LIMIT 1"
    )).first()
    if has_merge_requests:
        raise RuntimeError(
            "Scenario merge idempotency evidence exists; downgrade would remove its uniqueness guard"
        )
    op.drop_index("uq_v2_scenario_merge_request", table_name="v2_scenario_audits")
