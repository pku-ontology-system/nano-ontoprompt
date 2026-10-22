"""Join the decision-lab/Case history with the schema migration journal."""

revision = "0038_merge_decision_cases"
down_revision = ("0037_decision_cases", "0037_schema_migration_execution_journal")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
