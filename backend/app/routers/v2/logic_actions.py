"""PRD v1.1 Ontology Logic & Actions API"""
from __future__ import annotations
import uuid
from types import SimpleNamespace
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timezone
from app.deps import get_current_user, get_db, require_admin, require_editor
from app.models.user import User
from app.models.v2.logic import OntologyLogicRule, OntologyStateMachine
from app.models.v2.action import OntologyActionType, OntologyActionRun
from app.models.v2.decision_case import DecisionCase
from app.schemas.v2.object_query import ExecutionContext
from app.services.v2.action_context import ActionContextError, action_context_error, resolve_action_context
from app.services.v2.scenario_actions import compile_actions, validate_definition
from app.services.v2.decision_cases import CaseError, DecisionCaseService, digest
from app.services.v2.scenarios import ScenarioError, ScenarioService
from app.models.v2.query_view import QueryDataView
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.object_query.core import FalkorReadAdapter
from app.schemas.v2.scenario import Edit, RevisionRequest, Target

router = APIRouter(dependencies=[Depends(get_current_user)])

def require_action_ontology(ontology_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """HTTP gate; direct legacy test calls never bypass a real API request."""
    from app.models.ontology import OntologyProject
    if not db.get(OntologyProject, ontology_id):
        raise HTTPException(404, "Ontology not found")
    try:
        ScenarioService(db, ontology_id, current_user).authorize_ontology()
    except ScenarioError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "path": exc.path, "message": exc.message, "details": {}}) from exc


router.dependencies.append(Depends(require_action_ontology))


# ── Logic Rules ─────────────────────────────────────────────────

class LogicRuleCreate(BaseModel):
    name: str
    logic_type: str
    description: str = ""
    target_entity_type: Optional[str] = None
    expression: dict = {}
    source_type: Optional[str] = None
    severity: str = "info"
    enabled: bool = True


class LogicReviewRequest(BaseModel):
    enabled: Optional[bool] = None
    status: Optional[str] = None
    severity: Optional[str] = None
    notes: str = ""


class LogicTestRequest(BaseModel):
    row: dict = {}
    parameters: dict = {}


@router.get("/{ontology_id}/logic")
def list_logic_rules(ontology_id: str, logic_type: str = "", db: Session = Depends(get_db)):
    q = db.query(OntologyLogicRule).filter(OntologyLogicRule.ontology_id == ontology_id)
    if logic_type:
        q = q.filter(OntologyLogicRule.logic_type == logic_type)
    rules = q.order_by(OntologyLogicRule.created_at.desc()).all()
    return [{"id": r.id, "name": r.name, "logic_type": r.logic_type, "description": r.description,
             "target_entity_type": r.target_entity_type, "severity": r.severity, "enabled": r.enabled,
             "status": r.status, "version": r.version, "expression": r.expression,
             "source_type": r.source_type, "source_ref": r.source_ref,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rules]


@router.post("/{ontology_id}/logic", status_code=201, dependencies=[Depends(require_editor)])
def create_logic_rule(ontology_id: str, body: LogicRuleCreate, db: Session = Depends(get_db)):
    rule = OntologyLogicRule(
        ontology_id=ontology_id, name=body.name, logic_type=body.logic_type,
        description=body.description, target_entity_type=body.target_entity_type,
        expression=body.expression, source_type=body.source_type,
        severity=body.severity, enabled=body.enabled,
    )
    db.add(rule); db.commit(); db.refresh(rule)
    return {"id": rule.id, "name": rule.name, "status": rule.status}


@router.put("/{ontology_id}/logic/{rule_id}", dependencies=[Depends(require_editor)])
def update_logic_rule(ontology_id: str, rule_id: str, body: LogicRuleCreate, db: Session = Depends(get_db)):
    rule = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.id == rule_id, OntologyLogicRule.ontology_id == ontology_id
    ).first()
    if not rule:
        raise HTTPException(404, "Logic rule not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(rule, k, v)
    rule.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": rule.id, "status": "updated"}


@router.post("/{ontology_id}/logic/{rule_id}/review", dependencies=[Depends(require_admin)])
def review_logic_rule(ontology_id: str, rule_id: str, body: LogicReviewRequest, db: Session = Depends(get_db)):
    rule = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.id == rule_id, OntologyLogicRule.ontology_id == ontology_id
    ).first()
    if not rule:
        raise HTTPException(404, "Logic rule not found")
    if body.enabled is not None:
        rule.enabled = body.enabled
    if body.severity is not None:
        rule.severity = body.severity
    if body.status is not None:
        if body.status not in ("draft", "reviewed", "disabled", "published"):
            raise HTTPException(400, "Invalid logic status")
        rule.status = body.status
    if body.notes:
        ref = dict(rule.source_ref or {})
        ref["review_notes"] = body.notes
        ref["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        rule.source_ref = ref
    rule.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": rule.id, "enabled": rule.enabled, "status": rule.status, "severity": rule.severity}


@router.post("/{ontology_id}/logic/{rule_id}/test")
def test_logic_rule(ontology_id: str, rule_id: str, body: LogicTestRequest, db: Session = Depends(get_db)):
    rule = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.id == rule_id, OntologyLogicRule.ontology_id == ontology_id
    ).first()
    if not rule:
        raise HTTPException(404, "Logic rule not found")
    return {"rule_id": rule.id, **_evaluate_logic_rule(rule, body.row or {}, body.parameters or {})}


