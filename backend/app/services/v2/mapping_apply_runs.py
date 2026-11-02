"""Durable outbox, idempotency and recovery for Mapping Apply."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.v2.mapping_apply_run import MappingApplyRun, MappingApplyRunAttemptEvent

log = logging.getLogger(__name__)

MAX_EXECUTION_ATTEMPTS = 8
MAX_DISPATCH_ATTEMPTS = 20
DISPATCH_LEASE_SECONDS = 90
QUEUED_REDELIVERY_SECONDS = 120
RUN_LEASE_SECONDS = 30 * 60

_SAFE_ERROR_MESSAGES = {
    "dispatch_attempts_exhausted": "任务派发重试次数已耗尽。",
    "execution_attempts_exhausted": "任务执行重试次数已耗尽。",
    "dispatch_failed": "任务派发失败，系统将在预算允许时重试。",
    "execution_outcome_unknown": "工作进程租约过期，最近一次执行结果未知。",
    "permission_error": "权限或审核状态发生变化，任务已停止。",
    "invalid_mapping_input": "固定的映射定义或数据输入已不再有效。",
    "worker_execution_error": "任务执行失败。详细诊断信息已隐藏。",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff_seconds(attempt: int) -> int:
    return min(30 * (2 ** max(attempt - 1, 0)), 3600)


def _safe_error_message(error_code: str | None) -> str | None:
    if not error_code:
        return None
    return _SAFE_ERROR_MESSAGES.get(
        str(error_code), "任务执行失败。详细诊断信息已隐藏。",
    )


def _timestamp_for_digest(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _attempt_event_payload(
    *,
    run_id: str,
    sequence_no: int,
    attempt_number: int,
    digest_version: int,
    event_type: str,
    status: str,
    actor_kind: str,
    actor_id: str,
    celery_task_id: str | None,
    error_code: str | None,
    previous_digest: str | None,
    occurred_at: datetime,
) -> dict:
    return {
        "schema_version": int(digest_version),
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
        "occurred_at": _timestamp_for_digest(occurred_at),
    }


def _digest_attempt_event(payload: dict) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _attempt_budget_error(dispatch_attempts: int, execution_attempts: int) -> tuple[str, str] | None:
    if execution_attempts >= MAX_EXECUTION_ATTEMPTS:
        return (
            "execution_attempts_exhausted",
            f"Mapping Apply execution attempt budget exhausted ({MAX_EXECUTION_ATTEMPTS}).",
        )
    if dispatch_attempts >= MAX_DISPATCH_ATTEMPTS:
        return (
            "dispatch_attempts_exhausted",
            f"Mapping Apply dispatch attempt budget exhausted ({MAX_DISPATCH_ATTEMPTS}).",
        )
    return None


def mapping_definition_snapshot(mapping) -> tuple[dict, str]:
    snapshot = {
        "mapping_id": mapping.id,
        "ontology_id": mapping.ontology_id,
        "curated_dataset_id": mapping.curated_dataset_id,
        "entity_class": mapping.entity_class,
        "field_mapping": dict(mapping.field_mapping or {}),
        "confidence": mapping.confidence,
    }
    serialized = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return snapshot, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _append_attempt_event(
    db: Session,
    *,
    run_id: str,
    attempt_number: int,
    event_type: str,
    status: str,
    actor_kind: str,
    actor_id: str,
    celery_task_id: str | None = None,
    error_code: str | None = None,
) -> MappingApplyRunAttemptEvent:
    previous = db.query(MappingApplyRunAttemptEvent).filter(
        MappingApplyRunAttemptEvent.run_id == run_id,
    ).order_by(MappingApplyRunAttemptEvent.sequence_no.desc()).first()
    sequence_no = int(previous.sequence_no if previous else 0) + 1
    previous_digest = previous.event_digest if previous else None
    occurred_at = _now()
    safe_actor_id = str(actor_id or "unreported-worker")[:200]
    safe_task_id = str(celery_task_id)[:200] if celery_task_id else None
    safe_error_code = str(error_code)[:100] if error_code else None
    payload = _attempt_event_payload(
        run_id=run_id,
        sequence_no=sequence_no,
        attempt_number=attempt_number,
        digest_version=1,
        event_type=event_type,
        status=status,
        actor_kind=actor_kind,
        actor_id=safe_actor_id,
        celery_task_id=safe_task_id,
        error_code=safe_error_code,
        previous_digest=previous_digest,
        occurred_at=occurred_at,
    )
    event_digest = _digest_attempt_event(payload)
    row = MappingApplyRunAttemptEvent(
        run_id=str(run_id),
        sequence_no=sequence_no,
        attempt_number=int(attempt_number),
        digest_version=1,
        event_type=event_type,
        status=status,
        actor_kind=actor_kind,
        actor_id=safe_actor_id,
        celery_task_id=safe_task_id,
        error_code=safe_error_code,
        previous_digest=previous_digest,
        event_digest=event_digest,
        occurred_at=occurred_at,
    )
    db.add(row)
    db.flush()
    return row


def _serialize_attempt_history(run: MappingApplyRun) -> tuple[list[dict], str, bool | None]:
    events = list(run.attempt_events or [])
    previous_digest = None
    valid = True
    serialized = []
    starts: set[int] = set()
    terminals: dict[int, int] = {}
    for expected_sequence, item in enumerate(events, start=1):
        payload = _attempt_event_payload(
            run_id=item.run_id,
            sequence_no=item.sequence_no,
            attempt_number=item.attempt_number,
            digest_version=item.digest_version,
            event_type=item.event_type,
            status=item.status,
            actor_kind=item.actor_kind,
            actor_id=item.actor_id,
            celery_task_id=item.celery_task_id,
            error_code=item.error_code,
            previous_digest=item.previous_digest,
            occurred_at=item.occurred_at,
        )
        if (item.sequence_no != expected_sequence
                or item.previous_digest != previous_digest
                or _digest_attempt_event(payload) != item.event_digest):
            valid = False
        previous_digest = item.event_digest
        if item.event_type == "attempt_started":
            if item.attempt_number in starts:
                valid = False
            starts.add(item.attempt_number)
            if item.status != "running" or item.actor_kind != "worker":
                valid = False
        elif item.event_type == "attempt_succeeded":
            terminals[item.attempt_number] = terminals.get(item.attempt_number, 0) + 1
            if item.status != "succeeded" or item.actor_kind != "worker":
                valid = False
        elif item.event_type == "attempt_failed":
            terminals[item.attempt_number] = terminals.get(item.attempt_number, 0) + 1
            if item.status not in {"retry_wait", "failed"} or item.actor_kind != "worker":
                valid = False
        elif item.event_type == "attempt_outcome_unknown":
            terminals[item.attempt_number] = terminals.get(item.attempt_number, 0) + 1
            if item.status not in {"retry_wait", "failed"} or item.actor_kind != "system":
                valid = False
        else:
            valid = False
        serialized.append({
            "sequence_no": item.sequence_no,
            "attempt_number": item.attempt_number,
            "digest_version": item.digest_version,
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

    if not events and run.execution_attempts:
        return serialized, "legacy_untracked", None
    expected_attempts = set(range(1, int(run.execution_attempts or 0) + 1))
    if starts != expected_attempts:
        valid = False
    if any(count != 1 for count in terminals.values()):
        valid = False
    open_attempts = expected_attempts - set(terminals)
    if (run.status == "running" and run.execution_attempts >= 1
            and open_attempts == {int(run.execution_attempts)}):
        state = "in_progress"
    elif run.status != "running" and not open_attempts and valid:
        state = "complete"
    else:
        state = "invalid"
        valid = False
    return serialized, state, valid


def serialize_mapping_apply_run(run: MappingApplyRun) -> dict:
    provenance = run.provenance
    attempt_events, attempt_history_state, attempt_history_valid = _serialize_attempt_history(run)
    return {
        "run_id": run.id,
        "mapping_id": run.mapping_id,
        "ontology_id": run.ontology_id,
        "curated_dataset_id": run.curated_dataset_id,
        "review_id": run.review_id,
        "mapping_review_id": provenance.mapping_review_id if provenance else None,
        "requested_by": provenance.requested_by if provenance else None,
        "request_actor_kind": provenance.request_actor_kind if provenance else None,
        "request_actor_id": provenance.request_actor_id if provenance else None,
        "worker_actor_id": provenance.worker_actor_id if provenance else None,
        "dataset_version_id": run.dataset_version_id,
        "mapping_snapshot": run.mapping_snapshot_json,
        "mapping_digest": run.mapping_digest,
        "status": run.status,
        "execution_attempts": run.execution_attempts,
        "dispatch_attempts": run.dispatch_attempts,
        "celery_task_id": run.celery_task_id,
        "result": run.result_json,
        "error_code": run.error_code,
        # Older rows may contain raw exception text. Never expose it through the
        # review API; new failures persist the same safe, category-based copy.
        "error_message": _safe_error_message(run.error_code),
        "next_attempt_at": run.next_attempt_at.isoformat() if run.next_attempt_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "attempt_events": attempt_events,
        "attempt_history_state": attempt_history_state,
        "attempt_history_valid": attempt_history_valid,
    }


class MappingApplyRunService:
    def __init__(self, db: Session, *, dispatch_task=None):
        self._db = db
        self._dispatch_task = dispatch_task

    def enqueue_review_mapping(
        self,
        *,
        mapping_id: str,
        ontology_id: str,
        curated_dataset_id: str,
        review_id: str,
        mapping_review_id: str,
        dataset_version_id: str,
        requested_by: str | None,
        request_actor_kind: str,
        request_actor_id: str | None = None,
        commit: bool = True,
    ) -> tuple[MappingApplyRun, bool]:
        """Create one durable run for an approved data and Mapping revision."""
        from app.models.v2.curated import CuratedReview
        from app.models.v2.curated import CuratedDataset
        from app.models.v2.mapping import OntologyMapping
        from app.models.v2.mapping_apply_run import MappingApplyRunProvenance
        from app.services.v2.mapping_definition_reviews import approved_mapping_review_for_run

        if request_actor_kind not in {"user", "system"}:
            raise ValueError("Mapping Apply request actor must be user or system")
        if request_actor_kind == "user":
            if not requested_by:
                raise ValueError("User initiated Mapping Apply requires a requester ID")
            if request_actor_id and str(request_actor_id) != str(requested_by):
                raise ValueError("User request actor ID must match requested_by")
            request_actor_id = str(requested_by)
        elif not request_actor_id:
            raise ValueError("System initiated Mapping Apply requires a stable actor ID")

        mapping_query = self._db.query(OntologyMapping).filter(
            OntologyMapping.id == mapping_id,
            OntologyMapping.ontology_id == ontology_id,
            OntologyMapping.curated_dataset_id == curated_dataset_id,
        )
        if self._db.get_bind().dialect.name == "postgresql":
            mapping_query = mapping_query.with_for_update()
        mapping = mapping_query.first()
        if mapping is None or mapping.status == "disabled":
            raise ValueError("Mapping is missing, disabled, or does not match the requested source")
        mapping_review = approved_mapping_review_for_run(
            self._db, mapping, mapping_review_id,
        )
        curated = self._db.query(CuratedDataset).filter(
            CuratedDataset.id == curated_dataset_id,
            CuratedDataset.status == "approved",
        ).first()
        if curated is None:
            raise ValueError("Mapping source dataset is not currently approved")
        review = self._db.query(CuratedReview).filter(
            CuratedReview.id == review_id,
            CuratedReview.curated_dataset_id == curated_dataset_id,
            CuratedReview.status == "approved",
            CuratedReview.dataset_version_id == dataset_version_id,
        ).first()
        if review is None:
            raise ValueError("Mapping Apply requires an approved review for the pinned dataset version")
        if (not review.requested_by or not review.decided_by
                or str(review.requested_by) == str(review.decided_by)):
            raise ValueError("Mapping Apply requires approval by a reviewer different from the requester")
        if review.reviewer_id and str(review.reviewer_id) != str(review.decided_by):
            raise ValueError("Mapping Apply review was not approved by its assigned reviewer")
        mapping_snapshot, mapping_digest = mapping_definition_snapshot(mapping)
        if mapping_digest != mapping_review.candidate_digest:
            raise ValueError("Approved Mapping definition no longer matches the Mapping snapshot")

        idempotency_key = f"mapping-apply:{mapping_id}:{review_id}:{mapping_review_id}"
        existing = self._db.query(MappingApplyRun).filter(
            MappingApplyRun.mapping_id == mapping_id,
            MappingApplyRun.review_id == review_id,
        ).first()
        if existing:
            self._assert_same_request(
                existing, ontology_id, curated_dataset_id, dataset_version_id,
                mapping_review_id, mapping_digest,
            )
            return existing, False

        run = MappingApplyRun(
            mapping_id=mapping_id,
            ontology_id=ontology_id,
            curated_dataset_id=curated_dataset_id,
            review_id=review_id,
            dataset_version_id=dataset_version_id,
            mapping_snapshot_json=mapping_snapshot,
            mapping_digest=mapping_digest,
            idempotency_key=idempotency_key,
            status="pending",
            execution_attempts=0,
            dispatch_attempts=0,
            next_attempt_at=_now(),
        )
        run.provenance = MappingApplyRunProvenance(
            mapping_review_id=mapping_review_id,
            requested_by=str(requested_by) if requested_by is not None else None,
            request_actor_kind=request_actor_kind,
            request_actor_id=str(request_actor_id),
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
            existing = self._db.query(MappingApplyRun).filter(
                MappingApplyRun.mapping_id == mapping_id,
                MappingApplyRun.review_id == review_id,
            ).first()
            if existing is None:
                raise
            self._assert_same_request(
                existing, ontology_id, curated_dataset_id, dataset_version_id,
                mapping_review_id, mapping_digest,
            )
            return existing, False

    @staticmethod
    def _assert_same_request(
        run, ontology_id: str, dataset_id: str, version_id: str,
        mapping_review_id: str, mapping_digest: str,
    ) -> None:
        if (run.ontology_id != ontology_id or run.curated_dataset_id != dataset_id
                or run.dataset_version_id != version_id
                or run.mapping_review_id != mapping_review_id
                or run.mapping_digest != mapping_digest):
            raise ValueError("Idempotency key already exists for a different mapping request")

    def dispatch(self, run_id: str) -> dict:
        """Claim the outbox row, persist queue identity, then publish to Celery."""
        now = _now()
        exhausted = self._db.execute(update(MappingApplyRun).where(
            MappingApplyRun.id == run_id,
            MappingApplyRun.status.in_(("pending", "retry_wait")),
            or_(MappingApplyRun.next_attempt_at.is_(None), MappingApplyRun.next_attempt_at <= now),
            or_(
                MappingApplyRun.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS,
                MappingApplyRun.execution_attempts >= MAX_EXECUTION_ATTEMPTS,
            ),
        ).values(
            status="failed",
            error_code=case(
                (MappingApplyRun.execution_attempts >= MAX_EXECUTION_ATTEMPTS,
                 "execution_attempts_exhausted"),
                else_="dispatch_attempts_exhausted",
            ),
            error_message=case(
                (MappingApplyRun.execution_attempts >= MAX_EXECUTION_ATTEMPTS,
                 f"Mapping Apply execution attempt budget exhausted ({MAX_EXECUTION_ATTEMPTS})."),
                else_=f"Mapping Apply dispatch attempt budget exhausted ({MAX_DISPATCH_ATTEMPTS}).",
            ),
            next_attempt_at=None,
            dispatch_lease_until=None,
            run_lease_until=None,
            completed_at=now,
            updated_at=now,
        ).execution_options(synchronize_session=False))
        self._db.commit()
        if exhausted.rowcount == 1:
            self._db.expire_all()
            current = self._db.get(MappingApplyRun, run_id)
            return serialize_mapping_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

        claim = self._db.execute(update(MappingApplyRun).where(
            MappingApplyRun.id == run_id,
            MappingApplyRun.status.in_(("pending", "retry_wait")),
            or_(MappingApplyRun.next_attempt_at.is_(None), MappingApplyRun.next_attempt_at <= now),
            MappingApplyRun.dispatch_attempts < MAX_DISPATCH_ATTEMPTS,
            MappingApplyRun.execution_attempts < MAX_EXECUTION_ATTEMPTS,
        ).values(
            status="dispatching",
            dispatch_lease_until=now + timedelta(seconds=DISPATCH_LEASE_SECONDS),
            updated_at=now,
        ).execution_options(synchronize_session=False))
        self._db.commit()
        if claim.rowcount != 1:
            self._db.expire_all()
            current = self._db.get(MappingApplyRun, run_id)
            return serialize_mapping_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

        task_id = str(uuid.uuid4())
        queue_at = _now()
        queued = self._db.execute(update(MappingApplyRun).where(
            MappingApplyRun.id == run_id,
            MappingApplyRun.status == "dispatching",
        ).values(
            status="queued",
            celery_task_id=task_id,
            dispatch_attempts=MappingApplyRun.dispatch_attempts + 1,
            dispatch_lease_until=None,
            next_attempt_at=queue_at + timedelta(seconds=QUEUED_REDELIVERY_SECONDS),
            error_code=None,
            error_message=None,
            updated_at=queue_at,
        ).execution_options(synchronize_session=False))
        self._db.commit()
        if queued.rowcount != 1:
            self._db.expire_all()
            current = self._db.get(MappingApplyRun, run_id)
            return serialize_mapping_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

        try:
            task = self._dispatch_task
            if task is None:
                from app.tasks.v2.mapping_apply import mapping_apply_run_task
                task = mapping_apply_run_task
            task.apply_async(args=[run_id], task_id=task_id)
        except Exception as exc:
            self._db.rollback()
            current = self._db.get(MappingApplyRun, run_id)
            # The broker may have accepted the message before the client saw
            # an error. Never overwrite a worker that already claimed it.
            if current and current.status == "queued" and current.celery_task_id == task_id:
                now = _now()
                terminal = current.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS
                current.status = "failed" if terminal else "retry_wait"
                current.error_code = "dispatch_attempts_exhausted" if terminal else "dispatch_failed"
                current.error_message = _safe_error_message(current.error_code)
                current.next_attempt_at = None if terminal else now + timedelta(
                    seconds=_backoff_seconds(current.dispatch_attempts),
                )
                current.completed_at = now if terminal else None
                current.updated_at = now
                self._db.commit()
            log.error(
                "Unable to dispatch Mapping Apply run %s (error_type=%s)",
                run_id, type(exc).__name__,
            )

        self._db.expire_all()
        current = self._db.get(MappingApplyRun, run_id)
        return serialize_mapping_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

    def recover_and_dispatch(self, batch_size: int = 50) -> dict:
        now = _now()
        batch_size = min(max(int(batch_size), 1), 200)
        queued_stale = now - timedelta(seconds=QUEUED_REDELIVERY_SECONDS)
        queued_expired = and_(
            MappingApplyRun.status == "queued",
            or_(
                MappingApplyRun.next_attempt_at <= now,
                and_(MappingApplyRun.next_attempt_at.is_(None),
                     MappingApplyRun.updated_at <= queued_stale),
            ),
        )
        expired_lease = or_(
            and_(MappingApplyRun.status == "dispatching",
                 or_(MappingApplyRun.dispatch_lease_until <= now,
                     MappingApplyRun.dispatch_lease_until.is_(None))),
            queued_expired,
            and_(MappingApplyRun.status == "running",
                 or_(MappingApplyRun.run_lease_until <= now,
                     MappingApplyRun.run_lease_until.is_(None))),
        )
        recoverable = self._db.query(
            MappingApplyRun.id,
            MappingApplyRun.status,
            MappingApplyRun.dispatch_attempts,
            MappingApplyRun.execution_attempts,
        ).filter(
            expired_lease,
        ).order_by(MappingApplyRun.updated_at.asc()).limit(batch_size).all()
        recovered_count = 0
        terminalized = 0
        for row in recoverable:
            budget_error = _attempt_budget_error(row.dispatch_attempts, row.execution_attempts)
            if budget_error:
                status = "failed"
                error_code, error_message = budget_error
                next_attempt_at = None
                completed_at = now
            elif row.status == "running":
                status = "retry_wait"
                error_code = "execution_outcome_unknown"
                error_message = (
                    "Worker lease expired before a terminal result was recorded; "
                    "the prior attempt outcome is unknown."
                )
                next_attempt_at = now + timedelta(seconds=_backoff_seconds(row.execution_attempts))
                completed_at = None
            else:
                status = "pending"
                error_code = None
                error_message = None
                next_attempt_at = now
                completed_at = None

            # Recheck lease expiry in the UPDATE itself. A worker may claim or
            # finish a row after the scan but before this statement executes.
            # Counter equality also fences a concurrent reconciler decision.
            recovered = self._db.execute(update(MappingApplyRun).where(
                MappingApplyRun.id == row.id,
                expired_lease,
                MappingApplyRun.dispatch_attempts == row.dispatch_attempts,
                MappingApplyRun.execution_attempts == row.execution_attempts,
            ).values(
                status=status,
                error_code=error_code,
                error_message=error_message,
                celery_task_id=None,
                dispatch_lease_until=None,
                run_lease_until=None,
                next_attempt_at=next_attempt_at,
                completed_at=completed_at,
                updated_at=now,
            ).execution_options(synchronize_session=False))
            if recovered.rowcount == 1:
                recovered_count += 1
                terminalized += int(status == "failed")
                if row.status == "running":
                    _append_attempt_event(
                        self._db,
                        run_id=row.id,
                        attempt_number=int(row.execution_attempts),
                        event_type="attempt_outcome_unknown",
                        status=status,
                        actor_kind="system",
                        actor_id="mapping-apply-reconciler",
                        error_code="execution_outcome_unknown",
                    )
        if recovered_count:
            self._db.commit()
        else:
            self._db.rollback()

        due_rows = self._db.query(MappingApplyRun.id).filter(
            MappingApplyRun.status.in_(("pending", "retry_wait")),
            or_(MappingApplyRun.next_attempt_at.is_(None), MappingApplyRun.next_attempt_at <= now),
        ).order_by(MappingApplyRun.created_at.asc(), MappingApplyRun.id.asc()).limit(batch_size).all()
        due_ids = [row[0] for row in due_rows]
        dispatched = 0
        failed = terminalized
        for run_id in due_ids:
            result = self.dispatch(run_id)
            if result.get("status") == "queued":
                dispatched += 1
            elif result.get("status") == "failed":
                failed += 1
        return {
            "recovered": recovered_count,
            "scanned": len(due_ids),
            "dispatched": dispatched,
            "failed": failed,
        }

    def execute(
        self,
        run_id: str,
        *,
        celery_task_id: str | None = None,
        worker_actor_id: str | None = None,
    ) -> dict:
        now = _now()
        exhausted = update(MappingApplyRun).where(
            MappingApplyRun.id == run_id,
            MappingApplyRun.status == "queued",
            MappingApplyRun.execution_attempts >= MAX_EXECUTION_ATTEMPTS,
            *([MappingApplyRun.celery_task_id == celery_task_id] if celery_task_id else []),
        ).values(
            status="failed",
            error_code="execution_attempts_exhausted",
            error_message=f"Mapping Apply execution attempt budget exhausted ({MAX_EXECUTION_ATTEMPTS}).",
            next_attempt_at=None,
            dispatch_lease_until=None,
            run_lease_until=None,
            completed_at=now,
            updated_at=now,
        )
        exhausted_result = self._db.execute(exhausted.execution_options(synchronize_session=False))
        self._db.commit()
        if exhausted_result.rowcount == 1:
            self._db.expire_all()
            current = self._db.get(MappingApplyRun, run_id)
            return serialize_mapping_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

        query = update(MappingApplyRun).where(
            MappingApplyRun.id == run_id,
            MappingApplyRun.status == "queued",
            MappingApplyRun.execution_attempts < MAX_EXECUTION_ATTEMPTS,
        )
        if celery_task_id:
            query = query.where(MappingApplyRun.celery_task_id == celery_task_id)
        claimed = self._db.execute(query.values(
            status="running",
            execution_attempts=MappingApplyRun.execution_attempts + 1,
            started_at=now,
            run_lease_until=now + timedelta(seconds=RUN_LEASE_SECONDS),
            next_attempt_at=None,
            updated_at=now,
        ).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            self._db.commit()
            self._db.expire_all()
            current = self._db.get(MappingApplyRun, run_id)
            return serialize_mapping_apply_run(current) if current else {"run_id": run_id, "status": "missing"}

        self._db.expire_all()
        run = self._db.get(MappingApplyRun, run_id)
        attempt_number = run.execution_attempts if run else 0
        attempt_actor_id = str(worker_actor_id or "unreported-worker")[:200]
        if run and run.provenance:
            run.provenance.worker_actor_id = attempt_actor_id
        if run:
            _append_attempt_event(
                self._db,
                run_id=run.id,
                attempt_number=attempt_number,
                event_type="attempt_started",
                status="running",
                actor_kind="worker",
                actor_id=attempt_actor_id,
                celery_task_id=celery_task_id,
            )
        self._db.commit()
        try:
            from app.tasks.v2.mapping_apply import apply_approved_mapping
            result = apply_approved_mapping(
                self._db,
                run.mapping_id,
                run.ontology_id,
                run.dataset_version_id,
                run.review_id,
                mapping_review_id=run.mapping_review_id,
                expected_mapping_digest=run.mapping_digest,
                mapping_apply_run_id=run.id,
            )
        except Exception as exc:
            self._db.rollback()
            now = _now()
            permanent = isinstance(exc, (ValueError, TypeError, PermissionError))
            exhausted = attempt_number >= MAX_EXECUTION_ATTEMPTS
            dispatch_exhausted = run.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS
            terminal = permanent or exhausted or dispatch_exhausted
            error_code = (
                "permission_error" if isinstance(exc, PermissionError)
                else "invalid_mapping_input" if isinstance(exc, (ValueError, TypeError))
                else "worker_execution_error"
            )
            if not permanent and exhausted:
                error_code = "execution_attempts_exhausted"
            elif not permanent and dispatch_exhausted:
                error_code = "dispatch_attempts_exhausted"
            event_error_code = error_code
            error_message = _safe_error_message(error_code)
            conditions = [
                MappingApplyRun.id == run_id,
                MappingApplyRun.status == "running",
            ]
            if celery_task_id:
                conditions.append(MappingApplyRun.celery_task_id == celery_task_id)
            updated = self._db.execute(update(MappingApplyRun).where(*conditions).values(
                status="failed" if terminal else "retry_wait",
                error_code=error_code,
                error_message=error_message,
                run_lease_until=None,
                completed_at=now if terminal else None,
                next_attempt_at=None if terminal else now + timedelta(
                    seconds=_backoff_seconds(attempt_number),
                ),
                updated_at=now,
            ).execution_options(synchronize_session=False))
            if updated.rowcount != 1:
                self._db.commit()
                self._db.expire_all()
                current = self._db.get(MappingApplyRun, run_id)
                return serialize_mapping_apply_run(current) if current else {"run_id": run_id, "status": "missing"}
            _append_attempt_event(
                self._db,
                run_id=run_id,
                attempt_number=attempt_number,
                event_type="attempt_failed",
                status="failed" if terminal else "retry_wait",
                actor_kind="worker",
                actor_id=attempt_actor_id,
                celery_task_id=celery_task_id,
                error_code=event_error_code,
            )
            self._db.commit()
            self._db.expire_all()
            current = self._db.get(MappingApplyRun, run_id)
            log.error(
                "Mapping Apply run %s failed (error_type=%s)",
                run_id, type(exc).__name__,
            )
            return serialize_mapping_apply_run(current)

        completed_at = _now()
        conditions = [
            MappingApplyRun.id == run_id,
            MappingApplyRun.status == "running",
        ]
        if celery_task_id:
            conditions.append(MappingApplyRun.celery_task_id == celery_task_id)
        completed = self._db.execute(update(MappingApplyRun).where(*conditions).values(
            status="succeeded",
            result_json=result,
            error_code=None,
            error_message=None,
            run_lease_until=None,
            next_attempt_at=None,
            completed_at=completed_at,
            updated_at=completed_at,
        ).execution_options(synchronize_session=False))
        if completed.rowcount == 1:
            _append_attempt_event(
                self._db,
                run_id=run_id,
                attempt_number=attempt_number,
                event_type="attempt_succeeded",
                status="succeeded",
                actor_kind="worker",
                actor_id=attempt_actor_id,
                celery_task_id=celery_task_id,
            )
        self._db.commit()
        self._db.expire_all()
        current = self._db.get(MappingApplyRun, run_id)
        return serialize_mapping_apply_run(current) if current else {"run_id": run_id, "status": "missing"}
