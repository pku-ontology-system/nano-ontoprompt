"""Add append-only audit records for temporal Object instance reconciliation."""
from alembic import op
import sqlalchemy as sa


revision = "0066_temporal_instance_reconciliation"
down_revision = "0065_mapping_projection_event_immutability"
branch_labels = None
depends_on = None


TABLE = "v2_temporal_instance_reconciliations"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("replay_id", sa.String(), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["replay_id"], ["v2_temporal_replays.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "replay_id", "idempotency_key",
            name="uq_v2_temporal_instance_reconciliation_request_key",
        ),
    )
    op.create_index(
        "ix_v2_temporal_instance_reconciliations_ontology_created",
        TABLE,
        ["ontology_id", "created_at"],
        unique=False,
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_temporal_instance_reconciliation_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'Temporal instance reconciliation audit records are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_temporal_instance_reconciliation_no_update_delete "
            f"BEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW "
            "EXECUTE FUNCTION reject_temporal_instance_reconciliation_mutation()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_temporal_instance_reconciliation_no_update "
            f"BEFORE UPDATE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'Temporal instance reconciliation audit records are append-only'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_temporal_instance_reconciliation_no_delete "
            f"BEFORE DELETE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'Temporal instance reconciliation audit records are append-only'); END"
        ))


def downgrade():
    has_records = op.get_bind().execute(sa.text(f"SELECT 1 FROM {TABLE} LIMIT 1")).first()
    if has_records:
        raise RuntimeError(
            "Temporal instance reconciliation audit records exist; downgrade would erase audit evidence"
        )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            f"DROP TRIGGER trg_temporal_instance_reconciliation_no_update_delete ON {TABLE}"
        ))
        op.execute(sa.text("DROP FUNCTION reject_temporal_instance_reconciliation_mutation()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_temporal_instance_reconciliation_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_temporal_instance_reconciliation_no_delete"))
    op.drop_index("ix_v2_temporal_instance_reconciliations_ontology_created", table_name=TABLE)
    op.drop_table(TABLE)
