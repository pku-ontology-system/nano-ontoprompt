"""Scope Case request keys by ActionType, Context and principal."""
import hashlib
import json

from alembic import op
import sqlalchemy as sa

revision = "0039_action_request_scope"
down_revision = "0038_merge_decision_cases"
branch_labels = None
depends_on = None


def _digest(value):
    raw = json.dumps(value or {}, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def upgrade():
    bind = op.get_bind()
    op.add_column("v2_decision_cases", sa.Column("context_digest", sa.String(64), nullable=True))
    rows = bind.execute(sa.text("SELECT id, context_json FROM v2_decision_cases")).all()
    for case_id, context_json in rows:
        bind.execute(sa.text("UPDATE v2_decision_cases SET context_digest = :digest WHERE id = :id"),
                     {"digest": _digest(context_json), "id": case_id})
    op.alter_column("v2_decision_cases", "context_digest", nullable=False)
    op.drop_constraint("uq_decision_case_request", "v2_decision_cases", type_="unique")
    op.create_unique_constraint("uq_decision_case_request_scope", "v2_decision_cases",
        ["ontology_id", "created_by", "action_type_id", "context_digest", "client_request_id"])


def downgrade():
    bind = op.get_bind()
    collision = bind.execute(sa.text("""
        SELECT 1 FROM v2_decision_cases
        GROUP BY ontology_id, created_by, client_request_id
        HAVING count(*) > 1 LIMIT 1
    """)).first()
    if collision:
        raise RuntimeError("Downgrade would collapse distinct Action/Context request keys")
    op.drop_constraint("uq_decision_case_request_scope", "v2_decision_cases", type_="unique")
    op.create_unique_constraint("uq_decision_case_request", "v2_decision_cases",
        ["ontology_id", "created_by", "client_request_id"])
    op.drop_column("v2_decision_cases", "context_digest")
