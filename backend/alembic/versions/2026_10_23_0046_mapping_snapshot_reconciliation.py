"""Track structured Mapping row ownership, tombstones, and graph projection."""
from alembic import op
import sqlalchemy as sa


revision = "0046_mapping_snapshot_reconciliation"
down_revision = "0045_temporal_stream_tombstones"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "entities",
        sa.Column("is_source_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "entity_instances",
        sa.Column("source_mapping_id", sa.String(), nullable=True),
    )
    op.add_column(
        "entity_instances",
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "entity_instances",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_entity_instances_source_mapping",
        "entity_instances",
        "v2_ontology_mappings",
        ["source_mapping_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_entity_instances_source_mapping_id", "entity_instances", ["source_mapping_id"])

    # Backfill only when a source Object Type has exactly one Mapping.  If
    # multiple mappings own one type, the historical rows cannot be safely
    # attributed and are intentionally left unassigned for an explicit repair.
    op.execute(sa.text("""
        WITH mapping_candidates AS (
            SELECT
                id,
                ontology_id,
                entity_class,
                md5(ontology_id || chr(58) || entity_class || chr(58) || 'concept') AS concept_hash,
                count(*) OVER (PARTITION BY ontology_id, entity_class) AS mapping_count
            FROM v2_ontology_mappings
        ), unique_mappings AS (
            SELECT
                id,
                ontology_id,
                substr(concept_hash, 1, 8) || '-' || substr(concept_hash, 9, 4) || '-' ||
                substr(concept_hash, 13, 4) || '-' || substr(concept_hash, 17, 4) || '-' ||
                substr(concept_hash, 21, 12) AS concept_id
            FROM mapping_candidates
            WHERE mapping_count = 1
        )
        UPDATE entity_instances AS instance
        SET source_mapping_id = mapping.id
        FROM unique_mappings AS mapping
        WHERE instance.ontology_id = mapping.ontology_id
          AND instance.entity_id = mapping.concept_id
          AND instance.source_version IS NOT NULL
          AND instance.source_mapping_id IS NULL
          AND instance.is_deleted = false
    """))

    op.create_table(
        "v2_mapping_row_changes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("mapping_apply_run_id", sa.String(length=36), nullable=False),
        sa.Column("mapping_id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("row_identity", sa.String(length=200), nullable=False),
        sa.Column("dataset_version_id", sa.String(length=120), nullable=False),
        sa.Column("operation", sa.String(length=10), nullable=False),
        sa.Column("before_row_data", sa.JSON(), nullable=True),
        sa.Column("after_row_data", sa.JSON(), nullable=True),
        sa.Column("projection_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("projection_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("projection_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projection_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projection_projected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("projection_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("operation IN ('upsert', 'delete')", name="ck_mapping_row_change_operation"),
        sa.CheckConstraint("projection_status IN ('pending', 'projected')", name="ck_mapping_row_change_projection_status"),
        sa.ForeignKeyConstraint(["mapping_apply_run_id"], ["v2_mapping_apply_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mapping_id"], ["v2_ontology_mappings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ontology_id"], ["ontology_projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mapping_apply_run_id", "object_id", name="uq_mapping_row_change_run_object"),
    )
    op.create_index("ix_v2_mapping_row_changes_object_id", "v2_mapping_row_changes", ["object_id"])
    op.create_index(
        "ix_mapping_row_change_projection_due",
        "v2_mapping_row_changes",
        ["projection_status", "projection_next_attempt_at"],
    )
    op.create_index(
        "ix_mapping_row_change_mapping_created",
        "v2_mapping_row_changes",
        ["mapping_id", "created_at"],
    )


def downgrade():
    bind = op.get_bind()
    has_changes = bind.execute(sa.text("SELECT 1 FROM v2_mapping_row_changes LIMIT 1")).first()
    has_tombstones = bind.execute(sa.text(
        "SELECT 1 FROM entity_instances WHERE is_deleted = true LIMIT 1"
    )).first()
    has_mapping_ownership = bind.execute(sa.text(
        "SELECT 1 FROM entity_instances WHERE source_mapping_id IS NOT NULL LIMIT 1"
    )).first()
    has_source_deleted_entities = bind.execute(sa.text(
        "SELECT 1 FROM entities WHERE is_source_deleted = true LIMIT 1"
    )).first()
    if has_changes or has_tombstones or has_mapping_ownership or has_source_deleted_entities:
        raise RuntimeError(
            "Mapping row changes, source ownership, or source-deletion markers exist; "
            "downgrade would discard source history"
        )
    op.drop_index("ix_mapping_row_change_mapping_created", table_name="v2_mapping_row_changes")
    op.drop_index("ix_mapping_row_change_projection_due", table_name="v2_mapping_row_changes")
    op.drop_index("ix_v2_mapping_row_changes_object_id", table_name="v2_mapping_row_changes")
    op.drop_table("v2_mapping_row_changes")
    op.drop_index("ix_entity_instances_source_mapping_id", table_name="entity_instances")
    op.drop_constraint("fk_entity_instances_source_mapping", "entity_instances", type_="foreignkey")
    op.drop_column("entity_instances", "deleted_at")
    op.drop_column("entity_instances", "is_deleted")
    op.drop_column("entity_instances", "source_mapping_id")
    op.drop_column("entities", "is_source_deleted")
