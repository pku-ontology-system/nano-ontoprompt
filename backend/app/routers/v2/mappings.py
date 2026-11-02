"""v2 Ontology Mapping API — 含 Link Mapping 手动配置"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import Optional
from uuid import UUID
from app.deps import get_current_user, get_db
from app.models.user import User

router = APIRouter(dependencies=[Depends(get_current_user)])


def _require_ontology_scope(db: Session, ontology_id: str, user: User, *, write: bool = False) -> None:
    from app.services.v2.object_query.authorization import require_ontology_access
    from app.services.v2.object_query.errors import ObjectQueryError

    if write and user.role not in {"admin", "editor"}:
        raise HTTPException(403, "Editor role required")
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise HTTPException(404, "Ontology not found or inaccessible") from None


def _dataset_ontology_ids(db: Session, dataset_id: str) -> set[str]:
    from app.models.v2.mapping import OntologyLinkMapping, OntologyMapping

    ontology_ids = {
        str(row[0]) for row in db.query(OntologyMapping.ontology_id).filter(
            OntologyMapping.curated_dataset_id == dataset_id,
        ).all()
    }
    for column in (OntologyLinkMapping.src_dataset_id, OntologyLinkMapping.tgt_dataset_id):
        ontology_ids.update(
            str(row[0]) for row in db.query(OntologyLinkMapping.ontology_id).filter(
                column == dataset_id,
            ).all()
        )
    return ontology_ids


def _require_dataset_scope(db: Session, dataset_id: str, user: User, *, write: bool = False) -> None:
    from app.models.v2.curated import CuratedDataset
    from app.models.v2.dataset import Dataset
    from app.models.v2.pipeline import Pipeline

    legacy = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.kind == "curated").first()
    if legacy is None and dataset is None:
        raise HTTPException(404, "Curated dataset not found")
    if write and user.role not in {"admin", "editor"}:
        raise HTTPException(403, "Editor role required")
    if user.role == "admin":
        return

    ontology_ids = _dataset_ontology_ids(db, dataset_id)
    if ontology_ids:
        for mapped_ontology_id in sorted(ontology_ids):
            _require_ontology_scope(db, mapped_ontology_id, user, write=write)
        return

    # Before a dataset is attached to an ontology it inherits its Pipeline
    # creator as its only resource scope. v2 Dataset has no owner field yet.
    pipeline_id = str(legacy.pipeline_id) if legacy and legacy.pipeline_id else None
    pipeline = db.get(Pipeline, pipeline_id) if pipeline_id else None
    if pipeline is None or str(pipeline.created_by or "") != str(user.id):
        raise HTTPException(404, "Curated dataset not found")


class SuggestRequest(BaseModel):
    dataset_name: str
    columns: list[str]
    sample_rows: list[dict] = []
    ontology_domain: str = ""


class CreateMappingRequest(BaseModel):
    curated_dataset_id: str
    entity_class: str
    field_mapping: dict
    primary_key_column: Optional[str] = None
    property_mappings: Optional[list[dict]] = None
    confidence: float = 1.0


class ApplyMappingFromDatasetRequest(BaseModel):
    review_id: str
    mapping_review_id: str


class MappingReviewCandidateRequest(BaseModel):
    entity_class: str = Field(min_length=1, max_length=200)
    field_mapping: dict
    confidence: Optional[float] = Field(default=None, ge=0, le=1)


class MappingReviewRequest(BaseModel):
    reviewer_id: Optional[str] = None
    candidate: Optional[MappingReviewCandidateRequest] = None


class MappingReviewDecisionRequest(BaseModel):
    decision: str
    notes: str = ""


class LinkMappingReviewCandidateRequest(BaseModel):
    relation_type: str = Field(min_length=1, max_length=100)
    src_key: str = Field(min_length=1, max_length=100)
    tgt_key: str = Field(min_length=1, max_length=100)


class LinkMappingReviewRequest(BaseModel):
    reviewer_id: Optional[str] = None
    candidate: Optional[LinkMappingReviewCandidateRequest] = None


class LinkMappingReviewDecisionRequest(BaseModel):
    decision: str
    notes: str = Field(default="", max_length=4000)


class LinkMappingApplyPreviewRequest(BaseModel):
    link_review_id: str = Field(min_length=1, max_length=36)


class LinkMappingApplyRequest(BaseModel):
    link_review_id: str = Field(min_length=1, max_length=36)
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    client_request_id: UUID


class LinkMappingProjectionReplayBody(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    client_request_id: UUID
    batch_size: int = Field(default=100, ge=1, le=100)


@router.post("/{ontology_id}/mappings/suggest")
def suggest_mapping(ontology_id: str, body: SuggestRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_ontology_scope(db, ontology_id, user)
    from app.services.v2.mapping.auto_mapper import AutoMapper
    mapper = AutoMapper(db)
    suggestion = mapper.suggest_field_mapping(
        body.dataset_name, body.columns, body.sample_rows, body.ontology_domain
    )
    return {
        "entity_class": suggestion.entity_class,
        "entity_class_cn": suggestion.entity_class_cn,
        "description": suggestion.description,
        "primary_key_column": suggestion.primary_key_column,
        "field_mappings": [
            {
                "column_name": fm.column_name,
                "property_name": fm.property_name,
                "property_type": fm.property_type,
                "confidence": fm.confidence,
                "reason": fm.reason,
            }
            for fm in suggestion.field_mappings
        ],
    }


@router.post("/{ontology_id}/mappings")
def create_mapping(ontology_id: str, body: CreateMappingRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_ontology_scope(db, ontology_id, user, write=True)
    _require_dataset_scope(db, body.curated_dataset_id, user, write=True)
    from app.services.v2.mapping.mapping_service import MappingService
    svc = MappingService(db)
    field_mapping = dict(body.field_mapping or {})
    # Auto-dispatch is an internal compatibility flag. Mapping authors must
    # request an explicit durable Apply run against a pinned approved review.
    field_mapping.pop("__auto_apply_on_review__", None)
    if body.property_mappings:
        field_mapping["__properties__"] = body.property_mappings
    mapping = svc.create_mapping(
        ontology_id=ontology_id,
        curated_dataset_id=body.curated_dataset_id,
        entity_class=body.entity_class,
        field_mapping=field_mapping,
        primary_key_column=body.primary_key_column,
        confidence=body.confidence,
    )
    return {"mapping_id": mapping.id, "status": mapping.status}


@router.get("/{ontology_id}/mappings")
def list_mappings(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_ontology_scope(db, ontology_id, user)
    from app.services.v2.mapping.mapping_service import MappingService
    from app.models.v2.dataset import Dataset
    from app.models.v2.curated import CuratedDataset
    from app.models.v2.mapping_definition_review import MappingDefinitionReview
    from app.models.v2.mapping_apply_run import MappingApplyRun
    from app.services.v2.mapping_apply_runs import mapping_definition_snapshot
    from app.services.v2.mapping_definition_reviews import serialize_mapping_review
    svc = MappingService(db)
    mappings = svc.get_mappings(ontology_id)
    mapping_ids = [str(item.id) for item in mappings]
    review_rows = db.query(MappingDefinitionReview).filter(
        MappingDefinitionReview.mapping_id.in_(mapping_ids or ["__none__"]),
    ).order_by(
        MappingDefinitionReview.mapping_id,
        MappingDefinitionReview.revision_no.desc(),
    ).all()
    latest_reviews = {}
    for row in review_rows:
        latest_reviews.setdefault(str(row.mapping_id), row)
    mappings_with_runs = {
        str(row[0]) for row in db.query(MappingApplyRun.mapping_id).filter(
            MappingApplyRun.mapping_id.in_(mapping_ids or ["__none__"]),
        ).distinct().all()
    }
    result = []
    for m in mappings:
        if m.curated_dataset_id:
            _require_dataset_scope(db, m.curated_dataset_id, user)
        dataset_name = None
        row_count = None
        if m.curated_dataset_id:
            ds = db.query(Dataset).filter(Dataset.id == m.curated_dataset_id).first()
            if ds:
                dataset_name = ds.name
            else:
                cd = db.query(CuratedDataset).filter(CuratedDataset.id == m.curated_dataset_id).first()
                if cd:
                    dataset_name = cd.name
            from app.models.v2.dataset import DatasetVersion
            ver = db.query(DatasetVersion).filter(
                DatasetVersion.dataset_id == m.curated_dataset_id
            ).order_by(DatasetVersion.version_no.desc()).first()
            if ver:
                row_count = ver.rowcount
        result.append({
            "id": m.id,
            "curated_dataset_id": m.curated_dataset_id,
            "dataset_name": dataset_name,
            "row_count": row_count,
            "entity_class": m.entity_class,
            "field_mapping": m.field_mapping,
            "property_mappings": (m.field_mapping or {}).get("__properties__", []),
            "status": m.status,
            "confidence": m.confidence,
            "mapping_digest": mapping_definition_snapshot(m)[1],
            "can_edit": bool(
                user.role in {"admin", "editor"}
                and m.status == "draft"
                and str(m.id) not in mappings_with_runs
                and (
                    str(m.id) not in latest_reviews
                    or latest_reviews[str(m.id)].status in {"rejected", "changes_requested"}
                )
            ),
            "latest_review": (
                serialize_mapping_review(
                    latest_reviews[str(m.id)],
                    actor_id=str(user.id),
                    can_review=user.role in {"admin", "editor"},
                    include_events=True,
                    db=db,
                )
                if str(m.id) in latest_reviews else None
            ),
            "created_at": m.created_at.isoformat() if m.created_at else None,
        })
    return result


@router.get("/{ontology_id}/mappings/{mapping_id}/reviews")
def list_mapping_reviews(
    ontology_id: str,
    mapping_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.v2.mapping import OntologyMapping
    from app.services.v2.mapping_definition_reviews import (
        MappingDefinitionReviewService,
        serialize_mapping_review,
    )

    _require_ontology_scope(db, ontology_id, user)
    mapping = db.query(OntologyMapping).filter(
        OntologyMapping.id == mapping_id,
        OntologyMapping.ontology_id == ontology_id,
    ).first()
    if mapping is None:
        raise HTTPException(404, "Mapping not found")
    if mapping.curated_dataset_id:
        _require_dataset_scope(db, mapping.curated_dataset_id, user)
    service = MappingDefinitionReviewService(db)
    return [
        serialize_mapping_review(
            review,
            actor_id=str(user.id),
            can_review=user.role in {"admin", "editor"},
            include_events=True,
            db=db,
        )
        for review in service.list_reviews(mapping_id)
    ]


@router.post("/{ontology_id}/mappings/{mapping_id}/reviews")
def submit_mapping_review(
    ontology_id: str,
    mapping_id: str,
    body: MappingReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.v2.mapping import OntologyMapping
    from app.services.v2.mapping_definition_reviews import (
        MappingDefinitionReviewService,
        serialize_mapping_review,
    )

    _require_ontology_scope(db, ontology_id, user, write=True)
    mapping = db.query(OntologyMapping).filter(
        OntologyMapping.id == mapping_id,
        OntologyMapping.ontology_id == ontology_id,
    ).first()
    if mapping is None:
        raise HTTPException(404, "Mapping not found")
    if mapping.curated_dataset_id:
        _require_dataset_scope(db, mapping.curated_dataset_id, user, write=True)
    if body.reviewer_id:
        reviewer = db.query(User).filter(
            User.id == body.reviewer_id,
            User.is_active.is_(True),
        ).first()
        if reviewer is None or reviewer.role not in {"admin", "editor"}:
            raise HTTPException(422, "Assigned Mapping reviewer must be an active editor")
        _require_ontology_scope(db, ontology_id, reviewer, write=True)
        if mapping.curated_dataset_id:
            _require_dataset_scope(db, mapping.curated_dataset_id, reviewer, write=True)
    try:
        review, created = MappingDefinitionReviewService(db).submit(
            mapping.id,
            submitted_by=str(user.id),
            reviewer_id=body.reviewer_id,
            candidate=(
                body.candidate.model_dump(exclude_unset=True)
                if body.candidate is not None else None
            ),
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    result = serialize_mapping_review(
        review,
        actor_id=str(user.id),
        can_review=user.role in {"admin", "editor"},
        include_events=True,
        db=db,
    )
    result["deduplicated"] = not created
    return result


@router.post("/{ontology_id}/mappings/{mapping_id}/reviews/{review_id}:decide")
def decide_mapping_review(
    ontology_id: str,
    mapping_id: str,
    review_id: str,
    body: MappingReviewDecisionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.v2.mapping import OntologyMapping
    from app.services.v2.mapping_definition_reviews import (
        MappingDefinitionReviewService,
        serialize_mapping_review,
    )

    _require_ontology_scope(db, ontology_id, user, write=True)
    mapping = db.query(OntologyMapping).filter(
        OntologyMapping.id == mapping_id,
        OntologyMapping.ontology_id == ontology_id,
    ).first()
    if mapping is None:
        raise HTTPException(404, "Mapping not found")
    if mapping.curated_dataset_id:
        _require_dataset_scope(db, mapping.curated_dataset_id, user, write=True)
    try:
        review = MappingDefinitionReviewService(db).decide(
            mapping.id,
            review_id,
            decided_by=str(user.id),
            decision=body.decision,
            notes=body.notes,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return serialize_mapping_review(
        review,
        actor_id=str(user.id),
        can_review=user.role in {"admin", "editor"},
        include_events=True,
        db=db,
    )


@router.post("/{ontology_id}/mappings/{mapping_id}/apply")
def apply_mapping(ontology_id: str, mapping_id: str, data: list[dict], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.v2.mapping import OntologyMapping

    _require_ontology_scope(db, ontology_id, user, write=True)
    mapping = db.query(OntologyMapping).filter(
        OntologyMapping.id == mapping_id,
        OntologyMapping.ontology_id == ontology_id,
    ).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    if mapping.curated_dataset_id:
        _require_dataset_scope(db, mapping.curated_dataset_id, user, write=True)
    raise HTTPException(
        410,
        detail={
            "code": "mapping_apply_requires_review_run",
            "message": "Raw-row Mapping Apply is retired. Submit an approved review ID through apply-from-dataset.",
        },
    )


@router.post("/{ontology_id}/mappings/{mapping_id}/apply-from-dataset")
def apply_mapping_from_dataset(
    ontology_id: str,
    mapping_id: str,
    body: ApplyMappingFromDatasetRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.v2.mapping import OntologyMapping
    from app.models.v2.curated import CuratedReview
    from app.models.v2.mapping_apply_run import MappingApplyRun
    from app.services.v2.mapping_apply_runs import MappingApplyRunService, serialize_mapping_apply_run

    _require_ontology_scope(db, ontology_id, user, write=True)

    mapping = db.query(OntologyMapping).filter(
        OntologyMapping.id == mapping_id,
        OntologyMapping.ontology_id == ontology_id,
    ).first()
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    if not mapping.curated_dataset_id:
        raise HTTPException(400, "Mapping has no curated_dataset_id")
    _require_dataset_scope(db, mapping.curated_dataset_id, user, write=True)

    review = db.query(CuratedReview).filter(
        CuratedReview.id == body.review_id,
        CuratedReview.curated_dataset_id == mapping.curated_dataset_id,
        CuratedReview.status == "approved",
    ).first()
    if review is None:
        raise HTTPException(409, "Mapping Apply requires an approved review for this dataset")
    if not review.dataset_version_id:
        raise HTTPException(409, "Approved review has no immutable dataset version")

    service = MappingApplyRunService(db)
    try:
        run, created = service.enqueue_review_mapping(
            mapping_id=mapping.id,
            ontology_id=ontology_id,
            curated_dataset_id=mapping.curated_dataset_id,
            review_id=review.id,
            mapping_review_id=body.mapping_review_id,
            dataset_version_id=review.dataset_version_id,
            requested_by=str(user.id),
            request_actor_kind="user",
            request_actor_id=str(user.id),
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc

    try:
        result = service.dispatch(run.id)
    except Exception as exc:
        # Keep the committed run recoverable if immediate dispatch is unavailable.
        db.rollback()
        db.expire_all()
        current = db.get(MappingApplyRun, run.id)
        result = serialize_mapping_apply_run(current) if current else {
            "run_id": run.id,
            "status": "pending",
        }
        result["dispatch_error"] = type(exc).__name__
    result["deduplicated"] = not created
    return result


@router.post("/{ontology_id}/mappings/build-all")
def build_all_mappings(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.v2.mapping import OntologyLinkMapping, OntologyMapping
    _require_ontology_scope(db, ontology_id, user, write=True)
    datasets = {
        str(value) for value, in db.query(OntologyMapping.curated_dataset_id).filter(
            OntologyMapping.ontology_id == ontology_id,
            OntologyMapping.curated_dataset_id.is_not(None),
        ).all()
    }
    for source_column in (OntologyLinkMapping.src_dataset_id, OntologyLinkMapping.tgt_dataset_id):
        datasets.update(
            str(value) for value, in db.query(source_column).filter(
                OntologyLinkMapping.ontology_id == ontology_id,
                source_column.is_not(None),
            ).all()
        )
    for dataset_id in sorted(datasets):
        _require_dataset_scope(db, dataset_id, user, write=True)
    raise HTTPException(
        410,
        detail={
            "code": "mapping_build_all_retired",
            "message": "Build-all bypasses the approved-review MappingApplyRun lifecycle. Submit each Mapping with an approved review ID.",
        },
    )


class LinkMappingCreate(BaseModel):
    src_dataset_id: str
    tgt_dataset_id: str
    relation_type: str = Field(min_length=1, max_length=100)
    src_key: str = Field(min_length=1, max_length=100)
    tgt_key: str = Field(min_length=1, max_length=100)


@router.post("/{ontology_id}/link-mappings")
def create_link_mapping(ontology_id: str, body: LinkMappingCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.v2.mapping import OntologyLinkMapping
    from app.services.v2.dataset_service import DatasetService

    _require_ontology_scope(db, ontology_id, user, write=True)
    _require_dataset_scope(db, body.src_dataset_id, user, write=True)
    _require_dataset_scope(db, body.tgt_dataset_id, user, write=True)

    svc = DatasetService(db)
    try:
        _, src_rows, _ = svc.read_latest_complete_snapshot(body.src_dataset_id)
        _, tgt_rows, _ = svc.read_latest_complete_snapshot(body.tgt_dataset_id)
    except Exception as e:
        raise HTTPException(
            409,
            detail={
                "code": "link_mapping_preview_failed",
                "message": "无法读取固定数据快照以验证关系映射；请检查数据版本和服务状态。",
                "error_type": type(e).__name__,
            },
        ) from None
    if not src_rows:
        raise HTTPException(400, "Source dataset has no rows")
    if not tgt_rows:
        raise HTTPException(400, "Target dataset has no rows")
    if body.src_key not in src_rows[0]:
        raise HTTPException(400, f"Source key '{body.src_key}' not found in source dataset")
    if body.tgt_key not in tgt_rows[0]:
        raise HTTPException(400, f"Target key '{body.tgt_key}' not found in target dataset")

    tgt_values = {str(row.get(body.tgt_key, "")).strip() for row in tgt_rows if row.get(body.tgt_key)}
    match_count = sum(1 for row in src_rows if str(row.get(body.src_key, "")).strip() in tgt_values)
    if match_count == 0:
        raise HTTPException(400, "Link mapping produced 0 matches; choose different source/target keys")

    lm = OntologyLinkMapping(
        ontology_id=ontology_id,
        src_dataset_id=body.src_dataset_id,
        tgt_dataset_id=body.tgt_dataset_id,
        relation_type=body.relation_type,
        src_key=body.src_key,
        tgt_key=body.tgt_key,
        status="draft",
    )
    db.add(lm)
    db.commit()
    db.refresh(lm)
    return {"link_mapping_id": lm.id, "relation_type": lm.relation_type, "match_count": match_count}


@router.get("/{ontology_id}/link-mappings")
def list_link_mappings(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.v2.mapping import OntologyLinkMapping
    from app.services.v2.mapping_link_definition_reviews import (
        latest_link_mapping_review,
        serialize_link_mapping_review,
    )

    _require_ontology_scope(db, ontology_id, user)
    links = db.query(OntologyLinkMapping).filter(
        OntologyLinkMapping.ontology_id == ontology_id
    ).all()
    for link in links:
        for dataset_id in (link.src_dataset_id, link.tgt_dataset_id):
            if dataset_id:
                _require_dataset_scope(db, str(dataset_id), user)
    result = []
    for link in links:
        review = latest_link_mapping_review(db, link.id)
        result.append({
            "id": link.id,
            "src_dataset_id": link.src_dataset_id,
            "tgt_dataset_id": link.tgt_dataset_id,
            "relation_type": link.relation_type,
            "src_key": link.src_key,
            "tgt_key": link.tgt_key,
            "status": link.status,
            "can_edit": bool(
                user.role in {"admin", "editor"}
                and link.status in {"draft", "inferred"}
                and (review is None or review.status in {"rejected", "changes_requested"})
            ),
            "latest_review": serialize_link_mapping_review(
                review,
                actor_id=str(user.id),
                can_review=user.role in {"admin", "editor"},
                include_events=True,
                db=db,
            ) if review else None,
        })
    return result


def _load_link_mapping_for_review(db: Session, ontology_id: str, link_mapping_id: str, user: User, *, write: bool):
    from app.models.v2.mapping import OntologyLinkMapping

    _require_ontology_scope(db, ontology_id, user, write=write)
    link = db.query(OntologyLinkMapping).filter(
        OntologyLinkMapping.id == link_mapping_id,
        OntologyLinkMapping.ontology_id == ontology_id,
    ).first()
    if link is None:
        raise HTTPException(404, "Link Mapping not found")
    for dataset_id in (link.src_dataset_id, link.tgt_dataset_id):
        if dataset_id:
            _require_dataset_scope(db, str(dataset_id), user, write=write)
    return link


@router.get("/{ontology_id}/link-mappings/{link_mapping_id}/reviews")
def list_link_mapping_reviews(
    ontology_id: str,
    link_mapping_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.v2.mapping_link_definition_reviews import (
        MappingLinkDefinitionReviewService,
        serialize_link_mapping_review,
    )

    _load_link_mapping_for_review(db, ontology_id, link_mapping_id, user, write=False)
    service = MappingLinkDefinitionReviewService(db)
    return [
        serialize_link_mapping_review(
            review,
            actor_id=str(user.id),
            can_review=user.role in {"admin", "editor"},
            include_events=True,
            db=db,
        )
        for review in service.list_reviews(link_mapping_id)
    ]


@router.post("/{ontology_id}/link-mappings/{link_mapping_id}/reviews")
def submit_link_mapping_review(
    ontology_id: str,
    link_mapping_id: str,
    body: LinkMappingReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.v2.mapping_link_definition_reviews import (
        MappingLinkDefinitionReviewService,
        serialize_link_mapping_review,
    )

    link = _load_link_mapping_for_review(db, ontology_id, link_mapping_id, user, write=True)
    if body.reviewer_id:
        reviewer = db.query(User).filter(
            User.id == body.reviewer_id,
            User.is_active.is_(True),
        ).first()
        if reviewer is None or reviewer.role not in {"admin", "editor"}:
            raise HTTPException(422, "Assigned Link Mapping reviewer must be an active editor")
        _require_ontology_scope(db, ontology_id, reviewer, write=True)
        for dataset_id in (link.src_dataset_id, link.tgt_dataset_id):
            if dataset_id:
                _require_dataset_scope(db, str(dataset_id), reviewer, write=True)
    try:
        review, created = MappingLinkDefinitionReviewService(db).submit(
            link.id,
            submitted_by=str(user.id),
            reviewer_id=body.reviewer_id,
            candidate=body.candidate.model_dump(exclude_unset=True) if body.candidate else None,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    result = serialize_link_mapping_review(
        review,
        actor_id=str(user.id),
        can_review=user.role in {"admin", "editor"},
        include_events=True,
        db=db,
    )
    result["deduplicated"] = not created
    return result


@router.post("/{ontology_id}/link-mappings/{link_mapping_id}/reviews/{review_id}:decide")
def decide_link_mapping_review(
    ontology_id: str,
    link_mapping_id: str,
    review_id: str,
    body: LinkMappingReviewDecisionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.v2.mapping_link_definition_reviews import (
        MappingLinkDefinitionReviewService,
        serialize_link_mapping_review,
    )

    _load_link_mapping_for_review(db, ontology_id, link_mapping_id, user, write=True)
    try:
        review = MappingLinkDefinitionReviewService(db).decide(
            link_mapping_id,
            review_id,
            decided_by=str(user.id),
            decision=body.decision,
            notes=body.notes,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return serialize_link_mapping_review(
        review,
        actor_id=str(user.id),
        can_review=user.role in {"admin", "editor"},
        include_events=True,
        db=db,
    )


@router.post("/{ontology_id}/link-mappings/{link_mapping_id}/apply-preview")
def preview_link_mapping_apply(
    ontology_id: str,
    link_mapping_id: str,
    body: LinkMappingApplyPreviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.v2.mapping_link_apply import LinkApplyError, build_link_mapping_preview
    from app.services.v2.mapping_link_apply_runs import _safe_message

    _load_link_mapping_for_review(db, ontology_id, link_mapping_id, user, write=False)
    try:
        plan = build_link_mapping_preview(
            db,
            link_mapping_id=link_mapping_id,
            ontology_id=ontology_id,
            link_review_id=body.link_review_id,
        )
    except LinkApplyError as exc:
        raise HTTPException(
            409,
            detail={"code": exc.error_code, "message": _safe_message(exc.error_code)},
        ) from None
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            503,
            detail={"code": "link_apply_preview_unavailable", "message": "无法生成关系变更预览。"},
        ) from None
    return {
        "preview_digest": plan["preview_digest"],
        "summary": plan["summary"],
        "source_dataset_version_id": plan["summary"]["source_dataset_version_id"],
        "target_dataset_version_id": plan["summary"]["target_dataset_version_id"],
        "source_mapping_id": plan["summary"]["source_entity_mapping_id"],
        "target_mapping_id": plan["summary"]["target_entity_mapping_id"],
    }


@router.post("/{ontology_id}/link-mappings/{link_mapping_id}:reconciliation-preview")
def preview_legacy_link_relation_reconciliation(
    ontology_id: str,
    link_mapping_id: str,
    body: LinkMappingApplyPreviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(403, "Admin role required")

    from app.services.v2.mapping_link_apply import LinkApplyError, build_link_mapping_preview
    from app.services.v2.mapping_link_apply_runs import _safe_message

    _load_link_mapping_for_review(db, ontology_id, link_mapping_id, user, write=False)
    try:
        plan = build_link_mapping_preview(
            db,
            link_mapping_id=link_mapping_id,
            ontology_id=ontology_id,
            link_review_id=body.link_review_id,
            include_legacy_relation_report=True,
        )
    except LinkApplyError as exc:
        raise HTTPException(
            409,
            detail={"code": exc.error_code, "message": _safe_message(exc.error_code)},
        ) from None
    except Exception:
        db.rollback()
        raise HTTPException(
            503,
            detail={"code": "legacy_relation_reconciliation_unavailable", "message": "无法生成旧关系只读对账预览。"},
        ) from None

    return {
        "preview_digest": plan["preview_digest"],
        "summary": plan["summary"],
        "legacy_reconciliation": plan["legacy_reconciliation"],
    }


@router.post("/{ontology_id}/link-mappings/{link_mapping_id}/apply")
def apply_link_mapping(
    ontology_id: str,
    link_mapping_id: str,
    body: LinkMappingApplyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.v2.mapping_link_apply import LinkApplyError
    from app.services.v2.mapping_link_apply_runs import (
        MappingLinkApplyRunService,
        _safe_message,
        serialize_mapping_link_apply_run,
    )

    _load_link_mapping_for_review(db, ontology_id, link_mapping_id, user, write=True)
    try:
        service = MappingLinkApplyRunService(db)
        run, created = service.enqueue_approved_link_mapping(
            link_mapping_id=link_mapping_id,
            ontology_id=ontology_id,
            link_review_id=body.link_review_id,
            preview_digest=body.preview_digest,
            requested_by=str(user.id),
            client_request_id=body.client_request_id,
        )
        result = service.dispatch(run.id) if created or run.status in {"pending", "retry_wait"} else serialize_mapping_link_apply_run(run)
    except LinkApplyError as exc:
        db.rollback()
        raise HTTPException(
            409,
            detail={"code": exc.error_code, "message": _safe_message(exc.error_code)},
        ) from None
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(409, detail={"code": "link_apply_request_invalid", "message": "关系映射输入已变化，请重新预览。"}) from None
    result["deduplicated"] = not created
    return result


@router.get("/{ontology_id}/link-mappings/{link_mapping_id}/apply-runs")
def list_link_mapping_apply_runs(
    ontology_id: str,
    link_mapping_id: str,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.v2.mapping_link_apply_run import MappingLinkApplyRun
    from app.services.v2.mapping_link_apply_runs import serialize_mapping_link_apply_run
    from app.services.v2.mapping_link_projection import mapping_link_projection_summary

    _load_link_mapping_for_review(db, ontology_id, link_mapping_id, user, write=False)
    bounded = min(max(int(limit), 1), 100)
    runs = db.query(MappingLinkApplyRun).filter(
        MappingLinkApplyRun.ontology_id == ontology_id,
        MappingLinkApplyRun.link_mapping_id == link_mapping_id,
    ).order_by(MappingLinkApplyRun.created_at.desc(), MappingLinkApplyRun.id.desc()).limit(bounded).all()
    result = []
    for run in runs:
        item = serialize_mapping_link_apply_run(run)
        item["projection_summary"] = mapping_link_projection_summary(db, run.id)
        result.append(item)
    return result


@router.get("/{ontology_id}/link-mappings/{link_mapping_id}/apply-runs/{run_id}/projection")
def get_link_mapping_projection_summary(
    ontology_id: str,
    link_mapping_id: str,
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.v2.mapping_link_apply_run import MappingLinkApplyRun
    from app.services.v2.mapping_link_projection import mapping_link_projection_summary

    _load_link_mapping_for_review(db, ontology_id, link_mapping_id, user, write=False)
    run = db.query(MappingLinkApplyRun).filter_by(
        id=run_id, ontology_id=ontology_id, link_mapping_id=link_mapping_id,
    ).first()
    if run is None:
        raise HTTPException(404, "Link Mapping Apply run not found")
    return mapping_link_projection_summary(db, run.id)


@router.post("/{ontology_id}/link-mappings/{link_mapping_id}/apply-runs/{run_id}:reconcile-projection", status_code=202)
def replay_link_mapping_projection(
    ontology_id: str,
    link_mapping_id: str,
    run_id: str,
    body: LinkMappingProjectionReplayBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.v2.mapping_link_apply_run import MappingLinkApplyRun
    from app.services.v2.mapping_link_projection import (
        mapping_link_projection_summary,
        replay_link_relation_projection,
    )

    if user.role != "admin":
        raise HTTPException(403, "Admin role required for projection replay")
    link = _load_link_mapping_for_review(db, ontology_id, link_mapping_id, user, write=True)
    run = db.query(MappingLinkApplyRun).filter_by(
        id=run_id, ontology_id=ontology_id, link_mapping_id=link_mapping_id,
    ).first()
    if run is None:
        raise HTTPException(404, "Link Mapping Apply run not found")
    scope = {
        "run_id": run.id,
        "ontology_id": run.ontology_id,
        "link_mapping_id": link.id,
    }
    try:
        result = replay_link_relation_projection(
            db,
            run_id=run.id,
            actor_id=str(user.id),
            actor_role=user.role,
            client_request_id=str(body.client_request_id),
            reason=body.reason,
            authorization_scope=scope,
            batch_size=body.batch_size,
        )
    except LookupError as exc:
        raise HTTPException(404, "Link Mapping Apply run not found") from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, {"code": "projection_replay_conflict", "message": str(exc)}) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            503,
            {"code": "projection_replay_unavailable", "message": "关系投影人工重放未完成，请查看运行记录后再试。"},
        ) from None
    result["projection"] = mapping_link_projection_summary(db, run.id)
    return result
