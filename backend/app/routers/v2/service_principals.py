"""Admin-managed service identities and exact Pipeline execution grants.

These records name internal execution actors; they are not login credentials.
External API/MCP credentials remain a separate capability and are not minted
by this registry.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_admin
from app.models.user import User
from app.models.v2.dataset import Dataset
from app.models.v2.pipeline import Pipeline
from app.models.v2.service_principal import (
    ServicePrincipal,
    ServicePrincipalAuditEvent,
    ServicePrincipalGrant,
)
from app.services.v2.dataset_authorization import require_dataset_access


router = APIRouter()


class ServicePrincipalCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class PipelineRunGrantCreate(BaseModel):
    pipeline_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=5, max_length=1000)


class ServicePrincipalStatusUpdate(BaseModel):
    status: str
    reason: str = Field(min_length=5, max_length=1000)


class GrantRevoke(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def _principal_or_404(db: Session, principal_id: str, *, lock: bool = False) -> ServicePrincipal:
    query = db.query(ServicePrincipal).filter(ServicePrincipal.id == principal_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if not row:
        raise HTTPException(404, "Service principal not found")
    return row


def _append_event(
    db: Session,
    principal: ServicePrincipal,
    *,
    event_type: str,
    actor_id: str | None,
    payload: dict,
    reason: str | None = None,
    grant_id: str | None = None,
) -> ServicePrincipalAuditEvent:
    prior = db.query(ServicePrincipalAuditEvent.sequence, ServicePrincipalAuditEvent.event_digest).filter(
        ServicePrincipalAuditEvent.service_principal_id == principal.id
    ).order_by(ServicePrincipalAuditEvent.sequence.desc()).first()
    sequence = int(prior[0] if prior else 0) + 1
    previous_digest = prior[1] if prior else None
    envelope = {
        "service_principal_id": principal.id,
        "sequence": sequence,
        "event_type": event_type,
        "actor_user_id": actor_id,
        "grant_id": grant_id,
        "reason": reason,
        "payload": payload,
        "previous_digest": previous_digest,
    }
    row = ServicePrincipalAuditEvent(
        service_principal_id=principal.id,
        sequence=sequence,
        event_type=event_type,
        actor_user_id=actor_id,
        grant_id=grant_id,
        reason=reason,
        payload=payload,
        previous_digest=previous_digest,
        event_digest=_digest(envelope),
        created_at=_now(),
    )
    db.add(row)
    return row


def _grant_payload(grant: ServicePrincipalGrant) -> dict:
    return {
        "id": grant.id,
        "service_principal_id": grant.service_principal_id,
        "resource_type": grant.resource_type,
        "resource_id": grant.resource_id,
        "permission": grant.permission,
        "active": bool(grant.active),
        "created_by": grant.created_by,
        "revoked_by": grant.revoked_by,
        "reason": grant.reason,
        "created_at": grant.created_at.isoformat() if grant.created_at else None,
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
    }


def _principal_payload(db: Session, principal: ServicePrincipal, *, grants: list[ServicePrincipalGrant] | None = None) -> dict:
    if grants is None:
        grants = db.query(ServicePrincipalGrant).filter(
            ServicePrincipalGrant.service_principal_id == principal.id
        ).order_by(ServicePrincipalGrant.created_at.desc()).all()
    return {
        "id": principal.id,
        "kind": principal.kind,
        "name": principal.name,
        "description": principal.description,
        "status": principal.status,
        "builtin": bool(principal.builtin),
        "owner_user_id": principal.owner_user_id,
        "revision": principal.revision,
        "created_at": principal.created_at.isoformat() if principal.created_at else None,
        "updated_at": principal.updated_at.isoformat() if principal.updated_at else None,
        "grants": [_grant_payload(grant) for grant in grants],
    }


def _pipeline_for_grant(db: Session, pipeline_id: str) -> Pipeline:
    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")
    return pipeline


@router.get("")
def list_service_principals(
    pipeline_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Admins see the registry; editors see only principals authorized for one owned Pipeline."""
    if current_user.role not in {"admin", "editor"}:
        raise HTTPException(403, "Editor role required")

    if pipeline_id:
        pipeline = _pipeline_for_grant(db, pipeline_id)
        if current_user.role != "admin" and str(pipeline.created_by or "") != str(current_user.id):
            raise HTTPException(404, "Pipeline not found")
        for dataset_id in _pipeline_dataset_ids(pipeline):
            require_dataset_access(db, dataset_id, current_user)
        active_grants = db.query(ServicePrincipalGrant).filter(
            ServicePrincipalGrant.resource_type == "pipeline",
            ServicePrincipalGrant.resource_id == pipeline_id,
            ServicePrincipalGrant.permission == "run",
            ServicePrincipalGrant.active.is_(True),
        ).all()
        grant_by_principal = {grant.service_principal_id: grant for grant in active_grants}
        principals = db.query(ServicePrincipal).filter(
            ServicePrincipal.id.in_(grant_by_principal.keys()),
            ServicePrincipal.status == "active",
        ).order_by(ServicePrincipal.name.asc()).all() if grant_by_principal else []
        return {
            "items": [
                _principal_payload(db, principal, grants=[grant_by_principal[principal.id]])
                for principal in principals
            ]
        }

    if current_user.role != "admin":
        raise HTTPException(403, "Pipeline scope is required for service principal lookup")
    principals = db.query(ServicePrincipal).order_by(ServicePrincipal.created_at.desc()).all()
    return {"items": [_principal_payload(db, principal) for principal in principals]}


