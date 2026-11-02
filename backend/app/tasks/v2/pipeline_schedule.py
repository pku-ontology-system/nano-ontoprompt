"""Durable Pipeline Schedule tick materialization and recovery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_

from app.database import SessionLocal
from app.models.user import User
from app.models.v2.pipeline import (
    Pipeline,
    PipelineRun,
    PipelineSchedule,
    PipelineScheduleRevision,
    PipelineScheduleRun,
)
from app.models.v2.service_principal import ServicePrincipal, ServicePrincipalGrant
from app.services.v2.pipeline_execution import content_digest
from app.services.v2.scheduler.cron_service import CronService
from app.tasks.celery_app import celery_app


CLAIM_LEASE = timedelta(minutes=2)
MAX_SCHEDULE_DISPATCH_ATTEMPTS = 5
ACTIVE_SCHEDULE_RUNS = ("pending", "dispatching", "queued", "running")
TERMINAL_PIPELINE_RUNS = {"success", "degraded", "failed", "cancelled"}
MAX_MATERIALIZED_PER_SCHEDULE = 1000


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _local_slot(value: datetime, timezone_name: str) -> str:
    return _utc(value).astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:%M%z")


def _map_pipeline_status(status: str) -> str:
    if status in {"success", "degraded", "failed", "cancelled"}:
        return status
    if status == "running":
        return "running"
    return "queued"


def _refresh_schedule_run_projection(db, schedule_run: PipelineScheduleRun, pipeline_run: PipelineRun) -> None:
    mapped = _map_pipeline_status(pipeline_run.status)
    if schedule_run.status != mapped:
        schedule_run.status = mapped
    if pipeline_run.started_at and not schedule_run.started_at:
        schedule_run.started_at = pipeline_run.started_at
    if pipeline_run.finished_at:
        schedule_run.finished_at = pipeline_run.finished_at
    if pipeline_run.status == "failed" and not schedule_run.error_code:
        schedule_run.error_code = "PIPELINE_RUN_FAILED"
        schedule_run.error_message = "关联的 PipelineRun 失败；请查看 PipelineRun 详情"
    elif pipeline_run.status == "cancelled" and not schedule_run.error_code:
        schedule_run.error_code = "PIPELINE_RUN_CANCELLED"
        schedule_run.error_message = "关联的 PipelineRun 已取消"


def _refresh_linked_runs(batch_size: int) -> int:
    changed = 0
    with SessionLocal() as db:
        rows = db.query(PipelineScheduleRun, PipelineRun).join(
            PipelineRun, PipelineRun.id == PipelineScheduleRun.pipeline_run_id
        ).filter(
            PipelineScheduleRun.status.in_(ACTIVE_SCHEDULE_RUNS),
        ).order_by(PipelineScheduleRun.created_at.asc()).limit(batch_size).all()
        for schedule_run, pipeline_run in rows:
            before = (schedule_run.status, schedule_run.started_at, schedule_run.finished_at)
            _refresh_schedule_run_projection(db, schedule_run, pipeline_run)
            after = (schedule_run.status, schedule_run.started_at, schedule_run.finished_at)
            if before != after:
                changed += 1
        db.commit()
    return changed


def _new_schedule_run(
    db,
    schedule: PipelineSchedule,
    revision: PipelineScheduleRevision,
    scheduled_for: datetime,
    *,
    status: str = "pending",
    error_code: str | None = None,
    error_message: str | None = None,
    coalesced_count: int = 0,
) -> bool:
    slot = _local_slot(scheduled_for, schedule.timezone)
    existing = db.query(PipelineScheduleRun.id).filter(
        PipelineScheduleRun.schedule_revision_id == revision.id,
        PipelineScheduleRun.local_slot == slot,
    ).first()
    if existing:
        return False
    row = PipelineScheduleRun(
        id=str(uuid4()),
        schedule_id=schedule.id,
        schedule_revision_id=revision.id,
        scheduled_for=_utc(scheduled_for),
        local_slot=slot,
        status=status,
        error_code=error_code,
        error_message=error_message,
        coalesced_count=coalesced_count,
    )
    db.add(row)
    return True


def _disable_schedule_for_principal(db, schedule: PipelineSchedule, actor_id: str, now: datetime) -> None:
    if not schedule.enabled:
        return
    schedule.enabled = False
    schedule.revision += 1
    schedule.next_fire_at = None
    schedule.updated_at = now
    snapshot = {
        "id": schedule.id,
        "pipeline_id": schedule.pipeline_id,
        "owner_id": schedule.owner_id,
        "steward_id": schedule.steward_id,
        "service_principal_id": schedule.service_principal_id,
        "pipeline_version_id": schedule.pipeline_version_id,
        "cron_expression": schedule.cron_expression,
        "timezone": schedule.timezone,
        "enabled": False,
        "revision": schedule.revision,
        "misfire_policy": schedule.misfire_policy,
        "misfire_grace_seconds": schedule.misfire_grace_seconds,
        "overlap_policy": schedule.overlap_policy,
        "next_fire_at": None,
        "last_fire_at": schedule.last_fire_at.isoformat() if schedule.last_fire_at else None,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
        "updated_at": now.isoformat(),
    }
    db.add(PipelineScheduleRevision(
        schedule_id=schedule.id,
        revision=schedule.revision,
        pipeline_version_id=schedule.pipeline_version_id,
        snapshot=snapshot,
        snapshot_digest=content_digest(snapshot),
        actor_id=f"service_principal:{actor_id}",
        created_at=now,
    ))


def _materialize_due_schedules(now: datetime, batch_size: int) -> dict:
    counts = {"schedules": 0, "created": 0, "skipped": 0, "coalesced": 0, "invalid": 0}
    cron = CronService()
    with SessionLocal() as db:
        schedules = db.query(PipelineSchedule).filter(
            PipelineSchedule.enabled.is_(True),
            PipelineSchedule.next_fire_at.is_not(None),
            PipelineSchedule.next_fire_at <= now,
        ).order_by(PipelineSchedule.next_fire_at.asc()).with_for_update(skip_locked=True).limit(batch_size).all()
        for schedule in schedules:
            counts["schedules"] += 1
            revision = db.query(PipelineScheduleRevision).filter(
                PipelineScheduleRevision.schedule_id == schedule.id,
                PipelineScheduleRevision.revision == schedule.revision,
            ).first()
            if not revision:
                schedule.enabled = False
                schedule.next_fire_at = None
                counts["invalid"] += 1
                continue

            try:
                due: list[datetime] = []
                cursor = _utc(schedule.next_fire_at)
                while cursor and cursor <= now and len(due) < MAX_MATERIALIZED_PER_SCHEDULE:
                    due.append(cursor)
                    cursor = cron.next_fire_utc(schedule.cron_expression, schedule.timezone, cursor)

                grace_cutoff = now - timedelta(seconds=schedule.misfire_grace_seconds)
                if schedule.misfire_policy == "run_once":
                    missed = [fire_time for fire_time in due if fire_time < grace_cutoff]
                    if missed:
                        counts["created"] += int(_new_schedule_run(
                            db,
                            schedule,
                            revision,
                            missed[-1],
                            coalesced_count=max(0, len(missed) - 1),
                        ))
                        counts["coalesced"] += len(missed)
                    due = [fire_time for fire_time in due if fire_time >= grace_cutoff]

                for fire_time in due:
                    if schedule.misfire_policy == "skip" and fire_time < grace_cutoff:
                        created = _new_schedule_run(
                            db,
                            schedule,
                            revision,
                            fire_time,
                            status="skipped",
                            error_code="MISFIRE_EXPIRED",
                            error_message="Beat 延迟超过 misfire grace，按策略跳过",
                        )
                        counts["skipped"] += int(created)
                    else:
                        counts["created"] += int(_new_schedule_run(
                            db, schedule, revision, fire_time
                        ))

                # If the cap was reached, leave the cursor due for the next tick.
                schedule.next_fire_at = cursor
                schedule.last_fire_at = due[-1] if due else (missed[-1] if schedule.misfire_policy == "run_once" and missed else schedule.last_fire_at)
                schedule.updated_at = now
            except Exception:
                schedule.enabled = False
                schedule.next_fire_at = None
                counts["invalid"] += 1
        db.commit()
    return counts


def _claim_schedule_run(schedule_run_id: str, now: datetime) -> dict | None:
    claim = str(uuid4())
    with SessionLocal() as db:
        run_ref = db.query(PipelineScheduleRun.schedule_id).filter(
            PipelineScheduleRun.id == schedule_run_id
        ).first()
        if not run_ref:
            return None
        schedule = db.query(PipelineSchedule).filter(
            PipelineSchedule.id == run_ref[0]
        ).with_for_update().first()
        schedule_run = db.query(PipelineScheduleRun).filter(
            PipelineScheduleRun.id == schedule_run_id
        ).with_for_update().first()
        if not schedule_run:
            return None
        state = schedule_run.status
        if state == "dispatching":
            lease_until = _utc(schedule_run.lease_until)
            if lease_until and lease_until > now:
                return None
        elif state != "pending":
            return None

        revision = db.query(PipelineScheduleRevision).filter(
            PipelineScheduleRevision.id == schedule_run.schedule_revision_id
        ).first()
        if not schedule or not revision or not schedule.enabled or schedule.revision != revision.revision:
            schedule_run.status = "cancelled"
            schedule_run.error_code = "SCHEDULE_DISABLED_OR_SUPERSEDED"
            schedule_run.error_message = "Schedule 已停用或已由新 revision 取代"
            schedule_run.finished_at = now
            db.commit()
            return None

        principal = db.query(ServicePrincipal).filter(
            ServicePrincipal.id == schedule.service_principal_id,
        ).with_for_update().first()
        grant = db.query(ServicePrincipalGrant).filter(
            ServicePrincipalGrant.service_principal_id == schedule.service_principal_id,
            ServicePrincipalGrant.resource_type == "pipeline",
            ServicePrincipalGrant.resource_id == schedule.pipeline_id,
            ServicePrincipalGrant.permission == "run",
            ServicePrincipalGrant.active.is_(True),
        ).with_for_update().first()
        if not principal or principal.status != "active":
            schedule_run.status = "failed"
            schedule_run.error_code = "SERVICE_PRINCIPAL_INACTIVE"
            schedule_run.error_message = "Schedule 服务主体不存在或已暂停"
            schedule_run.finished_at = now
            _disable_schedule_for_principal(db, schedule, schedule.service_principal_id, now)
            db.commit()
            return None
        if not grant:
            schedule_run.status = "failed"
            schedule_run.error_code = "SERVICE_PRINCIPAL_SCOPE_REVOKED"
            schedule_run.error_message = "Schedule 服务主体已失去此 Pipeline 的 Run 授权"
            schedule_run.finished_at = now
            _disable_schedule_for_principal(db, schedule, schedule.service_principal_id, now)
            db.commit()
            return None

        if schedule_run.dispatch_attempts >= MAX_SCHEDULE_DISPATCH_ATTEMPTS:
            existing_run = db.query(PipelineRun).filter(
                PipelineRun.pipeline_id == schedule.pipeline_id,
                PipelineRun.request_actor_id == schedule.owner_id,
                PipelineRun.request_key == f"schedule-run-{schedule_run.id}",
            ).first()
            if existing_run:
                schedule_run.pipeline_run_id = existing_run.id
                schedule_run.status = "queued" if existing_run.status == "pending" else _map_pipeline_status(existing_run.status)
                schedule_run.claim_token = None
                schedule_run.lease_until = None
                if existing_run.started_at:
                    schedule_run.started_at = existing_run.started_at
                if existing_run.finished_at:
                    schedule_run.finished_at = existing_run.finished_at
                db.commit()
                return {"recovered_pipeline_run_id": existing_run.id}
            schedule_run.status = "failed"
            schedule_run.error_code = "SCHEDULE_DISPATCH_EXHAUSTED"
            schedule_run.error_message = "Schedule 派发超过最大尝试次数"
            schedule_run.finished_at = now
            schedule_run.claim_token = None
            schedule_run.lease_until = None
            db.commit()
            return None

        if schedule.overlap_policy == "forbid":
            prior_active = db.query(PipelineScheduleRun.id).filter(
                PipelineScheduleRun.schedule_id == schedule.id,
                PipelineScheduleRun.id != schedule_run.id,
                PipelineScheduleRun.scheduled_for < schedule_run.scheduled_for,
                PipelineScheduleRun.status.in_(ACTIVE_SCHEDULE_RUNS),
            ).first()
            if prior_active:
                schedule_run.status = "skipped"
                schedule_run.error_code = "OVERLAP_FORBIDDEN"
                schedule_run.error_message = "已有较早的 Schedule Run 未结束，按串行策略跳过"
                schedule_run.finished_at = now
                db.commit()
                return None

        schedule_run.status = "dispatching"
        schedule_run.claim_token = claim
        schedule_run.lease_until = now + CLAIM_LEASE
        schedule_run.dispatch_attempts += 1
        db.commit()
        return {
            "claim_token": claim,
            "schedule_id": schedule.id,
            "schedule_revision_id": revision.id,
            "schedule_revision": revision.revision,
            "pipeline_version_id": revision.pipeline_version_id,
            "service_principal_id": schedule.service_principal_id,
            "owner_id": schedule.owner_id,
            "pipeline_id": schedule.pipeline_id,
            "scheduled_for": _utc(schedule_run.scheduled_for).isoformat(),
            "local_slot": schedule_run.local_slot,
        }


def _fail_claim(schedule_run_id: str, claim_token: str, code: str, message: str) -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = db.query(PipelineScheduleRun).filter(
            PipelineScheduleRun.id == schedule_run_id
        ).with_for_update().first()
        if row and row.status == "dispatching" and row.claim_token == claim_token:
            row.status = "failed"
            row.error_code = code
            row.error_message = message[:1000]
            row.finished_at = now
            row.claim_token = None
            row.lease_until = None
            if code in {"SERVICE_PRINCIPAL_INACTIVE", "SERVICE_PRINCIPAL_SCOPE_REVOKED"}:
                schedule = db.query(PipelineSchedule).filter(
                    PipelineSchedule.id == row.schedule_id
                ).with_for_update().first()
                if schedule:
                    _disable_schedule_for_principal(db, schedule, schedule.service_principal_id, now)
            db.commit()


def _dispatch_schedule_run(schedule_run_id: str, now: datetime) -> dict:
    claim = _claim_schedule_run(schedule_run_id, now)
    if not claim:
        return {"id": schedule_run_id, "state": "not_claimed"}
    recovered_run_id = claim.get("recovered_pipeline_run_id")
    if recovered_run_id:
        from app.tasks.v2.pipeline_run import dispatch_pipeline_run
        dispatch_pipeline_run(recovered_run_id)
        return {"id": schedule_run_id, "state": "recovered", "pipeline_run_id": recovered_run_id}

    try:
        with SessionLocal() as db:
            principal = db.query(ServicePrincipal).filter(
                ServicePrincipal.id == claim["service_principal_id"],
                ServicePrincipal.status == "active",
            ).with_for_update().first()
            grant = db.query(ServicePrincipalGrant).filter(
                ServicePrincipalGrant.service_principal_id == claim["service_principal_id"],
                ServicePrincipalGrant.resource_type == "pipeline",
                ServicePrincipalGrant.resource_id == claim["pipeline_id"],
                ServicePrincipalGrant.permission == "run",
                ServicePrincipalGrant.active.is_(True),
            ).with_for_update().first()
            if not principal or not grant:
                failure_code = "SERVICE_PRINCIPAL_INACTIVE" if not principal else "SERVICE_PRINCIPAL_SCOPE_REVOKED"
                db.rollback()
                _fail_claim(
                    schedule_run_id,
                    claim["claim_token"],
                    failure_code,
                    "Schedule 服务主体不存在/已暂停，或已失去 Pipeline Run 授权",
                )
                return {"id": schedule_run_id, "state": "failed"}
            owner = db.query(User).filter(User.id == claim["owner_id"]).first()
            pipeline = db.query(Pipeline).filter(Pipeline.id == claim["pipeline_id"]).first()
            if not owner or not owner.is_active or owner.role not in {"admin", "editor"}:
                raise PermissionError("Schedule owner is inactive or no longer has editor access")
            if not pipeline or str(pipeline.created_by or "") != str(owner.id):
                raise PermissionError("Pipeline ownership changed after the Schedule was configured")

            # Re-evaluate dataset ACLs at trigger time with the schedule owner as caller.
            from app.routers.v2.pipelines import _create_pipeline_run, _pipeline_or_404
            from fastapi import HTTPException

            _pipeline_or_404(db, pipeline.id, owner, write=True)
            trigger_context = {
                "schedule_id": claim["schedule_id"],
                "schedule_revision_id": claim["schedule_revision_id"],
                "schedule_revision": claim["schedule_revision"],
                "schedule_run_id": schedule_run_id,
                "scheduled_for": claim["scheduled_for"],
                "local_slot": claim["local_slot"],
                "caller_actor": {"kind": "user", "id": owner.id},
                "service_actor": {
                    "kind": "service_principal",
                    "id": principal.id,
                    "principal_kind": principal.kind,
                    "revision": principal.revision,
                    "scope_grant_id": grant.id,
                },
            }
            try:
                pipeline_run, _created = _create_pipeline_run(
                    db,
                    pipeline.id,
                    trigger_mode="schedule",
                    dispatch_status="pending",
                    idempotency_key=f"schedule-run-{schedule_run_id}",
                    request_actor_id=owner.id,
                    service_principal_id=principal.id,
                    pinned_pipeline_version_id=claim["pipeline_version_id"],
                    trigger_context=trigger_context,
                )
            except HTTPException as exc:
                _fail_claim(
                    schedule_run_id,
                    claim["claim_token"],
                    "PIPELINE_RUN_REJECTED",
                    f"PipelineRun 创建被拒绝（HTTP {exc.status_code}）",
                )
                return {"id": schedule_run_id, "state": "failed"}

            with SessionLocal() as link_db:
                schedule_run = link_db.query(PipelineScheduleRun).filter(
                    PipelineScheduleRun.id == schedule_run_id
                ).with_for_update().first()
                if not schedule_run or schedule_run.claim_token != claim["claim_token"]:
                    orphan = link_db.query(PipelineRun).filter(
                        PipelineRun.id == pipeline_run.id
                    ).with_for_update().first()
                    if orphan and orphan.status == "pending":
                        orphan.status = "cancelled"
                        orphan.dispatch_status = "cancelled"
                        orphan.dispatch_claim_token = None
                        orphan.dispatch_lease_until = None
                        orphan.finished_at = datetime.now(timezone.utc)
                        orphan.error_log = "Schedule Run was cancelled before dispatch"
                        link_db.commit()
                    return {"id": schedule_run_id, "state": "claim_lost"}
                schedule_run.pipeline_run_id = pipeline_run.id
                schedule_run.status = "queued" if pipeline_run.status == "pending" else _map_pipeline_status(pipeline_run.status)
                schedule_run.claim_token = None
                schedule_run.lease_until = None
                if schedule_run.status == "running":
                    schedule_run.started_at = pipeline_run.started_at
                if schedule_run.status in {"success", "degraded", "failed", "cancelled"}:
                    schedule_run.finished_at = pipeline_run.finished_at or datetime.now(timezone.utc)
                link_db.commit()

            if pipeline_run.status == "pending":
                from app.tasks.v2.pipeline_run import dispatch_pipeline_run
                dispatch_pipeline_run(pipeline_run.id)
            return {"id": schedule_run_id, "state": "linked", "pipeline_run_id": pipeline_run.id}
    except Exception as exc:
        _fail_claim(
            schedule_run_id,
            claim["claim_token"],
            "SCHEDULE_DISPATCH_ERROR",
            f"Schedule 派发异常：{type(exc).__name__}",
        )
        return {"id": schedule_run_id, "state": "failed"}


def _dispatch_pending_runs(now: datetime, batch_size: int) -> dict:
    with SessionLocal() as db:
        due = db.query(PipelineScheduleRun.id).filter(
            or_(
                PipelineScheduleRun.status == "pending",
                and_(
                    PipelineScheduleRun.status == "dispatching",
                    or_(PipelineScheduleRun.lease_until.is_(None), PipelineScheduleRun.lease_until <= now),
                ),
            )
        ).order_by(PipelineScheduleRun.scheduled_for.asc()).limit(batch_size).all()
        ids = [row[0] for row in due]
    states = [_dispatch_schedule_run(schedule_run_id, now) for schedule_run_id in ids]
    return {
        "claimed": sum(state.get("state") == "linked" for state in states),
        "failed": sum(state.get("state") == "failed" for state in states),
        "considered": len(states),
    }


@celery_app.task(name="v2.pipeline_schedule.reconcile", acks_late=True, reject_on_worker_lost=True)
def reconcile_pipeline_schedules(batch_size: int = 100) -> dict:
    """Materialize cron ticks, safely dispatch them, and project PipelineRun state."""
    batch_size = min(max(int(batch_size), 1), 500)
    now = datetime.now(timezone.utc)
    refreshed_before = _refresh_linked_runs(batch_size)
    materialized = _materialize_due_schedules(now, batch_size)
    dispatched = _dispatch_pending_runs(now, batch_size)
    refreshed_after = _refresh_linked_runs(batch_size)
    return {
        "materialized": materialized,
        "dispatch": dispatched,
        "refreshed": refreshed_before + refreshed_after,
    }
