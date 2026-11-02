"""Add snapshot-pinned Link Mapping Apply runs and relation projection outbox.

Revision ID: 0087_mapping_link_apply_runs
Revises: 0086_mapping_link_definition_review
"""
from alembic import op
import sqlalchemy as sa


revision = "0087_mapping_link_apply_runs"
down_revision = "0086_mapping_link_definition_review"
branch_labels = None
depends_on = None

ACTIVE_RUN_SQL = "status IN ('pending','dispatching','queued','running','retry_wait')"


def upgrade():
    op.create_table(
        "v2_mapping_link_apply_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("link_mapping_id", sa.String(), nullable=False),
        sa.Column("link_review_id", sa.String(length=36), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("src_dataset_id", sa.String(), nullable=False),
        sa.Column("src_review_id", sa.String(), nullable=False),
        sa.Column("src_dataset_version_id", sa.String(length=120), nullable=False),
        sa.Column("src_mapping_id", sa.String(), nullable=False),
        sa.Column("src_mapping_review_id", sa.String(length=36), nullable=False),
        sa.Column("src_mapping_apply_run_id", sa.String(length=36), nullable=False),
        sa.Column("tgt_dataset_id", sa.String(), nullable=False),
        sa.Column("tgt_review_id", sa.String(), nullable=False),
        sa.Column("tgt_dataset_version_id", sa.String(length=120), nullable=False),
        sa.Column("tgt_mapping_id", sa.String(), nullable=False),
        sa.Column("tgt_mapping_review_id", sa.String(length=36), nullable=False),
        sa.Column("tgt_mapping_apply_run_id", sa.String(length=36), nullable=False),
        sa.Column("input_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("preview_digest", sa.String(length=64), nullable=False),
        sa.Column("preview_summary_json", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=True),
        sa.Column("request_actor_kind", sa.String(length=16), nullable=False),
        sa.Column("request_actor_id", sa.String(length=120), nullable=False),
        sa.Column("worker_actor_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("execution_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("dispatch_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("celery_task_id", sa.String(length=200), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','dispatching','queued','running','retry_wait','succeeded','failed')",
            name="ck_mapping_link_apply_run_status",
        ),
        sa.CheckConstraint("execution_attempts >= 0", name="ck_mapping_link_apply_run_execution_attempts"),
        sa.CheckConstraint("dispatch_attempts >= 0", name="ck_mapping_link_apply_run_dispatch_attempts"),
        sa.CheckConstraint("request_actor_kind IN ('user','system')", name="ck_mapping_link_apply_run_actor_kind"),
        sa.CheckConstraint(
            "(request_actor_kind = 'user' AND requested_by IS NOT NULL AND request_actor_id = requested_by) "
            "OR (request_actor_kind = 'system' AND request_actor_id <> '')",
            name="ck_mapping_link_apply_run_actor_identity",
        ),
        sa.ForeignKeyConstraint(["link_mapping_id"], ["v2_ontology_link_mappings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["link_review_id"], ["v2_mapping_link_definition_reviews.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["src_dataset_id"], ["v2_curated_datasets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["src_review_id"], ["v2_curated_reviews.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["src_mapping_id"], ["v2_ontology_mappings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["src_mapping_review_id"], ["v2_mapping_definition_reviews.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["src_mapping_apply_run_id"], ["v2_mapping_apply_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tgt_dataset_id"], ["v2_curated_datasets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tgt_review_id"], ["v2_curated_reviews.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tgt_mapping_id"], ["v2_ontology_mappings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tgt_mapping_review_id"], ["v2_mapping_definition_reviews.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tgt_mapping_apply_run_id"], ["v2_mapping_apply_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_mapping_link_apply_run_idempotency"),
    )
    op.create_index("ix_mapping_link_apply_dispatch_due", "v2_mapping_link_apply_runs", ["status", "next_attempt_at"])
    op.create_index("ix_mapping_link_apply_dispatch_lease", "v2_mapping_link_apply_runs", ["status", "dispatch_lease_until"])
    op.create_index("ix_mapping_link_apply_execution_lease", "v2_mapping_link_apply_runs", ["status", "run_lease_until"])
    op.create_index(
        "uq_mapping_link_apply_active", "v2_mapping_link_apply_runs", ["link_mapping_id"], unique=True,
        sqlite_where=sa.text(ACTIVE_RUN_SQL),
        postgresql_where=sa.text(ACTIVE_RUN_SQL),
    )

    op.create_table(
        "v2_mapping_link_apply_run_attempt_events",
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
        sa.CheckConstraint("sequence_no >= 1", name="ck_mapping_link_apply_attempt_sequence"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_mapping_link_apply_attempt_number"),
        sa.CheckConstraint("digest_version >= 1", name="ck_mapping_link_apply_attempt_digest_version"),
        sa.CheckConstraint(
            "event_type IN ('attempt_started','attempt_succeeded','attempt_failed','attempt_outcome_unknown')",
            name="ck_mapping_link_apply_attempt_event_type",
        ),
        sa.CheckConstraint("status IN ('running','succeeded','retry_wait','failed')", name="ck_mapping_link_apply_attempt_status"),
        sa.CheckConstraint("actor_kind IN ('worker','system')", name="ck_mapping_link_apply_attempt_actor_kind"),
        sa.CheckConstraint("actor_id <> ''", name="ck_mapping_link_apply_attempt_actor_id"),
        sa.ForeignKeyConstraint(["run_id"], ["v2_mapping_link_apply_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence_no", name="uq_mapping_link_apply_attempt_sequence"),
    )
    op.create_index(
        "ix_mapping_link_apply_attempt_run_sequence",
        "v2_mapping_link_apply_run_attempt_events", ["run_id", "sequence_no"],
    )

    op.create_table(
        "v2_mapping_link_relation_changes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("link_mapping_id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("relation_id", sa.String(), nullable=False),
        sa.Column("operation", sa.String(length=10), nullable=False),
        sa.Column("relation_type", sa.String(length=100), nullable=False),
        sa.Column("source_entity_id", sa.String(), nullable=False),
        sa.Column("target_entity_id", sa.String(), nullable=False),
        sa.Column("source_dataset_version_id", sa.String(length=120), nullable=False),
        sa.Column("target_dataset_version_id", sa.String(length=120), nullable=False),
        sa.Column("before_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("after_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("operation IN ('upsert','delete')", name="ck_mapping_link_relation_change_operation"),
        sa.CheckConstraint("sequence_no >= 1", name="ck_mapping_link_relation_change_sequence"),
        sa.ForeignKeyConstraint(["run_id"], ["v2_mapping_link_apply_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["link_mapping_id"], ["v2_ontology_link_mappings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence_no", name="uq_mapping_link_relation_change_sequence"),
        sa.UniqueConstraint("run_id", "relation_id", name="uq_mapping_link_relation_change_run_relation"),
    )
    op.create_index(
        "ix_mapping_link_relation_change_link_created",
        "v2_mapping_link_relation_changes", ["link_mapping_id", "occurred_at"],
    )
    op.create_table(
        "v2_mapping_link_relation_projection_states",
        sa.Column("change_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','projecting','projected','dead_letter')",
            name="ck_mapping_link_relation_projection_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_mapping_link_relation_projection_attempts"),
        sa.ForeignKeyConstraint(["change_id"], ["v2_mapping_link_relation_changes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("change_id"),
    )
    op.create_index(
        "ix_mapping_link_relation_projection_due",
        "v2_mapping_link_relation_projection_states", ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_mapping_link_relation_projection_lease",
        "v2_mapping_link_relation_projection_states", ["status", "lease_until"],
    )

    # The legacy Relation model points at durable row Entity IDs. These columns
    # identify which Link Mapping owns the current relation and both data pins.
    with op.batch_alter_table("relations") as batch:
        batch.add_column(sa.Column("source_link_mapping_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("source_link_apply_run_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("source_dataset_version_id", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("target_dataset_version_id", sa.String(length=120), nullable=True))
        batch.create_foreign_key(
            "fk_relations_source_link_mapping", "v2_ontology_link_mappings",
            ["source_link_mapping_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_relations_source_link_apply_run", "v2_mapping_link_apply_runs",
            ["source_link_apply_run_id"], ["id"], ondelete="RESTRICT",
        )
    op.create_index("ix_relations_source_link_mapping_id", "relations", ["source_link_mapping_id"])
    op.create_index("ix_relations_source_link_apply_run_id", "relations", ["source_link_apply_run_id"])

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION protect_mapping_link_apply_run_pins() RETURNS trigger AS $$ "
            "BEGIN IF ROW(NEW.link_mapping_id,NEW.link_review_id,NEW.ontology_id,"
            "NEW.src_dataset_id,NEW.src_review_id,NEW.src_dataset_version_id,NEW.src_mapping_id,"
            "NEW.src_mapping_review_id,NEW.src_mapping_apply_run_id,NEW.tgt_dataset_id,NEW.tgt_review_id,"
            "NEW.tgt_dataset_version_id,NEW.tgt_mapping_id,NEW.tgt_mapping_review_id,NEW.tgt_mapping_apply_run_id,"
            "NEW.input_snapshot_json,NEW.preview_digest,NEW.preview_summary_json,NEW.idempotency_key,"
            "NEW.requested_by,NEW.request_actor_kind,NEW.request_actor_id,NEW.created_at) IS DISTINCT FROM "
            "ROW(OLD.link_mapping_id,OLD.link_review_id,OLD.ontology_id,OLD.src_dataset_id,OLD.src_review_id,"
            "OLD.src_dataset_version_id,OLD.src_mapping_id,OLD.src_mapping_review_id,OLD.src_mapping_apply_run_id,"
            "OLD.tgt_dataset_id,OLD.tgt_review_id,OLD.tgt_dataset_version_id,OLD.tgt_mapping_id,"
            "OLD.tgt_mapping_review_id,OLD.tgt_mapping_apply_run_id,OLD.input_snapshot_json,OLD.preview_digest,"
            "OLD.preview_summary_json,OLD.idempotency_key,OLD.requested_by,OLD.request_actor_kind,"
            "OLD.request_actor_id,OLD.created_at) THEN RAISE EXCEPTION 'Link Apply pins are immutable'; END IF; "
            "IF OLD.status IN ('succeeded','failed') AND ROW(NEW.status,NEW.result_json,NEW.error_code,"
            "NEW.error_message,NEW.completed_at) IS DISTINCT FROM ROW(OLD.status,OLD.result_json,"
            "OLD.error_code,OLD.error_message,OLD.completed_at) THEN "
            "RAISE EXCEPTION 'Link Apply terminal state is immutable'; END IF; RETURN NEW; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            "CREATE TRIGGER trg_mapping_link_apply_run_pins BEFORE UPDATE ON v2_mapping_link_apply_runs "
            "FOR EACH ROW EXECUTE FUNCTION protect_mapping_link_apply_run_pins()"
        ))
        op.execute(sa.text(
            "CREATE FUNCTION reject_mapping_link_append_only_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'Link Mapping Apply evidence is append-only'; END; $$ LANGUAGE plpgsql"
        ))
        for table, trigger in (
            ("v2_mapping_link_apply_run_attempt_events", "trg_mapping_link_apply_attempt_no_mutation"),
            ("v2_mapping_link_relation_changes", "trg_mapping_link_relation_change_no_mutation"),
        ):
            op.execute(sa.text(
                f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_mapping_link_append_only_mutation()"
            ))
    elif dialect == "sqlite":
        immutable_fields = (
            "link_mapping_id,link_review_id,ontology_id,src_dataset_id,src_review_id,src_dataset_version_id,"
            "src_mapping_id,src_mapping_review_id,src_mapping_apply_run_id,tgt_dataset_id,tgt_review_id,"
            "tgt_dataset_version_id,tgt_mapping_id,tgt_mapping_review_id,tgt_mapping_apply_run_id,"
            "input_snapshot_json,preview_digest,preview_summary_json,idempotency_key,requested_by,"
            "request_actor_kind,request_actor_id,created_at"
        )
        changed = " OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in immutable_fields.split(","))
        op.execute(sa.text(
            "CREATE TRIGGER trg_mapping_link_apply_run_pins BEFORE UPDATE ON v2_mapping_link_apply_runs "
            f"WHEN {changed} OR (OLD.status IN ('succeeded','failed') AND (OLD.status IS NOT NEW.status "
            "OR OLD.result_json IS NOT NEW.result_json OR OLD.error_code IS NOT NEW.error_code "
            "OR OLD.error_message IS NOT NEW.error_message OR OLD.completed_at IS NOT NEW.completed_at)) "
            "BEGIN SELECT RAISE(ABORT, 'Link Apply pins or terminal state are immutable'); END"
        ))
        for table, prefix in (
            ("v2_mapping_link_apply_run_attempt_events", "mapping_link_apply_attempt"),
            ("v2_mapping_link_relation_changes", "mapping_link_relation_change"),
        ):
            for operation in ("UPDATE", "DELETE"):
                suffix = "update" if operation == "UPDATE" else "delete"
                op.execute(sa.text(
                    f"CREATE TRIGGER trg_{prefix}_no_{suffix} BEFORE {operation} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'Link Mapping Apply evidence is append-only'); END"
                ))


def downgrade():
    bind = op.get_bind()
    for table in (
        "v2_mapping_link_relation_changes",
        "v2_mapping_link_apply_run_attempt_events",
        "v2_mapping_link_apply_runs",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"{table} contains audit data; downgrade would delete Link Apply evidence")
    if bind.execute(sa.text(
        "SELECT 1 FROM relations WHERE source_link_mapping_id IS NOT NULL "
        "OR source_link_apply_run_id IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Link Mapping relation facts exist; downgrade would remove their provenance")

    dialect = bind.dialect.name
    if dialect == "postgresql":
        for table, trigger in (
            ("v2_mapping_link_relation_changes", "trg_mapping_link_relation_change_no_mutation"),
            ("v2_mapping_link_apply_run_attempt_events", "trg_mapping_link_apply_attempt_no_mutation"),
        ):
            op.execute(sa.text(f"DROP TRIGGER {trigger} ON {table}"))
        op.execute(sa.text("DROP TRIGGER trg_mapping_link_apply_run_pins ON v2_mapping_link_apply_runs"))
        op.execute(sa.text("DROP FUNCTION reject_mapping_link_append_only_mutation()"))
        op.execute(sa.text("DROP FUNCTION protect_mapping_link_apply_run_pins()"))
    elif dialect == "sqlite":
        for name in (
            "trg_mapping_link_relation_change_no_delete",
            "trg_mapping_link_relation_change_no_update",
            "trg_mapping_link_apply_attempt_no_delete",
            "trg_mapping_link_apply_attempt_no_update",
            "trg_mapping_link_apply_run_pins",
        ):
            op.execute(sa.text(f"DROP TRIGGER {name}"))

    op.drop_index("ix_relations_source_link_apply_run_id", table_name="relations")
    op.drop_index("ix_relations_source_link_mapping_id", table_name="relations")
    with op.batch_alter_table("relations", recreate="always") as batch:
        batch.drop_constraint("fk_relations_source_link_apply_run", type_="foreignkey")
        batch.drop_constraint("fk_relations_source_link_mapping", type_="foreignkey")
        batch.drop_column("target_dataset_version_id")
        batch.drop_column("source_dataset_version_id")
        batch.drop_column("source_link_apply_run_id")
        batch.drop_column("source_link_mapping_id")

    op.drop_index("ix_mapping_link_relation_projection_lease", table_name="v2_mapping_link_relation_projection_states")
    op.drop_index("ix_mapping_link_relation_projection_due", table_name="v2_mapping_link_relation_projection_states")
    op.drop_table("v2_mapping_link_relation_projection_states")
    op.drop_index("ix_mapping_link_relation_change_link_created", table_name="v2_mapping_link_relation_changes")
    op.drop_table("v2_mapping_link_relation_changes")
    op.drop_index("ix_mapping_link_apply_attempt_run_sequence", table_name="v2_mapping_link_apply_run_attempt_events")
    op.drop_table("v2_mapping_link_apply_run_attempt_events")
    op.drop_index("uq_mapping_link_apply_active", table_name="v2_mapping_link_apply_runs")
    op.drop_index("ix_mapping_link_apply_execution_lease", table_name="v2_mapping_link_apply_runs")
    op.drop_index("ix_mapping_link_apply_dispatch_lease", table_name="v2_mapping_link_apply_runs")
    op.drop_index("ix_mapping_link_apply_dispatch_due", table_name="v2_mapping_link_apply_runs")
    op.drop_table("v2_mapping_link_apply_runs")
