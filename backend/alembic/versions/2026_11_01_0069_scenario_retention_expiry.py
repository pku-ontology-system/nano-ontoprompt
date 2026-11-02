"""Persist Scenario expiry deadlines and append-only retention events."""
from datetime import timedelta, timezone

from alembic import op
import sqlalchemy as sa


revision = "0069_scenario_retention_expiry"
down_revision = "0068_temporal_vector_index_retention"
branch_labels = None
depends_on = None


SCENARIOS = "v2_scenarios"
EVENTS = "v2_scenario_retention_events"


def upgrade():
    op.add_column(SCENARIOS, sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(SCENARIOS, sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_v2_scenarios_expires_at", SCENARIOS, ["expires_at"])

    bind = op.get_bind()
    scenario_table = sa.table(
        SCENARIOS,
        sa.column("id", sa.String(36)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("ttl_seconds", sa.Integer()),
        sa.column("expires_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(sa.select(
        scenario_table.c.id, scenario_table.c.created_at, scenario_table.c.ttl_seconds,
    ).where(scenario_table.c.ttl_seconds.is_not(None))).mappings().all()
    for row in rows:
        created_at = row["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        bind.execute(scenario_table.update().where(scenario_table.c.id == row["id"]).values(
            expires_at=created_at + timedelta(seconds=row["ttl_seconds"]),
        ))

    op.create_table(
        EVENTS,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scenario_id", sa.String(36),
                  sa.ForeignKey(f"{SCENARIOS}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scenario_id", "event_type", name="uq_v2_scenario_retention_event"),
        sa.CheckConstraint("event_type IN ('expired')", name="ck_v2_scenario_retention_event_type"),
    )
    op.create_index("ix_v2_scenario_retention_events_scenario_id", EVENTS, ["scenario_id"])


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {EVENTS} LIMIT 1")).first():
        raise RuntimeError("Scenario retention events exist; downgrade would erase lifecycle audit")

    scenario_table = sa.table(
        SCENARIOS,
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("ttl_seconds", sa.Integer()),
        sa.column("expires_at", sa.DateTime(timezone=True)),
    )
    scenarios = bind.execute(sa.select(
        scenario_table.c.created_at, scenario_table.c.ttl_seconds, scenario_table.c.expires_at,
    ).where(scenario_table.c.expires_at.is_not(None))).mappings().all()
    for row in scenarios:
        created_at = row["created_at"]
        expires_at = row["expires_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expected = (created_at + timedelta(seconds=row["ttl_seconds"])) if row["ttl_seconds"] else None
        if expected != expires_at:
            raise RuntimeError("Scenario expiry deadlines were edited; downgrade would erase retention settings")

    op.drop_index("ix_v2_scenario_retention_events_scenario_id", table_name=EVENTS)
    op.drop_table(EVENTS)
    op.drop_index("ix_v2_scenarios_expires_at", table_name=SCENARIOS)
    op.drop_column(SCENARIOS, "expired_at")
    op.drop_column(SCENARIOS, "expires_at")
