"""Normalize the Decision Lab index to its historical query contract."""
from alembic import op
import sqlalchemy as sa


revision = "0063_decision_lab_index_contract"
down_revision = "0062_action_event_delete_contract"
branch_labels = None
depends_on = None


TABLE = "v2_decision_lab_runs"
INDEX = "ix_decision_lab_domain_created"
LEGACY_INDEX = "ix_v2_decision_lab_runs_domain"
INDEX_COLUMNS = ["domain", "created_at"]


def upgrade():
    bind = op.get_bind()
    indexes = {item["name"]: item for item in sa.inspect(bind).get_indexes(TABLE)}

    current = indexes.get(INDEX)
    if current is not None and current.get("column_names") != INDEX_COLUMNS:
        op.drop_index(INDEX, table_name=TABLE)
        current = None

    if LEGACY_INDEX in indexes:
        op.drop_index(LEGACY_INDEX, table_name=TABLE)

    if current is None:
        op.create_index(INDEX, TABLE, INDEX_COLUMNS)


def downgrade():
    # Revision 0036 already defines this composite index. This forward-only
    # revision repairs schema drift and must not reintroduce the incorrect
    # single-column index when a database returns to revision 0062.
    return None