@router.delete("/{ontology_id}/logic/{rule_id}", dependencies=[Depends(require_admin)])
def delete_logic_rule(ontology_id: str, rule_id: str, db: Session = Depends(get_db)):
    rule = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.id == rule_id, OntologyLogicRule.ontology_id == ontology_id
    ).first()
    if not rule:
        raise HTTPException(404, "Logic rule not found")
    db.delete(rule); db.commit()
    return {"status": "deleted"}


@router.post("/{ontology_id}/logic/publish", dependencies=[Depends(require_admin)])
def publish_logic_rules_v2(ontology_id: str, db: Session = Depends(get_db)):
    from app.models.logic import LogicRule as LogicRuleV1

    rules = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.ontology_id == ontology_id,
        OntologyLogicRule.status != "published",
        OntologyLogicRule.enabled == True,  # noqa: E712
    ).all()
    for rule in rules:
        rule.status = "published"
        rule.updated_at = datetime.now(timezone.utc)

    v1_rules = db.query(LogicRuleV1).filter(
        LogicRuleV1.ontology_id == ontology_id,
        LogicRuleV1.status != "published",
    ).all()
    for rule in v1_rules:
        rule.status = "published"
    db.commit()
    return {"published_v2": len(rules), "published_v1": len(v1_rules), "status": "published"}


# ── Logic: Discovery ────────────────────────────────────────────

@router.post("/{ontology_id}/logic/discover", dependencies=[Depends(require_editor)])
def discover_logic_rules(ontology_id: str, db: Session = Depends(get_db)):
    """发现 Logic Rules（同步写入 v2 + v1 表，供前端 LogicTab 读取）"""
    from app.services.v2.mapping.mapping_service import MappingService
    from app.models.logic import LogicRule as LogicRuleV1
    import uuid
    svc = MappingService(db)
    mappings = svc.get_mappings(ontology_id)
    created = []
    for m in mappings:
        name = f"Mapping: {m.entity_class}"
        exists = db.query(OntologyLogicRule).filter(
            OntologyLogicRule.ontology_id == ontology_id,
            OntologyLogicRule.name == name,
        ).first()
        if not exists:
            db.add(OntologyLogicRule(
                ontology_id=ontology_id, name=name, logic_type="mapping",
                description=f"Entity Mapping: {m.entity_class}",
                target_entity_type=m.entity_class,
                expression={"field_mapping": m.field_mapping},
                source_type="mapping", severity="info",
            ))
            # v1 表（前端 LogicTab 读取）
            if not db.query(LogicRuleV1).filter(
                LogicRuleV1.ontology_id == ontology_id, LogicRuleV1.name_cn == name,
            ).first():
                db.add(LogicRuleV1(
                    id=str(uuid.uuid4()), ontology_id=ontology_id,
                    name_cn=name, name_en=name,
                    description=f"Entity Mapping: {m.entity_class}",
                    formula=f"mapping:{m.entity_class}", confidence=0.85,
                    enabled=True, status="draft",
                    linked_entities=[m.entity_class],
                ))
            created.append(name)
    # PRD ④: 额外发现 Quality/Validation/State/Security/Automation 规则
    quality_rules = [
        ("Validation: 数据完整性检查", "validation", "完整性: 检查必填字段和 null 率", 0.8),
        ("Quality: 数据质量监控", "validation", "质量: 监控重复率和异常值", 0.75),
        ("Business: 业务规则推导", "business", "业务: 基于领域知识的业务规则", 0.7),
        ("Inference: 图推导关系", "inference", "推导: 基于实体关系推导隐含关联", 0.7),
        ("State: 状态流转规则", "state", "状态: 数据状态流转和审核流程", 0.75),
        ("Security: 访问控制", "security", "安全: 基于角色限制读写权限", 0.85),
        ("Automation: 自动同步触发", "automation", "自动化: Curated 审批通过后触发增量更新", 0.9),
    ]
    for qname, qtype, qdesc, qconf in quality_rules:
        if not db.query(LogicRuleV1).filter(
            LogicRuleV1.ontology_id == ontology_id, LogicRuleV1.name_cn == qname,
        ).first():
            db.add(LogicRuleV1(
                id=str(uuid.uuid4()), ontology_id=ontology_id,
                name_cn=qname, name_en=qname.replace(": ", "_"),
                description=qdesc, formula=qtype, confidence=qconf,
                enabled=True, status="draft",
            ))
    db.commit()
    total_v1 = db.query(LogicRuleV1).filter(LogicRuleV1.ontology_id == ontology_id).count()
    total_v2 = db.query(OntologyLogicRule).filter(OntologyLogicRule.ontology_id == ontology_id).count()
    return {"discovered": len(created), "total_v2": total_v2, "total_v1": total_v1}


# ── State Machines ──────────────────────────────────────────────

@router.get("/{ontology_id}/state-machines")
def list_state_machines(ontology_id: str, db: Session = Depends(get_db)):
    machines = db.query(OntologyStateMachine).filter(
        OntologyStateMachine.ontology_id == ontology_id
    ).all()
    return [{"id": m.id, "entity_type_name": m.entity_type_name, "state_property": m.state_property,
             "states": m.states, "transitions": m.transitions} for m in machines]


# ── Action Types ────────────────────────────────────────────────

class ActionTypeCreate(BaseModel):
    name: str
    action_category: str
    description: str = ""
    target_entity_type: Optional[str] = None
    parameters: list = []
    submission_criteria: Optional[list] = None
    effects: list = []
    side_effects: Optional[list] = None
    permission_rules: Optional[list] = None
    enabled: bool = True


