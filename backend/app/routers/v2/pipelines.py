"""v2 Pipeline API — 支持新 DSL (nodes/edges) + 旧 steps 格式兼容"""
from __future__ import annotations
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import Optional
from datetime import datetime, timezone
from uuid import uuid4
import logging
from app.database import SessionLocal
from app.deps import get_current_user, require_admin, require_editor
from app.models.user import User
from app.models.v2.pipeline import (
    Pipeline,
    PipelineRun,
    PipelineVersion,
    PipelineSchedule,
    PipelineScheduleRevision,
    PipelineScheduleRun,
)
from app.models.v2.service_principal import ServicePrincipal, ServicePrincipalGrant
from app.models.v2.dataset import Dataset
from app.services.v2.dataset_authorization import require_dataset_access
from app.models.v2.workbench_task import ModelInvocation, PipelineLLMInvocationEvent
from app.services.v2.pipeline.llm_invocation_recorder import verify_pipeline_invocation_event_chain
from app.services.v2.pipeline_execution import (
    PipelineExecutionError,
    build_pipeline_run_manifest,
    content_digest,
    make_pipeline_snapshot,
)
from app.services.v2.scheduler.cron_service import CronService
# 确保 Dataset 模型先导入以解析 FK
import app.models.v2.dataset  # noqa: F401

router = APIRouter(dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _pipeline_or_404(db: Session, pipeline_id: str, user: User, *, write: bool = False) -> Pipeline:
    if write and user.role not in {"admin", "editor"}:
        raise HTTPException(403, "Editor role required")
    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if pipeline is None or (user.role != "admin" and str(pipeline.created_by or "") != str(user.id)):
        raise HTTPException(404, "Pipeline not found")
    for dataset_id in _pipeline_dataset_ids(pipeline.source_dataset_id, pipeline.definition, pipeline.spec):
        require_dataset_access(db, dataset_id, user)
    return pipeline


def _pipeline_dataset_ids(source_dataset_id: str | None, definition: object, spec: object) -> set[str]:
    dataset_ids = {str(source_dataset_id)} if source_dataset_id else set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"dataset_id", "source_dataset_id", "datasetId", "sourceDatasetId"} and isinstance(child, str) and child:
                    dataset_ids.add(child)
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(definition)
    visit(spec)
    return dataset_ids


def _require_pipeline_dataset_ids(db: Session, user: User, source_dataset_id: str | None, definition: object, spec: object) -> None:
    for dataset_id in _pipeline_dataset_ids(source_dataset_id, definition, spec):
        require_dataset_access(db, dataset_id, user)


# ── Pydantic Models ────────────────────────────────────────────────

class PipelineCreate(BaseModel):
    name: str
    domain: str = "通用"
    description: str = ""
    source_dataset_id: Optional[str] = None
    route: Optional[str] = None  # A|B|C (legacy)
    spec: Optional[dict] = None  # legacy steps
    definition: Optional[dict] = None  # new DSL: {nodes: [...], edges: [...]}


class PipelineUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    description: Optional[str] = None
    source_dataset_id: Optional[str] = None
    route: Optional[str] = None
    spec: Optional[dict] = None
    definition: Optional[dict] = None
    status: Optional[str] = None


class PipelineResponse(BaseModel):
    id: str
    name: str
    domain: Optional[str] = "通用"
    description: Optional[str] = ""
    source_dataset_id: Optional[str] = None
    route: Optional[str] = None
    spec: Optional[dict] = None
    definition: Optional[dict] = None
    status: str = "draft"
    branch: Optional[str] = "main"
    version: int = 1
    target_curated_ids: Optional[list] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class ValidateResult(BaseModel):
    valid: bool
    errors: list[dict] = []
    warnings: list[dict] = []


class PipelineScheduleUpsert(BaseModel):
    cron_expression: str
    timezone: str = "UTC"
    enabled: bool = True
    pipeline_version_id: Optional[str] = None
    misfire_policy: str = "skip"
    misfire_grace_seconds: int = 300
    overlap_policy: str = "forbid"
    service_principal_id: Optional[str] = None
    expected_revision: Optional[int] = None


# ── CRUD ──────────────────────────────────────────────────────────

