"""Persist curated review requester and decision actor identities."""
from alembic import op
import sqlalchemy as sa


revision = "0059_curated_review_actor_attribution"
down_revision = "0058_agent_run_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_curated_reviews",
        sa.Column(
            "requested_by",
            sa.String(),
            sa.ForeignKey("users.id", name="fk_curated_review_requested_by_user"),
            nullable=True,
        ),
    )
    op.add_column(
        "v2_curated_reviews",
        sa.Column(
            "decided_by",
            sa.String(),
            sa.ForeignKey("users.id", name="fk_curated_review_decided_by_user"),
            nullable=True,
        ),
    )


def downgrade():
    has_actor_history = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_curated_reviews "
        "WHERE requested_by IS NOT NULL OR decided_by IS NOT NULL LIMIT 1"
    )).first()
    if has_actor_history:
        raise RuntimeError(
            "Curated review actor attribution exists; downgrade would erase audit evidence"
        )
    op.drop_column("v2_curated_reviews", "decided_by")
    op.drop_column("v2_curated_reviews", "requested_by")
