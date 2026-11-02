"""Bind Temporal snapshots to manifests and compare publish base versions."""
from alembic import op
import sqlalchemy as sa


revision = "0075_temporal_snapshot_publish_cas"
down_revision = "0074_temporal_event_envelope_hash"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_temporal_replays",
        sa.Column("base_snapshot_id", sa.String(), nullable=True),
    )
    op.add_column(
        "v2_data_model_snapshots",
        sa.Column("watermark_sequence", sa.Integer(), nullable=True),
    )
    op.add_column(
        "v2_data_model_snapshots",
        sa.Column(
            "snapshot_hash_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_data_model_snapshots "
        "WHERE snapshot_hash_version > 1 LIMIT 1"
    )).first():
        raise RuntimeError(
            "Temporal snapshots contain v2 manifest hashes; downgrade would "
            "erase their integrity contract"
        )
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_temporal_replays "
        "WHERE base_snapshot_id IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError(
            "Temporal replays contain publish base versions; downgrade would "
            "erase their optimistic concurrency contract"
        )
    op.drop_column("v2_data_model_snapshots", "snapshot_hash_version")
    op.drop_column("v2_data_model_snapshots", "watermark_sequence")
    op.drop_column("v2_temporal_replays", "base_snapshot_id")
