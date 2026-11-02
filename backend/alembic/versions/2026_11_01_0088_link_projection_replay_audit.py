"""Add fenced, auditable automatic and manual Link relation projection attempts.

Revision ID: 0088_link_projection_replay_audit
Revises: 0087_mapping_link_apply_runs
"""
from alembic import op
import sqlalchemy as sa


revision = "0088_link_projection_replay_audit"
down_revision = "0087_mapping_link_apply_runs"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("v2_mapping_link_relation_projection_states") as batch:
        batch.add_column(sa.Column("manual_attempts", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("current_attempt_id", sa.String(length=36), nullable=True))
        batch.create_check_constraint(
            "ck_mapping_link_relation_projection_manual_attempts", "manual_attempts >= 0",
        )

    op.create_table(
        "v2_mapping_link_relation_projection_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=12), nullable=False),
        sa.Column("change_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("link_mapping_id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("relation_id", sa.String(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("attempt_kind", sa.String(length=12), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("client_request_id", sa.String(length=120), nullable=True),
        sa.Column("request_digest", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence_no >= 1", name="ck_mapping_link_projection_event_sequence"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_mapping_link_projection_event_attempt"),
        sa.CheckConstraint("event_type IN ('started','finished')", name="ck_mapping_link_projection_event_type"),
        sa.CheckConstraint("attempt_kind IN ('automatic','manual')", name="ck_mapping_link_projection_attempt_kind"),
        sa.CheckConstraint("actor_kind IN ('system','worker','admin')", name="ck_mapping_link_projection_actor_kind"),
        sa.CheckConstraint(
            "(event_type = 'started' AND outcome IS NULL) OR "
            "(event_type = 'finished' AND outcome IS NOT NULL)",
            name="ck_mapping_link_projection_event_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["change_id"], ["v2_mapping_link_relation_changes.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["v2_mapping_link_apply_runs.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["link_mapping_id"], ["v2_ontology_link_mappings.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "event_type", name="uq_mapping_link_projection_attempt_event"),
        sa.UniqueConstraint("change_id", "sequence_no", name="uq_mapping_link_projection_change_sequence"),
    )
    op.create_index(
        "ix_mapping_link_projection_event_attempt_id",
        "v2_mapping_link_relation_projection_events", ["attempt_id"],
    )
    op.create_index(
        "ix_mapping_link_projection_event_run_sequence",
        "v2_mapping_link_relation_projection_events", ["run_id", "sequence_no"],
    )
    op.create_index(
        "ix_mapping_link_projection_event_change_sequence",
        "v2_mapping_link_relation_projection_events", ["change_id", "sequence_no"],
    )

    op.create_table(
        "v2_mapping_link_projection_replay_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("link_mapping_id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(length=120), nullable=False),
        sa.Column("actor_role", sa.String(length=24), nullable=False),
        sa.Column("client_request_id", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("authorization_scope_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="running", nullable=False),
        sa.Column("selected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("projected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pending_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','completed','partial','failed')",
            name="ck_mapping_link_projection_replay_status",
        ),
        sa.CheckConstraint("selected_count >= 0", name="ck_mapping_link_projection_replay_selected"),
        sa.CheckConstraint("projected_count >= 0", name="ck_mapping_link_projection_replay_projected"),
        sa.CheckConstraint("failed_count >= 0", name="ck_mapping_link_projection_replay_failed"),
        sa.CheckConstraint("pending_count >= 0", name="ck_mapping_link_projection_replay_pending"),
        sa.ForeignKeyConstraint(["run_id"], ["v2_mapping_link_apply_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["link_mapping_id"], ["v2_ontology_link_mappings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "actor_id", "client_request_id",
            name="uq_mapping_link_projection_replay_request",
        ),
    )
    op.create_index(
        "ix_mapping_link_projection_replay_run_created",
        "v2_mapping_link_projection_replay_requests", ["run_id", "created_at"],
    )
    op.create_index(
        "ix_mapping_link_projection_replay_lease",
        "v2_mapping_link_projection_replay_requests", ["status", "lease_until"],
    )

    dialect = op.get_bind().dialect.name
    immutable_replay_fields = (
        "run_id,link_mapping_id,ontology_id,actor_id,actor_role,client_request_id,"
        "reason,request_digest,authorization_scope_json,created_at"
    )
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_mapping_link_projection_evidence_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'Link relation projection evidence is append-only'; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            "CREATE FUNCTION protect_mapping_link_projection_replay_request() RETURNS trigger AS $$ "
            "BEGIN IF ROW(NEW.run_id,NEW.link_mapping_id,NEW.ontology_id,NEW.actor_id,NEW.actor_role,"
            "NEW.client_request_id,NEW.reason,NEW.request_digest,NEW.authorization_scope_json,NEW.created_at) "
            "IS DISTINCT FROM ROW(OLD.run_id,OLD.link_mapping_id,OLD.ontology_id,OLD.actor_id,OLD.actor_role,"
            "OLD.client_request_id,OLD.reason,OLD.request_digest,OLD.authorization_scope_json,OLD.created_at) "
            "THEN RAISE EXCEPTION 'Link projection replay request identity is immutable'; END IF; "
            "IF OLD.status IN ('completed','partial','failed') AND ROW(NEW.status,NEW.selected_count,"
            "NEW.projected_count,NEW.failed_count,NEW.pending_count,NEW.completed_at) IS DISTINCT FROM "
            "ROW(OLD.status,OLD.selected_count,OLD.projected_count,OLD.failed_count,OLD.pending_count,OLD.completed_at) "
            "THEN RAISE EXCEPTION 'Link projection replay terminal state is immutable'; END IF; RETURN NEW; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            "CREATE TRIGGER trg_mapping_link_projection_event_no_mutation "
            "BEFORE UPDATE OR DELETE ON v2_mapping_link_relation_projection_events "
            "FOR EACH ROW EXECUTE FUNCTION reject_mapping_link_projection_evidence_mutation()"
        ))
        op.execute(sa.text(
            "CREATE TRIGGER trg_mapping_link_projection_replay_request_basis "
            "BEFORE UPDATE ON v2_mapping_link_projection_replay_requests "
            "FOR EACH ROW EXECUTE FUNCTION protect_mapping_link_projection_replay_request()"
        ))
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            suffix = operation.lower()
            op.execute(sa.text(
                f"CREATE TRIGGER trg_mapping_link_projection_event_no_{suffix} "
                f"BEFORE {operation} ON v2_mapping_link_relation_projection_events "
                "BEGIN SELECT RAISE(ABORT, 'Link relation projection evidence is append-only'); END"
            ))
        changed = " OR ".join(
            f"OLD.{name} IS NOT NEW.{name}" for name in immutable_replay_fields.split(",")
        )
        terminal_changed = " OR ".join(
            f"OLD.{name} IS NOT NEW.{name}"
            for name in ("status", "selected_count", "projected_count", "failed_count", "pending_count", "completed_at")
        )
        op.execute(sa.text(
            "CREATE TRIGGER trg_mapping_link_projection_replay_request_basis "
            "BEFORE UPDATE ON v2_mapping_link_projection_replay_requests "
            f"WHEN {changed} OR (OLD.status IN ('completed','partial','failed') AND ({terminal_changed})) "
            "BEGIN SELECT RAISE(ABORT, 'Link projection replay request identity is immutable'); END"
        ))


def downgrade():
    bind = op.get_bind()
    for table in (
        "v2_mapping_link_relation_projection_events",
        "v2_mapping_link_projection_replay_requests",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"{table} contains recovery evidence; downgrade would delete audit history")
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_mapping_link_relation_projection_states "
        "WHERE manual_attempts > 0 OR current_attempt_id IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Link relation projection state contains replay data; downgrade is unsafe")

    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "DROP TRIGGER trg_mapping_link_projection_replay_request_basis "
            "ON v2_mapping_link_projection_replay_requests"
        ))
        op.execute(sa.text(
            "DROP TRIGGER trg_mapping_link_projection_event_no_mutation "
            "ON v2_mapping_link_relation_projection_events"
        ))
        op.execute(sa.text("DROP FUNCTION protect_mapping_link_projection_replay_request()"))
        op.execute(sa.text("DROP FUNCTION reject_mapping_link_projection_evidence_mutation()"))
    elif dialect == "sqlite":
        for name in (
            "trg_mapping_link_projection_replay_request_basis",
            "trg_mapping_link_projection_event_no_update",
            "trg_mapping_link_projection_event_no_delete",
        ):
            op.execute(sa.text(f"DROP TRIGGER {name}"))

    op.drop_index(
        "ix_mapping_link_projection_replay_lease",
        table_name="v2_mapping_link_projection_replay_requests",
    )
    op.drop_index(
        "ix_mapping_link_projection_replay_run_created",
        table_name="v2_mapping_link_projection_replay_requests",
    )
    op.drop_table("v2_mapping_link_projection_replay_requests")
    op.drop_index(
        "ix_mapping_link_projection_event_change_sequence",
        table_name="v2_mapping_link_relation_projection_events",
    )
    op.drop_index(
        "ix_mapping_link_projection_event_run_sequence",
        table_name="v2_mapping_link_relation_projection_events",
    )
    op.drop_index(
        "ix_mapping_link_projection_event_attempt_id",
        table_name="v2_mapping_link_relation_projection_events",
    )
    op.drop_table("v2_mapping_link_relation_projection_events")
    with op.batch_alter_table("v2_mapping_link_relation_projection_states") as batch:
        batch.drop_constraint("ck_mapping_link_relation_projection_manual_attempts", type_="check")
        batch.drop_column("current_attempt_id")
        batch.drop_column("manual_attempts")
