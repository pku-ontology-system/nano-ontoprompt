"""Persist named Scenario checkpoints, branches, and restore audit."""
from alembic import op
import sqlalchemy as sa


revision = "0071_scenario_checkpoints"
down_revision = "0070_query_view_graph_retention"
branch_labels = None
depends_on = None


CHECKPOINTS = "v2_scenario_checkpoints"
AUDITS = "v2_scenario_audits"


def upgrade():
    op.add_column("v2_scenarios", sa.Column("parent_scenario_id", sa.String(36), nullable=True))
    op.add_column("v2_scenarios", sa.Column("parent_checkpoint_id", sa.String(36), nullable=True))
    op.create_table(
        CHECKPOINTS,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scenario_id", sa.String(36), sa.ForeignKey("v2_scenarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("result_view_id", sa.String(64), sa.ForeignKey("v2_query_data_views.id"), nullable=False),
        sa.Column("content_digest", sa.String(128), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("client_request_id", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(128), nullable=False),
        sa.Column("source_etag", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scenario_id", "client_request_id", name="uq_v2_scenario_checkpoint_request"),
        sa.CheckConstraint("revision >= 0", name="ck_v2_scenario_checkpoint_revision"),
    )
    op.create_index("ix_v2_scenario_checkpoints_scenario_id", CHECKPOINTS, ["scenario_id"])
    op.create_index("ix_v2_scenario_checkpoints_scenario_created", CHECKPOINTS, ["scenario_id", "created_at"])
    op.create_index(
        "uq_v2_scenario_restore_request", AUDITS,
        ["scenario_id", "request_id"], unique=True,
        postgresql_where=sa.text("operation = 'restore_checkpoint' AND request_id IS NOT NULL"),
        sqlite_where=sa.text("operation = 'restore_checkpoint' AND request_id IS NOT NULL"),
    )
    op.create_index(
        "uq_v2_scenario_branch_request", AUDITS,
        ["scenario_id", "request_id"], unique=True,
        postgresql_where=sa.text("operation = 'branch' AND request_id IS NOT NULL"),
        sqlite_where=sa.text("operation = 'branch' AND request_id IS NOT NULL"),
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {CHECKPOINTS} LIMIT 1")).first():
        raise RuntimeError("Scenario checkpoints exist; downgrade would erase snapshot pins")
    if bind.execute(sa.text(
        f"SELECT 1 FROM {AUDITS} WHERE operation IN ('branch','restore_checkpoint') LIMIT 1"
    )).first():
        raise RuntimeError("Scenario branches/restores exist; downgrade would erase operation history")
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_scenarios WHERE parent_scenario_id IS NOT NULL "
        "OR parent_checkpoint_id IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Scenario branch lineage exists; downgrade would erase provenance")
    op.drop_index("uq_v2_scenario_branch_request", table_name=AUDITS)
    op.drop_index("uq_v2_scenario_restore_request", table_name=AUDITS)
    op.drop_index("ix_v2_scenario_checkpoints_scenario_created", table_name=CHECKPOINTS)
    op.drop_index("ix_v2_scenario_checkpoints_scenario_id", table_name=CHECKPOINTS)
    op.drop_table(CHECKPOINTS)
    op.drop_column("v2_scenarios", "parent_checkpoint_id")
    op.drop_column("v2_scenarios", "parent_scenario_id")
