"""Add durable read-only grants for Agent sessions."""
from alembic import op
import sqlalchemy as sa


revision = "0056_agent_session_participants"
down_revision = "0055_construction_dataset_pins"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "v2_agent_session_participants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id", sa.String(36),
            sa.ForeignKey("v2_agent_sessions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", sa.String(),
            sa.ForeignKey("users.id"), nullable=False,
        ),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column(
            "granted_by", sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_id", "user_id", name="uq_agent_session_participant"),
        sa.CheckConstraint("role = 'viewer'", name="ck_agent_session_participant_role"),
    )
    op.create_index(
        "ix_v2_agent_session_participants_session_id",
        "v2_agent_session_participants", ["session_id"],
    )
    op.create_index(
        "ix_v2_agent_session_participants_user_id",
        "v2_agent_session_participants", ["user_id"],
    )


def downgrade():
    has_participant_history = op.get_bind().execute(sa.text(
        "SELECT 1 FROM v2_agent_session_participants LIMIT 1"
    )).first()
    if has_participant_history:
        raise RuntimeError(
            "Agent session participant history exists; downgrade would discard access audit"
        )
    op.drop_index(
        "ix_v2_agent_session_participants_user_id", table_name="v2_agent_session_participants"
    )
    op.drop_index(
        "ix_v2_agent_session_participants_session_id", table_name="v2_agent_session_participants"
    )
    op.drop_table("v2_agent_session_participants")
