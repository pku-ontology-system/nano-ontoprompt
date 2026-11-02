"""Link Pipeline model calls to pinned inputs and preserve attempt fingerprints."""
from alembic import op
import sqlalchemy as sa


revision = "0078_pipeline_llm_attempt_provenance"
down_revision = "0077_temporal_graph_projection_rebuilds"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("v2_model_invocations") as batch_op:
        batch_op.add_column(sa.Column("pipeline_run_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("source_dataset_version_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("attempt_number", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("model_config_id", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("model_config_digest", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_v2_model_invocations_pipeline_run_id",
            "v2_pipeline_runs",
            ["pipeline_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_v2_model_invocations_pipeline_run_id",
            ["pipeline_run_id"],
        )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_model_invocations WHERE pipeline_run_id IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Pipeline LLM attempt history exists; downgrade would erase its Run lineage")
    with op.batch_alter_table("v2_model_invocations") as batch_op:
        batch_op.drop_index("ix_v2_model_invocations_pipeline_run_id")
        batch_op.drop_constraint("fk_v2_model_invocations_pipeline_run_id", type_="foreignkey")
        batch_op.drop_column("model_config_digest")
        batch_op.drop_column("model_config_id")
        batch_op.drop_column("attempt_number")
        batch_op.drop_column("source_dataset_version_id")
        batch_op.drop_column("pipeline_run_id")
