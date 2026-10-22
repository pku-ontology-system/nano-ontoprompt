"""Saved calculations for the bounded benchmark decision lab."""
from alembic import op
import sqlalchemy as sa

revision = "0036_decision_lab_runs"
down_revision = "0035_merge_semantic_runtime"
branch_labels = None
depends_on = None


def upgrade():
    if "v2_decision_lab_runs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "v2_decision_lab_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("domain", sa.String(40), nullable=False),
        sa.Column("ontology_id", sa.String(), sa.ForeignKey("ontology_projects.id"), nullable=False),
        sa.Column("dataset_version_id", sa.String(), sa.ForeignKey("v2_dataset_versions.id"), nullable=False),
        sa.Column("case_key", sa.String(100), nullable=False),
        sa.Column("case_name", sa.String(200), nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("output_digest", sa.String(64), nullable=False),
        sa.Column("client_request_id", sa.String(120), nullable=False),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("domain", "created_by", "client_request_id", name="uq_decision_lab_request"),
    )
    op.create_index("ix_decision_lab_domain_created", "v2_decision_lab_runs", ["domain", "created_at"])


def downgrade():
    if "v2_decision_lab_runs" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_decision_lab_domain_created", table_name="v2_decision_lab_runs")
        op.drop_table("v2_decision_lab_runs")
