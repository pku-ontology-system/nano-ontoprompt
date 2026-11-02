"""Record temporal vector index retention and allow pruned generations."""
from alembic import op
import sqlalchemy as sa


revision = "0068_temporal_vector_index_retention"
down_revision = "0067_temporal_vector_index_jobs"
branch_labels = None
depends_on = None


JOBS = "v2_temporal_vector_index_jobs"
EVENTS = "v2_temporal_vector_index_retention_events"


def upgrade():
    op.drop_constraint("ck_v2_temporal_vector_index_job_status", JOBS, type_="check")
    op.create_check_constraint(
        "ck_v2_temporal_vector_index_job_status",
        JOBS,
        "status IN ('queued','running','completed','failed','pruned')",
    )
    op.drop_index("ix_v2_temporal_vector_index_jobs_replay", table_name=JOBS)
    op.create_index(
        "ix_v2_temporal_vector_index_jobs_replay",
        JOBS,
        ["replay_id", "completed_at"],
    )
    op.create_table(
        EVENTS,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=True),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("replay_id", sa.String(), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=40), server_default="index_pruned", nullable=False),
        sa.Column("reason", sa.String(length=80), nullable=False),
        sa.Column("details", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], [f"{JOBS}.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("event_type = 'index_pruned'", name="ck_v2_temporal_vector_index_retention_event_type"),
    )
    op.create_index(
        "ix_v2_temporal_vector_index_retention_events_replay",
        EVENTS,
        ["replay_id", "created_at"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {EVENTS} LIMIT 1")).first():
        raise RuntimeError("Temporal vector index retention events exist; downgrade would erase retention audit")
    if bind.execute(sa.text(f"SELECT 1 FROM {JOBS} WHERE status = 'pruned' LIMIT 1")).first():
        raise RuntimeError("Pruned temporal vector index jobs exist; downgrade would erase retention state")
    op.drop_index("ix_v2_temporal_vector_index_retention_events_replay", table_name=EVENTS)
    op.drop_table(EVENTS)
    op.drop_index("ix_v2_temporal_vector_index_jobs_replay", table_name=JOBS)
    op.create_index(
        "ix_v2_temporal_vector_index_jobs_replay",
        JOBS,
        ["replay_id", "created_at"],
    )
    op.drop_constraint("ck_v2_temporal_vector_index_job_status", JOBS, type_="check")
    op.create_check_constraint(
        "ck_v2_temporal_vector_index_job_status",
        JOBS,
        "status IN ('queued','running','completed','failed')",
    )
