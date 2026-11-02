"""Add immutable, digest-chained Pipeline LLM invocation lifecycle events."""
from alembic import op
import sqlalchemy as sa


revision = "0079_pipeline_llm_invocation_events"
down_revision = "0078_pipeline_llm_attempt_provenance"
branch_labels = None
depends_on = None


TABLE = "v2_pipeline_llm_invocation_events"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("invocation_id", sa.String(), nullable=False),
        sa.Column("pipeline_run_id", sa.String(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("phase", sa.String(length=40), nullable=True),
        sa.Column("source_dataset_version_id", sa.String(), nullable=True),
        sa.Column("model_config_id", sa.String(length=100), nullable=True),
        sa.Column("model_config_digest", sa.String(length=64), nullable=True),
        sa.Column("prompt_id", sa.String(length=100), nullable=True),
        sa.Column("prompt_digest", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("response_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1",
            name="ck_pipeline_llm_invocation_event_sequence_positive",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_pipeline_llm_invocation_attempt_positive",
        ),
        sa.CheckConstraint(
            "event_type IN ('started', 'provider_result', 'response_validation_failed')",
            name="ck_pipeline_llm_invocation_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'empty_response', 'invalid_response')",
            name="ck_pipeline_llm_invocation_event_status",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["v2_model_invocations.id"],
            name="fk_pipeline_llm_invocation_event_invocation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invocation_id", "sequence",
            name="uq_pipeline_llm_invocation_event_sequence",
        ),
    )
    op.create_index(
        "ix_pipeline_llm_events_run_sequence",
        TABLE,
        ["pipeline_run_id", "attempt_number", "sequence"],
    )
    op.create_index(
        "uq_pipeline_run_llm_invocation_attempt_number",
        "v2_model_invocations",
        ["pipeline_run_id", "attempt_number"],
        unique=True,
        postgresql_where=sa.text("pipeline_run_id IS NOT NULL AND attempt_number IS NOT NULL"),
        sqlite_where=sa.text("pipeline_run_id IS NOT NULL AND attempt_number IS NOT NULL"),
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_pipeline_llm_invocation_event_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'Pipeline LLM invocation events are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_llm_invocation_event_no_update_delete "
            f"BEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW "
            "EXECUTE FUNCTION reject_pipeline_llm_invocation_event_mutation()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_llm_invocation_event_no_update "
            f"BEFORE UPDATE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'Pipeline LLM invocation events are append-only'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_llm_invocation_event_no_delete "
            f"BEFORE DELETE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'Pipeline LLM invocation events are append-only'); END"
        ))


def downgrade():
    if op.get_bind().execute(sa.text(f"SELECT 1 FROM {TABLE} LIMIT 1")).first():
        raise RuntimeError(
            "Pipeline LLM invocation lifecycle history exists; downgrade would erase evidence"
        )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            f"DROP TRIGGER trg_pipeline_llm_invocation_event_no_update_delete ON {TABLE}"
        ))
        op.execute(sa.text("DROP FUNCTION reject_pipeline_llm_invocation_event_mutation()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_pipeline_llm_invocation_event_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_pipeline_llm_invocation_event_no_delete"))

    op.drop_index("ix_pipeline_llm_events_run_sequence", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_index(
        "uq_pipeline_run_llm_invocation_attempt_number",
        table_name="v2_model_invocations",
    )
