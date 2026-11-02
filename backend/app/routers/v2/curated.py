"""v2 Curated Dataset API — reads from v2_datasets kind=curated"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional
from app.database import SessionLocal
from app.deps import get_current_user, require_admin, require_editor
from app.models.user import User
from app.models.v2.curated import CuratedDataset, CuratedReview, CuratedRowEdit

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _latest_dataset_version_id(db: Session, dataset_id: str) -> str | None:
    from app.services.v2.dataset_service import DatasetService, DatasetStorageError
    try:
        return DatasetService(db).latest_version(dataset_id).id
    except DatasetStorageError:
        return None


def _dataset_protected_row_fields(db: Session, dataset_id: str) -> set[str]:
    """Return source identity columns used by existing Ontology mappings."""
    from app.models.v2.mapping import OntologyMapping

    protected = {"id", "__pk__"}
    rows = db.query(OntologyMapping.field_mapping).filter(
        OntologyMapping.curated_dataset_id == dataset_id,
    ).all()
    for (field_mapping,) in rows:
        if not isinstance(field_mapping, dict):
            continue
        primary_key = field_mapping.get("__primary_key__")
        if primary_key and str(primary_key) != "__row_hash__":
            protected.add(str(primary_key))
    return protected


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


def _require_dataset_access(db: Session, dataset_id: str, user: User, *, write: bool = False) -> None:
    from app.models.v2.dataset import Dataset
    from app.models.v2.pipeline import Pipeline

    legacy = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    dataset = db.query(Dataset).filter(
        Dataset.id == dataset_id,
        Dataset.kind == "curated",
    ).first()
    if legacy is None and dataset is None:
        raise HTTPException(404, "Curated dataset not found")
    if write and user.role not in {"admin", "editor"}:
        raise HTTPException(403, "Editor role required")
    if user.role == "admin":
        return

    ontology_ids = _dataset_ontology_ids(db, dataset_id)
    if ontology_ids:
        from app.services.v2.object_query.authorization import require_ontology_access
        from app.services.v2.object_query.errors import ObjectQueryError

        try:
            for ontology_id in sorted(ontology_ids):
                require_ontology_access(db, ontology_id, str(user.id), user.role)
        except ObjectQueryError:
            raise HTTPException(404, "Curated dataset not found") from None
        return

    # Datasets without an Ontology mapping have no policy scope to inherit.
    # Allow only the owning Pipeline's creator until a resource is mapped.
    pipeline_id = str(legacy.pipeline_id) if legacy and legacy.pipeline_id else None
    pipeline = db.get(Pipeline, pipeline_id) if pipeline_id else None
    if pipeline is None or str(pipeline.created_by or "") != str(user.id):
        raise HTTPException(404, "Curated dataset not found")


def _require_review_access(db: Session, review: CuratedReview, user: User, *, write: bool = False) -> None:
    if user.role == "admin":
        return
    actor_ids = {str(review.requested_by or ""), str(review.reviewer_id or "")}
    if str(user.id) not in actor_ids:
        raise HTTPException(404, "Review not found")
    _require_dataset_access(db, review.curated_dataset_id, user, write=write)


def _review_payload(review: CuratedReview) -> dict:
    return {
        "review_id": review.id,
        "curated_dataset_id": getattr(review, "curated_dataset_id", None),
        "status": review.status,
        "dataset_version_id": review.dataset_version_id,
        "requested_by": getattr(review, "requested_by", None),
        "reviewer_id": getattr(review, "reviewer_id", None),
        "decided_by": getattr(review, "decided_by", None),
        "notes": getattr(review, "notes", None),
        "created_at": getattr(review, "created_at", None),
        "decided_at": getattr(review, "decided_at", None),
    }


def _trigger_mapping_apply_after_approval(db: Session, review_id: str) -> dict:
    from app.services.v2.incremental.orchestrator import IncrementalOrchestrator
    try:
        return IncrementalOrchestrator(db).on_review_approved(review_id)
    except Exception as exc:
        # Approval and durable Mapping Apply rows share a transaction. Roll
        # back the decision when its required run records cannot be persisted.
        db.rollback()
        logger.exception("Mapping trigger failed after review approve %s", review_id)
        raise HTTPException(
            503,
            detail={"status": "approval_not_persisted", "review_id": review_id,
                    "error": type(exc).__name__},
        ) from exc


class CuratedDatasetResponse(BaseModel):
    id: str
    name: str
    status: str
    row_count: Optional[int] = None
    quality_score: Optional[float] = None
    quality_status: Optional[str] = None

    class Config:
        from_attributes = True


class MappingProjectionReconcileBody(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
    client_request_id: str = Field(min_length=1, max_length=120)


@router.get("", response_model=list[CuratedDatasetResponse])
def list_curated(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """列出所有 Curated Dataset（从 v2_datasets 读 kind=curated）"""
    from app.models.v2.dataset import Dataset, DatasetVersion
    rows = db.query(Dataset).filter(Dataset.kind == "curated").order_by(Dataset.created_at.desc()).all()
    visible_rows = []
    for row in rows:
        try:
            _require_dataset_access(db, row.id, user)
            visible_rows.append(row)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    rows = visible_rows
    # Batch fetch all reviews for displayed datasets to avoid N+1
    dataset_ids = [r.id for r in rows]
    all_reviews = db.query(CuratedReview).filter(
        CuratedReview.curated_dataset_id.in_(dataset_ids)
    ).order_by(CuratedReview.created_at.desc()).all()

    # Build dict: dataset_id -> latest review
    review_by_dataset: dict = {}
    for rev in all_reviews:
        if rev.curated_dataset_id not in review_by_dataset:
            review_by_dataset[rev.curated_dataset_id] = rev

    result = []
    for r in rows:
        ver = db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == r.id
        ).order_by(DatasetVersion.version_no.desc()).first()
        # 从 schema_json 读质量分
        quality = None
        quality_status = None
        if r.schema_json and isinstance(r.schema_json, dict):
            quality = r.schema_json.get("quality_score")
            quality_status = r.schema_json.get("quality_status")
        review = review_by_dataset.get(r.id)
        real_status = review.status if review else "pending_review"
        result.append(CuratedDatasetResponse(
            id=r.id, name=r.name,
            status=real_status,
            row_count=ver.rowcount if ver else None,
            quality_score=quality,
            quality_status=quality_status,
        ))
    return result


@router.delete("/{dataset_id}", status_code=204)
def delete_curated(
    dataset_id: str,
    cascade: bool = Query(False, description="显式确认同时删除审核记录与行编辑历史"),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """删除 Curated Dataset 及其版本数据（仅管理员）"""
    from app.models.v2.dataset import Dataset, DatasetVersion, MediaItem
    ds = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.kind == "curated").first()
    if not ds:
        raise HTTPException(404, "Curated dataset not found")
    review_ids = [x[0] for x in db.query(CuratedReview.id).filter(
        CuratedReview.curated_dataset_id == dataset_id,
    ).all()]
    if review_ids and not cascade:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "CURATED_REVIEWS_REFERENCED",
                "review_count": len(review_ids),
                "message": "审核记录固定了数据版本；如确认同时删除审核与行编辑历史，请显式 cascade=true",
            },
        )
    if review_ids:
        db.query(CuratedRowEdit).filter(CuratedRowEdit.review_id.in_(review_ids)).delete(
            synchronize_session=False,
        )
        db.query(CuratedReview).filter(CuratedReview.id.in_(review_ids)).delete(
            synchronize_session=False,
        )
    # 清理关联版本和媒体项
    versions = db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset_id).all()
    ver_ids = [version.id for version in versions]
    storage_uris = [version.storage_uri for version in versions if version.storage_uri]
    if ver_ids:
        storage_uris.extend(
            item.storage_uri for item in db.query(MediaItem).filter(
                MediaItem.dataset_version_id.in_(ver_ids),
            ).all()
        )
        db.query(MediaItem).filter(MediaItem.dataset_version_id.in_(ver_ids)).delete(synchronize_session=False)
    db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset_id).delete(synchronize_session=False)
    db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).delete(synchronize_session=False)
    db.delete(ds)
    db.commit()
    try:
        from app.services.storage_service import get_storage_service

        storage = get_storage_service()
    except Exception as exc:
        logger.warning(
            "Curated storage cleanup unavailable (dataset_id=%s, error_type=%s)",
            dataset_id,
            type(exc).__name__,
        )
        storage = None
    for uri in storage_uris:
        if storage is None:
            break
        try:
            storage.delete_object(uri)
        except Exception as exc:
            logger.warning(
                "Curated storage cleanup failed (dataset_id=%s, error_type=%s)",
                dataset_id,
                type(exc).__name__,
            )


@router.get("/{dataset_id}", response_model=CuratedDatasetResponse)
def get_curated(dataset_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_dataset_access(db, dataset_id, user)
    # 先查旧 curated 表，再查 v2_datasets
    ds = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    if not ds:
        from app.models.v2.dataset import Dataset
        d2 = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.kind == "curated").first()
        if d2:
            schema = d2.schema_json if isinstance(d2.schema_json, dict) else {}
            return CuratedDatasetResponse(
                id=d2.id,
                name=d2.name,
                status="pending_review",
                quality_score=schema.get("quality_score"),
                quality_status=schema.get("quality_status"),
            )
    if not ds:
        raise HTTPException(404, "Curated dataset not found")
    schema = ds.schema_json if isinstance(ds.schema_json, dict) else {}
    return CuratedDatasetResponse(
        id=ds.id,
        name=ds.name,
        status=ds.status,
        quality_score=schema.get("quality_score"),
        quality_status=schema.get("quality_status"),
    )


@router.get("/{dataset_id}/preview")
def preview_curated(
    dataset_id: str,
    limit: int = Query(100, ge=1, le=1000),
    version_id: str | None = None,
    review_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """数据预览 — 从 v2_datasets 存储读取实际数据行"""
    from app.services.v2.dataset_service import DatasetService
    from app.models.v2.dataset import Dataset as Ds2
    from app.services.v2.curated.review_service import ReviewService
    _require_dataset_access(db, dataset_id, user)

    # 尝试旧 curated 表
    ds = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    if ds:
        name = ds.name
    else:
        d2 = db.query(Ds2).filter(Ds2.id == dataset_id, Ds2.kind == "curated").first()
        if not d2:
            raise HTTPException(404, "Curated dataset not found")
        name = d2.name

    # A review preview is always served from that review's pinned immutable version.
    if review_id:
        review = db.query(CuratedReview).filter(CuratedReview.id == review_id).first()
        if review is None or str(review.curated_dataset_id) != str(dataset_id):
            raise HTTPException(404, "Review not found")
        _require_review_access(db, review, user)
        if not review.dataset_version_id:
            raise HTTPException(409, "Review has no pinned dataset version")
        if version_id and str(version_id) != str(review.dataset_version_id):
            raise HTTPException(409, "Requested version does not match the review")
        version_id = str(review.dataset_version_id)

    # Read either the explicitly requested snapshot or the current latest.
    try:
        svc = DatasetService(db)
        if version_id:
            version, rows = svc.preview_version_id(dataset_id, version_id, limit=limit)
        else:
            version, rows = svc.preview_latest(dataset_id, limit=limit)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Curated preview unavailable (dataset_id=%s, error_type=%s)",
            dataset_id,
            type(exc).__name__,
        )
        return {"dataset_id": dataset_id, "name": name, "rows": [],
                "row_tokens": [], "count": 0, "dataset_version_id": None,
                "latest_dataset_version_id": _latest_dataset_version_id(db, dataset_id),
                "review_edits": {"applied": 0, "outside_preview": 0},
                "error": "Dataset preview is unavailable; verify the selected dataset version."}

    row_tokens = [
        ReviewService.row_edit_token(version.id, index, row)
        for index, row in enumerate(rows)
    ]
    edit_preview = {"applied": 0, "outside_preview": 0}
    protected_fields = _dataset_protected_row_fields(db, dataset_id)
    if review_id:
        try:
            rows, edit_preview = ReviewService(db).apply_edits_to_preview(
                review_id,
                version.id,
                rows,
                protected_fields=protected_fields,
            )
        except ValueError:
            raise HTTPException(
                409,
                "Saved review edits do not match the pinned dataset version; reload or start a new review.",
            ) from None
    return {
        "dataset_id": dataset_id,
        "name": name,
        "rows": rows,
        "row_tokens": row_tokens,
        "count": len(rows),
        "dataset_version_id": version.id,
        "latest_dataset_version_id": _latest_dataset_version_id(db, dataset_id),
        "protected_fields": sorted(protected_fields),
        "review_edits": edit_preview,
    }


@router.get("/{dataset_id}/quality")
def get_quality_report(dataset_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """获取质量报告（支持旧 curated 表和新 v2_datasets curated）"""
    from app.services.v2.curated.quality_service import QualityService
    from app.services.v2.dataset_service import DatasetService
    _require_dataset_access(db, dataset_id, user)

    # 查旧 curated 表
    ds = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    sample_data = []
    if ds:
        if ds.schema_json and isinstance(ds.schema_json, dict):
            sample_data = ds.schema_json.get("sample_rows", [])
    else:
        # 尝试从 v2_datasets 读取样本数据
        from app.models.v2.dataset import Dataset as Ds2
        d2 = db.query(Ds2).filter(Ds2.id == dataset_id, Ds2.kind == "curated").first()
        if not d2:
            raise HTTPException(404, "Curated dataset not found")
        # 读最新版本数据作为样本
        try:
            svc2 = DatasetService(db)
            _, sample_data = svc2.preview_latest(dataset_id, limit=200)
        except Exception:
            sample_data = []

    svc = QualityService(db)
    report = svc.compute_report(dataset_id, sample_data)
    return report.to_dict()


@router.post("/{dataset_id}/review")
def submit_review(
    dataset_id: str,
    action: str,  # "approve" | "reject"
    notes: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),  # PRD Security Logic: only admin can approve curated rows
):
    """Create an independent review request or record a rejection."""
    _require_dataset_access(db, dataset_id, user, write=True)
    if action not in {"approve", "reject"}:
        raise HTTPException(422, "action must be approve or reject")
    if action == "approve":
        pending = db.query(CuratedReview).filter(
            CuratedReview.curated_dataset_id == dataset_id,
            CuratedReview.status == "pending",
        ).order_by(CuratedReview.created_at.desc(), CuratedReview.id.asc()).first()
        if pending:
            _require_review_access(db, pending, user)
            return _review_payload(pending)
        from app.services.v2.curated.review_service import ReviewService
        review = ReviewService(db).start_review(
            dataset_id,
            requested_by=str(user.id),
            expected_dataset_version_id=_latest_dataset_version_id(db, dataset_id),
        )
        return _review_payload(review)

    ds = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
    if not ds:
        from app.models.v2.dataset import Dataset
        ds_v2 = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.kind == "curated").first()
        if not ds_v2:
            raise HTTPException(404, "Curated dataset not found")
        # Pipeline creates curated data in v2_datasets, but CuratedReview FK points to
        # v2_curated_datasets. If the CuratedDataset row doesn't exist yet, sync it now
        # so the FK constraint doesn't fail on PostgreSQL (SQLite silently ignores FK).
        ds = CuratedDataset(
            id=ds_v2.id,
            name=ds_v2.name,
            pipeline_id=getattr(ds_v2, "pipeline_id", None),
            schema_json=ds_v2.schema_json,
            status="draft",
        )
        db.add(ds)
        db.flush()

    from datetime import datetime, timezone
    review = CuratedReview(
        curated_dataset_id=dataset_id,
        status="rejected",
        dataset_version_id=_latest_dataset_version_id(db, dataset_id),
        requested_by=str(user.id),
        decided_by=str(user.id),
        notes=notes,
        decided_at=datetime.now(timezone.utc),
    )
    db.add(review)
    if ds:
        ds.status = "rejected"
    db.flush()
    db.commit()
    mapping_apply = None
    db.refresh(review)

    return {**_review_payload(review), "mapping_apply": mapping_apply}


# ── 审核工作流端点 ─────────────────────────────────────────────────────

class RowEditRequest(BaseModel):
    row_pk: str = Field(min_length=1, max_length=200)
    field_name: str = Field(min_length=1, max_length=200)
    old_value: str = Field(max_length=10000)
    new_value: str = Field(max_length=10000)


class BatchEditRequest(BaseModel):
    edits: list[RowEditRequest] = Field(min_length=1, max_length=500)


@router.post("/{dataset_id}/reviews")
def start_review(
    dataset_id: str,
    expected_version_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_editor),
):
    """为数据集启动审核流程"""
    _require_dataset_access(db, dataset_id, user, write=True)
    from app.services.v2.curated.review_service import ReviewService
    svc = ReviewService(db)
    review = svc.start_review(
        dataset_id,
        requested_by=str(user.id),
        expected_dataset_version_id=expected_version_id,
    )
    return _review_payload(review)


@router.get("/{dataset_id}/reviews/latest")
def get_latest_dataset_review(
    dataset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    review = db.query(CuratedReview).filter(
        CuratedReview.curated_dataset_id == dataset_id,
    ).order_by(CuratedReview.created_at.desc(), CuratedReview.id.asc()).first()
    if review is None:
        raise HTTPException(404, "Review not found")
    _require_review_access(db, review, user)
    return _review_payload(review)


@router.get("/{dataset_id}/reviews/latest-approved")
def get_latest_approved_dataset_review(
    dataset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Expose only the approved version reference to users with dataset access."""
    _require_dataset_access(db, dataset_id, user)
    review = db.query(CuratedReview).filter(
        CuratedReview.curated_dataset_id == dataset_id,
    ).order_by(CuratedReview.created_at.desc(), CuratedReview.id.asc()).first()
    if review is None or review.status != "approved":
        raise HTTPException(409, "The latest dataset review is not approved")
    return {
        "review_id": review.id,
        "curated_dataset_id": review.curated_dataset_id,
        "status": review.status,
        "dataset_version_id": review.dataset_version_id,
        "requested_by": review.requested_by,
        "decided_by": review.decided_by,
    }


