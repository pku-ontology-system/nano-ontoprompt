"""Fenced and auditable projection/recovery for Link Mapping relation facts."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.v2.mapping_link_apply_run import (
    MappingLinkApplyRun,
    MappingLinkProjectionReplayRequest,
    MappingLinkRelationChange,
    MappingLinkRelationProjectionEvent,
    MappingLinkRelationProjectionState,
)


MAX_AUTOMATIC_ATTEMPTS = 8
MAX_MANUAL_ATTEMPTS = 5
MAX_AUTOMATIC_BATCH = 200
MAX_MANUAL_BATCH = 100
PROJECTION_LEASE_SECONDS = 900
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 3600
REPLAY_REQUEST_LEASE_SECONDS = 1800
REPLAY_DISPATCH_LEASE_SECONDS = 90
REPLAY_QUEUED_REDELIVERY_SECONDS = 120
MAX_REPLAY_DISPATCH_ATTEMPTS = 20
EVENT_HISTORY_LIMIT = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _digest(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _backoff_seconds(attempt: int) -> int:
    return min(RETRY_BASE_SECONDS * (2 ** max(int(attempt) - 1, 0)), RETRY_MAX_SECONDS)


def _event_payload(event: MappingLinkRelationProjectionEvent) -> dict:
    return {
        "schema_version": 1,
        "attempt_id": event.attempt_id,
        "event_type": event.event_type,
        "change_id": event.change_id,
        "run_id": event.run_id,
        "link_mapping_id": event.link_mapping_id,
        "ontology_id": event.ontology_id,
        "relation_id": event.relation_id,
        "sequence_no": int(event.sequence_no),
        "attempt_number": int(event.attempt_number),
        "attempt_kind": event.attempt_kind,
        "actor_kind": event.actor_kind,
        "actor_id": event.actor_id,
        "reason": event.reason,
        "client_request_id": event.client_request_id,
        "request_digest": event.request_digest,
        "outcome": event.outcome,
        "error_code": event.error_code,
        "error_message": event.error_message,
        "previous_digest": event.previous_digest,
        "created_at": _timestamp(event.created_at),
    }


def _append_projection_event(
    db: Session,
    *,
    change: MappingLinkRelationChange,
    attempt_id: str,
    event_type: str,
    attempt_number: int,
    attempt_kind: str,
    actor_kind: str,
    actor_id: str,
    reason: str | None = None,
    client_request_id: str | None = None,
    request_digest: str | None = None,
    outcome: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> MappingLinkRelationProjectionEvent:
    previous = db.query(MappingLinkRelationProjectionEvent).filter(
        MappingLinkRelationProjectionEvent.change_id == change.id,
    ).order_by(MappingLinkRelationProjectionEvent.sequence_no.desc()).first()
    row = MappingLinkRelationProjectionEvent(
        attempt_id=attempt_id,
        event_type=event_type,
        change_id=str(change.id),
        run_id=str(change.run_id),
        link_mapping_id=str(change.link_mapping_id),
        ontology_id=str(change.ontology_id),
        relation_id=str(change.relation_id),
        sequence_no=int(previous.sequence_no if previous else 0) + 1,
        attempt_number=int(attempt_number),
        attempt_kind=attempt_kind,
        actor_kind=actor_kind,
        actor_id=str(actor_id or "unreported-worker")[:200],
        reason=reason,
        client_request_id=str(client_request_id)[:120] if client_request_id else None,
        request_digest=request_digest,
        outcome=outcome,
        error_code=str(error_code)[:100] if error_code else None,
        error_message=str(error_message)[:500] if error_message else None,
        previous_digest=previous.event_digest if previous else None,
        event_digest="pending",
        created_at=_now(),
    )
    row.event_digest = _digest(_event_payload(row))
    db.add(row)
    db.flush()
    return row


def _close_expired_attempt(
    db: Session,
    *,
    state: MappingLinkRelationProjectionState,
    change: MappingLinkRelationChange,
) -> None:
    attempt_id = str(state.current_attempt_id or "")
    start = db.query(MappingLinkRelationProjectionEvent).filter_by(
        attempt_id=attempt_id, event_type="started",
    ).first() if attempt_id else None
    finish = db.query(MappingLinkRelationProjectionEvent).filter_by(
        attempt_id=attempt_id, event_type="finished",
    ).first() if attempt_id else None
    if start is None:
        # 0087 rows may still hold a projection lease without the 0088 token.
        attempt_id = attempt_id or str(uuid.uuid4())
        attempt_kind = "automatic"
        attempt_number = max(int(state.attempts or 0), 1)
        _append_projection_event(
            db, change=change, attempt_id=attempt_id, event_type="started",
            attempt_number=attempt_number, attempt_kind=attempt_kind,
            actor_kind="system", actor_id="legacy-link-projection-lease",
            error_code=None,
        )
        start = db.query(MappingLinkRelationProjectionEvent).filter_by(
            attempt_id=attempt_id, event_type="started",
        ).first()
    if start is not None and finish is None:
        _append_projection_event(
            db, change=change, attempt_id=attempt_id, event_type="finished",
            attempt_number=start.attempt_number, attempt_kind=start.attempt_kind,
            actor_kind="system", actor_id="mapping-link-projection-reconciler",
            reason=start.reason, client_request_id=start.client_request_id,
            request_digest=start.request_digest, outcome="outcome_unknown",
            error_code="worker_lease_expired",
            error_message="图写入结果未知；使用稳定关系事实 ID 恢复。",
        )


def _safe_projection_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ValueError) and "endpoint" in str(exc).lower():
        return "projection_endpoints_missing", "关系端点在图数据库中不存在，投影未确认。"
    message = str(exc).lower()
    if "unavailable" in message or "connection" in message or "timeout" in message:
        return "graph_unavailable", "图数据库暂不可用，关系事实仍等待投影。"
    return "graph_projection_failed", f"关系图投影失败（{type(exc).__name__}）。"


def _apply_one_graph_change(change: MappingLinkRelationChange) -> None:
    from app.services.v2.graph.falkordb_service import FalkorDBService

    graph = FalkorDBService()
    if not graph.available:
        raise RuntimeError("FalkorDB unavailable")
    item = {
        "fact_id": change.relation_id,
        "source": change.source_entity_id,
        "target": change.target_entity_id,
        "type": change.relation_type,
        "properties": {
            "mapping_type": "link_mapping",
            "_fact_id": change.relation_id,
            "link_mapping_id": change.link_mapping_id,
            "link_apply_run_id": change.run_id,
            "source_dataset_version_id": change.source_dataset_version_id,
            "target_dataset_version_id": change.target_dataset_version_id,
        },
    }
    if change.operation == "delete":
        graph.apply_link_relation_facts(
            change.ontology_id, upserts=[], delete_fact_ids=[change.relation_id],
        )
        return
    result = graph.apply_link_relation_facts(
        change.ontology_id, upserts=[item], delete_fact_ids=[],
    )
    if int(result.get("upserted") or 0) != 1:
        raise ValueError("relationship endpoint missing from graph")


def _finalize_attempt(
    db: Session,
    *,
    change: MappingLinkRelationChange,
    attempt_id: str,
    attempt_number: int,
    attempt_kind: str,
    actor_kind: str,
    actor_id: str,
    manual_reason: str | None,
    client_request_id: str | None,
    request_digest: str | None,
    was_dead_letter: bool,
    error: Exception | None,
) -> str:
    from app.services.v2.mapping_link_apply import MAX_PROJECTION_ATTEMPTS

    now = _now()
    db.expire_all()
    state = db.get(MappingLinkRelationProjectionState, change.id)
    if state is None or state.current_attempt_id != attempt_id:
        db.rollback()
        return "lease_lost"
    if not state.lease_until or _aware(state.lease_until) <= now:
        db.rollback()
        return "lease_expired"

    if error is None:
        values = {
            "status": "projected", "current_attempt_id": None,
            "lease_until": None, "next_attempt_at": None,
            "projected_at": now, "error_code": None, "error_message": None,
            "updated_at": now,
        }
        outcome = "projected"
        error_code = error_message = None
    else:
        error_code, error_message = _safe_projection_error(error)
        automatic_exhausted = attempt_kind == "automatic" and attempt_number >= MAX_PROJECTION_ATTEMPTS
        manual_exhausted = attempt_kind == "manual" and int(state.manual_attempts or 0) >= MAX_MANUAL_ATTEMPTS
        dead_letter = automatic_exhausted or (attempt_kind == "manual" and (was_dead_letter or manual_exhausted))
        next_attempt = None if dead_letter else now + timedelta(seconds=_backoff_seconds(
            max(int(state.attempts or 0), 1),
        ))
        values = {
            "status": "dead_letter" if dead_letter else "pending",
            "current_attempt_id": None,
            "lease_until": None,
            "next_attempt_at": next_attempt,
            "error_code": "projection_attempts_exhausted" if automatic_exhausted else error_code,
            "error_message": "关系图投影自动重试次数已耗尽。" if automatic_exhausted else error_message,
            "updated_at": now,
        }
        outcome = "dead_letter" if dead_letter else "retry_wait"
        if automatic_exhausted:
            error_code, error_message = values["error_code"], values["error_message"]

    changed = db.execute(update(MappingLinkRelationProjectionState).where(
        MappingLinkRelationProjectionState.change_id == change.id,
        MappingLinkRelationProjectionState.status == "projecting",
        MappingLinkRelationProjectionState.current_attempt_id == attempt_id,
        MappingLinkRelationProjectionState.lease_until > now,
    ).values(**values).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        db.rollback()
        return "lease_lost"
    _append_projection_event(
        db, change=change, attempt_id=attempt_id, event_type="finished",
        attempt_number=attempt_number, attempt_kind=attempt_kind,
        actor_kind=actor_kind, actor_id=actor_id,
        reason=manual_reason, client_request_id=client_request_id,
        request_digest=request_digest, outcome=outcome,
        error_code=error_code, error_message=error_message,
    )
    db.commit()
    return outcome


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def project_link_relation_changes(
    db: Session,
    *,
    run: MappingLinkApplyRun,
    worker_actor_id: str,
) -> dict:
    from app.services.v2.mapping_link_apply import LinkApplyError, MAX_PROJECTION_ATTEMPTS

    total_projected = total_pending = total_dead_letter = 0
    attempts_started = 0
    while attempts_started < MAX_AUTOMATIC_BATCH:
        now = _now()
        query = db.query(MappingLinkRelationProjectionState, MappingLinkRelationChange).join(
            MappingLinkRelationChange,
            MappingLinkRelationChange.id == MappingLinkRelationProjectionState.change_id,
        ).filter(
            MappingLinkRelationChange.run_id == run.id,
            or_(
                and_(
                    MappingLinkRelationProjectionState.status == "pending",
                    or_(MappingLinkRelationProjectionState.next_attempt_at.is_(None),
                        MappingLinkRelationProjectionState.next_attempt_at <= now),
                ),
                and_(
                    MappingLinkRelationProjectionState.status == "projecting",
                    or_(MappingLinkRelationProjectionState.lease_until.is_(None),
                        MappingLinkRelationProjectionState.lease_until <= now),
                ),
            ),
        ).order_by(MappingLinkRelationChange.sequence_no.asc()).with_for_update(skip_locked=True)
        rows = query.limit(1).all()
        if not rows:
            break

        claimed = []
        for state, change in rows:
            if state.status == "projecting":
                _close_expired_attempt(db, state=state, change=change)
            if int(state.attempts or 0) >= MAX_PROJECTION_ATTEMPTS:
                state.status = "dead_letter"
                state.current_attempt_id = None
                state.lease_until = None
                state.next_attempt_at = None
                state.error_code = "projection_attempts_exhausted"
                state.error_message = "关系图投影自动重试次数已耗尽。"
                total_dead_letter += 1
                continue
            attempt_id = str(uuid.uuid4())
            attempt_number = int(state.attempts or 0) + 1
            state.attempts = attempt_number
            state.status = "projecting"
            state.current_attempt_id = attempt_id
            state.lease_until = now + timedelta(seconds=PROJECTION_LEASE_SECONDS)
            state.next_attempt_at = None
            state.error_code = None
            state.error_message = None
            _append_projection_event(
                db, change=change, attempt_id=attempt_id, event_type="started",
                attempt_number=attempt_number, attempt_kind="automatic",
                actor_kind="worker", actor_id=worker_actor_id or "unreported-worker",
            )
            claimed.append((change, attempt_id, attempt_number))
            attempts_started += 1
        db.commit()

        for change, attempt_id, attempt_number in claimed:
            try:
                _apply_one_graph_change(change)
                error = None
            except Exception as exc:
                error = exc
            outcome = _finalize_attempt(
                db, change=change, attempt_id=attempt_id,
                attempt_number=attempt_number, attempt_kind="automatic",
                actor_kind="worker", actor_id=worker_actor_id or "unreported-worker",
                manual_reason=None, client_request_id=None, request_digest=None,
                was_dead_letter=False, error=error,
            )
            if outcome == "projected":
                total_projected += 1
            elif outcome == "dead_letter":
                total_dead_letter += 1
            elif outcome in {"retry_wait", "lease_lost", "lease_expired"}:
                total_pending += 1
        db.expire_all()

    counts = _projection_state_counts(db, run.id)
    if counts["dead_letter"]:
        raise LinkApplyError("projection_attempts_exhausted")
    if counts["pending"] or counts["projecting"]:
        raise LinkApplyError("graph_projection_pending", permanent=False)
    return {
        "projected_changes": total_projected,
        "pending_changes": total_pending,
        "dead_letter": total_dead_letter,
        "state_counts": counts,
    }


def _request_payload(request: MappingLinkProjectionReplayRequest) -> dict:
    return {
        "request_id": request.id,
        "run_id": request.run_id,
        "link_mapping_id": request.link_mapping_id,
        "ontology_id": request.ontology_id,
        "actor_id": request.actor_id,
        "actor_role": request.actor_role,
        "client_request_id": request.client_request_id,
        "reason": request.reason,
        "request_digest": request.request_digest,
        "authorization_scope": request.authorization_scope_json,
        "status": request.status,
        "batch_size": int(request.batch_size or MAX_MANUAL_BATCH),
        "dispatch_attempts": int(request.dispatch_attempts or 0),
        "celery_task_id": request.celery_task_id,
        "error_code": request.error_code,
        "next_attempt_at": request.next_attempt_at.isoformat() if request.next_attempt_at else None,
        "selected_count": request.selected_count,
        "projected_count": request.projected_count,
        "failed_count": request.failed_count,
        "pending_count": request.pending_count,
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "completed_at": request.completed_at.isoformat() if request.completed_at else None,
    }


def _projection_state_counts(db: Session, run_id: str) -> dict[str, int]:
    values = {"pending": 0, "projecting": 0, "projected": 0, "dead_letter": 0}
    rows = db.query(
        MappingLinkRelationProjectionState.status,
        func.count(MappingLinkRelationProjectionState.change_id),
    ).join(
        MappingLinkRelationChange,
        MappingLinkRelationChange.id == MappingLinkRelationProjectionState.change_id,
    ).filter(MappingLinkRelationChange.run_id == run_id).group_by(
        MappingLinkRelationProjectionState.status,
    ).all()
    values.update({str(status): int(count) for status, count in rows})
    return values


def _event_history(db: Session, run_id: str, limit: int = EVENT_HISTORY_LIMIT) -> tuple[list[dict], int, bool | None]:
    total = int(db.query(func.count(MappingLinkRelationProjectionEvent.id)).filter(
        MappingLinkRelationProjectionEvent.run_id == run_id,
    ).scalar() or 0)
    bounded = min(max(int(limit), 1), EVENT_HISTORY_LIMIT)
    rows = db.query(MappingLinkRelationProjectionEvent).filter(
        MappingLinkRelationProjectionEvent.run_id == run_id,
    ).order_by(
        MappingLinkRelationProjectionEvent.created_at.desc(),
        MappingLinkRelationProjectionEvent.id.desc(),
    ).limit(bounded + 1).all()
    truncated = total > bounded
    rows = list(reversed(rows[:bounded]))
    valid: bool | None = None
    if not truncated:
        expected_by_change: dict[str, int] = {}
        digest_by_change: dict[str, str | None] = {}
        valid = True
        for row in sorted(rows, key=lambda item: (item.change_id, item.sequence_no)):
            expected = expected_by_change.get(row.change_id, 0) + 1
            previous = digest_by_change.get(row.change_id)
            if (row.sequence_no != expected or row.previous_digest != previous
                    or _digest(_event_payload(row)) != row.event_digest):
                valid = False
            expected_by_change[row.change_id] = expected
            digest_by_change[row.change_id] = row.event_digest
    return ([{
        "event_id": row.id,
        "attempt_id": row.attempt_id,
        "change_id": row.change_id,
        "relation_id": row.relation_id,
        "sequence_no": row.sequence_no,
        "attempt_number": row.attempt_number,
        "attempt_kind": row.attempt_kind,
        "event_type": row.event_type,
        "actor_kind": row.actor_kind,
        "actor_id": row.actor_id,
        "reason": row.reason,
        "client_request_id": row.client_request_id,
        "outcome": row.outcome,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "previous_digest": row.previous_digest,
        "event_digest": row.event_digest,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in rows], total, valid)


def mapping_link_projection_summary(db: Session, run_id: str) -> dict:
    run = db.get(MappingLinkApplyRun, run_id)
    if run is None:
        raise LookupError("Link Apply run not found")
    counts = _projection_state_counts(db, run_id)
    rows = db.query(MappingLinkRelationProjectionState).join(
        MappingLinkRelationChange,
        MappingLinkRelationChange.id == MappingLinkRelationProjectionState.change_id,
    ).filter(MappingLinkRelationChange.run_id == run_id).all()
    events, event_count, history_valid = _event_history(db, run_id)
    if history_valid is not None:
        start_counts = dict(db.query(
            MappingLinkRelationProjectionEvent.change_id,
            func.count(MappingLinkRelationProjectionEvent.id),
        ).filter(
            MappingLinkRelationProjectionEvent.run_id == run_id,
            MappingLinkRelationProjectionEvent.event_type == "started",
        ).group_by(MappingLinkRelationProjectionEvent.change_id).all())
        if any(
            int(start_counts.get(str(item.change_id), 0))
            != int(item.attempts or 0) + int(item.manual_attempts or 0)
            for item in rows
        ):
            history_valid = False
    requests = db.query(MappingLinkProjectionReplayRequest).filter(
        MappingLinkProjectionReplayRequest.run_id == run_id,
    ).order_by(
        MappingLinkProjectionReplayRequest.created_at.desc(),
        MappingLinkProjectionReplayRequest.id.desc(),
    ).limit(20).all()
    return {
        "run_id": run_id,
        "change_count": len(rows),
        "state_counts": counts,
        "pending_changes": counts["pending"] + counts["projecting"],
        "dead_letter_changes": counts["dead_letter"],
        "automatic_attempts": sum(int(item.attempts or 0) for item in rows),
        "manual_attempts": sum(int(item.manual_attempts or 0) for item in rows),
        "max_automatic_attempts_per_relation": MAX_AUTOMATIC_ATTEMPTS,
        "max_manual_attempts_per_relation": MAX_MANUAL_ATTEMPTS,
        "attempt_event_count": event_count,
        "attempt_history_truncated": event_count > EVENT_HISTORY_LIMIT,
        "attempt_history_valid": history_valid,
        "attempt_events": events,
        "manual_replays": [_request_payload(item) for item in requests],
    }


def _request_digest(
    *, run_id: str, actor_id: str, actor_role: str, client_request_id: str,
    reason: str, authorization_scope: dict, batch_size: int,
) -> str:
    return _digest({
        "schema_version": 1,
        "run_id": str(run_id),
        "actor_id": str(actor_id),
        "actor_role": str(actor_role),
        "client_request_id": str(client_request_id),
        "reason": reason.strip(),
        "authorization_scope": authorization_scope,
        "batch_size": int(batch_size),
    })


def _manual_request_result(
    request: MappingLinkProjectionReplayRequest,
    *,
    in_progress: bool | None = None,
    replayed: bool = False,
) -> dict:
    active = request.status not in {"completed", "partial", "failed"}
    payload = _request_payload(request)
    payload["execution_state"] = (
        "queued" if request.status in {"pending", "dispatching", "queued", "retry_wait"}
        else "running" if request.status == "running"
        else request.status
    )
    return {
        **payload,
        "in_progress": active if in_progress is None else in_progress,
        "replayed": replayed,
    }


def _validate_replay_request(
    *, actor_id: str, actor_role: str, client_request_id: str, reason: str,
    batch_size: int,
) -> tuple[str, str, int]:
    if actor_role != "admin" or not actor_id:
        raise PermissionError("Administrator role is required for Link projection replay")
    safe_reason = str(reason or "").strip()
    safe_request_id = str(client_request_id or "").strip()
    if not safe_reason or len(safe_reason) > 2000:
        raise ValueError("Replay reason must contain 1 to 2000 characters")
    if not safe_request_id or len(safe_request_id) > 120:
        raise ValueError("client_request_id must contain 1 to 120 characters")
    return safe_reason, safe_request_id, min(max(int(batch_size), 1), MAX_MANUAL_BATCH)


def replay_link_relation_projection(
    db: Session,
    *,
    run_id: str,
    actor_id: str,
    actor_role: str,
    client_request_id: str,
    reason: str,
    authorization_scope: dict,
    batch_size: int = MAX_MANUAL_BATCH,
    dispatch_task=None,
) -> dict:
    """Persist an authorized replay request, then dispatch it as background work."""
    safe_reason, safe_request_id, batch_size = _validate_replay_request(
        actor_id=actor_id, actor_role=actor_role, client_request_id=client_request_id,
        reason=reason, batch_size=batch_size,
    )
    run = db.get(MappingLinkApplyRun, run_id)
    if run is None:
        raise LookupError("Link Apply run not found")
    expected_scope = {
        "run_id": str(run.id),
        "ontology_id": str(run.ontology_id),
        "link_mapping_id": str(run.link_mapping_id),
    }
    if any(str(authorization_scope.get(key) or "") != value for key, value in expected_scope.items()):
        raise PermissionError("Replay authorization scope does not match the persisted Apply run")
    payload_digest = _request_digest(
        run_id=run_id, actor_id=str(actor_id), actor_role=actor_role,
        client_request_id=safe_request_id, reason=safe_reason,
        authorization_scope=expected_scope, batch_size=batch_size,
    )

    query = db.query(MappingLinkProjectionReplayRequest).filter(
        MappingLinkProjectionReplayRequest.run_id == run_id,
        MappingLinkProjectionReplayRequest.actor_id == str(actor_id),
        MappingLinkProjectionReplayRequest.client_request_id == safe_request_id,
    )
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    request = query.first()
    replayed = request is not None
    if request is not None:
        if request.request_digest != payload_digest:
            raise ValueError("client_request_id is already bound to a different replay request")
    else:
        now = _now()
        request = MappingLinkProjectionReplayRequest(
            run_id=str(run.id), link_mapping_id=str(run.link_mapping_id), ontology_id=str(run.ontology_id),
            actor_id=str(actor_id), actor_role=actor_role, client_request_id=safe_request_id,
            reason=safe_reason, request_digest=payload_digest,
            authorization_scope_json=expected_scope, status="pending", batch_size=batch_size,
            dispatch_attempts=0, selected_count=0, projected_count=0, failed_count=0, pending_count=0,
            created_at=now, updated_at=now, next_attempt_at=now,
        )
        db.add(request)
        try:
            db.commit()
            db.refresh(request)
        except IntegrityError:
            db.rollback()
            request = db.query(MappingLinkProjectionReplayRequest).filter(
                MappingLinkProjectionReplayRequest.run_id == run_id,
                MappingLinkProjectionReplayRequest.actor_id == str(actor_id),
                MappingLinkProjectionReplayRequest.client_request_id == safe_request_id,
            ).first()
            if request is None or request.request_digest != payload_digest:
                raise ValueError("client_request_id conflicts with another replay request") from None
            replayed = True

    result = dispatch_link_projection_replay_request(db, str(request.id), dispatch_task=dispatch_task)
    result["replayed"] = replayed or bool(result.get("replayed"))
    if result.get("status") in {"completed", "partial", "failed"}:
        result["projection"] = mapping_link_projection_summary(db, run_id)
    return result


def dispatch_link_projection_replay_request(db: Session, request_id: str, *, dispatch_task=None) -> dict:
    """Lease and publish a persisted replay request; duplicate publication is safe."""
    now = _now()
    request = db.get(MappingLinkProjectionReplayRequest, request_id)
    if request is None:
        return {"request_id": request_id, "status": "missing", "in_progress": False}
    if request.status in {"completed", "partial", "failed"}:
        return _manual_request_result(request, replayed=True)

    due = or_(
        and_(MappingLinkProjectionReplayRequest.status.in_(("pending", "retry_wait")),
             or_(MappingLinkProjectionReplayRequest.next_attempt_at.is_(None),
                 MappingLinkProjectionReplayRequest.next_attempt_at <= now)),
        and_(MappingLinkProjectionReplayRequest.status == "queued",
             or_(MappingLinkProjectionReplayRequest.next_attempt_at.is_(None),
                 MappingLinkProjectionReplayRequest.next_attempt_at <= now)),
        and_(MappingLinkProjectionReplayRequest.status == "dispatching",
             or_(MappingLinkProjectionReplayRequest.dispatch_lease_until.is_(None),
                 MappingLinkProjectionReplayRequest.dispatch_lease_until <= now)),
    )
    if request.dispatch_attempts >= MAX_REPLAY_DISPATCH_ATTEMPTS and request.status in {
        "pending", "retry_wait", "queued", "dispatching",
    } and (request.next_attempt_at is None or _aware(request.next_attempt_at) <= now):
        exhausted = db.execute(update(MappingLinkProjectionReplayRequest).where(
            MappingLinkProjectionReplayRequest.id == request_id,
            due,
            MappingLinkProjectionReplayRequest.dispatch_attempts >= MAX_REPLAY_DISPATCH_ATTEMPTS,
        ).values(
            status="failed", error_code="replay_dispatch_attempts_exhausted",
            completed_at=now, updated_at=now, next_attempt_at=None,
            dispatch_lease_token=None, dispatch_lease_until=None,
        ).execution_options(synchronize_session=False))
        db.commit()
        if exhausted.rowcount:
            db.expire_all()
            current = db.get(MappingLinkProjectionReplayRequest, request_id)
            return _manual_request_result(current) if current else {"request_id": request_id, "status": "missing"}

    dispatch_token = str(uuid.uuid4())
    claimed = db.execute(update(MappingLinkProjectionReplayRequest).where(
        MappingLinkProjectionReplayRequest.id == request_id,
        due,
        MappingLinkProjectionReplayRequest.dispatch_attempts < MAX_REPLAY_DISPATCH_ATTEMPTS,
    ).values(
        status="dispatching", dispatch_lease_token=dispatch_token,
        dispatch_lease_until=now + timedelta(seconds=REPLAY_DISPATCH_LEASE_SECONDS),
        updated_at=now,
    ).execution_options(synchronize_session=False))
    db.commit()
    if claimed.rowcount != 1:
        db.expire_all()
        current = db.get(MappingLinkProjectionReplayRequest, request_id)
        return _manual_request_result(current, replayed=True) if current else {"request_id": request_id, "status": "missing"}

    task_id = str(uuid.uuid4())
    queued_at = _now()
    queued = db.execute(update(MappingLinkProjectionReplayRequest).where(
        MappingLinkProjectionReplayRequest.id == request_id,
        MappingLinkProjectionReplayRequest.status == "dispatching",
        MappingLinkProjectionReplayRequest.dispatch_lease_token == dispatch_token,
    ).values(
        status="queued", celery_task_id=task_id,
        dispatch_attempts=MappingLinkProjectionReplayRequest.dispatch_attempts + 1,
        dispatch_lease_token=None, dispatch_lease_until=None,
        next_attempt_at=queued_at + timedelta(seconds=REPLAY_QUEUED_REDELIVERY_SECONDS),
        error_code=None, updated_at=queued_at,
    ).execution_options(synchronize_session=False))
    db.commit()
    if queued.rowcount != 1:
        db.expire_all()
        current = db.get(MappingLinkProjectionReplayRequest, request_id)
        return _manual_request_result(current, replayed=True) if current else {"request_id": request_id, "status": "missing"}
    try:
        task = dispatch_task
        if task is None:
            from app.tasks.v2.mapping_link_apply import mapping_link_projection_replay_task
            task = mapping_link_projection_replay_task
        task.apply_async(args=[request_id], task_id=task_id)
    except Exception:
        db.rollback()
        current = db.get(MappingLinkProjectionReplayRequest, request_id)
        if current and current.status == "queued" and current.celery_task_id == task_id:
            terminal = current.dispatch_attempts >= MAX_REPLAY_DISPATCH_ATTEMPTS
            current.status = "failed" if terminal else "retry_wait"
            current.error_code = "replay_dispatch_attempts_exhausted" if terminal else "replay_dispatch_failed"
            current.next_attempt_at = None if terminal else _now() + timedelta(
                seconds=_backoff_seconds(current.dispatch_attempts),
            )
            current.completed_at = _now() if terminal else None
            current.updated_at = _now()
            current.celery_task_id = None
            db.commit()
        db.expire_all()
        current = db.get(MappingLinkProjectionReplayRequest, request_id)
        return _manual_request_result(current) if current else {"request_id": request_id, "status": "missing"}
    db.expire_all()
    current = db.get(MappingLinkProjectionReplayRequest, request_id)
    return _manual_request_result(current, in_progress=True) if current else {"request_id": request_id, "status": "missing"}


def execute_link_projection_replay_request(
    db: Session,
    request_id: str,
    *,
    celery_task_id: str | None,
    worker_actor_id: str | None = None,
) -> dict:
    """Claim one queued request and replay its bounded batch with relation-level fencing."""
    now = _now()
    worker_token = str(uuid.uuid4())
    claim = update(MappingLinkProjectionReplayRequest).where(
        MappingLinkProjectionReplayRequest.id == request_id,
        MappingLinkProjectionReplayRequest.status == "queued",
        *([MappingLinkProjectionReplayRequest.celery_task_id == celery_task_id] if celery_task_id else []),
    ).values(
        status="running", lease_token=worker_token,
        lease_until=now + timedelta(seconds=REPLAY_REQUEST_LEASE_SECONDS),
        next_attempt_at=None, updated_at=now,
    )
    claimed = db.execute(claim.execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        db.commit()
        db.expire_all()
        current = db.get(MappingLinkProjectionReplayRequest, request_id)
        return _manual_request_result(current, replayed=True) if current else {"request_id": request_id, "status": "missing"}
    db.commit()
    db.expire_all()
    request = db.get(MappingLinkProjectionReplayRequest, request_id)
    if request is None:
        return {"request_id": request_id, "status": "missing"}
    run = db.get(MappingLinkApplyRun, request.run_id)
    expected_scope = {
        "run_id": str(request.run_id),
        "ontology_id": str(request.ontology_id),
        "link_mapping_id": str(request.link_mapping_id),
    }
    if (run is None or request.actor_role != "admin"
            or request.authorization_scope_json != expected_scope
            or str(run.ontology_id) != expected_scope["ontology_id"]
            or str(run.link_mapping_id) != expected_scope["link_mapping_id"]
            or request.request_digest != _request_digest(
                run_id=str(request.run_id), actor_id=str(request.actor_id), actor_role=request.actor_role,
                client_request_id=str(request.client_request_id), reason=str(request.reason),
                authorization_scope=expected_scope, batch_size=int(request.batch_size),
            )):
        db.execute(update(MappingLinkProjectionReplayRequest).where(
            MappingLinkProjectionReplayRequest.id == request_id,
            MappingLinkProjectionReplayRequest.status == "running",
            MappingLinkProjectionReplayRequest.lease_token == worker_token,
        ).values(
            status="failed", error_code="replay_request_invalid", completed_at=_now(),
            lease_token=None, lease_until=None, updated_at=_now(),
        ).execution_options(synchronize_session=False))
        db.commit()
        db.expire_all()
        current = db.get(MappingLinkProjectionReplayRequest, request_id)
        return _manual_request_result(current) if current else {"request_id": request_id, "status": "missing"}

    batch_size = min(max(int(request.batch_size), 1), MAX_MANUAL_BATCH)
    actor_id = str(request.actor_id)
    safe_reason = str(request.reason)
    safe_request_id = str(request.client_request_id)
    payload_digest = str(request.request_digest)
    claimed_count = 0
    processed_change_ids: set[str] = set()
    for _ in range(batch_size):
        now = _now()
        db.expire_all()
        owns_request = db.query(MappingLinkProjectionReplayRequest.id).filter(
            MappingLinkProjectionReplayRequest.id == request_id,
            MappingLinkProjectionReplayRequest.status == "running",
            MappingLinkProjectionReplayRequest.lease_token == worker_token,
            MappingLinkProjectionReplayRequest.lease_until > now,
        ).first()
        if owns_request is None:
            break
        rows_query = db.query(MappingLinkRelationProjectionState, MappingLinkRelationChange).join(
            MappingLinkRelationChange,
            MappingLinkRelationChange.id == MappingLinkRelationProjectionState.change_id,
        ).filter(
            MappingLinkRelationChange.run_id == run.id,
            *([MappingLinkRelationProjectionState.change_id.notin_(processed_change_ids)] if processed_change_ids else []),
            MappingLinkRelationProjectionState.manual_attempts < MAX_MANUAL_ATTEMPTS,
            or_(
                MappingLinkRelationProjectionState.status.in_(("pending", "dead_letter")),
                and_(
                    MappingLinkRelationProjectionState.status == "projecting",
                    or_(MappingLinkRelationProjectionState.lease_until.is_(None),
                        MappingLinkRelationProjectionState.lease_until <= now),
                ),
            ),
        ).order_by(MappingLinkRelationChange.sequence_no.asc())
        if db.get_bind().dialect.name == "postgresql":
            rows_query = rows_query.with_for_update(skip_locked=True)
        row = rows_query.limit(1).first()
        if row is None:
            break
        state, change = row
        processed_change_ids.add(str(change.id))
        was_dead_letter = state.status == "dead_letter"
        if state.status == "projecting":
            _close_expired_attempt(db, state=state, change=change)
        attempt_id = str(uuid.uuid4())
        attempt_number = int(state.manual_attempts or 0) + 1
        state.manual_attempts = attempt_number
        state.status = "projecting"
        state.current_attempt_id = attempt_id
        state.lease_until = now + timedelta(seconds=PROJECTION_LEASE_SECONDS)
        state.next_attempt_at = None
        state.error_code = None
        state.error_message = None
        _append_projection_event(
            db, change=change, attempt_id=attempt_id, event_type="started",
            attempt_number=attempt_number, attempt_kind="manual",
            actor_kind="admin", actor_id=actor_id, reason=safe_reason,
            client_request_id=safe_request_id, request_digest=payload_digest,
        )
        request_claimed = db.execute(update(MappingLinkProjectionReplayRequest).where(
            MappingLinkProjectionReplayRequest.id == request_id,
            MappingLinkProjectionReplayRequest.status == "running",
            MappingLinkProjectionReplayRequest.lease_token == worker_token,
            MappingLinkProjectionReplayRequest.lease_until > now,
        ).values(
            selected_count=MappingLinkProjectionReplayRequest.selected_count + 1,
            updated_at=now,
            lease_until=now + timedelta(seconds=REPLAY_REQUEST_LEASE_SECONDS),
        ).execution_options(synchronize_session=False))
        if request_claimed.rowcount != 1:
            db.rollback()
            break
        claimed_count += 1
        db.commit()
        try:
            _apply_one_graph_change(change)
            error = None
        except Exception as exc:
            error = exc
        _finalize_attempt(
            db, change=change, attempt_id=attempt_id,
            attempt_number=attempt_number, attempt_kind="manual",
            actor_kind="admin", actor_id=actor_id,
            manual_reason=safe_reason, client_request_id=safe_request_id,
            request_digest=payload_digest, was_dead_letter=was_dead_letter,
            error=error,
        )
        db.execute(update(MappingLinkProjectionReplayRequest).where(
            MappingLinkProjectionReplayRequest.id == request_id,
            MappingLinkProjectionReplayRequest.status == "running",
            MappingLinkProjectionReplayRequest.lease_token == worker_token,
        ).values(
            updated_at=_now(),
            lease_until=_now() + timedelta(seconds=REPLAY_REQUEST_LEASE_SECONDS),
        ).execution_options(synchronize_session=False))
        db.commit()

    db.expire_all()
    request = db.get(MappingLinkProjectionReplayRequest, request_id)
    if request is None:
        raise RuntimeError("Manual replay request disappeared")
    started_change_ids = [str(row[0]) for row in db.query(
        MappingLinkRelationProjectionEvent.change_id,
    ).filter(
        MappingLinkRelationProjectionEvent.run_id == run.id,
        MappingLinkRelationProjectionEvent.request_digest == payload_digest,
        MappingLinkRelationProjectionEvent.event_type == "started",
    ).distinct().all()]
    request_states = db.query(MappingLinkRelationProjectionState).filter(
        MappingLinkRelationProjectionState.change_id.in_(started_change_ids or ["__none__"]),
    ).all()
    projected_count = sum(item.status == "projected" for item in request_states)
    failed_count = sum(
        item.status == "dead_letter" or (item.status == "pending" and item.error_code is not None)
        for item in request_states
    )
    pending_count = sum(item.status in {"pending", "projecting"} for item in request_states)
    budget_exhausted = int(db.query(func.count(MappingLinkRelationProjectionState.change_id)).join(
        MappingLinkRelationChange,
        MappingLinkRelationChange.id == MappingLinkRelationProjectionState.change_id,
    ).filter(
        MappingLinkRelationChange.run_id == run.id,
        MappingLinkRelationProjectionState.status.in_(("pending", "dead_letter")),
        MappingLinkRelationProjectionState.manual_attempts >= MAX_MANUAL_ATTEMPTS,
    ).scalar() or 0)
    completed_at = _now()
    final_status = "partial" if failed_count or pending_count or budget_exhausted else "completed"
    finalized = db.execute(update(MappingLinkProjectionReplayRequest).where(
        MappingLinkProjectionReplayRequest.id == request_id,
        MappingLinkProjectionReplayRequest.status == "running",
        MappingLinkProjectionReplayRequest.lease_token == worker_token,
    ).values(
        status=final_status,
        selected_count=len(started_change_ids),
        projected_count=projected_count,
        failed_count=failed_count,
        pending_count=pending_count,
        completed_at=completed_at,
        updated_at=completed_at,
        lease_token=None,
        lease_until=None,
        next_attempt_at=None,
        celery_task_id=None,
        error_code=None,
    ).execution_options(synchronize_session=False))
    db.commit()
    if finalized.rowcount != 1:
        db.expire_all()
        current = db.get(MappingLinkProjectionReplayRequest, request_id)
        return _manual_request_result(current, replayed=True) if current else {"request_id": request_id, "status": "missing"}
    db.expire_all()
    request = db.get(MappingLinkProjectionReplayRequest, request_id)
    result = _manual_request_result(request)
    result.update({
        "manual_budget_exhausted": budget_exhausted,
        "claimed_count": claimed_count,
        "projection": mapping_link_projection_summary(db, run.id),
    })
    return result


def reconcile_link_projection_replay_requests(db: Session, batch_size: int = 50, *, dispatch_task=None) -> dict:
    """Recover expired request/dispatch leases and republish due bounded replay work."""
    now = _now()
    batch_size = min(max(int(batch_size), 1), 200)
    rows = db.query(
        MappingLinkProjectionReplayRequest.id,
        MappingLinkProjectionReplayRequest.status,
        MappingLinkProjectionReplayRequest.dispatch_attempts,
        MappingLinkProjectionReplayRequest.lease_until,
        MappingLinkProjectionReplayRequest.dispatch_lease_until,
        MappingLinkProjectionReplayRequest.next_attempt_at,
    ).filter(or_(
        and_(MappingLinkProjectionReplayRequest.status == "dispatching",
             or_(MappingLinkProjectionReplayRequest.dispatch_lease_until.is_(None),
                 MappingLinkProjectionReplayRequest.dispatch_lease_until <= now)),
        and_(MappingLinkProjectionReplayRequest.status == "queued",
             or_(MappingLinkProjectionReplayRequest.next_attempt_at.is_(None),
                 MappingLinkProjectionReplayRequest.next_attempt_at <= now)),
        and_(MappingLinkProjectionReplayRequest.status == "running",
             or_(MappingLinkProjectionReplayRequest.lease_until.is_(None),
                 MappingLinkProjectionReplayRequest.lease_until <= now)),
    )).order_by(MappingLinkProjectionReplayRequest.updated_at.asc()).limit(batch_size).all()
    recovered = 0
    for row in rows:
        terminal = int(row.dispatch_attempts or 0) >= MAX_REPLAY_DISPATCH_ATTEMPTS
        if row.status == "running":
            status = "failed" if terminal else "retry_wait"
            error_code = "replay_dispatch_attempts_exhausted" if terminal else "replay_execution_outcome_unknown"
            due = None if terminal else now + timedelta(seconds=_backoff_seconds(row.dispatch_attempts))
        elif row.status == "queued":
            status = "failed" if terminal else "pending"
            error_code = "replay_dispatch_attempts_exhausted" if terminal else "replay_delivery_unknown"
            due = None if terminal else now
        else:
            status = "failed" if terminal else "pending"
            error_code = "replay_dispatch_attempts_exhausted" if terminal else "replay_dispatch_recovered"
            due = None if terminal else now
        where = [
            MappingLinkProjectionReplayRequest.id == row.id,
            MappingLinkProjectionReplayRequest.status == row.status,
        ]
        if row.status == "running":
            where.append(MappingLinkProjectionReplayRequest.lease_until == row.lease_until)
        elif row.status == "dispatching":
            where.append(MappingLinkProjectionReplayRequest.dispatch_lease_until == row.dispatch_lease_until)
        else:
            where.append(MappingLinkProjectionReplayRequest.next_attempt_at == row.next_attempt_at)
        changed = db.execute(update(MappingLinkProjectionReplayRequest).where(*where).values(
            status=status, error_code=error_code,
            lease_token=None, lease_until=None,
            dispatch_lease_token=None, dispatch_lease_until=None,
            celery_task_id=None, next_attempt_at=due,
            completed_at=now if terminal else None, updated_at=now,
        ).execution_options(synchronize_session=False))
        recovered += int(changed.rowcount == 1)
    db.commit()

    due_rows = db.query(MappingLinkProjectionReplayRequest.id).filter(
        MappingLinkProjectionReplayRequest.status.in_(("pending", "retry_wait")),
        MappingLinkProjectionReplayRequest.dispatch_attempts < MAX_REPLAY_DISPATCH_ATTEMPTS,
        or_(MappingLinkProjectionReplayRequest.next_attempt_at.is_(None),
            MappingLinkProjectionReplayRequest.next_attempt_at <= now),
    ).order_by(
        MappingLinkProjectionReplayRequest.created_at.asc(),
        MappingLinkProjectionReplayRequest.id.asc(),
    ).limit(batch_size).all()
    dispatched = failed = 0
    for (request_id,) in due_rows:
        result = dispatch_link_projection_replay_request(db, str(request_id), dispatch_task=dispatch_task)
        dispatched += int(result.get("status") == "queued")
        failed += int(result.get("status") == "failed")
    return {"recovered": recovered, "scanned": len(due_rows), "dispatched": dispatched, "failed": failed}
