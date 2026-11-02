"""Track leased Scenario graph builds for orphan recovery.

Revision ID: 0091_scenario_graph_build_recovery
Revises: 0090_scenario_action_run_claims
"""
from alembic import op
import sqlalchemy as sa


revision = "0091_scenario_graph_build_recovery"
down_revision = "0090_scenario_action_run_claims"
branch_labels = None
depends_on = None


RUNS = "v2_ontology_action_runs"
CLAIMS = "v2_ontology_action_run_claim_events"
GRAPH_EVENTS = (
    "'graph_write_started', 'graph_orphan_deleted', 'graph_cleanup_failed', "
    "'graph_reference_preserved'"
)


def _sqlite_claim_event_triggers(create: bool) -> None:
    if create:
        op.execute(sa.text(
            f"CREATE TRIGGER trg_action_run_claim_event_no_update BEFORE UPDATE ON {CLAIMS} "
            "BEGIN SELECT RAISE(ABORT, 'ActionRun execution claim events are append-only'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_action_run_claim_event_no_delete BEFORE DELETE ON {CLAIMS} "
            "BEGIN SELECT RAISE(ABORT, 'ActionRun execution claim events are append-only'); END"
        ))
    else:
        op.execute(sa.text("DROP TRIGGER trg_action_run_claim_event_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_action_run_claim_event_no_delete"))


def upgrade():
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        # SQLite batch alteration rebuilds the table and removes attached
        # triggers, so restore the append-only contract after the change.
        _sqlite_claim_event_triggers(create=False)

    with op.batch_alter_table(RUNS) as batch:
        batch.add_column(sa.Column("scenario_build_graph_key", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column("scenario_build_revision", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_action_run_scenario_build_pair",
            "(scenario_build_graph_key IS NULL AND scenario_build_revision IS NULL) OR "
            "(scenario_build_graph_key IS NOT NULL AND scenario_build_revision IS NOT NULL "
            "AND scenario_build_revision >= 1)",
        )
    op.create_index(
        "ix_action_run_scenario_build_recovery",
        RUNS,
        ["scenario_build_graph_key", "status"],
    )

    with op.batch_alter_table(CLAIMS) as batch:
        batch.drop_constraint("ck_action_run_claim_event_type", type_="check")
        batch.add_column(sa.Column("resource_digest", sa.String(length=64), nullable=True))
        batch.create_check_constraint(
            "ck_action_run_claim_event_type",
            "event_type IN ('claim_acquired', 'lease_renewed', 'attempt_expired', "
            "'attempt_completed', 'attempt_failed', 'attempt_reconciliation_required', "
            f"{GRAPH_EVENTS})",
        )

    if dialect == "sqlite":
        _sqlite_claim_event_triggers(create=True)


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        f"SELECT 1 FROM {RUNS} WHERE scenario_build_graph_key IS NOT NULL "
        "OR scenario_build_revision IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Scenario graph build markers exist; downgrade would erase recovery state")
    if bind.execute(sa.text(
        f"SELECT 1 FROM {CLAIMS} WHERE resource_digest IS NOT NULL "
        f"OR event_type IN ({GRAPH_EVENTS}) LIMIT 1"
    )).first():
        raise RuntimeError("Scenario graph recovery evidence exists; downgrade would erase audit history")

    dialect = bind.dialect.name
    if dialect == "sqlite":
        _sqlite_claim_event_triggers(create=False)

    with op.batch_alter_table(CLAIMS) as batch:
        batch.drop_constraint("ck_action_run_claim_event_type", type_="check")
        batch.drop_column("resource_digest")
        batch.create_check_constraint(
            "ck_action_run_claim_event_type",
            "event_type IN ('claim_acquired', 'lease_renewed', 'attempt_expired', "
            "'attempt_completed', 'attempt_failed', 'attempt_reconciliation_required')",
        )

    op.drop_index("ix_action_run_scenario_build_recovery", table_name=RUNS)
    with op.batch_alter_table(RUNS) as batch:
        batch.drop_constraint("ck_action_run_scenario_build_pair", type_="check")
        batch.drop_column("scenario_build_revision")
        batch.drop_column("scenario_build_graph_key")

    if dialect == "sqlite":
        _sqlite_claim_event_triggers(create=True)
