"""Bounded, auditable projection of Mapping row changes into FalkorDB."""
from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.v2.mapping import OntologyMapping
from app.models.v2.mapping_apply_run import MappingApplyRun
from app.models.v2.mapping_row_change import MappingRowChange, MappingRowProjectionEvent
from app.models.v2.dataset import DatasetVersion


RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 3600
MAX_AUTOMATIC_ATTEMPTS = 8
PROJECTION_LEASE_SECONDS = 900


def _concept_id(mapping: OntologyMapping) -> str:
    digest = hashlib.md5(
        f"{mapping.ontology_id}:{mapping.entity_class}:concept".encode(),
    ).hexdigest()
    return str(uuid.UUID(digest))


def _retry_at(attempts: int, now: datetime) -> datetime:
    delay = min(RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)), RETRY_MAX_SECONDS)
    return now + timedelta(seconds=delay)


def _event_rank(change: MappingRowChange, order: dict[str, tuple[int, datetime]]) -> tuple:
    version_no, run_created_at = order.get(str(change.id), (0, change.created_at))
    return version_no, run_created_at, change.created_at, change.id


def _request_digest(row_change_id: str, run_id: str, actor_id: str, reason: str) -> str:
    canonical = "\n".join((row_change_id, run_id, actor_id, reason.strip()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_error(exc: Exception) -> tuple[str, str]:
    message = str(exc).lower()
    if "unavailable" in message or "connection" in message or "timeout" in message:
        return "graph_unavailable", "图数据库暂不可用，投影未确认完成。"
    if "wrote" in message:
        return "partial_graph_write", "图数据库返回的写入数量与请求不一致。"
    return "projection_failed", f"图投影失败（{type(exc).__name__}）。"


def _event_from_attempt(
    *,
    event_type: str,
    attempt_id: str,
    row: MappingRowChange,
    attempt_number: int,
    attempt_kind: str,
    actor_kind: str,
    actor_id: str | None,
    reason: str | None,
    client_request_id: str | None,
    request_digest: str | None,
    outcome: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> MappingRowProjectionEvent:
    return MappingRowProjectionEvent(
        attempt_id=attempt_id,
        event_type=event_type,
        row_change_id=row.id,
        mapping_apply_run_id=row.mapping_apply_run_id,
        mapping_id=row.mapping_id,
        ontology_id=row.ontology_id,
        object_id=row.object_id,
        attempt_number=attempt_number,
        attempt_kind=attempt_kind,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason=reason,
        client_request_id=client_request_id,
        request_digest=request_digest,
        outcome=outcome,
        error_code=error_code,
        error_message=error_message,
    )


def _close_expired_attempt(db: Session, row: MappingRowChange) -> None:
    attempt_id = row.projection_current_attempt_id
    if not attempt_id:
        return
    start = db.query(MappingRowProjectionEvent).filter_by(
        attempt_id=attempt_id, event_type="started",
    ).first()
    finish = db.query(MappingRowProjectionEvent).filter_by(
        attempt_id=attempt_id, event_type="finished",
    ).first()
    if start and not finish:
        db.add(_event_from_attempt(
            event_type="finished",
            attempt_id=attempt_id,
            row=row,
            attempt_number=start.attempt_number,
            attempt_kind=start.attempt_kind,
            actor_kind="system",
            actor_id=None,
            reason=start.reason,
            client_request_id=start.client_request_id,
            request_digest=start.request_digest,
            outcome="outcome_unknown",
            error_code="worker_lease_expired",
            error_message="投影 worker 租约到期，外部写入结果未知；已安全重放。",
        ))


def _manual_replay(db: Session, *, run_id: str, actor_id: str, request_id: str, reason: str) -> dict | None:
    starts = db.query(MappingRowProjectionEvent).filter(
        MappingRowProjectionEvent.mapping_apply_run_id == run_id,
        MappingRowProjectionEvent.actor_id == actor_id,
        MappingRowProjectionEvent.client_request_id == request_id,
        MappingRowProjectionEvent.event_type == "started",
    ).order_by(MappingRowProjectionEvent.created_at.asc()).all()
    if not starts:
        return None
    if any(start.reason != reason.strip() for start in starts):
        raise ValueError("client_request_id is already bound to a different manual projection request")
    if any(
        start.request_digest != _request_digest(
            start.row_change_id, run_id, actor_id, reason,
        )
        for start in starts
    ):
        raise ValueError("client_request_id is already bound to a different manual projection scope")
    outcomes = []
    in_progress = 0
    for start in starts:
        finish = db.query(MappingRowProjectionEvent).filter_by(
            attempt_id=start.attempt_id, event_type="finished",
        ).first()
        if finish is None:
            in_progress += 1
        else:
            outcomes.append(finish.outcome)
    return {
        "selected": len(starts),
        "projected": sum(outcome == "projected" for outcome in outcomes),
        "failed": sum(outcome in {"retry_wait", "dead_letter"} for outcome in outcomes),
        "dead_letter": sum(outcome == "dead_letter" for outcome in outcomes),
        "pending": in_progress,
        "replayed": True,
        "in_progress": in_progress > 0,
    }


def project_mapping_row_changes(
    db: Session,
    *,
    mapping_apply_run_id: str | None = None,
    batch_size: int = 100,
    manual_actor_id: str | None = None,
    manual_reason: str | None = None,
    manual_request_id: str | None = None,
) -> dict:
    """Project due row deltas with a finite automatic budget and manual replay.

    A durable ``started`` event and a lease are committed before the graph call.
    A second immutable ``finished`` event records the result. Expired leases are
    recovered as ``outcome_unknown`` before a new bounded attempt is claimed.
    Manual requests have their own counter and idempotency identity; they never
    reset or consume the automatic retry budget.
    """
    from app.services.v2.graph.falkordb_service import FalkorDBService

    manual = manual_actor_id is not None or manual_reason is not None or manual_request_id is not None
    if manual and (not manual_actor_id or not manual_reason or not manual_request_id):
        raise ValueError("Manual projection requires actor, reason, and client_request_id")
    reason = manual_reason.strip() if manual_reason else None
    if manual and (not reason or not reason.strip()):
        raise ValueError("Manual projection reason cannot be blank")
    if manual and mapping_apply_run_id:
        replay = _manual_replay(
            db,
            run_id=mapping_apply_run_id,
            actor_id=str(manual_actor_id),
            request_id=str(manual_request_id),
            reason=reason or "",
        )
        if replay is not None:
            return replay

    now = datetime.now(timezone.utc)
    if manual:
        eligible = or_(
            MappingRowChange.projection_status.in_(("pending", "dead_letter")),
            and_(
                MappingRowChange.projection_status == "projecting",
                MappingRowChange.projection_lease_until <= now,
            ),
        )
    else:
        eligible = or_(
            and_(
                MappingRowChange.projection_status == "pending",
                or_(
                    MappingRowChange.projection_next_attempt_at.is_(None),
                    MappingRowChange.projection_next_attempt_at <= now,
                ),
            ),
            and_(
                MappingRowChange.projection_status == "projecting",
                MappingRowChange.projection_lease_until <= now,
            ),
        )
    query = db.query(MappingRowChange).filter(eligible)
    if mapping_apply_run_id:
        query = query.filter(MappingRowChange.mapping_apply_run_id == mapping_apply_run_id)
    selected = query.order_by(
        MappingRowChange.created_at.asc(), MappingRowChange.id.asc(),
    ).with_for_update(skip_locked=True).limit(min(max(int(batch_size), 1), 500)).all()
    if not selected:
        return {"selected": 0, "projected": 0, "failed": 0, "pending": 0, "dead_letter": 0}

    pairs = {(str(change.mapping_id), str(change.object_id)) for change in selected}
    pair_filter = or_(*[
        and_(MappingRowChange.mapping_id == mapping_id, MappingRowChange.object_id == object_id)
        for mapping_id, object_id in pairs
    ])
    relevant = db.query(MappingRowChange).filter(pair_filter).all()
    order_rows = db.query(
        MappingRowChange.id, DatasetVersion.version_no, MappingApplyRun.created_at,
    ).join(
        DatasetVersion, DatasetVersion.id == MappingRowChange.dataset_version_id,
    ).join(
        MappingApplyRun, MappingApplyRun.id == MappingRowChange.mapping_apply_run_id,
    ).filter(MappingRowChange.id.in_([change.id for change in relevant])).all()
    order = {str(row[0]): (int(row[1] or 0), row[2]) for row in order_rows}

    latest_by_pair: dict[tuple[str, str], MappingRowChange] = {}
    for change in relevant:
        pair = (str(change.mapping_id), str(change.object_id))
        current = latest_by_pair.get(pair)
        if current is None or _event_rank(change, order) > _event_rank(current, order):
            latest_by_pair[pair] = change

    selected_by_pair: dict[tuple[str, str], list[MappingRowChange]] = defaultdict(list)
    for change in selected:
        selected_by_pair[(str(change.mapping_id), str(change.object_id))].append(change)

    selected_ids = {str(change.id) for change in selected}
    claimed: dict[str, list[dict]] = defaultdict(list)
    projected = deferred = dead_letter = replayed = 0
    actor_kind = "admin" if manual else "system"
    for pair, pair_rows in selected_by_pair.items():
        latest = latest_by_pair[pair]
        if latest.projection_status == "projected":
            for row in pair_rows:
                row.projection_status = "projected"
                row.projection_projected_at = latest.projection_projected_at or now
                row.projection_error = None
                row.projection_next_attempt_at = None
            projected += len(pair_rows)
            continue

        if latest.projection_status == "dead_letter" and not manual:
            for row in pair_rows:
                row.projection_status = "dead_letter"
                row.projection_next_attempt_at = None
            dead_letter += len(pair_rows)
            continue

        # An older run must never project over a newer row snapshot.
        if str(latest.id) not in selected_ids:
            due_at = latest.projection_lease_until if latest.projection_status == "projecting" else latest.projection_next_attempt_at
            due_at = due_at or _retry_at(1, now)
            for row in pair_rows:
                row.projection_next_attempt_at = due_at
            deferred += len(pair_rows)
            continue

        if latest.projection_status == "projecting" and latest.projection_lease_until and latest.projection_lease_until > now:
            for row in pair_rows:
                row.projection_next_attempt_at = latest.projection_lease_until
            deferred += len(pair_rows)
            continue

        if latest.projection_status == "projecting":
            _close_expired_attempt(db, latest)

        if not manual and (
            latest.projection_status == "dead_letter"
            or int(latest.projection_attempts or 0) >= MAX_AUTOMATIC_ATTEMPTS
        ):
            latest.projection_status = "dead_letter"
            latest.projection_lease_until = None
            latest.projection_current_attempt_id = None
            latest.projection_next_attempt_at = None
            for row in pair_rows:
                row.projection_status = "dead_letter"
                row.projection_next_attempt_at = None
            dead_letter += len(pair_rows)
            continue

        digest = None
        if manual:
            digest = _request_digest(str(latest.id), str(latest.mapping_apply_run_id), str(manual_actor_id), reason or "")
            prior = db.query(MappingRowProjectionEvent).filter(
                MappingRowProjectionEvent.row_change_id == latest.id,
                MappingRowProjectionEvent.actor_id == manual_actor_id,
                MappingRowProjectionEvent.client_request_id == manual_request_id,
                MappingRowProjectionEvent.event_type == "started",
            ).first()
            if prior:
                if prior.request_digest != digest:
                    raise ValueError("client_request_id is already bound to a different projection payload")
                replayed += 1
                continue

        attempt_id = str(uuid.uuid4())
        attempt_kind = "manual" if manual else "automatic"
        was_dead_letter = latest.projection_status == "dead_letter"
        if manual:
            latest.projection_manual_attempts = int(latest.projection_manual_attempts or 0) + 1
            attempt_number = latest.projection_manual_attempts
        else:
            latest.projection_attempts = int(latest.projection_attempts or 0) + 1
            attempt_number = latest.projection_attempts
        latest.projection_status = "projecting"
        latest.projection_last_attempt_at = now
        latest.projection_next_attempt_at = None
        latest.projection_lease_until = now + timedelta(seconds=PROJECTION_LEASE_SECONDS)
        latest.projection_current_attempt_id = attempt_id
        db.add(_event_from_attempt(
            event_type="started",
            attempt_id=attempt_id,
            row=latest,
            attempt_number=attempt_number,
            attempt_kind=attempt_kind,
            actor_kind=actor_kind,
            actor_id=str(manual_actor_id) if manual else None,
            reason=reason,
            client_request_id=str(manual_request_id) if manual else None,
            request_digest=digest,
        ))
        claimed[str(latest.mapping_id)].append({
            "latest_id": str(latest.id),
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "attempt_kind": attempt_kind,
            "was_dead_letter": was_dead_letter,
            "pair": pair,
            "selected_ids": [str(row.id) for row in pair_rows],
            "row": latest,
        })

    # Persist the claim and immutable start events before making an external call.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if manual and mapping_apply_run_id:
            replay = _manual_replay(
                db,
                run_id=mapping_apply_run_id,
                actor_id=str(manual_actor_id),
                request_id=str(manual_request_id),
                reason=reason or "",
            )
            if replay is not None:
                return replay
        raise
    if not claimed:
        return {
            "selected": len(selected), "projected": projected, "failed": 0,
            "pending": deferred, "dead_letter": dead_letter, "replayed": replayed,
        }

    try:
        service = FalkorDBService()
        service_error = None
    except Exception as exc:
        service = None
        service_error = exc
    failed = pending = 0
    for mapping_id, items in claimed.items():
        mapping_error = None
        try:
            mapping = db.get(OntologyMapping, mapping_id)
            if mapping is None:
                raise RuntimeError("Mapping definition unavailable")
            if service_error is not None:
                raise service_error
            if service is None:
                raise RuntimeError("FalkorDB unavailable")
            if not service.available:
                raise RuntimeError("FalkorDB unavailable")
        except Exception as exc:
            mapping_error = exc
            mapping = None

        # Isolate graph writes by object so one bad row cannot consume the
        # retry budget of every other row in the same Mapping Apply Run.
        for item in items:
            try:
                if mapping_error is not None:
                    raise mapping_error
                assert mapping is not None and service is not None
                concept_id = _concept_id(mapping)
                change = item["row"]
                deleted = change.operation == "delete"
                row_data = dict(change.before_row_data if deleted else change.after_row_data or {})
                row_data.update({
                    "row_identity": change.row_identity,
                    "entity_id": concept_id,
                    "_source_mapping_id": mapping.id,
                    "_source_deleted": deleted,
                })
                nodes = [
                    {
                        "id": concept_id,
                        "entity_type": mapping.entity_class,
                        "properties": {"is_concept": True, "ontology_id": mapping.ontology_id},
                    },
                    {"id": change.object_id, "entity_type": mapping.entity_class, "properties": row_data},
                ]
                if service.upsert_instances(mapping.ontology_id, nodes) != len(nodes):
                    raise RuntimeError("FalkorDB node write count mismatch")
                if not deleted:
                    edge = {
                        "source": change.object_id,
                        "target": concept_id,
                        "type": "INSTANCE_OF",
                        "properties": {
                            "ontology_id": mapping.ontology_id,
                            "source": "mapping",
                            "source_mapping_id": mapping.id,
                        },
                    }
                    if service.upsert_relations(mapping.ontology_id, [edge]) != 1:
                        raise RuntimeError("FalkorDB relation write count mismatch")

                latest = db.get(MappingRowChange, item["latest_id"])
                if latest is None or latest.projection_current_attempt_id != item["attempt_id"]:
                    continue
                projected_at = datetime.now(timezone.utc)
                latest.projection_status = "projected"
                latest.projection_projected_at = projected_at
                latest.projection_error = None
                latest.projection_next_attempt_at = None
                latest.projection_lease_until = None
                latest.projection_current_attempt_id = None
                for row_id in item["selected_ids"]:
                    row = db.get(MappingRowChange, row_id)
                    if row is not None:
                        row.projection_status = "projected"
                        row.projection_projected_at = projected_at
                        row.projection_error = None
                        row.projection_next_attempt_at = None
                db.add(_event_from_attempt(
                    event_type="finished", attempt_id=item["attempt_id"], row=latest,
                    attempt_number=item["attempt_number"], attempt_kind=item["attempt_kind"],
                    actor_kind=actor_kind,
                    actor_id=str(manual_actor_id) if manual else None,
                    reason=reason,
                    client_request_id=str(manual_request_id) if manual else None,
                    request_digest=(
                        _request_digest(str(latest.id), str(latest.mapping_apply_run_id), str(manual_actor_id), reason or "")
                        if manual else None
                    ),
                    outcome="projected",
                ))
                db.commit()
                projected += len(item["selected_ids"])
            except Exception as exc:
                db.rollback()
                error_code, error_message = _safe_error(exc)
                latest = db.get(MappingRowChange, item["latest_id"])
                if latest is None or latest.projection_current_attempt_id != item["attempt_id"]:
                    continue
                exhausted = (
                    item["attempt_kind"] == "automatic"
                    and item["attempt_number"] >= MAX_AUTOMATIC_ATTEMPTS
                ) or (manual and item["was_dead_letter"])
                latest.projection_status = "dead_letter" if exhausted else "pending"
                latest.projection_error = error_code
                latest.projection_lease_until = None
                latest.projection_current_attempt_id = None
                latest.projection_next_attempt_at = (
                    None if exhausted else _retry_at(max(1, int(latest.projection_attempts or 0)), datetime.now(timezone.utc))
                )
                for row_id in item["selected_ids"]:
                    row = db.get(MappingRowChange, row_id)
                    if row is not None:
                        row.projection_status = "dead_letter" if exhausted else "pending"
                        row.projection_error = error_code
                        row.projection_next_attempt_at = latest.projection_next_attempt_at
                db.add(_event_from_attempt(
                    event_type="finished", attempt_id=item["attempt_id"], row=latest,
                    attempt_number=item["attempt_number"], attempt_kind=item["attempt_kind"],
                    actor_kind=actor_kind,
                    actor_id=str(manual_actor_id) if manual else None,
                    reason=reason,
                    client_request_id=str(manual_request_id) if manual else None,
                    request_digest=(
                        _request_digest(str(latest.id), str(latest.mapping_apply_run_id), str(manual_actor_id), reason or "")
                        if manual else None
                    ),
                    outcome="dead_letter" if exhausted else "retry_wait",
                    error_code=error_code,
                    error_message=error_message,
                ))
                if exhausted:
                    dead_letter += len(item["selected_ids"])
                else:
                    failed += len(item["selected_ids"])
                    pending += len(item["selected_ids"])
                db.commit()

    return {
        "selected": len(selected),
        "projected": projected,
        "failed": failed,
        "pending": pending + deferred,
        "dead_letter": dead_letter,
        "replayed": replayed,
    }


def mapping_row_projection_summary(
    db: Session,
    mapping_apply_run_id: str,
    *,
    limit: int = 20,
    include_attempts: bool = True,
) -> dict:
    """Summarize SQL Apply and graph projection separately for an Apply Run."""
    changes = db.query(MappingRowChange).filter_by(
        mapping_apply_run_id=mapping_apply_run_id,
    ).order_by(MappingRowChange.created_at.asc(), MappingRowChange.id.asc()).all()
    status_counts = {"pending": 0, "projecting": 0, "projected": 0, "dead_letter": 0}
    for change in changes:
        status_counts[change.projection_status] = status_counts.get(change.projection_status, 0) + 1
    active = [change.projection_status for change in changes]
    if not changes:
        status = "not_applicable"
    elif all(value == "projected" for value in active):
        status = "projected"
    elif all(value == "dead_letter" for value in active):
        status = "dead_letter"
    elif all(value == "pending" for value in active):
        status = "pending"
    elif all(value == "projecting" for value in active):
        status = "projecting"
    else:
        status = "mixed"

    events = db.query(MappingRowProjectionEvent).filter_by(
        mapping_apply_run_id=mapping_apply_run_id,
    ).order_by(MappingRowProjectionEvent.created_at.desc(), MappingRowProjectionEvent.id.desc()).limit(
        max(1, min(limit, 100)) * 2
    ).all() if include_attempts else []
    starts = {event.attempt_id: event for event in events if event.event_type == "started"}
    finishes = {event.attempt_id: event for event in events if event.event_type == "finished"}
    attempts = []
    for attempt_id, start in starts.items():
        finish = finishes.get(attempt_id)
        attempts.append({
            "attempt_id": attempt_id,
            "row_change_id": start.row_change_id,
            "object_id": start.object_id,
            "attempt_kind": start.attempt_kind,
            "attempt_number": start.attempt_number,
            "actor_kind": start.actor_kind,
            "actor_id": start.actor_id,
            "reason": start.reason,
            "client_request_id": start.client_request_id,
            "status": finish.outcome if finish else "running",
            "error_code": finish.error_code if finish else None,
            "error_message": finish.error_message if finish else None,
            "started_at": start.created_at.isoformat() if start.created_at else None,
            "finished_at": finish.created_at.isoformat() if finish and finish.created_at else None,
        })
    attempts.sort(key=lambda event: event["started_at"] or "", reverse=True)
    return {
        "status": status,
        "row_count": len(changes),
        **status_counts,
        "automatic_attempts": sum(int(change.projection_attempts or 0) for change in changes),
        "manual_attempts": sum(int(change.projection_manual_attempts or 0) for change in changes),
        "max_automatic_attempts": MAX_AUTOMATIC_ATTEMPTS,
        "attempts": attempts[:max(1, min(limit, 100))],
    }
