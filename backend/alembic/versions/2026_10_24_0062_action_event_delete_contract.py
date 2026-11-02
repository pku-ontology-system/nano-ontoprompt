"""Normalize ActionRun event references to preserve append-only audit history."""
from alembic import op
import sqlalchemy as sa


revision = "0062_action_event_delete_contract"
down_revision = "0061_action_run_lifecycle_events"
branch_labels = None
depends_on = None


TABLE = "v2_ontology_action_run_events"
CONSTRAINT = "fk_action_run_event_run"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    foreign_keys = inspector.get_foreign_keys(TABLE)
    event_fk = next(
        (
            foreign_key
            for foreign_key in foreign_keys
            if foreign_key.get("name") == CONSTRAINT
            and foreign_key.get("constrained_columns") == ["action_run_id"]
        ),
        None,
    )

    ondelete = ((event_fk or {}).get("options") or {}).get("ondelete")
    if event_fk is not None and ondelete not in {"CASCADE", "cascade"}:
        return

    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "ActionRun event FK uses CASCADE; normalize it with a PostgreSQL migration "
            "that preserves the append-only triggers before continuing."
        )

    if event_fk is not None:
        op.drop_constraint(CONSTRAINT, TABLE, type_="foreignkey")
    op.create_foreign_key(
        CONSTRAINT,
        TABLE,
        "v2_ontology_action_runs",
        ["action_run_id"],
        ["id"],
    )


def downgrade():
    # Revision 0061 already defines the same NO ACTION FK contract. The
    # migration only corrects drift in databases that had CASCADE, so keeping
    # the normalized constraint is also the correct 0061 schema on downgrade.
    return None
