"""DecisionCase lifecycle and reviewed ActionRun provenance."""
from alembic import op
import sqlalchemy as sa

revision = "0037_decision_cases"
down_revision = "0036_decision_lab_runs"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    run_columns = {column["name"] for column in inspector.get_columns("v2_ontology_action_runs")}
    additions = {
        "decision_case_id": sa.String(36), "client_request_id": sa.String(120),
        "input_digest": sa.String(64), "preview_digest": sa.String(64),
        "permission_digest": sa.String(64), "before_digest": sa.String(64),
        "after_digest": sa.String(64), "expected_etag": sa.String(64),
        "action_version": sa.Integer(), "trace_id": sa.String(36),
        "projection_status": sa.String(20), "projection_error": sa.Text(),
    }
    for name, column_type in additions.items():
        if name not in run_columns:
            op.add_column("v2_ontology_action_runs", sa.Column(name, column_type, nullable=True))
    if "v2_decision_cases" not in inspector.get_table_names():
        op.create_table(
            "v2_decision_cases",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("ontology_id", sa.String(), sa.ForeignKey("ontology_projects.id"), nullable=False),
            sa.Column("action_type_id", sa.String(), sa.ForeignKey("v2_ontology_action_types.id"), nullable=False),
            sa.Column("kind", sa.String(60), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("target_object_id", sa.String(200), nullable=False),
            sa.Column("target_object_type", sa.String(200), nullable=False),
            sa.Column("parameters", sa.JSON(), nullable=False),
            sa.Column("evidence_refs", sa.JSON(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("context_json", sa.JSON(), nullable=False),
            sa.Column("expected_etag", sa.String(64), nullable=False),
            sa.Column("client_request_id", sa.String(120), nullable=False),
            sa.Column("request_digest", sa.String(64), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("preview_json", sa.JSON(), nullable=True),
            sa.Column("preview_digest", sa.String(64), nullable=True),
            sa.Column("permission_digest", sa.String(64), nullable=True),
            sa.Column("action_version", sa.Integer(), nullable=True),
            sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("reviewed_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("review_comment", sa.Text(), nullable=True),
            sa.Column("action_run_id", sa.String(), sa.ForeignKey("v2_ontology_action_runs.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("ontology_id", "created_by", "client_request_id", name="uq_decision_case_request"),
        )
        op.create_index("ix_decision_cases_ontology", "v2_decision_cases", ["ontology_id"])
        op.create_index("ix_decision_cases_target", "v2_decision_cases", ["target_object_id"])
        op.create_index("ix_decision_cases_status", "v2_decision_cases", ["status"])
    if "v2_decision_case_events" not in inspector.get_table_names():
        op.create_table(
            "v2_decision_case_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("case_id", sa.String(36), sa.ForeignKey("v2_decision_cases.id"), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(60), nullable=False),
            sa.Column("actor_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("trace_id", sa.String(36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("case_id", "sequence", name="uq_decision_case_event_sequence"),
        )
        op.create_index("ix_decision_case_events_case", "v2_decision_case_events", ["case_id"])
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("v2_ontology_action_runs")}
    if "ix_action_runs_decision_case" not in indexes:
        op.create_index("ix_action_runs_decision_case", "v2_ontology_action_runs", ["decision_case_id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()
    if "v2_decision_cases" in tables and bind.execute(sa.text(
        "SELECT 1 FROM v2_decision_cases LIMIT 1")).first():
        raise RuntimeError("DecisionCase audit facts exist; downgrade would delete them")
    if "v2_decision_case_events" in tables:
        op.drop_index("ix_decision_case_events_case", table_name="v2_decision_case_events")
        op.drop_table("v2_decision_case_events")
    if "v2_decision_cases" in tables:
        for name in ("ix_decision_cases_status", "ix_decision_cases_target", "ix_decision_cases_ontology"):
            op.drop_index(name, table_name="v2_decision_cases")
        op.drop_table("v2_decision_cases")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("v2_ontology_action_runs")}
    if "ix_action_runs_decision_case" in indexes:
        op.drop_index("ix_action_runs_decision_case", table_name="v2_ontology_action_runs")
    for name in reversed(("decision_case_id", "client_request_id", "input_digest", "preview_digest",
                          "permission_digest", "before_digest", "after_digest", "expected_etag",
                          "action_version", "trace_id", "projection_status", "projection_error")):
        if name in {column["name"] for column in sa.inspect(bind).get_columns("v2_ontology_action_runs")}:
            op.drop_column("v2_ontology_action_runs", name)
