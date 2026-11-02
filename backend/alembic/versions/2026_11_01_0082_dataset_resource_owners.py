"""Record Dataset and multimodal import task creator scopes."""
from alembic import op
import sqlalchemy as sa


revision = "0082_dataset_resource_owners"
down_revision = "0081_construction_draft_resource_owner"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("v2_datasets") as batch:
        batch.add_column(sa.Column("created_by", sa.String(), nullable=True))
        batch.create_foreign_key(
            "fk_v2_datasets_created_by_users",
            "users",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_v2_datasets_created_by", ["created_by"], unique=False)

    with op.batch_alter_table("v2_multimodal_install_tasks") as batch:
        batch.add_column(sa.Column("created_by", sa.String(), nullable=True))
        batch.create_foreign_key(
            "fk_v2_multimodal_install_tasks_created_by_users",
            "users",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_v2_multimodal_install_tasks_created_by",
            ["created_by"],
            unique=False,
        )


def downgrade():
    bind = op.get_bind()
    for table in ("v2_datasets", "v2_multimodal_install_tasks"):
        if bind.execute(sa.text(
            f"SELECT 1 FROM {table} WHERE created_by IS NOT NULL LIMIT 1"
        )).first():
            raise RuntimeError(
                f"{table} creator records exist; downgrade would erase resource authorization"
            )

    with op.batch_alter_table("v2_multimodal_install_tasks") as batch:
        batch.drop_index("ix_v2_multimodal_install_tasks_created_by")
        batch.drop_constraint("fk_v2_multimodal_install_tasks_created_by_users", type_="foreignkey")
        batch.drop_column("created_by")

    with op.batch_alter_table("v2_datasets") as batch:
        batch.drop_index("ix_v2_datasets_created_by")
        batch.drop_constraint("fk_v2_datasets_created_by_users", type_="foreignkey")
        batch.drop_column("created_by")