class ActionRunRequest(BaseModel):
    target_object_id: Optional[str] = None
    parameters: dict = {}
    context: Optional[ExecutionContext] = None
    expected_etag: Optional[int | str] = None
    client_request_id: Optional[str] = None
    evidence_refs: list[str | dict] = Field(default_factory=list)
    reason: str = ""
    case_context: dict = Field(default_factory=dict)


class ActionReviewRequest(BaseModel):
    enabled: Optional[bool] = None
    status: Optional[str] = None
    permission_rules: Optional[list] = None
    submission_criteria: Optional[list] = None
    notes: str = ""


@router.get("/{ontology_id}/actions")
def list_action_types(ontology_id: str, category: str = "", db: Session = Depends(get_db)):
    q = db.query(OntologyActionType).filter(OntologyActionType.ontology_id == ontology_id)
    if category:
        q = q.filter(OntologyActionType.action_category == category)
    actions = q.order_by(OntologyActionType.created_at.desc()).all()
    return [{"id": a.id, "name": a.name, "action_category": a.action_category,
             "description": a.description, "target_entity_type": a.target_entity_type,
             "enabled": a.enabled, "status": a.status, "version": a.version,
             "parameters": a.parameters, "submission_criteria": a.submission_criteria,
             "effects": a.effects, "side_effects": a.side_effects,
             "permission_rules": a.permission_rules, "backed_by_function": a.backed_by_function,
             "created_at": a.created_at.isoformat() if a.created_at else None} for a in actions]


@router.post("/{ontology_id}/actions", status_code=201, dependencies=[Depends(require_editor)])
def create_action_type(ontology_id: str, body: ActionTypeCreate, db: Session = Depends(get_db)):
    act = OntologyActionType(
        ontology_id=ontology_id, name=body.name, action_category=body.action_category,
        description=body.description, target_entity_type=body.target_entity_type,
        parameters=body.parameters, submission_criteria=body.submission_criteria,
        effects=body.effects, side_effects=body.side_effects,
        permission_rules=body.permission_rules, enabled=body.enabled,
    )
    db.add(act); db.commit(); db.refresh(act)
    return {"id": act.id, "name": act.name, "status": act.status}


@router.post("/{ontology_id}/actions/{action_id}/review", dependencies=[Depends(require_admin)])
def review_action_type(ontology_id: str, action_id: str, body: ActionReviewRequest, db: Session = Depends(get_db)):
    act = db.query(OntologyActionType).filter(
        OntologyActionType.id == action_id, OntologyActionType.ontology_id == ontology_id
    ).first()
    if not act:
        raise HTTPException(404, "Action type not found")
    if body.enabled is not None:
        act.enabled = body.enabled
    if body.status is not None:
        if body.status not in ("draft", "reviewed", "disabled", "published"):
            raise HTTPException(400, "Invalid action status")
        if body.status == "published" and not act.backed_by_function:
            try:
                validate_definition(act.parameters or [], act.effects or [])
            except ScenarioError as exc:
                raise HTTPException(422, {"code": exc.code, "message": exc.message}) from exc
        act.status = body.status
    if body.permission_rules is not None:
        act.permission_rules = body.permission_rules
    if body.submission_criteria is not None:
        act.submission_criteria = body.submission_criteria
    if body.notes:
        side_effects = list(act.side_effects or [])
        side_effects.append({
            "type": "review_note",
            "notes": body.notes,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        })
        act.side_effects = side_effects
    act.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": act.id, "enabled": act.enabled, "status": act.status}


@router.post("/{ontology_id}/actions/discover", dependencies=[Depends(require_editor)])
def discover_actions(ontology_id: str, db: Session = Depends(get_db)):
    """发现 Actions（同步写入 v2 + v1 表，供前端 ActionsTab 读取）"""
    from app.services.v2.mapping.mapping_service import MappingService
    from app.models.action import Action as ActionV1
    import uuid
    svc = MappingService(db)
    mappings = svc.get_mappings(ontology_id)
    created = []
    for m in mappings:
        name = f"Create {m.entity_class}"
        if not db.query(OntologyActionType).filter(
            OntologyActionType.ontology_id == ontology_id,
            OntologyActionType.name == name,
        ).first():
            db.add(OntologyActionType(
                ontology_id=ontology_id, name=name, action_category="crud",
                description=f"创建 {m.entity_class} 实体",
                target_entity_type=m.entity_class,
                parameters=[{"name": "data", "type": "object", "required": True}],
                effects=[{"action": "create_node", "entity_type": m.entity_class}],
            ))
            if not db.query(ActionV1).filter(
                ActionV1.ontology_id == ontology_id, ActionV1.name_cn == name,
            ).first():
                db.add(ActionV1(
                    id=str(uuid.uuid4()), ontology_id=ontology_id,
                    name_cn=name, name_en=name,
                    description=f"创建 {m.entity_class}", confidence=0.85,
                    enabled=True, status="draft",
                    linked_entities=[m.entity_class],
                ))
            created.append(name)
    # PRD ⑤: Review/Repair 动作类型
    for aname, acat, adesc, aconf in [
        ("Link: 关系维护", "link", "维护实体间的关系链接", 0.8),
        ("State: 状态流转", "state_transition", "执行实体状态迁移操作", 0.8),
        ("Review: 人工审核确认", "review", "对 pending_review 数据进行人工审核", 0.85),
        ("Repair: 数据质量修复", "repair", "修复质量报告中标记的异常数据", 0.75),
        ("Writeback: 外部写回", "writeback", "将审核通过的数据写回外部系统", 0.7),
    ]:
        if not db.query(ActionV1).filter(
            ActionV1.ontology_id == ontology_id, ActionV1.name_cn == aname,
        ).first():
            db.add(ActionV1(
                id=str(uuid.uuid4()), ontology_id=ontology_id,
                name_cn=aname, name_en=aname.replace(": ","_"),
                description=adesc, confidence=aconf,
                enabled=True, status="draft",
            ))
    db.commit()
    tv1 = db.query(ActionV1).filter(ActionV1.ontology_id == ontology_id).count()
    tv2 = db.query(OntologyActionType).filter(OntologyActionType.ontology_id == ontology_id).count()
    return {"discovered": len(created), "total_v2": tv2, "total_v1": tv1}


