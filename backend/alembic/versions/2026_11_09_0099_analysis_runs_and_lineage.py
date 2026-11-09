"""Add lineage events and analysis runs."""
from alembic import op
import sqlalchemy as sa


revision = "0099_analysis_runs_lineage"
down_revision = "0098_sensor_playbacks"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v2_lineage_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("job_namespace", sa.String(length=200), nullable=False),
        sa.Column("job_name", sa.String(length=400), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_v2_lineage_events_job", "v2_lineage_events", ["job_namespace", "job_name", "event_time"])
    op.create_index("ix_v2_lineage_events_run", "v2_lineage_events", ["run_id"])
    op.create_index("ix_v2_lineage_events_source", "v2_lineage_events", ["source"])

    op.create_table(
        "v2_analysis_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("dataset_id", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_v2_analysis_runs_kind", "v2_analysis_runs", ["kind", "dataset_id", "created_at"])


def downgrade():
    op.drop_index("ix_v2_analysis_runs_kind", table_name="v2_analysis_runs")
    op.drop_table("v2_analysis_runs")
    op.drop_index("ix_v2_lineage_events_source", table_name="v2_lineage_events")
    op.drop_index("ix_v2_lineage_events_run", table_name="v2_lineage_events")
    op.drop_index("ix_v2_lineage_events_job", table_name="v2_lineage_events")
    op.drop_table("v2_lineage_events")
