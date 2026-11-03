"""Sensor playback API and the alert-to-review bridge."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_editor
from app.models.user import User
from app.models.v2.sensor_playback import SensorPlayback
from app.services.v2 import sensor_playback as service
from app.services.v2.alert_actions import AlertActionError, propose_alert_action
from app.services.v2.decision_cases import case_payload
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError

router = APIRouter(dependencies=[Depends(get_current_user)])


class MappingRow(BaseModel):
    asset_id: str = Field(min_length=1, max_length=100)
    object_id: str = Field(min_length=1, max_length=200)
    unit: int = Field(ge=1)


class CreatePlayback(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=60)
    rule_id: str = Field(min_length=1, max_length=36)
    mapping: list[MappingRow] = Field(min_length=1, max_length=50)
    start_cycle: int = Field(default=1, ge=1)
    step_cycles: int = Field(default=5, ge=1, le=50)


class Advance(BaseModel):
    steps: int = Field(default=1, ge=1, le=service.MAX_STEPS)
    # Stop at the first step that opens a new alert, so a person can look at it before the replay moves on.
    until_alert: bool = False


def get_graph() -> FalkorDBService:
    return FalkorDBService()


def _access(db: Session, ontology_id: str, user: User):
    try:
        return require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise HTTPException(404, "Ontology not found or inaccessible") from None


def _fail(exc: service.PlaybackError | AlertActionError):
    raise HTTPException(exc.status, {"code": exc.code, "message": exc.message})


def _get(db: Session, ontology_id: str, playback_id: str) -> SensorPlayback:
    playback = db.query(SensorPlayback).filter_by(id=playback_id, ontology_id=ontology_id).first()
    if playback is None:
        raise HTTPException(404, {"code": "playback_not_found", "message": "回放不存在"})
    return playback


@router.get("/{ontology_id}/sensor-playbacks")
def list_playbacks(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _access(db, ontology_id, user)
    rows = db.query(SensorPlayback).filter_by(ontology_id=ontology_id).order_by(SensorPlayback.created_at.desc()).limit(20).all()
    return {"playbacks": [{"id": row.id, "name": row.name, "cursor": row.cursor, "max_cycle": row.max_cycle, "start_cycle": row.start_cycle} for row in rows]}


@router.post("/{ontology_id}/sensor-playbacks", status_code=201)
def create_playback(ontology_id: str, body: CreatePlayback, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _access(db, ontology_id, user)
    try:
        playback = service.create_playback(db, ontology_id, user, name=body.name, dataset_id=body.dataset_id, rule_id=body.rule_id,
                                           mapping=[row.model_dump() for row in body.mapping], start_cycle=body.start_cycle, step_cycles=body.step_cycles)
    except service.PlaybackError as exc:
        db.rollback()
        _fail(exc)
    return service.detail(db, playback)


@router.get("/{ontology_id}/sensor-playbacks/{playback_id}")
def get_playback(ontology_id: str, playback_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _access(db, ontology_id, user)
    return service.detail(db, _get(db, ontology_id, playback_id))


@router.post("/{ontology_id}/sensor-playbacks/{playback_id}:advance")
def advance_playback(ontology_id: str, playback_id: str, body: Advance, db: Session = Depends(get_db), user: User = Depends(require_editor),
                     graph: FalkorDBService = Depends(get_graph)):
    _access(db, ontology_id, user)
    _get(db, ontology_id, playback_id)
    try:
        ticks = service.advance(db, graph, playback_id, ontology_id, user, steps=body.steps, until_alert=body.until_alert)
    except service.PlaybackError as exc:
        db.rollback()
        _fail(exc)
    result = service.detail(db, _get(db, ontology_id, playback_id))
    result["advanced"] = [{"sequence": tick.sequence, "cursor": tick.cursor, "new_alert_ids": tick.new_alert_ids} for tick in ticks]
    return result


@router.post("/{ontology_id}/alerts/{alert_id}:propose", status_code=201)
def propose_alert(ontology_id: str, alert_id: str, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    """Open a reviewed Decision Case for the Action this alert suggests (once per alert)."""
    access = _access(db, ontology_id, user)
    try:
        case, created = propose_alert_action(db, ontology_id, user, alert_id, access)
    except AlertActionError as exc:
        db.rollback()
        _fail(exc)
    return {"created": created, "case": case_payload(case)}