@router.delete("/{ontology_id}/actions/{action_id}", dependencies=[Depends(require_admin)])
def delete_action_type(ontology_id: str, action_id: str, db: Session = Depends(get_db)):
    act = db.query(OntologyActionType).filter(
        OntologyActionType.id == action_id, OntologyActionType.ontology_id == ontology_id
    ).first()
    if not act:
        raise HTTPException(404, "Action type not found")
    db.delete(act); db.commit()
    return {"status": "deleted"}


@router.post("/{ontology_id}/actions/publish", dependencies=[Depends(require_admin)])
def publish_actions_v2(ontology_id: str, db: Session = Depends(get_db)):
    from app.models.action import Action as ActionV1

    actions = db.query(OntologyActionType).filter(
        OntologyActionType.ontology_id == ontology_id,
        OntologyActionType.status != "published",
        OntologyActionType.enabled == True,  # noqa: E712
    ).all()
    for action in actions:
        if not action.backed_by_function:
            try:
                validate_definition(action.parameters or [], action.effects or [])
            except ScenarioError as exc:
                raise HTTPException(422, {"code": exc.code, "action_type_id": action.id,
                                          "message": exc.message}) from exc
    for action in actions:
        action.status = "published"
        action.updated_at = datetime.now(timezone.utc)

    v1_actions = db.query(ActionV1).filter(
        ActionV1.ontology_id == ontology_id,
        ActionV1.status != "published",
    ).all()
    for action in v1_actions:
        action.status = "published"
    db.commit()
    return {"published_v2": len(actions), "published_v1": len(v1_actions), "status": "published"}


# ── Action Runs ─────────────────────────────────────────────────

