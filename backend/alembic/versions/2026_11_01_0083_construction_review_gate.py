"""Add an independent review gate before Dataset Construction writes.

Revision ID: 0083_construction_review_gate
Revises: 0082_dataset_resource_owners
"""
from alembic import op
import sqlalchemy as sa


revision = "0083_construction_review_gate"
down_revision = "0082_dataset_resource_owners"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v2_construction_reviews",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("draft_id", sa.String(), nullable=False),
        sa.Column("dataset_id", sa.String(), nullable=False),
        sa.Column("dataset_version_id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=True),
        sa.Column("base_revision_id", sa.String(), nullable=True),
        sa.Column("mapping_task_id", sa.String(), nullable=True),
        sa.Column("construction_run_id", sa.String(), nullable=True),
        sa.Column("candidate_digest", sa.String(length=64), nullable=False),
        sa.Column("candidate_json", sa.JSON(), nullable=False),
        sa.Column("impact_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending_review", nullable=False),
        sa.Column("submitted_by", sa.String(), nullable=True),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("decision", sa.String(length=30), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_review','approved','rejected','changes_requested')",
            name="ck_v2_construction_reviews_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approved','rejected','changes_requested')",
            name="ck_v2_construction_reviews_decision",
        ),
        sa.ForeignKeyConstraint(["draft_id"], ["v2_construction_drafts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dataset_id"], ["v2_datasets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dataset_version_id"], ["v2_dataset_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["base_revision_id"], ["ontology_revisions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["mapping_task_id"], ["v2_mapping_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["construction_run_id"], ["v2_construction_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_v2_construction_reviews_status_created",
        "v2_construction_reviews", ["status", "created_at"], unique=False,
    )
    op.create_index(
        "ix_v2_construction_reviews_draft_created",
        "v2_construction_reviews", ["draft_id", "created_at"], unique=False,
    )
    op.create_index(
        "ix_v2_construction_reviews_dataset",
        "v2_construction_reviews", ["dataset_id"], unique=False,
    )

    op.create_table(
        "v2_construction_review_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("review_id", sa.String(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["review_id"], ["v2_construction_reviews.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("sequence_no >= 1", name="ck_v2_construction_review_event_sequence_positive"),
        sa.CheckConstraint(
            "event_type IN ('submitted','approved','rejected','changes_requested','dispatch_failed','materialized')",
            name="ck_v2_construction_review_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review','approved','rejected','changes_requested')",
            name="ck_v2_construction_review_event_status",
        ),
        sa.UniqueConstraint("review_id", "sequence_no", name="uq_v2_construction_review_event_sequence"),
    )
    op.create_index(
        "ix_v2_construction_review_events_review",
        "v2_construction_review_events", ["review_id", "sequence_no"], unique=False,
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_construction_review_event_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'Construction review events are append-only'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            "CREATE TRIGGER trg_construction_review_event_no_update_delete "
            "BEFORE UPDATE OR DELETE ON v2_construction_review_events FOR EACH ROW "
            "EXECUTE FUNCTION reject_construction_review_event_mutation()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            "CREATE TRIGGER trg_construction_review_event_no_update "
            "BEFORE UPDATE ON v2_construction_review_events "
            "BEGIN SELECT RAISE(ABORT, 'Construction review events are append-only'); END"
        ))
        op.execute(sa.text(
            "CREATE TRIGGER trg_construction_review_event_no_delete "
            "BEFORE DELETE ON v2_construction_review_events "
            "BEGIN SELECT RAISE(ABORT, 'Construction review events are append-only'); END"
        ))


def downgrade():
    bind = op.get_bind()
    for table in ("v2_construction_review_events", "v2_construction_reviews"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                f"{table} contains review evidence; downgrade would erase review history"
            )

    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "DROP TRIGGER trg_construction_review_event_no_update_delete "
            "ON v2_construction_review_events"
        ))
        op.execute(sa.text("DROP FUNCTION reject_construction_review_event_mutation()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_construction_review_event_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_construction_review_event_no_delete"))

    op.drop_index("ix_v2_construction_review_events_review", table_name="v2_construction_review_events")
    op.drop_table("v2_construction_review_events")
    op.drop_index("ix_v2_construction_reviews_dataset", table_name="v2_construction_reviews")
    op.drop_index("ix_v2_construction_reviews_draft_created", table_name="v2_construction_reviews")
    op.drop_index("ix_v2_construction_reviews_status_created", table_name="v2_construction_reviews")
    op.drop_table("v2_construction_reviews")
