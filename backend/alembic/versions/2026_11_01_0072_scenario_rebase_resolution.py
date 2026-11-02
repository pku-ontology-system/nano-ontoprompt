"""Idempotent audit key for authorized Tracking rebase conflict decisions."""
from alembic import op
import sqlalchemy as sa


revision = "0072_scenario_rebase_resolution"
down_revision = "0071_scenario_checkpoints"
branch_labels = None
depends_on = None


AUDITS = "v2_scenario_audits"
INDEX = "uq_v2_scenario_rebase_resolution_request"


def upgrade():
    op.create_index(
        INDEX,
        AUDITS,
        ["scenario_id", "request_id"],
        unique=True,
        postgresql_where=sa.text("operation = 'resolve_rebase_conflicts' AND request_id IS NOT NULL"),
        sqlite_where=sa.text("operation = 'resolve_rebase_conflicts' AND request_id IS NOT NULL"),
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        f"SELECT 1 FROM {AUDITS} WHERE operation = 'resolve_rebase_conflicts' LIMIT 1"
    )).first():
        raise RuntimeError("Rebase conflict decisions exist; downgrade would erase idempotency protection")
    op.drop_index(INDEX, table_name=AUDITS)
