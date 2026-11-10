"""Import data as an ontology: prepared demo bundles, files of any kind, and the steps of a replayed event stream. Imports run as jobs that can be watched."""
from __future__ import annotations

import dataclasses
import json
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_editor
from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.models.user import User
from app.services.storage_service import get_storage_service
from app.services.v2.data_import import catalog, profile, runner, uploads
from app.services.v2.data_import.model import snake
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError

router = APIRouter(dependencies=[Depends(get_current_user)])
_UPLOADS: dict[str, dict[str, Any]] = {}
_UPLOADS_LOCK = threading.Lock()
UPLOAD_SECONDS = 3600


def fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _counts(db: Session, ontology_id: str) -> int:
    return int(db.query(func.count(EntityInstance.id)).filter(EntityInstance.ontology_id == ontology_id, EntityInstance.is_deleted.is_(False)).scalar() or 0)


class BundleImport(BaseModel):
    replace: bool = False


class FileImport(BaseModel):
    ontology_name: str = Field(min_length=1, max_length=200)
    replace: bool = False
    replay: dict[str, Any] | None = None


class Advance(BaseModel):
    count: int | None = Field(default=None, ge=1, le=5000)


@router.get("/bundles")
def list_bundles(db: Session = Depends(get_db)):
    """The prepared data sets, and for each the ontology it has already been imported as."""
    out = []
    for entry in catalog.ENTRIES:
        project = db.query(OntologyProject).filter(OntologyProject.name.ilike(entry.title)).first()
        imported = {"ontology_id": project.id, "name": project.name, "objects": _counts(db, project.id)} if project else None
        out.append({"id": entry.id, "title": entry.title, "summary": entry.summary, "domain": entry.domain, "kind": entry.kind, "tags": entry.tags, "shows": entry.shows,
                    "replay": entry.replay, "sources": [dataclasses.asdict(s) for s in entry.sources], "imported": imported})
    return {"bundles": out}


def _start(title: str, user: User, load, replace: bool) -> dict[str, Any]:
    if replace and user.role != "admin":
        raise fail(403, "admin_required", "替换已有的本体要管理员账号（会删除它和它的对象）")
    try:
        return runner.start(title, str(user.id), load, replace=replace).public()
    except RuntimeError as exc:
        raise fail(409, "busy", str(exc)) from exc


@router.post("/bundles/{bundle_id}:import", status_code=202)
def import_bundle(bundle_id: str, body: BundleImport = Body(default_factory=BundleImport), user: User = Depends(require_editor)):
    entry = catalog.get(bundle_id)
    if entry is None:
        raise fail(404, "bundle_not_found", "没有这份演示数据")
    return _start(entry.title, user, entry.load, body.replace)


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = runner.get(job_id)
    if job is None:
        raise fail(404, "job_not_found", "没有这个导入任务（后端重启后任务记录会清空）")
    return job.public()


@router.post("/files")
async def upload_files(files: list[UploadFile] = File(...), title: str | None = Form(default=None), user: User = Depends(require_editor)):
    """Files of any kind, kept in the platform's storage, and what they contain: tables with their columns, links found between them, assets by kind."""
    raw = [(f.filename or "file", await f.read()) for f in files]
    try:
        expanded = uploads.expand(raw)
        upload_id = uuid.uuid4().hex[:12]
        name = (title or "").strip() or (expanded[0][0].rsplit(".", 1)[0] if len(expanded) == 1 else f"导入的 {len(expanded)} 个文件")
        uris = uploads.store(upload_id, expanded)
        bundle = uploads.build(upload_id, name, name, expanded, uris)
        shown = uploads.preview(bundle)
    except uploads.UploadError as exc:
        raise fail(422, "invalid_upload", str(exc)) from exc
    with _UPLOADS_LOCK:
        for old in [k for k, v in _UPLOADS.items() if v["at"] < time.time() - UPLOAD_SECONDS]:
            _UPLOADS.pop(old, None)
        _UPLOADS[upload_id] = {"title": name, "files": [(n, uris[n]) for n, _ in expanded], "at": time.time()}
    return {"upload_id": upload_id, "title": name, "preview": shown}


@router.post("/files/{upload_id}:import", status_code=202)
def import_files(upload_id: str, body: FileImport, user: User = Depends(require_editor)):
    record = _UPLOADS.get(upload_id)
    if record is None:
        raise fail(404, "upload_not_found", "这批文件已经过期，请重新上传")
    storage = get_storage_service()

    def load():
        files = [(name, storage.get_object(uri)) for name, uri in record["files"]]
        try:
            return uploads.build(upload_id, record["title"], body.ontology_name.strip(), files, dict(record["files"]), replay=body.replay)
        except uploads.UploadError as exc:
            raise ValueError(str(exc)) from exc

    return _start(body.ontology_name.strip(), user, load, body.replace)


def _scope(db: Session, ontology_id: str, user: User) -> None:
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise fail(404, "ontology_not_found", "Ontology not found or inaccessible") from None


@router.post("/{ontology_id}/advance")
def advance(ontology_id: str, body: Advance = Body(default_factory=Advance), db: Session = Depends(get_db), user: User = Depends(require_editor)):
    """Load the next events of an ontology whose event table is replayed."""
    _scope(db, ontology_id, user)
    try:
        return runner.advance(db, user, ontology_id, body.count)
    except ValueError as exc:
        raise fail(409, "not_replayable", str(exc)) from exc


@router.get("/{ontology_id}/replay")
def replay_state(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Where a replayed import stands, or {replay: null} for an ontology that was imported all at once."""
    from app.models.v2.ontology_candidates import OntologyCandidateSet

    _scope(db, ontology_id, user)
    for cset in db.query(OntologyCandidateSet).filter(OntologyCandidateSet.ontology_id == ontology_id, OntologyCandidateSet.status == "published").order_by(OntologyCandidateSet.created_at.desc()).all():
        replay = ((cset.generator or {}).get("import") or {}).get("replay")
        if replay:
            return {"replay": {k: replay.get(k) for k in ("table", "order_by", "initial", "batch", "total", "cursor")}}
    return {"replay": None}


@router.get("/{ontology_id}/assets/{asset_id}")
def get_asset(ontology_id: str, asset_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """The bytes of an imported file, for a preview in the page."""
    _scope(db, ontology_id, user)
    row = db.get(EntityInstance, profile.object_id(ontology_id, uploads.ASSET_TYPE, asset_id))
    uri = (row.row_data or {}).get("storage_uri") if row is not None and row.ontology_id == ontology_id else None
    if not uri:
        raise fail(404, "asset_not_found", "没有这个文件")
    data = get_storage_service().get_object(uri)
    name = (row.row_data or {}).get("file_name", "file")
    return Response(content=data, media_type=(row.row_data or {}).get("mime_type") or "application/octet-stream", headers={"Content-Disposition": f"inline; filename*=UTF-8''{json.dumps(name)[1:-1]}"})
