"""AI ontology candidates API: generate with Auto Ontology (or import its export), review item by item, publish."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_editor
from app.models.user import User
from app.models.v2.ontology_candidates import OntologyCandidateItem, OntologyCandidateSet
from app.services.v2 import ontology_candidates as service
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError

router = APIRouter(dependencies=[Depends(get_current_user)])


class GenerateBody(BaseModel):
    source_id: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=100)
    acknowledge_cloud: bool = False


class ImportBody(BaseModel):
    document: str | None = Field(default=None, max_length=3_000_000)
    example_id: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, max_length=200)


class ReviewBody(BaseModel):
    decision: Literal["pending", "accepted", "modified", "rejected"]
    final: dict | None = None
    reject_category: str | None = Field(default=None, max_length=24)
    reject_reason: str | None = Field(default=None, max_length=2000)


class PublishBody(BaseModel):
    base_revision_id: str | None = Field(default=None, max_length=64)


def _scope(db: Session, ontology_id: str, user: User) -> None:
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise HTTPException(404, "Ontology not found or inaccessible") from None


def _fail(exc: service.CandidateError):
    raise HTTPException(exc.status, {"code": exc.code, "message": exc.message, **({"details": exc.details} if exc.details else {})})


def _get_set(db: Session, ontology_id: str, set_id: str) -> OntologyCandidateSet:
    cset = db.query(OntologyCandidateSet).filter_by(id=set_id, ontology_id=ontology_id).first()
    if cset is None:
        raise HTTPException(404, {"code": "candidate_set_not_found", "message": "候选集不存在"})
    return cset


def _items(db: Session, set_id: str) -> list[OntologyCandidateItem]:
    return db.query(OntologyCandidateItem).filter_by(set_id=set_id).order_by(OntologyCandidateItem.ordinal).all()


@router.get("/{ontology_id}/ai-candidates/runner")
def runner(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Whether one-click generation is available, and the sources and models it offers."""
    _scope(db, ontology_id, user)
    return {**service.runner_overview(), "examples": service.list_examples(),
            "reject_categories": [{"id": key, "label": label} for key, label in service.REJECT_CATEGORIES.items()]}


@router.get("/{ontology_id}/ai-candidates")
def list_sets(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _scope(db, ontology_id, user)
    rows = db.query(OntologyCandidateSet).filter_by(ontology_id=ontology_id).order_by(OntologyCandidateSet.created_at.desc()).limit(50).all()
    result = []
    for cset in rows:
        summary = service.serialize_set(cset)
        summary["stats"] = service.stats(_items(db, cset.id))
        result.append(summary)
    return {"sets": result}


@router.post("/{ontology_id}/ai-candidates:generate", status_code=201)
def generate(ontology_id: str, body: GenerateBody, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _scope(db, ontology_id, user)
    try:
        cset = service.start_generation(db, ontology_id, user, source_id=body.source_id, model_id=body.model_id,
                                        acknowledge_cloud=body.acknowledge_cloud)
    except service.CandidateError as exc:
        _fail(exc)
    return service.serialize_set(cset, [])


@router.post("/{ontology_id}/ai-candidates:import", status_code=201)
def import_document(ontology_id: str, body: ImportBody, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _scope(db, ontology_id, user)
    try:
        cset = service.import_document(db, ontology_id, user, text=body.document, example_id=body.example_id, title=body.title)
    except service.CandidateError as exc:
        db.rollback()
        _fail(exc)
    return service.serialize_set(cset, _items(db, cset.id))


@router.get("/{ontology_id}/ai-candidates/{set_id}")
def get_set(ontology_id: str, set_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _scope(db, ontology_id, user)
    cset = _get_set(db, ontology_id, set_id)
    if cset.status == "generating":
        cset = service.sync_generation(db, cset)
    return service.serialize_set(cset, _items(db, cset.id))


@router.post("/{ontology_id}/ai-candidates/{set_id}/items/{item_id}:review")
def review_item(ontology_id: str, set_id: str, item_id: str, body: ReviewBody, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _scope(db, ontology_id, user)
    cset = _get_set(db, ontology_id, set_id)
    item = db.query(OntologyCandidateItem).filter_by(id=item_id, set_id=set_id).first()
    if item is None:
        raise HTTPException(404, {"code": "candidate_item_not_found", "message": "候选不存在"})
    try:
        service.review_item(db, cset, item, user, decision=body.decision, final=body.final,
                            reject_category=body.reject_category, reject_reason=body.reject_reason)
    except service.CandidateError as exc:
        db.rollback()
        _fail(exc)
    return {"item": service.serialize_item(item), "stats": service.stats(_items(db, set_id))}


@router.post("/{ontology_id}/ai-candidates/{set_id}:validate")
def validate(ontology_id: str, set_id: str, body: PublishBody, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    """Dry-run the accepted items together, without changing the ontology."""
    _scope(db, ontology_id, user)
    cset = _get_set(db, ontology_id, set_id)
    try:
        return service.validate_publish(db, cset, _items(db, set_id), body.base_revision_id)
    except service.CandidateError as exc:
        db.rollback()
        _fail(exc)


@router.post("/{ontology_id}/ai-candidates/{set_id}:publish")
def publish(ontology_id: str, set_id: str, body: PublishBody, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _scope(db, ontology_id, user)
    cset = _get_set(db, ontology_id, set_id)
    items = _items(db, set_id)
    try:
        result = service.publish(db, cset, items, user, body.base_revision_id)
    except service.CandidateError as exc:
        db.rollback()
        _fail(exc)
    db.refresh(cset)
    return {"set": service.serialize_set(cset, _items(db, set_id)), "revision": result["revision"], "audit_task_id": result.get("audit_task_id")}


@router.post("/{ontology_id}/ai-candidates/{set_id}:cancel")
def cancel(ontology_id: str, set_id: str, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _scope(db, ontology_id, user)
    cset = _get_set(db, ontology_id, set_id)
    try:
        service.cancel_generation(db, cset)
    except service.CandidateError as exc:
        _fail(exc)
    return service.serialize_set(cset, _items(db, set_id))
