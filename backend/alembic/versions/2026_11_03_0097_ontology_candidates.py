"""Add AI ontology candidate sets and their reviewable items."""
from alembic import op
import sqlalchemy as sa


revision = "0097_ontology_candidates"
down_revision = "0096_merge_main_plan_a_v4"
branch_labels = None
depends_on = None


SETS = "v2_ontology_candidate_sets"
ITEMS = "v2_ontology_candidate_items"


def upgrade():
    op.create_table(
        SETS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("ontology_id", sa.String(), sa.ForeignKey("ontology_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("generator", sa.JSON(), nullable=False),
        sa.Column("runner_run_id", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("document_digest", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("published_revision_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("published_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('generating','review','published','failed','cancelled')", name="ck_v2_ontology_candidate_set_status"),
        sa.CheckConstraint("origin IN ('runner','file','example')", name="ck_v2_ontology_candidate_set_origin"),
    )
    op.create_index("ix_v2_ontology_candidate_sets_ontology", SETS, ["ontology_id", "created_at"])

    op.create_table(
        ITEMS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("set_id", sa.String(length=36), sa.ForeignKey(f"{SETS}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=300), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("basis", sa.String(length=24), nullable=False),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("final", sa.JSON(), nullable=True),
        sa.Column("decision", sa.String(length=12), nullable=False),
        sa.Column("reject_category", sa.String(length=24), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("exists_in_ontology", sa.Boolean(), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("reviewed_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_result", sa.JSON(), nullable=True),
        sa.UniqueConstraint("set_id", "key", name="uq_v2_ontology_candidate_item_key"),
        sa.CheckConstraint("kind IN ('entity_type','relationship')", name="ck_v2_ontology_candidate_item_kind"),
        sa.CheckConstraint("decision IN ('pending','accepted','modified','rejected')", name="ck_v2_ontology_candidate_item_decision"),
    )
    op.create_index("ix_v2_ontology_candidate_items_set", ITEMS, ["set_id", "ordinal"])


def downgrade():
    op.drop_index("ix_v2_ontology_candidate_items_set", table_name=ITEMS)
    op.drop_table(ITEMS)
    op.drop_index("ix_v2_ontology_candidate_sets_ontology", table_name=SETS)
    op.drop_table(SETS)
