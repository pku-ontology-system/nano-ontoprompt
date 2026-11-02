"""Persist temporal vector index builds and their bounded worker progress."""
from alembic import op
import sqlalchemy as sa


revision = "0067_temporal_vector_index_jobs"
down_revision = "0066_temporal_instance_reconciliation"
branch_labels = None
depends_on = None


TABLE = "v2_temporal_vector_index_jobs"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("replay_id", sa.String(), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("total_facts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("indexed_facts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_fact_id", sa.String(), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("execution_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("celery_task_id", sa.String(length=120), nullable=True),
        sa.Column("worker_token", sa.String(length=80), nullable=True),
        sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["replay_id"], ["v2_temporal_replays.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("replay_id", "source_revision", name="uq_v2_temporal_vector_index_revision"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_v2_temporal_vector_index_job_status",
        ),
    )
    op.create_index(
        "ix_v2_temporal_vector_index_jobs_dispatch",
        TABLE,
        ["status", "next_attempt_at", "updated_at"],
    )
    op.create_index(
        "ix_v2_temporal_vector_index_jobs_replay",
        TABLE,
        ["replay_id", "created_at"],
    )


def downgrade():
    has_jobs = op.get_bind().execute(sa.text(f"SELECT 1 FROM {TABLE} LIMIT 1")).first()
    if has_jobs:
        raise RuntimeError("Temporal vector index job history exists; downgrade would erase task evidence")
    op.drop_index("ix_v2_temporal_vector_index_jobs_replay", table_name=TABLE)
    op.drop_index("ix_v2_temporal_vector_index_jobs_dispatch", table_name=TABLE)
    op.drop_table(TABLE)
