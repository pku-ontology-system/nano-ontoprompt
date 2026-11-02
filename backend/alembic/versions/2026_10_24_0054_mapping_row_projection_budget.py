"""Bound Mapping row projection retries and retain attempt events."""
from alembic import op
import sqlalchemy as sa


revision = "0054_mapping_row_projection_budget"
down_revision = "0053_scenario_merge_reconciliation_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("v2_mapping_row_changes") as batch:
        batch.drop_constraint("ck_mapping_row_change_projection_status", type_="check")
        batch.add_column(sa.Column(
            "projection_manual_attempts", sa.Integer(), server_default="0", nullable=False,
        ))
        batch.add_column(sa.Column("projection_lease_until", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("projection_current_attempt_id", sa.String(length=36), nullable=True))
        batch.create_check_constraint(
            "ck_mapping_row_change_projection_status",
            "projection_status IN ('pending', 'projecting', 'projected', 'dead_letter')",
        )

    op.create_table(
        "v2_mapping_row_projection_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=12), nullable=False),
        sa.Column("row_change_id", sa.String(length=36), nullable=False),
        sa.Column("mapping_apply_run_id", sa.String(length=36), nullable=False),
        sa.Column("mapping_id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attempt_kind", sa.String(length=12), nullable=False),
        sa.Column("actor_kind", sa.String(length=24), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("client_request_id", sa.String(length=120), nullable=True),
        sa.Column("request_digest", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("event_type IN ('started', 'finished')", name="ck_mapping_row_projection_event_type"),
        sa.CheckConstraint("attempt_kind IN ('automatic', 'manual')", name="ck_mapping_row_projection_attempt_kind"),
        sa.ForeignKeyConstraint(["row_change_id"], ["v2_mapping_row_changes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mapping_apply_run_id"], ["v2_mapping_apply_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mapping_id"], ["v2_ontology_mappings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "event_type", name="uq_mapping_row_projection_attempt_event"),
    )
    op.create_index(
        "ix_v2_mapping_row_projection_events_attempt_id",
        "v2_mapping_row_projection_events", ["attempt_id"],
    )
    op.create_index(
        "ix_mapping_row_projection_event_run_created",
        "v2_mapping_row_projection_events", ["mapping_apply_run_id", "created_at"],
    )
    op.create_index(
        "uq_mapping_row_projection_manual_request",
        "v2_mapping_row_projection_events",
        ["row_change_id", "actor_id", "client_request_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'started' AND client_request_id IS NOT NULL"),
        sqlite_where=sa.text("event_type = 'started' AND client_request_id IS NOT NULL"),
    )


def downgrade():
    bind = op.get_bind()
    has_events = bind.execute(sa.text(
        "SELECT 1 FROM v2_mapping_row_projection_events LIMIT 1"
    )).first()
    has_terminal_work = bind.execute(sa.text(
        "SELECT 1 FROM v2_mapping_row_changes "
        "WHERE projection_status IN ('projecting', 'dead_letter') "
        "OR projection_manual_attempts > 0 "
        "OR projection_lease_until IS NOT NULL "
        "OR projection_current_attempt_id IS NOT NULL LIMIT 1"
    )).first()
    if has_events or has_terminal_work:
        raise RuntimeError(
            "Mapping projection attempt evidence exists; downgrade would discard recovery history"
        )
    op.drop_index(
        "uq_mapping_row_projection_manual_request",
        table_name="v2_mapping_row_projection_events",
    )
    op.drop_index(
        "ix_mapping_row_projection_event_run_created",
        table_name="v2_mapping_row_projection_events",
    )
    op.drop_index(
        "ix_v2_mapping_row_projection_events_attempt_id",
        table_name="v2_mapping_row_projection_events",
    )
    op.drop_table("v2_mapping_row_projection_events")
    with op.batch_alter_table("v2_mapping_row_changes") as batch:
        batch.drop_constraint("ck_mapping_row_change_projection_status", type_="check")
        batch.drop_column("projection_current_attempt_id")
        batch.drop_column("projection_lease_until")
        batch.drop_column("projection_manual_attempts")
        batch.create_check_constraint(
            "ck_mapping_row_change_projection_status",
            "projection_status IN ('pending', 'projected')",
        )