@router.get("/{ontology_id}/action-runs", dependencies=[Depends(require_action_ontology)])
def list_action_runs(ontology_id: str, limit: int = 20, target_object_id: str | None = None,
                     db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.models.ontology import OntologyProject
    from app.services.v2.decision_cases import CaseError, DecisionCaseService
    query = db.query(OntologyActionRun).filter(
        OntologyActionRun.ontology_id == ontology_id
    )
    if target_object_id:
        query = query.filter(OntologyActionRun.target_object_id == target_object_id)
    runs = query.order_by(OntologyActionRun.started_at.desc()).limit(max(1, min(limit, 100))).all()
    service = DecisionCaseService(db, ontology_id, current_user) if db.get(OntologyProject, ontology_id) else None
    visible = []
    for run in runs:
        if current_user.role != "admin" and run.executed_by != current_user.id:
            continue
        if service and run.target_object_id:
            try:
                service._target(run.target_object_id)
            except CaseError:
                continue
        # The log lists operations, not raw values or context payloads. A
        # reader's current field policy must not be bypassed by old Run JSON.
        operations = [{"op": item.get("op")}
                      for item in (run.side_effect_results or []) if isinstance(item, dict)]
        visible.append({"id": run.id, "action_type_id": run.action_type_id, "status": run.status,
                        "target_object_id": run.target_object_id, "error_code": "action_failed" if run.error else None,
                        "side_effect_results": operations, "decision_case_id": run.decision_case_id,
                        "trace_id": run.trace_id, "projection_status": run.projection_status,
                        "started_at": run.started_at.isoformat() if run.started_at else None})
    return visible


@router.post("/{ontology_id}/action-runs/{run_id}:revert", dependencies=[Depends(require_action_ontology), Depends(require_editor)])
def revert_action_run(ontology_id: str, run_id: str, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    original = db.query(OntologyActionRun).filter_by(id=run_id, ontology_id=ontology_id).with_for_update().first()
    if not original:
        raise HTTPException(404, "Action run not found")
    if original.decision_case_id:
        from app.services.v2.decision_cases import CaseError, DecisionCaseService
        try:
            run = DecisionCaseService(db, ontology_id, current_user).revert(original)
            return {"run_id": run.id, "reverted_run_id": original.id, "status": run.status,
                    "projection_status": run.projection_status, "edits": len(run.side_effect_results or [])}
        except CaseError as exc:
            raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc
    if current_user.role != "admin" and original.executed_by != current_user.id:
        raise HTTPException(403, "Only the executor or an administrator can revert this Action")
    if original.status != "completed" or (original.execution_context or {}).get("scenario_id"):
        raise HTTPException(409, "Only a completed live Action can be reverted")
    effects = original.side_effect_results or []
    if not effects or any(item.get("op") not in {"set_property", "unset_property"} for item in effects):
        raise HTTPException(422, "Only property edits can be reverted automatically")
    graph = FalkorDBService()
    if not graph.available:
        raise HTTPException(503, "FalkorDB is unavailable")
    snapshot = FalkorReadAdapter(graph._graph(ontology_id)).read(ontology_id)
    edits = []
    for object_id, after in (original.after_snapshot or {}).items():
        before = (original.before_snapshot or {}).get(object_id)
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise HTTPException(409, "Action snapshots are incomplete")
        matches = [key for key in snapshot.objects if key[1] == object_id]
        if len(matches) != 1:
            raise HTTPException(409, "Action target is missing or ambiguous")
        kind = matches[0][0]
        for prop in sorted(set(before) | set(after)):
            if before.get(prop) == after.get(prop) and (prop in before) == (prop in after):
                continue
            edits.append(Edit(sequence=len(edits), op="set_property" if prop in before else "unset_property",
                              target=Target(ontology_id=ontology_id, concrete_type=kind, object_id=object_id),
                              property=prop, value=before.get(prop), expected_old_value=after.get(prop)))
    if not edits:
        raise HTTPException(409, "Action has no changed properties to revert")
    from app.services.v2.live_edits import apply_live_edits
    try:
        before_snapshot, after_snapshot, results, restore = apply_live_edits(db, ontology_id, edits, graph)
        reverted = OntologyActionRun(action_type_id=original.action_type_id, ontology_id=ontology_id,
                                     target_object_id=original.target_object_id, parameters={"revert_of": original.id},
                                     execution_context=original.execution_context, status="completed",
                                     before_snapshot=before_snapshot, after_snapshot=after_snapshot,
                                     side_effect_results=results, executed_by=current_user.id,
                                     completed_at=datetime.now(timezone.utc))
        original.status = "reverted"
        db.add(reverted)
        try:
            db.commit()
        except Exception:
            db.rollback()
            restore()
            raise
        return {"run_id": reverted.id, "reverted_run_id": original.id, "status": "completed", "edits": len(edits)}
    except ScenarioError as exc:
        db.rollback()
        raise HTTPException(exc.status, detail={"code": exc.code, "path": exc.path, "message": exc.message}) from exc


def _live_action_snapshot(db, ontology_id):
    from app.models.ontology import OntologyProject
    if db.get(OntologyProject, ontology_id):
        graph = FalkorDBService()
        if not graph.available:
            raise ScenarioError("graph_unavailable", "FalkorDB is unavailable", 503, "execution")
        return FalkorReadAdapter(graph._graph(ontology_id)).read(ontology_id)
    from app.models.entity import Entity
    from app.models.relation import Relation
    entities = db.query(Entity).filter(Entity.ontology_id == ontology_id).all()
    objects = {}
    for entity in entities:
        object_type = entity.type or entity.name_en or entity.name_cn or "Entity"
        objects[(object_type, entity.id)] = dict(entity.properties or {})
    edges = []
    for relation in db.query(Relation).filter(Relation.ontology_id == ontology_id).all():
        source = next((key for key in objects if key[1] == relation.source_entity), None)
        target = next((key for key in objects if key[1] == relation.target_entity), None)
        if source and target:
            edges.append((source, relation.type, target))
    return SimpleNamespace(objects=objects, edges=edges)


def _scenario_action_snapshot(db, context, current_user):
    view = db.get(QueryDataView, context.data_view_id) if context.data_view_id else None
    if not view or view.status != "ready":
        raise ScenarioError("view_unavailable", "Scenario data view is unavailable", 409, "context.data_view_id")
    graph = FalkorDBService()
    if not graph.available:
        raise ScenarioError("graph_unavailable", "FalkorDB is unavailable", 503, "execution")
    return FalkorReadAdapter(graph._graph(view.graph_key), graph_ontology_id=view.graph_key).read(view.ontology_id)


def _compile_action_request(ontology_id, action, body, resolved_context, db, current_user):
    from app.services.v2.function_binding import function_resolver
    if resolved_context.target == "scenario":
        snapshot = _scenario_action_snapshot(db, resolved_context.context, current_user)
        scenario_key = resolved_context.context.scenario_id or "scenario"
    else:
        snapshot = _live_action_snapshot(db, ontology_id)
        scenario_key = "live"
    invocation = {"action_type_id": action.id, "parameters": body.parameters or {}}
    if body.target_object_id:
        invocation["target_object_id"] = body.target_object_id
    id_factory = (lambda action_index, rule_index: str(uuid.uuid4())) if resolved_context.target == "live" else None
    return compile_actions(ontology_id, scenario_key, [invocation], {action.id: action}, snapshot,
                           actor=current_user, id_factory=id_factory, function_resolver=function_resolver(db, ontology_id))


def _apply_live_edits(db, ontology_id, action, edits):
    from app.models.entity import Entity
    from app.models.relation import Relation
    before_snapshot = {}
    after_snapshot = {}
    results = []
    for edit in edits:
        if edit.op == "invoke_action":
            continue
        if edit.op in {"set_property", "unset_property", "delete_object"}:
            entity = db.query(Entity).filter(Entity.id == edit.target.object_id, Entity.ontology_id == ontology_id).with_for_update().first()
            if not entity:
                raise ValueError(f"Target entity not found: {edit.target.object_id}")
            props = dict(entity.properties or {})
            before_snapshot.setdefault(entity.id, dict(props))
            if edit.op in {"set_property", "unset_property"} and "expected_old_value" in edit.model_fields_set and props.get(edit.property) != edit.expected_old_value:
                raise ScenarioError("edit_conflict", "Target property changed since the Action was prepared", 409, "edits")
            if edit.op == "set_property":
                props[edit.property] = edit.value
            elif edit.op == "unset_property":
                props.pop(edit.property, None)
            else:
                db.delete(entity)
                results.append({"op": edit.op, "target_id": entity.id})
                continue
            entity.properties = props
            entity.updated_at = datetime.now(timezone.utc)
            after_snapshot[entity.id] = props
            results.append({"op": edit.op, "target_id": entity.id, "property": edit.property})
        elif edit.op == "create_object":
            data = dict(edit.properties or {})
            entity_id = edit.target.object_id
            entity = Entity(id=entity_id, ontology_id=ontology_id,
                            name_cn=str(data.get("display_name") or data.get("name") or f"{edit.object_type} {entity_id[:8]}")[:200],
                            name_en=edit.object_type, type=edit.object_type, properties=data, confidence=1.0)
            db.add(entity)
            after_snapshot[entity_id] = data
            results.append({"op": edit.op, "entity_id": entity_id, "entity_type": edit.object_type})
        elif edit.op in {"add_link", "remove_link"}:
            link = edit.link
            if edit.op == "add_link":
                db.merge(Relation(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ontology_id}:{link.source.object_id}:{link.relation_type}:{link.target.object_id}:action")),
                                  ontology_id=ontology_id, source_entity=link.source.object_id, target_entity=link.target.object_id,
                                  type=link.relation_type, properties={"source": "action_runtime", "action_type_id": action.id}, confidence=1.0))
            else:
                db.query(Relation).filter(Relation.ontology_id == ontology_id,
                                          Relation.source_entity == link.source.object_id,
                                          Relation.target_entity == link.target.object_id,
                                          Relation.type == link.relation_type).delete()
            results.append({"op": edit.op, "relation_type": link.relation_type})
        else:
            raise ValueError(f"Unsupported compiled edit: {edit.op}")
    return before_snapshot, after_snapshot, results


def _reviewed_live_proposal(ontology_id: str, action_id: str, body: ActionRunRequest,
                            db: Session, current_user: User, resolved_context):
    """Adapt the legacy live route to the read-only DecisionCase preview core."""
    from app.services.v2.decision_cases import CaseError, DecisionCaseService

    service = DecisionCaseService(db, ontology_id, current_user)
    if not body.target_object_id:
        raise CaseError("target_required", "正式对象操作须指定目标对象", 422)
    target = service.target_state(body.target_object_id)
    action = service._action(action_id)
    if body.expected_etag is not None and (not isinstance(body.expected_etag, str)
                                           or len(body.expected_etag) != 64):
        raise CaseError("etag_format", "正式对象操作须使用 target-state 返回的 64 位 etag", 422)
    expected_etag = body.expected_etag or target["etag"]
    if expected_etag != target["etag"]:
        raise CaseError("etag_conflict", "目标对象已变化，请重新读取", 409)
    parameters = dict(body.parameters or {})
    if any(item.get("name") == "target" for item in (action.parameters or [])) and "target" not in parameters:
        parameters["target"] = {"object_type": target["object_type"], "object_id": target["object_id"]}
    if body.reason and "reason" in parameters and body.reason != parameters["reason"]:
        raise CaseError("reason_conflict", "事项理由与 Action 参数理由不一致", 422)
    reason = (body.reason or parameters.get("reason") or "通过旧 Action API 提交，等待人工审核")
    if not isinstance(reason, str) or not reason.strip():
        raise CaseError("reason_required", "须说明操作理由", 422)
    if any(item.get("name") == "reason" for item in (action.parameters or [])) and "reason" not in parameters:
        parameters["reason"] = reason
    kind = ("quality_review" if target["object_type"] == "InspectionSample"
            else "dynamic_event" if target["object_type"] == "DynamicIncident"
            else "maintenance_review" if target["object_type"] in {"Record", "Episode"}
            else "general")
    context = {**body.case_context, "source": "legacy_action_api",
               "execution_context": resolved_context.context.model_dump(mode="json")}
    proposed = SimpleNamespace(id="unpersisted", action_type_id=action.id,
        created_by=current_user.id, target_object_id=target["object_id"],
        target_object_type=target["object_type"], expected_etag=expected_etag,
        parameters=parameters, evidence_refs=body.evidence_refs,
        context_json=context, kind=kind)
    preview, preview_digest, _, _ = service._preview(proposed)
    return service, action, proposed, reason, preview, preview_digest


@router.post("/{ontology_id}/actions/{action_id}/preview", dependencies=[Depends(require_action_ontology)])
def preview_action_type(
    ontology_id: str,
    action_id: str,
    body: ActionRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        resolved_context = resolve_action_context(db, ontology_id, body.context, current_user)
        from app.models.ontology import OntologyProject
        if resolved_context.target == "live" and db.get(OntologyProject, ontology_id):
            _, _, _, _, preview, preview_digest = _reviewed_live_proposal(
                ontology_id, action_id, body, db, current_user, resolved_context)
            return {"valid": True, "target": "live", "requires_review": True,
                    "preview": preview, "preview_digest": preview_digest,
                    "edits": [{"op": item["op"], "target": preview["target"],
                               "property": item["property"], "value": item["after"]}
                              for item in preview["diff"]],
                    "edit_count": len(preview["diff"]),
                    "decision_case_path": f"/api/v2/ontologies/{ontology_id}/decision-cases"}
        if db.get(OntologyProject, ontology_id):
            ScenarioService(db, ontology_id, current_user).authorize_ontology()
        action = db.query(OntologyActionType).filter(OntologyActionType.id == action_id, OntologyActionType.ontology_id == ontology_id).first()
        if not action:
            raise ScenarioError("not_found", "Action type not found", 404, "action_id")
        if not action.enabled or action.status != "published":
            raise ScenarioError("action_type_unavailable", "Action type must be enabled and published", 422, "action_id")
        edits = _compile_action_request(ontology_id, action, body, resolved_context, db, current_user)
        return {"valid": True, "target": resolved_context.target, "execution_context": resolved_context.context.model_dump(mode="json"),
                "edits": [edit.model_dump(mode="json") for edit in edits], "edit_count": sum(edit.op != "invoke_action" for edit in edits)}
    except ActionContextError as exc:
        raise HTTPException(exc.status, detail=action_context_error(exc)) from exc
    except ScenarioError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "path": exc.path, "message": exc.message, "details": {}}) from exc
    except CaseError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc


