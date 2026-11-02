"""Durable dispatch, retry, and provenance for approved Link Mapping Apply."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.v2.mapping import OntologyLinkMapping
from app.models.v2.mapping_link_apply_run import (
    MappingLinkApplyRun,
    MappingLinkApplyRunAttemptEvent,
)
from app.services.v2.mapping_link_apply import LinkApplyError, build_link_mapping_preview

log = logging.getLogger(__name__)

MAX_EXECUTION_ATTEMPTS = 8
MAX_DISPATCH_ATTEMPTS = 20
DISPATCH_LEASE_SECONDS = 90
QUEUED_REDELIVERY_SECONDS = 120
RUN_LEASE_SECONDS = 30 * 60

_SAFE_MESSAGES = {
    "approved_link_mapping_review_required": "必须先完成关系映射定义的独立审核。",
    "link_mapping_review_invalid": "关系映射审核记录不满足独立审核要求。",
    "approved_dataset_review_required": "来源和目标数据集都需要已批准的数据审核版本。",
    "dataset_not_approved": "来源或目标数据集当前未获批准。",
    "entity_mapping_apply_required": "请先为来源和目标数据集各完成一项实体映射 Apply。",
    "approved_entity_mapping_review_required": "两端实体映射都需要已批准的定义审核。",
    "entity_mapping_changed": "实体映射定义已变化，请重新审核并预览。",
    "entity_projection_pending": "实体节点仍在等待图数据库投影。",
    "entity_mapping_has_no_instances": "固定实体映射没有可用于建立关系的行。",
    "entity_instances_do_not_match_pinned_version": "实体节点与固定数据版本不一致，请重新 Apply 实体映射。",
    "link_key_must_be_scalar": "关系连接字段必须是单值字段。",
    "legacy_link_relations_require_reconciliation": "发现旧版关系边，需要先完成关系数据核对。",
    "dispatch_failed": "关系映射任务派发失败，系统将在预算允许时重试。",
    "dispatch_attempts_exhausted": "关系映射任务派发重试次数已耗尽。",
    "execution_attempts_exhausted": "关系映射任务执行重试次数已耗尽。",
    "execution_outcome_unknown": "工作进程租约过期，最近一次执行结果未知。",
    "link_apply_worker_error": "关系映射执行失败。详细诊断信息已隐藏。",
    "graph_unavailable": "图数据库暂不可用，关系边仍保留在待投影队列。",
    "graph_projection_failed": "图数据库投影失败，系统将在预算允许时重试。",
    "graph_projection_pending": "部分关系边仍在等待图数据库投影。",
    "projection_attempts_exhausted": "关系边图投影重试次数已耗尽，需要人工处理。",
    "link_apply_inputs_changed": "固定输入已发生变化，请重新预览后再提交。",
    "link_apply_preview_stale": "关系状态已发生变化，请重新预览后再提交。",
    "link_apply_snapshot_is_stale": "已批准的数据版本已变化，请重新预览后再提交。",
    "link_apply_already_running": "当前关系映射已有 Apply 任务正在执行，请等待完成后重新预览。",
    "link_apply_idempotency_conflict": "幂等请求编号已用于另一份预览，请刷新后重试。",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff_seconds(attempt: int) -> int:
    return min(30 * (2 ** max(int(attempt) - 1, 0)), 3600)


def _safe_message(error_code: str | None) -> str | None:
    if not error_code:
        return None
    return _SAFE_MESSAGES.get(str(error_code), "关系映射执行失败。详细诊断信息已隐藏。")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _event_payload(
    *, run_id: str, sequence_no: int, attempt_number: int, event_type: str,
    status: str, actor_kind: str, actor_id: str, celery_task_id: str | None,
    error_code: str | None, previous_digest: str | None, occurred_at: datetime,
) -> dict:
    return {
        "schema_version": 1,
        "run_id": str(run_id),
        "sequence_no": int(sequence_no),
        "attempt_number": int(attempt_number),
        "event_type": event_type,
        "status": status,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "celery_task_id": celery_task_id,
        "error_code": error_code,
        "previous_digest": previous_digest,
        "occurred_at": _timestamp(occurred_at),
    }


def _digest(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _append_attempt_event(
    db: Session,
    *, run_id: str, attempt_number: int, event_type: str, status: str,
    actor_kind: str, actor_id: str, celery_task_id: str | None = None,
    error_code: str | None = None,
) -> MappingLinkApplyRunAttemptEvent:
    previous = db.query(MappingLinkApplyRunAttemptEvent).filter(
        MappingLinkApplyRunAttemptEvent.run_id == run_id,
    ).order_by(MappingLinkApplyRunAttemptEvent.sequence_no.desc()).first()
    sequence_no = int(previous.sequence_no if previous else 0) + 1
    previous_digest = previous.event_digest if previous else None
    occurred_at = _now()
    safe_actor_id = str(actor_id or "unreported-worker")[:200]
    safe_task_id = str(celery_task_id)[:200] if celery_task_id else None
    safe_error = str(error_code)[:100] if error_code else None
    payload = _event_payload(
        run_id=run_id, sequence_no=sequence_no, attempt_number=attempt_number,
        event_type=event_type, status=status, actor_kind=actor_kind,
        actor_id=safe_actor_id, celery_task_id=safe_task_id,
        error_code=safe_error, previous_digest=previous_digest, occurred_at=occurred_at,
    )
    row = MappingLinkApplyRunAttemptEvent(
        run_id=str(run_id), sequence_no=sequence_no, attempt_number=int(attempt_number),
        digest_version=1, event_type=event_type, status=status, actor_kind=actor_kind,
        actor_id=safe_actor_id, celery_task_id=safe_task_id, error_code=safe_error,
        previous_digest=previous_digest, event_digest=_digest(payload), occurred_at=occurred_at,
    )
    db.add(row)
    db.flush()
    return row


def _serialize_history(run: MappingLinkApplyRun) -> tuple[list[dict], str, bool | None]:
    events = list(run.attempt_events or [])
    serialized: list[dict] = []
    previous_digest = None
    starts: set[int] = set()
    terminals: dict[int, int] = {}
    valid = True
    for expected, item in enumerate(events, 1):
        payload = _event_payload(
            run_id=item.run_id, sequence_no=item.sequence_no, attempt_number=item.attempt_number,
            event_type=item.event_type, status=item.status, actor_kind=item.actor_kind,
            actor_id=item.actor_id, celery_task_id=item.celery_task_id,
            error_code=item.error_code, previous_digest=item.previous_digest, occurred_at=item.occurred_at,
        )
        if (item.sequence_no != expected or item.previous_digest != previous_digest
                or _digest(payload) != item.event_digest):
            valid = False
        previous_digest = item.event_digest
        if item.event_type == "attempt_started":
            if item.attempt_number in starts or item.status != "running" or item.actor_kind != "worker":
                valid = False
            starts.add(int(item.attempt_number))
        elif item.event_type in {"attempt_succeeded", "attempt_failed", "attempt_outcome_unknown"}:
            terminals[item.attempt_number] = terminals.get(item.attempt_number, 0) + 1
            valid_status = (
                item.status == "succeeded" if item.event_type == "attempt_succeeded"
                else item.status in {"retry_wait", "failed"}
            )
            valid_actor = item.actor_kind == ("system" if item.event_type == "attempt_outcome_unknown" else "worker")
            if not valid_status or not valid_actor:
                valid = False
        else:
            valid = False
        serialized.append({
            "sequence_no": item.sequence_no,
            "attempt_number": item.attempt_number,
            "event_type": item.event_type,
            "status": item.status,
            "actor_kind": item.actor_kind,
            "actor_id": item.actor_id,
            "celery_task_id": item.celery_task_id,
            "error_code": item.error_code,
            "previous_digest": item.previous_digest,
            "event_digest": item.event_digest,
            "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
        })

    attempts = set(range(1, int(run.execution_attempts or 0) + 1))
    if starts != attempts or any(count != 1 for count in terminals.values()):
        valid = False
    open_attempts = attempts - set(terminals)
    if run.status == "running" and open_attempts == {int(run.execution_attempts)}:
        state = "in_progress"
    elif run.status != "running" and not open_attempts and valid:
        state = "complete"
    else:
        state = "invalid"
        valid = False
    return serialized, state, valid


def serialize_mapping_link_apply_run(run: MappingLinkApplyRun) -> dict:
    events, history_state, history_valid = _serialize_history(run)
    return {
        "run_id": run.id,
        "link_mapping_id": run.link_mapping_id,
        "link_review_id": run.link_review_id,
        "ontology_id": run.ontology_id,
        "source_dataset_id": run.src_dataset_id,
        "source_review_id": run.src_review_id,
        "source_dataset_version_id": run.src_dataset_version_id,
        "source_mapping_id": run.src_mapping_id,
        "source_mapping_apply_run_id": run.src_mapping_apply_run_id,
        "target_dataset_id": run.tgt_dataset_id,
        "target_review_id": run.tgt_review_id,
        "target_dataset_version_id": run.tgt_dataset_version_id,
        "target_mapping_id": run.tgt_mapping_id,
        "target_mapping_apply_run_id": run.tgt_mapping_apply_run_id,
        "preview_digest": run.preview_digest,
        "preview_summary": run.preview_summary_json or {},
        "requested_by": run.requested_by,
        "request_actor_kind": run.request_actor_kind,
        "request_actor_id": run.request_actor_id,
        "worker_actor_id": run.worker_actor_id,
        "status": run.status,
        "execution_attempts": run.execution_attempts,
        "dispatch_attempts": run.dispatch_attempts,
        "celery_task_id": run.celery_task_id,
        "result": run.result_json,
        "error_code": run.error_code,
        "error_message": _safe_message(run.error_code),
        "next_attempt_at": run.next_attempt_at.isoformat() if run.next_attempt_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "attempt_events": events,
        "attempt_history_state": history_state,
        "attempt_history_valid": history_valid,
    }


class MappingLinkApplyRunService:
    def __init__(self, db: Session, *, dispatch_task=None):
        self._db = db
        self._dispatch_task = dispatch_task

    def enqueue_approved_link_mapping(
        self,
        *,
        link_mapping_id: str,
        ontology_id: str,
        link_review_id: str,
        preview_digest: str,
        requested_by: str,
        client_request_id: str,
        request_actor_kind: str = "user",
        commit: bool = True,
    ) -> tuple[MappingLinkApplyRun, bool]:
        if request_actor_kind != "user" or not requested_by:
            raise ValueError("Link Mapping Apply requires a user requester")
        query = self._db.query(OntologyLinkMapping).filter(
            OntologyLinkMapping.id == link_mapping_id,
            OntologyLinkMapping.ontology_id == ontology_id,
        )
        if self._db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update()
        link = query.first()
        if link is None:
            raise ValueError("Link Mapping is missing")

        plan = build_link_mapping_preview(
            self._db,
            link_mapping_id=link_mapping_id,
            ontology_id=ontology_id,
            link_review_id=link_review_id,
        )
        if str(plan["preview_digest"]) != str(preview_digest):
            raise LinkApplyError("link_apply_preview_stale")
        snapshot = plan["snapshot"]
        idempotency_key = f"link-apply:{link_mapping_id}:{client_request_id}"
        existing = self._db.query(MappingLinkApplyRun).filter(
            MappingLinkApplyRun.idempotency_key == idempotency_key,
        ).first()
        if existing is not None:
            if (existing.preview_digest != str(preview_digest)
                    or existing.link_review_id != str(link_review_id)
                    or existing.ontology_id != str(ontology_id)):
                raise LinkApplyError("link_apply_idempotency_conflict")
            return existing, False

        run = MappingLinkApplyRun(
            link_mapping_id=link_mapping_id,
            link_review_id=link_review_id,
            ontology_id=ontology_id,
            src_dataset_id=str(plan["summary"]["source_dataset_id"]),
            src_review_id=str(plan["summary"]["source_review_id"]),
            src_dataset_version_id=str(plan["summary"]["source_dataset_version_id"]),
            src_mapping_id=str(plan["summary"]["source_entity_mapping_id"]),
            src_mapping_review_id=str(snapshot["source_mapping_review_id"]),
            src_mapping_apply_run_id=str(snapshot["source_mapping_apply_run_id"]),
            tgt_dataset_id=str(plan["summary"]["target_dataset_id"]),
            tgt_review_id=str(plan["summary"]["target_review_id"]),
            tgt_dataset_version_id=str(plan["summary"]["target_dataset_version_id"]),
            tgt_mapping_id=str(plan["summary"]["target_entity_mapping_id"]),
            tgt_mapping_review_id=str(snapshot["target_mapping_review_id"]),
            tgt_mapping_apply_run_id=str(snapshot["target_mapping_apply_run_id"]),
            input_snapshot_json=snapshot,
            preview_digest=str(preview_digest),
            preview_summary_json=plan["summary"],
            idempotency_key=idempotency_key,
            requested_by=str(requested_by),
            request_actor_kind="user",
            request_actor_id=str(requested_by),
            status="pending",
            execution_attempts=0,
            dispatch_attempts=0,
            next_attempt_at=_now(),
        )
        self._db.add(run)
        try:
            if commit:
                self._db.commit()
                self._db.refresh(run)
            else:
                self._db.flush()
            return run, True
        except IntegrityError:
            if not commit:
                raise
            self._db.rollback()
            existing = self._db.query(MappingLinkApplyRun).filter(
                MappingLinkApplyRun.idempotency_key == idempotency_key,
            ).first()
            if existing is not None:
                if (existing.preview_digest != str(preview_digest)
                        or existing.link_review_id != str(link_review_id)
                        or existing.ontology_id != str(ontology_id)):
                    raise LinkApplyError("link_apply_idempotency_conflict") from None
                return existing, False
            active = self._db.query(MappingLinkApplyRun).filter(
                MappingLinkApplyRun.link_mapping_id == link_mapping_id,
                MappingLinkApplyRun.status.in_(("pending", "dispatching", "queued", "running", "retry_wait")),
            ).first()
            if active is not None:
                raise LinkApplyError("link_apply_already_running") from None
            raise

    def dispatch(self, run_id: str) -> dict:
        now = _now()
        exhausted = self._db.execute(update(MappingLinkApplyRun).where(
            MappingLinkApplyRun.id == run_id,
            MappingLinkApplyRun.status.in_(("pending", "retry_wait")),
            or_(MappingLinkApplyRun.next_attempt_at.is_(None), MappingLinkApplyRun.next_attempt_at <= now),
            or_(MappingLinkApplyRun.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS,
                MappingLinkApplyRun.execution_attempts >= MAX_EXECUTION_ATTEMPTS),
        ).values(
            status="failed",
            error_code=case(
                (MappingLinkApplyRun.execution_attempts >= MAX_EXECUTION_ATTEMPTS, "execution_attempts_exhausted"),
                else_="dispatch_attempts_exhausted",
            ),
            error_message="关系映射任务重试预算已耗尽。",
            next_attempt_at=None,
            dispatch_lease_until=None,
            run_lease_until=None,
            completed_at=now,
            updated_at=now,
        ).execution_options(synchronize_session=False))
        self._db.commit()
        if exhausted.rowcount == 1:
            self._db.expire_all()
            current = self._db.get(MappingLinkApplyRun, run_id)
            return serialize_mapping_link_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

        claimed = self._db.execute(update(MappingLinkApplyRun).where(
            MappingLinkApplyRun.id == run_id,
            MappingLinkApplyRun.status.in_(("pending", "retry_wait")),
            or_(MappingLinkApplyRun.next_attempt_at.is_(None), MappingLinkApplyRun.next_attempt_at <= now),
            MappingLinkApplyRun.dispatch_attempts < MAX_DISPATCH_ATTEMPTS,
            MappingLinkApplyRun.execution_attempts < MAX_EXECUTION_ATTEMPTS,
        ).values(
            status="dispatching",
            dispatch_lease_until=now + timedelta(seconds=DISPATCH_LEASE_SECONDS),
            updated_at=now,
        ).execution_options(synchronize_session=False))
        self._db.commit()
        if claimed.rowcount != 1:
            self._db.expire_all()
            current = self._db.get(MappingLinkApplyRun, run_id)
            return serialize_mapping_link_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

        task_id = str(uuid.uuid4())
        queued_at = _now()
        queued = self._db.execute(update(MappingLinkApplyRun).where(
            MappingLinkApplyRun.id == run_id,
            MappingLinkApplyRun.status == "dispatching",
        ).values(
            status="queued",
            celery_task_id=task_id,
            dispatch_attempts=MappingLinkApplyRun.dispatch_attempts + 1,
            dispatch_lease_until=None,
            next_attempt_at=queued_at + timedelta(seconds=QUEUED_REDELIVERY_SECONDS),
            error_code=None,
            error_message=None,
            updated_at=queued_at,
        ).execution_options(synchronize_session=False))
        self._db.commit()
        if queued.rowcount != 1:
            self._db.expire_all()
            current = self._db.get(MappingLinkApplyRun, run_id)
            return serialize_mapping_link_apply_run(current) if current else {"run_id": run_id, "status": "missing"}
        try:
            task = self._dispatch_task
            if task is None:
                from app.tasks.v2.mapping_link_apply import mapping_link_apply_run_task
                task = mapping_link_apply_run_task
            task.apply_async(args=[run_id], task_id=task_id)
        except Exception as exc:
            self._db.rollback()
            current = self._db.get(MappingLinkApplyRun, run_id)
            if current and current.status == "queued" and current.celery_task_id == task_id:
                terminal = current.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS
                current.status = "failed" if terminal else "retry_wait"
                current.error_code = "dispatch_attempts_exhausted" if terminal else "dispatch_failed"
                current.error_message = _safe_message(current.error_code)
                current.next_attempt_at = None if terminal else _now() + timedelta(
                    seconds=_backoff_seconds(current.dispatch_attempts),
                )
                current.completed_at = _now() if terminal else None
                current.updated_at = _now()
                self._db.commit()
            log.error("Unable to dispatch Link Mapping Apply run %s (error_type=%s)", run_id, type(exc).__name__)
        self._db.expire_all()
        current = self._db.get(MappingLinkApplyRun, run_id)
        return serialize_mapping_link_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

    def recover_and_dispatch(self, batch_size: int = 50) -> dict:
        now = _now()
        batch_size = min(max(int(batch_size), 1), 200)
        queued_stale = now - timedelta(seconds=QUEUED_REDELIVERY_SECONDS)
        queued_expired = and_(
            MappingLinkApplyRun.status == "queued",
            or_(MappingLinkApplyRun.next_attempt_at <= now,
                and_(MappingLinkApplyRun.next_attempt_at.is_(None), MappingLinkApplyRun.updated_at <= queued_stale)),
        )
        expired = or_(
            and_(MappingLinkApplyRun.status == "dispatching",
                 or_(MappingLinkApplyRun.dispatch_lease_until <= now,
                     MappingLinkApplyRun.dispatch_lease_until.is_(None))),
            queued_expired,
            and_(MappingLinkApplyRun.status == "running",
                 or_(MappingLinkApplyRun.run_lease_until <= now,
                     MappingLinkApplyRun.run_lease_until.is_(None))),
        )
        rows = self._db.query(
            MappingLinkApplyRun.id, MappingLinkApplyRun.status,
            MappingLinkApplyRun.dispatch_attempts, MappingLinkApplyRun.execution_attempts,
        ).filter(expired).order_by(MappingLinkApplyRun.updated_at.asc()).limit(batch_size).all()
        recovered = terminalized = 0
        for row in rows:
            budget_exhausted = (
                row.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS
                or row.execution_attempts >= MAX_EXECUTION_ATTEMPTS
            )
            if budget_exhausted:
                status = "failed"
                error_code = ("execution_attempts_exhausted" if row.execution_attempts >= MAX_EXECUTION_ATTEMPTS
                              else "dispatch_attempts_exhausted")
                due, completed = None, now
            elif row.status == "running":
                status, error_code = "retry_wait", "execution_outcome_unknown"
                due, completed = now + timedelta(seconds=_backoff_seconds(row.execution_attempts)), None
            else:
                status, error_code = "pending", None
                due, completed = now, None
            updated = self._db.execute(update(MappingLinkApplyRun).where(
                MappingLinkApplyRun.id == row.id,
                expired,
                MappingLinkApplyRun.dispatch_attempts == row.dispatch_attempts,
                MappingLinkApplyRun.execution_attempts == row.execution_attempts,
            ).values(
                status=status,
                error_code=error_code,
                error_message=_safe_message(error_code),
                celery_task_id=None,
                dispatch_lease_until=None,
                run_lease_until=None,
                next_attempt_at=due,
                completed_at=completed,
                updated_at=now,
            ).execution_options(synchronize_session=False))
            if updated.rowcount == 1:
                recovered += 1
                terminalized += int(status == "failed")
                if row.status == "running":
                    _append_attempt_event(
                        self._db, run_id=row.id, attempt_number=max(int(row.execution_attempts), 1),
                        event_type="attempt_outcome_unknown", status=status,
                        actor_kind="system", actor_id="mapping-link-apply-reconciler",
                        error_code=error_code,
                    )
        if recovered:
            self._db.commit()
        else:
            self._db.rollback()

        due_rows = self._db.query(MappingLinkApplyRun.id).filter(
            MappingLinkApplyRun.status.in_(("pending", "retry_wait")),
            or_(MappingLinkApplyRun.next_attempt_at.is_(None), MappingLinkApplyRun.next_attempt_at <= now),
        ).order_by(MappingLinkApplyRun.created_at.asc(), MappingLinkApplyRun.id.asc()).limit(batch_size).all()
        dispatched = failed = 0
        for (run_id,) in due_rows:
            result = self.dispatch(run_id)
            dispatched += int(result.get("status") == "queued")
            failed += int(result.get("status") == "failed")
        return {"recovered": recovered, "scanned": len(due_rows), "dispatched": dispatched,
                "failed": failed + terminalized}

    def execute(
        self, run_id: str, *, celery_task_id: str | None = None,
        worker_actor_id: str | None = None,
    ) -> dict:
        now = _now()
        claim_query = update(MappingLinkApplyRun).where(
            MappingLinkApplyRun.id == run_id,
            MappingLinkApplyRun.status == "queued",
            MappingLinkApplyRun.execution_attempts < MAX_EXECUTION_ATTEMPTS,
            *([MappingLinkApplyRun.celery_task_id == celery_task_id] if celery_task_id else []),
        ).values(
            status="running",
            execution_attempts=MappingLinkApplyRun.execution_attempts + 1,
            started_at=now,
            run_lease_until=now + timedelta(seconds=RUN_LEASE_SECONDS),
            next_attempt_at=None,
            updated_at=now,
        )
        claimed = self._db.execute(claim_query.execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            self._db.commit()
            self._db.expire_all()
            current = self._db.get(MappingLinkApplyRun, run_id)
            return serialize_mapping_link_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

        self._db.expire_all()
        run = self._db.get(MappingLinkApplyRun, run_id)
        attempt = int(run.execution_attempts if run else 1)
        actor = str(worker_actor_id or "unreported-worker")[:200]
        if run:
            run.worker_actor_id = actor
            _append_attempt_event(
                self._db, run_id=run_id, attempt_number=attempt, event_type="attempt_started",
                status="running", actor_kind="worker", actor_id=actor, celery_task_id=celery_task_id,
            )
        self._db.commit()

        try:
            result = self._execute_pinned(run_id, worker_actor_id=actor)
        except Exception as exc:
            self._db.rollback()
            current = self._db.get(MappingLinkApplyRun, run_id)
            if current is None:
                return {"run_id": run_id, "status": "missing"}
            permanent = isinstance(exc, LinkApplyError) and exc.permanent
            error_code = exc.error_code if isinstance(exc, LinkApplyError) else "link_apply_worker_error"
            terminal = permanent or attempt >= MAX_EXECUTION_ATTEMPTS or current.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS
            if not permanent and attempt >= MAX_EXECUTION_ATTEMPTS:
                error_code = "execution_attempts_exhausted"
            elif not permanent and current.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS:
                error_code = "dispatch_attempts_exhausted"
            updated = self._db.execute(update(MappingLinkApplyRun).where(
                MappingLinkApplyRun.id == run_id,
                MappingLinkApplyRun.status == "running",
                *([MappingLinkApplyRun.celery_task_id == celery_task_id] if celery_task_id else []),
            ).values(
                status="failed" if terminal else "retry_wait",
                error_code=error_code,
                error_message=_safe_message(error_code),
                run_lease_until=None,
                completed_at=_now() if terminal else None,
                next_attempt_at=None if terminal else _now() + timedelta(seconds=_backoff_seconds(attempt)),
                updated_at=_now(),
            ).execution_options(synchronize_session=False))
            if updated.rowcount == 1:
                _append_attempt_event(
                    self._db, run_id=run_id, attempt_number=attempt, event_type="attempt_failed",
                    status="failed" if terminal else "retry_wait", actor_kind="worker", actor_id=actor,
                    celery_task_id=celery_task_id, error_code=error_code,
                )
            self._db.commit()
            self._db.expire_all()
            current = self._db.get(MappingLinkApplyRun, run_id)
            log.error("Link Mapping Apply run %s failed (error_type=%s)", run_id, type(exc).__name__)
            return serialize_mapping_link_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

        completed_at = _now()
        completed = self._db.execute(update(MappingLinkApplyRun).where(
            MappingLinkApplyRun.id == run_id,
            MappingLinkApplyRun.status == "running",
            *([MappingLinkApplyRun.celery_task_id == celery_task_id] if celery_task_id else []),
        ).values(
            status="succeeded", result_json=result, error_code=None, error_message=None,
            run_lease_until=None, next_attempt_at=None, completed_at=completed_at, updated_at=completed_at,
        ).execution_options(synchronize_session=False))
        if completed.rowcount == 1:
            _append_attempt_event(
                self._db, run_id=run_id, attempt_number=attempt, event_type="attempt_succeeded",
                status="succeeded", actor_kind="worker", actor_id=actor, celery_task_id=celery_task_id,
            )
        self._db.commit()
        self._db.expire_all()
        current = self._db.get(MappingLinkApplyRun, run_id)
        return serialize_mapping_link_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

    def _execute_pinned(self, run_id: str, *, worker_actor_id: str | None = None) -> dict:
        return self._apply_pinned(run_id, worker_actor_id=worker_actor_id)

    def _apply_pinned(self, run_id: str, *, worker_actor_id: str | None = None) -> dict:
        from app.services.v2.mapping_link_apply import apply_approved_link_mapping
        return apply_approved_link_mapping(
            self._db, run_id, worker_actor_id=worker_actor_id,
        )
