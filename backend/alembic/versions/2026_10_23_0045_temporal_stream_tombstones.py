"""Record upsert/delete operations for generic temporal stream events."""
from alembic import op
import sqlalchemy as sa


revision = "0045_temporal_stream_tombstones"
down_revision = "0044_mapping_apply_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_temporal_stream_events",
        sa.Column("operation", sa.String(length=20), nullable=False, server_default="upsert"),
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_temporal_stream_events WHERE operation = 'delete' LIMIT 1"
    )).first():
        raise RuntimeError("Temporal delete event audit facts exist; downgrade would erase operation history")
    op.drop_column("v2_temporal_stream_events", "operation")
