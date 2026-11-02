"""Register service principals, exact grants, and scheduled run identity."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "0094_service_principal_registry"
down_revision = "0093_pipeline_schedules"
branch_labels = None
depends_on = None

PRINCIPALS = "v2_service_principals"
GRANTS = "v2_service_principal_grants"
EVENTS = "v2_service_principal_audit_events"
SCHEDULES = "v2_pipeline_schedules"
RUNS = "v2_pipeline_runs"
BUILTIN_ID = "system:pipeline-scheduler"


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _audit(connection, events, principal_id: str, sequence: int, event_type: str, actor_id: str | None,
           grant_id: str | None, reason: str | None, payload: dict, now: datetime) -> None:
    previous_digest = connection.execute(sa.select(events.c.event_digest).where(
        events.c.service_principal_id == principal_id,
    ).order_by(events.c.sequence.desc()).limit(1)).scalar()
    envelope = {
        "service_principal_id": principal_id,
        "sequence": sequence,
        "event_type": event_type,
        "actor_user_id": actor_id,
        "grant_id": grant_id,
        "reason": reason,
        "payload": payload,
        "previous_digest": previous_digest,
    }
    connection.execute(events.insert().values(
        id=str(uuid.uuid4()),
        service_principal_id=principal_id,
        sequence=sequence,
        event_type=event_type,
        actor_user_id=actor_id,
        grant_id=grant_id,
        reason=reason,
        payload=payload,
        previous_digest=previous_digest,
        event_digest=_digest(envelope),
        created_at=now,
    ))


def upgrade():
    op.create_table(
        PRINCIPALS,
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("owner_user_id", sa.String(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('service','system')", name="ck_v2_service_principal_kind"),
        sa.CheckConstraint("status IN ('active','suspended')", name="ck_v2_service_principal_status"),
        sa.CheckConstraint("revision >= 1", name="ck_v2_service_principal_revision"),
    )
    op.create_index("ix_v2_service_principals_status_kind", PRINCIPALS, ["status", "kind"])

    op.create_table(
        GRANTS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("service_principal_id", sa.String(length=100), sa.ForeignKey(f"{PRINCIPALS}.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=False),
        sa.Column("resource_id", sa.String(length=200), nullable=False),
        sa.Column("permission", sa.String(length=40), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("revoked_by", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("resource_type <> ''", name="ck_v2_service_principal_grant_resource_type"),
        sa.CheckConstraint("resource_id <> ''", name="ck_v2_service_principal_grant_resource_id"),
        sa.CheckConstraint("permission <> ''", name="ck_v2_service_principal_grant_permission"),
    )
    op.create_index(
        "ix_v2_service_principal_grants_lookup", GRANTS,
        ["service_principal_id", "resource_type", "resource_id", "permission", "active"],
    )

    op.create_table(
        EVENTS,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("service_principal_id", sa.String(length=100), sa.ForeignKey(f"{PRINCIPALS}.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("actor_user_id", sa.String(), nullable=True),
        sa.Column("grant_id", sa.String(length=36), sa.ForeignKey(f"{GRANTS}.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("previous_digest", sa.String(length=64), nullable=True),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("service_principal_id", "sequence", name="uq_v2_service_principal_event_sequence"),
    )
    op.create_index("ix_v2_service_principal_events_principal", EVENTS, ["service_principal_id", "created_at"])

    # Preserve existing schedules while replacing the former free-form actor
    # string with a real, stable registry entry. Unknown legacy names are
    # retained as suspended system principals and therefore fail closed.
    connection = op.get_bind()
    now = datetime.now(timezone.utc)
    principals = sa.table(
        PRINCIPALS,
        sa.column("id", sa.String), sa.column("kind", sa.String), sa.column("name", sa.String),
        sa.column("description", sa.Text), sa.column("status", sa.String), sa.column("builtin", sa.Boolean),
        sa.column("owner_user_id", sa.String), sa.column("created_by", sa.String), sa.column("revision", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)), sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    grants = sa.table(
        GRANTS,
        sa.column("id", sa.String), sa.column("service_principal_id", sa.String),
        sa.column("resource_type", sa.String), sa.column("resource_id", sa.String),
        sa.column("permission", sa.String), sa.column("active", sa.Boolean),
        sa.column("created_by", sa.String), sa.column("revoked_by", sa.String),
        sa.column("reason", sa.Text), sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("revoked_at", sa.DateTime(timezone=True)),
    )
    schedule_revisions = sa.table(
        "v2_pipeline_schedule_revisions",
        sa.column("id", sa.String),
        sa.column("schedule_id", sa.String),
        sa.column("revision", sa.Integer),
        sa.column("pipeline_version_id", sa.String),
        sa.column("snapshot", sa.JSON),
        sa.column("snapshot_digest", sa.String),
        sa.column("actor_id", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    events = sa.table(
        EVENTS,
        sa.column("id", sa.String), sa.column("service_principal_id", sa.String),
        sa.column("sequence", sa.Integer), sa.column("event_type", sa.String),
        sa.column("actor_user_id", sa.String), sa.column("grant_id", sa.String),
        sa.column("reason", sa.Text), sa.column("payload", sa.JSON),
        sa.column("previous_digest", sa.String), sa.column("event_digest", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    legacy_rows = connection.execute(sa.text(
        f"SELECT id, pipeline_id, service_principal_id, owner_id, steward_id, pipeline_version_id, "
        f"cron_expression, timezone, enabled, revision, misfire_policy, misfire_grace_seconds, "
        f"overlap_policy, next_fire_at, last_fire_at, created_at "
        f"FROM {SCHEDULES} ORDER BY created_at, id"
    )).mappings().all()
    legacy_ids = {str(row["service_principal_id"]) for row in legacy_rows}
    legacy_ids.add(BUILTIN_ID)
    for principal_id in sorted(legacy_ids):
        builtin = principal_id == BUILTIN_ID
        connection.execute(principals.insert().values(
            id=principal_id,
            kind="system",
            name="Pipeline 定时调度器" if builtin else f"未登记的旧调度主体 {principal_id[:40]}",
            description=(
                "内置身份，仅用于通过精确 Pipeline Run 授权运行定时 Pipeline。"
                if builtin else "从旧 Schedule 字段迁移；已暂停，需管理员重新登记和授权。"
            ),
            status="active" if builtin else "suspended",
            builtin=builtin,
            owner_user_id=None,
            created_by=None,
            revision=1,
            created_at=now,
            updated_at=now,
        ))
        payload = {"id": principal_id, "kind": "system", "builtin": builtin, "status": "active" if builtin else "suspended"}
        _audit(connection, events, principal_id, 1, "principal_registered", None, None,
               "0094 migration registry seed", payload, now)

    next_sequence = {principal_id: 1 for principal_id in legacy_ids}
    for row in legacy_rows:
        principal_id = str(row["service_principal_id"])
        if principal_id != BUILTIN_ID:
            continue
        grant_id = str(uuid.uuid4())
        created_at = _utc(row["created_at"] or now)
        actor_id = row["steward_id"] or row["owner_id"]
        payload = {
            "grant_id": grant_id,
            "resource_type": "pipeline",
            "resource_id": str(row["pipeline_id"]),
            "permission": "run",
            "active": True,
        }
        connection.execute(grants.insert().values(
            id=grant_id,
            service_principal_id=principal_id,
            resource_type="pipeline",
            resource_id=str(row["pipeline_id"]),
            permission="run",
            active=True,
            created_by=actor_id,
            revoked_by=None,
            reason="Migration 0094 preserved existing Pipeline Schedule scope",
            created_at=created_at,
            revoked_at=None,
        ))
        next_sequence[principal_id] += 1
        _audit(connection, events, principal_id, next_sequence[principal_id], "grant_created",
               actor_id, grant_id, "Migration 0094 preserved existing Pipeline Schedule scope", payload, created_at)

    # Unknown legacy values cannot be proven safe. Suspend those principals
    # and disable their schedules instead of allowing an unregistered actor to
    # continue dispatching after the registry is introduced. Record the
    # migration-driven disable as a new immutable schedule revision.
    for row in legacy_rows:
        if str(row["service_principal_id"]) == BUILTIN_ID or not bool(row["enabled"]):
            continue
        revision = int(row["revision"] or 1) + 1
        snapshot = {
            "id": str(row["id"]),
            "pipeline_id": str(row["pipeline_id"]),
            "owner_id": str(row["owner_id"]),
            "steward_id": str(row["steward_id"]),
            "service_principal_id": str(row["service_principal_id"]),
            "pipeline_version_id": str(row["pipeline_version_id"]),
            "cron_expression": row["cron_expression"],
            "timezone": row["timezone"],
            "enabled": False,
            "revision": revision,
            "misfire_policy": row["misfire_policy"],
            "misfire_grace_seconds": row["misfire_grace_seconds"],
            "overlap_policy": row["overlap_policy"],
            "next_fire_at": None,
            "last_fire_at": _utc(row["last_fire_at"]).isoformat() if row["last_fire_at"] else None,
            "created_at": _utc(row["created_at"]).isoformat(),
            "updated_at": now.isoformat(),
        }
        connection.execute(sa.text(
            f"UPDATE {SCHEDULES} SET enabled = :disabled, next_fire_at = NULL, "
            "revision = :revision, updated_at = :updated_at WHERE id = :schedule_id"
        ), {
            "disabled": False,
            "revision": revision,
            "updated_at": now,
            "schedule_id": row["id"],
        })
        connection.execute(schedule_revisions.insert().values(
            id=str(uuid.uuid4()),
            schedule_id=str(row["id"]),
            revision=revision,
            pipeline_version_id=str(row["pipeline_version_id"]),
            snapshot=snapshot,
            snapshot_digest=_digest(snapshot),
            actor_id="system:migration:0094",
            created_at=now,
        ))

    for principal_id, sequence in next_sequence.items():
        connection.execute(sa.text(
            f"UPDATE {PRINCIPALS} SET revision = :revision WHERE id = :principal_id"
        ), {"revision": sequence, "principal_id": principal_id})

    dialect = connection.dialect.name
    if dialect == "sqlite":
        with op.batch_alter_table(RUNS) as batch:
            batch.add_column(sa.Column("service_principal_id", sa.String(length=100), nullable=True))
    else:
        op.add_column(RUNS, sa.Column("service_principal_id", sa.String(length=100), nullable=True))

    # Backfill a typed actor reference only where the old immutable manifest
    # already recorded a service_actor; never infer one from request_actor_id.
    run_rows = connection.execute(sa.text(
        f"SELECT id, execution_manifest FROM {RUNS} WHERE execution_manifest IS NOT NULL"
    )).mappings().all()
    valid_principal_ids = legacy_ids
    for row in run_rows:
        manifest = row["execution_manifest"]
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except (TypeError, ValueError):
                continue
        context = manifest.get("trigger_context") if isinstance(manifest, dict) else None
        actor = context.get("service_actor") if isinstance(context, dict) else None
        actor_id = actor.get("id") if isinstance(actor, dict) and actor.get("kind") == "service_principal" else None
        if actor_id and str(actor_id) in valid_principal_ids:
            connection.execute(sa.text(
                f"UPDATE {RUNS} SET service_principal_id = :principal_id WHERE id = :run_id"
            ), {"principal_id": str(actor_id), "run_id": row["id"]})

    with op.batch_alter_table(SCHEDULES) as batch:
        batch.create_foreign_key(
            "fk_v2_pipeline_schedules_service_principal",
            PRINCIPALS,
            ["service_principal_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table(RUNS) as batch:
        batch.create_foreign_key(
            "fk_v2_pipeline_runs_service_principal",
            PRINCIPALS,
            ["service_principal_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index("ix_v2_pipeline_runs_service_principal_id", RUNS, ["service_principal_id"])

    if dialect == "postgresql":
        op.execute(sa.text(
            "CREATE FUNCTION protect_service_principal_registry() RETURNS trigger AS $$ BEGIN "
            "IF ROW(NEW.id, NEW.kind, NEW.name, NEW.description, NEW.builtin, NEW.owner_user_id, NEW.created_by, NEW.created_at) "
            "IS DISTINCT FROM ROW(OLD.id, OLD.kind, OLD.name, OLD.description, OLD.builtin, OLD.owner_user_id, OLD.created_by, OLD.created_at) "
            "THEN RAISE EXCEPTION 'Service principal identity is immutable'; END IF; "
            "IF NEW.revision <> OLD.revision + 1 "
            "THEN RAISE EXCEPTION 'Service principal change must advance revision'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_identity BEFORE UPDATE ON {PRINCIPALS} "
            "FOR EACH ROW EXECUTE FUNCTION protect_service_principal_registry()"
        ))
        op.execute(sa.text(
            f"CREATE FUNCTION reject_service_principal_delete() RETURNS trigger AS $$ BEGIN "
            "RAISE EXCEPTION 'Service principal identities are retained'; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_no_delete BEFORE DELETE ON {PRINCIPALS} "
            "FOR EACH ROW EXECUTE FUNCTION reject_service_principal_delete()"
        ))
        op.execute(sa.text(
            "CREATE FUNCTION protect_service_principal_grant() RETURNS trigger AS $$ BEGIN "
            "IF ROW(NEW.id, NEW.service_principal_id, NEW.resource_type, NEW.resource_id, NEW.permission, NEW.created_by, NEW.reason, NEW.created_at) "
            "IS DISTINCT FROM ROW(OLD.id, OLD.service_principal_id, OLD.resource_type, OLD.resource_id, OLD.permission, OLD.created_by, OLD.reason, OLD.created_at) "
            "THEN RAISE EXCEPTION 'Service principal grant identity is immutable'; END IF; "
            "IF NOT OLD.active AND NEW.active THEN RAISE EXCEPTION 'Revoked grants cannot be reactivated'; END IF; "
            "IF OLD.active AND NOT NEW.active AND (NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL) "
            "THEN RAISE EXCEPTION 'Grant revocation requires actor and timestamp'; END IF; "
            "IF NOT OLD.active AND ROW(NEW.revoked_at, NEW.revoked_by) IS DISTINCT FROM ROW(OLD.revoked_at, OLD.revoked_by) "
            "THEN RAISE EXCEPTION 'Grant revocation evidence is immutable'; END IF; "
            "IF OLD.active AND NEW.active AND (NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL) "
            "THEN RAISE EXCEPTION 'Active grant cannot contain revocation evidence'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_grant_protect BEFORE UPDATE ON {GRANTS} "
            "FOR EACH ROW EXECUTE FUNCTION protect_service_principal_grant()"
        ))
        op.execute(sa.text(
            f"CREATE FUNCTION reject_service_principal_grant_delete() RETURNS trigger AS $$ BEGIN "
            "RAISE EXCEPTION 'Service principal grants are retained'; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_grant_no_delete BEFORE DELETE ON {GRANTS} "
            "FOR EACH ROW EXECUTE FUNCTION reject_service_principal_grant_delete()"
        ))
        op.execute(sa.text(
            f"CREATE FUNCTION reject_service_principal_event_mutation() RETURNS trigger AS $$ BEGIN "
            "RAISE EXCEPTION 'Service principal audit events are immutable'; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_event_immutable BEFORE UPDATE OR DELETE ON {EVENTS} "
            "FOR EACH ROW EXECUTE FUNCTION reject_service_principal_event_mutation()"
        ))
        op.execute(sa.text(
            "CREATE FUNCTION validate_service_principal_event_chain() RETURNS trigger AS $$ DECLARE "
            "last_sequence INTEGER; last_digest VARCHAR(64); BEGIN "
            f"SELECT sequence, event_digest INTO last_sequence, last_digest FROM {EVENTS} "
            "WHERE service_principal_id = NEW.service_principal_id ORDER BY sequence DESC LIMIT 1; "
            "IF NEW.sequence <> COALESCE(last_sequence, 0) + 1 THEN "
            "RAISE EXCEPTION 'Service principal event sequence must be contiguous'; END IF; "
            "IF NEW.previous_digest IS DISTINCT FROM last_digest THEN "
            "RAISE EXCEPTION 'Service principal event chain predecessor mismatch'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_event_chain BEFORE INSERT ON {EVENTS} "
            "FOR EACH ROW EXECUTE FUNCTION validate_service_principal_event_chain()"
        ))
    elif dialect == "sqlite":
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_identity BEFORE UPDATE ON {PRINCIPALS} "
            "WHEN OLD.id IS NOT NEW.id OR OLD.kind IS NOT NEW.kind OR OLD.name IS NOT NEW.name "
            "OR OLD.description IS NOT NEW.description OR OLD.builtin IS NOT NEW.builtin "
            "OR OLD.owner_user_id IS NOT NEW.owner_user_id OR OLD.created_by IS NOT NEW.created_by "
            "OR OLD.created_at IS NOT NEW.created_at "
            "OR NEW.revision <> OLD.revision + 1 "
            "BEGIN SELECT RAISE(ABORT, 'Service principal identity or revision is immutable'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_no_delete BEFORE DELETE ON {PRINCIPALS} "
            "BEGIN SELECT RAISE(ABORT, 'Service principal identities are retained'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_grant_protect BEFORE UPDATE ON {GRANTS} "
            "WHEN OLD.id IS NOT NEW.id OR OLD.service_principal_id IS NOT NEW.service_principal_id "
            "OR OLD.resource_type IS NOT NEW.resource_type OR OLD.resource_id IS NOT NEW.resource_id "
            "OR OLD.permission IS NOT NEW.permission OR OLD.created_by IS NOT NEW.created_by "
            "OR OLD.reason IS NOT NEW.reason OR OLD.created_at IS NOT NEW.created_at "
            "OR (OLD.active = 0 AND NEW.active = 1) "
            "OR (OLD.active = 1 AND NEW.active = 0 AND (NEW.revoked_at IS NULL OR NEW.revoked_by IS NULL)) "
            "OR (OLD.active = 0 AND (OLD.revoked_at IS NOT NEW.revoked_at OR OLD.revoked_by IS NOT NEW.revoked_by)) "
            "OR (OLD.active = 1 AND NEW.active = 1 AND (NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL)) "
            "BEGIN SELECT RAISE(ABORT, 'Service principal grant identity is immutable'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_grant_no_delete BEFORE DELETE ON {GRANTS} "
            "BEGIN SELECT RAISE(ABORT, 'Service principal grants are retained'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_event_update BEFORE UPDATE ON {EVENTS} "
            "BEGIN SELECT RAISE(ABORT, 'Service principal audit events are immutable'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_event_delete BEFORE DELETE ON {EVENTS} "
            "BEGIN SELECT RAISE(ABORT, 'Service principal audit events are immutable'); END"
        ))
        op.execute(sa.text(
            f"CREATE TRIGGER trg_service_principal_event_chain BEFORE INSERT ON {EVENTS} "
            f"WHEN NEW.sequence <> COALESCE((SELECT MAX(sequence) FROM {EVENTS} "
            "WHERE service_principal_id = NEW.service_principal_id), 0) + 1 "
            f"OR NEW.previous_digest IS NOT (SELECT event_digest FROM {EVENTS} "
            "WHERE service_principal_id = NEW.service_principal_id ORDER BY sequence DESC LIMIT 1) "
            "BEGIN SELECT RAISE(ABORT, 'Service principal audit event chain mismatch'); END"
        ))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(f"SELECT 1 FROM {SCHEDULES} LIMIT 1")).first():
        raise RuntimeError("Pipeline schedules depend on the service principal registry; downgrade is unsafe")
    if bind.execute(sa.text(f"SELECT 1 FROM {RUNS} WHERE service_principal_id IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Pipeline runs contain service principal attribution; downgrade is unsafe")
    if bind.execute(sa.text(f"SELECT 1 FROM {PRINCIPALS} WHERE id <> :builtin LIMIT 1"), {"builtin": BUILTIN_ID}).first():
        raise RuntimeError("Non-built-in service principals exist; downgrade would delete registry data")
    if bind.execute(sa.text(f"SELECT 1 FROM {GRANTS} LIMIT 1")).first():
        raise RuntimeError("Service principal grants exist; downgrade would delete authorization history")
    if bind.execute(sa.text(f"SELECT 1 FROM {EVENTS} LIMIT 1")).first():
        raise RuntimeError("Service principal audit events exist; downgrade would delete registry history")

    dialect = bind.dialect.name
    if dialect == "postgresql":
        for table, trigger, function in (
            (EVENTS, "trg_service_principal_event_immutable", "reject_service_principal_event_mutation"),
            (EVENTS, "trg_service_principal_event_chain", "validate_service_principal_event_chain"),
            (GRANTS, "trg_service_principal_grant_no_delete", "reject_service_principal_grant_delete"),
            (GRANTS, "trg_service_principal_grant_protect", "protect_service_principal_grant"),
            (PRINCIPALS, "trg_service_principal_no_delete", "reject_service_principal_delete"),
            (PRINCIPALS, "trg_service_principal_identity", "protect_service_principal_registry"),
        ):
            op.execute(sa.text(f"DROP TRIGGER {trigger} ON {table}"))
            op.execute(sa.text(f"DROP FUNCTION {function}()"))
    elif dialect == "sqlite":
        for trigger in (
            "trg_service_principal_event_chain",
            "trg_service_principal_event_delete", "trg_service_principal_event_update",
            "trg_service_principal_grant_no_delete", "trg_service_principal_grant_protect",
            "trg_service_principal_no_delete", "trg_service_principal_identity",
        ):
            op.execute(sa.text(f"DROP TRIGGER {trigger}"))

    op.drop_index("ix_v2_pipeline_runs_service_principal_id", table_name=RUNS)
    with op.batch_alter_table(RUNS) as batch:
        batch.drop_constraint("fk_v2_pipeline_runs_service_principal", type_="foreignkey")
        batch.drop_column("service_principal_id")
    with op.batch_alter_table(SCHEDULES) as batch:
        batch.drop_constraint("fk_v2_pipeline_schedules_service_principal", type_="foreignkey")
    op.drop_index("ix_v2_service_principal_events_principal", table_name=EVENTS)
    op.drop_table(EVENTS)
    op.drop_index("ix_v2_service_principal_grants_lookup", table_name=GRANTS)
    op.drop_table(GRANTS)
    op.drop_index("ix_v2_service_principals_status_kind", table_name=PRINCIPALS)
    op.drop_table(PRINCIPALS)
