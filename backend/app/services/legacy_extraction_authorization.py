"""Access checks for the retired direct-write Extraction route."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.ontology import OntologyProject
from app.models.user import User
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError


class LegacyExtractionAccessError(ValueError):
    def __init__(self, message: str, *, status_code: int = 404) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require_owner_or_admin(db: Session, ontology_id: str, user: User) -> OntologyProject:
    project = db.get(OntologyProject, str(ontology_id))
    if project is None:
        raise LegacyExtractionAccessError("本体不存在或无访问权限。")
    if user.role != "admin" and str(project.created_by or "") != str(user.id):
        raise LegacyExtractionAccessError("本体不存在或无访问权限。")
    try:
        require_ontology_access(db, str(project.id), str(user.id), user.role,
                                metadata_revision_id=project.current_revision_id)
    except ObjectQueryError as exc:
        raise LegacyExtractionAccessError("本体不存在或无访问权限。") from exc
    return project


def require_legacy_extraction_actor(db: Session, ontology_id: str, user: User) -> None:
    """Avoid revealing route state to users who cannot administer the ontology."""
    _require_owner_or_admin(db, ontology_id, user)


def require_extraction_task_visibility(db: Session, ontology_id: str, task, user: User) -> None:
    """Only ontology owners/admins can inspect legacy task validation details."""
    project = _require_owner_or_admin(db, ontology_id, user)
    if str(task.ontology_id) != str(project.id):
        raise LegacyExtractionAccessError("任务不存在或无访问权限。")
