"""Dispatch manual Link relation projection replay as durable background work.

Revision ID: 0089_link_projection_replay_dispatch
Revises: 0088_link_projection_replay_audit
"""
from alembic import op
import sqlalchemy as sa


revision = "0089_link_projection_replay_dispatch"
down_revision = "0088_link_projection_replay_audit"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("v2_mapping_link_projection_replay_requests") as batch:
        batch.drop_constraint("ck_mapping_link_projection_replay_status", type_="check")
        batch.add_column(sa.Column("batch_size", sa.Integer(), server_default="100", nullable=False))
        batch.add_column(sa.Column("dispatch_attempts", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("celery_task_id", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column("error_code", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("dispatch_lease_token", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True))
        batch.alter_column("status", server_default="pending", existing_type=sa.String(length=16), existing_nullable=False)
        batch.create_check_constraint(
            "ck_mapping_link_projection_replay_status",
            "status IN ('pending','dispatching','queued','running','retry_wait','completed','partial','failed')",
        )
        batch.create_check_constraint(
            "ck_mapping_link_projection_replay_batch_size", "batch_size >= 1 AND batch_size <= 100",
        )
        batch.create_check_constraint(
            "ck_mapping_link_projection_replay_dispatch_attempts", "dispatch_attempts >= 0",
        )

    op.create_index(
        "ix_mapping_link_projection_replay_dispatch",
        "v2_mapping_link_projection_replay_requests", ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_mapping_link_projection_replay_dispatch_lease",
        "v2_mapping_link_projection_replay_requests", ["status", "dispatch_lease_until"],
    )

    immutable_replay_fields = (
        "run_id,link_mapping_id,ontology_id,actor_id,actor_role,client_request_id,"
        "reason,request_digest,authorization_scope_json,batch_size,created_at"
    )
    terminal_fields = (
        "status,selected_count,projected_count,failed_count,pending_count,completed_at,error_code"
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE OR REPLACE FUNCTION protect_mapping_link_projection_replay_request() RETURNS trigger AS $$ "
            "BEGIN IF ROW(NEW.run_id,NEW.link_mapping_id,NEW.ontology_id,NEW.actor_id,NEW.actor_role,"
            "NEW.client_request_id,NEW.reason,NEW.request_digest,NEW.authorization_scope_json,NEW.batch_size,NEW.created_at) "
            "IS DISTINCT FROM ROW(OLD.run_id,OLD.link_mapping_id,OLD.ontology_id,OLD.actor_id,OLD.actor_role,"
            "OLD.client_request_id,OLD.reason,OLD.request_digest,OLD.authorization_scope_json,OLD.batch_size,OLD.created_at) "
            "THEN RAISE EXCEPTION 'Link projection replay request identity is immutable'; END IF; "
            "IF OLD.status IN ('completed','partial','failed') AND ROW(NEW.status,NEW.selected_count,"
            "NEW.projected_count,NEW.failed_count,NEW.pending_count,NEW.completed_at,NEW.error_code) IS DISTINCT FROM "
            "ROW(OLD.status,OLD.selected_count,OLD.projected_count,OLD.failed_count,OLD.pending_count,OLD.completed_at,OLD.error_code) "
            "THEN RAISE EXCEPTION 'Link projection replay terminal state is immutable'; END IF; RETURN NEW; END; "
            "$$ LANGUAGE plpgsql"
        ))
    elif dialect == "sqlite":
        changed = " OR ".join(
            f"OLD.{name} IS NOT NEW.{name}" for name in immutable_replay_fields.split(",")
        )
        terminal_changed = " OR ".join(
            f"OLD.{name} IS NOT NEW.{name}" for name in terminal_fields.split(",")
        )
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_mapping_link_projection_replay_request_basis"))
        op.execute(sa.text(
            "CREATE TRIGGER trg_mapping_link_projection_replay_request_basis "
            "BEFORE UPDATE ON v2_mapping_link_projection_replay_requests "
            f"WHEN {changed} OR (OLD.status IN ('completed','partial','failed') AND ({terminal_changed})) "
            "BEGIN SELECT RAISE(ABORT, 'Link projection replay request identity is immutable'); END"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_mapping_link_projection_replay_requests LIMIT 1"
    )).first():
        raise RuntimeError("Link projection replay requests contain dispatch/audit evidence; downgrade is unsafe")

    dialect = bind.dialect.name
    if dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_mapping_link_projection_replay_request_basis"))
    with op.batch_alter_table("v2_mapping_link_projection_replay_requests") as batch:
        batch.drop_index("ix_mapping_link_projection_replay_dispatch_lease")
        batch.drop_index("ix_mapping_link_projection_replay_dispatch")
        batch.drop_constraint("ck_mapping_link_projection_replay_dispatch_attempts", type_="check")
        batch.drop_constraint("ck_mapping_link_projection_replay_batch_size", type_="check")
        batch.drop_constraint("ck_mapping_link_projection_replay_status", type_="check")
        batch.drop_column("dispatch_lease_until")
        batch.drop_column("dispatch_lease_token")
        batch.drop_column("next_attempt_at")
        batch.drop_column("celery_task_id")
        batch.drop_column("error_code")
        batch.drop_column("dispatch_attempts")
        batch.drop_column("batch_size")
        batch.create_check_constraint(
            "ck_mapping_link_projection_replay_status",
            "status IN ('running','completed','partial','failed')",
        )
        batch.alter_column("status", server_default="running", existing_type=sa.String(length=16), existing_nullable=False)

    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE OR REPLACE FUNCTION protect_mapping_link_projection_replay_request() RETURNS trigger AS $$ "
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
    elif dialect == "sqlite":
        op.execute(sa.text(
            "CREATE TRIGGER trg_mapping_link_projection_replay_request_basis "
            "BEFORE UPDATE ON v2_mapping_link_projection_replay_requests "
            "WHEN OLD.run_id IS NOT NEW.run_id OR OLD.link_mapping_id IS NOT NEW.link_mapping_id "
            "OR OLD.ontology_id IS NOT NEW.ontology_id OR OLD.actor_id IS NOT NEW.actor_id "
            "OR OLD.actor_role IS NOT NEW.actor_role OR OLD.client_request_id IS NOT NEW.client_request_id "
            "OR OLD.reason IS NOT NEW.reason OR OLD.request_digest IS NOT NEW.request_digest "
            "OR OLD.authorization_scope_json IS NOT NEW.authorization_scope_json "
            "OR OLD.created_at IS NOT NEW.created_at "
            "OR (OLD.status IN ('completed','partial','failed') AND (OLD.status IS NOT NEW.status "
            "OR OLD.selected_count IS NOT NEW.selected_count OR OLD.projected_count IS NOT NEW.projected_count "
            "OR OLD.failed_count IS NOT NEW.failed_count OR OLD.pending_count IS NOT NEW.pending_count "
            "OR OLD.completed_at IS NOT NEW.completed_at OR OLD.error_code IS NOT NEW.error_code)) "
            "BEGIN SELECT RAISE(ABORT, 'Link projection replay request identity is immutable'); END"
        ))
