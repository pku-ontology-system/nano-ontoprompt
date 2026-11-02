"""Pin curated reviews to the immutable dataset version they inspected."""
from alembic import op
import sqlalchemy as sa


revision = "0043_review_dataset_version"
down_revision = "0042_action_projection_retries"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_curated_reviews",
        sa.Column("dataset_version_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_curated_review_dataset_version",
        "v2_curated_reviews",
        "v2_dataset_versions",
        ["dataset_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_v2_curated_reviews_dataset_version_id",
        "v2_curated_reviews",
        ["dataset_version_id"],
    )


def downgrade():
    has_review_pins = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_curated_reviews WHERE dataset_version_id IS NOT NULL LIMIT 1"
    )).first()
    if has_review_pins:
        raise RuntimeError(
            "Curated reviews reference DatasetVersion rows; downgrade would erase review provenance"
        )
    op.drop_index(
        "ix_v2_curated_reviews_dataset_version_id",
        table_name="v2_curated_reviews",
    )
    op.drop_constraint(
        "fk_curated_review_dataset_version",
        "v2_curated_reviews",
        type_="foreignkey",
    )
    op.drop_column("v2_curated_reviews", "dataset_version_id")
