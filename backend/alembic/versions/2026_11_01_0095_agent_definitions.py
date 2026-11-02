"""Add versioned Agent Definitions and session version pins."""
from alembic import op
import sqlalchemy as sa


revision = "0095_agent_definitions"
down_revision = "0094_service_principal_registry"
branch_labels = None
depends_on = None


DEFINITIONS = "v2_agent_definitions"
VERSIONS = "v2_agent_definition_versions"
EVENTS = "v2_agent_definition_events"
SESSIONS = "v2_agent_sessions"


def upgrade():
    op.create_table(
        DEFINITIONS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("ontology_id", sa.String(), sa.ForeignKey("ontology_projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=True),
        sa.Column("draft_config", sa.JSON(), nullable=False),
        sa.Column("draft_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('draft','published','withdrawn')", name="ck_v2_agent_definition_status"),
        sa.CheckConstraint("draft_revision >= 1", name="ck_v2_agent_definition_draft_revision"),
        sa.CheckConstraint("published_version IS NULL OR published_version >= 1", name="ck_v2_agent_definition_published_version"),
    )
    op.create_index("ix_v2_agent_definitions_ontology_status", DEFINITIONS, ["ontology_id", "status"])
    op.create_index("ix_v2_agent_definitions_owner", DEFINITIONS, ["owner_id", "updated_at"])

    op.create_table(
        VERSIONS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("definition_id", sa.String(length=36), sa.ForeignKey(f"{DEFINITIONS}.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("published_by", sa.String(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("definition_id", "version", name="uq_v2_agent_definition_version"),
        sa.CheckConstraint("version >= 1", name="ck_v2_agent_definition_version_no"),
    )
    op.create_index("ix_v2_agent_definition_versions_published", VERSIONS, ["definition_id", "published_at"])

    op.create_table(
        EVENTS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("definition_id", sa.String(length=36), sa.ForeignKey(f"{DEFINITIONS}.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("definition_id", "sequence", name="uq_v2_agent_definition_event_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_v2_agent_definition_event_sequence"),
        sa.CheckConstraint(
            "event_type IN ('definition_created','draft_updated','published','withdrawn','reactivated')",
            name="ck_v2_agent_definition_event_type",
        ),
    )
    op.create_index("ix_v2_agent_definition_events_definition", EVENTS, ["definition_id", "created_at"])

    with op.batch_alter_table(SESSIONS) as batch:
        batch.add_column(sa.Column("agent_definition_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("agent_definition_version_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("agent_definition_digest", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_v2_agent_sessions_definition_id", DEFINITIONS,
            ["agent_definition_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_v2_agent_sessions_definition_version_id", VERSIONS,
            ["agent_definition_version_id"], ["id"], ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_v2_agent_session_definition_pin",
            "(agent_definition_id IS NULL AND agent_definition_version_id IS NULL AND agent_definition_digest IS NULL) "
            "OR (agent_definition_id IS NOT NULL AND agent_definition_version_id IS NOT NULL AND agent_definition_digest IS NOT NULL)",
        )
    op.create_index("ix_agent_sessions_definition", SESSIONS, ["agent_definition_id", "agent_definition_version_id"])

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION reject_agent_definition_immutable_mutation() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'Published Agent Definition history is immutable'; END; "
            "$$ LANGUAGE plpgsql"
        ))
        for table, stem in ((VERSIONS, "agent_definition_version"), (EVENTS, "agent_definition_event")):
            op.execute(sa.text(
                f"CREATE TRIGGER trg_{stem}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_agent_definition_immutable_mutation()"
            ))
        op.execute(sa.text(
            "CREATE FUNCTION validate_agent_definition_event_chain() RETURNS trigger AS $$ "
            "DECLARE prior_sequence integer; prior_digest varchar(64); BEGIN "
            f"SELECT sequence, event_digest INTO prior_sequence, prior_digest FROM {EVENTS} "
            "WHERE definition_id = NEW.definition_id ORDER BY sequence DESC LIMIT 1; "
            "IF NEW.sequence <> COALESCE(prior_sequence, 0) + 1 OR "
            "NEW.previous_digest IS DISTINCT FROM prior_digest THEN "
            "RAISE EXCEPTION 'Agent Definition event chain is not contiguous'; END IF; RETURN NEW; END; "
            "$$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_agent_definition_event_chain BEFORE INSERT ON {EVENTS} "
            "FOR EACH ROW EXECUTE FUNCTION validate_agent_definition_event_chain()"
        ))
    elif dialect == "sqlite":
        for table, stem in ((VERSIONS, "agent_definition_version"), (EVENTS, "agent_definition_event")):
            op.execute(sa.text(
                f"CREATE TRIGGER trg_{stem}_no_update BEFORE UPDATE ON {table} BEGIN "
                "SELECT RAISE(ABORT, 'Published Agent Definition history is immutable'); END"
            ))
            op.execute(sa.text(
                f"CREATE TRIGGER trg_{stem}_no_delete BEFORE DELETE ON {table} BEGIN "
                "SELECT RAISE(ABORT, 'Published Agent Definition history is immutable'); END"
            ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_agent_definition_event_chain BEFORE INSERT ON {EVENTS} BEGIN "
            f"SELECT CASE WHEN NEW.sequence <> COALESCE((SELECT MAX(sequence) + 1 FROM {EVENTS} "
            "WHERE definition_id = NEW.definition_id), 1) THEN "
            "RAISE(ABORT, 'Agent Definition event chain is not contiguous') END; "
            f"SELECT CASE WHEN NEW.previous_digest IS NOT (SELECT event_digest FROM {EVENTS} "
            "WHERE definition_id = NEW.definition_id ORDER BY sequence DESC LIMIT 1) THEN "
            "RAISE(ABORT, 'Agent Definition event chain digest does not match') END; END"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {SESSIONS} WHERE agent_definition_id IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Agent sessions pin Definition versions; downgrade would erase session provenance")
    if bind.execute(sa.text(f"SELECT 1 FROM {EVENTS} LIMIT 1")).first():
        raise RuntimeError("Agent Definition lifecycle history exists; downgrade would erase audit evidence")
    if bind.execute(sa.text(f"SELECT 1 FROM {VERSIONS} LIMIT 1")).first():
        raise RuntimeError("Published Agent Definition versions exist; downgrade would erase immutable versions")
    if bind.execute(sa.text(f"SELECT 1 FROM {DEFINITIONS} LIMIT 1")).first():
        raise RuntimeError("Agent Definitions exist; downgrade would erase authored configurations")

    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER trg_agent_definition_event_chain ON {EVENTS}"))
        for table, stem in ((VERSIONS, "agent_definition_version"), (EVENTS, "agent_definition_event")):
            op.execute(sa.text(f"DROP TRIGGER trg_{stem}_no_update_delete ON {table}"))
        op.execute(sa.text("DROP FUNCTION validate_agent_definition_event_chain()"))
        op.execute(sa.text("DROP FUNCTION reject_agent_definition_immutable_mutation()"))
    elif dialect == "sqlite":
        for trigger in (
            "trg_agent_definition_event_chain",
            "trg_agent_definition_version_no_update", "trg_agent_definition_version_no_delete",
            "trg_agent_definition_event_no_update", "trg_agent_definition_event_no_delete",
        ):
            op.execute(sa.text(f"DROP TRIGGER {trigger}"))

    op.drop_index("ix_agent_sessions_definition", table_name=SESSIONS)
    with op.batch_alter_table(SESSIONS) as batch:
        batch.drop_constraint("ck_v2_agent_session_definition_pin", type_="check")
        batch.drop_constraint("fk_v2_agent_sessions_definition_version_id", type_="foreignkey")
        batch.drop_constraint("fk_v2_agent_sessions_definition_id", type_="foreignkey")
        batch.drop_column("agent_definition_digest")
        batch.drop_column("agent_definition_version_id")
        batch.drop_column("agent_definition_id")
    op.drop_index("ix_v2_agent_definition_events_definition", table_name=EVENTS)
    op.drop_table(EVENTS)
    op.drop_index("ix_v2_agent_definition_versions_published", table_name=VERSIONS)
    op.drop_table(VERSIONS)
    op.drop_index("ix_v2_agent_definitions_owner", table_name=DEFINITIONS)
    op.drop_index("ix_v2_agent_definitions_ontology_status", table_name=DEFINITIONS)
    op.drop_table(DEFINITIONS)
