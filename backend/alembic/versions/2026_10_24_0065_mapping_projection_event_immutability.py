"""Protect Mapping row projection attempt history from mutation."""
from alembic import op
import sqlalchemy as sa


revision = "0065_mapping_projection_event_immutability"
down_revision = "0064_legacy_task_leases"
branch_labels = None
depends_on = None


TABLE = "v2_mapping_row_projection_events"


def upgrade():
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_mapping_row_projection_event_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'Mapping row projection events are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_row_projection_event_no_update_delete "
            f"BEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW "
            "EXECUTE FUNCTION reject_mapping_row_projection_event_mutation()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_row_projection_event_no_update "
            f"BEFORE UPDATE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'Mapping row projection events are append-only'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_row_projection_event_no_delete "
            f"BEFORE DELETE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'Mapping row projection events are append-only'); END"
        ))


def downgrade():
    has_events = op.get_bind().execute(sa.text(
        f"SELECT 1 FROM {TABLE} LIMIT 1"
    )).first()
    if has_events:
        raise RuntimeError(
            "Mapping row projection attempt history exists; downgrade would erase audit evidence"
        )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            f"DROP TRIGGER trg_mapping_row_projection_event_no_update_delete ON {TABLE}"
        ))
        op.execute(sa.text("DROP FUNCTION reject_mapping_row_projection_event_mutation()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_mapping_row_projection_event_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_mapping_row_projection_event_no_delete"))
