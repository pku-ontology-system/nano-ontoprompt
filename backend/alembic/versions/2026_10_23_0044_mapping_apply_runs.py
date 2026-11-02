"""Persist idempotent Mapping Apply runs and recovery leases."""
from alembic import op
import sqlalchemy as sa


revision = "0044_mapping_apply_runs"
down_revision = "0043_review_dataset_version"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v2_mapping_apply_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("mapping_id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("curated_dataset_id", sa.String(), nullable=False),
        sa.Column("review_id", sa.String(), nullable=False),
        sa.Column("dataset_version_id", sa.String(length=120), nullable=False),
        sa.Column("mapping_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("mapping_digest", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("execution_attempts", sa.Integer(), nullable=False),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False),
        sa.Column("celery_task_id", sa.String(length=200), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["mapping_id"], ["v2_ontology_mappings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["curated_dataset_id"], ["v2_curated_datasets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["review_id"], ["v2_curated_reviews.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mapping_id", "review_id", name="uq_mapping_apply_run_mapping_review"),
        sa.UniqueConstraint("idempotency_key", name="uq_mapping_apply_run_idempotency_key"),
    )
    op.create_index(
        "ix_mapping_apply_run_dispatch_due",
        "v2_mapping_apply_runs",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_mapping_apply_run_dispatch_lease",
        "v2_mapping_apply_runs",
        ["status", "dispatch_lease_until"],
    )
    op.create_index(
        "ix_mapping_apply_run_execution_lease",
        "v2_mapping_apply_runs",
        ["status", "run_lease_until"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM v2_mapping_apply_runs LIMIT 1")).first():
        raise RuntimeError("Mapping Apply run audit facts exist; downgrade would delete them")
    op.drop_index("ix_mapping_apply_run_execution_lease", table_name="v2_mapping_apply_runs")
    op.drop_index("ix_mapping_apply_run_dispatch_lease", table_name="v2_mapping_apply_runs")
    op.drop_index("ix_mapping_apply_run_dispatch_due", table_name="v2_mapping_apply_runs")
    op.drop_table("v2_mapping_apply_runs")
