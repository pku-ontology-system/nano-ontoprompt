"""Execute and reconcile durable Agent session turns."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


def _persist_worker_failure(db, run_id: str, code: str, message: str,
                            worker_task_id: str | None = None) -> None:
    from app.models.v2.agent import AgentCheckpoint, AgentContextItem, AgentEvent, AgentRun, AgentSession
    from app.services.v2.agent_sessions import digest

    hint = db.get(AgentRun, run_id)
    if not hint or not hint.session_id:
        return
    session = db.query(AgentSession).filter_by(id=hint.session_id).with_for_update().first()
    run = db.query(AgentRun).filter_by(id=run_id, session_id=hint.session_id).with_for_update().first()
    if not run or run.status not in {"queued", "running"}:
        return
    payload = dict(run.payload or {})
    claimed_worker = payload.get("worker_task_id")
    if worker_task_id and claimed_worker and claimed_worker != worker_task_id:
        return
    if worker_task_id and run.worker_task_id and run.worker_task_id != worker_task_id:
        return
    if worker_task_id and run.status == "queued" and not claimed_worker and any(
            item.get("previous_worker_task_id") == worker_task_id
            for item in (payload.get("recovery_history") or []) if isinstance(item, dict)):
        return

    now = datetime.now(timezone.utc)
    cancelling = bool(run.cancel_requested_at or (session and session.status == "cancelling"))
    failure = {"code": code, "message": message}
    run.status = "cancelled" if cancelling else "failed"
    run.completed_at = now
    run.output_digest = digest({"status": "cancelled", "run_id": run.id} if cancelling else failure)
    if cancelling:
        run.cancel_requested_at = run.cancel_requested_at or now
        run.cancel_acknowledged_at = now
        run.lease_expires_at = None
    else:
        run.payload = {**(run.payload or {}), "error": failure}
    if session and session.status in {"queued", "running", "cancelling"}:
        session.status = "cancelled" if cancelling else "idle"
        session.version += 1
        session.updated_at = now
        sequence = (db.query(func.max(AgentEvent.sequence)).filter_by(
            session_id=session.id).scalar() or 0) + 1
        event = AgentEvent(session_id=session.id, sequence=sequence,
            event_type="cancelled" if cancelling else "error", actor_id=None,
            payload={"run_id": run.id, "reason": "worker_acknowledged",
                     "model_lineage": (run.payload or {}).get("model_lineage")} if cancelling else
                    {**failure, "run_id": run.id, "mode": (run.payload or {}).get("mode"),
                     "model_lineage": (run.payload or {}).get("model_lineage")},
            trace_id=run.trace_id or run.id)
        db.add(event)
        db.flush()
        refs = [{"kind": item.kind, "ref_id": item.ref_id,
                 "source_version": item.source_version}
                for item in db.query(AgentContextItem).filter_by(
                    session_id=session.id, active=True).order_by(
                    AgentContextItem.created_at, AgentContextItem.id).all()]
        db.add(AgentCheckpoint(session_id=session.id, event_sequence=sequence,
            state={"status": session.status, "version": session.version,
                   "pending_case_id": session.pending_case_id, "context_refs": refs},
            tool_request_keys=[str(run.client_request_id or "")],
            pinned_versions={"ontology_revision_id": session.metadata_revision_id,
                "permission_digest": session.permission_digest,
                "tools": (run.payload or {}).get("tool_versions", {}),
                "tool_registry_digest": (run.payload or {}).get("tool_registry_digest"),
                "model_lineage": (run.payload or {}).get("model_lineage")}))
    db.commit()


def _mark_reconcile_dispatch(db, run_id: str, dispatched: bool) -> None:
    from app.models.v2.agent import AgentCheckpoint, AgentContextItem, AgentEvent, AgentRun, AgentSession
    from app.services.v2.agent_sessions import digest

    hint = db.get(AgentRun, run_id)
    if not hint or not hint.session_id:
        return
    session = db.query(AgentSession).filter_by(id=hint.session_id).with_for_update().first()
    if session is None:
        return
    run = db.query(AgentRun).filter_by(id=run_id, session_id=hint.session_id).with_for_update().first()
    if not run or run.status != "queued":
        return
    payload = dict(run.payload or {})
    attempts = int(payload.get("dispatch_attempts") or 0) + 1
    now = datetime.now(timezone.utc)
    base_delay = 60 if dispatched else 30
    delay = min(base_delay * (2 ** min(attempts - 1, 4)), 300)
    payload.update({
        "dispatch_attempts": attempts,
        "dispatch_state": "sent" if dispatched else "pending_reconcile",
        "last_dispatch_at": now.isoformat(),
        "next_dispatch_at": (now + timedelta(seconds=delay)).isoformat(),
    })
    run.payload = payload
    if not dispatched and session and not payload.get("dispatch_deferred_event_recorded"):
        sequence = (db.query(func.max(AgentEvent.sequence)).filter_by(
            session_id=session.id).scalar() or 0) + 1
        event = AgentEvent(session_id=session.id, sequence=sequence,
            event_type="turn_dispatch_deferred", actor_id=None,
            payload={"run_id": run.id, "reason": "worker_queue_unavailable",
                     "model_lineage": payload.get("model_lineage")},
            trace_id=run.trace_id or run.id)
        db.add(event)
        db.flush()
        refs = [{"kind": item.kind, "ref_id": item.ref_id,
                 "source_version": item.source_version}
                for item in db.query(AgentContextItem).filter_by(
                    session_id=session.id, active=True).order_by(
                    AgentContextItem.created_at, AgentContextItem.id).all()]
        db.add(AgentCheckpoint(session_id=session.id, event_sequence=sequence,
            state={"status": session.status, "version": session.version,
                   "pending_case_id": session.pending_case_id, "context_refs": refs},
            tool_request_keys=[str(run.client_request_id or "")],
            pinned_versions={"ontology_revision_id": session.metadata_revision_id,
                "permission_digest": session.permission_digest,
                "tools": payload.get("tool_versions", {}),
                "tool_registry_digest": payload.get("tool_registry_digest"),
                "model_lineage": payload.get("model_lineage")}))
        payload["dispatch_deferred_event_recorded"] = True
        run.payload = payload
    db.commit()


def _as_utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _renew_agent_run_lease(run_id: str, worker_task_id: str, lease_generation: int) -> bool | None:
    from app.database import SessionLocal
    from app.models.v2.agent import AGENT_RUN_LEASE_SECONDS, AgentRun, AgentSession

    try:
        with SessionLocal() as db:
            run = db.query(AgentRun).filter_by(id=run_id).with_for_update().first()
            if (not run or run.status != "running"
                    or run.worker_task_id != worker_task_id
                    or int(run.lease_generation or 0) != lease_generation):
                db.rollback()
                return False
            session = db.get(AgentSession, run.session_id) if run.session_id else None
            if not session or session.status not in {"running", "cancelling"}:
                db.rollback()
                return False
            now = datetime.now(timezone.utc)
            run.heartbeat_at = now
            run.lease_expires_at = now + timedelta(seconds=AGENT_RUN_LEASE_SECONDS)
            db.commit()
            return True
    except Exception:
        log.exception("Could not renew Agent run lease %s", run_id)
        return None


def _heartbeat_agent_run(run_id: str, worker_task_id: str, lease_generation: int,
                         stop_event: threading.Event) -> None:
    while not stop_event.wait(20):
        renewed = _renew_agent_run_lease(run_id, worker_task_id, lease_generation)
        if renewed is False:
            return


@celery_app.task(
    bind=True,
    name="v2.agent_sessions.execute_turn",
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_agent_session_turn(self, run_id: str):
    from app.database import SessionLocal
    from app.models.user import User
    from app.models.v2.agent import AgentRun
    from app.services.v2.agent_sessions import AgentSessionService, SessionError

    worker_task_id = str(self.request.id or "")
    with SessionLocal() as db:
        run = db.get(AgentRun, run_id)
        if run is None or not run.session_id:
            return {"run_id": run_id, "status": "missing"}
        user = db.get(User, run.created_by) if run.created_by else None
        if user is None or not user.is_active:
            _persist_worker_failure(db, run_id, "actor_unavailable", "会话所有者不可用",
                                    worker_task_id=worker_task_id)
            return {"run_id": run_id, "status": "failed", "code": "actor_unavailable"}

        try:
            service = AgentSessionService(db, run.ontology_id, user)
        except SessionError as exc:
            _persist_worker_failure(db, run_id, exc.code, str(exc),
                                    worker_task_id=worker_task_id)
            return {"run_id": run_id, "status": "failed", "code": exc.code}
        heartbeat_stop = threading.Event()
        heartbeat_thread = None

        def start_heartbeat(lease_generation: int) -> None:
            nonlocal heartbeat_thread
            heartbeat_thread = threading.Thread(
                target=_heartbeat_agent_run,
                args=(run_id, worker_task_id, lease_generation, heartbeat_stop),
                name=f"agent-lease-{run_id[:8]}", daemon=True,
            )
            heartbeat_thread.start()

        try:
            result = service.execute_queued_turn(
                run_id, worker_task_id, on_claim=start_heartbeat)
            return result or {"run_id": run_id, "status": "not_claimed"}
        except SessionError as exc:
            db.rollback()
            run = db.get(AgentRun, run_id)
            if run:
                service._record_turn_failure(run.session_id, run.id, exc, run.trace_id,
                                             worker_task_id=worker_task_id)
            return {"run_id": run_id, "status": "failed", "code": exc.code}
        except Exception:
            db.rollback()
            log.exception("Agent session turn failed unexpectedly for run %s", run_id)
            _persist_worker_failure(db, run_id, "agent_turn_failed",
                "Agent 本轮未能完成；请检查运行记录后重试",
                worker_task_id=worker_task_id)
            return {"run_id": run_id, "status": "failed", "code": "agent_turn_failed"}
        finally:
            heartbeat_stop.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=2)


@celery_app.task(
    name="v2.agent_sessions.reconcile",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_queued_agent_turns(batch_size: int = 50):
    """Recover expired fenced leases and redispatch persisted Agent turns."""
    from app.database import SessionLocal
    from app.models.user import User
    from app.models.v2.agent import AgentRun, AgentSession
    from app.services.v2.agent_sessions import AgentSessionService, SessionError

    batch_size = min(max(int(batch_size), 1), 200)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        expired_run_ids = [run_id for (run_id,) in db.query(AgentRun.id).join(
            AgentSession, AgentSession.id == AgentRun.session_id).filter(
                AgentRun.status == "running",
                AgentSession.status.in_(("running", "cancelling")),
                AgentRun.lease_expires_at.isnot(None),
                AgentRun.lease_expires_at <= now,
            ).order_by(AgentRun.lease_expires_at.asc(), AgentRun.id.asc()).limit(batch_size).all()]

    recovered = 0
    cancelled = 0
    interrupted = 0
    for run_id in expired_run_ids:
        with SessionLocal() as db:
            run = db.get(AgentRun, run_id)
            session = db.get(AgentSession, run.session_id) if run and run.session_id else None
            user = db.get(User, session.owner_id) if session else None
            if not run or not session or not user or not user.is_active:
                continue
            try:
                result = AgentSessionService(db, run.ontology_id, user).resume(
                    session.id, expected_version=session.version)
                recovered += bool(result.get("recovered"))
                cancelled += bool(result.get("cancelled"))
                interrupted += bool(result.get("interrupted"))
            except SessionError as exc:
                db.rollback()
                if exc.code not in {"version_conflict", "invalid_state"}:
                    log.warning("Could not reconcile expired Agent lease %s: %s", run_id, exc.code)
            except Exception:
                db.rollback()
                log.exception("Could not reconcile expired Agent lease %s", run_id)

    with SessionLocal() as db:
        queued = db.query(AgentRun).join(
            AgentSession, AgentSession.id == AgentRun.session_id).filter(
                AgentRun.status == "queued", AgentSession.status.in_(("queued", "running"))
            ).order_by(AgentRun.created_at.asc(), AgentRun.id.asc()).limit(
                min(batch_size * 20, 2000)).all()
        run_ids = []
        for run in queued:
            payload = run.payload or {}
            next_dispatch = _as_utc(payload.get("next_dispatch_at"))
            if next_dispatch is None:
                created_at = _as_utc(run.created_at)
                next_dispatch = created_at + timedelta(seconds=30) if created_at else now
            if next_dispatch <= now:
                run_ids.append(run.id)
                if len(run_ids) >= batch_size:
                    break

    dispatched = 0
    failed = 0
    for run_id in run_ids:
        try:
            execute_agent_session_turn.apply_async(args=[run_id])
            with SessionLocal() as db:
                _mark_reconcile_dispatch(db, run_id, dispatched=True)
            dispatched += 1
        except Exception:
            failed += 1
            log.exception("Could not redispatch queued Agent run %s", run_id)
            with SessionLocal() as db:
                try:
                    _mark_reconcile_dispatch(db, run_id, dispatched=False)
                except Exception:
                    db.rollback()
                    log.exception("Could not persist redispatch failure for Agent run %s", run_id)
    return {"scanned": len(run_ids), "dispatched": dispatched, "failed": failed,
            "expired_scanned": len(expired_run_ids), "recovered": recovered,
            "cancelled": cancelled, "interrupted": interrupted}
