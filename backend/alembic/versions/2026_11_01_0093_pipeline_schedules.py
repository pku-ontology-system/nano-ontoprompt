"""Persist version-pinned Pipeline schedules and their run history."""
from alembic import op
import sqlalchemy as sa


revision = "0093_pipeline_schedules"
down_revision = "0092_curated_review_version_restrict"
branch_labels = None
depends_on = None


SCHEDULES = "v2_pipeline_schedules"
REVISIONS = "v2_pipeline_schedule_revisions"
RUNS = "v2_pipeline_schedule_runs"


def upgrade():
    op.create_table(
        SCHEDULES,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("pipeline_id", sa.String(), sa.ForeignKey("v2_pipelines.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("steward_id", sa.String(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("service_principal_id", sa.String(length=100), nullable=False),
        sa.Column("pipeline_version_id", sa.String(), sa.ForeignKey("v2_pipeline_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("cron_expression", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("misfire_policy", sa.String(length=20), nullable=False),
        sa.Column("misfire_grace_seconds", sa.Integer(), nullable=False),
        sa.Column("overlap_policy", sa.String(length=20), nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("pipeline_id", name="uq_v2_pipeline_schedules_pipeline"),
        sa.CheckConstraint("revision >= 1", name="ck_v2_pipeline_schedule_revision"),
        sa.CheckConstraint("misfire_policy IN ('skip','run_once')", name="ck_v2_pipeline_schedule_misfire"),
        sa.CheckConstraint("overlap_policy IN ('forbid','allow')", name="ck_v2_pipeline_schedule_overlap"),
        sa.CheckConstraint("misfire_grace_seconds >= 0", name="ck_v2_pipeline_schedule_grace"),
    )
    op.create_index("ix_v2_pipeline_schedules_due", SCHEDULES, ["enabled", "next_fire_at"])

    op.create_table(
        REVISIONS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("schedule_id", sa.String(length=36), sa.ForeignKey(f"{SCHEDULES}.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("pipeline_version_id", sa.String(), sa.ForeignKey("v2_pipeline_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("schedule_id", "revision", name="uq_v2_pipeline_schedule_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_v2_pipeline_schedule_revision_no"),
    )
    op.create_index("ix_v2_pipeline_schedule_revisions_version", REVISIONS, ["pipeline_version_id"])

    op.create_table(
        RUNS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("schedule_id", sa.String(length=36), sa.ForeignKey(f"{SCHEDULES}.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("schedule_revision_id", sa.String(length=36), sa.ForeignKey(f"{REVISIONS}.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("local_slot", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("pipeline_run_id", sa.String(), sa.ForeignKey("v2_pipeline_runs.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("coalesced_count", sa.Integer(), nullable=False),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("schedule_revision_id", "local_slot", name="uq_v2_pipeline_schedule_tick"),
        sa.UniqueConstraint("pipeline_run_id", name="uq_v2_pipeline_schedule_run_link"),
        sa.CheckConstraint(
            "status IN ('pending','dispatching','queued','running','success','degraded','failed','skipped','cancelled')",
            name="ck_v2_pipeline_schedule_run_status",
        ),
    )
    op.create_index("ix_v2_pipeline_schedule_runs_due", RUNS, ["status", "scheduled_for"])
    op.create_index("ix_v2_pipeline_schedule_runs_schedule", RUNS, ["schedule_id", "created_at"])

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_pipeline_schedule_revision_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'Pipeline Schedule revisions are immutable'; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_schedule_revision_immutable BEFORE UPDATE OR DELETE ON {REVISIONS} "
            "FOR EACH ROW EXECUTE FUNCTION reject_pipeline_schedule_revision_mutation()"
        ))
        op.execute(sa.text(
            "CREATE FUNCTION protect_pipeline_schedule_run() RETURNS trigger AS $$ BEGIN "
            "IF ROW(NEW.id, NEW.schedule_id, NEW.schedule_revision_id, NEW.scheduled_for, NEW.local_slot, "
            "NEW.coalesced_count, NEW.created_at) IS DISTINCT FROM ROW(OLD.id, OLD.schedule_id, "
            "OLD.schedule_revision_id, OLD.scheduled_for, OLD.local_slot, OLD.coalesced_count, OLD.created_at) "
            "THEN RAISE EXCEPTION 'Pipeline Schedule Run provenance is immutable'; END IF; "
            "IF OLD.pipeline_run_id IS NOT NULL AND NEW.pipeline_run_id IS DISTINCT FROM OLD.pipeline_run_id "
            "THEN RAISE EXCEPTION 'Pipeline Schedule Run link is immutable'; END IF; "
            "IF NEW.dispatch_attempts < OLD.dispatch_attempts "
            "THEN RAISE EXCEPTION 'Pipeline Schedule Run dispatch attempts cannot decrease'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_schedule_run_protect BEFORE UPDATE ON {RUNS} "
            "FOR EACH ROW EXECUTE FUNCTION protect_pipeline_schedule_run()"
        ))
        op.execute(sa.text(
            "CREATE FUNCTION reject_pipeline_schedule_run_delete() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'Pipeline Schedule Run history is retained'; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_schedule_run_no_delete BEFORE DELETE ON {RUNS} "
            "FOR EACH ROW EXECUTE FUNCTION reject_pipeline_schedule_run_delete()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_schedule_revision_no_update BEFORE UPDATE ON {REVISIONS} "
            "BEGIN SELECT RAISE(ABORT, 'Pipeline Schedule revisions are immutable'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_schedule_revision_no_delete BEFORE DELETE ON {REVISIONS} "
            "BEGIN SELECT RAISE(ABORT, 'Pipeline Schedule revisions are immutable'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_schedule_run_protect BEFORE UPDATE ON {RUNS} "
            "WHEN OLD.id IS NOT NEW.id OR OLD.schedule_id IS NOT NEW.schedule_id "
            "OR OLD.schedule_revision_id IS NOT NEW.schedule_revision_id "
            "OR OLD.scheduled_for IS NOT NEW.scheduled_for OR OLD.local_slot IS NOT NEW.local_slot "
            "OR OLD.coalesced_count IS NOT NEW.coalesced_count OR OLD.created_at IS NOT NEW.created_at "
            "OR (OLD.pipeline_run_id IS NOT NULL AND OLD.pipeline_run_id IS NOT NEW.pipeline_run_id) "
            "OR NEW.dispatch_attempts < OLD.dispatch_attempts "
            "BEGIN SELECT RAISE(ABORT, 'Pipeline Schedule Run provenance is immutable'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_pipeline_schedule_run_no_delete BEFORE DELETE ON {RUNS} "
            "BEGIN SELECT RAISE(ABORT, 'Pipeline Schedule Run history is retained'); END"
        ))


def downgrade():
    bind = op.get_bind()
    for table in (RUNS, REVISIONS, SCHEDULES):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"{table} contains schedule audit data; downgrade would delete history")
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER trg_pipeline_schedule_run_no_delete ON {RUNS}"))
        op.execute(sa.text("DROP FUNCTION reject_pipeline_schedule_run_delete()"))
        op.execute(sa.text(f"DROP TRIGGER trg_pipeline_schedule_run_protect ON {RUNS}"))
        op.execute(sa.text("DROP FUNCTION protect_pipeline_schedule_run()"))
        op.execute(sa.text(f"DROP TRIGGER trg_pipeline_schedule_revision_immutable ON {REVISIONS}"))
        op.execute(sa.text("DROP FUNCTION reject_pipeline_schedule_revision_mutation()"))
    elif dialect == "sqlite":
        for name in (
            "trg_pipeline_schedule_run_no_delete",
            "trg_pipeline_schedule_run_protect",
            "trg_pipeline_schedule_revision_no_delete",
            "trg_pipeline_schedule_revision_no_update",
        ):
            op.execute(sa.text(f"DROP TRIGGER {name}"))
    op.drop_index("ix_v2_pipeline_schedule_runs_schedule", table_name=RUNS)
    op.drop_index("ix_v2_pipeline_schedule_runs_due", table_name=RUNS)
    op.drop_table(RUNS)
    op.drop_index("ix_v2_pipeline_schedule_revisions_version", table_name=REVISIONS)
    op.drop_table(REVISIONS)
    op.drop_index("ix_v2_pipeline_schedules_due", table_name=SCHEDULES)
    op.drop_table(SCHEDULES)
