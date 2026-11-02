"""Persist the actor responsible for each curated review row edit."""
from alembic import op
import sqlalchemy as sa


revision = "0060_curated_row_edit_actor"
down_revision = "0059_curated_review_actor_attribution"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_curated_row_edits",
        sa.Column(
            "edited_by",
            sa.String(),
            sa.ForeignKey("users.id", name="fk_curated_row_edit_edited_by_user"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_v2_curated_row_edits_edited_by",
        "v2_curated_row_edits",
        ["edited_by"],
    )


def downgrade():
    has_actor_history = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_curated_row_edits WHERE edited_by IS NOT NULL LIMIT 1"
    )).first()
    if has_actor_history:
        raise RuntimeError(
            "Curated row edit actor attribution exists; downgrade would erase audit evidence"
        )
    op.drop_index("ix_v2_curated_row_edits_edited_by", table_name="v2_curated_row_edits")
    op.drop_column("v2_curated_row_edits", "edited_by")