def _pipeline_dataset_ids(pipeline: Pipeline) -> set[str]:
    found: set[str] = {str(pipeline.source_dataset_id)} if pipeline.source_dataset_id else set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"dataset_id", "source_dataset_id", "datasetId", "sourceDatasetId"} and isinstance(child, str) and child:
                    found.add(child)
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(pipeline.definition)
    visit(pipeline.spec)
    return found


@router.post("", status_code=201)
def create_service_principal(
    body: ServicePrincipalCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    principal = ServicePrincipal(
        id=f"sp:{uuid4()}",
        kind="service",
        name=body.name.strip(),
        description=(body.description or "").strip() or None,
        status="active",
        builtin=False,
        owner_user_id=current_user.id,
        created_by=current_user.id,
        revision=1,
        created_at=_now(),
        updated_at=_now(),
    )
    if not principal.name:
        raise HTTPException(422, "名称不能为空")
    db.add(principal)
    db.flush()
    _append_event(
        db, principal, event_type="principal_registered", actor_id=current_user.id,
        payload={"id": principal.id, "kind": principal.kind, "name": principal.name, "status": principal.status},
    )
    db.commit()
    db.refresh(principal)
    return {"principal": _principal_payload(db, principal)}


@router.post("/{principal_id}/pipeline-run-grants", status_code=201)
def grant_pipeline_run(
    principal_id: str,
    body: PipelineRunGrantCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    principal = _principal_or_404(db, principal_id, lock=True)
    if principal.status != "active":
        raise HTTPException(409, "Service principal 已暂停")
    _pipeline_for_grant(db, body.pipeline_id)
    existing = db.query(ServicePrincipalGrant).filter(
        ServicePrincipalGrant.service_principal_id == principal.id,
        ServicePrincipalGrant.resource_type == "pipeline",
        ServicePrincipalGrant.resource_id == body.pipeline_id,
        ServicePrincipalGrant.permission == "run",
        ServicePrincipalGrant.active.is_(True),
    ).first()
    if existing:
        raise HTTPException(409, "该 Pipeline Run 授权已存在")

    grant = ServicePrincipalGrant(
        id=str(uuid4()),
        service_principal_id=principal.id,
        resource_type="pipeline",
        resource_id=body.pipeline_id,
        permission="run",
        active=True,
        created_by=current_user.id,
        reason=body.reason.strip(),
        created_at=_now(),
    )
    db.add(grant)
    db.flush()
    principal.revision += 1
    principal.updated_at = _now()
    _append_event(
        db, principal, event_type="grant_created", actor_id=current_user.id,
        payload={"grant_id": grant.id, "resource_type": grant.resource_type,
                 "resource_id": grant.resource_id, "permission": grant.permission, "active": True},
        reason=grant.reason, grant_id=grant.id,
    )
    db.commit()
    db.refresh(grant)
    return {"grant": _grant_payload(grant), "principal_revision": principal.revision}


@router.post("/{principal_id}/grants/{grant_id}/revoke")
def revoke_service_principal_grant(
    principal_id: str,
    grant_id: str,
    body: GrantRevoke,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    principal = _principal_or_404(db, principal_id, lock=True)
    grant = db.query(ServicePrincipalGrant).filter(
        ServicePrincipalGrant.id == grant_id,
        ServicePrincipalGrant.service_principal_id == principal.id,
    ).with_for_update().first()
    if not grant:
        raise HTTPException(404, "Grant not found")
    if grant.active:
        grant.active = False
        grant.revoked_by = current_user.id
        grant.revoked_at = _now()
        principal.revision += 1
        principal.updated_at = _now()
        _append_event(
            db, principal, event_type="grant_revoked", actor_id=current_user.id,
            payload={"grant_id": grant.id, "resource_type": grant.resource_type,
                     "resource_id": grant.resource_id, "permission": grant.permission, "active": False},
            reason=body.reason.strip(), grant_id=grant.id,
        )
        db.commit()
        db.refresh(grant)
        db.refresh(principal)
    return {"grant": _grant_payload(grant), "principal_revision": principal.revision}


@router.patch("/{principal_id}/status")
def update_service_principal_status(
    principal_id: str,
    body: ServicePrincipalStatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if body.status not in {"active", "suspended"}:
        raise HTTPException(422, "status 只能是 active 或 suspended")
    principal = _principal_or_404(db, principal_id, lock=True)
    if principal.status != body.status:
        previous_status = principal.status
        principal.status = body.status
        principal.revision += 1
        principal.updated_at = _now()
        _append_event(
            db, principal, event_type=f"principal_{body.status}", actor_id=current_user.id,
            payload={"id": principal.id, "previous_status": previous_status,
                     "status": body.status, "revision": principal.revision},
            reason=body.reason.strip(),
        )
        db.commit()
        db.refresh(principal)
    return {"principal": _principal_payload(db, principal)}


@router.get("/{principal_id}/events")
def list_service_principal_events(
    principal_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    _principal_or_404(db, principal_id)
    total = db.query(ServicePrincipalAuditEvent).filter(
        ServicePrincipalAuditEvent.service_principal_id == principal_id
    ).count()
    rows = db.query(ServicePrincipalAuditEvent).filter(
        ServicePrincipalAuditEvent.service_principal_id == principal_id
    ).order_by(ServicePrincipalAuditEvent.sequence.asc()).limit(1000).all()

    items = []
    previous_digest = None
    verified_count = 0
    chain_status = "verified"
    valid_prefix = True
    for expected_sequence, row in enumerate(rows, start=1):
        envelope = {
            "service_principal_id": principal_id,
            "sequence": row.sequence,
            "event_type": row.event_type,
            "actor_user_id": row.actor_user_id,
            "grant_id": row.grant_id,
            "reason": row.reason,
            "payload": row.payload,
            "previous_digest": row.previous_digest,
        }
        event_verified = valid_prefix and (
            row.sequence == expected_sequence
            and row.previous_digest == previous_digest
            and row.event_digest == _digest(envelope)
        )
        if not event_verified:
            chain_status = "invalid"
            valid_prefix = False
        else:
            verified_count += 1
        items.append({
            "id": row.id,
            "sequence": row.sequence,
            "event_type": row.event_type,
            "actor_user_id": row.actor_user_id,
            "grant_id": row.grant_id,
            "reason": row.reason,
            "payload": row.payload,
            "previous_digest": row.previous_digest,
            "event_digest": row.event_digest,
            "chain_verified": event_verified,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
        previous_digest = row.event_digest

    if chain_status != "invalid" and total > len(rows):
        chain_status = "truncated"
    elif chain_status != "invalid" and total == 0:
        chain_status = "empty"

    return {
        "items": items,
        "chain_status": chain_status,
        "verified_event_count": verified_count,
        "total_event_count": total,
    }
