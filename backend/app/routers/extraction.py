"""Compatibility endpoints for legacy extraction task history.

The direct-write extraction flow is retired. New ontology builds must use the
versioned Construction workflow, which holds generated mappings for explicit
review before publishing a new revision.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user, require_editor
from app.models.extraction_task import ExtractionTask
from app.schemas.extraction import ExtractionTaskOut
from app.services.legacy_extraction_authorization import (
    LegacyExtractionAccessError,
    require_extraction_task_visibility,
    require_legacy_extraction_actor,
)


router = APIRouter()


@router.post("")
def start_extraction(
    ontology_id: str,
    db: Session = Depends(get_db),
    user=Depends(require_editor),
):
    try:
        require_legacy_extraction_actor(db, ontology_id, user)
    except LegacyExtractionAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    raise HTTPException(status_code=410, detail={
        "code": "legacy_extraction_retired",
        "message": "旧版文档抽取会直接写入本体，现已关闭。请使用常规数据构建流程固定来源版本、确认映射并构建新修订。",
        "next_path": f"/data/regular/build?ontology_id={ontology_id}",
    })


@router.get("/status")
def get_extraction_status(
    ontology_id: str,
    task_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    task = db.query(ExtractionTask).filter(
        ExtractionTask.id == task_id,
        ExtractionTask.ontology_id == ontology_id,
    ).first()
    if not task:
        raise HTTPException(404, "Task not found")
    try:
        require_extraction_task_visibility(db, ontology_id, task, user)
    except LegacyExtractionAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    payload = ExtractionTaskOut.model_validate(task).model_dump()
    payload["parameters"] = {
        key: value for key, value in (payload.get("parameters") or {}).items()
        if not str(key).startswith("_")
    }
    return {"data": payload}