@router.get("/reviews/{review_id}")
def get_review(review_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """获取审核记录详情"""
    from app.models.v2.curated import CuratedReview
    review = db.query(CuratedReview).filter(CuratedReview.id == review_id).first()
    if not review:
        raise HTTPException(404, "Review not found")
    _require_review_access(db, review, user)
    return _review_payload(review)


def _row_edit_payload(edit) -> dict:
    return {
        "id": edit.id,
        "review_id": edit.review_id,
        "edited_by": getattr(edit, "edited_by", None),
        "row_pk": edit.row_pk,
        "field_name": edit.field_name,
        "old_value": edit.old_value,
        "new_value": edit.new_value,
        "edited_at": edit.edited_at.isoformat() if edit.edited_at else None,
    }


@router.get("/reviews/{review_id}/edits")
def list_review_edits(
    review_id: str,
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return the append-only, actor-attributed row edit history."""
    review = db.query(CuratedReview).filter(CuratedReview.id == review_id).first()
    if review is None:
        raise HTTPException(404, "Review not found")
    _require_dataset_access(db, review.curated_dataset_id, user)
    from app.models.v2.curated import CuratedRowEdit

    query = db.query(CuratedRowEdit).filter(
        CuratedRowEdit.review_id == review.id,
    )
    total = query.count()
    edits = query.order_by(
        CuratedRowEdit.edited_at.desc(), CuratedRowEdit.id.desc(),
    ).limit(limit).all()
    return {
        "review_id": review.id,
        "total": total,
        "edits": [_row_edit_payload(edit) for edit in edits],
    }


@router.post("/reviews/{review_id}/edits")
def add_edit(
    review_id: str,
    body: BatchEditRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_editor),
):
    """批量提交行编辑"""
    from app.services.v2.curated.review_service import ReviewService, ROW_EDIT_TOKEN_PREFIX
    from app.models.v2.curated import CuratedRowEdit
    from app.services.v2.dataset_service import DatasetService
    review = db.query(CuratedReview).filter(
        CuratedReview.id == review_id,
    ).with_for_update().first()
    if review is None:
        raise HTTPException(404, "Review not found")
    _require_review_access(db, review, user, write=True)
    svc = ReviewService(db)
    if review.status != "pending":
        raise HTTPException(409, "Only pending reviews can be edited")
    if not review.dataset_version_id:
        raise HTTPException(409, "Row edits require a review with a pinned dataset version")
    try:
        version, preview_rows = DatasetService(db).preview_version_id(
            review.curated_dataset_id,
            str(review.dataset_version_id),
            limit=500,
        )
        protected_fields = _dataset_protected_row_fields(db, review.curated_dataset_id)
        candidates = [
            CuratedRowEdit(
                review_id=review_id,
                edited_by=str(user.id),
                row_pk=edit.row_pk,
                field_name=edit.field_name,
                old_value=edit.old_value,
                new_value=edit.new_value,
            )
            for edit in body.edits
        ]
        for edit in candidates:
            if edit.row_pk.startswith(ROW_EDIT_TOKEN_PREFIX):
                token_version, row_index = svc._token_coordinates(edit.row_pk)
                if token_version != str(version.id) or row_index >= len(preview_rows):
                    raise ValueError("REVIEW_EDIT_ROW_TOKEN_INVALID")
                if svc.row_edit_token(version.id, row_index, preview_rows[row_index]) != edit.row_pk:
                    raise ValueError("REVIEW_EDIT_ROW_TOKEN_INVALID")
                continue

            matches = []
            for row_index, row in enumerate(preview_rows):
                aliases = svc._legacy_row_aliases(row_index, row)
                for field_name in protected_fields:
                    value = row.get(field_name)
                    if isinstance(value, (str, int, float, bool)):
                        aliases.add(str(value))
                if edit.row_pk in aliases:
                    matches.append(row_index)
            if len(matches) != 1:
                raise ValueError("REVIEW_EDIT_ROW_IDENTITY_INVALID")

        # Validate the visible edit history and the new batch against the
        # pinned source before appending any audit rows.
        svc.apply_edits_to_preview(
            review_id,
            version.id,
            preview_rows,
            protected_fields=protected_fields,
            additional_edits=candidates,
        )
    except ValueError:
        raise HTTPException(409, "Edits do not match the pinned dataset rows or current cell values") from None
    except Exception as exc:
        logger.warning(
            "Curated edit validation unavailable (review_id=%s, error_type=%s)",
            review_id,
            type(exc).__name__,
        )
        raise HTTPException(409, "Pinned dataset version is unavailable for edit validation") from None

    edits = svc.batch_edit_rows(
        review_id,
        [edit.model_dump() for edit in body.edits],
        edited_by=str(user.id),
    )
    return {"saved": len(edits), "edits": [_row_edit_payload(edit) for edit in edits]}


@router.post("/reviews/{review_id}/approve")
def approve_review(review_id: str, notes: str = "", db: Session = Depends(get_db),
                   _admin: User = Depends(require_admin)):
    """审核通过"""
    from app.services.v2.curated.review_service import ReviewService
    svc = ReviewService(db)
    review = svc.approve(
        review_id, notes,
        decided_by=str(_admin.id) if _admin is not None else None,
        commit=False,
    )
    mapping_apply = _trigger_mapping_apply_after_approval(db, review.id)
    return {**_review_payload(review), "mapping_apply": mapping_apply}


@router.get("/reviews/{review_id}/mapping-runs")
def list_review_mapping_runs(
    review_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return durable Mapping Apply states for a reviewed dataset snapshot."""
    review = db.query(CuratedReview).filter(CuratedReview.id == review_id).first()
    if review is None:
        raise HTTPException(404, "Review not found")
    _require_dataset_access(db, review.curated_dataset_id, user)

    from app.models.v2.mapping_apply_run import MappingApplyRun
    from app.services.v2.mapping_apply_runs import serialize_mapping_apply_run
    from app.services.v2.mapping_row_projection import mapping_row_projection_summary

    runs = db.query(MappingApplyRun).filter(
        MappingApplyRun.review_id == review.id,
        MappingApplyRun.curated_dataset_id == review.curated_dataset_id,
    ).order_by(MappingApplyRun.created_at.desc(), MappingApplyRun.id.asc()).limit(limit).all()
    return {
        "review_id": review.id,
        "dataset_version_id": review.dataset_version_id,
        "runs": [
            {
                **serialize_mapping_apply_run(run),
                "projection": mapping_row_projection_summary(
                    db, run.id, include_attempts=(user.role == "admin"),
                ),
            }
            for run in runs
        ],
    }


@router.get("/reviews/{review_id}/mapping-runs/{run_id}/projection-attempts")
def list_mapping_projection_attempts(
    review_id: str,
    run_id: str,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    review = db.query(CuratedReview).filter_by(id=review_id).first()
    if review is None:
        raise HTTPException(404, "Review not found")
    from app.models.v2.mapping_apply_run import MappingApplyRun
    from app.services.v2.mapping_row_projection import mapping_row_projection_summary

    run = db.query(MappingApplyRun).filter_by(
        id=run_id, review_id=review.id, curated_dataset_id=review.curated_dataset_id,
    ).first()
    if run is None:
        raise HTTPException(404, "Mapping Apply run not found")
    summary = mapping_row_projection_summary(db, run.id, limit=limit, include_attempts=True)
    return {"run_id": run.id, "projection": summary}


@router.post("/reviews/{review_id}/mapping-runs/{run_id}:reconcile-projection")
def reconcile_mapping_row_projection(
    review_id: str,
    run_id: str,
    body: MappingProjectionReconcileBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    review = db.query(CuratedReview).filter_by(id=review_id).first()
    if review is None:
        raise HTTPException(404, "Review not found")
    from app.models.v2.mapping_apply_run import MappingApplyRun
    from app.services.v2.mapping_row_projection import (
        mapping_row_projection_summary,
        project_mapping_row_changes,
    )

    run = db.query(MappingApplyRun).filter_by(
        id=run_id, review_id=review.id, curated_dataset_id=review.curated_dataset_id,
    ).first()
    if run is None:
        raise HTTPException(404, "Mapping Apply run not found")
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(422, {"code": "reason_required", "message": "人工重放原因不能为空"})
    client_request_id = body.client_request_id.strip()
    if not client_request_id:
        raise HTTPException(422, {"code": "request_id_required", "message": "请求编号不能为空"})
    try:
        result = project_mapping_row_changes(
            db,
            mapping_apply_run_id=run.id,
            batch_size=500,
            manual_actor_id=str(user.id),
            manual_reason=reason,
            manual_request_id=client_request_id,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, {"code": "projection_replay_conflict", "message": str(exc)}) from exc
    return {
        "run_id": run.id,
        "result": result,
        "projection": mapping_row_projection_summary(db, run.id),
    }


@router.post("/reviews/{review_id}/reject")
def reject_review(review_id: str, notes: str = "", db: Session = Depends(get_db),
                  _admin: User = Depends(require_admin)):
    """审核拒绝"""
    from app.services.v2.curated.review_service import ReviewService
    svc = ReviewService(db)
    review = svc.reject(
        review_id, notes,
        decided_by=str(_admin.id) if _admin is not None else None,
    )
    return _review_payload(review)
