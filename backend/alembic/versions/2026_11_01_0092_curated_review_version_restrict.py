"""Keep Curated review DatasetVersion provenance when versions are deleted."""
from alembic import op
import sqlalchemy as sa


revision = "0092_curated_review_version_restrict"
down_revision = "0091_scenario_graph_build_recovery"
branch_labels = None
depends_on = None


TABLE = "v2_curated_reviews"
CONSTRAINT = "fk_curated_review_dataset_version"
VERSION_TABLE = "v2_dataset_versions"


def _replace_dataset_version_fk(ondelete: str) -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(CONSTRAINT, type_="foreignkey")
        batch.create_foreign_key(
            CONSTRAINT,
            VERSION_TABLE,
            ["dataset_version_id"],
            ["id"],
            ondelete=ondelete,
        )


def upgrade():
    _replace_dataset_version_fk("RESTRICT")


def downgrade():
    has_review_pins = op.get_bind().execute(sa.text(
        f"SELECT 1 FROM {TABLE} WHERE dataset_version_id IS NOT NULL LIMIT 1"
    )).first()
    if has_review_pins:
        raise RuntimeError(
            "Curated review DatasetVersion pins exist; downgrade would restore SET NULL deletion semantics"
        )
    _replace_dataset_version_fk("SET NULL")
