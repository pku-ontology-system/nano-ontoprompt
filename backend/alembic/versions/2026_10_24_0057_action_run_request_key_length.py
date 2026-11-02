"""Widen ActionRun request IDs to match Scenario Workbench's API contract."""
from alembic import op
import sqlalchemy as sa


revision = "0057_action_run_request_key_length"
down_revision = "0056_agent_session_participants"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("v2_ontology_action_runs") as batch:
        batch.alter_column(
            "client_request_id",
            existing_type=sa.String(length=120),
            type_=sa.String(length=200),
            existing_nullable=True,
        )


def downgrade():
    has_long_request_ids = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_ontology_action_runs "
        "WHERE length(client_request_id) > 120 LIMIT 1"
    )).first()
    if has_long_request_ids:
        raise RuntimeError(
            "ActionRun request IDs longer than 120 characters exist; "
            "downgrade would truncate idempotency keys"
        )
    with op.batch_alter_table("v2_ontology_action_runs") as batch:
        batch.alter_column(
            "client_request_id",
            existing_type=sa.String(length=200),
            type_=sa.String(length=120),
            existing_nullable=True,
        )
