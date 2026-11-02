"""Ontology revision history, comparison and non-destructive restore."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_user, require_editor
from app.models.user import User
from app.models.ontology import OntologyProject
from app.models.ontology_revision import OntologyRevision
from app.models.v2.dynamic_ontology import OntologyChange
from app.services.v2.object_query.normalize import stable_hash
from app.services.v2.revision_service import (
    compare_revisions,
    create_revision,
    materialize_snapshot,
    serialize_revision,
    snapshot_digest,
    snapshot_ontology,
    snapshot_restore_blockers,
)
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError

router = APIRouter(dependencies=[Depends(get_current_user)])


class RestoreConfirmation(BaseModel):
    expected_current_revision_id: str | None = Field(...)
    preview_digest: str = Field(min_length=64, max_length=64)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_scope(db: Session, ontology_id: str, user: User) -> None:
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise HTTPException(404, "Ontology not found or inaccessible") from None


@router.get("/{ontology_id}/revisions")
def list_revisions(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_scope(db, ontology_id, user)
    if not db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first():
        raise HTTPException(404, "Ontology not found")
    rows = db.query(OntologyRevision).filter(OntologyRevision.ontology_id == ontology_id).order_by(OntologyRevision.revision_no.desc()).all()
    return {"ontology_id": ontology_id, "current_revision_id": getattr(db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first(), "current_revision_id", None), "revisions": [serialize_revision(row) for row in rows], "count": len(rows)}


@router.get("/{ontology_id}/revisions/compare")
def compare(ontology_id: str, left: str = Query(...), right: str = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_scope(db, ontology_id, user)
    rows = db.query(OntologyRevision).filter(OntologyRevision.ontology_id == ontology_id, OntologyRevision.id.in_([left, right])).all()
    by_id = {row.id: row for row in rows}
    if left not in by_id or right not in by_id:
        raise HTTPException(404, "版本不存在")
    return compare_revisions(by_id[left], by_id[right])


def _current_revision(db: Session, project: OntologyProject) -> OntologyRevision | None:
    if project.current_revision_id:
        current = db.query(OntologyRevision).filter_by(
            id=project.current_revision_id, ontology_id=project.id,
        ).populate_existing().first()
        if current is None:
            raise HTTPException(409, "本体当前修订指针无效，请先核对版本记录")
        if not current.is_current:
            raise HTTPException(409, "本体当前修订指针与版本状态不一致，请先核对版本记录")
        return current
    return db.query(OntologyRevision).filter_by(
        ontology_id=project.id, is_current=True,
    ).order_by(OntologyRevision.revision_no.desc()).first()


def _restore_preview(db: Session, project: OntologyProject, target: OntologyRevision) -> dict:
    target_snapshot = target.snapshot_json or {}
    target_digest = snapshot_digest(target_snapshot)
    if target.snapshot_hash and target.snapshot_hash != target_digest:
        raise HTTPException(409, "目标修订快照哈希不匹配，拒绝恢复")
    current = _current_revision(db, project)
    current_snapshot = snapshot_ontology(db, project.id)
    current_digest = snapshot_digest(current_snapshot)
    blockers = snapshot_restore_blockers(db, project.id, target_snapshot)
    diff_summary = {}
    for kind, key in (("entities", "id"), ("relations", "id"), ("logic_rules", "id"), ("actions", "id")):
        before = {str(item.get(key)): item for item in current_snapshot.get(kind, [])}
        after = {str(item.get(key)): item for item in target_snapshot.get(kind, [])}
        diff_summary[kind] = {
            "added": len(set(after) - set(before)),
            "removed": len(set(before) - set(after)),
            "changed": sum(1 for item in set(before) & set(after) if before[item] != after[item]),
        }
    expected_current_revision_id = current.id if current else None
    preview_digest = stable_hash({
        "ontology_id": project.id,
        "target_revision_id": target.id,
        "target_snapshot_digest": target_digest,
        "expected_current_revision_id": expected_current_revision_id,
        "current_live_snapshot_digest": current_digest,
    })
    return {
        "target_revision": serialize_revision(target),
        "expected_current_revision_id": expected_current_revision_id,
        "preview_digest": preview_digest,
        "can_restore": target.id != expected_current_revision_id and not blockers,
        "blockers": blockers,
        "diff": diff_summary,
        "preserves_instance_rows": True,
    }


@router.post("/{ontology_id}/revisions/{revision_id}:preview-restore")
def preview_restore(ontology_id: str, revision_id: str, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _require_scope(db, ontology_id, user)
    project = db.query(OntologyProject).filter_by(id=ontology_id).first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    target = db.query(OntologyRevision).filter_by(id=revision_id, ontology_id=ontology_id).first()
    if target is None:
        raise HTTPException(404, "版本不存在")
    try:
        return _restore_preview(db, project, target)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{ontology_id}/revisions/{revision_id}/restore")
def restore(ontology_id: str, revision_id: str, body: RestoreConfirmation,
            db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _require_scope(db, ontology_id, user)
    try:
        project = db.query(OntologyProject).filter_by(id=ontology_id).populate_existing().with_for_update().first()
        if project is None:
            raise HTTPException(404, "Ontology not found")
        target = db.query(OntologyRevision).filter_by(
            id=revision_id, ontology_id=ontology_id,
        ).populate_existing().first()
        if target is None:
            raise HTTPException(404, "版本不存在")
        current = _current_revision(db, project)
        current_id = current.id if current else None
        if body.expected_current_revision_id != current_id:
            raise HTTPException(409, "当前修订已变化，请重新预览恢复")
        preview = _restore_preview(db, project, target)
        if body.preview_digest != preview["preview_digest"]:
            raise HTTPException(409, "恢复预览已过期，请重新预览")
        if not preview["can_restore"]:
            raise HTTPException(409, {"message": "该修订当前不可恢复", "blockers": preview["blockers"]})

        before = snapshot_ontology(db, ontology_id)
        materialize_snapshot(db, ontology_id, target.snapshot_json or {})
        restored_snapshot = snapshot_ontology(db, ontology_id)
        revision = create_revision(
            db, ontology_id, snapshot=restored_snapshot, parent_revision_id=current.id if current else target.id,
            summary={**(target.summary or {}), "restored_from": target.id}, commit=False,
        )
        change = OntologyChange(
            id=str(__import__("uuid").uuid4()), ontology_id=ontology_id,
            base_revision_id=current.id if current else None, result_revision_id=revision.id,
            target_kind="ontology", operation="restore", target_id=target.id,
            before_json=before, after_json=restored_snapshot,
            impact_json={"restored_from": target.id}, validation_json={"ok": True}, status="applied",
        )
        db.add(change)
        db.commit(); db.refresh(revision)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc))
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    # Restoring a revision changes the published schema, so it receives the
    # same asynchronous local audit as a normal editor save.  The audit is
    # queued after the revision transaction has committed and cannot roll the
    # restoration back if Ollama is unavailable.
    try:
        from app.services.v2.audit_runner import queue_local_audit
        queue_local_audit(db, ontology_id=ontology_id, revision_id=revision.id, construction_run_id=None)
    except Exception:
        pass
    return serialize_revision(revision)
