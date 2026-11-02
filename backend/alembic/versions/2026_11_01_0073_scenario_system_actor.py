"""Record Scenario executor separately from the policy subject."""
from alembic import op
import sqlalchemy as sa


revision = "0073_scenario_system_actor"
down_revision = "0072_scenario_rebase_resolution"
branch_labels = None
depends_on = None


ACTOR_TABLES = (
    ("v2_scenario_changesets", "ck_v2_scenario_changeset_actor_kind"),
    ("v2_scenario_audits", "ck_v2_scenario_audit_actor_kind"),
)


def upgrade():
    for table, constraint in ACTOR_TABLES:
        op.add_column(table, sa.Column(
            "actor_kind", sa.String(length=16), nullable=False,
            server_default=sa.text("'user'"),
        ))
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "actor_id", existing_type=sa.String(), nullable=True,
            )
            batch.create_check_constraint(
                constraint, "actor_kind IN ('user','system')",
            )


def downgrade():
    bind = op.get_bind()
    for table, constraint in ACTOR_TABLES:
        if bind.execute(sa.text(
            f"SELECT 1 FROM {table} WHERE actor_id IS NULL LIMIT 1"
        )).first():
            raise RuntimeError(
                f"{table} contains system actor records; downgrade would erase attribution"
            )
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(constraint, type_="check")
            batch.alter_column(
                "actor_id", existing_type=sa.String(), nullable=False,
            )
            batch.drop_column("actor_kind")
