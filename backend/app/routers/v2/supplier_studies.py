"""Study/Case commands and read models for the supplier workbench."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.deps import get_current_user, get_db
from app.models.v2.query_view import QueryDataView
from app.models.v2.scenario import (ScenarioChangeSet, ScenarioResource, ScenarioRevision, ScenarioRun,
                                    ScenarioRunStage, ScenarioStudy, ScenarioStudyCase,
                                    ScenarioRunWarning, ScenarioRunArtifact, ScenarioAudit)
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.object_query.core import FalkorReadAdapter
from app.services.v2.object_query.normalize import stable_hash
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError
from app.services.v2.scenarios import ScenarioError, ScenarioService
from app.services.v2.action_authorization import authorize_action_snapshot
from app.services.v2.action_runtime import (
    ActionRequest,
    compile_with_authorization,
    create_action_run_record,
    acquire_initial_scenario_action_run_claim,
    claim_idempotent_scenario_action_run,
    fail_scenario_action_run_claim,
    renew_scenario_action_run_claim,
    scenario_action_run_claim_manifest,
)
from app.services.v2.supplier_action import compile_supplier_action
from app.services.v2.supplier_resilience import WOOD, validate_profile
from app.tasks.v2.scenario import STAGES, run_scenario

router = APIRouter()


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Options(StrictBody):
    etag: int
    scenario_time: str
    scope_items: list[str]
    smoothing_minutes: int = 0
    run_baseline_sim: bool = True
    parameter_projection: list[str] = Field(default_factory=list)


class Rename(StrictBody):
    etag: int
    name: str = Field(min_length=1, max_length=200)


class CreateCase(StrictBody):
    name: str = Field(min_length=1, max_length=100)
    source_case_id: str | None = None


class RenameCase(StrictBody):
    etag: int
    name: str = Field(min_length=1, max_length=100)


class ArchiveCase(StrictBody):
    etag: int


class PreviewAction(StrictBody):
    etag: int
    client_request_id: str = Field(min_length=1, max_length=200)
    action_key: str = "replace_wood_supplier_v1"
    parameters: dict


class Submit(PreviewAction):
    preview_digest: str = Field(min_length=64, max_length=64)


class RunCommand(StrictBody):
    etag: int
    client_request_id: str = Field(min_length=1, max_length=200)


class RetryCommand(StrictBody):
    client_request_id: str = Field(min_length=1, max_length=200)


def _require_editor(user):
    if user.role not in ("admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")


def _study(db, user, ontology_id, study_id, *, write: bool = False):
    row = db.get(ScenarioStudy, study_id)
    if not row or row.ontology_id != ontology_id:
        raise HTTPException(404, "未找到研究")
    if user.role != "admin" and row.owner_id != user.id:
        if write:
            raise HTTPException(403, "没有研究编辑权限")
        try:
            require_ontology_access(db, ontology_id, str(user.id), user.role)
        except ObjectQueryError as exc:
            raise HTTPException(403, "没有研究访问权限") from exc
    return row


def _case(db, study, case_id):
    row = db.get(ScenarioStudyCase, case_id)
    if not row or row.study_id != study.id or db.get(ScenarioResource, row.scenario_id).status != "active":
        raise HTTPException(404, "未找到情景")
    return row


def _snapshot(db, view_id, ontology_id):
    view = db.get(QueryDataView, view_id)
    if not view or view.ontology_id != ontology_id or view.status != "ready":
        raise HTTPException(409, "固定数据视图尚未就绪")
    graph = FalkorDBService()
    if not graph.available:
        raise HTTPException(503, "图数据库不可用")
    return FalkorReadAdapter(graph._graph(view.graph_key), graph_ontology_id=view.graph_key).read(ontology_id)


def _compatibility(study, view):
    return stable_hash({"base": view.content_digest, "manifest": study.source_manifest_digest,
                        "time": study.scenario_time, "scope": study.scope_hash,
                        "smoothing": study.smoothing_minutes, "model": (study.model_key, study.model_version,
                        study.model_config_alias), "projection": study.parameter_projection})


def _study_action_context(study):
    return {
        "study_etag": study.etag,
        "model_key": study.model_key,
        "model_version": study.model_version,
        "source_manifest_digest": study.source_manifest_digest,
        "scenario_time": study.scenario_time,
        "scope_definition": study.scope_definition or {},
        "scope_hash": study.scope_hash,
        "smoothing_minutes": study.smoothing_minutes,
        "run_baseline_sim": study.run_baseline_sim,
        "parameter_projection": study.parameter_projection or [],
    }


def _compile_supplier_action_preview(db, user, ontology_id, study, case, scenario, *,
                                     action_key, parameters, client_request_id, expected_case_etag):
    """Compile the exact authorized edit batch used by preview and submit."""
    if expected_case_etag != case.etag:
        raise ScenarioError("etag_conflict", "情景已变化，请刷新后再预览", 409)
    if action_key != "replace_wood_supplier_v1":
        raise ScenarioError("unsupported_action", "不支持的操作", 422)
    validate_profile(parameters)
    raw_snapshot = _snapshot(db, scenario.base_view_id, ontology_id)
    study_context = _study_action_context(study)
    study_context_digest = stable_hash(study_context)
    action_request = ActionRequest.from_invocations(
        ontology_id=ontology_id,
        compiler_scope=scenario.id,
        source="supplier_study",
        invocations=[{"action_type_id": action_key, "parameters": parameters}],
        definition_versions={
            f"scenario_study_case:{case.id}": case.definition_revision,
            f"logic_asset:{study.model_key}": study.model_version,
        },
        execution_context={
            "study_id": study.id,
            "case_id": case.id,
            "case_key": case.case_key,
            "scenario_id": scenario.id,
            "scenario_revision": scenario.head_revision,
            "scenario_etag": scenario.etag,
            "case_definition_revision": case.definition_revision,
            **study_context,
            "study_context_digest": study_context_digest,
        },
        expected_etag=expected_case_etag,
        client_request_id=client_request_id,
    )
    command, permission_digest = compile_with_authorization(
        db, ontology_id, user, raw_snapshot,
        lambda snapshot: compile_supplier_action(
            ontology_id,
            scenario,
            snapshot,
            action_request.invocations[0].parameters,
            action_request.client_request_id,
        ),
        edits_from=lambda result: result.edits,
    )
    preview_digest = stable_hash({
        "action_request": action_request.manifest(),
        "compiled_edits": [edit.model_dump(mode="json") for edit in command.edits],
        "permission_digest": permission_digest,
    })
    return {
        "action_request": action_request,
        "command": command,
        "permission_digest": permission_digest,
        "preview_digest": preview_digest,
        "study_context": study_context,
        "study_context_digest": study_context_digest,
    }


def _latest_compatible_run(db, case, compatibility, run_id=None):
    """Resolve the one result identity shared by every Supplier Study read."""
    scenario = db.get(ScenarioResource, case.scenario_id)
    query = db.query(ScenarioRun).filter_by(
        case_id=case.id,
        revision=scenario.head_revision,
        case_definition_revision=case.definition_revision,
        compatibility_hash=compatibility,
        status="completed",
    )
    run = query.filter(ScenarioRun.id == run_id).first() if run_id else query.order_by(
        ScenarioRun.created_at.desc()
    ).first()
    if run_id and not run:
        raise HTTPException(409, "固定运行与当前情景上下文不兼容")
    if not run:
        return None, "no_compatible_result"
    manifest_view = (run.result_manifest or {}).get("provenance", {}).get("result_view_id")
    if run.result_view_id and manifest_view and run.result_view_id != manifest_view:
        return run, "inconsistent_result_view"
    if not run.result_view_id:
        return run, "missing_result_view"
    return run, "current_success"


def _context_ref(db, study, case, compatibility, run_id=None):
    scenario = db.get(ScenarioResource, case.scenario_id)
    revision = db.query(ScenarioRevision).filter_by(
        scenario_id=scenario.id, revision=scenario.head_revision
    ).first() if scenario.head_revision else None
    run, selection_reason = _latest_compatible_run(db, case, compatibility, run_id)
    revision_view_id = revision.result_view_id if revision else scenario.base_view_id
    selected_result_view_id = run.result_view_id if run and selection_reason == "current_success" else None
    token_payload = {
        "ontology_id": study.ontology_id,
        "study_id": study.id,
        "case_id": case.id,
        "case_definition_revision": case.definition_revision,
        "compatibility_hash": compatibility,
        "scenario_id": scenario.id,
        "scenario_revision": scenario.head_revision,
        "revision_view_id": revision_view_id,
        "selected_run_id": run.id if selected_result_view_id else None,
        "selected_result_view_id": selected_result_view_id,
    }
    return {
        **token_payload,
        "case_key": case.case_key,
        "study_etag": study.etag,
        "case_etag": case.etag,
        "changeset_id": revision.changeset_id if revision else None,
        "selected_run_status": run.status if run else None,
        "input_digest": run.input_digest if selected_result_view_id else None,
        "output_digest": run.output_digest if selected_result_view_id else None,
        "selection_reason": selection_reason,
        "context_token": stable_hash(token_payload),
    }


def _run_json(db, run):
    stages = db.query(ScenarioRunStage).filter_by(run_id=run.id).order_by(ScenarioRunStage.sequence).all()
    warnings = db.query(ScenarioRunWarning).filter_by(run_id=run.id).order_by(ScenarioRunWarning.code).all()
    artifacts = db.query(ScenarioRunArtifact).filter_by(run_id=run.id).order_by(ScenarioRunArtifact.artifact_type).all()
    return {"id": run.id, "case_id": run.case_id, "case_key": run.case_key, "status": run.status,
            "client_request_id": run.client_request_id, "case_definition_revision": run.case_definition_revision,
            "result_view_id": run.result_view_id, "retry_of_run_id": run.retry_of_run_id,
            "progress": run.progress, "current_stage": run.current_stage, "revision": run.revision,
            "depends_on_run_id": run.depends_on_run_id, "compatibility_hash": run.compatibility_hash,
            "duration_ms": run.duration_ms, "input_digest": run.input_digest, "output_digest": run.output_digest,
            "error": run.error_json, "result_manifest": run.result_manifest,
            "warnings": [{"code": item.code, "severity": item.severity, "message": item.message,
                          "object_ref": item.object_ref, "provenance": item.provenance} for item in warnings],
            "artifacts": [{"id": item.id, "type": item.artifact_type, "data_view_id": item.data_view_id,
                           "object_set_definition": item.object_set_definition,
                           "manifest": item.manifest_json, "content_digest": item.content_digest} for item in artifacts],
            "stages": [{"key": s.stage_key, "status": s.status, "duration_ms": s.duration_ms,
                        "summary": s.summary_json, "error": s.error_json} for s in stages]}


def _view(db, study):
    base_view = db.get(QueryDataView, study.base_view_id)
    compatibility = _compatibility(study, base_view)
    cases = [case for case in db.query(ScenarioStudyCase).filter_by(study_id=study.id).order_by(ScenarioStudyCase.sort_order).all()
             if db.get(ScenarioResource, case.scenario_id).status == "active"]
    entries = []
    for case in cases:
        scenario = db.get(ScenarioResource, case.scenario_id)
        revision = db.query(ScenarioRevision).filter_by(scenario_id=scenario.id, revision=scenario.head_revision).first() if scenario.head_revision else None
        runs = db.query(ScenarioRun).filter_by(case_id=case.id).order_by(ScenarioRun.created_at.desc()).limit(10).all()
        entries.append({"id": case.id, "key": case.case_key, "name": case.display_name, "kind": case.case_kind,
                        "protected": scenario.protected_demo,
                        "etag": case.etag, "definition_revision": case.definition_revision,
                        "action_key": case.action_key, "parameters": case.submitted_parameters,
                        "scenario_id": scenario.id, "scenario_revision": scenario.head_revision,
                        "changeset_id": revision.changeset_id if revision else None,
                        "revision_view_id": revision.result_view_id if revision else scenario.base_view_id,
                        "context_ref": _context_ref(db, study, case, compatibility),
                        "runs": [_run_json(db, run) for run in runs]})
    return {"id": study.id, "name": study.name, "description": study.description,
            "ontology_id": study.ontology_id, "etag": study.etag,
            "compatibility_hash": compatibility,
            "base_view_id": study.base_view_id, "source_manifest_digest": study.source_manifest_digest,
            "scenario_time": study.scenario_time, "scope": study.scope_definition,
            "smoothing_minutes": study.smoothing_minutes, "run_baseline_sim": study.run_baseline_sim,
            "model": {"key": study.model_key, "version": study.model_version,
                      "config_alias": study.model_config_alias},
            "parameter_projection": study.parameter_projection, "cases": entries}


@router.get("/{ontology_id}/scenario-studies/{study_id}")
def get_study(ontology_id: str, study_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    return _view(db, _study(db, user, ontology_id, study_id))


@router.patch("/{ontology_id}/scenario-studies/{study_id}")
def rename_study(ontology_id: str, study_id: str, body: Rename, db=Depends(get_db), user=Depends(get_current_user)):
    _require_editor(user)
    study = _study(db, user, ontology_id, study_id, write=True)
    if study.etag != body.etag:
        raise HTTPException(409, "研究已变化，请刷新后再重命名")
    study.name = body.name.strip()
    if not study.name:
        raise HTTPException(422, "研究名称不能为空")
    study.etag += 1
    study.updated_at = datetime.now(timezone.utc)
    db.commit()
    return _view(db, study)


@router.post("/{ontology_id}/scenario-studies/{study_id}/cases", status_code=201)
def create_case(ontology_id: str, study_id: str, body: CreateCase,
                db=Depends(get_db), user=Depends(get_current_user)):
    _require_editor(user)
    study = _study(db, user, ontology_id, study_id, write=True)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "情景名称不能为空")
    cases = db.query(ScenarioStudyCase).filter_by(study_id=study.id).all()
    if any(case.display_name.casefold() == name.casefold() and
           db.get(ScenarioResource, case.scenario_id).status == "active" for case in cases):
        raise HTTPException(409, "情景名称已存在")
    if body.source_case_id:
        source = _case(db, study, body.source_case_id)
        if source.case_kind == "baseline":
            raise HTTPException(422, "请选择一个候选情景作为配置来源")
    else:
        source = next((case for case in cases if case.case_kind == "candidate" and
                       db.get(ScenarioResource, case.scenario_id).status == "active"), None)
        if source is None:
            raise HTTPException(409, "需要候选情景配置")
    profile = dict(source.submitted_parameters)
    validate_profile(profile)
    scenario = ScenarioResource(ontology_id=ontology_id, owner_id=user.id, name=name,
                                description=f"{name} in {study.name}", base_view_id=study.base_view_id)
    db.add(scenario)
    db.flush()
    case = ScenarioStudyCase(study_id=study.id, scenario_id=scenario.id,
                             case_key=f"case_{uuid4().hex[:24]}", display_name=name,
                             case_kind="candidate", sort_order=max((item.sort_order for item in cases), default=0) + 1,
                             action_key="replace_wood_supplier_v1", submitted_parameters=profile,
                             parameters_hash=stable_hash(profile), synthetic_manifest_hash=study.source_manifest_digest)
    db.add(case)
    db.add(ScenarioAudit(scenario_id=scenario.id, actor_id=user.id, operation="create",
                         summary={"study_id": study.id, "source_case_id": source.id}))
    db.commit()
    return next(item for item in _view(db, study)["cases"] if item["id"] == case.id)


@router.patch("/{ontology_id}/scenario-studies/{study_id}/cases/{case_id}/name")
def rename_case(ontology_id: str, study_id: str, case_id: str, body: RenameCase,
                db=Depends(get_db), user=Depends(get_current_user)):
    _require_editor(user)
    study = _study(db, user, ontology_id, study_id, write=True)
    case_hint = _case(db, study, case_id)
    study = db.query(ScenarioStudy).filter_by(
        id=study.id, ontology_id=ontology_id,
    ).populate_existing().with_for_update().first()
    if study is None:
        raise HTTPException(404, "未找到研究")
    scenario = db.query(ScenarioResource).filter_by(
        id=case_hint.scenario_id, ontology_id=ontology_id,
    ).populate_existing().with_for_update().first()
    if scenario is None:
        raise HTTPException(404, "未找到情景")
    case = db.query(ScenarioStudyCase).filter_by(
        id=case_id, study_id=study.id,
    ).populate_existing().with_for_update().first()
    if case is None:
        raise HTTPException(404, "未找到情景")
    if case.etag != body.etag:
        raise HTTPException(409, "情景已变化，请刷新后再重命名")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "情景名称不能为空")
    if db.query(ScenarioStudyCase).filter_by(study_id=study.id, display_name=name).filter(
            ScenarioStudyCase.id != case.id).first():
        raise HTTPException(409, "情景名称已存在")
    case.display_name = name
    case.etag += 1
    scenario.name = name
    scenario.etag += 1
    scenario.updated_at = datetime.now(timezone.utc)
    db.add(ScenarioAudit(scenario_id=scenario.id, actor_id=user.id, operation="rename",
                         summary={"name": name}))
    db.commit()
    return next(item for item in _view(db, study)["cases"] if item["id"] == case.id)


@router.post("/{ontology_id}/scenario-studies/{study_id}/cases/{case_id}:archive")
def archive_case(ontology_id: str, study_id: str, case_id: str, body: ArchiveCase,
                 db=Depends(get_db), user=Depends(get_current_user)):
    _require_editor(user)
    study = _study(db, user, ontology_id, study_id, write=True)
    case_hint = _case(db, study, case_id)
    study = db.query(ScenarioStudy).filter_by(
        id=study.id, ontology_id=ontology_id,
    ).populate_existing().with_for_update().first()
    if study is None:
        raise HTTPException(404, "未找到研究")
    scenario = db.query(ScenarioResource).filter_by(
        id=case_hint.scenario_id, ontology_id=ontology_id,
    ).populate_existing().with_for_update().first()
    if scenario is None:
        raise HTTPException(404, "未找到情景")
    case = db.query(ScenarioStudyCase).filter_by(
        id=case_id, study_id=study.id,
    ).populate_existing().with_for_update().first()
    if case is None:
        raise HTTPException(404, "未找到情景")
    if case.case_kind == "baseline" or scenario.protected_demo:
        raise HTTPException(422, "准备好的演示情景不能归档")
    if case.etag != body.etag:
        raise HTTPException(409, "情景已变化，请刷新后再归档")
    if db.query(ScenarioRun).filter_by(case_id=case.id).filter(ScenarioRun.status.in_(["queued", "running"])).first():
        raise HTTPException(409, "请等待当前运行完成")
    scenario.status = "archived"
    scenario.etag += 1
    scenario.updated_at = datetime.now(timezone.utc)
    db.add(ScenarioAudit(scenario_id=scenario.id, actor_id=user.id, operation="archive",
                         summary={"study_id": study.id, "case_id": case.id}))
    db.commit()
    return {"id": case.id, "status": "archived"}


@router.get("/{ontology_id}/scenario-studies/{study_id}/cases/{case_id}/graph")
def case_graph(ontology_id: str, study_id: str, case_id: str,
               run_id: str | None = Query(default=None),
               context_token: str | None = Query(default=None),
               db=Depends(get_db), user=Depends(get_current_user)):
    try:
        study = _study(db, user, ontology_id, study_id)
        case = _case(db, study, case_id)
        compatibility = _compatibility(study, db.get(QueryDataView, study.base_view_id))
        context = _context_ref(db, study, case, compatibility, run_id)
        if context_token and context_token != context["context_token"]:
            raise HTTPException(409, "情景上下文已过期，请刷新研究")
        if context["selection_reason"] == "inconsistent_result_view":
            raise HTTPException(409, "运行结果视图来源不一致")
        view_id = context["selected_result_view_id"] or context["revision_view_id"]
        snapshot, _ = authorize_action_snapshot(db, ontology_id, user,
                                                _snapshot(db, view_id, ontology_id))
        kinds = {"Supplier", "SupplyOption", "Item", "Demand", "Operation", "MaterialRequirement", "Inventory", "DemandImpact"}
        nodes = [{"id": identity[1], "type": identity[0], "name": props.get("name", identity[1]),
                  "properties": props} for identity, props in snapshot.objects.items() if identity[0] in kinds]
        ids = {node["id"] for node in nodes}
        edges = [{"source": source[1], "target": target[1], "type": relation}
                 for source, relation, target in snapshot.edges if source[1] in ids and target[1] in ids]
        return {"context": {**context, "view_id": view_id,
                             "read_layer": "result" if context["selected_result_view_id"] else "revision"},
                "nodes": nodes, "edges": edges, "truncated": False}
    except ScenarioError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    except ObjectQueryError as exc:
        error_status = {
            "not_found": 404,
            "forbidden": 403,
            "version_conflict": 409,
            "expired_reference": 410,
            "stale_reference": 409,
            "metadata_mismatch": 409,
            "graph_unavailable": 503,
            "query_timeout": 504,
            "snapshot_unavailable": 409,
            "view_build_failed": 422,
        }.get(exc.code, 422)
        raise HTTPException(error_status, detail=exc.as_dict()) from exc


@router.patch("/{ontology_id}/scenario-studies/{study_id}/options")
def update_options(ontology_id: str, study_id: str, body: Options, db=Depends(get_db), user=Depends(get_current_user)):
    _require_editor(user)
    study = _study(db, user, ontology_id, study_id, write=True)
    if study.etag != body.etag:
        raise HTTPException(409, "研究已变化，请刷新后再编辑")
    if body.smoothing_minutes != 0:
        raise HTTPException(422, "当前模型不支持非零平滑")
    try:
        anchor = datetime.fromisoformat(body.scenario_time.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(422, "情景时间无效") from exc
    if not datetime(2021, 1, 1) <= anchor.replace(tzinfo=None) <= datetime(2021, 12, 31):
        raise HTTPException(422, "情景时间必须位于固定样例年份内")
    if not body.scope_items or not set(body.scope_items) <= set(WOOD):
        raise HTTPException(422, "范围必须包含一种或两种木材")
    allowed = {"lead_days", "minimum_order", "order_multiple", "capacity", "cost_multiplier"}
    if not set(body.parameter_projection) <= allowed:
        raise HTTPException(422, "未知的参数投影")
    study.scenario_time = body.scenario_time
    study.scope_definition = {"kind": "objects_on_graph", "items": body.scope_items}
    study.scope_hash = stable_hash(study.scope_definition)
    study.run_baseline_sim = body.run_baseline_sim
    study.parameter_projection = body.parameter_projection
    study.etag += 1
    study.updated_at = datetime.now(timezone.utc)
    db.commit()
    return _view(db, study)


@router.post("/{ontology_id}/scenario-studies/{study_id}/cases/{case_id}:preview-action")
def preview_action(ontology_id: str, study_id: str, case_id: str, body: PreviewAction,
                   db=Depends(get_db), user=Depends(get_current_user)):
    _require_editor(user)
    study = _study(db, user, ontology_id, study_id, write=True)
    case = _case(db, study, case_id)
    if case.case_kind == "baseline":
        raise HTTPException(422, "基线方案没有操作")
    try:
        service = ScenarioService(db, ontology_id, user)
        scenario = service.get(case.scenario_id, write=True)
        prepared = _compile_supplier_action_preview(
            db, user, ontology_id, study, case, scenario,
            action_key=body.action_key,
            parameters=body.parameters,
            client_request_id=body.client_request_id,
            expected_case_etag=body.etag,
        )
        command = prepared["command"]
        edits = [edit for edit in command.edits if edit.op != "invoke_action"]
        created = [edit for edit in edits if edit.op == "create_object"]
        disabled = [edit for edit in edits if edit.op == "set_property"
                    and edit.property == "active" and edit.value is False]
        links = [edit for edit in edits if edit.op == "add_link"]
        return {
            "valid": True,
            "client_request_id": body.client_request_id,
            "preview_digest": prepared["preview_digest"],
            "permission_digest": prepared["permission_digest"],
            "case_etag": case.etag,
            "study_etag": study.etag,
            "scenario_etag": scenario.etag,
            "scenario_revision": scenario.head_revision,
            "summary": {
                "supplier": body.parameters["supplier"],
                "affected_items": list(WOOD),
                "created_objects": len(created),
                "disabled_options": len(disabled),
                "created_links": len(links),
                "total_changes": len(edits),
            },
        }
    except ScenarioError as exc:
        db.rollback()
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.patch("/{ontology_id}/scenario-studies/{study_id}/cases/{case_id}")
def submit_action(ontology_id: str, study_id: str, case_id: str, body: Submit,
                  db=Depends(get_db), user=Depends(get_current_user)):
    runtime_action_run = None
    execution_claim_token = None

    def fail_current_claim(error_code: str):
        if runtime_action_run is None or not execution_claim_token:
            return
        try:
            fail_scenario_action_run_claim(
                db,
                run_id=runtime_action_run.id,
                claim_token=execution_claim_token,
                error=error_code,
                actor_id=str(user.id),
            )
        except Exception:
            db.rollback()

    _require_editor(user)
    study = _study(db, user, ontology_id, study_id, write=True)
    case = _case(db, study, case_id)
    if case.case_kind == "baseline":
        raise HTTPException(422, "基线方案没有操作")
    try:
        service = ScenarioService(db, ontology_id, user)
        scenario = service.get(case.scenario_id, write=True)
        action_scope = f"scenario:{scenario.id}"
        request_digest = stable_hash({
            "ontology_id": ontology_id,
            "study_id": study.id,
            "case_id": case.id,
            "action_key": body.action_key,
            "parameters": body.parameters,
            "expected_case_etag": body.etag,
            "preview_digest": body.preview_digest,
        })

        def run_response(run, *, replayed):
            after = run.after_snapshot or {}
            revision_number = after.get("scenario_revision")
            revision_row = db.query(ScenarioRevision).filter_by(
                scenario_id=scenario.id,
                revision=revision_number,
                status="ready",
            ).first() if revision_number is not None else None
            if revision_row is None:
                raise ScenarioError(
                    "action_request_incomplete",
                    "ActionRun 已完成但关联的 Scenario 修订记录不可读取。", 409)
            return {
                "case": next(item for item in _view(db, study)["cases"] if item["id"] == case.id),
                "revision": revision_row.revision,
                "changeset_id": revision_row.changeset_id,
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
                return run_response(existing_run, replayed=True)
            runtime_action_run = existing_run
            execution_claim_token = reclaimed_token

        prior = db.query(ScenarioChangeSet).filter_by(
            scenario_id=scenario.id,
            client_request_id=body.client_request_id,
        ).first()
        if prior:
            prior_action = prior.ordered_edits[0] if prior.ordered_edits else {}
            prior_profile = prior_action.get("parameters", {})
            if (prior_action.get("action_key") != body.action_key
                    or stable_hash(prior_profile) != stable_hash(body.parameters)):
                raise ScenarioError(
                    "idempotency_conflict", "操作请求编号已用于其他 Action 或参数", 409)
            prior_report = prior.validation_report or {}
            revision = db.get(ScenarioRevision, prior_report.get("revision_id"))
            if revision is None:
                raise ScenarioError(
                    "request_incomplete", "已记录的请求缺少可重放的情景修订", 409)
            return {
                "case": next(item for item in _view(db, study)["cases"] if item["id"] == case.id),
                "revision": revision.revision,
                "changeset_id": revision.changeset_id,
            }

        prepared = _compile_supplier_action_preview(
            db, user, ontology_id, study, case, scenario,
            action_key=body.action_key,
            parameters=body.parameters,
            client_request_id=body.client_request_id,
            expected_case_etag=body.etag,
        )
        action_request = prepared["action_request"]
        command = prepared["command"]
        permission_digest = prepared["permission_digest"]
        preview_digest = prepared["preview_digest"]
        study_context = prepared["study_context"]
        study_context_digest = prepared["study_context_digest"]
        if body.preview_digest != preview_digest:
            raise ScenarioError(
                "preview_stale", "操作内容、权限或运行上下文已变化，请重新预览后提交", 409)

        # Always lock Study -> Scenario -> case. The same order is reacquired
        # after the durable graph-build marker is committed.
        def lock_action_context():
            nonlocal study, scenario, case
            locked_study = db.query(ScenarioStudy).filter_by(
                id=study.id, ontology_id=ontology_id,
            ).populate_existing().with_for_update().first()
            if locked_study is None:
                raise ScenarioError("study_not_found", "未找到研究", 404)
            if _study_action_context(locked_study) != study_context:
                raise ScenarioError(
                    "study_changed", "研究范围、时间或模型配置已变化，请重新加载后提交", 409)
            study = locked_study
            locked_scenario = db.query(ScenarioResource).filter_by(
                id=scenario.id, ontology_id=ontology_id,
            ).populate_existing().with_for_update().first()
            if locked_scenario is None:
                raise ScenarioError("not_found", "Scenario not found", 404)
            service.get(locked_scenario.id, write=True)
            locked_case = db.query(ScenarioStudyCase).filter_by(
                id=case.id, study_id=study.id,
            ).populate_existing().with_for_update().first()
            if locked_case is None:
                raise ScenarioError("case_not_found", "未找到情景", 404)
            scenario, case = locked_scenario, locked_case

        lock_action_context()

        # Recompile under the locked Study/Scenario/Case context so a policy,
        # case, or source change while waiting for locks cannot use an older
        # preview digest to claim the ActionRun key.
        locked_prepared = _compile_supplier_action_preview(
            db, user, ontology_id, study, case, scenario,
            action_key=body.action_key,
            parameters=body.parameters,
            client_request_id=body.client_request_id,
            expected_case_etag=body.etag,
        )
        if body.preview_digest != locked_prepared["preview_digest"]:
            raise ScenarioError(
                "preview_stale", "操作内容、权限或运行上下文已变化，请重新预览后提交", 409)
        action_request = locked_prepared["action_request"]
        command = locked_prepared["command"]
        permission_digest = locked_prepared["permission_digest"]
        preview_digest = locked_prepared["preview_digest"]
        study_context = locked_prepared["study_context"]
        study_context_digest = locked_prepared["study_context_digest"]

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
                    return run_response(existing_run, replayed=True)
                runtime_action_run = existing_run
                execution_claim_token = reclaimed_token
                # The new claim was committed while the lock set was held;
                # reacquire Study -> Scenario -> case before recompiling.
                lock_action_context()
                locked_prepared = _compile_supplier_action_preview(
                    db, user, ontology_id, study, case, scenario,
                    action_key=body.action_key,
                    parameters=body.parameters,
                    client_request_id=body.client_request_id,
                    expected_case_etag=body.etag,
                )
                if body.preview_digest != locked_prepared["preview_digest"]:
                    raise ScenarioError(
                        "preview_stale", "操作内容、权限或运行上下文已变化，请重新预览后提交", 409)
                action_request = locked_prepared["action_request"]
                command = locked_prepared["command"]
                permission_digest = locked_prepared["permission_digest"]
                preview_digest = locked_prepared["preview_digest"]
                study_context = locked_prepared["study_context"]
                study_context_digest = locked_prepared["study_context_digest"]

        prior = db.query(ScenarioChangeSet).filter_by(
            scenario_id=scenario.id,
            client_request_id=body.client_request_id,
        ).first()
        if prior:
            prior_action = prior.ordered_edits[0] if prior.ordered_edits else {}
            if (prior_action.get("action_key") != body.action_key
                    or stable_hash(prior_action.get("parameters", {})) != stable_hash(body.parameters)):
                raise ScenarioError(
                    "idempotency_conflict", "操作请求编号已用于其他 Action 或参数", 409)
            prior_report = prior.validation_report or {}
            revision = db.get(ScenarioRevision, prior_report.get("revision_id"))
            if revision is None:
                raise ScenarioError(
                    "request_incomplete", "已记录的请求缺少可重放的情景修订", 409)
            return {
                "case": next(item for item in _view(db, study)["cases"] if item["id"] == case.id),
                "revision": revision.revision,
                "changeset_id": revision.changeset_id,
            }
        if body.etag != case.etag:
            raise ScenarioError("etag_conflict", "情景已变化，请刷新后再提交", 409)
        if command.expected_etag != scenario.etag:
            raise ScenarioError("scenario_changed", "情景本体已变化，请重新加载后提交", 409)

        command = command.model_copy(update={"permission_digest": permission_digest})
        runtime_manifest = {
            "action_input_hash": stable_hash({
                "action_key": body.action_key,
                "parameters": body.parameters,
            }),
            "action_request_digest": request_digest,
            "expected_etag": body.etag,
            "scenario_etag": command.expected_etag,
            "study_etag": study_context["study_etag"],
            "study_context_digest": study_context_digest,
            "permission_digest": permission_digest,
            "preview_digest": preview_digest,
            "preview_mode": "compiled_preview",
            "confirmation_mode": "human_confirmed",
            "approval_mode": "not_required",
            "typed_action_request": action_request.manifest(),
        }
        revision_state = {"study": study, "case": case}
        expected_case_definition_revision = case.definition_revision

        def before_scenario_relock():
            locked_study = db.query(ScenarioStudy).filter_by(
                id=study.id, ontology_id=ontology_id,
            ).populate_existing().with_for_update().first()
            if locked_study is None:
                raise ScenarioError("study_not_found", "未找到研究", 404)
            if _study_action_context(locked_study) != study_context:
                raise ScenarioError(
                    "study_changed", "研究范围、时间或模型配置已变化，请重新加载后提交", 409)
            revision_state["study"] = locked_study

        def after_scenario_relock(locked_scenario):
            locked_case = db.query(ScenarioStudyCase).filter_by(
                id=case.id, study_id=revision_state["study"].id,
            ).populate_existing().with_for_update().first()
            if locked_case is None:
                raise ScenarioError("case_not_found", "未找到情景", 404)
            if (
                locked_case.etag != body.etag
                or locked_case.definition_revision != expected_case_definition_revision
            ):
                raise ScenarioError("etag_conflict", "情景已变化，请刷新后再提交", 409)
            rechecked = _compile_supplier_action_preview(
                db, user, ontology_id, revision_state["study"], locked_case,
                locked_scenario,
                action_key=body.action_key,
                parameters=body.parameters,
                client_request_id=body.client_request_id,
                expected_case_etag=body.etag,
            )
            if rechecked["preview_digest"] != preview_digest:
                raise ScenarioError(
                    "preview_stale", "操作内容、权限或运行上下文已变化，请重新预览后提交", 409)
            revision_state["case"] = locked_case

        def before_sql_commit():
            finalized_case = revision_state["case"]
            finalized_case.submitted_parameters = body.parameters
            finalized_case.parameters_hash = stable_hash(body.parameters)
            finalized_case.definition_revision += 1
            finalized_case.etag += 1

        if runtime_action_run is None:
            runtime_action_run = create_action_run_record(
                db=db,
                # The wood-specific compiler is server-owned and has no
                # OntologyActionType row; retain its identity in the manifest.
                action_type_id=None,
                ontology_id=ontology_id,
                parameters={"invocations": action_request.compiler_payloads()},
                execution_context={
                    "scenario_id": scenario.id,
                    "study_id": study.id,
                    "case_id": case.id,
                    "source": "supplier_study",
                    "action_request": action_request.manifest(),
                    "preview_mode": "compiled_preview",
                    "confirmation_mode": "human_confirmed",
                    "approval_mode": "not_required",
                },
                initial_status="running",
                requester_id=str(user.id),
                executor_id=str(user.id),
                client_request_id=body.client_request_id,
                idempotency_scope=action_scope,
                input_digest=request_digest,
                preview_digest=preview_digest,
                permission_digest=permission_digest,
                expected_etag=str(body.etag),
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
        revision = service.revision(
            scenario,
            command,
            runtime_manifest=runtime_manifest,
            runtime_action_run=runtime_action_run,
            before_scenario_relock=before_scenario_relock,
            after_scenario_relock=after_scenario_relock,
            before_sql_commit=before_sql_commit,
        )
        study, case = revision_state["study"], revision_state["case"]
    except ScenarioError as exc:
        db.rollback()
        fail_current_claim(exc.code)
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc
    except ValueError as exc:
        db.rollback()
        fail_current_claim("validation_failed")
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        db.rollback()
        fail_current_claim("action_submission_failed")
        raise
    return {"case": next(item for item in _view(db, study)["cases"] if item["id"] == case.id),
            "revision": revision.revision, "changeset_id": revision.changeset_id,
            "action_run_id": runtime_action_run.id,
            "action_run_status": runtime_action_run.status,
            "replayed": False}


def _enqueue(db, user, study, case, compatibility, dependency=None, request_id=None, retry_of=None):
    scenario = db.get(ScenarioResource, case.scenario_id)
    profile = case.submitted_parameters
    frozen = {"profile": profile, "scenario_time": study.scenario_time,
              "scope": study.scope_definition, "smoothing_minutes": study.smoothing_minutes,
              "definition_revision": case.definition_revision,
              "manifest_hash": study.source_manifest_digest,
              "model_config_alias": study.model_config_alias,
              "parameter_projection": study.parameter_projection}
    run = ScenarioRun(scenario_id=scenario.id, study_id=study.id, case_id=case.id,
                      revision=scenario.head_revision, case_key=case.case_key,
                      logic_asset_key=study.model_key, logic_version=study.model_version,
                      compatibility_hash=compatibility, depends_on_run_id=dependency,
                      client_request_id=request_id or str(uuid4()),
                      case_definition_revision=case.definition_revision, retry_of_run_id=retry_of,
                      parameters=frozen, input_digest=stable_hash(frozen), created_by=user.id,
                      status="queued", current_stage="queued")
    db.add(run); db.flush()
    db.add_all([ScenarioRunStage(run_id=run.id, sequence=i + 1, stage_key=key) for i, key in enumerate(STAGES)])
    db.commit()
    try:
        task = run_scenario.apply_async(args=(run.id,), countdown=1 if dependency else 0)
        run.celery_task_id = task.id
        db.commit()
    except Exception as exc:
        run.status = "failed"; run.current_stage = "dispatch"
        run.error_json = {"code": "celery_dispatch_failed", "message": str(exc)}
        db.commit()
    return run


@router.post("/{ontology_id}/scenario-studies/{study_id}/cases/{case_id}/runs", status_code=status.HTTP_202_ACCEPTED)
def start_run(ontology_id: str, study_id: str, case_id: str, body: RunCommand,
              db=Depends(get_db), user=Depends(get_current_user)):
    _require_editor(user)
    study = _study(db, user, ontology_id, study_id, write=True)
    case = _case(db, study, case_id)
    if case.etag != body.etag:
        raise HTTPException(409, "情景已变化，请刷新后再运行")
    if case.case_kind != "baseline" and case.definition_revision == 0:
        raise HTTPException(409, "请先提交候选操作再运行")
    view = db.get(QueryDataView, study.base_view_id)
    if not view or view.status != "ready":
        raise HTTPException(409, "基础视图不可用")
    compatibility = _compatibility(study, view)
    existing_request = db.query(ScenarioRun).filter_by(case_id=case.id, client_request_id=body.client_request_id).first()
    if existing_request:
        if (existing_request.case_definition_revision != case.definition_revision
                or existing_request.compatibility_hash != compatibility):
            raise HTTPException(409, "运行请求编号已用于其他输入")
        return _run_json(db, existing_request)
    dependency = None
    if case.case_kind != "baseline":
        baseline = db.query(ScenarioStudyCase).filter_by(study_id=study.id, case_key="baseline").one()
        existing = db.query(ScenarioRun).filter_by(case_id=baseline.id, compatibility_hash=compatibility,
                                                       status="completed").order_by(ScenarioRun.created_at.desc()).first()
        if existing and not db.query(ScenarioRunArtifact).filter_by(
                run_id=existing.id, artifact_type="demand_impacts").first():
            # A completed Run from before evidence persistence is not a valid
            # dependency for the current contract; preserve it for audit and
            # create a fresh attempt with complete artifacts.
            existing = None
        if not existing:
            if not study.run_baseline_sim:
                raise HTTPException(409, "需要兼容的基线运行")
            queued = db.query(ScenarioRun).filter_by(case_id=baseline.id, compatibility_hash=compatibility).filter(
                ScenarioRun.status.in_(["queued", "running"])).order_by(ScenarioRun.created_at.desc()).first()
            existing = queued or _enqueue(db, user, study, baseline, compatibility)
        dependency = existing.id
    run = _enqueue(db, user, study, case, compatibility, dependency, request_id=body.client_request_id)
    return _run_json(db, run)


@router.get("/{ontology_id}/scenario-runs/{run_id}")
def get_run(ontology_id: str, run_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    run = db.get(ScenarioRun, run_id)
    if not run or not run.study_id:
        raise HTTPException(404, "未找到运行")
    _study(db, user, ontology_id, run.study_id)
    return _run_json(db, run)


@router.post("/{ontology_id}/scenario-runs/{run_id}:cancel")
def cancel_run(ontology_id: str, run_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    run = db.get(ScenarioRun, run_id)
    if not run or not run.study_id:
        raise HTTPException(404, "未找到运行")
    _require_editor(user)
    _study(db, user, ontology_id, run.study_id, write=True)
    if run.status in ("queued", "running"):
        run.cancel_requested = True
        db.commit()
    return _run_json(db, run)


@router.post("/{ontology_id}/scenario-runs/{run_id}:retry", status_code=status.HTTP_202_ACCEPTED)
def retry_run(ontology_id: str, run_id: str, body: RetryCommand, db=Depends(get_db), user=Depends(get_current_user)):
    previous = db.get(ScenarioRun, run_id)
    if not previous or not previous.study_id:
        raise HTTPException(404, "未找到运行")
    _require_editor(user)
    study = _study(db, user, ontology_id, previous.study_id, write=True)
    if previous.status not in {"failed", "cancelled"}:
        raise HTTPException(409, "只有失败或已取消的运行可以重试")
    case = _case(db, study, previous.case_id)
    result = start_run(ontology_id, study.id, case.id,
                       RunCommand(etag=case.etag, client_request_id=body.client_request_id), db, user)
    retry = db.get(ScenarioRun, result["id"])
    if retry.id != previous.id and retry.retry_of_run_id is None:
        retry.retry_of_run_id = previous.id
        db.commit()
    return _run_json(db, retry)


@router.get("/{ontology_id}/scenario-studies/{study_id}/compare")
def compare(ontology_id: str, study_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    study = _study(db, user, ontology_id, study_id)
    view = db.get(QueryDataView, study.base_view_id)
    compatibility = _compatibility(study, view)
    cases = [case for case in db.query(ScenarioStudyCase).filter_by(study_id=study.id).order_by(ScenarioStudyCase.sort_order).all()
             if db.get(ScenarioResource, case.scenario_id).status == "active"]
    columns = []
    for case in cases:
        run, selection_reason = _latest_compatible_run(db, case, compatibility)
        context = _context_ref(db, study, case, compatibility)
        selected = run if selection_reason == "current_success" else None
        columns.append({"case_key": case.case_key, "name": case.display_name,
                        "run_id": selected.id if selected else None,
                        "context_token": context["context_token"],
                        "selection_reason": selection_reason,
                        "result_view_id": selected.result_view_id if selected else None,
                        "metrics": selected.result_manifest.get("metrics", []) if selected and selected.result_manifest else [],
                        "duration_ms": selected.duration_ms if selected else None})
    baseline = {m["key"]: m["value"] for m in columns[0]["metrics"]} if columns else {}
    for col in columns:
        col["deltas"] = {m["key"]: round(m["value"] - baseline[m["key"]], 4)
                         for m in col["metrics"] if m["key"] in baseline}
    compatible = all(col["run_id"] for col in columns)
    preference = {"on_time_delivery_rate": "high", "late_demand_count": "low",
                  "shortage_quantity": "low", "estimated_procurement_cost": "low",
                  "weighted_lead_time_days": "low", "affected_customer_count": "low"}
    rankings = {}
    if compatible:
        for key, direction in preference.items():
            entries = [(col["case_key"], next((metric for metric in col["metrics"] if metric["key"] == key), None))
                       for col in columns]
            if any(metric is None or metric["completeness"] != "exact" for _, metric in entries):
                continue
            ordered = sorted(entries, key=lambda pair: (pair[1]["value"] * (-1 if direction == "high" else 1), pair[0]))
            groups = []
            for case_key, metric in ordered:
                if not groups or metric["value"] != groups[-1][0][1]:
                    groups.append([])
                groups[-1].append((case_key, metric["value"]))
            rankings[key] = [[case_key for case_key, _ in group] for group in groups]
    return {"compatible": compatible, "compatibility_hash": compatibility,
            "metric_contract": "supplier-metrics-v1", "rankings": rankings,
            "ranking_available": compatible and len(rankings) == len(preference), "columns": columns}
