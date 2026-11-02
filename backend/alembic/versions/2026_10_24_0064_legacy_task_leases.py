"""Add fenced leases for legacy audit and extraction workers."""
from alembic import op
import sqlalchemy as sa


revision = "0064_legacy_task_leases"
down_revision = "0063_decision_lab_index_contract"
branch_labels = None
depends_on = None


def _add_lease_columns(table: str) -> None:
    op.add_column(table, sa.Column("worker_task_id", sa.String(length=200), nullable=True))
    op.add_column(
        table,
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(table, sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(table, sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(f"ix_{table}_lease_expires_at", table, ["lease_expires_at"])


def upgrade():
    _add_lease_columns("audit_tasks")
    _add_lease_columns("extraction_tasks")


def _has_lease_history(table: str) -> bool:
    return op.get_bind().execute(sa.text(
        f"SELECT 1 FROM {table} WHERE lease_generation > 0 "
        "OR worker_task_id IS NOT NULL OR heartbeat_at IS NOT NULL "
        "OR lease_expires_at IS NOT NULL LIMIT 1"
    )).first() is not None


def downgrade():
    for table in ("audit_tasks", "extraction_tasks"):
        if _has_lease_history(table):
            raise RuntimeError(
                f"{table} contains worker lease history; downgrade would discard execution fencing"
            )
    for table in ("extraction_tasks", "audit_tasks"):
        op.drop_index(f"ix_{table}_lease_expires_at", table_name=table)
        op.drop_column(table, "lease_expires_at")
        op.drop_column(table, "heartbeat_at")
        op.drop_column(table, "lease_generation")
        op.drop_column(table, "worker_task_id")
