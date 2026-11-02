"""Add fenced execution leases for registered Scenario ActionRuns.

Revision ID: 0090_scenario_action_run_claims
Revises: 0089_link_projection_replay_dispatch
"""
from alembic import op
import sqlalchemy as sa


revision = "0090_scenario_action_run_claims"
down_revision = "0089_link_projection_replay_dispatch"
branch_labels = None
depends_on = None


RUNS = "v2_ontology_action_runs"
CLAIMS = "v2_ontology_action_run_claim_events"


def upgrade():
    with op.batch_alter_table(RUNS) as batch:
        batch.add_column(sa.Column(
            "execution_attempt", sa.Integer(), server_default="0", nullable=False,
        ))
        batch.add_column(sa.Column("execution_lease_token", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column(
            "execution_lease_until", sa.DateTime(timezone=True), nullable=True,
        ))
        batch.create_check_constraint(
            "ck_action_run_execution_attempt_nonnegative", "execution_attempt >= 0",
        )
        batch.create_check_constraint(
            "ck_action_run_execution_lease_pair",
            "(execution_lease_token IS NULL AND execution_lease_until IS NULL) OR "
            "(execution_lease_token IS NOT NULL AND execution_lease_until IS NOT NULL)",
        )

    op.create_index(
        "ix_action_run_execution_lease", RUNS, ["status", "execution_lease_until"],
    )
    op.create_table(
        CLAIMS,
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("action_run_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(length=200), nullable=True),
        sa.Column("input_digest", sa.String(length=64), nullable=True),
        sa.Column("claim_token_digest", sa.String(length=64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_action_run_claim_event_sequence_positive"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_action_run_claim_event_attempt_positive"),
        sa.CheckConstraint(
            "event_type IN ('claim_acquired', 'lease_renewed', 'attempt_expired', "
            "'attempt_completed', 'attempt_failed', 'attempt_reconciliation_required')",
            name="ck_action_run_claim_event_type",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'agent', 'worker', 'system')",
            name="ck_action_run_claim_event_actor_kind",
        ),
        sa.ForeignKeyConstraint(
            ["action_run_id"], [f"{RUNS}.id"], name="fk_action_run_claim_event_run",
        ),
        sa.UniqueConstraint(
            "action_run_id", "sequence", name="uq_action_run_claim_event_sequence",
        ),
    )
    op.create_index(
        "ix_action_run_claim_events_run_sequence", CLAIMS, ["action_run_id", "sequence"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_action_run_claim_event_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'ActionRun execution claim events are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_action_run_claim_event_no_update_delete "
            f"BEFORE UPDATE OR DELETE ON {CLAIMS} FOR EACH ROW "
            "EXECUTE FUNCTION reject_action_run_claim_event_mutation()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_action_run_claim_event_no_update BEFORE UPDATE ON {CLAIMS} "
            "BEGIN SELECT RAISE(ABORT, 'ActionRun execution claim events are append-only'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_action_run_claim_event_no_delete BEFORE DELETE ON {CLAIMS} "
            "BEGIN SELECT RAISE(ABORT, 'ActionRun execution claim events are append-only'); END"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {CLAIMS} LIMIT 1")).first():
        raise RuntimeError("ActionRun execution claim history exists; downgrade would erase audit evidence")
    if bind.execute(sa.text(
        f"SELECT 1 FROM {RUNS} WHERE execution_attempt > 0 LIMIT 1"
    )).first():
        raise RuntimeError("Scenario ActionRun claims exist; downgrade would erase fencing state")

    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER trg_action_run_claim_event_no_update_delete ON {CLAIMS}"))
        op.execute(sa.text("DROP FUNCTION reject_action_run_claim_event_mutation()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_action_run_claim_event_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_action_run_claim_event_no_delete"))

    op.drop_index("ix_action_run_claim_events_run_sequence", table_name=CLAIMS)
    op.drop_table(CLAIMS)
    op.drop_index("ix_action_run_execution_lease", table_name=RUNS)
    with op.batch_alter_table(RUNS) as batch:
        batch.drop_constraint("ck_action_run_execution_lease_pair", type_="check")
        batch.drop_constraint("ck_action_run_execution_attempt_nonnegative", type_="check")
        batch.drop_column("execution_lease_until")
        batch.drop_column("execution_lease_token")
        batch.drop_column("execution_attempt")
