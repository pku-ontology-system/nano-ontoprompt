"""Freeze and independently review Ontology Link Mapping definitions."""
from alembic import op
import sqlalchemy as sa


revision = "0086_mapping_link_definition_review"
down_revision = "0085_mapping_apply_attempt_events"
branch_labels = None
depends_on = None


REVIEWS = "v2_mapping_link_definition_reviews"
EVENTS = "v2_mapping_link_definition_review_events"


def upgrade():
    op.create_table(
        REVIEWS,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("link_mapping_id", sa.String(), nullable=False),
        sa.Column("ontology_id", sa.String(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("candidate_json", sa.JSON(), nullable=False),
        sa.Column("candidate_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending_review", nullable=False),
        sa.Column("submitted_by", sa.String(length=120), nullable=True),
        sa.Column("reviewer_id", sa.String(length=120), nullable=True),
        sa.Column("decided_by", sa.String(length=120), nullable=True),
        sa.Column("decision", sa.String(length=30), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_review','approved','rejected','changes_requested')",
            name="ck_v2_mapping_link_definition_review_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approved','rejected','changes_requested')",
            name="ck_v2_mapping_link_definition_review_decision",
        ),
        sa.CheckConstraint(
            "(status = 'pending_review' AND decision IS NULL AND decided_by IS NULL AND decided_at IS NULL) "
            "OR (status <> 'pending_review' AND decision = status AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_v2_mapping_link_definition_review_decision_state",
        ),
        sa.CheckConstraint("revision_no >= 1", name="ck_v2_mapping_link_definition_review_revision_positive"),
        sa.ForeignKeyConstraint(
            ["link_mapping_id"], ["v2_ontology_link_mappings.id"],
            name="fk_v2_mapping_link_definition_review_link_mapping", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ontology_id"], ["ontology_projects.id"],
            name="fk_v2_mapping_link_definition_review_ontology", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "link_mapping_id", "revision_no", name="uq_v2_mapping_link_definition_review_revision",
        ),
    )
    op.create_index(
        "ix_v2_mapping_link_definition_review_status_created",
        REVIEWS, ["status", "created_at"],
    )
    op.create_index(
        "ix_v2_mapping_link_definition_review_mapping",
        REVIEWS, ["link_mapping_id", "revision_no"],
    )

    op.create_table(
        EVENTS,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=120), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("sequence_no >= 1", name="ck_v2_mapping_link_review_event_sequence_positive"),
        sa.CheckConstraint("actor_kind IN ('user','system')", name="ck_v2_mapping_link_review_event_actor_kind"),
        sa.CheckConstraint(
            "event_type IN ('submitted','approved','rejected','changes_requested')",
            name="ck_v2_mapping_link_review_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review','approved','rejected','changes_requested')",
            name="ck_v2_mapping_link_review_event_status",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"], [REVIEWS + ".id"],
            name="fk_v2_mapping_link_review_event_review", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_id", "sequence_no", name="uq_v2_mapping_link_review_event_sequence"),
    )
    op.create_index(
        "ix_v2_mapping_link_review_events_review",
        EVENTS, ["review_id", "sequence_no"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION protect_mapping_link_definition_review() RETURNS trigger AS $$ "
            "BEGIN "
            "IF ROW(NEW.link_mapping_id, NEW.ontology_id, NEW.revision_no, NEW.candidate_json, "
            "NEW.candidate_digest, NEW.submitted_by, NEW.reviewer_id, NEW.created_at) "
            "IS DISTINCT FROM ROW(OLD.link_mapping_id, OLD.ontology_id, OLD.revision_no, OLD.candidate_json, "
            "OLD.candidate_digest, OLD.submitted_by, OLD.reviewer_id, OLD.created_at) THEN "
            "RAISE EXCEPTION 'Link Mapping review candidate is immutable'; END IF; "
            "IF OLD.status <> 'pending_review' AND ROW(NEW.status, NEW.decided_by, NEW.decision, "
            "NEW.decision_note, NEW.decided_at) IS DISTINCT FROM ROW(OLD.status, OLD.decided_by, "
            "OLD.decision, OLD.decision_note, OLD.decided_at) THEN "
            "RAISE EXCEPTION 'Link Mapping review decision is terminal'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_link_definition_review_protect BEFORE UPDATE ON {REVIEWS} "
            "FOR EACH ROW EXECUTE FUNCTION protect_mapping_link_definition_review()"
        ))
        op.execute(sa.text(
            "CREATE FUNCTION reject_mapping_link_review_event_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'Link Mapping review events are append-only'; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_link_review_event_no_update_delete BEFORE UPDATE OR DELETE ON {EVENTS} "
            "FOR EACH ROW EXECUTE FUNCTION reject_mapping_link_review_event_mutation()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_link_review_candidate_immutable BEFORE UPDATE ON {REVIEWS} "
            "WHEN OLD.link_mapping_id IS NOT NEW.link_mapping_id OR OLD.ontology_id IS NOT NEW.ontology_id "
            "OR OLD.revision_no IS NOT NEW.revision_no OR OLD.candidate_json IS NOT NEW.candidate_json "
            "OR OLD.candidate_digest IS NOT NEW.candidate_digest OR OLD.submitted_by IS NOT NEW.submitted_by "
            "OR OLD.reviewer_id IS NOT NEW.reviewer_id OR OLD.created_at IS NOT NEW.created_at "
            "BEGIN SELECT RAISE(ABORT, 'Link Mapping review candidate is immutable'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_link_review_decision_terminal BEFORE UPDATE ON {REVIEWS} "
            "WHEN OLD.status <> 'pending_review' AND (OLD.status IS NOT NEW.status "
            "OR OLD.decided_by IS NOT NEW.decided_by OR OLD.decision IS NOT NEW.decision "
            "OR OLD.decision_note IS NOT NEW.decision_note OR OLD.decided_at IS NOT NEW.decided_at) "
            "BEGIN SELECT RAISE(ABORT, 'Link Mapping review decision is terminal'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_link_review_event_no_update BEFORE UPDATE ON {EVENTS} "
            "BEGIN SELECT RAISE(ABORT, 'Link Mapping review events are append-only'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_mapping_link_review_event_no_delete BEFORE DELETE ON {EVENTS} "
            "BEGIN SELECT RAISE(ABORT, 'Link Mapping review events are append-only'); END"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {EVENTS} LIMIT 1")).first():
        raise RuntimeError("Link Mapping review events exist; downgrade would erase audit history")
    if bind.execute(sa.text(f"SELECT 1 FROM {REVIEWS} LIMIT 1")).first():
        raise RuntimeError("Link Mapping reviews exist; downgrade would erase approved definitions")

    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER trg_mapping_link_review_event_no_update_delete ON {EVENTS}"))
        op.execute(sa.text("DROP FUNCTION reject_mapping_link_review_event_mutation()"))
        op.execute(sa.text(f"DROP TRIGGER trg_mapping_link_definition_review_protect ON {REVIEWS}"))
        op.execute(sa.text("DROP FUNCTION protect_mapping_link_definition_review()"))
    elif dialect == "sqlite":
        op.execute(sa.text("DROP TRIGGER trg_mapping_link_review_event_no_delete"))
        op.execute(sa.text("DROP TRIGGER trg_mapping_link_review_event_no_update"))
        op.execute(sa.text("DROP TRIGGER trg_mapping_link_review_decision_terminal"))
        op.execute(sa.text("DROP TRIGGER trg_mapping_link_review_candidate_immutable"))

    op.drop_index("ix_v2_mapping_link_review_events_review", table_name=EVENTS)
    op.drop_table(EVENTS)
    op.drop_index("ix_v2_mapping_link_definition_review_mapping", table_name=REVIEWS)
    op.drop_index("ix_v2_mapping_link_definition_review_status_created", table_name=REVIEWS)
    op.drop_table(REVIEWS)
