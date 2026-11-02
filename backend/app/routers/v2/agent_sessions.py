"""Persistent Agent session API with optional local-only model inference."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_editor
from app.models.user import User
from app.services.v2.agent_sessions import (
    AgentDefinitionService, AgentSessionService, SessionError, TOOL_REGISTRY,
    TOOL_REGISTRY_DIGEST, session_payload,
)
from app.services.v2.decision_cases import CaseError


router = APIRouter()


class SessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    agent_definition_version_id: str | None = Field(default=None, min_length=36, max_length=36)


class AgentDefinitionDraft(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    mode: Literal["deterministic", "local"] = "deterministic"
    tool_names: list[str] = Field(default_factory=lambda: ["read_object", "read_context_ref", "propose_action"], max_length=10)
    model_config_id: str | None = Field(default=None, min_length=1, max_length=200)


class AgentDefinitionUpdate(AgentDefinitionDraft):
    expected_revision: int = Field(ge=1)


class AgentDefinitionPublish(BaseModel):
    expected_revision: int = Field(ge=1)


class ParticipantGrant(BaseModel):
    email: str = Field(min_length=3, max_length=200)


class VersionBody(BaseModel):
    expected_version: int = Field(ge=1)


class ContextAdd(VersionBody):
    kind: Literal["object", "evidence", "scenario", "what_if", "query", "temporal_event"]
    ref_id: str = Field(min_length=1, max_length=200)


class TurnBody(VersionBody):
    task: str = Field(min_length=1, max_length=4000)
    client_request_id: str = Field(min_length=1, max_length=72)
    mode: Literal["deterministic", "local"] = "deterministic"


class ActionProposal(VersionBody):
    action_type_id: str = Field(min_length=1)
    target_object_id: str = Field(min_length=1)
    parameters: dict = Field(default_factory=dict)
    evidence_refs: list[str | dict] = Field(default_factory=list, max_length=30)
    title: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)
    client_request_id: str = Field(min_length=1, max_length=72)


def _service(db: Session, ontology_id: str, user: User) -> AgentSessionService:
    try:
        return AgentSessionService(db, ontology_id, user)
    except SessionError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc


def _call(fn):
    try:
        return fn()
    except (SessionError, CaseError) as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc


@router.get("/{ontology_id}/agent/tools")
def list_agent_tools(ontology_id: str, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    _service(db, ontology_id, user)
    return {"tools": TOOL_REGISTRY, "registry_digest": TOOL_REGISTRY_DIGEST,
            "mode": "deterministic", "paid_model_required": False}


@router.get("/{ontology_id}/agent/definitions")
def list_agent_definitions(ontology_id: str, db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    return {"definitions": _call(lambda: AgentDefinitionService(db, ontology_id, user).list())}


@router.post("/{ontology_id}/agent/definitions", status_code=201)
def create_agent_definition(ontology_id: str, body: AgentDefinitionDraft,
                            db: Session = Depends(get_db), user: User = Depends(require_editor)):
    return _call(lambda: AgentDefinitionService(db, ontology_id, user).create(
        body.name, body.description, body.mode, body.tool_names, body.model_config_id,
    ))


@router.get("/{ontology_id}/agent/definitions/{definition_id}")
def get_agent_definition(ontology_id: str, definition_id: str, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    return _call(lambda: AgentDefinitionService(db, ontology_id, user).get(definition_id))


@router.put("/{ontology_id}/agent/definitions/{definition_id}")
def update_agent_definition(ontology_id: str, definition_id: str, body: AgentDefinitionUpdate,
                            db: Session = Depends(get_db), user: User = Depends(require_editor)):
    data = body.model_dump()
    expected_revision = data.pop("expected_revision")
    return _call(lambda: AgentDefinitionService(db, ontology_id, user).update(
        definition_id, expected_revision, **data,
    ))


@router.post("/{ontology_id}/agent/definitions/{definition_id}:publish")
def publish_agent_definition(ontology_id: str, definition_id: str, body: AgentDefinitionPublish,
                             db: Session = Depends(get_db), user: User = Depends(require_editor)):
    return _call(lambda: AgentDefinitionService(db, ontology_id, user).publish(
        definition_id, body.expected_revision,
    ))


@router.post("/{ontology_id}/agent/definitions/{definition_id}:withdraw")
def withdraw_agent_definition(ontology_id: str, definition_id: str, db: Session = Depends(get_db),
                              user: User = Depends(require_editor)):
    return _call(lambda: AgentDefinitionService(db, ontology_id, user).withdraw(definition_id))


@router.post("/{ontology_id}/agent/definitions/{definition_id}:reactivate")
def reactivate_agent_definition(ontology_id: str, definition_id: str, db: Session = Depends(get_db),
                                user: User = Depends(require_editor)):
    return _call(lambda: AgentDefinitionService(db, ontology_id, user).reactivate(definition_id))


@router.get("/{ontology_id}/agent/definitions/{definition_id}/events")
def agent_definition_events(ontology_id: str, definition_id: str, limit: int = 200,
                            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _call(lambda: AgentDefinitionService(db, ontology_id, user).events(definition_id, limit))


@router.post("/{ontology_id}/agent/sessions", status_code=201)
def create_session(ontology_id: str, body: SessionCreate, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    service = _service(db, ontology_id, user)
    return session_payload(_call(lambda: service.create(
        body.title, body.agent_definition_version_id,
    )), access_role="owner")


@router.get("/{ontology_id}/agent/sessions")
def list_sessions(ontology_id: str, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    service = _service(db, ontology_id, user)
    return {"sessions": _call(service.list)}


@router.get("/{ontology_id}/agent/sessions/{session_id}/participants")
def session_participants(ontology_id: str, session_id: str, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    return {"participants": _call(lambda: _service(db, ontology_id, user).participants(session_id))}


@router.post("/{ontology_id}/agent/sessions/{session_id}/participants")
def grant_session_participant(ontology_id: str, session_id: str, body: ParticipantGrant,
                              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    participants = _call(lambda: _service(db, ontology_id, user).grant_participant(
        session_id, body.email))
    return {"participants": participants}


@router.delete("/{ontology_id}/agent/sessions/{session_id}/participants/{participant_user_id}")
def revoke_session_participant(ontology_id: str, session_id: str, participant_user_id: str,
                               db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    participants = _call(lambda: _service(db, ontology_id, user).revoke_participant(
        session_id, participant_user_id))
    return {"participants": participants}


@router.get("/{ontology_id}/agent/sessions/{session_id}")
def get_session(ontology_id: str, session_id: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    return _call(lambda: _service(db, ontology_id, user).get(session_id))


@router.get("/{ontology_id}/agent/sessions/{session_id}/events")
def session_events(ontology_id: str, session_id: str, limit: int = 100,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"events": _call(lambda: _service(db, ontology_id, user).events(session_id, limit))}


@router.get("/{ontology_id}/agent/sessions/{session_id}/checkpoints")
def session_checkpoints(ontology_id: str, session_id: str, limit: int = 20,
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"checkpoints": _call(lambda: _service(db, ontology_id, user).checkpoints(session_id, limit))}


@router.post("/{ontology_id}/agent/sessions/{session_id}/context")
def add_context(ontology_id: str, session_id: str, body: ContextAdd,
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _call(lambda: _service(db, ontology_id, user).add_context(
        session_id, body.expected_version, body.kind, body.ref_id))


@router.delete("/{ontology_id}/agent/sessions/{session_id}/context/{item_id}")
def remove_context(ontology_id: str, session_id: str, item_id: str, expected_version: int,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _call(lambda: _service(db, ontology_id, user).remove_context(
        session_id, item_id, expected_version))


@router.post("/{ontology_id}/agent/sessions/{session_id}:refresh-context")
def refresh_context(ontology_id: str, session_id: str, body: VersionBody,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _call(lambda: _service(db, ontology_id, user).refresh_context(
        session_id, body.expected_version))


@router.post("/{ontology_id}/agent/sessions/{session_id}:turn", status_code=202)
def run_turn(ontology_id: str, session_id: str, body: TurnBody,
             db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _call(lambda: _service(db, ontology_id, user).turn(
        session_id, body.expected_version, body.task, body.client_request_id, body.mode,
        enqueue=True))


@router.post("/{ontology_id}/agent/sessions/{session_id}:propose-action")
def propose_action(ontology_id: str, session_id: str, body: ActionProposal,
                   db: Session = Depends(get_db), user: User = Depends(require_editor)):
    data = body.model_dump()
    expected_version = data.pop("expected_version")
    return _call(lambda: _service(db, ontology_id, user).propose_action(
        session_id, expected_version, **data))


@router.post("/{ontology_id}/agent/sessions/{session_id}:resume")
def resume_session(ontology_id: str, session_id: str, body: VersionBody,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _call(lambda: _service(db, ontology_id, user).resume(
        session_id, body.expected_version))


@router.post("/{ontology_id}/agent/sessions/{session_id}:pause")
def pause_session(ontology_id: str, session_id: str, body: VersionBody,
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _call(lambda: _service(db, ontology_id, user).set_paused(
        session_id, body.expected_version, True))


@router.post("/{ontology_id}/agent/sessions/{session_id}:unpause")
def unpause_session(ontology_id: str, session_id: str, body: VersionBody,
                    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _call(lambda: _service(db, ontology_id, user).set_paused(
        session_id, body.expected_version, False))


@router.post("/{ontology_id}/agent/sessions/{session_id}:cancel")
def cancel_session(ontology_id: str, session_id: str, body: VersionBody,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _call(lambda: _service(db, ontology_id, user).cancel(
        session_id, body.expected_version))
