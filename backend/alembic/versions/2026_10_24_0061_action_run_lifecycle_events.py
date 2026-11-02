"""Persist append-only, digest-chained ActionRun lifecycle events."""
from alembic import op
import sqlalchemy as sa


revision = "0061_action_run_lifecycle_events"
down_revision = "0060_curated_row_edit_actor"
branch_labels = None
depends_on = None


TABLE = "v2_ontology_action_run_events"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("action_run_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(length=200), nullable=True),
        sa.Column("trace_id", sa.String(length=36), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("preview_digest", sa.String(length=64), nullable=True),
        sa.Column("permission_digest", sa.String(length=64), nullable=True),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_action_run_event_sequence_positive"),
        sa.CheckConstraint(
            "event_type IN ('created', 'status_changed')",
            name="ck_action_run_event_type",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'agent', 'worker', 'system')",
            name="ck_action_run_event_actor_kind",
        ),
        sa.ForeignKeyConstraint(
            ["action_run_id"],
            ["v2_ontology_action_runs.id"],
            name="fk_action_run_event_run",
        ),
        sa.UniqueConstraint(
            "action_run_id", "sequence", name="uq_action_run_event_sequence",
        ),
    )
    op.create_index(
        "ix_action_run_events_run_sequence",
        TABLE,
        ["action_run_id", "sequence"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_action_run_event_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'ActionRun lifecycle events are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_action_run_event_no_update_delete "
            f"BEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW "
            "EXECUTE FUNCTION reject_action_run_event_mutation()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_action_run_event_no_update "
            f"BEFORE UPDATE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'ActionRun lifecycle events are append-only'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_action_run_event_no_delete "
            f"BEFORE DELETE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'ActionRun lifecycle events are append-only'); END"
        ))


def downgrade():
    has_events = op.get_bind().execute(sa.text(
        f"SELECT 1 FROM {TABLE} LIMIT 1"
    )).first()
    if has_events:
        raise RuntimeError(
            "ActionRun lifecycle history exists; downgrade would erase audit evidence"
        )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            f"DROP TRIGGER trg_action_run_event_no_update_delete ON {TABLE}"
        ))
        op.execute(sa.text("DROP FUNCTION reject_action_run_event_mutation()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_action_run_event_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_action_run_event_no_delete"))
    op.drop_index("ix_action_run_events_run_sequence", table_name=TABLE)
    op.drop_table(TABLE)
