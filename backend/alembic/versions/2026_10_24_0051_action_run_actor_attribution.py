"""Persist requester and reviewer identities for ActionRun audit."""
from alembic import op
import sqlalchemy as sa


revision = "0051_action_run_actor_attribution"
down_revision = "0050_scenario_action_run_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "v2_ontology_action_runs",
        sa.Column(
            "requested_by",
            sa.String(),
            sa.ForeignKey("users.id", name="fk_action_run_requested_by_user"),
            nullable=True,
        ),
    )
    op.add_column(
        "v2_ontology_action_runs",
        sa.Column(
            "reviewed_by",
            sa.String(),
            sa.ForeignKey("users.id", name="fk_action_run_reviewed_by_user"),
            nullable=True,
        ),
    )


def downgrade():
    attributed = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_ontology_action_runs "
        "WHERE requested_by IS NOT NULL OR reviewed_by IS NOT NULL LIMIT 1"
    )).first()
    if attributed:
        raise RuntimeError(
            "ActionRun requester or reviewer attribution exists; downgrade would erase audit evidence"
        )
    op.drop_column("v2_ontology_action_runs", "reviewed_by")
    op.drop_column("v2_ontology_action_runs", "requested_by")
