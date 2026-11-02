"""Pin Pipeline definitions and DatasetVersion inputs for each Run."""
from alembic import op
import sqlalchemy as sa


revision = "0047_pipeline_execution_manifests"
down_revision = "0046_mapping_snapshot_reconciliation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v2_pipeline_versions", sa.Column("execution_snapshot", sa.JSON(), nullable=True))
    op.add_column("v2_pipeline_versions", sa.Column("definition_digest", sa.String(length=64), nullable=True))
    op.add_column("v2_pipeline_versions", sa.Column("snapshot_digest", sa.String(length=64), nullable=True))

    op.add_column("v2_pipeline_runs", sa.Column("pipeline_version_id", sa.String(), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("execution_mode", sa.String(length=40), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("execution_manifest", sa.JSON(), nullable=True))
    op.add_column("v2_pipeline_runs", sa.Column("execution_digest", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        "fk_v2_pipeline_runs_pipeline_version",
        "v2_pipeline_runs",
        "v2_pipeline_versions",
        ["pipeline_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_v2_pipeline_runs_pipeline_version_id", "v2_pipeline_runs", ["pipeline_version_id"])
    op.create_index("ix_v2_pipeline_runs_execution_digest", "v2_pipeline_runs", ["execution_digest"])


def downgrade():
    bind = op.get_bind()
    has_run_lineage = bind.execute(sa.text(
        "SELECT 1 FROM v2_pipeline_runs WHERE pipeline_version_id IS NOT NULL "
        "OR execution_mode IS NOT NULL OR execution_manifest IS NOT NULL "
        "OR execution_digest IS NOT NULL LIMIT 1"
    )).first()
    has_version_lineage = bind.execute(sa.text(
        "SELECT 1 FROM v2_pipeline_versions WHERE execution_snapshot IS NOT NULL "
        "OR definition_digest IS NOT NULL OR snapshot_digest IS NOT NULL LIMIT 1"
    )).first()
    if has_run_lineage or has_version_lineage:
        raise RuntimeError("Pipeline execution lineage exists; downgrade would discard immutable run inputs")

    op.drop_index("ix_v2_pipeline_runs_execution_digest", table_name="v2_pipeline_runs")
    op.drop_index("ix_v2_pipeline_runs_pipeline_version_id", table_name="v2_pipeline_runs")
    op.drop_constraint("fk_v2_pipeline_runs_pipeline_version", "v2_pipeline_runs", type_="foreignkey")
    op.drop_column("v2_pipeline_runs", "execution_digest")
    op.drop_column("v2_pipeline_runs", "execution_manifest")
    op.drop_column("v2_pipeline_runs", "execution_mode")
    op.drop_column("v2_pipeline_runs", "pipeline_version_id")
    op.drop_column("v2_pipeline_versions", "snapshot_digest")
    op.drop_column("v2_pipeline_versions", "definition_digest")
    op.drop_column("v2_pipeline_versions", "execution_snapshot")
