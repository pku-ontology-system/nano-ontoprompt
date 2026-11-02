"""Add an immutable event ledger for Construction MappingTask attempts."""
from alembic import op
import sqlalchemy as sa


revision = "0080_construction_mapping_attempt_events"
down_revision = "0079_pipeline_llm_invocation_events"
branch_labels = None
depends_on = None


TABLE = "v2_construction_mapping_attempt_events"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("mapping_task_id", sa.String(), nullable=False),
        sa.Column("invocation_id", sa.String(), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("phase", sa.String(length=40), nullable=True),
        sa.Column("source_dataset_version_id", sa.String(), nullable=True),
        sa.Column("model_config_id", sa.String(length=100), nullable=True),
        sa.Column("model_config_digest", sa.String(length=64), nullable=True),
        sa.Column("prompt_digest", sa.String(length=64), nullable=True),
        sa.Column("parser_version", sa.String(length=100), nullable=True),
        sa.Column("normalizer_version", sa.String(length=100), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("response_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_number >= 1", name="ck_construction_mapping_event_attempt_positive"),
        sa.CheckConstraint("sequence >= 1", name="ck_construction_mapping_event_sequence_positive"),
        sa.CheckConstraint(
            "event_type IN ('attempt_started', 'invocation_started', 'provider_result', "
            "'response_validation_failed', 'invocation_finalized', 'attempt_result')",
            name="ck_construction_mapping_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled', 'invalid_response', 'waiting_for_model')",
            name="ck_construction_mapping_event_status",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_task_id"], ["v2_mapping_tasks.id"],
            name="fk_construction_mapping_event_task", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"], ["v2_model_invocations.id"],
            name="fk_construction_mapping_event_invocation", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mapping_task_id", "sequence", name="uq_construction_mapping_event_sequence"),
    )
    op.create_index(
        "ix_construction_mapping_events_task_sequence",
        TABLE,
        ["mapping_task_id", "sequence"],
    )
    op.create_index(
        "uq_construction_mapping_attempt_start",
        TABLE,
        ["mapping_task_id", "attempt_number"],
        unique=True,
        postgresql_where=sa.text("event_type = 'attempt_started'"),
        sqlite_where=sa.text("event_type = 'attempt_started'"),
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_construction_mapping_event_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'Construction MappingTask attempt events are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_construction_mapping_event_no_update_delete "
            f"BEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW "
            "EXECUTE FUNCTION reject_construction_mapping_event_mutation()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_construction_mapping_event_no_update BEFORE UPDATE ON {TABLE} "
            "BEGIN SELECT RAISE(ABORT, 'Construction MappingTask attempt events are append-only'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_construction_mapping_event_no_delete BEFORE DELETE ON {TABLE} "
            "BEGIN SELECT RAISE(ABORT, 'Construction MappingTask attempt events are append-only'); END"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {TABLE} LIMIT 1")).first():
        raise RuntimeError("Construction MappingTask attempt events exist; downgrade would erase lineage")

    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER trg_construction_mapping_event_no_update_delete ON {TABLE}"))
        op.execute(sa.text("DROP FUNCTION reject_construction_mapping_event_mutation()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_construction_mapping_event_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_construction_mapping_event_no_delete"))
    op.drop_index("uq_construction_mapping_attempt_start", table_name=TABLE)
    op.drop_index("ix_construction_mapping_events_task_sequence", table_name=TABLE)
    op.drop_table(TABLE)
