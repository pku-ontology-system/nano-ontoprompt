"""Persist append-only Mapping Apply worker attempt events."""
from alembic import op
import sqlalchemy as sa


revision = "0085_mapping_apply_attempt_events"
down_revision = "0084_mapping_definition_review"
branch_labels = None
depends_on = None


TABLE = "v2_mapping_apply_run_attempt_events"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("digest_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("celery_task_id", sa.String(length=200), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence_no >= 1", name="ck_mapping_apply_attempt_sequence_positive"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_mapping_apply_attempt_number_positive"),
        sa.CheckConstraint("digest_version >= 1", name="ck_mapping_apply_attempt_digest_version_positive"),
        sa.CheckConstraint(
            "event_type IN ('attempt_started','attempt_succeeded','attempt_failed','attempt_outcome_unknown')",
            name="ck_mapping_apply_attempt_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('running','succeeded','retry_wait','failed')",
            name="ck_mapping_apply_attempt_status",
        ),
        sa.CheckConstraint("actor_kind IN ('worker','system')", name="ck_mapping_apply_attempt_actor_kind"),
        sa.CheckConstraint("actor_id <> ''", name="ck_mapping_apply_attempt_actor_id"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["v2_mapping_apply_runs.id"],
            name="fk_mapping_apply_attempt_event_run", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence_no", name="uq_mapping_apply_attempt_event_sequence"),
    )
    op.create_index(
        "ix_mapping_apply_attempt_run_sequence", TABLE, ["run_id", "sequence_no"],
    )
    op.create_index(
        "uq_mapping_apply_attempt_start", TABLE, ["run_id", "attempt_number"],
        unique=True,
        sqlite_where=sa.text("event_type = 'attempt_started'"),
        postgresql_where=sa.text("event_type = 'attempt_started'"),
    )
    op.create_index(
        "uq_mapping_apply_attempt_terminal", TABLE, ["run_id", "attempt_number"],
        unique=True,
        sqlite_where=sa.text("event_type <> 'attempt_started'"),
        postgresql_where=sa.text("event_type <> 'attempt_started'"),
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_mapping_apply_attempt_event_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'Mapping Apply attempt events are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_apply_attempt_event_no_update_delete "
            f"BEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW "
            "EXECUTE FUNCTION reject_mapping_apply_attempt_event_mutation()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_apply_attempt_event_no_update "
            f"BEFORE UPDATE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'Mapping Apply attempt events are append-only'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_apply_attempt_event_no_delete "
            f"BEFORE DELETE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'Mapping Apply attempt events are append-only'); END"
        ))


def downgrade():
    has_events = op.get_bind().execute(sa.text(f"SELECT 1 FROM {TABLE} LIMIT 1")).first()
    if has_events:
        raise RuntimeError("Mapping Apply attempt history exists; downgrade would erase audit evidence")

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER trg_mapping_apply_attempt_event_no_update_delete ON {TABLE}"))
        op.execute(sa.text("DROP FUNCTION reject_mapping_apply_attempt_event_mutation()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_mapping_apply_attempt_event_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_mapping_apply_attempt_event_no_delete"))
    op.drop_index("uq_mapping_apply_attempt_terminal", table_name=TABLE)
    op.drop_index("uq_mapping_apply_attempt_start", table_name=TABLE)
    op.drop_index("ix_mapping_apply_attempt_run_sequence", table_name=TABLE)
    op.drop_table(TABLE)
