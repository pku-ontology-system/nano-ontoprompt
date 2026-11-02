"""Retain query-view manifests while pruning expired FalkorDB graphs."""
from alembic import op
import sqlalchemy as sa


revision = "0070_query_view_graph_retention"
down_revision = "0069_scenario_retention_expiry"
branch_labels = None
depends_on = None


EVENTS = "v2_query_view_retention_events"


def upgrade():
    op.create_table(
        EVENTS,
        sa.Column("id", sa.String(36), primary_key=True),
        # No foreign keys by design: the cleanup audit must outlive its source
        # view and ontology records.
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("view_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False, server_default="graph_deleted"),
        sa.Column("reason", sa.String(80), nullable=False, server_default="retention_expired"),
        sa.Column("content_digest", sa.String(128), nullable=True),
        sa.Column("object_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("edge_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("view_id", "event_type", name="uq_v2_query_view_retention_event"),
        sa.CheckConstraint("event_type = 'graph_deleted'", name="ck_v2_query_view_retention_event_type"),
        sa.CheckConstraint("reason = 'retention_expired'", name="ck_v2_query_view_retention_event_reason"),
        sa.CheckConstraint("object_count >= 0 AND edge_count >= 0", name="ck_v2_query_view_retention_event_counts"),
    )
    op.create_index(
        "ix_v2_query_view_retention_events_ontology_created",
        EVENTS,
        ["ontology_id", "created_at"],
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {EVENTS} LIMIT 1")).first():
        raise RuntimeError("Query-view retention events exist; downgrade would erase cleanup audit")
    op.drop_index("ix_v2_query_view_retention_events_ontology_created", table_name=EVENTS)
    op.drop_table(EVENTS)
