"""Record ConstructionDraft ownership for resource-scoped access checks."""
from alembic import op
import sqlalchemy as sa


revision = "0081_construction_draft_resource_owner"
down_revision = "0080_construction_mapping_attempt_events"
branch_labels = None
depends_on = None


TABLE = "v2_construction_drafts"
COLUMN = "created_by"
FOREIGN_KEY = "fk_v2_construction_drafts_created_by_users"
INDEX = "ix_v2_construction_drafts_created_by"


def upgrade():
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.String(), nullable=True))
        batch.create_foreign_key(
            FOREIGN_KEY,
            "users",
            [COLUMN],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(INDEX, [COLUMN], unique=False)


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        f"SELECT 1 FROM {TABLE} WHERE {COLUMN} IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError(
            "ConstructionDraft ownership exists; downgrade would erase resource authorization"
        )

    with op.batch_alter_table(TABLE) as batch:
        batch.drop_index(INDEX)
        batch.drop_constraint(FOREIGN_KEY, type_="foreignkey")
        batch.drop_column(COLUMN)
