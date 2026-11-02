"""User-configurable Scenario workspace over existing Ontology data."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.deps import get_current_user, get_db
from app.models.ontology import OntologyProject
from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.v2.action import OntologyActionRun, OntologyActionType
from app.models.v2.query_view import QueryDataView
from app.models.v2.scenario import ScenarioAudit, ScenarioChangeSet, ScenarioResource, ScenarioRetentionEvent, ScenarioRevision
from app.schemas.v2.scenario import Edit, RevisionRequest, CreateScenarioRequest
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.object_query.core import FalkorReadAdapter
from app.services.v2.object_query.data_views import build_live_view
from app.services.v2.object_query.metadata import load_sql_metadata
from app.services.v2.object_query.normalize import stable_hash
from app.services.v2.action_authorization import authorize_action_snapshot, authorize_compiled_edits
from app.services.v2.action_runtime import (
    ActionRequest,
    complete_action_run,
    compile_action_definitions,
    create_action_run_record,
    acquire_initial_scenario_action_run_claim,
    fail_action_run,
    fail_scenario_action_run_claim,
    claim_idempotent_scenario_action_run,
    renew_scenario_action_run_claim,
    scenario_action_run_claim_manifest,
    transition_action_run,
)
from app.services.v2.scenario_actions import validate_definition
from app.services.v2.scenarios import ScenarioError, ScenarioService, error, _summary, scenario_expires_at
from app.services.v2.live_edits import reconcile_live_edits

router = APIRouter()


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FromLive(StrictBody):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    ttl_seconds: int = Field(default=30 * 86400, ge=3600, le=365 * 86400)
    mode: Literal['tracking', 'pinned'] = 'tracking'


class ScenarioSettings(StrictBody):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    expected_etag: int = Field(ge=1)
    ttl_seconds: int = Field(ge=3600, le=365 * 86400)


class ActionDefinition(StrictBody):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    parameters: list[dict] = Field(default_factory=list)
    rules: list[dict] = Field(min_length=1, max_length=30)


class ActionInvocation(StrictBody):
    action_type_id: str
    parameters: dict = Field(default_factory=dict)


class ActionList(StrictBody):
    expected_etag: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=200)
    preview_digest: str | None = Field(default=None, min_length=64, max_length=64)
    actions: list[ActionInvocation] = Field(max_length=30)


class MergeCommand(StrictBody):
    expected_revision: int = Field(ge=1)
    preview_digest: str = Field(min_length=64, max_length=64)
    client_request_id: str = Field(min_length=1, max_length=200)


class MergeReconcileCommand(StrictBody):
    merge_request_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)
    client_request_id: str = Field(min_length=1, max_length=200)


def _service(ontology_id, db, user):
    service = ScenarioService(db, ontology_id, user)
    service.authorize_ontology()
    return service


def _http(exc):
    raise HTTPException(exc.status, error(exc)) from exc


def _action_dict(row):
    return {"id": row.id, "name": row.name, "description": row.description or "",
            "parameters": row.parameters or [], "rules": row.effects or [], "version": row.version,
            "status": row.status, "enabled": row.enabled}


def _snapshot(service, view_id):
    view = service.db.get(QueryDataView, view_id)
    if not view or view.ontology_id != service.ontology_id or view.status != "ready":
        raise ScenarioError("view_unavailable", "Scenario data view is unavailable", 409)
    if not service.graph.available:
        raise ScenarioError("graph_unavailable", "FalkorDB is unavailable", 503)
    return FalkorReadAdapter(service.graph._graph(view.graph_key), graph_ontology_id=view.graph_key).read(service.ontology_id)


@router.post("/{ontology_id}/scenarios:from-live", status_code=201)
def create_from_live(ontology_id: str, body: FromLive, db=Depends(get_db), user=Depends(get_current_user)):
    try:
        service = _service(ontology_id, db, user)
        if not service.graph.available:
            raise ScenarioError("graph_unavailable", "FalkorDB is unavailable", 503)
        metadata = load_sql_metadata(db, ontology_id)
        capture_id = stable_hash({"ontology": ontology_id, "time": datetime.now(timezone.utc).isoformat(),
                                  "nonce": uuid4().hex})
        view = build_live_view(db, service.graph, metadata, ontology_id, user.id, capture_id, body.ttl_seconds)
        scenario = service.create(CreateScenarioRequest(name=body.name.strip(), description=body.description,
                                                        base_view_id=view.id, ttl_seconds=body.ttl_seconds, mode=body.mode))
        return _summary(scenario)
    except ScenarioError as exc:
        _http(exc)


class RebaseCommand(StrictBody):
    expected_etag: int = Field(ge=1)


class RebaseConflictChoice(StrictBody):
    conflict_id: str = Field(min_length=32, max_length=32)
    choice: Literal["keep_scenario", "keep_live"]


class RebaseConflictResolution(StrictBody):
    expected_etag: int = Field(ge=1)
    conflict_digest: str = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=1, max_length=4000)
    client_request_id: str = Field(min_length=1, max_length=200)
    resolutions: list[RebaseConflictChoice] = Field(min_length=1, max_length=2000)


@router.post('/{ontology_id}/scenarios/{scenario_id}:rebase')
def rebase_scenario(ontology_id: str, scenario_id: str, body: RebaseCommand, db=Depends(get_db), user=Depends(get_current_user)):
    from app.services.v2.scenario_tracking import rebase
    try:
        return rebase(_service(ontology_id, db, user), scenario_id, body.expected_etag)
    except ScenarioError as exc:
        db.rollback()
        _http(exc)


@router.get('/{ontology_id}/scenarios/{scenario_id}/rebase-conflicts')
def get_rebase_conflicts(ontology_id: str, scenario_id: str,
                         db=Depends(get_db), user=Depends(get_current_user)):
    from app.services.v2.scenario_tracking import get_rebase_conflicts as read_conflicts
    try:
        return read_conflicts(_service(ontology_id, db, user), scenario_id)
    except ScenarioError as exc:
        db.rollback()
        _http(exc)


@router.post('/{ontology_id}/scenarios/{scenario_id}:rebase-resolve')
def resolve_rebase_conflicts(ontology_id: str, scenario_id: str,
                             body: RebaseConflictResolution,
                             db=Depends(get_db), user=Depends(get_current_user)):
    from app.services.v2.scenario_tracking import resolve_rebase_conflicts as resolve
    try:
        return resolve(_service(ontology_id, db, user), scenario_id, body)
    except ScenarioError as exc:
        db.rollback()
        _http(exc)


@router.put("/{ontology_id}/scenarios/{scenario_id}/settings")
def update_scenario_settings(ontology_id: str, scenario_id: str, body: ScenarioSettings,
                             db=Depends(get_db), user=Depends(get_current_user)):
    try:
        service = _service(ontology_id, db, user)
        scenario = db.query(ScenarioResource).filter_by(
            id=scenario_id, ontology_id=ontology_id,
        ).populate_existing().with_for_update().first()
        if not scenario:
            raise ScenarioError("not_found", "Scenario not found", 404)
        scenario = service.get(scenario_id, write=True)
        if scenario.status != "active" or scenario.protected_demo:
            raise ScenarioError("scenario_read_only", "Scenario settings are read only", 409)
        if scenario.etag != body.expected_etag:
            raise ScenarioError("etag_conflict", "Scenario changed; reload settings", 409)
        scenario.name = body.name.strip()
        scenario.description = body.description
        scenario.ttl_seconds = body.ttl_seconds
        retention = datetime.now(timezone.utc) + timedelta(seconds=body.ttl_seconds)
        scenario.expires_at = retention
        view_ids = [scenario.base_view_id] + [item.result_view_id for item in
            db.query(ScenarioRevision).filter_by(scenario_id=scenario.id).all()]
        for view_id in view_ids:
            view = db.get(QueryDataView, view_id)
            if view and view.ontology_id == ontology_id:
                view.retention_until = retention
        scenario.etag += 1
        scenario.updated_at = datetime.now(timezone.utc)
        db.add(ScenarioAudit(scenario_id=scenario.id, actor_id=user.id, operation="settings",
                             summary={"name": scenario.name, "ttl_seconds": scenario.ttl_seconds}))
        db.commit()
        return _summary(scenario)
    except ScenarioError as exc:
        _http(exc)


@router.get("/{ontology_id}/scenarios/{scenario_id}/retention")
def get_scenario_retention(ontology_id: str, scenario_id: str,
                           db=Depends(get_db), user=Depends(get_current_user)):
    try:
        service = _service(ontology_id, db, user)
        scenario = service.get(scenario_id, allow_expired=True)
        events = db.query(ScenarioRetentionEvent).filter_by(
            scenario_id=scenario.id,
        ).order_by(ScenarioRetentionEvent.created_at.asc()).all()
        return {
            "scenario_id": scenario.id,
            "expires_at": scenario_expires_at(scenario).isoformat() if scenario_expires_at(scenario) else None,
            "expired_at": scenario.expired_at.isoformat() if scenario.expired_at else None,
            "events": [{"event_type": item.event_type,
                        "expires_at": item.expires_at.isoformat(),
                        "created_at": item.created_at.isoformat(),
                        "details": item.details}
                       for item in events],
        }
    except ScenarioError as exc:
        _http(exc)


@router.get("/{ontology_id}/scenario-action-types")
def list_action_types(ontology_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    try:
        _service(ontology_id, db, user)
        rows = db.query(OntologyActionType).filter_by(ontology_id=ontology_id,
                  action_category="scenario").order_by(OntologyActionType.created_at.desc()).all()
        return {"action_types": [_action_dict(row) for row in rows]}
    except ScenarioError as exc:
        _http(exc)


@router.post("/{ontology_id}/scenario-action-types", status_code=201)
def create_action_type(ontology_id: str, body: ActionDefinition, db=Depends(get_db), user=Depends(get_current_user)):
    try:
        _service(ontology_id, db, user)
        validate_definition(body.parameters, body.rules)
        row = OntologyActionType(ontology_id=ontology_id, name=body.name.strip(),
                                 description=body.description, action_category="scenario",
                                 parameters=body.parameters, effects=body.rules, created_by=user.id,
                                 status="draft", version=1)
        db.add(row); db.commit(); db.refresh(row)
        return _action_dict(row)
    except ScenarioError as exc:
        _http(exc)


@router.put("/{ontology_id}/scenario-action-types/{action_type_id}")
def update_action_type(ontology_id: str, action_type_id: str, body: ActionDefinition,
                       db=Depends(get_db), user=Depends(get_current_user)):
    try:
        _service(ontology_id, db, user)
        row = db.query(OntologyActionType).filter_by(id=action_type_id, ontology_id=ontology_id,
                   action_category="scenario").first()
        if not row:
            raise ScenarioError("action_type_not_found", "Action type not found", 404)
        if user.role != "admin" and row.created_by != user.id:
            raise ScenarioError("forbidden", "Only the Action type author may edit it", 403)
        validate_definition(body.parameters, body.rules)
        row.name, row.description = body.name.strip(), body.description
        row.parameters, row.effects = body.parameters, body.rules
        row.version += 1
        row.status = "draft"
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return _action_dict(row)
    except ScenarioError as exc:
        _http(exc)


@router.post("/{ontology_id}/scenario-action-types/{action_type_id}:publish")
def publish_action_type(ontology_id: str, action_type_id: str,
                        db=Depends(get_db), user=Depends(get_current_user)):
    try:
        _service(ontology_id, db, user)
        row = db.query(OntologyActionType).filter_by(id=action_type_id, ontology_id=ontology_id,
                   action_category="scenario").first()
        if not row:
            raise ScenarioError("action_type_not_found", "Action type not found", 404)
        if user.role != "admin" and row.created_by != user.id:
            raise ScenarioError("forbidden", "Only the Action type author may publish it", 403)
        validate_definition(row.parameters, row.effects)
        row.status = "published"
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return _action_dict(row)
    except ScenarioError as exc:
        _http(exc)


def _compiled(service, scenario, body):
    if scenario.status != "active":
        raise ScenarioError("scenario_archived", "Archived scenarios cannot be edited", 409)
    if scenario.etag != body.expected_etag:
        raise ScenarioError("etag_conflict", "Scenario changed; reload before submitting", 409)
    base = _snapshot(service, scenario.base_view_id)
    actions = [item.model_dump() for item in body.actions]
    ids = {item["action_type_id"] for item in actions}
    types = {row.id: row for row in service.db.query(OntologyActionType).filter(
        OntologyActionType.ontology_id == service.ontology_id,
        OntologyActionType.action_category == "scenario",
        OntologyActionType.id.in_(ids)).all()} if ids else {}
    request = ActionRequest.from_invocations(
        ontology_id=service.ontology_id,
        compiler_scope=scenario.id,
        source="scenario_workbench",
        invocations=actions,
        action_type_versions={action_id: row.version for action_id, row in types.items()},
        execution_context={
            "scenario_id": scenario.id,
            "scenario_revision": scenario.head_revision,
            "base_view_id": scenario.base_view_id,
        },
        expected_etag=body.expected_etag,
        client_request_id=body.client_request_id,
        preview_digest=body.preview_digest,
    )
    edits, permission_digest = compile_action_definitions(
        service.db, request, service.user, types, base)
    if not edits:
        edits = [Edit(sequence=0, op="invoke_action", action_key="scenario_reset",
                      parameters={}, source_action="scenario_reset")]
    return edits, permission_digest, request, base


def _preview_digest(scenario, body, edits, permission_digest, request):
    return stable_hash({
        "scenario_id": scenario.id,
        "action_request": request.manifest(),
        "compiled_edits": [edit.model_dump(mode="json") for edit in edits],
        "permission_digest": permission_digest,
    })


@router.post("/{ontology_id}/scenarios/{scenario_id}/actions:preview")
def preview_actions(ontology_id: str, scenario_id: str, body: ActionList,
                    db=Depends(get_db), user=Depends(get_current_user)):
    try:
        service = _service(ontology_id, db, user)
        scenario = service.get(scenario_id, write=True)
        edits, permission_digest, action_request, base = _compiled(service, scenario, body)
        preview_digest = _preview_digest(scenario, body, edits, permission_digest, action_request)
        authorized_snapshot, _ = authorize_action_snapshot(
            db, ontology_id, user, base,
        )
        return {"valid": True, "edits": [edit.model_dump(mode="json") for edit in edits],
                "changes": service._revision_preview_changes(authorized_snapshot, edits),
                "edit_count": sum(edit.op != "invoke_action" for edit in edits),
                "action_count": len(body.actions),
                "permission_digest": permission_digest,
                "client_request_id": body.client_request_id,
                "preview_digest": preview_digest}
    except ScenarioError as exc:
        _http(exc)


@router.put("/{ontology_id}/scenarios/{scenario_id}/actions")
def submit_actions(ontology_id: str, scenario_id: str, body: ActionList,
                   db=Depends(get_db), user=Depends(get_current_user)):
    runtime_action_run = None
    execution_claim_token = None
    try:
        service = _service(ontology_id, db, user)
        scenario = service.get(scenario_id, write=True)
        input_hash = stable_hash([item.model_dump(mode="json") for item in body.actions])
        request_digest = stable_hash({
            "actions": [item.model_dump(mode="json") for item in body.actions],
            "expected_etag": body.expected_etag,
            "preview_digest": body.preview_digest,
        })
        action_scope = f"scenario:{scenario.id}"

        def completed_run_response(run, *, replayed):
            snapshot = run.after_snapshot or {}
            revision_number = snapshot.get("scenario_revision")
            revision_row = db.query(ScenarioRevision).filter_by(
                scenario_id=scenario.id, revision=revision_number, status="ready",
            ).first() if revision_number is not None else None
            if revision_row is None:
                raise ScenarioError(
                    "action_request_incomplete",
                    "ActionRun 已完成但关联的 Scenario 修订记录不可读取。", 409)
            return {
                "scenario_id": scenario.id,
                "revision": revision_row.revision,
                "result_view_id": revision_row.result_view_id,
                "changeset_id": revision_row.changeset_id,
                "action_count": len(body.actions),
                "preview_digest": run.preview_digest,
                "permission_digest": run.permission_digest,
                "action_run_id": run.id,
                "action_run_status": run.status,
                "replayed": replayed,
            }

        existing_run, reclaimed_token, replayed = claim_idempotent_scenario_action_run(
            db,
            ontology_id=ontology_id,
            actor_id=str(user.id),
            scope=action_scope,
            request_id=body.client_request_id,
            request_digest=request_digest,
            scenario_id=scenario.id,
        )
        if existing_run is not None:
            if replayed:
                return completed_run_response(existing_run, replayed=True)
            runtime_action_run = existing_run
            execution_claim_token = reclaimed_token

        prior = db.query(ScenarioChangeSet).filter_by(scenario_id=scenario.id,
                    client_request_id=body.client_request_id).first()
        if prior:
            report = prior.validation_report or {}
            manifest = report.get("runtime_manifest") or {}
            same_legacy_request = (
                report.get("action_input_hash") == input_hash
                and report.get("preview_digest") == body.preview_digest
            )
            if manifest:
                same_request = manifest.get("action_request_digest") == request_digest
            else:
                same_request = same_legacy_request
            if not same_request:
                raise ScenarioError("idempotency_conflict", "Request ID was used for different Actions", 409)
            revision_id = report.get("revision_id")
            prior_revision = db.get(ScenarioRevision, revision_id) if revision_id else None
            if prior_revision is None:
                raise ScenarioError("request_incomplete", "已记录的请求缺少可重放的情景修订", 409)
            return {"scenario_id": scenario.id, "revision": prior_revision.revision,
                    "result_view_id": prior_revision.result_view_id,
                    "changeset_id": prior_revision.changeset_id,
                    "action_count": len(body.actions),
                    "preview_digest": manifest.get("preview_digest") or report.get("preview_digest"),
                    "permission_digest": manifest.get("permission_digest") or report.get("permission_digest")}
        edits, permission_digest, action_request, _ = _compiled(service, scenario, body)
        preview_digest = _preview_digest(scenario, body, edits, permission_digest, action_request)
        if not body.preview_digest or body.preview_digest != preview_digest:
            raise ScenarioError("preview_conflict", "Action、Scenario 或权限已变化；请重新预览后提交", 409)

        # Lock the Scenario before the final idempotency check or creating a
        # new ActionRun, so a concurrent retry observes the same durable run.
        locked_scenario = db.query(ScenarioResource).filter_by(
            id=scenario.id, ontology_id=ontology_id,
        ).populate_existing().with_for_update().first()
        if locked_scenario is None:
            raise ScenarioError("not_found", "Scenario not found", 404)
        scenario = locked_scenario
        service.get(scenario.id, write=True)

        if runtime_action_run is None:
            existing_run, reclaimed_token, replayed = claim_idempotent_scenario_action_run(
                db,
                ontology_id=ontology_id,
                actor_id=str(user.id),
                scope=action_scope,
                request_id=body.client_request_id,
                request_digest=request_digest,
                scenario_id=scenario.id,
            )
            if existing_run is not None:
                if replayed:
                    return completed_run_response(existing_run, replayed=True)
                runtime_action_run = existing_run
                execution_claim_token = reclaimed_token
                # Reclaiming commits the new lease and releases the Scenario
                # lock. Reacquire it before validating the preview or writing.
                locked_scenario = db.query(ScenarioResource).filter_by(
                    id=scenario.id, ontology_id=ontology_id,
                ).populate_existing().with_for_update().first()
                if locked_scenario is None:
                    raise ScenarioError("not_found", "Scenario not found", 404)
                scenario = locked_scenario
                service.get(scenario.id, write=True)

        # Compilation happened before acquiring the row lock; refuse to commit
        # if the Scenario changed while it was compiling.
        current_preview_digest = _preview_digest(
            scenario, body, edits, permission_digest, action_request)
        if body.preview_digest != current_preview_digest:
            raise ScenarioError(
                "preview_conflict", "Scenario 已变化；请重新预览后提交", 409)
        preview_digest = current_preview_digest

        # Recheck the legacy ChangeSet ledger after taking the lock. Older
        # deployments may have committed the revision before an ActionRun
        # record was introduced.
        prior = db.query(ScenarioChangeSet).filter_by(
            scenario_id=scenario.id,
            client_request_id=body.client_request_id,
        ).first()
        if prior:
            report = prior.validation_report or {}
            manifest = report.get("runtime_manifest") or {}
            same_request = (
                manifest.get("action_request_digest") == request_digest
                if manifest else (
                    report.get("action_input_hash") == input_hash
                    and report.get("preview_digest") == body.preview_digest
                )
            )
            if not same_request:
                raise ScenarioError(
                    "idempotency_conflict",
                    "Request ID was used for different Actions", 409)
            revision_id = report.get("revision_id")
            prior_revision = db.get(ScenarioRevision, revision_id) if revision_id else None
            if prior_revision is None:
                raise ScenarioError(
                    "request_incomplete",
                    "已记录的请求缺少可重放的情景修订", 409)
            return {
                "scenario_id": scenario.id,
                "revision": prior_revision.revision,
                "result_view_id": prior_revision.result_view_id,
                "changeset_id": prior_revision.changeset_id,
                "action_count": len(body.actions),
                "preview_digest": manifest.get("preview_digest") or report.get("preview_digest"),
                "permission_digest": manifest.get("permission_digest") or report.get("permission_digest"),
            }

        request = RevisionRequest(base_revision=scenario.head_revision,
                                  expected_etag=scenario.etag, client_request_id=body.client_request_id,
                                  permission_digest=permission_digest, edits=edits)
        runtime_manifest = {
            "action_input_hash": input_hash,
            "action_request_digest": request_digest,
            "expected_etag": body.expected_etag,
            "permission_digest": permission_digest,
            "preview_digest": preview_digest,
            "action_count": len(body.actions),
            "typed_action_request": action_request.manifest(),
        }
        action_types = action_request.action_type_versions
        if runtime_action_run is None:
            runtime_action_run = create_action_run_record(
                db=db,
                action_type_id=(next(iter(action_request.invocations)).action_type_id
                                if len(action_request.invocations) == 1 else None),
                ontology_id=ontology_id,
                parameters={"invocations": action_request.compiler_payloads()},
                execution_context={
                    "scenario_id": scenario.id,
                    "source": "scenario_workbench",
                    "typed_action_request": action_request.manifest(),
                },
                initial_status="running",
                requester_id=str(user.id),
                executor_id=str(user.id),
                client_request_id=body.client_request_id,
                idempotency_scope=action_scope,
                input_digest=request_digest,
                preview_digest=preview_digest,
                permission_digest=permission_digest,
                expected_etag=str(body.expected_etag),
                action_version=(next(iter(action_types.values()))
                                if len(action_types) == 1 else None),
            )
            db.add(runtime_action_run)
            db.flush()
            execution_claim_token = acquire_initial_scenario_action_run_claim(
                db,
                runtime_action_run,
                actor_id=str(user.id),
            )

        if not execution_claim_token:
            raise ScenarioError(
                "action_claim_missing", "Scenario Action 缺少有效执行租约", 409,
            )
        runtime_action_run = renew_scenario_action_run_claim(
            db,
            run_id=runtime_action_run.id,
            claim_token=execution_claim_token,
            actor_id=str(user.id),
        )
        db.commit()
        db.refresh(runtime_action_run)
        runtime_manifest.update(scenario_action_run_claim_manifest(
            runtime_action_run,
            execution_claim_token,
            request_digest,
        ))
        revision = service.revision(scenario, request, replace_all=True,
                                    runtime_manifest=runtime_manifest,
                                    runtime_action_run=runtime_action_run)
        return {"scenario_id": scenario.id, "revision": revision.revision,
                "result_view_id": revision.result_view_id, "changeset_id": revision.changeset_id,
                "action_count": len(body.actions), "preview_digest": preview_digest,
                "permission_digest": permission_digest,
                "action_run_id": runtime_action_run.id,
                "action_run_status": runtime_action_run.status,
                "replayed": False}
    except ScenarioError as exc:
        db.rollback()
        if runtime_action_run is not None and execution_claim_token:
            try:
                fail_scenario_action_run_claim(
                    db,
                    run_id=runtime_action_run.id,
                    claim_token=execution_claim_token,
                    error=exc.code,
                    actor_id=str(user.id),
                )
            except Exception:
                db.rollback()
        _http(exc)
    except Exception as exc:
        db.rollback()
        if runtime_action_run is not None and execution_claim_token:
            try:
                fail_scenario_action_run_claim(
                    db,
                    run_id=runtime_action_run.id,
                    claim_token=execution_claim_token,
                    error="action_submission_failed",
                    actor_id=str(user.id),
                )
            except Exception:
                db.rollback()
        raise HTTPException(422, {"code": "action_submission_failed", "message": str(exc)}) from exc


@router.get("/{ontology_id}/scenarios/{scenario_id}/workspace")
def get_workspace(ontology_id: str, scenario_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    try:
        service = _service(ontology_id, db, user)
        scenario = service.get(scenario_id)
        base_full = _snapshot(service, scenario.base_view_id)
        base, current_permission_digest = authorize_action_snapshot(db, ontology_id, user, base_full)
        revision = db.query(ScenarioRevision).filter_by(scenario_id=scenario.id,
                    revision=scenario.head_revision).first() if scenario.head_revision else None
        candidate_full = _snapshot(service, revision.result_view_id) if revision else base_full
        candidate, _ = authorize_action_snapshot(db, ontology_id, user, candidate_full)
        changeset = db.get(ScenarioChangeSet, revision.changeset_id) if revision else None
        def objects(snapshot):
            return [{"object_type": kind, "object_id": oid, "properties": props}
                    for (kind, oid), props in sorted(snapshot.objects.items())]
        base_keys, candidate_keys = set(base.objects), set(candidate.objects)
        modified = [key for key in base_keys & candidate_keys
                    if base.objects[key] != candidate.objects[key]]
        actions = []
        change_authorization = (changeset.validation_report.get("authorization") or {}) if changeset else {}
        is_policy_subject = str(change_authorization.get("policy_subject_id") or "") == str(user.id)
        may_inspect_inputs = bool(changeset and (
            user.role == "admin" or ((changeset.actor_id == user.id or is_policy_subject) and
            changeset.validation_report.get("permission_digest") == current_permission_digest)))
        for edit in changeset.ordered_edits if may_inspect_inputs else []:
            if edit.get("op") == "invoke_action" and edit.get("action_key") != "scenario_reset":
                actions.append({"action_type_id": edit["action_key"],
                                "parameters": edit.get("parameters", {}).get("values", {})})
        return {"scenario": _summary(scenario), "revision": revision.revision if revision else 0,
                "view_id": revision.result_view_id if revision else scenario.base_view_id,
                "actions": actions, "base_objects": objects(base), "objects": objects(candidate),
                "edges": [{"source_type": src[0], "source_id": src[1], "relation_type": rel,
                           "target_type": dst[0], "target_id": dst[1]} for src, rel, dst in candidate.edges],
                "changes": {"created": [{"object_type": key[0], "object_id": key[1]} for key in sorted(candidate_keys - base_keys)],
                            "deleted": [{"object_type": key[0], "object_id": key[1]} for key in sorted(base_keys - candidate_keys)],
                            "modified": [{"object_type": key[0], "object_id": key[1],
                                          "before": base.objects[key], "after": candidate.objects[key]} for key in sorted(modified)]},
                "edits": changeset.ordered_edits if may_inspect_inputs else []}
    except ScenarioError as exc:
        _http(exc)


@router.get("/{ontology_id}/scenarios/{scenario_id}/compare")
def compare_scenarios(ontology_id: str, scenario_id: str, metric_type: str = "",
                      metric_property: str = "", db=Depends(get_db), user=Depends(get_current_user)):
    try:
        service = _service(ontology_id, db, user)
        active = service.get(scenario_id)
        base_full = _snapshot(service, active.base_view_id)
        base, _ = authorize_action_snapshot(db, ontology_id, user, base_full)
        options = sorted({(kind, prop) for (kind, _), values in base.objects.items()
                          for prop, value in values.items() if not prop.startswith("_")
                          and isinstance(value, (int, float)) and not isinstance(value, bool)})
        if metric_type and (metric_type, metric_property) not in options:
            raise ScenarioError("invalid_metric", "Select a numeric property from this Scenario base")
        def metric(snapshot):
            if not metric_type:
                return len(snapshot.objects)
            return sum(float(props.get(metric_property) or 0) for (kind, _), props in
                       snapshot.objects.items() if kind == metric_type and
                       isinstance(props.get(metric_property), (int, float)) and
                       not isinstance(props.get(metric_property), bool))
        from app.models.v2.scenario import ScenarioResource
        peers = db.query(ScenarioResource).filter_by(ontology_id=ontology_id,
                  base_view_id=active.base_view_id, status="active").order_by(ScenarioResource.name).all()
        columns = []
        for peer in peers:
            try:
                service.get(peer.id)
            except ScenarioError:
                continue
            revision = db.query(ScenarioRevision).filter_by(scenario_id=peer.id,
                        revision=peer.head_revision).first() if peer.head_revision else None
            snapshot_full = _snapshot(service, revision.result_view_id) if revision else base_full
            snapshot, _ = authorize_action_snapshot(db, ontology_id, user, snapshot_full)
            base_keys, keys = set(base.objects), set(snapshot.objects)
            columns.append({"id": peer.id, "name": peer.name, "revision": peer.head_revision,
                            "metric": metric(snapshot),
                            "created": len(keys - base_keys), "deleted": len(base_keys - keys),
                            "modified": sum(base.objects[key] != snapshot.objects[key]
                                            for key in keys & base_keys),
                            "links_added": len(set(snapshot.edges) - set(base.edges)),
                            "links_removed": len(set(base.edges) - set(snapshot.edges))})
        return {"base_view_id": active.base_view_id,
                "metric_options": [{"object_type": kind, "property": prop} for kind, prop in options],
                "metric": {"object_type": metric_type, "property": metric_property},
                "baseline": {"name": "Baseline", "objects": len(base.objects),
                             "links": len(base.edges), "metric": metric(base)}, "columns": columns}
    except ScenarioError as exc:
        _http(exc)


def _merge_state(service, scenario, *, ignore_audit_id: str | None = None):
    db = service.db
    revision = db.query(ScenarioRevision).filter_by(scenario_id=scenario.id,
                revision=scenario.head_revision).first() if scenario.head_revision else None
    if not revision:
        return {"can_merge": False, "conflicts": [{"object_type": "", "object_id": "",
                "property": "", "reason": "Submit Actions before merging"}], "applied_edits": 0,
                "already_merged": False}, [], None
    prior_query = db.query(ScenarioAudit).filter_by(
        scenario_id=scenario.id, revision=revision.revision, operation="merge",
    )
    if ignore_audit_id:
        prior_query = prior_query.filter(ScenarioAudit.id != ignore_audit_id)
    prior_merges = prior_query.order_by(ScenarioAudit.created_at.asc()).all()
    if prior_merges:
        statuses = [(item.summary or {}).get("status") for item in prior_merges]
        if any(status in {None, "completed"} for status in statuses):
            return {"can_merge": False, "conflicts": [], "applied_edits": 0,
                    "already_merged": True}, [], None
        if any(status in {"pending", "reconciliation_required"} for status in statuses):
            return {"can_merge": False, "conflicts": [{
                "object_type": "", "object_id": "", "property": "",
                "reason": "A previous merge request requires reconciliation before this revision can be merged again",
            }], "applied_edits": 0, "already_merged": False,
                "reconciliation_required": True}, [], None
        # Rejected intents are terminal and had no graph effects. They must
        # not permanently prevent a fresh preview/request for this revision.
    changeset = db.get(ScenarioChangeSet, revision.changeset_id)
    if not changeset or not changeset.validation_report.get("replace_all"):
        return {"can_merge": False, "conflicts": [{"object_type": "", "object_id": "",
                "property": "", "reason": "This revision predates the reviewed Action contract"}],
                "applied_edits": 0, "already_merged": False}, [], None
    base = _snapshot(service, scenario.base_view_id)
    live = FalkorReadAdapter(service.graph._graph(scenario.ontology_id)).read(scenario.ontology_id)
    edits = [item for item in changeset.ordered_edits if item.get("op") != "invoke_action"]
    if not changeset.validation_report.get("permission_digest") or not changeset.validation_report.get("preview_digest"):
        return {"can_merge": False, "conflicts": [{"object_type": "", "object_id": "",
                "property": "", "reason": "Review this revision again under the current Action authorization contract"}],
                "applied_edits": 0, "already_merged": False}, [], None
    typed_edits = [Edit.model_validate(item) for item in edits]
    permission_digest = authorize_compiled_edits(db, service.ontology_id, service.user, base, typed_edits)
    if permission_digest != changeset.validation_report.get("permission_digest"):
        raise ScenarioError("permission_changed", "权限已变化；请重新执行 Action 预览并提交", 409)
    # Revalidate against current targets. Visible create collisions remain
    # merge conflicts, while the authorizer still fails closed for hidden IDs.
    live_permission_digest = authorize_compiled_edits(
        db, service.ontology_id, service.user, live, typed_edits,
        allow_create_collisions=True,
    )
    if live_permission_digest != permission_digest:
        raise ScenarioError("permission_changed", "当前数据权限上下文已变化；请重新预览", 409)
    conflicts = []
    projected = {key: dict(props) for key, props in live.objects.items()}
    projected_links = set(live.edges)
    base_links = set(base.edges)
    for edit in edits:
        op = edit.get("op")
        ref = edit.get("target") or {}
        key = (ref.get("concrete_type", ""), ref.get("object_id", ""))
        reason = None
        prop = edit.get("property", "")
        if op in {"set_property", "unset_property"}:
            if key not in projected:
                reason = "Object is absent from main Ontology"
            elif not db.query(EntityInstance).filter_by(
                ontology_id=scenario.ontology_id, id=key[1], is_deleted=False,
            ).first():
                reason = "Object has no canonical SQL row to merge"
            elif projected[key].get(prop) != edit.get("expected_old_value"):
                reason = "Main Ontology value changed since this Scenario was created"
            elif op == "set_property":
                projected[key][prop] = edit.get("value")
            else:
                projected[key].pop(prop, None)
        elif op == "create_object":
            if key in projected:
                reason = "Object already exists in main Ontology"
            else:
                known = db.query(Entity.id).filter(Entity.ontology_id == scenario.ontology_id,
                        (Entity.name_en == key[0]) | (Entity.name_cn == key[0])).first()
                if not known:
                    reason = "Object type is not defined in main Ontology"
                else:
                    projected[key] = dict(edit.get("properties") or {})
        elif op == "delete_object":
            if key not in projected:
                reason = "Object is absent from main Ontology"
            elif not db.query(EntityInstance).filter_by(
                ontology_id=scenario.ontology_id, id=key[1], is_deleted=False,
            ).first():
                reason = "Object has no canonical SQL row to merge"
            elif key not in base.objects or {k: v for k, v in projected[key].items() if not k.startswith("_")} != {
                    k: v for k, v in base.objects[key].items() if not k.startswith("_")}:
                reason = "Object changed since this Scenario was created"
            else:
                projected.pop(key)
                projected_links = {link for link in projected_links if key not in (link[0], link[2])}
        elif op in {"add_link", "remove_link"}:
            link = edit.get("link") or {}
            source, target = link.get("source") or {}, link.get("target") or {}
            edge = ((source.get("concrete_type"), source.get("object_id")), link.get("relation_type"),
                    (target.get("concrete_type"), target.get("object_id")))
            if edge[0] not in projected or edge[2] not in projected:
                reason = "Link endpoint is absent from main Ontology"
            elif op == "add_link" and edge in projected_links:
                reason = "Link already exists in main Ontology"
            elif op == "remove_link" and edge not in projected_links:
                reason = "Link is absent from main Ontology"
            elif op == "remove_link" and edge not in base_links:
                reason = "Link was not present in the Scenario base"
            elif op == "add_link":
                projected_links.add(edge)
            else:
                projected_links.remove(edge)
        else:
            reason = f"Unsupported merge edit {op}"
        if reason:
            conflicts.append({"object_type": key[0], "object_id": key[1], "property": prop,
                              "reason": reason})
    visible_live, _ = authorize_action_snapshot(
        db, service.ontology_id, service.user, live,
    )
    changes = service._revision_preview_changes(
        visible_live, typed_edits, allow_existing_create_conflicts=True,
    )
    result = {"can_merge": bool(edits) and not conflicts, "conflicts": conflicts,
              "applied_edits": len(edits), "already_merged": False,
              "changes": changes}
    merge_digest = stable_hash({
        "scenario_id": scenario.id,
        "scenario_revision": scenario.head_revision,
        "scenario_etag": scenario.etag,
        "scenario_content_digest": revision.content_digest,
        "live_objects": sorted(live.objects.items()),
        "live_edges": sorted(live.edges),
        "permission_digest": permission_digest,
        "merge_result": result,
        "edits": edits,
    })
    return result, edits, merge_digest


@router.post("/{ontology_id}/scenarios/{scenario_id}:merge-preview")
def preview_merge(ontology_id: str, scenario_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    try:
        service = _service(ontology_id, db, user)
        scenario = service.get(scenario_id)
        if scenario.protected_demo:
            raise ScenarioError("protected_scenario", "Prepared demo Scenarios cannot be merged", 403)
        result, _, preview_digest = _merge_state(service, scenario)
        return result | {"preview_digest": preview_digest,
                         "expected_revision": scenario.head_revision}
    except ScenarioError as exc:
        _http(exc)


@router.post("/{ontology_id}/scenarios/{scenario_id}:merge")
def merge_scenario(ontology_id: str, scenario_id: str, body: MergeCommand,
                   db=Depends(get_db), user=Depends(get_current_user)):
    intent_id = None
    effects_started = False
    try:
        service = _service(ontology_id, db, user)
        scenario = service.get(scenario_id, write=True)
        scenario = db.query(ScenarioResource).filter_by(
            id=scenario.id, ontology_id=ontology_id,
        ).with_for_update().first()
        if scenario is None:
            raise ScenarioError("not_found", "Scenario not found", 404)
        if scenario.protected_demo:
            raise ScenarioError("protected_scenario", "Prepared demo Scenarios cannot be merged", 403)

        request_digest = stable_hash({
            "ontology_id": ontology_id,
            "scenario_id": scenario.id,
            "actor_id": str(user.id),
            "expected_revision": body.expected_revision,
            "preview_digest": body.preview_digest,
        })
        prior = db.query(ScenarioAudit).filter_by(
            scenario_id=scenario.id,
            operation="merge",
            request_id=body.client_request_id,
        ).first()
        if prior:
            summary = prior.summary or {}
            if summary.get("request_digest") != request_digest:
                raise ScenarioError("idempotency_conflict", "Request ID was used for a different Scenario merge", 409)
            if summary.get("status") == "completed":
                return {
                    "scenario_id": scenario.id,
                    "revision": summary.get("revision"),
                    "status": "merged",
                    "applied_edits": summary.get("applied_edits", 0),
                    "audit_id": prior.id,
                    "action_run_id": summary.get("action_run_id"),
                    "replayed": True,
                }
            if summary.get("status") == "rejected":
                raise ScenarioError(
                    "merge_request_rejected",
                    summary.get("last_error") or "The recorded merge request failed preconditions",
                    409,
                )
            raise ScenarioError(
                "merge_reconciliation_required",
                "此合并请求已有持久记录但未确认完成；为避免重复写入，请先核对审计记录并完成恢复。",
                409,
            )

        if scenario.head_revision != body.expected_revision:
            raise ScenarioError("revision_conflict", "Scenario changed; review the merge again", 409)
        result, edits, preview_digest = _merge_state(service, scenario)
        if not preview_digest or body.preview_digest != preview_digest:
            raise ScenarioError("preview_conflict", "本体、权限或情景已变化；请重新检查合并", 409)
        if not result["can_merge"]:
            raise ScenarioError("merge_conflict", "Merge preview contains conflicts or no supported edits", 409)

        revision = db.query(ScenarioRevision).filter_by(
            scenario_id=scenario.id, revision=scenario.head_revision,
        ).first()
        changeset = db.get(ScenarioChangeSet, revision.changeset_id) if revision else None
        if revision is None or changeset is None:
            raise ScenarioError("merge_manifest_missing", "Scenario revision manifest is unavailable", 409)
        permission_digest = (changeset.validation_report or {}).get("permission_digest")
        if not permission_digest:
            raise ScenarioError("merge_manifest_missing", "Scenario permission manifest is unavailable", 409)

        intent = ScenarioAudit(
            scenario_id=scenario.id,
            revision=scenario.head_revision,
            actor_id=user.id,
            operation="merge",
            request_id=body.client_request_id,
            summary={
                "status": "pending",
                "request_digest": request_digest,
                "preview_digest": preview_digest,
                "applied_edits": len(edits),
                "base_view_id": scenario.base_view_id,
                "scenario_revision_id": revision.id,
                "changeset_id": changeset.id,
                "content_digest": revision.content_digest,
                "permission_digest": permission_digest,
                "edits_digest": stable_hash(edits),
            },
        )
        db.add(intent)
        runtime_action_run = create_action_run_record(
            db=db,
            action_type_id=None,
            ontology_id=ontology_id,
            parameters={
                "scenario_id": scenario.id,
                "scenario_revision": scenario.head_revision,
                "edit_count": len(edits),
                "edits_digest": stable_hash(edits),
            },
            execution_context={
                "scenario_id": scenario.id,
                "scenario_revision": scenario.head_revision,
                "base_view_id": scenario.base_view_id,
                "source": "scenario_merge",
                "target": "live",
            },
            initial_status="running",
            requester_id=str(user.id),
            executor_id=str(user.id),
            client_request_id=body.client_request_id,
            idempotency_scope=f"scenario-merge:{scenario.id}",
            input_digest=request_digest,
            preview_digest=preview_digest,
            permission_digest=permission_digest,
        )
        db.add(runtime_action_run)
        db.flush()
        intent.summary = {
            **(intent.summary or {}),
            "action_run_id": runtime_action_run.id,
        }
        db.commit()
        intent_id = intent.id

        # The durable intent commit releases the first row lock. Re-lock and
        # revalidate the Scenario/merge digest before beginning graph writes.
        scenario = db.query(ScenarioResource).filter_by(
            id=scenario_id, ontology_id=ontology_id,
        ).with_for_update().populate_existing().first()
        if scenario is None or scenario.head_revision != body.expected_revision:
            raise ScenarioError("revision_conflict", "Scenario changed before merge commit", 409)
        refreshed_result, refreshed_edits, refreshed_digest = _merge_state(
            service, scenario, ignore_audit_id=intent_id,
        )
        if (refreshed_digest != preview_digest or not refreshed_result["can_merge"]
                or stable_hash(refreshed_edits) != stable_hash(edits)):
            raise ScenarioError("preview_conflict", "本体、权限或情景在合并开始前发生变化；请重新检查", 409)

        from app.services.v2.live_edits import apply_live_edits
        restore_graph = None
        try:
            typed_edits = [Edit.model_validate(item) for item in refreshed_edits]
            effects_started = True
            before_snapshot, after_snapshot, side_effect_results, restore_graph = apply_live_edits(
                db,
                ontology_id,
                typed_edits,
                service.graph,
                actor=user,
                expected_permission_digest=permission_digest,
            )
            intent = db.get(ScenarioAudit, intent_id)
            runtime_action_run = db.get(OntologyActionRun, runtime_action_run.id)
            complete_action_run(
                db,
                runtime_action_run,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                side_effect_results=side_effect_results,
                actor_id=str(user.id),
                actor_kind="user",
            )
            runtime_action_run.before_digest = stable_hash(before_snapshot)
            runtime_action_run.after_digest = stable_hash(after_snapshot)
            intent.summary = {
                **(intent.summary or {}),
                "status": "completed",
                "revision": scenario.head_revision,
                "applied_edits": len(refreshed_edits),
            }
            db.commit()
        except Exception as exc:
            db.rollback()
            if restore_graph:
                try:
                    restore_graph()
                except Exception:
                    pass
            _mark_merge_reconciliation_required(db, intent_id, exc, status="reconciliation_required")
            raise
        return {"scenario_id": scenario.id, "revision": scenario.head_revision,
                "status": "merged", "applied_edits": len(refreshed_edits),
                "audit_id": intent_id, "action_run_id": runtime_action_run.id,
                "replayed": False}
    except ScenarioError as exc:
        if intent_id:
            status = "reconciliation_required" if effects_started else "rejected"
            _mark_merge_reconciliation_required(db, intent_id, exc, status=status)
            if effects_started:
                raise HTTPException(409, {
                    "code": "merge_reconciliation_required",
                    "message": "合并已开始但未能确认完成；请使用持久请求编号执行恢复。",
                }) from exc
        _http(exc)
    except Exception as exc:
        if intent_id:
            status = "reconciliation_required" if effects_started else "rejected"
            _mark_merge_reconciliation_required(db, intent_id, exc, status=status)
            if effects_started:
                raise HTTPException(409, {
                    "code": "merge_reconciliation_required",
                    "message": "合并已开始但未能确认完成；请使用持久请求编号执行恢复。",
                }) from exc
        raise HTTPException(500, {"code": "merge_failed", "message": "Scenario merge failed"}) from exc


def _mark_merge_reconciliation_required(
    db, audit_id: str, error: Exception, *, status: str,
) -> None:
    """Leave a durable, non-replayable audit state after a merge intent exists."""
    try:
        db.rollback()
        intent = db.get(ScenarioAudit, audit_id)
        if intent is None:
            return
        summary = dict(intent.summary or {})
        if summary.get("status") == "completed":
            return
        action_run_id = summary.get("action_run_id")
        runtime_action_run = db.get(OntologyActionRun, action_run_id) if action_run_id else None
        if runtime_action_run is not None and runtime_action_run.status == "running":
            if status == "reconciliation_required":
                transition_action_run(
                    db, runtime_action_run, "reconciliation_required",
                    actor_id=runtime_action_run.executed_by, actor_kind="user",
                )
            else:
                fail_action_run(
                    db, runtime_action_run,
                    "Scenario merge stopped before live writes",
                    actor_id=runtime_action_run.executed_by, actor_kind="user",
                )
        intent.summary = {
            **summary,
            "status": status,
            "last_error": str(error)[:1000],
        }
        db.commit()
    except Exception:
        db.rollback()


@contextmanager
def _merge_reconciliation_lock(db, audit_id: str):
    """Serialize recovery attempts across API workers on PostgreSQL."""
    if db.get_bind().dialect.name != "postgresql":
        # The local SQLite development profile runs a single API process.
        yield
        return

    lock_key = int.from_bytes(
        hashlib.sha256(f"scenario-merge-reconcile:{audit_id}".encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )
    acquired = bool(db.execute(
        text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": lock_key},
    ).scalar())
    db.commit()
    if not acquired:
        raise ScenarioError(
            "merge_reconciliation_in_progress",
            "Another recovery attempt is already processing this merge",
            409,
        )
    try:
        yield
    finally:
        try:
            db.execute(text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key})
            db.commit()
        except Exception:
            db.rollback()


def _mark_merge_reconciliation_attempt_failed(db, attempt_id: str, merge_id: str, code: str) -> None:
    """Keep only a safe error code in durable recovery audit state."""
    try:
        db.rollback()
        attempt = db.get(ScenarioAudit, attempt_id)
        merge = db.get(ScenarioAudit, merge_id)
        if attempt is not None:
            summary = dict(attempt.summary or {})
            if summary.get("status") != "completed":
                attempt.summary = {
                    **summary,
                    "status": "reconciliation_required",
                    "last_error_code": code,
                }
        if merge is not None:
            summary = dict(merge.summary or {})
            if summary.get("status") != "completed":
                merge.summary = {
                    **summary,
                    "status": "reconciliation_required",
                    "last_reconciliation_error_code": code,
                }
        db.commit()
    except Exception:
        db.rollback()


@router.post("/{ontology_id}/scenarios/{scenario_id}:merge-reconcile")
def reconcile_scenario_merge(
    ontology_id: str,
    scenario_id: str,
    body: MergeReconcileCommand,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    attempt_id = None
    merge_id = None
    try:
        reason = body.reason.strip()
        if not reason:
            raise ScenarioError("reason_required", "填写恢复原因后才能继续", 422, "reason")

        service = _service(ontology_id, db, user)
        service.get(scenario_id, write=True)
        original = db.query(ScenarioAudit).filter_by(
            scenario_id=scenario_id,
            operation="merge",
            request_id=body.merge_request_id,
        ).first()
        if original is None:
            raise ScenarioError("merge_request_not_found", "找不到待恢复的原合并请求", 404)
        merge_id = original.id
        if str(original.actor_id) != str(user.id) and user.role != "admin":
            raise ScenarioError(
                "forbidden",
                "只有原合并操作者或管理员可以恢复这次合并",
                403,
            )

        with _merge_reconciliation_lock(db, merge_id):
            db.expire_all()
            scenario = db.query(ScenarioResource).filter_by(
                id=scenario_id, ontology_id=ontology_id,
            ).populate_existing().with_for_update().first()
            if scenario is None:
                raise ScenarioError("not_found", "Scenario not found", 404)
            service.get(scenario.id, write=True)
            if scenario.protected_demo:
                raise ScenarioError("protected_scenario", "Prepared demo Scenarios cannot be merged", 403)

            original = db.query(ScenarioAudit).filter_by(
                id=merge_id, scenario_id=scenario.id, operation="merge",
            ).populate_existing().with_for_update().first()
            if original is None:
                raise ScenarioError("merge_request_not_found", "找不到待恢复的原合并请求", 404)
            summary = dict(original.summary or {})
            current_status = summary.get("status")
            if current_status == "completed":
                return {
                    "scenario_id": scenario.id,
                    "revision": original.revision,
                    "status": "merged",
                    "applied_edits": summary.get("applied_edits", 0),
                    "audit_id": original.id,
                    "action_run_id": summary.get("action_run_id"),
                    "replayed": True,
                    "reconciled": False,
                }
            if current_status not in {"pending", "reconciliation_required"}:
                raise ScenarioError(
                    "merge_reconciliation_unavailable",
                    "这个合并请求没有可恢复的持久状态",
                    409,
                )

            revision = db.get(ScenarioRevision, summary.get("scenario_revision_id"))
            if (
                revision is None
                or revision.scenario_id != scenario.id
                or revision.revision != original.revision
                or revision.content_digest != summary.get("content_digest")
                or revision.changeset_id != summary.get("changeset_id")
            ):
                raise ScenarioError("merge_manifest_conflict", "已批准的修订清单校验失败", 409)
            changeset = db.get(ScenarioChangeSet, revision.changeset_id)
            if changeset is None:
                raise ScenarioError("merge_manifest_missing", "合并变更集不可用", 409)
            validation = changeset.validation_report or {}
            if (
                not validation.get("replace_all")
                or validation.get("permission_digest") != summary.get("permission_digest")
            ):
                raise ScenarioError("merge_manifest_conflict", "变更集授权清单校验失败", 409)
            raw_edits = [
                item for item in changeset.ordered_edits
                if item.get("op") != "invoke_action"
            ]
            if not raw_edits or stable_hash(raw_edits) != summary.get("edits_digest"):
                raise ScenarioError("merge_manifest_conflict", "已批准的操作内容与恢复清单不一致", 409)
            edits = [Edit.model_validate(item) for item in raw_edits]

            base_view_id = summary.get("base_view_id")
            if not base_view_id:
                raise ScenarioError("merge_manifest_missing", "合并基础视图不可用", 409)
            base = _snapshot(service, base_view_id)
            recovery_permission_digest = authorize_compiled_edits(
                db, ontology_id, user, base, edits,
            )
            if (
                str(original.actor_id) == str(user.id)
                and recovery_permission_digest != summary.get("permission_digest")
            ):
                raise ScenarioError(
                    "permission_changed",
                    "原操作者的权限已变化；请管理员检查后再恢复",
                    409,
                )

            request_digest = stable_hash({
                "ontology_id": ontology_id,
                "scenario_id": scenario.id,
                "merge_audit_id": original.id,
                "actor_id": str(user.id),
                "reason": reason,
            })
            prior = db.query(ScenarioAudit).filter_by(
                scenario_id=scenario.id,
                operation="merge_reconcile",
                request_id=body.client_request_id,
            ).populate_existing().first()
            if prior is not None:
                prior_summary = dict(prior.summary or {})
                if prior_summary.get("request_digest") != request_digest:
                    raise ScenarioError(
                        "idempotency_conflict",
                        "恢复请求编号已用于不同的原因或合并请求",
                        409,
                    )
                if prior_summary.get("status") == "completed":
                    return {
                        "scenario_id": scenario.id,
                        "revision": original.revision,
                        "status": "merged",
                        "applied_edits": len(edits),
                        "audit_id": original.id,
                        "action_run_id": (original.summary or {}).get("action_run_id"),
                        "reconciliation_audit_id": prior.id,
                        "replayed": True,
                        "reconciled": True,
                    }
                if prior_summary.get("status") not in {"pending", "reconciliation_required"}:
                    raise ScenarioError("merge_reconciliation_unavailable", "恢复请求不可重放", 409)
                attempt = prior
            else:
                attempts = db.query(ScenarioAudit).filter_by(
                    scenario_id=scenario.id,
                    operation="merge_reconcile",
                ).all()
                active = next((row for row in attempts
                               if (row.summary or {}).get("target_merge_audit_id") == original.id
                               and (row.summary or {}).get("status") in {"pending", "reconciliation_required"}), None)
                if active is not None:
                    raise ScenarioError(
                        "merge_reconciliation_in_progress",
                        "已有恢复请求正在处理；请使用原恢复请求编号重试",
                        409,
                    )
                attempt = ScenarioAudit(
                    scenario_id=scenario.id,
                    revision=original.revision,
                    actor_id=user.id,
                    operation="merge_reconcile",
                    request_id=body.client_request_id,
                    summary={
                        "status": "pending",
                        "request_digest": request_digest,
                        "target_merge_audit_id": original.id,
                        "reason": reason,
                        "authorization_digest": recovery_permission_digest,
                    },
                )
                db.add(attempt)
                db.flush()
                db.commit()
            attempt_id = attempt.id

            try:
                counts = reconcile_live_edits(
                    db,
                    ontology_id,
                    edits,
                    service.graph,
                    base,
                    actor=user,
                    expected_permission_digest=recovery_permission_digest,
                )
                original = db.get(ScenarioAudit, merge_id)
                attempt = db.get(ScenarioAudit, attempt_id)
                runtime_action_run_id = (original.summary or {}).get("action_run_id")
                runtime_action_run = (
                    db.get(OntologyActionRun, runtime_action_run_id)
                    if runtime_action_run_id else None
                )
                if runtime_action_run is not None and runtime_action_run.status in {
                    "running", "reconciliation_required",
                }:
                    complete_action_run(
                        db,
                        runtime_action_run,
                        before_snapshot={
                            "scenario_id": scenario.id,
                            "base_view_id": base_view_id,
                            "scenario_revision": original.revision,
                        },
                        after_snapshot={
                            "scenario_id": scenario.id,
                            "scenario_revision": original.revision,
                            "edits_digest": summary.get("edits_digest"),
                        },
                        side_effect_results=[
                            {"op": "reconciliation", **counts},
                        ],
                        actor_id=str(user.id),
                        actor_kind="user",
                    )
                    runtime_action_run.before_digest = stable_hash({
                        "base_view_id": base_view_id,
                        "scenario_revision": original.revision,
                    })
                    runtime_action_run.after_digest = stable_hash({
                        "edits_digest": summary.get("edits_digest"),
                        **counts,
                    })
                now = datetime.now(timezone.utc).isoformat()
                original.summary = {
                    **dict(original.summary or {}),
                    "status": "completed",
                    "reconciled_at": now,
                    "reconciled_by": str(user.id),
                    "reconciliation_reason": reason,
                    "reconciliation_audit_id": attempt.id,
                    "authorization_digest_at_recovery": recovery_permission_digest,
                }
                attempt.summary = {
                    **dict(attempt.summary or {}),
                    "status": "completed",
                    "completed_at": now,
                    "objects_reconciled": counts["objects_reconciled"],
                    "relationships_reconciled": counts["relationships_reconciled"],
                }
                db.commit()
            except Exception as exc:
                code = exc.code if isinstance(exc, ScenarioError) else "reconciliation_failed"
                _mark_merge_reconciliation_attempt_failed(db, attempt_id, merge_id, code)
                raise

            return {
                "scenario_id": scenario.id,
                "revision": original.revision,
                "status": "merged",
                "applied_edits": len(edits),
                "audit_id": original.id,
                "action_run_id": (original.summary or {}).get("action_run_id"),
                "reconciliation_audit_id": attempt_id,
                "replayed": False,
                "reconciled": True,
            }
    except ScenarioError as exc:
        if attempt_id and merge_id:
            _mark_merge_reconciliation_attempt_failed(db, attempt_id, merge_id, exc.code)
        _http(exc)
    except Exception as exc:
        if attempt_id and merge_id:
            _mark_merge_reconciliation_attempt_failed(db, attempt_id, merge_id, "reconciliation_failed")
        raise HTTPException(500, {"code": "merge_reconciliation_failed", "message": "Scenario merge recovery failed"}) from exc
