"""Construction Run and provenance APIs shared by all build modes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_user, require_editor
from app.models.user import User
from app.models.v2.dataset import Dataset, DatasetVersion, MediaItem, MultimodalSample
from app.services.v2.dataset_authorization import require_dataset_access
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError
from app.models.v2.construction import ConstructionRun, EvidenceRef
from app.services.v2.construction_service import (
    add_evidence,
    create_run,
    serialize_evidence,
    serialize_run,
    update_run,
)

router = APIRouter(dependencies=[Depends(get_current_user)])
assertions_router = APIRouter(dependencies=[Depends(get_current_user)])
construction_root_router = APIRouter(dependencies=[Depends(get_current_user)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_ontology_scope(db: Session, ontology_id: str, user: User) -> None:
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise HTTPException(404, "本体不存在或不可访问") from None


def _run_or_404(db: Session, run_id: str, user: User, *, write: bool = False) -> ConstructionRun:
    if write and user.role not in {"admin", "editor"}:
        raise HTTPException(403, "Editor role required")
    run = db.query(ConstructionRun).filter(ConstructionRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "Construction run not found")
    _require_ontology_scope(db, run.ontology_id, user)
    if run.dataset_id:
        require_dataset_access(db, run.dataset_id, user)
    return run


def _require_evidence_source_access(db: Session, body: "EvidenceCreate", user: User) -> None:
    dataset_ids: set[str] = set()
    if body.source_dataset_version:
        version = db.get(DatasetVersion, body.source_dataset_version)
        if not version:
            raise HTTPException(404, "来源数据版本不存在或不可访问")
        dataset_ids.add(str(version.dataset_id))
    if body.source_media_id:
        media = db.get(MediaItem, body.source_media_id)
        if not media:
            raise HTTPException(404, "来源资产不存在或不可访问")
        version = db.get(DatasetVersion, media.dataset_version_id)
        if version:
            dataset_ids.add(str(version.dataset_id))
    if body.source_sample_id:
        sample = db.get(MultimodalSample, body.source_sample_id)
        if not sample:
            raise HTTPException(404, "来源样例不存在或不可访问")
        version = db.get(DatasetVersion, sample.dataset_version_id)
        if version:
            dataset_ids.add(str(version.dataset_id))
    for dataset_id in dataset_ids:
        require_dataset_access(db, dataset_id, user)


class ConstructionRunCreate(BaseModel):
    mode: str = Field(pattern="^(regular|temporal|multimodal|quality_benchmark)$")
    dataset_id: str | None = None
    model_name: str | None = None
    config: dict = {}


class RunUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(queued|running|waiting_for_model|completed|failed|cancelled)$")
    progress: dict | None = None
    metrics: dict | None = None
    artifact_uri: str | None = None
    error: str | None = None
    cancel_requested: bool | None = None


class EvidenceCreate(BaseModel):
    assertion_id: str
    assertion_kind: str = "node"
    extractor: str = Field(default="rule", pattern="^(rule|llm|ocr|bridge_fallback)$")
    source_file: str | None = None
    source_row: int | None = None
    source_media_id: str | None = None
    source_sample_id: str | None = None
    source_dataset_version: str | None = None
    revision_id: str | None = None
    model_name: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_method: str = "not_calibrated"
    evidence_text: str | None = None
    content: object | None = None


@router.post("/{ontology_id}/construction-runs", status_code=202)
def create_construction_run(ontology_id: str, body: ConstructionRunCreate, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    if body.mode in {"regular", "multimodal"}:
        raise HTTPException(409, "常规与多模态构建必须通过构筑草案和独立审阅后启动")
    _require_ontology_scope(db, ontology_id, user)
    if body.dataset_id:
        require_dataset_access(db, body.dataset_id, user)
    run = create_run(
        db,
        ontology_id=ontology_id,
        mode=body.mode,
        dataset_id=body.dataset_id,
        model_name=body.model_name,
        config=body.config,
    )
    return serialize_run(run)


@router.get("/construction-runs/{run_id}")
def get_construction_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    run = _run_or_404(db, run_id, user)
    return serialize_run(run)


@router.patch("/construction-runs/{run_id}")
def patch_construction_run(run_id: str, body: RunUpdate, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    run = _run_or_404(db, run_id, user, write=True)
    return serialize_run(update_run(db, run, **body.model_dump(exclude_none=True)))


@construction_root_router.post("/construction-runs/{run_id}/cancel")
def cancel_construction_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    run = _run_or_404(db, run_id, user, write=True)
    if run.status in {"completed", "failed", "cancelled"}:
        return serialize_run(run)
    updated = update_run(db, run, cancel_requested=True, status="cancelled", error="用户取消任务")
    return serialize_run(updated)


@router.get("/construction-runs/{run_id}/evidence")
def list_run_evidence(run_id: str, limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    run = _run_or_404(db, run_id, user)
    refs = db.query(EvidenceRef).filter(EvidenceRef.construction_run_id == run_id).order_by(EvidenceRef.created_at.desc()).limit(limit).all()
    return {"run_id": run_id, "evidence": [serialize_evidence(ref) for ref in refs], "count": len(refs)}


@router.post("/construction-runs/{run_id}/evidence")
def create_run_evidence(run_id: str, body: EvidenceCreate, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    run = _run_or_404(db, run_id, user, write=True)
    _require_evidence_source_access(db, body, user)
    ref = add_evidence(db, run=run, **body.model_dump())
    return serialize_evidence(ref)


@router.get("/assertions/{assertion_id}/provenance")
def assertion_provenance(assertion_id: str, ontology_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = db.query(EvidenceRef).filter(EvidenceRef.assertion_id == assertion_id)
    if ontology_id:
        _require_ontology_scope(db, ontology_id, user)
        query = query.filter(EvidenceRef.ontology_id == ontology_id)
    refs = query.order_by(EvidenceRef.created_at.desc()).limit(1000).all()
    accessible = []
    for ref in refs:
        try:
            _require_ontology_scope(db, ref.ontology_id, user)
            if ref.source_dataset_id:
                require_dataset_access(db, ref.source_dataset_id, user)
            accessible.append(ref)
        except HTTPException:
            continue
    return {"assertion_id": assertion_id, "evidence": [serialize_evidence(ref) for ref in accessible], "count": len(accessible)}


@assertions_router.get("/assertions/{assertion_id}/provenance")
def assertion_provenance_root(assertion_id: str, ontology_id: str | None = None, db: Session = Depends(get_db)):
    return assertion_provenance(assertion_id, ontology_id=ontology_id, db=db)


@construction_root_router.get("/construction-runs/{run_id}")
def get_construction_run_root(run_id: str, db: Session = Depends(get_db)):
    return get_construction_run(run_id, db=db)


@construction_root_router.patch("/construction-runs/{run_id}")
def patch_construction_run_root(run_id: str, body: RunUpdate, db: Session = Depends(get_db)):
    return patch_construction_run(run_id, body=body, db=db)


@construction_root_router.get("/construction-runs/{run_id}/evidence")
def list_run_evidence_root(run_id: str, limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    return list_run_evidence(run_id, limit=limit, db=db)


@construction_root_router.post("/construction-runs/{run_id}/evidence")
def create_run_evidence_root(run_id: str, body: EvidenceCreate, db: Session = Depends(get_db)):
    return create_run_evidence(run_id, body=body, db=db)
