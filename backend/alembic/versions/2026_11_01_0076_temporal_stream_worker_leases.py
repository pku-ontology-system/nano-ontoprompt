"""Persist temporal stream worker leases and projection attempts."""
from alembic import op
import sqlalchemy as sa


revision = "0076_temporal_stream_worker_leases"
down_revision = "0075_temporal_snapshot_publish_cas"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v2_temporal_replays", sa.Column("worker_generation", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("v2_temporal_replays", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_temporal_replays", sa.Column("worker_lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_temporal_replays", sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("v2_temporal_replays", sa.Column("dispatch_lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_temporal_replays", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_temporal_stream_events", sa.Column("retry_generation", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("v2_temporal_stream_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_temporal_stream_events", sa.Column("graph_projection_status", sa.String(length=20), nullable=True))
    op.add_column("v2_temporal_stream_events", sa.Column("graph_projected_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE v2_temporal_stream_events SET graph_projection_status = 'unknown' WHERE graph_projection_status IS NULL"))

    op.create_table(
        "v2_temporal_projection_attempts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("replay_id", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("retry_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("attempt_type", sa.String(length=20), nullable=False, server_default="automatic"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="started"),
        sa.Column("worker_token", sa.String(length=80), nullable=True),
        sa.Column("request_key", sa.String(length=120), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_type IN ('automatic','manual')", name="ck_v2_temporal_projection_attempt_type"),
        sa.CheckConstraint("status IN ('authorized','started','committed','failed','abandoned')", name="ck_v2_temporal_projection_attempt_status"),
        sa.ForeignKeyConstraint(["event_id"], ["v2_temporal_stream_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["replay_id"], ["v2_temporal_replays.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "attempt_no", name="uq_v2_temporal_projection_attempt_no"),
        sa.UniqueConstraint("replay_id", "request_key", name="uq_v2_temporal_projection_request_key"),
    )
    op.create_index("ix_v2_temporal_projection_attempts_replay", "v2_temporal_projection_attempts", ["replay_id", "created_at"])
    op.create_index("ix_v2_temporal_projection_attempts_event", "v2_temporal_projection_attempts", ["event_id", "attempt_no"])
    op.create_index("ix_v2_temporal_replays_dispatch", "v2_temporal_replays", ["status", "next_attempt_at", "dispatch_lease_until"])
    op.create_index("ix_v2_temporal_stream_events_retry", "v2_temporal_stream_events", ["replay_id", "status", "next_attempt_at"])
    op.create_index("ix_v2_temporal_stream_events_graph_projection", "v2_temporal_stream_events", ["replay_id", "graph_projection_status"])


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_temporal_projection_attempts LIMIT 1"
    )).first():
        raise RuntimeError("Temporal projection attempts have audit history; downgrade would erase it")
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_temporal_replays "
        "WHERE worker_generation > 0 OR dispatch_attempts > 0 LIMIT 1"
    )).first():
        raise RuntimeError("Temporal stream workers have used durable leases; downgrade would erase their fencing state")
    if bind.execute(sa.text(
        "SELECT 1 FROM v2_temporal_stream_events "
        "WHERE graph_projection_status IN ('pending','projected') OR graph_projected_at IS NOT NULL LIMIT 1"
    )).first():
        raise RuntimeError("Temporal graph projection status has been recorded; downgrade would erase it")
    op.drop_index("ix_v2_temporal_stream_events_graph_projection", table_name="v2_temporal_stream_events")
    op.drop_index("ix_v2_temporal_stream_events_retry", table_name="v2_temporal_stream_events")
    op.drop_index("ix_v2_temporal_replays_dispatch", table_name="v2_temporal_replays")
    op.drop_index("ix_v2_temporal_projection_attempts_event", table_name="v2_temporal_projection_attempts")
    op.drop_index("ix_v2_temporal_projection_attempts_replay", table_name="v2_temporal_projection_attempts")
    op.drop_table("v2_temporal_projection_attempts")
    op.drop_column("v2_temporal_stream_events", "next_attempt_at")
    op.drop_column("v2_temporal_stream_events", "retry_generation")
    op.drop_column("v2_temporal_stream_events", "graph_projected_at")
    op.drop_column("v2_temporal_stream_events", "graph_projection_status")
    op.drop_column("v2_temporal_replays", "next_attempt_at")
    op.drop_column("v2_temporal_replays", "dispatch_lease_until")
    op.drop_column("v2_temporal_replays", "dispatch_attempts")
    op.drop_column("v2_temporal_replays", "worker_lease_expires_at")
    op.drop_column("v2_temporal_replays", "heartbeat_at")
    op.drop_column("v2_temporal_replays", "worker_generation")
