"""Agno decision assistant endpoints."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user, require_editor
from app.models.ontology import OntologyProject
from app.models.user import User
from app.models.v2.agent import AgentRun
from app.services.v2.authorization_service import AuthorizationContext, resolve_projection
from app.services.v2.agent_decision import AgentToolbox, run_agno
from app.services.v2.agent_context import context_for

router = APIRouter(dependencies=[Depends(get_current_user)])


class AgentRunInput(BaseModel):
    task: str = Field(min_length=5, max_length=4000)
    object_id: str | None = None
    reasoning_run_id: str | None = None
    conclusion: str | None = None
    mode: Literal["deterministic", "agno"] = "deterministic"


class ContextDraftInput(BaseModel):
    task: str = Field(min_length=5, max_length=4000)


class ContextUpdateInput(BaseModel):
    object_ids: list[str] = Field(default_factory=list, max_length=50)
    reasoning_run_id: str | None = None
    conclusions: list[str] = Field(default_factory=list, max_length=50)


def scope(db: Session, oid: str, user):
    item = db.get(OntologyProject, oid)
    if not item or (user.role != "admin" and item.created_by != user.id):
        raise HTTPException(404, "本体不存在或无访问权限")
    projection = resolve_projection(db, AuthorizationContext(
        ontology_id=oid, principal_id=str(user.id), principal_role=user.role,
        metadata_revision_id=item.current_revision_id), records=[])
    if not projection.unrestricted:
        raise HTTPException(403, "当前对象或字段权限策略不支持旧版 Agent 读取；请使用受权对象查询")
    return item


def _authorization_pin(ontology: OntologyProject, projection) -> dict[str, str]:
    return {
        "ontology_revision_id": str(ontology.current_revision_id or ""),
        "permission_digest": str(projection.permission_digest or ""),
    }


def _pin_for_user(db: Session, oid: str, user_id: str) -> dict[str, str] | None:
    actor = db.get(User, user_id)
    if actor is None or not getattr(actor, "is_active", True):
        return None
    ontology = db.get(OntologyProject, oid)
    if not ontology or (actor.role != "admin" and str(ontology.created_by) != str(actor.id)):
        return None
    projection = resolve_projection(db, AuthorizationContext(
        ontology_id=oid, principal_id=str(actor.id), principal_role=actor.role,
        metadata_revision_id=ontology.current_revision_id), records=[])
    if not projection.unrestricted:
        return None
    return _authorization_pin(ontology, projection)


def _run_is_current(db: Session, oid: str, run: AgentRun) -> bool:
    payload = run.payload if isinstance(run.payload, dict) else {}
    recorded = payload.get("_authorization")
    if not isinstance(recorded, dict) or not run.created_by:
        return False
    current = _pin_for_user(db, oid, str(run.created_by))
    return current is not None and current == recorded


def _require_current_run(db: Session, oid: str, run: AgentRun) -> None:
    if not _run_is_current(db, oid, run):
        raise HTTPException(status_code=409, detail={
            "code": "agent_run_context_stale",
            "message": "该 Agent 运行缺少有效权限快照，或本体版本/权限已变化；请重新加载当前上下文并运行。",
        })


def _public_payload(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if not str(key).startswith("_")}


@router.post("/{oid}/agent/runs")
def create_agent_run(oid: str, body: AgentRunInput, db: Session = Depends(get_db), user=Depends(get_current_user)):
    ontology = scope(db, oid, user)
    projection = resolve_projection(db, AuthorizationContext(
        ontology_id=oid, principal_id=str(user.id), principal_role=user.role,
        metadata_revision_id=ontology.current_revision_id), records=[])
    authorization_pin = _authorization_pin(ontology, projection)
    toolbox = AgentToolbox(oid, db, user)
    try:
        proposal = (run_agno(toolbox, body.task, body.object_id, body.reasoning_run_id, body.conclusion)
                    if body.mode == "agno" else toolbox.propose(body.task, body.object_id, body.reasoning_run_id, body.conclusion))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if _pin_for_user(db, oid, str(user.id)) != authorization_pin:
        raise HTTPException(status_code=409, detail={"code": "agent_run_context_stale", "message": "生成期间本体版本或权限发生变化，请重新加载上下文。"})
    payload = {
        "task": body.task,
        "mode": body.mode,
        "object_id": body.object_id,
        "reasoning_run_id": body.reasoning_run_id,
        "conclusion": body.conclusion,
        "proposal": {**proposal, "ontology_revision_id": ontology.current_revision_id},
        "tool_trace": toolbox.trace,
        "created_by": user.id,
        "_authorization": authorization_pin,
    }
    run = AgentRun(ontology_id=oid, created_by=user.id, status="proposed", payload=payload)
    db.add(run)
    db.commit()
    return {"run_id": run.id, "status": run.status, **_public_payload(payload)}


@router.post("/{oid}/agent/context-drafts")
def create_context_draft(oid: str, body: ContextDraftInput, db: Session = Depends(get_db), user=Depends(get_current_user)):
    ontology = scope(db, oid, user)
    projection = resolve_projection(db, AuthorizationContext(
        ontology_id=oid, principal_id=str(user.id), principal_role=user.role,
        metadata_revision_id=ontology.current_revision_id), records=[])
    authorization_pin = _authorization_pin(ontology, projection)
    toolbox = AgentToolbox(oid, db, user)
    try:
        context = toolbox.discover_context(body.task)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if _pin_for_user(db, oid, str(user.id)) != authorization_pin:
        raise HTTPException(status_code=409, detail={"code": "agent_run_context_stale", "message": "生成期间本体版本或权限发生变化，请重新加载上下文。"})
    payload = {"task": body.task, "mode": "natural_language", "context": context, "tool_trace": toolbox.trace, "created_by": user.id, "ontology_revision_id": ontology.current_revision_id, "_authorization": authorization_pin}
    run = AgentRun(ontology_id=oid, created_by=user.id, status="context_review", payload=payload)
    db.add(run)
    db.commit()
    return {"run_id": run.id, "status": run.status, **_public_payload(payload)}


@router.post("/{oid}/agent/context-preview")
def preview_context(oid: str, body: ContextUpdateInput, db: Session = Depends(get_db), user=Depends(get_current_user)):
    scope(db, oid, user)
    try:
        return context_for(AgentToolbox(oid, db, user), "", body.reasoning_run_id, body.object_ids)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/{oid}/agent/runs/{run_id}/context")
def update_agent_context(oid: str, run_id: str, body: ContextUpdateInput, db: Session = Depends(get_db), user=Depends(require_editor)):
    scope(db, oid, user)
    run = db.query(AgentRun).filter_by(id=run_id, ontology_id=oid, session_id=None).first()
    if not run:
        raise HTTPException(404, "Agent run not found")
    _require_current_run(db, oid, run)
    if run.status != "context_review":
        raise HTTPException(409, "Only a context-review run can be edited")
    toolbox = AgentToolbox(oid, db, user)
    try:
        context = context_for(toolbox, run.payload["task"], body.reasoning_run_id, body.object_ids)
        available = {p["conclusion"] for p in context["conclusions"]}
        if set(body.conclusions) - available:
            raise ValueError("结论不属于所选运行或没有与所选对象关联的证明")
        context["conclusions"] = [p for p in context["conclusions"] if p["conclusion"] in body.conclusions]
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if not _run_is_current(db, oid, run):
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "agent_run_context_stale", "message": "编辑期间本体版本或权限发生变化，请重新创建上下文。"})
    run.payload = {**run.payload, "context": context, "context_edited": True}
    db.commit()
    return {"run_id": run.id, "status": run.status, "context": context}


@router.post("/{oid}/agent/runs/{run_id}/generate")
def generate_agent_proposal(oid: str, run_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    ontology = scope(db, oid, user)
    run = db.query(AgentRun).filter_by(id=run_id, ontology_id=oid, session_id=None).first()
    if not run:
        raise HTTPException(404, "Agent run not found")
    _require_current_run(db, oid, run)
    if run.status != "context_review":
        raise HTTPException(409, "Only a context-review run can generate a proposal")
    context = (run.payload or {}).get("context") or {}
    objects = context.get("objects") or []
    reasoning_run = context.get("reasoning_run") or {}
    conclusions = context.get("conclusions") or []
    conclusion = conclusions[0].get("conclusion") if conclusions else None
    if not reasoning_run.get("id") or not conclusion:
        raise HTTPException(422, "Please approve at least one reasoning conclusion")
    toolbox = AgentToolbox(oid, db, user)
    try:
        verified = context_for(toolbox, run.payload["task"], reasoning_run["id"], [o["id"] for o in objects])
        selected_values = {p["conclusion"] for p in conclusions}
        selected_proofs = [p for p in verified["conclusions"] if p["conclusion"] in selected_values]
        if {p["conclusion"] for p in selected_proofs} != selected_values:
            raise ValueError("上下文已变化或结论没有关联证明，请重新审核")
        verified_objects = verified["objects"]
        evaluation = toolbox.evaluate_decision_rules(verified_objects)
        matched = evaluation["matches"]
        outcomes = {item.get("outcome") for item in matched if item.get("outcome")}
        if len(outcomes) > 1:
            outcome = "规则冲突，需人工复核"
            reasoning_text = "当前证据同时触发了不同的已发布决策规则，系统不会擅自选择其中一条。请检查规则优先级或交由负责人复核。"
        elif matched:
            outcome = next(iter(outcomes))
            reasoning_text = matched[0].get("explanation") or "该建议由已发布决策规则根据当前对象属性生成。"
        elif evaluation["missing"]:
            missing_fields = sorted({c["field"] for item in evaluation["missing"] for c in item["checks"] if c.get("missing") and c.get("field")})
            outcome = "关键信息缺失，暂缓放行"
            reasoning_text = "已发布规则需要以下数据，但当前对象没有提供：" + "、".join(missing_fields)
        else:
            outcome = "当前证据未触发决策规则，需人工复核"
            reasoning_text = "系统检查了当前本体中已发布的决策规则；现有观测未满足任何一条规则的触发条件，因此不给出放行、点检或停机结论。"
        proposal = {
            "scenario": run.payload["task"], "status": "proposed",
            "outcome": outcome,
            "reasoning": reasoning_text,
            "entity_ids": [o["id"] for o in verified_objects],
            "basis": [{"run_id": reasoning_run["id"], "conclusion": p["conclusion"]} for p in selected_proofs],
            "evidence": selected_proofs, "agent_mode": "deterministic",
            "decision_rule_evaluation": evaluation,
            "decision_maker": "evidence_review", "ontology_revision_id": ontology.current_revision_id,
        }
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if not _run_is_current(db, oid, run):
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "agent_run_context_stale", "message": "生成期间本体版本或权限发生变化，请重新创建上下文。"})
    run.payload = {**run.payload, "proposal": {**proposal, "ontology_revision_id": ontology.current_revision_id}, "tool_trace": toolbox.trace}
    run.status = "proposed"
    db.commit()
    return {"run_id": run.id, "status": run.status, "proposal": run.payload["proposal"], "tool_trace": run.payload["tool_trace"]}


@router.get("/{oid}/agent/runs")
def list_agent_runs(oid: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    scope(db, oid, user)
    rows = db.query(AgentRun).filter_by(ontology_id=oid, session_id=None).order_by(AgentRun.created_at.desc()).limit(100).all()
    items = []
    for run in rows:
        current = _run_is_current(db, oid, run)
        payload = run.payload if isinstance(run.payload, dict) else {}
        items.append({
            "run_id": run.id,
            "status": run.status,
            "created_at": run.created_at.isoformat(),
            "task": payload.get("task") if current else None,
            "mode": payload.get("mode") if current else None,
            "authorization_status": "current" if current else "stale_or_unpinned",
        })
    return items


@router.get("/{oid}/agent/runs/{run_id}")
def get_agent_run(oid: str, run_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    scope(db, oid, user)
    run = db.query(AgentRun).filter_by(id=run_id, ontology_id=oid, session_id=None).first()
    if not run:
        raise HTTPException(404, "Agent 运行不存在")
    _require_current_run(db, oid, run)
    payload = run.payload if isinstance(run.payload, dict) else {}
    return {"run_id": run.id, "status": run.status, **_public_payload(payload)}


@router.post("/{oid}/agent/runs/{run_id}/confirm")
def confirm_agent_run(oid: str, run_id: str, db: Session = Depends(get_db), user=Depends(require_editor)):
    scope(db, oid, user)
    run = db.query(AgentRun).filter_by(id=run_id, ontology_id=oid, session_id=None).first()
    if not run:
        raise HTTPException(404, "Agent 运行不存在")
    _require_current_run(db, oid, run)
    if run.status == "confirmed":
        return {"status": run.status, "decision_id": (run.payload or {}).get("decision_id")}
    if run.status != "proposed":
        raise HTTPException(409, "该 Agent 运行不能确认")
    raise HTTPException(409, {
        "code": "legacy_decision_confirmation_disabled",
        "message": "旧版 Agent 只能生成建议，不能直接确认或写入决策。请将线索带入持久会话；需要业务写回时，必须通过 DecisionCase、只读预览、独立审核和 ActionRun。",
    })