@router.post("/{ontology_id}/actions/{action_id}/run", dependencies=[Depends(require_action_ontology), Depends(require_editor)])
def run_action_type(
    ontology_id: str,
    action_id: str,
    body: ActionRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        resolved_context = resolve_action_context(db, ontology_id, body.context, current_user)
        from app.models.ontology import OntologyProject
        if resolved_context.target == "live" and db.get(OntologyProject, ontology_id):
            if body.client_request_id:
                legacy_context = {**body.case_context, "source": "legacy_action_api",
                                  "execution_context": resolved_context.context.model_dump(mode="json")}
                existing = db.query(DecisionCase).filter_by(
                    ontology_id=ontology_id, created_by=current_user.id,
                    action_type_id=action_id, context_digest=digest(legacy_context),
                    client_request_id=body.client_request_id).first()
                if existing:
                    action_row = db.get(OntologyActionType, action_id)
                    parameters = dict(body.parameters or {})
                    if (action_row and any(item.get("name") == "target" for item in (action_row.parameters or []))
                            and "target" not in parameters):
                        parameters["target"] = {"object_type": existing.target_object_type,
                                                "object_id": existing.target_object_id}
                    if body.reason and "reason" in parameters and body.reason != parameters["reason"]:
                        raise CaseError("reason_conflict", "事项理由与 Action 参数理由不一致", 422)
                    reason = body.reason or parameters.get("reason") or "通过旧 Action API 提交，等待人工审核"
                    if (action_row and any(item.get("name") == "reason" for item in (action_row.parameters or []))
                            and "reason" not in parameters):
                        parameters["reason"] = reason
                    original_request = {"kind": existing.kind, "title": existing.title,
                        "action_type_id": action_id, "target_object_id": body.target_object_id,
                        "target_object_type": existing.target_object_type,
                        "parameters": parameters, "evidence_refs": body.evidence_refs,
                        "reason": reason, "expected_etag": existing.expected_etag,
                        "context": legacy_context}
                    if ((body.expected_etag is not None and body.expected_etag != existing.expected_etag)
                            or digest(original_request) != existing.request_digest):
                        raise CaseError("idempotency_conflict", "请求编号已用于其他操作内容", 409)
                    DecisionCaseService(db, ontology_id, current_user).get_case(existing.id)
                    return JSONResponse(status_code=200, content={
                        "case_id": existing.id, "status": existing.status,
                        "action_run_id": existing.action_run_id,
                        "requires_review": existing.status != "completed",
                        "preview_digest": existing.preview_digest,
                        "decision_case_path": f"/api/v2/ontologies/{ontology_id}/decision-cases/{existing.id}"})
            service, action, proposed, reason, _, _ = _reviewed_live_proposal(
                ontology_id, action_id, body, db, current_user, resolved_context)
            case = service.create(kind=proposed.kind, title=action.name,
                action_type_id=action.id, target_object_id=proposed.target_object_id,
                target_object_type=proposed.target_object_type, parameters=proposed.parameters,
                evidence_refs=proposed.evidence_refs, reason=reason,
                expected_etag=proposed.expected_etag,
                client_request_id=body.client_request_id or str(uuid.uuid4()),
                context=proposed.context_json)
            preview_result = service.preview(case.id)
            return JSONResponse(status_code=202, content={
                "case_id": case.id, "status": case.status,
                "action_run_id": case.action_run_id, "requires_review": case.status == "draft",
                "preview_digest": preview_result["preview_digest"],
                "decision_case_path": f"/api/v2/ontologies/{ontology_id}/decision-cases/{case.id}"})
        if db.get(OntologyProject, ontology_id):
            ScenarioService(db, ontology_id, current_user).authorize_ontology()
    except ActionContextError as exc:
        raise HTTPException(exc.status, detail=action_context_error(exc)) from exc
    except ScenarioError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "path": exc.path, "message": exc.message, "details": {}}) from exc
    except CaseError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc)}) from exc

    action = db.query(OntologyActionType).filter(
        OntologyActionType.id == action_id,
        OntologyActionType.ontology_id == ontology_id,
    ).first()
    if not action:
        raise HTTPException(404, "Action type not found")
    if not action.enabled:
        raise HTTPException(400, "Action type is disabled")
    if action.status != "published":
        raise HTTPException(400, "Action type must be published before runtime execution")

    run = OntologyActionRun(
        action_type_id=action.id,
        ontology_id=ontology_id,
        target_object_id=body.target_object_id,
        parameters=body.parameters or {},
        execution_context=resolved_context.context.model_dump(mode="json"),
        status="running",
        executed_by=getattr(current_user, "id", None),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    before_snapshot = {}
    after_snapshot = {}
    side_effect_results = []
    try:
        edits = _compile_action_request(ontology_id, action, body, resolved_context, db, current_user)
        if resolved_context.target == "scenario":
            scenario_service = ScenarioService(db, ontology_id, current_user)
            scenario = scenario_service.get(resolved_context.context.scenario_id, write=True)
            if body.expected_etag is None or not body.client_request_id:
                raise ScenarioError("scenario_write_contract_required", "Scenario Action writes require expected_etag and client_request_id", 422, "expected_etag")
            revision = scenario_service.revision(
                scenario,
                RevisionRequest(base_revision=scenario.head_revision, expected_etag=body.expected_etag,
                                client_request_id=body.client_request_id, edits=edits),
            )
            run.status = "completed"
            run.side_effect_results = [{"op": edit.op, "sequence": edit.sequence} for edit in edits if edit.op != "invoke_action"]
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return {"run_id": run.id, "status": run.status, "target": "scenario", "revision": revision.revision,
                    "execution_context": resolved_context.context.model_dump(mode="json"), "edits": [edit.model_dump(mode="json") for edit in edits]}
        from app.models.ontology import OntologyProject
        restore_graph = None
        if db.get(OntologyProject, ontology_id):
            from app.services.v2.live_edits import apply_live_edits
            before_snapshot, after_snapshot, side_effect_results, restore_graph = apply_live_edits(db, ontology_id, edits, FalkorDBService())
        else:
            # Compatibility for pre-project fixtures only; registered ontologies
            # must use EntityInstance and the canonical graph projection.
            before_snapshot, after_snapshot, side_effect_results = _apply_live_edits(db, ontology_id, action, edits)

        run.status = "completed"
        run.before_snapshot = before_snapshot
        run.after_snapshot = after_snapshot
        run.side_effect_results = side_effect_results
        run.completed_at = datetime.now(timezone.utc)
        try:
            db.commit()
        except Exception:
            if restore_graph:
                restore_graph()
            raise
        return {
            "run_id": run.id,
            "status": run.status,
            "execution_context": resolved_context.context.model_dump(mode="json"),
            "side_effect_results": side_effect_results,
        }
    except ScenarioError as e:
        db.rollback()
        db.add(run)
        run.status = "failed"
        run.error = e.message
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(e.status, {"run_id": run.id, "code": e.code, "path": e.path, "error": e.message}) from e
    except Exception:
        db.rollback()
        db.add(run)
        run.status = "failed"
        run.error = "Unexpected Action runtime failure"
        run.before_snapshot = before_snapshot or None
        run.after_snapshot = after_snapshot or None
        run.side_effect_results = side_effect_results or None
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(500, {"run_id": run.id, "error": run.error})


def _evaluate_logic_rule(rule: OntologyLogicRule, row: dict, parameters: dict) -> dict:
    expr = dict(rule.expression or {})
    op = str(expr.get("operator") or expr.get("op") or "").lower()
    field = expr.get("field") or expr.get("column") or expr.get("property")
    value = row.get(field) if field else None

    if not op and rule.logic_type in ("mapping", "inference", "automation"):
        return {"status": "skipped", "passed": None, "reason": f"{rule.logic_type} rule is declarative"}
    if not op and expr.get("missing_count") is not None:
        return {"status": "completed", "passed": int(expr.get("missing_count") or 0) == 0}
    if not op:
        return {"status": "skipped", "passed": None, "reason": "No executable operator configured"}

    expected = expr.get("value", parameters.get(str(field)) if field else None)
    try:
        if op in ("required", "non_empty"):
            passed = value not in (None, "")
        elif op in ("equals", "eq"):
            passed = str(value) == str(expected)
        elif op in ("not_equals", "ne"):
            passed = str(value) != str(expected)
        elif op in ("gt", "gte", "lt", "lte"):
            left = float(value)
            right = float(expected)
            passed = {
                "gt": left > right,
                "gte": left >= right,
                "lt": left < right,
                "lte": left <= right,
            }[op]
        elif op == "in":
            choices = expected if isinstance(expected, list) else []
            passed = value in choices or str(value) in {str(v) for v in choices}
        else:
            return {"status": "skipped", "passed": None, "reason": f"Unsupported operator: {op}"}
        return {"status": "completed", "passed": bool(passed), "operator": op, "field": field}
    except Exception as e:
        return {"status": "failed", "passed": False, "error": str(e), "operator": op, "field": field}


def _validate_action_submission(action: OntologyActionType, params: dict, target_object_id: str | None,
                                db: Session, ontology_id: str) -> list[dict]:
    from app.models.entity import Entity

    errors: list[dict] = []
    for item in action.parameters or []:
        if not isinstance(item, dict) or not item.get("required"):
            continue
        name = item.get("name")
        if name and params.get(str(name)) in (None, ""):
            errors.append({"type": "missing_parameter", "parameter": name})

    for criterion in action.submission_criteria or []:
        if not isinstance(criterion, dict):
            continue
        ctype = str(criterion.get("type") or "").lower()
        if not ctype and criterion.get("logic_type"):
            continue
        if ctype == "required_target" and not target_object_id and not params.get("target_id"):
            errors.append({"type": ctype, "message": "target_object_id is required"})
        elif ctype == "entity_exists":
            entity_id = target_object_id or params.get("target_id")
            if not entity_id or not db.query(Entity).filter(Entity.id == entity_id, Entity.ontology_id == ontology_id).first():
                errors.append({"type": ctype, "message": "target entity does not exist"})
        elif ctype == "field_equals":
            field = criterion.get("field")
            expected = criterion.get("value")
            entity_id = target_object_id or params.get("target_id")
            entity = db.query(Entity).filter(Entity.id == entity_id, Entity.ontology_id == ontology_id).first() if entity_id else None
            actual = (entity.properties or {}).get(field) if entity and field else None
            if str(actual) != str(expected):
                errors.append({"type": ctype, "field": field, "expected": expected, "actual": actual})
        elif ctype == "required_param":
            name = criterion.get("name")
            if name and params.get(str(name)) in (None, ""):
                errors.append({"type": ctype, "parameter": name})
    return errors
