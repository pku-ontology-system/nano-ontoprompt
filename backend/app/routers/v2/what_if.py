"""Version-pinned temporal What-If scenarios; results are isolated previews."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_user, require_editor
from app.models.user import User
from app.models.ontology import OntologyProject
from app.models.ontology_revision import OntologyRevision
from app.models.v2.dynamic_ontology import WhatIfRun, WhatIfScenario
from app.models.v2.temporal_vector_index import TemporalVectorIndexJob
from app.schemas.v2.scenario_context import WhatIfContextResponse, scenario_inputs_digest
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError
from app.services.v2.what_if_service import (
    WhatIfCancelled,
    WhatIfError,
    build_context,
    bind_scenario_branch,
    context_content_digest,
    revalidate_scenario_context,
    run_preview,
    _rules_for_context,
    _validate_assumptions,
    serialize_run,
    serialize_scenario,
    what_if_run_error_code,
    _vector_query_from_context,
)
from app.services.v2.temporal_vector_index import serialize_temporal_vector_index_job

router = APIRouter(dependencies=[Depends(get_current_user)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _error(exc: WhatIfError):
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})


def _has_ontology_access(db: Session, ontology_id: str, user: User) -> bool:
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        return False
    return True


def _require_ontology_access(db: Session, ontology_id: str, user: User) -> None:
    if not _has_ontology_access(db, ontology_id, user):
        raise HTTPException(status_code=404, detail="本体不存在或无权访问")


def _require_record_owner(record, user: User, message: str) -> None:
    if user.role != "admin" and str(record.created_by or "") != str(user.id):
        raise HTTPException(status_code=404, detail=message)


def _scenario_input_hash(scenario: WhatIfScenario) -> str:
    payload = {
        "scenario_id": scenario.id,
        "baseline": scenario.baseline_json or {},
        "assumptions": scenario.assumptions_json or [],
        "rules": scenario.rule_overrides_json or {},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _validate_scenario_payload(db: Session, scenario: WhatIfScenario, baseline: dict) -> None:
    assumptions = scenario.assumptions_json
    overrides = scenario.rule_overrides_json
    if not isinstance(assumptions, list) or any(not isinstance(item, dict) for item in assumptions):
        raise WhatIfError("情景假设必须是对象列表")
    if not isinstance(overrides, dict):
        raise WhatIfError("规则覆盖格式无效")
    for key in ("enabled", "disabled"):
        if key in overrides and (
            not isinstance(overrides[key], list)
            or any(not isinstance(item, (str, int)) for item in overrides[key])
        ):
            raise WhatIfError(f"规则覆盖 {key} 必须是规则 ID 列表")
    if "temporary" in overrides and (
        not isinstance(overrides["temporary"], list)
        or any(not isinstance(item, dict) for item in overrides["temporary"])
    ):
        raise WhatIfError("临时规则必须是对象列表")
    revision_snapshot = None
    if scenario.base_revision_id:
        revision = db.query(OntologyRevision).filter(
            OntologyRevision.id == scenario.base_revision_id,
            OntologyRevision.ontology_id == scenario.ontology_id,
        ).first()
        if revision is None:
            raise WhatIfError("情景固定的本体修订不存在")
        revision_snapshot = revision.snapshot_json or {}
    _rules_for_context(db, scenario.ontology_id, overrides, snapshot=revision_snapshot)
    _validate_assumptions(db, scenario, baseline)


@router.get("/{ontology_id}/what-if/context", response_model=WhatIfContextResponse)
def context(ontology_id: str, target_instance_id: str | None = None, episode_id: str | None = None, at: str | None = None, mode: str = Query("cumulative", pattern="^(cumulative|window)$"), replay_id: str | None = None, evidence_query: str | None = Query(default=None, max_length=1000), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_ontology_access(db, ontology_id, user)
    try:
        return build_context(db, ontology_id, target_instance_id=target_instance_id, episode_id=episode_id, at=at, mode=mode, replay_id=replay_id, evidence_query=evidence_query, user=user)
    except WhatIfError as exc:
        if exc.status_code == 202:
            return JSONResponse(status_code=202, content={
                "status": "indexing",
                "code": exc.code,
                "message": str(exc),
                "status_path": f"/api/v2/ontologies/{ontology_id}/what-if/vector-index-jobs/{exc.details.get('job', {}).get('id', '')}",
                **exc.details,
            })
        _error(exc)


@router.get("/{ontology_id}/what-if/vector-index-jobs/{job_id}")
def get_vector_index_job(ontology_id: str, job_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_ontology_access(db, ontology_id, user)
    job = db.query(TemporalVectorIndexJob).filter(
        TemporalVectorIndexJob.id == job_id,
        TemporalVectorIndexJob.ontology_id == ontology_id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="向量索引任务不存在")
    return serialize_temporal_vector_index_job(job)


@router.get("/{ontology_id}/what-if/scenarios")
def list_scenarios(ontology_id: str, summary: bool = Query(False), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_ontology_access(db, ontology_id, user)
    query = db.query(WhatIfScenario).filter(WhatIfScenario.ontology_id == ontology_id)
    if user.role != "admin":
        query = query.filter(WhatIfScenario.created_by == user.id)
    rows = query.order_by(WhatIfScenario.updated_at.desc()).limit(100).all()
    if summary:
        items = []
        for row in rows:
            baseline = row.baseline_json if isinstance(row.baseline_json, dict) else {}
            nodes = baseline.get("nodes") if isinstance(baseline.get("nodes"), list) else []
            assumptions = row.assumptions_json or []
            rule_overrides = row.rule_overrides_json or {}
            items.append({
                "id": row.id,
                "ontology_id": row.ontology_id,
                "name": row.name,
                "status": row.status,
                "created_by": row.created_by,
                "baseline": {"scenario_context": baseline.get("scenario_context")},
                "target_ids": [str(node["id"]) for node in nodes
                               if isinstance(node, dict) and node.get("id")],
                "assumptions": assumptions,
                "rule_overrides": rule_overrides,
                "scenario_input_digest": scenario_inputs_digest(assumptions, rule_overrides),
            })
        return {"scenarios": items}
    return {"scenarios": [serialize_scenario(row) for row in rows]}


@router.post("/{ontology_id}/what-if/scenarios")
def create_scenario(ontology_id: str, body: dict, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _require_ontology_access(db, ontology_id, user)
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise HTTPException(404, "本体不存在")
    submitted_baseline = body.get("baseline") or {}
    if not isinstance(submitted_baseline, dict):
        raise HTTPException(status_code=422, detail="推演基线格式无效，请重新读取基线")
    target_instance_id = body.get("target_instance_id") or submitted_baseline.get("target_instance_id")
    episode_id = body.get("episode_id", submitted_baseline.get("episode_id"))
    at = body.get("at", submitted_baseline.get("at"))
    mode = body.get("mode", submitted_baseline.get("mode", "cumulative"))
    provenance = submitted_baseline.get("provenance") or {}
    replay_id = body.get("replay_id") or provenance.get("replay_id")
    if mode not in {"cumulative", "window"}:
        raise HTTPException(status_code=422, detail="推演时间模式无效")
    try:
        baseline = build_context(
            db,
            ontology_id,
            target_instance_id=target_instance_id,
            episode_id=episode_id,
            at=at,
            mode=mode,
            replay_id=replay_id,
            evidence_query=_vector_query_from_context(submitted_baseline),
            user=user,
        )
    except WhatIfError as exc:
        if exc.status_code == 202:
            raise HTTPException(status_code=409, detail={
                "code": exc.code,
                "message": "向量索引仍在后台构建，请完成后重新检索并读取基线。",
                **exc.details,
            }) from exc
        _error(exc)
    if submitted_baseline:
        submitted_digest = context_content_digest(submitted_baseline)
        authoritative_digest = baseline["provenance"]["context_digest"]
        if submitted_digest != authoritative_digest:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "WHAT_IF_CONTEXT_STALE",
                    "message": "推演基线与当前数据、规则或本体修订不一致，请重新读取基线。",
                },
            )
    if len(baseline.get("nodes") or []) > 500:
        raise HTTPException(422, "推演基线超过 500 个节点限制，请缩小范围")
    if len(baseline.get("edges") or []) > 2000:
        raise HTTPException(422, "推演基线超过 2,000 条事实限制，请缩小范围")
    scenario_id = str(uuid.uuid4())
    try:
        baseline = bind_scenario_branch(baseline, scenario_id)
    except WhatIfError as exc:
        _error(exc)
    scenario = WhatIfScenario(id=scenario_id, ontology_id=ontology_id, base_revision_id=baseline.get("base_revision_id") or project.current_revision_id, dataset_version_id=baseline.get("dataset_version_id"), name=str(body.get("name") or "FactoryNet 情景"), status="draft", baseline_json=baseline, assumptions_json=body.get("assumptions") or [], rule_overrides_json=body.get("rule_overrides") or {}, created_by=user.id)
    try:
        _validate_scenario_payload(db, scenario, baseline)
    except WhatIfError as exc:
        _error(exc)
    db.add(scenario); db.commit(); db.refresh(scenario)
    return serialize_scenario(scenario)


@router.patch("/{ontology_id}/what-if/scenarios/{scenario_id}")
def patch_scenario(ontology_id: str, scenario_id: str, body: dict, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _require_ontology_access(db, ontology_id, user)
    scenario = db.query(WhatIfScenario).with_for_update().filter(WhatIfScenario.id == scenario_id, WhatIfScenario.ontology_id == ontology_id).first()
    if not scenario:
        raise HTTPException(404, "情景不存在")
    _require_record_owner(scenario, user, "情景不存在")
    active_run = db.query(WhatIfRun).filter(
        WhatIfRun.scenario_id == scenario.id,
        WhatIfRun.status.in_(["queued", "running"]),
    ).first()
    if active_run:
        raise HTTPException(status_code=409, detail="情景正在运行，不能同时修改")
    try:
        baseline = revalidate_scenario_context(db, scenario, user)
        if "name" in body: scenario.name = str(body["name"])
        if "assumptions" in body: scenario.assumptions_json = body["assumptions"] if body["assumptions"] is not None else []
        if "rule_overrides" in body: scenario.rule_overrides_json = body["rule_overrides"] if body["rule_overrides"] is not None else {}
        _validate_scenario_payload(db, scenario, baseline)
        db.commit(); db.refresh(scenario)
    except WhatIfError as exc:
        db.rollback()
        _error(exc)
    return serialize_scenario(scenario)


def _execute_run(run_id: str) -> None:
    db = SessionLocal()
    try:
        claimed = db.execute(
            update(WhatIfRun)
            .where(WhatIfRun.id == run_id, WhatIfRun.status == "queued")
            .values(status="running", stage="prepare_baseline", progress=10, started_at=datetime.now(timezone.utc))
        ).rowcount
        db.commit()
        if not claimed:
            return
        run = db.query(WhatIfRun).filter(WhatIfRun.id == run_id).first()
        if not run:
            return
        actor = db.get(User, run.created_by) if run.created_by else None
        if actor is None or actor.role not in {"admin", "editor"} or not _has_ontology_access(db, run.ontology_id, actor):
            run.status = "failed"
            run.stage = "failed"
            run.error = "WHAT_IF_ACTOR_UNAVAILABLE"
            db.commit()
            return
        scenario = db.query(WhatIfScenario).filter(WhatIfScenario.id == run.scenario_id).first()
        if not scenario:
            run.status = "failed"; run.error = "WHAT_IF_SCENARIO_UNAVAILABLE"; db.commit(); return
        if actor.role != "admin" and str(scenario.created_by or "") != str(actor.id):
            run.status = "failed"; run.stage = "failed"; run.error = "WHAT_IF_OWNER_MISMATCH"; db.commit(); return
        if not run.input_hash or not hmac.compare_digest(str(run.input_hash), _scenario_input_hash(scenario)):
            run.status = "failed"; run.stage = "failed"; run.error = "WHAT_IF_INPUT_CHANGED"; db.commit(); return
        revalidate_scenario_context(db, scenario, actor)
        if run.cancel_requested:
            run.status = "cancelled"; run.stage = "cancelled"; db.commit(); return
        run.stage = "baseline_reasoning"; run.progress = 35; db.commit()

        def on_stage(stage: str, progress: int) -> None:
            db.refresh(run)
            if run.cancel_requested:
                run.status = "cancelled"
                run.stage = "cancelled"
                run.error = "WHAT_IF_CANCELLED"
                db.commit()
                raise WhatIfCancelled("推演已取消")
            run.stage = stage
            run.progress = progress
            db.commit()

        payload = run_preview(db, scenario, run, on_stage=on_stage)
        db.refresh(scenario)
        db.refresh(run)
        if run.status != "running":
            return
        if run.cancel_requested:
            raise WhatIfCancelled("推演已取消")
        if not run.input_hash or not hmac.compare_digest(str(run.input_hash), _scenario_input_hash(scenario)):
            raise WhatIfError("情景内容在执行期间发生变化，结果未保存")
        revalidate_scenario_context(db, scenario, actor)
        run.stage = "completed"; run.progress = 100; run.context_json = scenario.baseline_json or {}; run.baseline_result_json = payload["baseline"]; run.scenario_result_json = payload["scenario"]; run.diff_json = payload["diff"]; run.engine = payload["engine"]; run.engine_version = payload["engine_version"]; run.status = "completed"; run.completed_at = datetime.now(timezone.utc); scenario.status = "completed"; db.commit()
    except WhatIfCancelled:
        db.rollback()
        run = db.query(WhatIfRun).filter(WhatIfRun.id == run_id).first()
        if run:
            run.status = "cancelled"; run.stage = "cancelled"; run.error = "WHAT_IF_CANCELLED"
            scenario = db.query(WhatIfScenario).filter(WhatIfScenario.id == run.scenario_id).first()
            if scenario: scenario.status = "cancelled"
            db.commit()
    except WhatIfError as exc:
        db.rollback(); run = db.query(WhatIfRun).filter(WhatIfRun.id == run_id).first()
        if run:
            run.status = "failed"; run.stage = "failed"; run.error = what_if_run_error_code(exc.code)
            scenario = db.query(WhatIfScenario).filter(WhatIfScenario.id == run.scenario_id).first()
            if scenario: scenario.status = "failed"
            db.commit()
    except Exception as exc:
        db.rollback(); run = db.query(WhatIfRun).filter(WhatIfRun.id == run_id).first()
        if run:
            run.status = "failed"; run.stage = "failed"; run.error = "WHAT_IF_RUN_FAILED"
            scenario = db.query(WhatIfScenario).filter(WhatIfScenario.id == run.scenario_id).first()
            if scenario: scenario.status = "failed"
            db.commit()
    finally:
        db.close()


@router.post("/{ontology_id}/what-if/scenarios/{scenario_id}/runs", status_code=202)
def start_run(ontology_id: str, scenario_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    _require_ontology_access(db, ontology_id, user)
    scenario = db.query(WhatIfScenario).with_for_update().filter(WhatIfScenario.id == scenario_id, WhatIfScenario.ontology_id == ontology_id).first()
    if not scenario:
        raise HTTPException(404, "情景不存在")
    _require_record_owner(scenario, user, "情景不存在")
    active_run = db.query(WhatIfRun).filter(
        WhatIfRun.scenario_id == scenario.id,
        WhatIfRun.status.in_(["queued", "running"]),
    ).first()
    if active_run:
        raise HTTPException(status_code=409, detail="该情景已有正在排队或运行的推演")
    try:
        baseline = revalidate_scenario_context(db, scenario, user)
        _validate_scenario_payload(db, scenario, baseline)
    except WhatIfError as exc:
        _error(exc)
    run = WhatIfRun(id=str(uuid.uuid4()), scenario_id=scenario.id, ontology_id=ontology_id, status="queued", stage="prepare_baseline", progress=0, context_json=scenario.baseline_json or {}, input_hash=_scenario_input_hash(scenario), created_by=user.id)
    scenario.status = "queued"; db.add(run); db.commit(); db.refresh(run)
    if os.getenv("CELERY_ENABLED", "").lower() in {"1", "true", "yes"}:
        try:
            from app.tasks.v2.workbench import run_what_if_task
            run_what_if_task.delay(run.id)
        except Exception:
            background_tasks.add_task(_execute_run, run.id)
    else:
        background_tasks.add_task(_execute_run, run.id)
    return serialize_run(run)


@router.get("/{ontology_id}/what-if/runs/{run_id}")
def get_run(ontology_id: str, run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_ontology_access(db, ontology_id, user)
    run = db.query(WhatIfRun).filter(WhatIfRun.id == run_id, WhatIfRun.ontology_id == ontology_id).first()
    if not run:
        raise HTTPException(404, "推演运行不存在")
    _require_record_owner(run, user, "推演运行不存在")
    return serialize_run(run)


@router.post("/{ontology_id}/what-if/runs/{run_id}/cancel")
def cancel_run(ontology_id: str, run_id: str, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    """Request cooperative cancellation; the worker owns the final state."""
    _require_ontology_access(db, ontology_id, user)
    run = db.query(WhatIfRun).filter(
        WhatIfRun.id == run_id, WhatIfRun.ontology_id == ontology_id
    ).first()
    if not run:
        raise HTTPException(404, "推演运行不存在")
    _require_record_owner(run, user, "推演运行不存在")
    if run.status in {"queued", "running"}:
        run.cancel_requested = True
        db.commit()
    return serialize_run(run)