@router.post("", response_model=PipelineResponse, status_code=201, dependencies=[Depends(require_editor)])
def create_pipeline(body: PipelineCreate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    """创建新 Pipeline。支持旧 steps 格式和新 nodes/edges DSL。"""
    # 重名校验
    existing = db.query(Pipeline).filter(
        Pipeline.name == body.name,
        Pipeline.domain == body.domain,
    ).first()
    if existing:
        raise HTTPException(400, "已存在同名 Pipeline，请更换名称。")
    _require_pipeline_dataset_ids(db, current_user, body.source_dataset_id, body.definition, body.spec)

    # 从 definition 推断 route，如无法推断默认为 'A'
    inferred_route = body.route
    if not inferred_route and body.definition:
        nodes = body.definition.get("nodes", [])
        types = {n.get("type") for n in nodes if n.get("type")}
        if "transform" in types:
            # 根据 Transform 节点配置推断
            pass  # 保留默认
        inferred_route = inferred_route or "A"
    pl = Pipeline(
        name=body.name,
        domain=body.domain or "通用",
        description=body.description or "",
        source_dataset_id=body.source_dataset_id,
        route=inferred_route or "A",  # SQLite 该列有 NOT NULL 约束
        spec=body.spec or {},
        definition=body.definition,
        status="draft",
        branch="main",
        version=1,
        created_by=str(current_user.id),
    )
    db.add(pl)
    db.commit()
    db.refresh(pl)
    return _format_pipeline(pl)


@router.get("", response_model=list[dict])
def list_pipelines(
    search: str = "",
    domain: str = "",
    status: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pipeline 列表，支持按名称/ID/域/状态搜索。"""
    q = db.query(Pipeline)
    if current_user.role != "admin":
        q = q.filter(Pipeline.created_by == str(current_user.id))
    if search:
        q = q.filter(
            Pipeline.name.ilike(f"%{search}%") | Pipeline.id.ilike(f"%{search}%")
        )
    if domain:
        q = q.filter(Pipeline.domain == domain)
    if status:
        q = q.filter(Pipeline.status == status)
    q = q.order_by(Pipeline.updated_at.desc()).limit(100)
    results = []
    for pl in q:
        try:
            _require_pipeline_dataset_ids(db, current_user, pl.source_dataset_id, pl.definition, pl.spec)
        except HTTPException:
            continue
        d = _format_pipeline(pl)
        # 添加最近运行信息
        last_run = db.query(PipelineRun).filter(
            PipelineRun.pipeline_id == pl.id
        ).order_by(PipelineRun.created_at.desc()).first()
        if last_run:
            d["last_run_status"] = last_run.status
            d["last_run_at"] = (
                last_run.started_at.isoformat() if last_run.started_at else None
            )
        results.append(d)
    return results


@router.get("/{pipeline_id}", response_model=PipelineResponse)
def get_pipeline(pipeline_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    pl = _pipeline_or_404(db, pipeline_id, current_user)
    return _format_pipeline(pl)


@router.put("/{pipeline_id}", response_model=PipelineResponse, dependencies=[Depends(require_editor)])
def update_pipeline(pipeline_id: str, body: PipelineUpdate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    pl = _pipeline_or_404(db, pipeline_id, current_user, write=True)

    update_data = body.model_dump(exclude_unset=True)
    _require_pipeline_dataset_ids(
        db, current_user,
        update_data.get("source_dataset_id", pl.source_dataset_id),
        update_data.get("definition", pl.definition),
        update_data.get("spec", pl.spec),
    )
    requested_status = update_data.pop("status", None)
    if requested_status not in (None, "draft", "editing"):
        raise HTTPException(400, "Pipeline 状态只能由对应的发布或运行操作更新。")
    runtime_fields = {"definition", "spec", "source_dataset_id", "route"}
    if pl.status == "published" and any(
        key in update_data and update_data[key] != getattr(pl, key)
        for key in runtime_fields
    ):
        pl.status = "editing"
    for k, v in update_data.items():
        setattr(pl, k, v)
    if requested_status is not None:
        pl.status = requested_status
    pl.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(pl)
    return _format_pipeline(pl)


@router.delete("/{pipeline_id}", dependencies=[Depends(require_admin)])
def delete_pipeline(pipeline_id: str, db: Session = Depends(get_db)):
    pl = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pl:
        raise HTTPException(404, "Pipeline not found")
    if db.query(PipelineSchedule.id).filter(PipelineSchedule.pipeline_id == pipeline_id).first():
        raise HTTPException(409, "Pipeline 有持久 Schedule 历史；请先停用并保留运行审计记录")
    # 级联删除 runs + versions
    db.query(PipelineRun).filter(PipelineRun.pipeline_id == pipeline_id).delete()
    db.query(PipelineVersion).filter(PipelineVersion.pipeline_id == pipeline_id).delete()
    db.delete(pl)
    db.commit()
    return {"status": "deleted", "id": pipeline_id}


# ── Validate ──────────────────────────────────────────────────────

@router.post("/{pipeline_id}/validate", response_model=ValidateResult)
def validate_pipeline(pipeline_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """校验 Pipeline definition 是否合法。"""
    pl = _pipeline_or_404(db, pipeline_id, current_user)

    errors = []
    warnings = []
    definition = pl.definition

    if not definition:
        # 旧格式：无 definition，认为合法
        return ValidateResult(valid=True, errors=[], warnings=[])

    nodes = definition.get("nodes", [])
    edges = definition.get("edges", [])

    if not nodes:
        errors.append({"node_id": "", "severity": "error", "message": "Pipeline 至少需要一个节点。"})

    node_ids = set()
    node_types = {}
    llm_fallback_policies: set[str] = set()
    base_extraction = (pl.spec or {}).get("md_to_structured") if isinstance(pl.spec, dict) else None
    base_fallback_policy = (
        base_extraction.get("fallback_policy", "fail_closed")
        if isinstance(base_extraction, dict) else "fail_closed"
    )
    for n in nodes:
        nid = n.get("id", "")
        node_ids.add(nid)
        node_types[nid] = n.get("type", "")
        if n.get("type") == "transform":
            config = n.get("config")
            if not isinstance(config, dict):
                continue
            for step in config.get("steps") or []:
                if not isinstance(step, dict) or step.get("op") not in ("llm_structurize", "llm_enrich"):
                    continue
                params = step.get("params")
                if params is None:
                    params = {}
                if not isinstance(params, dict):
                    errors.append({
                        "node_id": nid,
                        "severity": "error",
                        "message": "LLM 提取参数必须是对象。",
                    })
                    continue
                fallback_policy = params.get("fallback_policy", base_fallback_policy)
                if fallback_policy not in ("fail_closed", "rule_based"):
                    errors.append({
                        "node_id": nid,
                        "severity": "error",
                        "message": "LLM 操作的回退策略只能是 fail_closed 或 rule_based。",
                    })
                else:
                    llm_fallback_policies.add(fallback_policy)

    if len(llm_fallback_policies) > 1:
        errors.append({
            "node_id": "",
            "severity": "error",
            "message": "同一 Pipeline 的 LLM 结构化与语义增强共享失败策略；请统一设置 fail_closed 或 rule_based。",
        })

    # 检查连接规则
    for edge in edges:
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if src not in node_ids:
            errors.append({"node_id": src, "severity": "error", "message": f"边引用了不存在的源节点: {src}"})
        if tgt not in node_ids:
            errors.append({"node_id": tgt, "severity": "error", "message": f"边引用了不存在的目标节点: {tgt}"})

        src_type = node_types.get(src, "")
        tgt_type = node_types.get(tgt, "")

        # Connector → Transform 不允许
        if src_type == "connector" and tgt_type == "transform":
            errors.append({
                "node_id": edge.get("id", ""),
                "severity": "error",
                "message": "Connector 不能直接连接 Transform，需要经过 Storage。",
            })
        # Connector → Output 不允许
        if src_type == "connector" and tgt_type == "output":
            errors.append({
                "node_id": edge.get("id", ""),
                "severity": "error",
                "message": "Connector 不能直接连接 Output。",
            })
        # Output → anything 不允许
        if src_type == "output":
            errors.append({
                "node_id": edge.get("id", ""),
                "severity": "error",
                "message": "Output 节点不能作为边的起点。",
            })

    # 检查是否有至少一条 Connector → Storage → Transform → Output 路径
    has_connector = any(t == "connector" for t in node_types.values())
    has_output = any(t == "output" for t in node_types.values())
    if has_connector and not has_output:
        warnings.append({
            "node_id": "",
            "severity": "warning",
            "message": "存在 Connector 但没有 Output 节点，Pipeline 不会产生输出。",
        })

    return ValidateResult(valid=len(errors) == 0, errors=errors, warnings=warnings)


# ── Publish ───────────────────────────────────────────────────────

@router.post("/{pipeline_id}/publish")
def publish_pipeline(pipeline_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    """发布 Pipeline 为稳定版本。"""
    pl = _pipeline_or_404(db, pipeline_id, current_user, write=True)

    # 先校验
    validation = validate_pipeline(pipeline_id, db, current_user)
    if not validation.valid:
        raise HTTPException(400, f"Pipeline 校验失败，无法发布: {validation.errors}")

    next_version = (pl.version or 1) + 1
    pl.status = "published"
    pl.version = next_version
    pl.updated_at = datetime.now(timezone.utc)
    snapshot = make_pipeline_snapshot(pl, version=next_version)
    version_record = PipelineVersion(
        pipeline_id=pipeline_id,
        version=next_version,
        definition=snapshot["definition"],
        execution_snapshot=snapshot,
        definition_digest=content_digest(snapshot.get("definition") or {}),
        snapshot_digest=content_digest(snapshot),
        status="published",
    )
    db.add(version_record)
    db.commit()
    db.refresh(version_record)

    return {
        "id": pl.id,
        "status": pl.status,
        "version": pl.version,
        "version_id": version_record.id,
        "definition_digest": version_record.definition_digest,
        "snapshot_digest": version_record.snapshot_digest,
    }


# ── Versions ──────────────────────────────────────────────────────

@router.get("/{pipeline_id}/versions")
def list_versions(pipeline_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """查看版本历史。"""
    _pipeline_or_404(db, pipeline_id, current_user)
    versions = db.query(PipelineVersion).filter(
        PipelineVersion.pipeline_id == pipeline_id
    ).order_by(PipelineVersion.version.desc()).all()
    return [
        {
            "id": v.id,
            "version": v.version,
            "status": v.status,
            "definition_digest": v.definition_digest,
            "snapshot_digest": v.snapshot_digest,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in versions
    ]


# ── Run (保留原有) ────────────────────────────────────────────────

@router.post("/{pipeline_id}/run")
def run_pipeline(
    pipeline_id: str,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(require_editor),
):
    _pipeline_or_404(db, pipeline_id, current_user, write=True)
    run, _created = _create_pipeline_run(
        db, pipeline_id, trigger_mode="manual", dispatch_status="pending",
        idempotency_key=idempotency_key,
        request_actor_id=getattr(current_user, "id", None),
    )
    if run.status == "pending":
        try:
            from app.tasks.v2.pipeline_run import dispatch_pipeline_run
            dispatch_pipeline_run(run.id)
        except Exception:
            # The persisted pending Run is the dispatch intent. Beat reconciliation
            # will retry after a route/process interruption.
            logger.exception("Pipeline Run dispatch requires reconciliation: %s", run.id)
        db.refresh(run)
    return {
        "run_id": run.id,
        "status": run.status,
        "dispatch_status": run.dispatch_status,
        "execution_mode": run.execution_mode,
        "execution_digest": run.execution_digest,
        "error": run.error_log,
    }


def _create_pipeline_run(
    db: Session,
    pipeline_id: str,
    *,
    trigger_mode: str,
    dispatch_status: str,
    idempotency_key: Optional[str],
    request_actor_id: Optional[str] = None,
    service_principal_id: Optional[str] = None,
    pinned_pipeline_version_id: Optional[str] = None,
    trigger_context: Optional[dict] = None,
) -> tuple[PipelineRun, bool]:
    raw_request_key = idempotency_key if isinstance(idempotency_key, str) else None
    request_key = (raw_request_key or "").strip() or uuid4().hex
    if len(request_key) > 128:
        raise HTTPException(400, "Idempotency-Key 最长 128 个字符")
    fingerprint_payload = {
        "pipeline_id": pipeline_id,
        "trigger_mode": trigger_mode,
        "request_actor_id": request_actor_id,
    }
    if pinned_pipeline_version_id or trigger_context:
        fingerprint_payload["pipeline_version_id"] = pinned_pipeline_version_id
        fingerprint_payload["trigger_context"] = trigger_context or {}
    if service_principal_id:
        fingerprint_payload["service_principal_id"] = service_principal_id
    fingerprint = content_digest(fingerprint_payload)

    existing = db.query(PipelineRun).filter(
        PipelineRun.pipeline_id == pipeline_id,
        PipelineRun.request_actor_id == request_actor_id,
        PipelineRun.request_key == request_key,
    ).with_for_update().first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(409, "Idempotency-Key 已被不同的 Pipeline 操作使用")
        return existing, False

    pl = db.query(Pipeline).filter(Pipeline.id == pipeline_id).with_for_update().first()
    if not pl:
        raise HTTPException(404, "Pipeline not found")
    try:
        manifest = build_pipeline_run_manifest(
            db,
            pl,
            trigger_mode=trigger_mode,
            pipeline_version_id=pinned_pipeline_version_id,
            trigger_context=trigger_context,
        )
    except PipelineExecutionError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc

    pinned_sources = manifest["sources"]
    run = PipelineRun(
        pipeline_id=pipeline_id,
        status="pending",
        dataset_version_id=pinned_sources[0]["dataset_version_id"] if len(pinned_sources) == 1 else None,
        pipeline_version_id=manifest.get("pipeline_version_id"),
        execution_mode=manifest["execution_mode"],
        execution_manifest=manifest,
        execution_digest=manifest["execution_digest"],
        request_key=request_key,
        request_actor_id=request_actor_id,
        service_principal_id=service_principal_id,
        request_fingerprint=fingerprint,
        dispatch_status=dispatch_status,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        winner = db.query(PipelineRun).filter(
            PipelineRun.pipeline_id == pipeline_id,
            PipelineRun.request_actor_id == request_actor_id,
            PipelineRun.request_key == request_key,
        ).with_for_update().first()
        if winner and winner.request_fingerprint == fingerprint:
            return winner, False
        if winner:
            raise HTTPException(409, "Pipeline Run 请求键冲突；请使用新的 Idempotency-Key")
        raise
    db.refresh(run)
    return run, True


def _schedule_payload(schedule: PipelineSchedule | None) -> dict | None:
    if schedule is None:
        return None
    return {
        "id": schedule.id,
        "pipeline_id": schedule.pipeline_id,
        "owner_id": schedule.owner_id,
        "steward_id": schedule.steward_id,
        "service_principal_id": schedule.service_principal_id,
        "pipeline_version_id": schedule.pipeline_version_id,
        "cron_expression": schedule.cron_expression,
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "revision": schedule.revision,
        "misfire_policy": schedule.misfire_policy,
        "misfire_grace_seconds": schedule.misfire_grace_seconds,
        "overlap_policy": schedule.overlap_policy,
        "next_fire_at": schedule.next_fire_at.isoformat() if schedule.next_fire_at else None,
        "last_fire_at": schedule.last_fire_at.isoformat() if schedule.last_fire_at else None,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
        "updated_at": schedule.updated_at.isoformat() if schedule.updated_at else None,
    }


def _cancel_pending_schedule_runs(db: Session, schedule: PipelineSchedule, reason: str) -> None:
    rows = db.query(PipelineScheduleRun).filter(
        PipelineScheduleRun.schedule_id == schedule.id,
        PipelineScheduleRun.status.in_(("pending", "dispatching", "queued")),
    ).with_for_update().all()
    now = datetime.now(timezone.utc)
    for schedule_run in rows:
        pipeline_run = None
        if schedule_run.pipeline_run_id:
            pipeline_run = db.query(PipelineRun).filter(
                PipelineRun.id == schedule_run.pipeline_run_id
            ).with_for_update().first()
        if pipeline_run and pipeline_run.status != "pending":
            continue
        if pipeline_run:
            pipeline_run.status = "cancelled"
            pipeline_run.dispatch_status = "cancelled"
            pipeline_run.dispatch_claim_token = None
            pipeline_run.dispatch_lease_until = None
            pipeline_run.finished_at = now
            pipeline_run.error_log = reason
        schedule_run.status = "cancelled"
        schedule_run.error_code = "SCHEDULE_REVISION_SUPERSEDED"
        schedule_run.error_message = reason
        schedule_run.finished_at = now
        schedule_run.claim_token = None
        schedule_run.lease_until = None


def _record_schedule_revision(
    db: Session,
    schedule: PipelineSchedule,
    *,
    actor_id: str | None,
) -> PipelineScheduleRevision:
    snapshot = _schedule_payload(schedule) or {}
    revision = PipelineScheduleRevision(
        schedule_id=schedule.id,
        revision=schedule.revision,
        pipeline_version_id=schedule.pipeline_version_id,
        snapshot=snapshot,
        snapshot_digest=content_digest(snapshot),
        actor_id=actor_id,
    )
    db.add(revision)
    return revision


@router.get("/{pipeline_id}/schedule")
def get_pipeline_schedule(
    pipeline_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _pipeline_or_404(db, pipeline_id, current_user)
    schedule = db.query(PipelineSchedule).filter(PipelineSchedule.pipeline_id == pipeline_id).first()
    return {"schedule": _schedule_payload(schedule)}


@router.put("/{pipeline_id}/schedule", dependencies=[Depends(require_editor)])
def upsert_pipeline_schedule(
    pipeline_id: str,
    body: PipelineScheduleUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor),
):
    pipeline = _pipeline_or_404(db, pipeline_id, current_user, write=True)
    current_schedule = db.query(PipelineSchedule).filter(
        PipelineSchedule.pipeline_id == pipeline_id
    ).with_for_update().first()
    cron = CronService()
    try:
        cron.parse_cron(body.cron_expression)
        computed_next = cron.next_fire_utc(body.cron_expression, body.timezone, datetime.now(timezone.utc))
        next_fire_at = computed_next if body.enabled else None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.misfire_policy not in {"skip", "run_once"}:
        raise HTTPException(422, "misfire_policy 只能是 skip 或 run_once")
    if body.overlap_policy not in {"forbid", "allow"}:
        raise HTTPException(422, "overlap_policy 只能是 forbid 或 allow")
    if body.misfire_grace_seconds < 0 or body.misfire_grace_seconds > 604800:
        raise HTTPException(422, "misfire_grace_seconds 必须在 0 到 604800 之间")

    version_query = db.query(PipelineVersion).filter(
        PipelineVersion.pipeline_id == pipeline_id,
        PipelineVersion.status == "published",
        PipelineVersion.execution_snapshot.is_not(None),
    )
    if body.pipeline_version_id:
        version = version_query.filter(PipelineVersion.id == body.pipeline_version_id).first()
    else:
        version = version_query.order_by(PipelineVersion.version.desc()).first()
    if not version:
        raise HTTPException(409, "定时运行必须固定一个已有的已发布 PipelineVersion")

    selected_principal_id = body.service_principal_id or (
        current_schedule.service_principal_id if current_schedule else None
    )
    if not selected_principal_id:
        raise HTTPException(422, "请先选择已登记且已授权此 Pipeline Run 的服务主体")
    principal = db.query(ServicePrincipal).filter(
        ServicePrincipal.id == selected_principal_id,
        ServicePrincipal.status == "active",
    ).with_for_update().first()
    if not principal:
        raise HTTPException(409, "服务主体不存在或已暂停")
    grant = db.query(ServicePrincipalGrant).filter(
        ServicePrincipalGrant.service_principal_id == selected_principal_id,
        ServicePrincipalGrant.resource_type == "pipeline",
        ServicePrincipalGrant.resource_id == pipeline_id,
        ServicePrincipalGrant.permission == "run",
        ServicePrincipalGrant.active.is_(True),
    ).with_for_update().first()
    if not grant:
        raise HTTPException(403, "服务主体缺少此 Pipeline 的精确 Run 授权；请管理员先授予")

    schedule = current_schedule
    if schedule is None:
        if body.expected_revision not in (None, 0):
            raise HTTPException(409, "Schedule revision 已变化，请重新读取后再保存")
        schedule = PipelineSchedule(
            pipeline_id=pipeline_id,
            owner_id=pipeline.created_by or current_user.id,
            steward_id=current_user.id,
            service_principal_id=selected_principal_id,
            pipeline_version_id=version.id,
            cron_expression=body.cron_expression.strip(),
            timezone=body.timezone,
            enabled=body.enabled,
            revision=1,
            misfire_policy=body.misfire_policy,
            misfire_grace_seconds=body.misfire_grace_seconds,
            overlap_policy=body.overlap_policy,
            next_fire_at=next_fire_at,
        )
        db.add(schedule)
        db.flush()
    else:
        if body.expected_revision is not None and body.expected_revision != schedule.revision:
            raise HTTPException(409, "Schedule revision 已变化，请重新读取后再保存")
        _cancel_pending_schedule_runs(db, schedule, "Schedule 已更新，待执行 tick 已取消")
        schedule.revision += 1
        schedule.steward_id = current_user.id
        schedule.service_principal_id = selected_principal_id
        schedule.pipeline_version_id = version.id
        schedule.cron_expression = body.cron_expression.strip()
        schedule.timezone = body.timezone
        schedule.enabled = body.enabled
        schedule.misfire_policy = body.misfire_policy
        schedule.misfire_grace_seconds = body.misfire_grace_seconds
        schedule.overlap_policy = body.overlap_policy
        schedule.next_fire_at = next_fire_at
        schedule.updated_at = datetime.now(timezone.utc)

    revision = _record_schedule_revision(db, schedule, actor_id=current_user.id)
    db.commit()
    db.refresh(schedule)
    db.refresh(revision)
    return {"schedule": _schedule_payload(schedule), "revision_id": revision.id}


@router.delete("/{pipeline_id}/schedule", dependencies=[Depends(require_editor)])
def disable_pipeline_schedule(
    pipeline_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor),
):
    _pipeline_or_404(db, pipeline_id, current_user, write=True)
    schedule = db.query(PipelineSchedule).filter(
        PipelineSchedule.pipeline_id == pipeline_id
    ).with_for_update().first()
    if not schedule:
        return {"status": "not_found"}
    if schedule.enabled:
        _cancel_pending_schedule_runs(db, schedule, "Schedule 已停用")
        schedule.enabled = False
        schedule.revision += 1
        schedule.next_fire_at = None
        schedule.updated_at = datetime.now(timezone.utc)
        revision = _record_schedule_revision(db, schedule, actor_id=current_user.id)
        db.commit()
        db.refresh(revision)
    return {"status": "disabled", "schedule": _schedule_payload(schedule)}


@router.get("/{pipeline_id}/schedule/runs")
def list_pipeline_schedule_runs(
    pipeline_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _pipeline_or_404(db, pipeline_id, current_user)
    schedule = db.query(PipelineSchedule).filter(PipelineSchedule.pipeline_id == pipeline_id).first()
    if not schedule:
        return []
    rows = db.query(PipelineScheduleRun).filter(
        PipelineScheduleRun.schedule_id == schedule.id
    ).order_by(PipelineScheduleRun.scheduled_for.desc()).limit(limit).all()
    return [
        {
            "id": row.id,
            "schedule_revision_id": row.schedule_revision_id,
            "scheduled_for": row.scheduled_for.isoformat(),
            "local_slot": row.local_slot,
            "status": row.status,
            "pipeline_run_id": row.pipeline_run_id,
            "coalesced_count": row.coalesced_count,
            "dispatch_attempts": row.dispatch_attempts,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


@router.post("/{pipeline_id}/schedule/runs/{schedule_run_id}/cancel", dependencies=[Depends(require_editor)])
def cancel_pipeline_schedule_run(
    pipeline_id: str,
    schedule_run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor),
):
    _pipeline_or_404(db, pipeline_id, current_user, write=True)
    schedule_run = db.query(PipelineScheduleRun).join(PipelineSchedule).filter(
        PipelineScheduleRun.id == schedule_run_id,
        PipelineSchedule.pipeline_id == pipeline_id,
    ).with_for_update(of=PipelineScheduleRun).first()
    if not schedule_run:
        raise HTTPException(404, "Schedule Run not found")
    if schedule_run.status in {"success", "degraded", "failed", "skipped", "cancelled"}:
        return {"id": schedule_run.id, "status": schedule_run.status}
    pipeline_run = None
    if schedule_run.pipeline_run_id:
        pipeline_run = db.query(PipelineRun).filter(
            PipelineRun.id == schedule_run.pipeline_run_id
        ).with_for_update().first()
    if pipeline_run and pipeline_run.status == "running":
        raise HTTPException(409, "PipelineRun 已开始执行；当前运行器不支持中断正在运行的任务")
    if pipeline_run and pipeline_run.status in {"success", "degraded", "failed", "cancelled"}:
        schedule_run.status = pipeline_run.status
        schedule_run.started_at = pipeline_run.started_at
        schedule_run.finished_at = pipeline_run.finished_at
        db.commit()
        return {"id": schedule_run.id, "status": schedule_run.status}
    if pipeline_run and pipeline_run.status == "pending":
        pipeline_run.status = "cancelled"
        pipeline_run.dispatch_status = "cancelled"
        pipeline_run.dispatch_claim_token = None
        pipeline_run.dispatch_lease_until = None
        pipeline_run.finished_at = datetime.now(timezone.utc)
        pipeline_run.error_log = "Schedule Run cancelled by user"
    schedule_run.status = "cancelled"
    schedule_run.error_code = "CANCELLED_BY_USER"
    schedule_run.error_message = "已由用户取消"
    schedule_run.finished_at = datetime.now(timezone.utc)
    schedule_run.claim_token = None
    schedule_run.lease_until = None
    db.commit()
    return {"id": schedule_run.id, "status": schedule_run.status}


@router.get("/{pipeline_id}/runs")
def list_runs(pipeline_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _pipeline_or_404(db, pipeline_id, current_user)
    runs = db.query(PipelineRun).filter(PipelineRun.pipeline_id == pipeline_id).order_by(PipelineRun.created_at.desc()).all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "dispatch_status": r.dispatch_status,
            "dispatch_attempts": r.dispatch_attempts,
            "execution_mode": r.execution_mode,
            "execution_digest": r.execution_digest,
            "pipeline_version_id": r.pipeline_version_id,
            "service_principal_id": r.service_principal_id,
            "source_versions": _run_source_versions(r.execution_manifest),
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in runs
    ]


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    run = db.query(PipelineRun).filter(PipelineRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    _pipeline_or_404(db, run.pipeline_id, current_user)
    invocations = db.query(ModelInvocation).filter(
        ModelInvocation.pipeline_run_id == run.id,
    ).order_by(ModelInvocation.attempt_number.desc(), ModelInvocation.created_at.desc()).limit(100).all()
    invocations.reverse()
    invocation_ids = [item.id for item in invocations]
    event_rows = db.query(PipelineLLMInvocationEvent).filter(
        PipelineLLMInvocationEvent.invocation_id.in_(invocation_ids),
    ).order_by(
        PipelineLLMInvocationEvent.invocation_id.asc(),
        PipelineLLMInvocationEvent.sequence.asc(),
    ).all() if invocation_ids else []
    events_by_invocation = {}
    for event_row in event_rows:
        events_by_invocation.setdefault(event_row.invocation_id, []).append(event_row)
    raw_model_pin = (run.execution_manifest or {}).get("llm_model_pin")
    model_pin = None
    if isinstance(raw_model_pin, dict):
        model_pin = {
            key: raw_model_pin.get(key)
            for key in (
                "requested", "model_config_id", "model_config_digest", "provider", "model_name",
                "prompt_provenance", "provider_model_artifact_reproducibility",
            )
        }
    return {
        "id": run.id,
        "status": run.status,
        "dispatch_status": run.dispatch_status,
        "dispatch_attempts": run.dispatch_attempts,
        "stats": run.stats,
        "error_log": run.error_log,
        "execution_mode": run.execution_mode,
        "execution_digest": run.execution_digest,
        "pipeline_version_id": run.pipeline_version_id,
        "service_principal_id": run.service_principal_id,
        "source_versions": _run_source_versions(run.execution_manifest),
        "llm_model_pin": model_pin,
        "llm_invocations": [{
            "id": item.id,
            "attempt_number": item.attempt_number,
            "source_dataset_version_id": item.source_dataset_version_id,
            "phase": item.phase,
            "provider": item.provider,
            "model_name": item.model_name,
            "model_config_id": item.model_config_id,
            "model_config_digest": item.model_config_digest,
            "prompt_id": (item.metadata_json or {}).get("prompt_id"),
            "prompt_digest": (item.metadata_json or {}).get("prompt_digest"),
            "request_hash": item.request_hash,
            "response_hash": item.response_hash,
            "status": item.status,
            "error_code": item.error,
            "duration_ms": item.duration_ms,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "completed_at": item.completed_at.isoformat() if item.completed_at else None,
            "provider_model_artifact_reproducibility": (item.metadata_json or {}).get(
                "provider_model_artifact_reproducibility", "unverified",
            ),
            "request_payload_persisted": False,
            "response_payload_persisted": False,
            "event_chain_status": (
                "unavailable" if not events_by_invocation.get(item.id)
                else "verified" if verify_pipeline_invocation_event_chain(events_by_invocation[item.id])
                else "invalid"
            ),
            "events": [{
                "id": event_row.id,
                "sequence": event_row.sequence,
                "event_type": event_row.event_type,
                "status": event_row.status,
                "error_code": event_row.error_code,
                "request_hash": event_row.request_hash,
                "response_hash": event_row.response_hash,
                "previous_digest": event_row.previous_digest,
                "event_digest": event_row.event_digest,
                "occurred_at": event_row.occurred_at.isoformat() if event_row.occurred_at else None,
            } for event_row in events_by_invocation.get(item.id, [])],
        } for item in invocations],
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


@router.post("/{pipeline_id}/run-sync")
def run_pipeline_sync(
    pipeline_id: str,
    db: Session = Depends(get_db),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(require_editor),
):
    """同步执行 Pipeline（无需 Celery/Redis，适用于开发/测试）"""
    _pipeline_or_404(db, pipeline_id, current_user, write=True)
    run, _created = _create_pipeline_run(
        db, pipeline_id, trigger_mode="sync", dispatch_status="not_required",
        idempotency_key=idempotency_key,
        request_actor_id=getattr(current_user, "id", None),
    )
    try:
        if run.status == "pending":
            from app.tasks.v2.pipeline_run import pipeline_run_task
            pipeline_run_task(pipeline_id, run.id)
            db.refresh(run)
        return {
            "run_id": run.id,
            "status": run.status,
            "dispatch_status": run.dispatch_status,
            "stats": run.stats,
            "error": run.error_log,
            "execution_mode": run.execution_mode,
            "execution_digest": run.execution_digest,
        }
    except Exception as e:
        run.status = "failed"
        run.dispatch_status = "failed"
        run.error_log = str(e)
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return {"run_id": run.id, "status": "failed", "error": str(e)}


def _run_source_versions(manifest: dict | None) -> list[dict]:
    if not isinstance(manifest, dict):
        return []
    return [
        {
            "dataset_id": source.get("dataset_id"),
            "dataset_version_id": source.get("dataset_version_id"),
            "version_no": source.get("version_no"),
            "checksum": source.get("checksum"),
        }
        for source in manifest.get("sources", [])
        if isinstance(source, dict)
    ]


class PreviewStepBody(BaseModel):
    op: str
    params: dict = {}
    sample_data: list[dict] = []


@router.post("/preview-step")
def preview_step(body: PreviewStepBody):
    """预览某个 Transform 步骤的输出"""
    try:
        from app.services.v2.pipeline.steps.cleansing import CleansingStep
        from app.services.v2.pipeline.steps.schema_inference import SchemaInferenceStep
        from app.services.v2.pipeline.base import PipelineContext

        ctx = PipelineContext(dataset_id="", version_no=1, route="A", spec={})
        data = body.sample_data or [{"col": "sample"}]

        if body.op in ("drop_duplicates", "fill_nulls", "normalize_dates"):
            step = CleansingStep()
            data = step.run(ctx, data)
        elif body.op == "schema_inference":
            step = SchemaInferenceStep()
            data = step.run(ctx, data)

        return {"op": body.op, "rows_in": len(body.sample_data), "rows_out": len(data), "preview": data[:20]}
    except Exception as e:
        return {"op": body.op, "error": str(e), "rows_in": 0, "rows_out": 0, "preview": []}


# ── Helper ────────────────────────────────────────────────────────

def _format_pipeline(pl: Pipeline) -> dict:
    return {
        "id": pl.id,
        "name": pl.name,
        "domain": pl.domain or "通用",
        "description": pl.description or "",
        "source_dataset_id": pl.source_dataset_id,
        "route": pl.route,
        "spec": pl.spec or {},
        "definition": pl.definition,
        "status": pl.status or "draft",
        "branch": pl.branch or "main",
        "version": pl.version or 1,
        "target_curated_ids": pl.target_curated_ids or [],
        "created_at": pl.created_at.isoformat() if pl.created_at else None,
        "updated_at": pl.updated_at.isoformat() if pl.updated_at else None,
    }
