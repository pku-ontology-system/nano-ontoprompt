"""Version temporal event idempotency hashes by normalized envelope contract."""
from alembic import op
import sqlalchemy as sa


revision = "0074_temporal_event_envelope_hash"
down_revision = "0073_scenario_system_actor"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_temporal_stream_events",
        sa.Column(
            "payload_hash_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_temporal_stream_events "
        "WHERE payload_hash_version > 1 LIMIT 1"
    )).first():
        raise RuntimeError(
            "temporal stream events contain v2 envelope hashes; "
            "downgrade would erase the hash contract version"
        )
    op.drop_column("v2_temporal_stream_events", "payload_hash_version")
