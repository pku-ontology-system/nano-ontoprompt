"""Reviewable DecisionCase API backed by the shared Action compiler."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_admin, require_editor
from app.models.user import User
from app.models.v2.action import OntologyActionRun
from app.models.v2.decision_case import DecisionCase, DecisionCaseEvent
from app.services.v2.decision_cases import CaseError, DecisionCaseService, case_payload


router = APIRouter()


class CreateCaseBody(BaseModel):
    kind: Literal["quality_review", "dynamic_event", "maintenance_review", "general"]
    title: str = Field(min_length=1, max_length=200)
    action_type_id: str = Field(min_length=1)
    target_object_id: str = Field(min_length=1, max_length=200)
    target_object_type: str = Field(min_length=1, max_length=200)
    parameters: dict
    evidence_refs: list[str | dict] = Field(default_factory=list, max_length=30)
    reason: str = Field(min_length=1, max_length=4000)
    context: dict = Field(default_factory=dict)
    expected_etag: str = Field(min_length=64, max_length=64)
    client_request_id: str = Field(min_length=1, max_length=120)


class SubmitBody(BaseModel):
    preview_digest: str = Field(min_length=64, max_length=64)


class UpdateCaseBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    parameters: dict
    evidence_refs: list[str | dict] = Field(default_factory=list, max_length=30)
    reason: str = Field(min_length=1, max_length=4000)
    context: dict | None = None
    expected_etag: str = Field(min_length=64, max_length=64)


class ReviewBody(BaseModel):
    decision: Literal["approved", "rejected", "changes_requested"]
    comment: str = Field(default="", max_length=4000)


class ManualProjectionReconcileBody(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
    client_request_id: str = Field(min_length=1, max_length=120)


def _service(db: Session, ontology_id: str, user: User) -> DecisionCaseService:
    try:
        return DecisionCaseService(db, ontology_id, user)
    except CaseError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc


def _call(operation):
    try:
        return operation()
    except CaseError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc


def _payload(db: Session, case: DecisionCase) -> dict:
    run = db.get(OntologyActionRun, case.action_run_id) if case.action_run_id else None
    payload = case_payload(case, run)
    if case.status == "reverted":
        event = db.query(DecisionCaseEvent).filter_by(case_id=case.id,
            event_type="reverted").order_by(DecisionCaseEvent.sequence.desc()).first()
        reversal_id = (event.payload or {}).get("revert_run_id") if event else None
        reversal = db.get(OntologyActionRun, reversal_id) if reversal_id else None
        payload["revert_action_run_id"] = reversal_id
        payload["projection_status"] = reversal.projection_status if reversal else None
    return payload


@router.get("/{ontology_id}/decision-cases/target-state/{object_id}")
def target_state(ontology_id: str, object_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    service = _service(db, ontology_id, user)
    return _call(lambda: service.target_state(object_id))


@router.get("/{ontology_id}/decision-cases")
def list_cases(ontology_id: str, target_object_id: str | None = None, limit: int = 50,
               db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service = _service(db, ontology_id, user)
    query = db.query(DecisionCase).filter_by(ontology_id=ontology_id)
    if target_object_id:
        query = query.filter_by(target_object_id=target_object_id)
    if user.role != "admin":
        query = query.filter((DecisionCase.created_by == user.id) | (DecisionCase.reviewed_by == user.id))
    items = []
    for case in query.order_by(DecisionCase.created_at.desc()).limit(max(1, min(limit, 100))).all():
        try:
            service.get_case(case.id)
        except CaseError:
            continue
        items.append(_payload(db, case))
    return {"cases": items}


@router.post("/{ontology_id}/decision-cases", status_code=201)
def create_case(ontology_id: str, body: CreateCaseBody, db: Session = Depends(get_db),
                user: User = Depends(require_editor)):
    service = _service(db, ontology_id, user)
    case = _call(lambda: service.create(**body.model_dump()))
    return _payload(db, case)


@router.get("/{ontology_id}/decision-cases/{case_id}")
def get_case(ontology_id: str, case_id: str, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    service = _service(db, ontology_id, user)
    case = _call(lambda: service.get_case(case_id))
    return _payload(db, case)


@router.patch("/{ontology_id}/decision-cases/{case_id}")
def update_case(ontology_id: str, case_id: str, body: UpdateCaseBody,
                db: Session = Depends(get_db), user: User = Depends(require_editor)):
    service = _service(db, ontology_id, user)
    case = _call(lambda: service.update(case_id, **body.model_dump()))
    return _payload(db, case)


@router.get("/{ontology_id}/decision-cases/{case_id}/events")
def case_events(ontology_id: str, case_id: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    service = _service(db, ontology_id, user)
    _call(lambda: service.get_case(case_id))
    rows = db.query(DecisionCaseEvent).filter_by(case_id=case_id).order_by(DecisionCaseEvent.sequence).all()
    return {"events": [{"id": row.id, "sequence": row.sequence, "event_type": row.event_type,
                        "actor_id": row.actor_id, "payload": row.payload,
                        "trace_id": row.trace_id,
                        "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]}


@router.post("/{ontology_id}/decision-cases/{case_id}:preview")
def preview_case(ontology_id: str, case_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    service = _service(db, ontology_id, user)
    return _call(lambda: service.preview(case_id))


@router.post("/{ontology_id}/decision-cases/{case_id}:submit")
def submit_case(ontology_id: str, case_id: str, body: SubmitBody, db: Session = Depends(get_db),
                user: User = Depends(require_editor)):
    service = _service(db, ontology_id, user)
    case = _call(lambda: service.submit(case_id, body.preview_digest))
    return _payload(db, case)


@router.post("/{ontology_id}/decision-cases/{case_id}:review")
def review_case(ontology_id: str, case_id: str, body: ReviewBody, db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    service = _service(db, ontology_id, user)
    case = _call(lambda: service.review(case_id, body.decision, body.comment))
    return _payload(db, case)


@router.post("/{ontology_id}/decision-cases/{case_id}:execute")
def execute_case(ontology_id: str, case_id: str, db: Session = Depends(get_db),
                 user: User = Depends(require_editor)):
    service = _service(db, ontology_id, user)
    case, run = _call(lambda: service.execute(case_id))
    return {"case": case_payload(case, run), "run": {"id": run.id, "status": run.status,
            "projection_status": run.projection_status, "before_digest": run.before_digest,
            "after_digest": run.after_digest, "trace_id": run.trace_id,
            "requested_by": run.requested_by, "reviewed_by": run.reviewed_by,
            "executed_by": run.executed_by}}


@router.post("/{ontology_id}/decision-cases/action-runs/{run_id}:reconcile")
def reconcile_case_run(ontology_id: str, run_id: str, body: ManualProjectionReconcileBody,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_admin)):
    service = _service(db, ontology_id, user)
    run = db.query(OntologyActionRun).filter_by(id=run_id, ontology_id=ontology_id).first()
    if run is None:
        raise HTTPException(404, {"code": "not_found", "message": "Action 运行不存在"})
    case = _call(lambda: service.case_for_run(run))
    manual_attempts_before = run.projection_manual_attempts or 0
    run = _call(lambda: service.sync_projection(
        run,
        manual_retry=True,
        manual_reason=body.reason,
        client_request_id=body.client_request_id,
    ))
    case_id = case.id
    starts = db.query(DecisionCaseEvent).filter_by(
        case_id=case_id, event_type="projection_attempt_started", actor_id=user.id,
    ).order_by(DecisionCaseEvent.sequence.desc()).all()
    start = next((event for event in starts if (event.payload or {}).get("run_id") == run.id
                  and (event.payload or {}).get("client_request_id") == body.client_request_id), None)
    attempt = None
    if start:
        attempt_id = (start.payload or {}).get("attempt_id")
        finish = next((event for event in db.query(DecisionCaseEvent).filter_by(
            case_id=case_id, event_type="projection_attempt_finished",
        ).order_by(DecisionCaseEvent.sequence.desc()).all()
                       if (event.payload or {}).get("attempt_id") == attempt_id), None)
        attempt = {"id": attempt_id,
                   "kind": (start.payload or {}).get("attempt_kind"),
                   "number": (start.payload or {}).get("attempt_number"),
                   "status": (finish.payload or {}).get("status") if finish else "running",
                   "reason": (start.payload or {}).get("reason"),
                   "replayed": (run.projection_manual_attempts or 0) == manual_attempts_before}
    return {"run_id": run.id, "status": run.status, "projection_status": run.projection_status,
            "projection_error": run.projection_error,
            "projection_attempts": run.projection_attempts,
            "projection_manual_attempts": run.projection_manual_attempts,
            "attempt": attempt}
