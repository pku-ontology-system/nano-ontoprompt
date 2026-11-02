"""Enforce at most one ActionRun per reviewed DecisionCase."""
from alembic import op


revision = "0041_action_run_case_idempotency"
down_revision = "0040_agent_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_action_run_decision_case",
        "v2_ontology_action_runs",
        ["decision_case_id"],
    )


def downgrade():
    op.drop_constraint(
        "uq_action_run_decision_case",
        "v2_ontology_action_runs",
        type_="unique",
    )
