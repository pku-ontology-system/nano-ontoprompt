"""Track operator projection retries separately from automatic attempts."""
from alembic import op
import sqlalchemy as sa


revision = "0052_action_manual_projection_budget"
down_revision = "0051_action_run_actor_attribution"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_ontology_action_runs",
        sa.Column(
            "projection_manual_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )


def downgrade():
    manual_attempts = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_ontology_action_runs "
        "WHERE projection_manual_attempts > 0 LIMIT 1"
    )).first()
    if manual_attempts:
        raise RuntimeError(
            "Manual Action projection attempts exist; downgrade would erase retry accounting"
        )
    op.drop_column("v2_ontology_action_runs", "projection_manual_attempts")
