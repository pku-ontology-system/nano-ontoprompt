"""Pin Construction drafts and MappingTask inputs to DatasetVersions."""
from alembic import op
import sqlalchemy as sa


revision = "0055_construction_dataset_pins"
down_revision = "0054_mapping_row_projection_budget"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_construction_drafts",
        sa.Column("dataset_version_id", sa.String(), nullable=True),
    )
    op.add_column(
        "v2_mapping_tasks",
        sa.Column("source_version_id", sa.String(), nullable=True),
    )
    op.add_column(
        "v2_mapping_tasks",
        sa.Column("input_manifest_json", sa.JSON(), nullable=True),
    )


def downgrade():
    bind = op.get_bind()
    pinned_drafts = bind.execute(sa.text(
        "SELECT 1 FROM v2_construction_drafts WHERE dataset_version_id IS NOT NULL LIMIT 1"
    )).first()
    pinned_tasks = bind.execute(sa.text(
        "SELECT 1 FROM v2_mapping_tasks "
        "WHERE source_version_id IS NOT NULL OR input_manifest_json IS NOT NULL LIMIT 1"
    )).first()
    if pinned_drafts or pinned_tasks:
        raise RuntimeError(
            "Construction source pin evidence exists; downgrade would discard input lineage"
        )
    op.drop_column("v2_mapping_tasks", "input_manifest_json")
    op.drop_column("v2_mapping_tasks", "source_version_id")
    op.drop_column("v2_construction_drafts", "dataset_version_id")
