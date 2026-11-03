"""Add sensor playbacks and their steps."""
from alembic import op
import sqlalchemy as sa


revision = "0098_sensor_playbacks"
down_revision = "0097_ontology_candidates"
branch_labels = None
depends_on = None


PLAYBACKS = "v2_sensor_playbacks"
TICKS = "v2_sensor_playback_ticks"


def upgrade():
    op.create_table(
        PLAYBACKS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("ontology_id", sa.String(), sa.ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("dataset_id", sa.String(length=60), nullable=False),
        sa.Column("rule_id", sa.String(length=36), nullable=False),
        sa.Column("series_property", sa.String(length=100), nullable=False),
        sa.Column("mapping", sa.JSON(), nullable=False),
        sa.Column("base_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_cycle", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("step_cycles", sa.Integer(), nullable=False),
        sa.Column("max_cycle", sa.Integer(), nullable=False),
        sa.Column("latest_view_id", sa.String(length=64), nullable=True),
        sa.Column("etag", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_v2_sensor_playbacks_ontology", PLAYBACKS, ["ontology_id", "created_at"])

    op.create_table(
        TICKS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("playback_id", sa.String(length=36), sa.ForeignKey(f"{PLAYBACKS}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("view_id", sa.String(length=64), nullable=False),
        sa.Column("evaluation_id", sa.String(length=36), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("new_alert_ids", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("playback_id", "sequence", name="uq_v2_sensor_playback_tick_sequence"),
    )


def downgrade():
    op.drop_table(TICKS)
    op.drop_index("ix_v2_sensor_playbacks_ontology", table_name=PLAYBACKS)
    op.drop_table(PLAYBACKS)
