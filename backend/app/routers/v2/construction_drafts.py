"""Five-step draft/preflight/mapping API shared by regular and multimodal flows."""
from __future__ import annotations

import json
import uuid
import hashlib
import base64
import io
import os
import time
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_user, require_editor
from app.models.user import User
from app.models.model_config import ModelConfig
from app.models.ontology import OntologyProject
from app.models.ontology_revision import OntologyRevision
from app.models.v2.construction import ConstructionRun
from app.models.v2.construction_draft import ConstructionDraft
from app.models.v2.construction_review import ConstructionReview, ConstructionReviewEvent
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.workbench_task import ConstructionMappingAttemptEvent, MappingTask, ModelInvocation
from app.services.model_config_selector import llm_call_kwargs
from app.services import encryption_service
from app.services.v2.construction_service import create_run, serialize_run, update_run, add_evidence
from app.services.v2.construction_reviews import append_review_event
from app.services.v2.construction_review_diff import (
    candidate_schema_diff,
    construction_impact_preview,
    live_schema_snapshot,
    schema_diff,
    schema_snapshot_digest,
)
from app.services.v2.dataset_authorization import require_dataset_access
from app.services.v2.construction_mapping_events import (
    append_mapping_attempt_event,
    next_mapping_attempt_number,
    verify_mapping_attempt_event_chain,
)
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError
from app.services.v2.ontology_materializer import (
    attach_revision_to_materialized_rows,
    mapping_suggestions,
    materialize_ontology,
    normalise_mapping,
)

router = APIRouter(prefix="/construction/drafts", dependencies=[Depends(get_current_user)])
review_router = APIRouter(prefix="/construction/reviews", dependencies=[Depends(get_current_user)])
mapping_tasks_router = APIRouter(dependencies=[Depends(get_current_user)])
MODEL_NAME = "MiniMax-M3"
SOURCE_ANALYSIS_SYSTEM_PROMPT = "只返回 JSON。你是本体构筑的数据读取规划器。根据来源档案和表头，返回 {items:[{kind,fields?,sample_ids?,asset_roles?,max_rows,reason}],summary}。只能请求已选范围内的数据；不要输出隐藏推理、不要生成本体。"
ONTOLOGY_MAPPING_SYSTEM_PROMPT = "只返回 JSON。基于已发送数据生成 ontology-mapping-v2：{schema_version:'ontology-mapping-v2',entity_types:[{id,name,name_cn?,name_en?,description,source_fields,identifier_property,confidence}],properties:[{id,entity_type,name,type,is_identifier?,unit?,values?,description?,source_field,confidence}],relationships:[{id,name,from,to,cardinality,description,attributes?,source_fields?,confidence}],logic_rules:[{id,name,name_cn?,description,if,then,linked_entities,confidence,evidence}]}. cardinality 优先使用 one-to-one、one-to-many、many-to-one、many-to-many；也接受 UML 简写 1:1、1:N、N:1、M:N 及 0..1:1 等可选基数，系统会保留原写法并规范化。关系必须连接已有实体；规则必须有 IF 和 THEN，且只能基于已发送字段/样例提出。不要生成配置项、隐私策略或无来源的业务实体。"
MAPPING_PARSER_VERSION = "llm_service.parse_response/json-object-v1"
MAPPING_NORMALIZER_VERSION = "ontology-mapping-v2"


_SECRET_SOURCE_KEYS = {"password", "passwd", "pwd", "token", "api_key", "apikey", "access_token", "signature", "sig", "secret"}


class MappingResponseValidationError(ValueError):
    code = "mapping_response_invalid"


_MAPPING_ERROR_MESSAGES = {
    "mapping_input_manifest_missing": "任务缺少固定输入清单，请重新预检并创建映射任务。",
    "mapping_input_pin_mismatch": "任务输入版本不一致，请重新预检并创建映射任务。",
    "mapping_input_changed": "任务输入或配置已变化，请保留历史并创建新的映射任务。",
    "mapping_source_unavailable": "任务固定的数据版本不可用，请重新选择数据版本后创建任务。",
    "mapping_draft_unavailable": "构筑草案不可用，请检查草案状态后重试。",
    "model_unavailable": "模型当前不可用，请检查模型配置后重试。",
    "mapping_response_invalid": "模型返回结果未通过映射结构校验，请调整输入后重试。",
    "mapping_dispatch_failed": "映射任务未能派发，请稍后重试。",
    "mapping_attempt_failed": "映射任务执行失败，请检查安全事件摘要后重试。",
}


def _mapping_failure_code(exc: Exception) -> str:
    """Classify failures without persisting provider or exception text."""
    if isinstance(exc, MappingResponseValidationError):
        return "mapping_response_invalid"
    message = str(exc)
    if "MiniMax M3" in message:
        return "model_unavailable"
    if message.startswith("MAPPING_INPUT_MANIFEST_MISSING:"):
        return "mapping_input_manifest_missing"
    if message.startswith("MAPPING_INPUT_PIN_MISMATCH:"):
        return "mapping_input_pin_mismatch"
    if message.startswith("MAPPING_INPUT_CHANGED:"):
        return "mapping_input_changed"
    if message.startswith("MAPPING_SOURCE_VERSION_MISSING:"):
        return "mapping_source_unavailable"
    if message == "构筑草案不存在":
        return "mapping_draft_unavailable"
    return "mapping_attempt_failed"


def _mapping_error_message(error_code: str | None, *, status: str | None = None) -> str | None:
    if error_code in _MAPPING_ERROR_MESSAGES:
        return _MAPPING_ERROR_MESSAGES[error_code]
    if status == "waiting_for_model":
        return _MAPPING_ERROR_MESSAGES["model_unavailable"]
    if status == "failed":
        return _MAPPING_ERROR_MESSAGES["mapping_attempt_failed"]
    if status == "cancelled":
        return "映射任务已取消。"
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _safe_source_locator(value: str | None) -> str | None:
    """Keep a meaningful source address while never serialising credentials."""
    if not value:
        return value
    text = str(value)
    try:
        parsed = urlsplit(text)
        if parsed.scheme:
            username = parsed.username
            host = parsed.hostname or ""
            netloc = host
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            if username:
                netloc = f"{username}@{netloc}"
            query = urlencode([(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.casefold() not in _SECRET_SOURCE_KEYS])
            return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except Exception:
        pass
    # DSN-ish strings and local paths: retain the locator but mask common
    # password segments before the planning request/log is persisted.
    return re.sub(r"(?i)(password|passwd|pwd|token|api[_-]?key|secret)\s*=\s*[^\s;,&]+", r"\1=***", text)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_ontology_scope(db: Session, ontology_id: str, user: User) -> None:
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise HTTPException(404, "本体不存在或当前用户无权访问") from None


def _require_draft_access(db: Session, draft: ConstructionDraft, user: User) -> None:
    """Authorize draft data through its live ontology policy or creator scope."""
    if user.role == "admin":
        return
    require_dataset_access(db, str(draft.dataset_id), user)
    if draft.ontology_id:
        _require_ontology_scope(db, str(draft.ontology_id), user)
        return
    # Create-mode drafts have no ontology policy yet. Only their recorded
    # creator may access them; legacy ownerless rows are deliberately hidden.
    if draft.created_by and str(draft.created_by) == str(user.id):
        return
    raise HTTPException(404, "构筑草案不存在或当前用户无权访问")


def _assert_draft_editable(draft: ConstructionDraft) -> None:
    if draft.status in {"pending_review", "building", "built"} or draft.build_run_id:
        raise HTTPException(409, "草案已提交审核或构建，输入已冻结；请等待审核结果或创建新草案")


class DraftCreate(BaseModel):
    dataset_id: str
    ontology_id: str | None = None
    data_class: str = Field(default="regular", pattern="^(regular|temporal|multimodal)$")
    target_mode: str | None = Field(default=None, pattern="^(create|append)$")
    new_ontology_name: str | None = Field(default=None, max_length=200)
    new_ontology_domain: str | None = Field(default=None, max_length=100)
    privacy_level: str = Field(default="standard", pattern="^(standard|private)$")
    selection: dict[str, Any] = Field(default_factory=dict)
    processing_config: dict[str, Any] = Field(default_factory=dict)


class DraftPatch(BaseModel):
    data_class: str | None = Field(default=None, pattern="^(regular|temporal|multimodal)$")
    privacy_level: str | None = Field(default=None, pattern="^(standard|private)$")
    selection: dict[str, Any] | None = None
    processing_config: dict[str, Any] | None = None
    mapping: dict[str, Any] | None = None
    ontology_id: str | None = None
    target_mode: str | None = Field(default=None, pattern="^(create|append)$")
    new_ontology_name: str | None = Field(default=None, max_length=200)
    new_ontology_domain: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, pattern="^(draft|preflight_ready|mapping_ready|pending_review|building|built|waiting_for_model|failed|rejected)$")


class BuildRequest(BaseModel):
    mapping_confirmed: bool = False
    mode: str = Field(default="create", pattern="^(create|append)$")
    model_id: str | None = None


class ConstructionReviewDecision(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|changes_requested)$")
    note: str | None = Field(default=None, max_length=2000)


def _confirmed_mapping_payload(mapping_state: dict[str, Any]) -> dict[str, Any]:
    """Apply the user's explicit mapping-card selection to the canonical map."""
    canonical = mapping_state.get("ontology_mapping")
    if not isinstance(canonical, dict):
        return mapping_state

    suggestions = mapping_state.get("suggestions")
    confirmed = mapping_state.get("confirmed")
    if not isinstance(suggestions, list) or not suggestions:
        raise HTTPException(409, "映射草案缺少可确认的候选清单；请重新生成映射")
    if not isinstance(confirmed, list) or not confirmed:
        raise HTTPException(409, "请至少选择一项映射建议后再构建")

    allowed_refs = {
        str(item.get("mapping_ref"))
        for item in suggestions
        if isinstance(item, dict) and item.get("mapping_ref")
    }
    selected_refs: set[str] = set()
    for item in confirmed:
        reference = item.get("mapping_ref") if isinstance(item, dict) else None
        if not reference or str(reference) not in allowed_refs:
            raise HTTPException(409, "已确认的映射项与当前候选清单不一致；请重新选择")
        selected_refs.add(str(reference))

    filtered = dict(canonical)
    sections = {
        "entity_types": "entity_type:",
        "properties": "property:",
        "relationships": "relationship:",
        "logic_rules": "logic_rule:",
    }
    for section, prefix in sections.items():
        filtered[section] = [
            item for item in canonical.get(section, [])
            if isinstance(item, dict) and f"{prefix}{item.get('id')}" in selected_refs
        ]
    return filtered


def serialize_draft(draft: ConstructionDraft) -> dict[str, Any]:
    draft_error = draft.error
    if draft.mapping_task_id and draft.status in {"failed", "waiting_for_model"}:
        # Older MappingTask failures may have stored raw provider exceptions.
        draft_error = _mapping_error_message(None, status=draft.status)
    return {
        "id": draft.id, "dataset_id": draft.dataset_id,
        "dataset_version_id": draft.dataset_version_id,
        "ontology_id": draft.ontology_id,
        "data_class": draft.data_class, "privacy_level": draft.privacy_level, "status": draft.status,
        "target_mode": draft.target_mode, "new_ontology_name": draft.new_ontology_name,
        "new_ontology_domain": draft.new_ontology_domain, "mapping_task_id": draft.mapping_task_id,
        "selection": draft.selection_json or {}, "processing_config": draft.processing_json or {},
        "preflight": draft.preflight_json or {}, "mapping": draft.mapping_json or {},
        "build_run_id": draft.build_run_id, "error": draft_error,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
    }


def _review_mapping_summary(mapping: dict[str, Any]) -> dict[str, Any]:
    """Expose schema-level mapping details without source sample values."""
    def pick(rows: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        return [
            {key: row.get(key) for key in fields if key in row}
            for row in rows if isinstance(row, dict)
        ]

    return {
        "entity_types": pick(
            mapping.get("entity_types"),
            ("id", "name", "name_cn", "name_en", "description", "source_fields", "identifier_property"),
        ),
        "properties": pick(
            mapping.get("properties"),
            ("id", "entity_type", "name", "type", "source_field", "is_identifier", "is_required", "unit"),
        ),
        "relationships": pick(
            mapping.get("relationships"),
            ("id", "name", "from", "to", "cardinality", "source_fields"),
        ),
        "logic_rules": pick(
            mapping.get("logic_rules"),
            ("id", "name", "name_cn", "description", "linked_entities", "if", "then"),
        ),
    }


def _current_revision_id(db: Session, ontology_id: str | None) -> str | None:
    if not ontology_id:
        return None
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise HTTPException(404, "目标本体不存在")
    if project.current_revision_id:
        revision = db.query(OntologyRevision).filter(
            OntologyRevision.id == project.current_revision_id,
            OntologyRevision.ontology_id == ontology_id,
        ).first()
        if not revision or not revision.is_current:
            raise HTTPException(409, "目标本体当前修订指针无效，拒绝提交审阅")
        return revision.id
    revision = db.query(OntologyRevision).filter(
        OntologyRevision.ontology_id == ontology_id,
        OntologyRevision.is_current.is_(True),
    ).order_by(OntologyRevision.revision_no.desc()).first()
    return revision.id if revision else None


def _prepare_build_review(db: Session, draft: ConstructionDraft, body: BuildRequest) -> dict[str, Any]:
    if draft.data_class not in {"regular", "multimodal"}:
        raise HTTPException(409, "该数据类型请使用对应的 Temporal 构建流程")
    if body.mode != draft.target_mode:
        raise HTTPException(409, "构建目标与草案目标不一致，请刷新草案后重试")

    mapping_state = draft.mapping_json or {}
    mapping_payload = _confirmed_mapping_payload(mapping_state)
    validation = normalise_mapping(mapping_payload, data_class=draft.data_class)
    if not body.mapping_confirmed or not mapping_state.get("suggestions"):
        raise HTTPException(409, "请先确认本体映射建议")
    if validation.errors:
        raise HTTPException(409, "本体映射不可构建：" + "；".join(validation.errors))
    if not validation.mapping.get("relationships"):
        raise HTTPException(409, "映射至少需要保留一条关系")
    for entity in validation.mapping.get("entity_types", []):
        has_identifier = any(
            prop.get("entity_type") == entity.get("id") and prop.get("is_identifier")
            for prop in validation.mapping.get("properties", [])
        )
        if not has_identifier:
            raise HTTPException(409, f"实体类型「{entity.get('name_cn') or entity.get('name')}」需要至少一个已确认的标识属性")

    dataset, source_version = _pin_draft_dataset_version(db, draft)
    _validate_target(
        db, dataset=dataset, data_class=draft.data_class, target_mode=draft.target_mode,
        ontology_id=draft.ontology_id, new_ontology_name=draft.new_ontology_name,
    )
    if source_version is None or not source_version.checksum:
        raise HTTPException(409, "固定的数据版本缺少内容校验和，拒绝提交发布审阅")
    mapping_task = db.query(MappingTask).filter(
        MappingTask.id == draft.mapping_task_id,
        MappingTask.draft_id == draft.id,
        MappingTask.status == "completed",
    ).first() if draft.mapping_task_id else None
    if not mapping_task or not mapping_task.payload_hash or not mapping_task.input_manifest_json:
        raise HTTPException(409, "映射任务缺少完整输入清单；请重新生成并确认映射")

    source_manifest = (mapping_task.input_manifest_json or {}).get("dataset") or {}
    schema_digest = _stable_digest(dataset.schema_json if isinstance(dataset.schema_json, dict) else {})
    if (
        mapping_task.source_version_id != source_version.id
        or source_manifest.get("dataset_version_id") != source_version.id
        or source_manifest.get("checksum") != source_version.checksum
        or source_manifest.get("schema_digest") != schema_digest
    ):
        raise HTTPException(409, "映射建议绑定的数据/结构版本已变化；请重新生成并确认映射")

    project = None
    base_revision_id = None
    if draft.target_mode == "create":
        name = (draft.new_ontology_name or "").strip()
        if db.query(OntologyProject.id).filter(OntologyProject.name.ilike(name)).first():
            raise HTTPException(409, f"Ontology 名称「{name}」已存在")
    else:
        project = db.query(OntologyProject).filter(OntologyProject.id == draft.ontology_id).first()
        if not project:
            raise HTTPException(404, "目标本体不存在")
        base_revision_id = _current_revision_id(db, project.id)

    base_revision = None
    current_snapshot: dict[str, Any] | None = None
    if project:
        if base_revision_id:
            base_revision = db.query(OntologyRevision).filter(
                OntologyRevision.id == base_revision_id,
                OntologyRevision.ontology_id == project.id,
            ).first()
            if not base_revision or not isinstance(base_revision.snapshot_json, dict):
                raise HTTPException(409, "目标本体基准修订缺少可用快照，拒绝提交审阅")
            current_snapshot = base_revision.snapshot_json
            if schema_snapshot_digest(live_schema_snapshot(db, project.id)) != schema_snapshot_digest(current_snapshot):
                raise HTTPException(409, "本体现行结构与基准修订不一致；请先恢复或发布修订后再提交审阅")
        else:
            current_snapshot = live_schema_snapshot(db, project.id)

    _projected_snapshot, schema_diff_preview = candidate_schema_diff(
        current_snapshot,
        validation.mapping,
        ontology_id=project.id if project else None,
        data_class=draft.data_class,
    )
    schema_diff_preview["basis"] = "pinned_ontology_revision" if base_revision_id else "pinned_live_schema"
    impact_preview = construction_impact_preview(
        db,
        project.id if project else None,
        current_snapshot,
        _projected_snapshot,
        schema_diff_preview,
    )
    model_manifest = (mapping_task.input_manifest_json or {}).get("model_config") or {}
    llm_contract = (mapping_task.input_manifest_json or {}).get("llm_contract") or {}
    manifest = {
        "draft_id": draft.id,
        "target_mode": draft.target_mode,
        "ontology_id": draft.ontology_id,
        "base_revision_id": base_revision_id,
        "dataset_id": dataset.id,
        "dataset_version_id": source_version.id,
        "dataset_version_no": source_version.version_no,
        "dataset_checksum": source_version.checksum,
        "dataset_schema_digest": schema_digest,
        "selection_digest": _stable_digest(draft.selection_json or {}),
        "processing_digest": _stable_digest(draft.processing_json or {}),
        "mapping_task_id": mapping_task.id,
        "mapping_task_input_digest": mapping_task.payload_hash,
        "mapping_task_schema_version": mapping_task.schema_version,
        "mapping_digest": _stable_digest(validation.mapping),
        "privacy_level": draft.privacy_level,
        "data_class": draft.data_class,
        "requested_build_mode": body.mode,
        "requested_model_id": body.model_id,
        "base_revision_snapshot_hash": base_revision.snapshot_hash if base_revision else None,
        "base_schema_digest": schema_diff_preview["base_schema_digest"],
        "projected_schema_digest": schema_diff_preview["projected_schema_digest"],
        "schema_diff": schema_diff_preview,
        "impact_preview": impact_preview,
    }
    candidate_digest = _stable_digest(manifest)
    candidate = {
        "manifest": manifest,
        "mapping_summary": _review_mapping_summary(validation.mapping),
        "lineage": {
            "model_config_id": model_manifest.get("config_id"),
            "model_provider": model_manifest.get("provider"),
            "model_names": model_manifest.get("models") or [],
            "model_config_digest": model_manifest.get("config_digest"),
            "credential_fingerprint": model_manifest.get("credential_fingerprint"),
            "model_artifact_reproducibility": model_manifest.get("provider_model_artifact_reproducibility"),
            "prompt_contract": llm_contract,
            "mapping_attempt_input_digest": mapping_task.payload_hash,
        },
    }
    mapping_summary = candidate["mapping_summary"]
    impact = {
        "preview_kind": "projected_schema_and_downstream_impact",
        "note": "以下结构与下游影响由固定基准修订和已确认映射推演；审核及物化时会重新核对。",
        "base_revision_id": base_revision_id,
        "target_mode": draft.target_mode,
        "schema_diff_digest": schema_diff_preview["digest"],
        "impact_preview": impact_preview,
        "current_counts": {
            "entity_types": schema_diff_preview["current_counts"].get("entity_types", 0),
            "properties": schema_diff_preview["current_counts"].get("properties", 0),
            "relations": schema_diff_preview["current_counts"].get("relationships", 0),
            "logic_rules": schema_diff_preview["current_counts"].get("logic_rules", 0),
        },
        "proposed_counts": {
            "entity_types": schema_diff_preview["proposed_counts"].get("entity_types", 0),
            "properties": schema_diff_preview["proposed_counts"].get("properties", 0),
            "relationships": schema_diff_preview["proposed_counts"].get("relationships", 0),
            "logic_rules": schema_diff_preview["proposed_counts"].get("logic_rules", 0),
        },
        "canonical_write_before_approval": False,
    }
    return {
        "dataset": dataset,
        "source_version": source_version,
        "mapping_task": mapping_task,
        "mapping": validation.mapping,
        "manifest": manifest,
        "candidate": candidate,
        "candidate_digest": candidate_digest,
        "impact": impact,
    }


def _serialize_review_event(item: ConstructionReviewEvent) -> dict[str, Any]:
    return {
        "id": item.id,
        "sequence_no": item.sequence_no,
        "actor_id": item.actor_id,
        "event_type": item.event_type,
        "status": item.status,
        "detail": item.detail_json or {},
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def serialize_construction_review(db: Session, review: ConstructionReview) -> dict[str, Any]:
    events = db.query(ConstructionReviewEvent).filter(
        ConstructionReviewEvent.review_id == review.id,
    ).order_by(ConstructionReviewEvent.sequence_no.asc()).all()
    return {
        "id": review.id,
        "draft_id": review.draft_id,
        "dataset_id": review.dataset_id,
        "dataset_version_id": review.dataset_version_id,
        "ontology_id": review.ontology_id,
        "base_revision_id": review.base_revision_id,
        "mapping_task_id": review.mapping_task_id,
        "construction_run_id": review.construction_run_id,
        "candidate_digest": review.candidate_digest,
        "candidate": review.candidate_json or {},
        "impact": review.impact_json or {},
        "status": review.status,
        "submitted_by": review.submitted_by,
        "reviewed_by": review.reviewed_by,
        "decision": review.decision,
        "decision_note": review.decision_note,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
        "events": [_serialize_review_event(item) for item in events],
    }


def _require_review_access(db: Session, review: ConstructionReview, user: User) -> None:
    require_dataset_access(db, review.dataset_id, user)
    if review.ontology_id and str(review.reviewed_by or "") != str(user.id):
        _require_ontology_scope(db, review.ontology_id, user)


def _deterministic_mapping(draft: ConstructionDraft, *, descriptor: dict[str, Any] | None = None) -> dict[str, Any]:
    dataset_name = str(((descriptor or {}).get("dataset") or {}).get("name") or "")
    if draft.data_class == "regular" and "c-mapss" in dataset_name.casefold():
        selection = draft.selection_json or {}
        fields = [str(field) for field in (selection.get("fields") or selection.get("columns") or (descriptor or {}).get("headers") or [])]
        equipment_fields = {"equipment_id", "equipment_type", "source_unit", "description"}

        def field_type(field: str) -> str:
            lower = field.casefold()
            if lower in {"cycle", "source_unit", "sensor_17", "sensor_18"}:
                return "integer"
            if lower.startswith("sensor_") or lower.startswith("op_setting_"):
                return "decimal"
            return "string"

        properties = []
        for field in fields:
            entity_type = "equipment" if field in equipment_fields else "sensor_reading"
            properties.append({
                "id": f"{entity_type}-{field}",
                "entity_type": entity_type,
                "name": field,
                "label": field,
                "type": field_type(field),
                "is_identifier": field in {"equipment_id", "reading_id"},
                "source_field": field,
                "confidence": 1.0,
            })
        return {
            "schema_version": "ontology-mapping-v2",
            "entity_types": [
                {
                    "id": "equipment",
                    "name": "Equipment",
                    "name_cn": "设备",
                    "name_en": "Equipment",
                    "description": "NASA C-MAPSS FD001 中由 equipment_id 标识的涡扇发动机设备。",
                    "source_fields": [field for field in fields if field in equipment_fields],
                    "identifier_property": "equipment_id",
                    "confidence": 1.0,
                },
                {
                    "id": "sensor_reading",
                    "name": "SensorReading",
                    "name_cn": "传感器读数",
                    "name_en": "SensorReading",
                    "description": "NASA C-MAPSS FD001 中每台设备每个 cycle 的真实运行和传感器读数。",
                    "source_fields": [field for field in fields if field not in equipment_fields],
                    "identifier_property": "reading_id",
                    "confidence": 1.0,
                },
            ],
            "properties": properties,
            "relationships": [{
                "id": "equipment-has-sensor-reading",
                "name": "HAS_SENSOR_READING",
                "from": "equipment",
                "to": "sensor_reading",
                "cardinality": "one-to-many",
                "description": "设备具有按 cycle 记录的传感器读数。",
                "source_fields": ["equipment_id"],
                "confidence": 1.0,
            }],
            "logic_rules": [{
                "id": "cmapss-cycle-ordinal",
                "name": "C-MAPSS 读数顺序",
                "name_cn": "C-MAPSS 读数顺序",
                "description": "cycle 仅表示同一设备内的来源顺序，不转换为日期。",
                "if": {"all": [{"entity": "sensor_reading", "property": "cycle", "operator": ">=", "value": 1}], "time_kind": "Ordinal"},
                "then": {"relation": "HAS_SENSOR_READING", "order_by": "cycle", "meaning": "同一设备内的相对顺序"},
                "linked_entities": ["equipment", "sensor_reading"],
                "evidence": {"source_fields": ["equipment_id", "reading_id", "cycle"]},
                "confidence": 1.0,
            }],
            "source": {"engine": "rules", "case": "cmapss_fd001"},
        }
    if draft.data_class == "temporal":
        suggestions = [
            {"kind": "class", "source": "episode_id", "target": "Episode", "confidence": 1.0, "extractor": "rule"},
            {"kind": "property", "source": "time_s / event_seq", "target": "Instant / Ordinal", "confidence": 1.0, "extractor": "rule"},
            {"kind": "relation", "source": "observation", "target": "episode", "target_relation": "HAS_OBSERVATION", "confidence": 1.0, "extractor": "rule"},
        ]
    elif draft.data_class == "multimodal":
        selected_assets = [str(value) for value in ((draft.selection_json or {}).get("selected_assets") or (draft.selection_json or {}).get("modalities") or [])]
        if not selected_assets:
            selected_assets = sorted({
                str(role)
                for sample in ((descriptor or {}).get("multimodal_catalog") or [])
                for role in (sample.get("modalities") or [])
                if role
            })
        return {
            "schema_version": "ontology-mapping-v2",
            "entity_types": [
                {"id": "scene", "name": "Scene", "name_cn": "场景", "name_en": "Scene", "description": "I-BADAS 采集场景。", "source_fields": ["scene_id"], "identifier_property": "scene_id", "confidence": 1.0},
                {"id": "multimodalsample", "name": "MultimodalSample", "name_cn": "多模态样例", "name_en": "MultimodalSample", "description": "由官方 sample_key 标识的 I-BADAS 多模态样例。", "source_fields": ["sample_key", "scene_id", "label"], "identifier_property": "sample_key", "confidence": 1.0},
                {"id": "mediaasset", "name": "MediaAsset", "name_cn": "媒体资产", "name_en": "MediaAsset", "description": "与样例绑定的 RGB、深度、掩码、点云或元数据资产。", "source_fields": ["asset_role", "mime_type", "checksum", "source_path"], "identifier_property": "id", "confidence": 1.0},
                {"id": "anomalyevent", "name": "AnomalyEvent", "name_cn": "异常标注", "name_en": "AnomalyEvent", "description": "I-BADAS 官方提供的正常或异常标签。", "source_fields": ["label", "labels"], "identifier_property": "label", "confidence": 1.0},
            ],
            "properties": [
                {"id": "scene-id", "entity_type": "scene", "name": "scene_id", "type": "string", "is_identifier": True, "source_field": "scene_id", "confidence": 1.0},
                {"id": "sample-key", "entity_type": "multimodalsample", "name": "sample_key", "type": "string", "is_identifier": True, "source_field": "sample_key", "confidence": 1.0},
                {"id": "sample-label", "entity_type": "multimodalsample", "name": "label", "type": "string", "source_field": "label", "confidence": 1.0},
                {"id": "asset-id", "entity_type": "mediaasset", "name": "id", "type": "string", "is_identifier": True, "source_field": "id", "confidence": 1.0},
                {"id": "asset-role", "entity_type": "mediaasset", "name": "asset_role", "type": "string", "source_field": "asset_role", "values": selected_assets, "confidence": 1.0},
                {"id": "asset-checksum", "entity_type": "mediaasset", "name": "checksum", "type": "string", "source_field": "checksum", "confidence": 1.0},
                {"id": "anomaly-label", "entity_type": "anomalyevent", "name": "label", "type": "string", "is_identifier": True, "source_field": "label", "confidence": 1.0},
            ],
            "relationships": [
                {"id": "scene-has-sample", "name": "HAS_SAMPLE", "from": "scene", "to": "multimodalsample", "cardinality": "one-to-many", "description": "场景包含多模态样例。", "source_fields": ["scene_id"], "confidence": 1.0},
                {"id": "sample-has-asset", "name": "HAS_ASSET", "from": "multimodalsample", "to": "mediaasset", "cardinality": "one-to-many", "description": "样例包含关联媒体资产。", "source_fields": ["sample_id", "asset_role"], "confidence": 1.0},
                {"id": "sample-has-label", "name": "HAS_LABEL", "from": "multimodalsample", "to": "anomalyevent", "cardinality": "many-to-one", "description": "样例保留官方正常或异常标注。", "source_fields": ["label"], "confidence": 1.0},
            ],
            "logic_rules": [{
                "id": "ibadas-official-anomaly-label",
                "name": "I-BADAS 官方异常标注",
                "name_cn": "I-BADAS 官方异常标注",
                "description": "当来源标签为 anomaly 时，样例关联到对应的官方异常标注；不由模型生成标签。",
                "if": {"all": [{"entity": "multimodalsample", "property": "label", "operator": "=", "value": "anomaly"}]},
                "then": {"relation": "HAS_LABEL", "target_entity": "anomalyevent", "source": "official_label"},
                "linked_entities": ["multimodalsample", "anomalyevent"],
                "evidence": {"source_fields": ["label", "labels"], "authority": "I-BADAS official annotation"},
                "confidence": 1.0,
            }],
            "source": {"engine": "rules", "case": "multimodal_manifest"},
        }
    elif draft.data_class == "regular":
        # A private Dataset must be buildable without calling a cloud model.
        # Keep this fallback generic and explicit: it describes the
        # Dataset/Record structure and only includes fields selected in the
        # draft. It does not pretend to infer domain semantics.
        selection = draft.selection_json or {}
        profile = (descriptor or {}).get("header_profile") or []
        profile_by_name = {
            str(item.get("name")): item
            for item in profile
            if isinstance(item, dict) and item.get("name") is not None
        }
        fields = [
            str(field)
            for field in (
                selection.get("fields")
                or selection.get("columns")
                or (descriptor or {}).get("headers")
                or []
            )
        ]

        def field_type(field: str) -> str:
            samples = [
                value for value in (profile_by_name.get(field, {}).get("sample_values") or [])
                if value not in (None, "")
            ]
            if not samples:
                return "string"
            if all(isinstance(value, bool) for value in samples):
                return "boolean"
            if all(isinstance(value, int) and not isinstance(value, bool) for value in samples):
                return "integer"
            if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in samples):
                return "decimal"
            return "string"

        identifier_field = next(
            (
                field for field in fields
                if field.casefold() in {"id", "record_id", "row_id"}
                or field.casefold().endswith("_id")
            ),
            fields[0] if fields else None,
        )

        def field_property_id(index: int, field: str) -> str:
            field_slug = re.sub(r"[^a-z0-9]+", "-", field.casefold()).strip("-")
            if not field_slug:
                field_slug = hashlib.sha256(field.encode("utf-8")).hexdigest()[:10]
            return f"record-field-{index + 1}-{field_slug}"

        properties = [
            {
                "id": field_property_id(index, field),
                "entity_type": "record",
                "name": field,
                "label": field,
                "type": field_type(field),
                "is_identifier": field == identifier_field,
                "source_field": field,
                "confidence": 1.0,
                "evidence": {"kind": "selected_dataset_column", "field": field},
            }
            for index, field in enumerate(fields)
        ]
        dataset = (descriptor or {}).get("dataset") or {}
        dataset_name = str(dataset.get("name") or "来源数据集")
        return {
            "schema_version": "ontology-mapping-v2",
            "entity_types": [
                {
                    "id": "source_dataset",
                    "name": "SourceDataset",
                    "name_cn": "来源数据集",
                    "name_en": "SourceDataset",
                    "description": f"导入的结构化数据来源：{dataset_name}。",
                    "source_fields": ["dataset_id"],
                    "identifier_property": "dataset_id",
                    "confidence": 1.0,
                    "evidence": {"kind": "dataset_resource", "dataset_id": dataset.get("id")},
                },
                {
                    "id": "record",
                    "name": "Record",
                    "name_cn": "数据记录",
                    "name_en": "Record",
                    "description": "按用户选择字段保留的来源数据行；字段类型依据来源样例做确定性识别。",
                    "source_fields": fields,
                    "identifier_property": identifier_field,
                    "confidence": 1.0,
                    "evidence": {"kind": "selected_dataset_columns", "fields": fields},
                },
            ],
            "properties": [
                {
                    "id": "dataset-id",
                    "entity_type": "source_dataset",
                    "name": "dataset_id",
                    "label": "数据集 ID",
                    "type": "string",
                    "is_identifier": True,
                    "source_field": None,
                    "confidence": 1.0,
                    "evidence": {"kind": "dataset_resource"},
                },
                *properties,
            ],
            "relationships": [
                {
                    "id": "source-dataset-has-record",
                    "name": "HAS_RECORD",
                    "from": "source_dataset",
                    "to": "record",
                    "cardinality": "one-to-many",
                    "description": "来源数据集包含其导入版本中的记录。",
                    "source_fields": [],
                    "confidence": 1.0,
                    "evidence": {"kind": "dataset_version", "dataset_id": dataset.get("id")},
                },
            ],
            "logic_rules": [],
            "source": {"engine": "rules", "case": "private_regular_dataset_profile"},
        }
    else:
        suggestions = [
            {"kind": "class", "source": "selected columns", "target": "Domain entity", "confidence": 1.0, "extractor": "deterministic_profile"},
            {"kind": "property", "source": "typed fields", "target": "属性 / datatype", "confidence": 1.0, "extractor": "deterministic_profile"},
        ]
    return {"status": "ready", "engine": "rules", "suggestions": suggestions, "confirmed": False}


def _select_m3_config(db: Session, *, model_id: str | None = None, multimodal: bool = False) -> ModelConfig | None:
    """Return only an actual MiniMax-M3 slot.

    ``select_llm_model_config`` intentionally has a broad compatibility
    fallback for legacy text extraction.  A construction draft cannot use
    that fallback: standard data must either call the configured M3 model or
    remain in ``waiting_for_model``.  In particular, the local qwen slot is
    reserved for audit/private work and must never silently become the M3
    builder.
    """
    # The generic selector intentionally filters VLM/local slots for legacy
    # callers.  Construction is stricter: scan the registry for the exact
    # MiniMax-M3 model so a correctly configured M3 slot is still selected
    # when it has not been given a usage tag yet.
    configs: list[ModelConfig] = []
    if model_id:
        selected = db.query(ModelConfig).filter(ModelConfig.id == model_id).first()
        if not selected:
            return None
        configs.append(selected)
    else:
        configs = db.query(ModelConfig).filter(ModelConfig.config_type == "llm").order_by(ModelConfig.updated_at.desc()).all()
    return next(
        (
            config for config in configs
            if config.config_type == "llm"
            and MODEL_NAME in [str(item) for item in (config.models or [])]
            and str(config.provider or "").lower() not in {"ollama", "local"}
        ),
        None,
    )


def _default_domain(data_class: str) -> str:
    return "供应链" if data_class == "regular" else "制造"


def _build_mode(data_class: str) -> str:
    return {"regular": "regular_workbench", "temporal": "temporal_pipeline", "multimodal": "multimodal_workbench"}[data_class]


def _validate_target(db: Session, *, dataset: Dataset, data_class: str, target_mode: str, ontology_id: str | None, new_ontology_name: str | None) -> None:
    if dataset.data_class != data_class:
        raise HTTPException(409, f"数据集分类为 {dataset.data_class}，不能按 {data_class} 构筑")
    if target_mode == "create":
        if not (new_ontology_name or "").strip():
            raise HTTPException(422, "请输入新本体名称")
        return
    if not ontology_id:
        raise HTTPException(422, "请选择要追加的目标本体")
    ontology = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not ontology:
        raise HTTPException(404, "目标本体不存在")
    if ontology.data_class != data_class:
        raise HTTPException(409, f"只能追加到同类本体；目标本体为 {ontology.data_class}")


def _dataset_version(
    db: Session,
    dataset: Dataset,
    version_id: str | None = None,
) -> DatasetVersion | None:
    if version_id:
        return db.query(DatasetVersion).filter(
            DatasetVersion.id == version_id,
            DatasetVersion.dataset_id == dataset.id,
        ).first()
    if dataset.latest_version_id:
        return db.query(DatasetVersion).filter(
            DatasetVersion.id == dataset.latest_version_id,
            DatasetVersion.dataset_id == dataset.id,
        ).first()
    return db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version_no.desc()).first()


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping_model_config_snapshot(config: ModelConfig | None) -> dict[str, Any] | None:
    if config is None:
        return None
    encrypted_credential = config.api_key_encrypted or ""
    gateway_base = os.getenv("LITELLM_API_BASE", "").strip()
    gateway_key = os.getenv("LITELLM_API_KEY", "").strip()
    snapshot = {
        "config_id": config.id,
        "provider": config.provider,
        "models": [str(value) for value in (config.models or [])],
        "config_type": config.config_type,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
        "api_base_digest": _stable_digest(config.api_base) if config.api_base else None,
        "options_digest": _stable_digest(config.options or {}),
        # Persist only a one-way fingerprint, never the credential or its ciphertext.
        "credential_fingerprint": hashlib.sha256(encrypted_credential.encode("utf-8")).hexdigest() if encrypted_credential else None,
        "gateway_config_digest": _stable_digest({
            "api_base": gateway_base,
            "api_key": gateway_key,
        }) if gateway_base or gateway_key else None,
        "provider_model_artifact_reproducibility": "unverified",
    }
    snapshot["config_digest"] = _stable_digest(snapshot)
    return snapshot


def _draft_mapping_inputs(draft: ConstructionDraft) -> dict[str, Any]:
    return {
        "dataset_id": draft.dataset_id,
        "dataset_version_id": draft.dataset_version_id,
        "data_class": draft.data_class,
        "privacy_level": draft.privacy_level,
        "selection": draft.selection_json or {},
        "processing": draft.processing_json or {},
    }


def _mapping_input_manifest(
    draft: ConstructionDraft,
    dataset: Dataset,
    version: DatasetVersion,
    *,
    model_config: ModelConfig | None = None,
) -> tuple[dict[str, Any], str]:
    schema_snapshot = dataset.schema_json if isinstance(dataset.schema_json, dict) else {}
    mapping_system_prompt = _ontology_mapping_system_prompt(draft.data_class, dataset.name)
    manifest = {
        "contract": "construction-mapping-input-v1",
        "draft_input": _draft_mapping_inputs(draft),
        "draft_input_digest": _stable_digest(_draft_mapping_inputs(draft)),
        "dataset": {
            "dataset_id": dataset.id,
            "dataset_version_id": version.id,
            "version_no": version.version_no,
            "checksum": version.checksum,
            "row_count": version.rowcount,
            "schema": schema_snapshot,
            "schema_digest": _stable_digest(schema_snapshot),
        },
        "model_config": _mapping_model_config_snapshot(model_config),
        "mapping_schema_version": "ontology-mapping-v2",
        "llm_contract": {
            "source_analysis_prompt_digest": _stable_digest(SOURCE_ANALYSIS_SYSTEM_PROMPT),
            "ontology_mapping_prompt_digest": _stable_digest(mapping_system_prompt),
            "parser_version": MAPPING_PARSER_VERSION,
            "normalizer_version": MAPPING_NORMALIZER_VERSION,
            "tool_versions": [],
        },
        "model_artifact_reproducibility": "unverified",
    }
    return manifest, _stable_digest(manifest)


def _ontology_mapping_system_prompt(data_class: str, dataset_name: str) -> str:
    fixed_case_instruction = ""
    if data_class == "regular" and "c-mapss" in str(dataset_name or "").casefold():
        fixed_case_instruction = " 这是 NASA C-MAPSS FD001 固定子集：必须包含 id=equipment 的 Equipment（设备）和 id=sensor_reading 的 SensorReading（传感器读数）实体类型，必须包含从 equipment 到 sensor_reading 的 one-to-many 关系；规则只能引用 cycle、运行设置或实际传感器字段，不能编造阈值或日期。"
    return ONTOLOGY_MAPPING_SYSTEM_PROMPT + fixed_case_instruction


def _pin_draft_dataset_version(
    db: Session,
    draft: ConstructionDraft,
) -> tuple[Dataset, DatasetVersion | None]:
    dataset = db.query(Dataset).filter(Dataset.id == draft.dataset_id).first()
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    version = _dataset_version(db, dataset, draft.dataset_version_id)
    if draft.dataset_version_id and version is None:
        raise HTTPException(409, "草案固定的数据版本已不存在；请重新创建草案以选择可用版本")
    if version is None:
        version = _dataset_version(db, dataset)
        if version is not None:
            draft.dataset_version_id = version.id
            db.commit()
            db.refresh(draft)
    return dataset, version


def _source_descriptor(
    db: Session,
    draft: ConstructionDraft,
    *,
    input_manifest: dict[str, Any] | None = None,
    source_version_id: str | None = None,
) -> dict[str, Any]:
    """Create the first, deliberately bounded M3 planning request."""
    dataset = db.query(Dataset).filter(Dataset.id == draft.dataset_id).first()
    if not dataset:
        raise ValueError("数据集不存在")
    pinned_version_id = source_version_id or draft.dataset_version_id
    version = _dataset_version(db, dataset, pinned_version_id)
    if pinned_version_id and version is None:
        raise ValueError("MAPPING_SOURCE_VERSION_MISSING: pinned DatasetVersion is unavailable")
    selection = dict(draft.selection_json or {})
    manifest_dataset = (input_manifest or {}).get("dataset") or {}
    schema_snapshot = manifest_dataset.get("schema") if isinstance(manifest_dataset.get("schema"), dict) else (dataset.schema_json or {})
    descriptor: dict[str, Any] = {
        "dataset": {
            "id": dataset.id,
            "name": dataset.name,
            "data_class": dataset.data_class,
            "privacy_level": dataset.privacy_level,
            "schema": schema_snapshot,
            "schema_digest": manifest_dataset.get("schema_digest") or _stable_digest(schema_snapshot),
            "version_id": version.id if version else None,
            "source_locator": _safe_source_locator((version.source_path if version else None) or (version.storage_uri if version else None)),
            "storage_uri": _safe_source_locator(version.storage_uri if version else None),
            "checksum": version.checksum if version else None,
            "record_count": version.rowcount if version else None,
            "version": version.version_no if version else None,
        },
        "selection": selection,
        "data_class": draft.data_class,
    }
    if draft.data_class == "multimodal":
        from app.models.v2.dataset import MediaItem, MultimodalSample
        if not version:
            raise ValueError("多模态数据集没有可读取版本")
        samples = db.query(MultimodalSample).filter(MultimodalSample.dataset_version_id == version.id).order_by(MultimodalSample.sample_key.asc()).all()
        selected_sample_ids = {str(value) for value in selection.get("sample_ids", [])}
        if selected_sample_ids:
            samples = [item for item in samples if item.id in selected_sample_ids]
        media = db.query(MediaItem).filter(MediaItem.dataset_version_id == version.id).all()
        roles_by_sample: dict[str, list[str]] = {}
        for item in media:
            if item.sample_id:
                roles_by_sample.setdefault(item.sample_id, []).append(item.asset_role)
        descriptor["multimodal_catalog"] = [
            {"sample_id": item.id, "sample_key": item.sample_key, "scene_id": item.scene_id, "label": item.label, "labels": item.labels or [], "modalities": sorted(set(roles_by_sample.get(item.id, []))), "metadata": item.metadata_json or {}}
            for item in samples[:100]
        ]
        descriptor["allowed_asset_roles"] = list(selection.get("selected_assets") or selection.get("modalities") or ["rgb", "depth", "mask", "point_cloud", "metadata"])
    else:
        from app.services.v2.dataset_service import DatasetService
        if version:
            rows = DatasetService(db).preview_version_id(dataset.id, version.id, limit=25)[1]
        else:
            rows = []
        columns = list(rows[0].keys()) if rows else list((dataset.schema_json or {}).get("columns") or [])
        descriptor["headers"] = columns
        descriptor["header_profile"] = [
            {"name": str(column), "sample_values": [row.get(column) for row in rows[:5]]}
            for column in columns[:100]
        ]
        descriptor["preview_row_count"] = len(rows)
    return descriptor


def _deterministic_acquisition_plan(descriptor: dict[str, Any]) -> dict[str, Any]:
    selection = descriptor.get("selection") or {}
    if descriptor.get("data_class") == "multimodal":
        return {
            "items": [{"kind": "samples", "sample_ids": list(selection.get("sample_ids") or []), "reason": "使用已选多模态样例"}, {"kind": "assets", "asset_roles": list(selection.get("selected_assets") or selection.get("modalities") or ["rgb"]), "reason": "使用已选证据模态"}],
            "summary": "本地规则根据用户选定的多模态样例和资产准备映射内容。",
        }
    fields = list(selection.get("fields") or selection.get("columns") or descriptor.get("headers") or [])
    return {"items": [{"kind": "table", "fields": fields, "max_rows": int(selection.get("row_limit") or selection.get("sample_limit") or 200), "reason": "使用已选字段和记录范围"}], "summary": "本地规则根据用户选定的字段和记录范围准备映射内容。"}


def _clip_acquisition_plan(plan: dict[str, Any], descriptor: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Intersect M3's plan with the explicit selection and record every clamp."""
    selection = descriptor.get("selection") or {}
    allowed_fields = {str(value) for value in (selection.get("fields") or selection.get("columns") or descriptor.get("headers") or [])}
    allowed_samples = {str(value) for value in selection.get("sample_ids", [])}
    allowed_assets = {str(value) for value in (selection.get("selected_assets") or selection.get("modalities") or descriptor.get("allowed_asset_roles") or [])}
    trace: list[dict[str, Any]] = []
    accepted_items: list[dict[str, Any]] = []
    for raw in _as_list(plan.get("items") if isinstance(plan, dict) else []):
        item = dict(raw) if isinstance(raw, dict) else {}
        fields = [str(value) for value in _as_list(item.get("fields"))]
        if fields and allowed_fields:
            clipped = [value for value in fields if value in allowed_fields]
            if set(clipped) != set(fields):
                trace.append({"stage": "范围校验", "status": "trimmed", "detail": "模型请求的字段已限制为用户选定字段", "requested": fields, "accepted": clipped})
            item["fields"] = clipped
        elif not fields and allowed_fields and item.get("kind") in {"table", "rows", "fields"}:
            item["fields"] = sorted(allowed_fields)
        sample_ids = [str(value) for value in _as_list(item.get("sample_ids"))]
        if sample_ids and allowed_samples:
            clipped = [value for value in sample_ids if value in allowed_samples]
            if set(clipped) != set(sample_ids):
                trace.append({"stage": "范围校验", "status": "trimmed", "detail": "模型请求的样例已限制为用户选定样例", "requested": sample_ids, "accepted": clipped})
            item["sample_ids"] = clipped
        elif not sample_ids and allowed_samples and item.get("kind") in {"samples", "assets", "multimodal"}:
            item["sample_ids"] = sorted(allowed_samples)
        roles = [str(value) for value in _as_list(item.get("asset_roles") or item.get("modalities"))]
        if roles and allowed_assets:
            clipped = [value for value in roles if value in allowed_assets]
            if set(clipped) != set(roles):
                trace.append({"stage": "范围校验", "status": "trimmed", "detail": "模型请求的模态已限制为用户选定模态", "requested": roles, "accepted": clipped})
            item["asset_roles"] = clipped
        elif not roles and allowed_assets and item.get("kind") in {"assets", "multimodal"}:
            item["asset_roles"] = sorted(allowed_assets)
        try:
            item["max_rows"] = max(1, min(int(item.get("max_rows") or selection.get("row_limit") or selection.get("sample_limit") or 200), 2000))
        except (TypeError, ValueError):
            item["max_rows"] = 200
        accepted_items.append(item)
    if not accepted_items:
        accepted_items = _deterministic_acquisition_plan(descriptor)["items"]
    return {"items": accepted_items, "summary": str(plan.get("summary") or plan.get("rationale") or "模型已生成读取计划")}, trace


def _pack_transfer_content(
    db: Session,
    draft: ConstructionDraft,
    descriptor: dict[str, Any],
    plan: dict[str, Any],
    *,
    source_version_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Materialise the second call's bounded data payload and its audit manifest."""
    dataset = db.query(Dataset).filter(Dataset.id == draft.dataset_id).first()
    if not dataset:
        raise ValueError("数据集不存在")
    pinned_version_id = source_version_id or draft.dataset_version_id or (descriptor.get("dataset") or {}).get("version_id")
    version = _dataset_version(db, dataset, pinned_version_id)
    if pinned_version_id and version is None:
        raise ValueError("MAPPING_SOURCE_VERSION_MISSING: pinned DatasetVersion is unavailable")
    manifest: dict[str, Any] = {
        "dataset_id": dataset.id,
        "version_id": version.id if version else None,
        "version_no": version.version_no if version else None,
        "checksum": version.checksum if version else None,
        "items": [],
    }
    if draft.data_class == "multimodal":
        from app.models.v2.dataset import MediaItem, MultimodalSample
        if not version:
            raise ValueError("多模态数据集没有版本")
        wanted_samples = {sample for item in plan.get("items", []) for sample in _as_list(item.get("sample_ids"))}
        wanted_roles = {role for item in plan.get("items", []) for role in _as_list(item.get("asset_roles"))}
        samples = db.query(MultimodalSample).filter(MultimodalSample.dataset_version_id == version.id).all()
        if wanted_samples:
            samples = [item for item in samples if item.id in wanted_samples]
        media = db.query(MediaItem).filter(MediaItem.dataset_version_id == version.id).all()
        packed_assets = []
        for item in media:
            if wanted_samples and item.sample_id not in wanted_samples:
                continue
            if wanted_roles and item.asset_role not in wanted_roles:
                continue
            # Mapping receives only addresses and deterministic summaries.
            # RGB and derived image previews are attached by the multimodal
            # worker when the route supports vision content; raw depth/mask/
            # point-cloud bytes are never represented here.
            packed_assets.append({"media_id": item.id, "sample_id": item.sample_id, "asset_role": item.asset_role, "mime_type": item.mime_type, "storage_uri": _safe_source_locator(item.storage_uri), "checksum": item.checksum, "metadata": item.metadata_json or {}})
        payload = {"source": descriptor, "plan": plan, "samples": [{"id": item.id, "sample_key": item.sample_key, "scene_id": item.scene_id, "label": item.label, "labels": item.labels or [], "metadata": item.metadata_json or {}} for item in samples], "assets": packed_assets}
        manifest["items"] = [{"kind": "asset", "media_id": item["media_id"], "sample_id": item["sample_id"], "asset_role": item["asset_role"], "checksum": item["checksum"]} for item in packed_assets]
        return payload, manifest
    from app.services.v2.dataset_service import DatasetService
    if not version:
        raise ValueError("数据集没有可读取版本")
    selected_fields = {field for item in plan.get("items", []) for field in _as_list(item.get("fields"))}
    max_rows = max([int(item.get("max_rows") or 1) for item in plan.get("items", [])] or [200])
    rows = DatasetService(db).preview_version_id(dataset.id, version.id, limit=max_rows)[1]
    if selected_fields:
        rows = [{key: value for key, value in row.items() if key in selected_fields} for row in rows]
    payload = {"source": descriptor, "plan": plan, "headers": list(rows[0].keys()) if rows else descriptor.get("headers", []), "rows": rows}
    manifest["items"] = [{"kind": "row", "row_index": index + 1, "fields": list(row.keys())} for index, row in enumerate(rows)]
    return payload, manifest


def _multimodal_mapping_message_parts(
    db: Session,
    draft: ConstructionDraft,
    transfer_payload: dict[str, Any],
    transfer_manifest: dict[str, Any],
    mapping_request: str,
) -> list[dict[str, Any]] | str:
    """Attach explicitly selected visual evidence without persisting Base64.

    The durable invocation log stores ``mapping_request`` and the manifest
    only.  Image bytes exist in memory solely for this standard-privacy model
    call, then are discarded.  Depth arrays are converted to a pseudo-colour
    PNG and masks are overlaid over their paired RGB image so the model never
    receives raw numerical or binary modality files.
    """
    if draft.data_class != "multimodal":
        return mapping_request
    from app.models.v2.dataset import MediaItem
    from app.services.storage_service import get_storage_service

    allowed_roles = set((draft.selection_json or {}).get("selected_assets") or (draft.selection_json or {}).get("modalities") or [])
    visual_roles = {"rgb", "depth", "mask", "mask_visible"}
    if not (allowed_roles & visual_roles):
        return mapping_request
    listed = [item for item in transfer_payload.get("assets", []) if item.get("asset_role") in allowed_roles & visual_roles]
    # The fixed demo can contain 12 synchronized samples.  Cap the visual
    # pack deterministically so a user selection cannot create an unbounded
    # cloud request.  The full selected catalogue remains in the text payload.
    role_order = {"rgb": 0, "depth": 1, "mask": 2, "mask_visible": 3}
    listed.sort(key=lambda item: (str(item.get("sample_id") or ""), role_order.get(str(item.get("asset_role")), 9), str(item.get("media_id") or "")))
    selected = listed[:12]
    by_sample: dict[str, dict[str, MediaItem]] = {}
    media_by_id: dict[str, MediaItem] = {}
    if selected:
        ids = [str(item["media_id"]) for item in selected if item.get("media_id")]
        for item in db.query(MediaItem).filter(MediaItem.id.in_(ids)).all():
            media_by_id[item.id] = item
            if item.sample_id:
                by_sample.setdefault(item.sample_id, {})[item.asset_role] = item
    parts: list[dict[str, Any]] = [{"type": "text", "text": mapping_request}]
    visual_manifest: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    storage = get_storage_service()
    for listed_asset in selected:
        media = media_by_id.get(str(listed_asset.get("media_id") or ""))
        if media is None:
            continue
        role = media.asset_role
        try:
            payload: bytes
            mime: str
            label = role
            if role == "depth":
                # Reuse the deterministic rendering used by the browser.
                from app.routers.v2.multimodal import _depth_preview, _read_checked_asset
                payload, _ = _depth_preview(media)
                mime = "image/png"
                label = "depth_pseudocolor"
            elif role in {"mask", "mask_visible"}:
                from PIL import Image
                mask_raw = _read_checked_asset(media, storage)
                rgb_item = (by_sample.get(media.sample_id or "") or {}).get("rgb")
                if rgb_item is None:
                    raise ValueError("没有同一样例的 RGB 资产用于掩码叠加")
                rgb_raw = _read_checked_asset(rgb_item, storage)
                rgb = Image.open(io.BytesIO(rgb_raw)).convert("RGBA")
                mask = Image.open(io.BytesIO(mask_raw)).convert("L").resize(rgb.size)
                overlay = Image.new("RGBA", rgb.size, (0, 220, 255, 0))
                overlay.putalpha(mask.point(lambda value: int(value * 0.58)))
                rendered = Image.alpha_composite(rgb, overlay).convert("RGB")
                output = io.BytesIO(); rendered.save(output, format="PNG", optimize=True)
                payload = output.getvalue(); mime = "image/png"; label = "mask_overlay"
            else:
                payload = _read_checked_asset(media, storage)
                mime = media.mime_type or "image/png"
            data_url = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
            parts.append({"type": "text", "text": f"视觉证据：{label}；asset_id={media.id}；sample_id={media.sample_id or ''}；sha256={media.checksum or ''}"})
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
            visual_manifest.append({"media_id": media.id, "sample_id": media.sample_id, "asset_role": media.asset_role, "rendered_as": label, "checksum": media.checksum, "image_bytes_logged": False})
        except Exception as exc:
            omissions.append({"media_id": media.id, "asset_role": role, "reason": str(exc)[:180]})
    transfer_manifest["visual_evidence"] = visual_manifest
    if omissions:
        transfer_manifest["visual_evidence_omissions"] = omissions
    return parts


def _execute_regular_run(run_id: str, sample_limit: int = 5000) -> None:
    """Build a bounded, provenance-first graph for tabular datasets.

    This deterministic path is deliberately small: M3 proposes the mapping,
    while row identity, field values, evidence and graph writes remain
    reproducible and inspectable.  It also gives the regular five-step shell a
    real background task instead of leaving a permanently queued placeholder.
    """
    db = SessionLocal()
    try:
        run = db.query(ConstructionRun).filter(ConstructionRun.id == run_id).first()
        if not run:
            return
        from app.services.v2.construction_reviews import approved_review_for_run
        if not approved_review_for_run(db, run):
            update_run(db, run, status="failed", error="构建候选缺少有效的独立审批记录；拒绝写入本体")
            return
        pinned_selection = (run.config or {}).get("selection") or {}
        sample_limit = int(pinned_selection.get("row_limit", pinned_selection.get("sample_limit", sample_limit)))
        from app.services.v2.dataset_service import DatasetService
        from app.services.v2.graph.falkordb_service import FalkorDBService
        from app.services.v2.revision_service import create_revision
        from app.services.v2.audit_runner import queue_local_audit
        dataset = db.query(Dataset).filter(Dataset.id == run.dataset_id).first() if run.dataset_id else None
        if not dataset:
            update_run(db, run, status="failed", error="数据集不存在")
            return
        pinned_version_id = (run.config or {}).get("dataset_version_id")
        expected_checksum = (run.config or {}).get("dataset_checksum")
        version = _dataset_version(db, dataset, pinned_version_id) if pinned_version_id else None
        if pinned_version_id and version is None:
            update_run(db, run, status="failed", error="固定的数据版本已不存在，拒绝改读最新版本")
            return
        if version is None and not pinned_version_id:
            version = db.query(DatasetVersion).filter(DatasetVersion.id == dataset.latest_version_id).first() if dataset.latest_version_id else None
        if version is None and not pinned_version_id:
            version = db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version_no.desc()).first()
        if not version:
            update_run(db, run, status="failed", error="数据集没有可用版本")
            return
        if expected_checksum and version.checksum != expected_checksum:
            update_run(db, run, status="failed", error="固定的数据版本校验和已变化，拒绝继续构筑")
            return
        expected_schema_digest = (run.config or {}).get("dataset_schema_digest")
        if expected_schema_digest and expected_schema_digest != _stable_digest(
            dataset.schema_json if isinstance(dataset.schema_json, dict) else {},
        ):
            update_run(db, run, status="failed", error="固定的数据结构摘要已变化，拒绝继续构筑")
            return
        rows = DatasetService(db).preview(
            dataset.id,
            version.version_no,
            limit=max(1, min(int(sample_limit), 10000)),
            version_id=version.id,
            verify_checksum=bool(version.checksum),
        )
        if not rows:
            update_run(db, run, status="failed", progress={"completed": 0, "total": 0}, error="数据集预览为空，无法构建")
            return
        update_run(db, run, status="running", progress={"stage": "读取常规数据", "completed": 0, "total": len(rows), "pct": 10})
        selection = (run.config or {}).get("selection") or {}
        fields = selection.get("fields") or selection.get("columns") or list(rows[0].keys())
        fields = [str(field) for field in fields if str(field) in rows[0]][:80]
        built_in = "c-mapss" in str(dataset.name or "").casefold() or "cmapss" in str(dataset.name or "").casefold()
        nodes: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        if built_in:
            # C-MAPSS is a concrete equipment/readings model, not a generic
            # Dataset → Record graph.  Keep the graph plane aligned with the
            # canonical SQL ontology and its inspector terminology.
            seen_equipment_nodes: set[str] = set()
            for index, row in enumerate(rows):
                equipment_id = str(row.get("equipment_id") or "").strip()
                reading_id = str(row.get("reading_id") or f"row-{index + 1}").strip()
                if equipment_id and equipment_id not in seen_equipment_nodes:
                    seen_equipment_nodes.add(equipment_id)
                    nodes.append({
                        "id": f"equipment:{dataset.id}:{equipment_id}",
                        "entity_type": "Equipment",
                        "properties": {field: row.get(field) for field in fields if field in {"equipment_id", "equipment_type", "source_unit", "description"}},
                    })
                sensor_node = f"sensor-reading:{dataset.id}:{reading_id}"
                nodes.append({
                    "id": sensor_node,
                    "entity_type": "SensorReading",
                    "properties": {"row_index": index, **{field: row.get(field) for field in fields}},
                })
                if equipment_id:
                    relations.append({
                        "source": f"equipment:{dataset.id}:{equipment_id}",
                        "target": sensor_node,
                        "type": "HAS_SENSOR_READING",
                        "properties": {"equipment_id": equipment_id, "reading_id": reading_id, "dataset_version_id": version.id},
                    })
                if index % 100 == 0 or index == len(rows) - 1:
                    update_run(db, run, progress={"stage": "整理数据实例", "completed": index + 1, "total": len(rows), "pct": 20 + int((index + 1) / len(rows) * 30)})
        else:
            dataset_node = f"dataset:{dataset.id}"
            nodes.append({"id": dataset_node, "entity_type": "Dataset", "properties": {"name": dataset.name, "dataset_id": dataset.id, "version_id": version.id}})
            for index, row in enumerate(rows):
                key = next((str(row.get(field)).strip() for field in fields if row.get(field) not in (None, "")), str(index + 1))
                record_id = f"record:{dataset.id}:{index}:{key[:80]}"
                properties = {field: row.get(field) for field in fields}
                nodes.append({"id": record_id, "entity_type": "Record", "properties": {"row_index": index, "row_key": key, **properties}})
                relations.append({"source": dataset_node, "target": record_id, "type": "HAS_RECORD", "properties": {"row_index": index, "dataset_version_id": version.id}})
                if index % 100 == 0 or index == len(rows) - 1:
                    update_run(db, run, progress={"stage": "整理数据实例", "completed": index + 1, "total": len(rows), "pct": 20 + int((index + 1) / len(rows) * 30)})
        update_run(db, run, progress={"stage": "写入本体", "completed": len(rows), "total": len(rows), "pct": 58})
        mapping = ((run.config or {}).get("mapping") or {}).get("ontology_mapping") or ((run.config or {}).get("mapping") or {})
        from app.services.v2.construction_reviews import approved_review_for_run
        review = approved_review_for_run(db, run)
        if not review:
            raise ValueError("独立审批或基准修订已失效；拒绝物化候选")
        review_manifest = (review.candidate_json or {}).get("manifest") or {}
        base_schema_snapshot = live_schema_snapshot(db, run.ontology_id)
        if schema_snapshot_digest(base_schema_snapshot) != review_manifest.get("base_schema_digest"):
            raise ValueError("审批后本体基线结构已变化；拒绝物化候选")
        if built_in:
            checked = normalise_mapping(mapping, data_class="regular")
            if checked.errors:
                raise ValueError("C-MAPSS 映射校验失败：" + "；".join(checked.errors))
            entity_ids = {item["id"] for item in checked.mapping.get("entity_types", [])}
            relationships = checked.mapping.get("relationships", [])
            if not {"equipment", "sensor_reading"}.issubset(entity_ids):
                raise ValueError("C-MAPSS 映射必须确认 Equipment 与 SensorReading 两个实体类型")
            if not any(item.get("from") == "equipment" and item.get("to") == "sensor_reading" for item in relationships):
                raise ValueError("C-MAPSS 映射必须确认 Equipment → SensorReading 关系")
            equipment_rows: list[dict[str, Any]] = []
            seen_equipment: set[str] = set()
            for row in rows:
                equipment_id = str(row.get("equipment_id") or "")
                if not equipment_id or equipment_id in seen_equipment:
                    continue
                seen_equipment.add(equipment_id)
                equipment_rows.append({key: row.get(key) for key in ("equipment_id", "equipment_type", "source_unit", "description") if key in row})
            materialized = materialize_ontology(
                db, run=run, mapping=checked.mapping, instances=equipment_rows, data_class="regular",
                source_file=version.source_path or version.storage_uri, source_version=version.id,
                instance_entity="equipment", model_name=run.model_name, require_rule=True,
            )
            reading_materialized = materialize_ontology(
                db, run=run, mapping=checked.mapping, instances=rows, data_class="regular",
                source_file=version.source_path or version.storage_uri, source_version=version.id,
                instance_entity="sensor_reading", model_name=run.model_name, require_rule=False,
            )
            materialized.instance_count += reading_materialized.instance_count
            materialized.evidence_count += reading_materialized.evidence_count
        else:
            materialized = materialize_ontology(
                db, run=run, mapping=mapping, instances=rows, data_class="regular",
                source_file=version.source_path or version.storage_uri, source_version=version.id,
                instance_entity="Record", model_name=run.model_name, require_rule=False,
            )
            # Keep the source Dataset visible as a canonical instance too, so
            # the SQL ontology and the instance graph both contain the source
            # endpoint used by HAS_RECORD.
            dataset_materialized = materialize_ontology(
                db,
                run=run,
                mapping=mapping,
                instances=[{
                    "dataset_id": dataset.id,
                    "name": dataset.name,
                    "version_id": version.id,
                    "version_no": version.version_no,
                }],
                data_class="regular",
                source_file=version.source_path or version.storage_uri,
                source_version=version.id,
                instance_entity="source_dataset",
                model_name=run.model_name,
                require_rule=False,
            )
            materialized.instance_count += dataset_materialized.instance_count
            materialized.evidence_count += dataset_materialized.evidence_count
        actual_schema_snapshot = live_schema_snapshot(db, run.ontology_id)
        actual_schema_digest = schema_snapshot_digest(actual_schema_snapshot)
        expected_schema_digest = review_manifest.get("projected_schema_digest")
        if not expected_schema_digest or actual_schema_digest != expected_schema_digest:
            raise ValueError("实际物化结构与审核差异预览不一致；已停止发布修订")
        actual_schema_diff = schema_diff(base_schema_snapshot, actual_schema_snapshot)
        expected_diff = review_manifest.get("schema_diff") or {}
        if actual_schema_diff.get("digest") != expected_diff.get("digest"):
            raise ValueError("实际物化差异与审核预览不一致；已停止发布修订")
        actual_schema_diff["basis"] = "materialized_revision"
        run.progress = {"stage": "写入证据关系", "completed": len(rows), "total": len(rows), "pct": 76}
        graph = FalkorDBService()
        written_nodes = graph.upsert_instances(run.ontology_id, nodes)
        written_edges = graph.upsert_relations(run.ontology_id, relations)
        revision = create_revision(
            db, run.ontology_id, source_run_id=run.id,
            parent_revision_id=review.base_revision_id,
            summary={"rows_processed": len(rows), "nodes_written": written_nodes, "edges_written": written_edges, "entity_type_count": materialized.entity_type_count, "instance_count": materialized.instance_count, "relation_count": materialized.relation_count, "logic_rule_count": materialized.rule_count, "privacy_level": (run.config or {}).get("privacy_level", "standard")},
            commit=False,
        )
        append_review_event(
            db, review, actor_id=None, event_type="materialized",
            detail={
                "base_revision_id": review.base_revision_id,
                "revision_id": revision.id,
                "revision_no": revision.revision_no,
                "snapshot_hash": revision.snapshot_hash,
                "schema_diff": actual_schema_diff,
                "verification": {
                    "status": "matched",
                    "expected_schema_digest": expected_schema_digest,
                    "actual_schema_digest": actual_schema_digest,
                    "expected_diff_digest": expected_diff.get("digest"),
                    "actual_diff_digest": actual_schema_diff.get("digest"),
                },
            },
        )
        run.revision_id = revision.id
        attach_revision_to_materialized_rows(db, run=run, revision_id=revision.id)
        project = db.query(OntologyProject).filter(OntologyProject.id == run.ontology_id).first()
        if project:
            project.status = "created"
        db.commit()
        queue_local_audit(db, ontology_id=run.ontology_id, revision_id=revision.id, construction_run_id=run.id)
        update_run(db, run, status="completed", progress={"stage": "构建完成", "completed": len(rows), "total": len(rows), "pct": 100}, metrics={"rows_processed": len(rows), "nodes_written": written_nodes, "edges_written": written_edges, "entity_type_count": materialized.entity_type_count, "instance_count": materialized.instance_count, "relation_count": materialized.relation_count, "logic_rule_count": materialized.rule_count, "revision_id": revision.id, "privacy_level": (run.config or {}).get("privacy_level", "standard")})
    except Exception as exc:
        db.rollback()
        run = db.query(ConstructionRun).filter(ConstructionRun.id == run_id).first()
        if run:
            update_run(db, run, status="failed", error=str(exc)[:2000])
    finally:
        db.close()


@router.post("", status_code=201)
def create_draft(body: DraftCreate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    dataset = db.query(Dataset).filter(Dataset.id == body.dataset_id).first()
    if not dataset:
        raise HTTPException(404, "数据集不存在")
    require_dataset_access(db, dataset, current_user, write=True)
    source_version = _dataset_version(db, dataset)
    target_mode = body.target_mode or ("append" if body.ontology_id else "create")
    target_ontology_id = body.ontology_id if target_mode == "append" else None
    if target_ontology_id:
        _require_ontology_scope(db, target_ontology_id, current_user)
    _validate_target(db, dataset=dataset, data_class=body.data_class or dataset.data_class, target_mode=target_mode, ontology_id=target_ontology_id, new_ontology_name=body.new_ontology_name)
    privacy = "private" if dataset.privacy_level == "private" else body.privacy_level
    draft = ConstructionDraft(
        dataset_id=dataset.id,
        dataset_version_id=source_version.id if source_version else None,
        ontology_id=target_ontology_id,
        created_by=str(current_user.id),
        data_class=body.data_class or dataset.data_class or "regular",
        target_mode=target_mode,
        new_ontology_name=(body.new_ontology_name or "").strip() or None,
        new_ontology_domain=(body.new_ontology_domain or "").strip() or None,
        privacy_level=privacy,
        selection_json=body.selection,
        processing_json=body.processing_config,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return serialize_draft(draft)


@router.get("/{draft_id}")
def get_draft(draft_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == draft_id).first()
    if not draft:
        raise HTTPException(404, "构筑草案不存在")
    _require_draft_access(db, draft, current_user)
    return serialize_draft(draft)


@router.patch("/{draft_id}")
def patch_draft(draft_id: str, body: DraftPatch, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == draft_id).with_for_update().first()
    if not draft:
        raise HTTPException(404, "构筑草案不存在")
    _require_draft_access(db, draft, current_user)
    _assert_draft_editable(draft)
    updates = body.model_dump(exclude_none=True)
    active_mapping_task = db.query(MappingTask.id).filter(
        MappingTask.draft_id == draft.id,
        MappingTask.status.in_(("queued", "running")),
    ).first()
    immutable_while_running = {
        "data_class", "privacy_level", "selection", "processing_config", "mapping",
        "ontology_id", "target_mode", "new_ontology_name", "new_ontology_domain",
    }
    if active_mapping_task and immutable_while_running.intersection(updates):
        raise HTTPException(409, "映射任务运行期间不能修改草案输入或目标；请等待任务结束后重新编辑")
    if "privacy_level" in updates:
        dataset = db.query(Dataset).filter(Dataset.id == draft.dataset_id).first()
        requested = str(updates["privacy_level"])
        if dataset and dataset.privacy_level == "private":
            requested = "private"
        updates["privacy_level"] = requested
    target_mode = updates.get("target_mode", draft.target_mode)
    target_id = updates.get("ontology_id", draft.ontology_id)
    target_name = updates.get("new_ontology_name", draft.new_ontology_name)
    target_data_class = updates.get("data_class", draft.data_class)
    dataset = db.query(Dataset).filter(Dataset.id == draft.dataset_id).first()
    if target_id:
        _require_ontology_scope(db, str(target_id), current_user)
    if dataset:
        _validate_target(db, dataset=dataset, data_class=target_data_class, target_mode=target_mode, ontology_id=target_id, new_ontology_name=target_name)
    if "selection" in updates:
        selection = updates.pop("selection")
        # The UI persists its current selection before both mapping and build.
        # Only a material change invalidates the confirmed send scope; an
        # identical PATCH must not erase a just-confirmed mapping.
        if selection != (draft.selection_json or {}):
            draft.selection_json = selection
            draft.preflight_json = {}
            draft.mapping_json = {}
            draft.mapping_task_id = None
            draft.status = "draft"
    if "processing_config" in updates:
        draft.processing_json = updates.pop("processing_config")
    if "mapping" in updates:
        incoming = updates.pop("mapping") or {}
        # Existing mapping pages submit their checkbox selection as
        # ``suggestions``/``confirmed``.  Preserve the canonical v2 mapping
        # produced by the two-stage task instead of replacing it with a flat
        # display list.
        current = dict(draft.mapping_json or {})
        if current.get("ontology_mapping") and any(key in incoming for key in ("suggestions", "confirmed")):
            current["confirmed"] = incoming.get("confirmed", incoming.get("suggestions", []))
            current["suggestions"] = incoming.get("suggestions", current.get("suggestions", []))
            current["confirmed_at"] = datetime.now(timezone.utc).isoformat()
            draft.mapping_json = current
        else:
            draft.mapping_json = incoming
    for key, value in updates.items():
        setattr(draft, key, value)
    db.commit()
    db.refresh(draft)
    return serialize_draft(draft)


@router.post("/{draft_id}/m3-preflight")
def m3_preflight(draft_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == draft_id).with_for_update().first()
    if not draft:
        raise HTTPException(404, "构筑草案不存在")
    _require_draft_access(db, draft, current_user)
    _assert_draft_editable(draft)
    dataset, source_version = _pin_draft_dataset_version(db, draft)
    selected = draft.selection_json or {}
    summary = {
        "dataset_id": draft.dataset_id, "data_class": draft.data_class,
        "dataset_version_id": source_version.id if source_version else None,
        "dataset_version_no": source_version.version_no if source_version else None,
        "dataset_checksum": source_version.checksum if source_version else None,
        "privacy_level": draft.privacy_level, "sample_ids": selected.get("sample_ids", []),
        "fields": selected.get("fields", selected.get("columns", [])),
        "modalities": selected.get("modalities", selected.get("selected_assets", [])),
        "send_fields": selected.get("send_fields", []),
        "binary_excluded": ["raw_point_cloud", "depth_array", "mask_binary"],
    }
    config = None if draft.privacy_level == "private" else _select_m3_config(db, multimodal=draft.data_class == "multimodal")
    model_config_snapshot = _mapping_model_config_snapshot(config)
    source_available = source_version is not None
    preflight = {
        "allowed": source_available and (draft.privacy_level == "private" or bool(config)),
        "privacy_level": draft.privacy_level,
        "model": str((config.models or [None])[0]) if config else None,
        "model_id": config.id if config else None,
        "model_config_digest": model_config_snapshot.get("config_digest") if model_config_snapshot else None,
        "summary": summary,
        "requires_explicit_confirm": False,
        "two_stage": True,
        "stages": ["来源识别", "读取计划", "范围校验", "内容发送", "本体生成"],
    }
    draft.preflight_json = preflight
    draft.status = "preflight_ready" if preflight["allowed"] else (
        "waiting_for_model" if source_available and draft.privacy_level != "private" else "draft"
    )
    draft.error = None if preflight["allowed"] else (
        "数据集没有可读取版本" if not source_available else "标准数据需要先配置 MiniMax M3；不会自动切换本地模型"
    )
    db.commit()
    return {"draft": serialize_draft(draft), "preflight": preflight}


def _merge_mapping_suggestions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep every M3 class/property/relation suggestion in deterministic order."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group, kind in (("classes", "class"), ("properties", "property"), ("relations", "relation"), ("suggestions", "suggestion")):
        values = payload.get(group) or []
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                value = {"target": str(value)}
            item = {"kind": value.get("kind") or kind, **value}
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                merged.append(item)
    return merged


def _execute_mapping_task(task_id: str) -> None:
    db = SessionLocal()
    invocation = None
    attempt_no = None
    raw_response_hash = None
    started = time.monotonic()
    try:
        task = db.query(MappingTask).filter(MappingTask.id == task_id).with_for_update().first()
        if not task:
            return
        # Celery can deliver a message more than once after a worker/API
        # restart.  Claim the task row atomically so only one process calls
        # MiniMax or writes a mapping result.
        if task.status in {"running", "completed"}:
            return
        if task.cancel_requested:
            task.status = "cancelled"
            db.commit()
            return
        task.status = "running"
        task.stage = "来源识别"
        task.progress = {"stage": "来源识别", "pct": 8}
        attempt_no = next_mapping_attempt_number(db, task)
        append_mapping_attempt_event(
            db, task, attempt_number=attempt_no,
            event_type="attempt_started", status="running",
        )
        task.trace_json = list(task.trace_json or []) + [{
            "step": 1,
            "attempt": attempt_no,
            "stage": "来源识别",
            "status": "running",
            "detail": "正在读取数据来源、表头和已选范围",
        }]
        task.started_at = datetime.now(timezone.utc)
        db.commit()
        draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == task.draft_id).first()
        if not draft:
            raise ValueError("构筑草案不存在")
        input_manifest = task.input_manifest_json or {}
        if not input_manifest or not task.payload_hash:
            raise ValueError("MAPPING_INPUT_MANIFEST_MISSING: task has no immutable input contract")
        source_version_id = task.source_version_id or draft.dataset_version_id
        if draft.privacy_level == "private":
            config = None
        elif input_manifest:
            pinned_config_id = ((input_manifest.get("model_config") or {}).get("config_id"))
            config = _select_m3_config(
                db,
                model_id=str(pinned_config_id) if pinned_config_id else None,
                multimodal=draft.data_class == "multimodal",
            ) if pinned_config_id else None
        else:
            raise ValueError("MAPPING_INPUT_MANIFEST_MISSING: legacy tasks must be recreated with pinned inputs")
        if input_manifest:
            manifest_version_id = ((input_manifest.get("dataset") or {}).get("dataset_version_id"))
            if not source_version_id or source_version_id != manifest_version_id:
                raise ValueError("MAPPING_INPUT_PIN_MISMATCH: MappingTask source pin differs from its manifest")
            dataset = db.query(Dataset).filter(Dataset.id == draft.dataset_id).first()
            source_version = _dataset_version(db, dataset, source_version_id) if dataset else None
            if not dataset or not source_version:
                raise ValueError("MAPPING_SOURCE_VERSION_MISSING: pinned DatasetVersion is unavailable")
            _, current_digest = _mapping_input_manifest(
                draft, dataset, source_version, model_config=config,
            )
            if current_digest != task.payload_hash or current_digest != _stable_digest(input_manifest):
                raise ValueError("MAPPING_INPUT_CHANGED: draft, selection, schema, or DatasetVersion changed after enqueue")
        descriptor = _source_descriptor(
            db, draft, input_manifest=input_manifest, source_version_id=source_version_id,
        )
        trace = list(task.trace_json or [])
        trace.append({"step": 1, "stage": "来源识别", "status": "completed", "detail": "已读取来源地址、结构、表头/模态目录和用户选定范围"})
        if draft.privacy_level == "private":
            raw_plan = _deterministic_acquisition_plan(descriptor)
            plan_engine = "rules"
        else:
            kwargs = llm_call_kwargs(config) if config else None
            if not kwargs:
                raise RuntimeError("MiniMax M3 尚未配置或无法解密；映射停在等待模型")
            from app.services.llm_service import _call_llm, _parse_response
            task.stage = "读取计划"
            task.progress = {"stage": "读取计划", "pct": 22}
            trace.append({"step": 2, "stage": "读取计划", "status": "running", "detail": "MiniMax 正在根据来源与表头规划下一次读取内容"})
            request_payload = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, default=str)
            llm_contract = input_manifest.get("llm_contract") or {}
            source_prompt_digest = llm_contract.get("source_analysis_prompt_digest")
            source_request_hash = _stable_digest({
                "prompt_digest": source_prompt_digest,
                "payload_digest": _stable_digest(request_payload),
            })
            invocation = ModelInvocation(
                draft_id=draft.id, mapping_task_id=task.id, route_alias=task.model_route or MODEL_NAME,
                provider=config.provider, model_name=str((config.models or [MODEL_NAME])[0]), status="running", phase="source_analysis",
                request_ciphertext=encryption_service.encrypt(request_payload), request_hash=source_request_hash,
                metadata_json={"purpose": "source_analysis", "data_class": draft.data_class, "privacy_level": draft.privacy_level, "source_locator": descriptor.get("dataset", {}).get("source_locator"), "mapping_attempt": attempt_no, "model_config_id": config.id, "model_config_digest": _mapping_model_config_snapshot(config).get("config_digest"), "prompt_digest": source_prompt_digest, "parser_version": llm_contract.get("parser_version"), "normalizer_version": llm_contract.get("normalizer_version")},
            )
            db.add(invocation); db.flush()
            append_mapping_attempt_event(
                db, task, attempt_number=attempt_no, invocation=invocation,
                event_type="invocation_started", status="running", phase="source_analysis",
            )
            db.commit()
            raw = _call_llm(kwargs["provider"], kwargs["api_key"], kwargs["api_base"], kwargs["model"], [
                {"role": "system", "content": SOURCE_ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": request_payload},
            ], json_mode=True)
            raw_response_hash = _stable_digest(raw)
            try:
                parsed = _parse_response(raw)
            except Exception as parse_error:
                raise MappingResponseValidationError("MiniMax M3 来源读取计划无法解析") from parse_error
            if not isinstance(parsed, dict):
                raise MappingResponseValidationError("MiniMax M3 来源读取计划不是对象")
            raw_plan = parsed.get("data_acquisition_plan") if isinstance(parsed.get("data_acquisition_plan"), dict) else parsed
            invocation.status = "completed"; invocation.phase = "source_analysis"
            response_payload = json.dumps(raw_plan, ensure_ascii=False, sort_keys=True, default=str)
            invocation.response_ciphertext = encryption_service.encrypt(response_payload)
            invocation.response_hash = hashlib.sha256(response_payload.encode("utf-8")).hexdigest()
            invocation.duration_ms = int((time.monotonic() - started) * 1000); invocation.completed_at = datetime.now(timezone.utc)
            append_mapping_attempt_event(
                db, task, attempt_number=attempt_no, invocation=invocation,
                event_type="provider_result", status="completed", phase="source_analysis",
                duration_ms=invocation.duration_ms, response_hash=raw_response_hash,
            )
            db.commit()
            plan_engine = "m3"
        plan, clip_trace = _clip_acquisition_plan(raw_plan, descriptor)
        trace.append({"step": 2, "stage": "读取计划", "status": "completed", "detail": plan.get("summary") or "已生成读取计划"})
        trace.extend({"step": 3, **entry} for entry in clip_trace)
        if not clip_trace:
            trace.append({"step": 3, "stage": "范围校验", "status": "completed", "detail": "计划全部位于用户已选范围内"})
        task.source_plan_json = plan
        task.stage = "内容准备"; task.progress = {"stage": "内容准备", "pct": 42}; task.trace_json = trace
        db.commit()
        transfer_payload, transfer_manifest = _pack_transfer_content(
            db, draft, descriptor, plan, source_version_id=source_version_id,
        )
        task.transfer_manifest_json = transfer_manifest
        trace.append({"step": 4, "stage": "内容发送", "status": "completed", "detail": f"已按计划准备 {len(transfer_manifest.get('items', []))} 项内容"})
        if draft.privacy_level == "private":
            raw_mapping = _deterministic_mapping(draft, descriptor=descriptor)
            plan_engine = "rules"
        else:
            kwargs = llm_call_kwargs(config) if config else None
            if not kwargs:
                raise RuntimeError("MiniMax M3 尚未配置或无法解密；映射停在等待模型")
            task.stage = "本体生成"; task.progress = {"stage": "本体生成", "pct": 62}; task.trace_json = trace + [{"step": 5, "stage": "本体生成", "status": "running", "detail": "MiniMax 正在生成实体、属性、关系和逻辑规则"}]
            db.commit()
            mapping_request = json.dumps(transfer_payload, ensure_ascii=False, sort_keys=True, default=str)
            # Keep the durable request text-only.  Selected multimodal visual
            # evidence is appended only in memory for this one M3 request;
            # the task manifest records asset references and hashes, never
            # image bytes or Base64.
            mapping_message = _multimodal_mapping_message_parts(
                db,
                draft,
                transfer_payload,
                transfer_manifest,
                mapping_request,
            )
            task.transfer_manifest_json = transfer_manifest
            dataset_name = str((descriptor.get("dataset") or {}).get("name") or "")
            mapping_system_prompt = _ontology_mapping_system_prompt(draft.data_class, dataset_name)
            llm_contract = input_manifest.get("llm_contract") or {}
            mapping_prompt_digest = llm_contract.get("ontology_mapping_prompt_digest")
            mapping_request_hash = _stable_digest({
                "prompt_digest": mapping_prompt_digest,
                "payload_digest": _stable_digest(mapping_request),
                "transfer_manifest_digest": _stable_digest(transfer_manifest),
            })
            invocation = ModelInvocation(
                draft_id=draft.id, mapping_task_id=task.id, route_alias=task.model_route or MODEL_NAME,
                provider=config.provider, model_name=str((config.models or [MODEL_NAME])[0]), status="running", phase="ontology_mapping",
                request_ciphertext=encryption_service.encrypt(mapping_request), request_hash=mapping_request_hash,
                metadata_json={"purpose": "ontology_mapping", "data_class": draft.data_class, "privacy_level": draft.privacy_level, "transfer_manifest": transfer_manifest, "mapping_attempt": attempt_no, "model_config_id": config.id, "model_config_digest": _mapping_model_config_snapshot(config).get("config_digest"), "prompt_digest": mapping_prompt_digest, "parser_version": llm_contract.get("parser_version"), "normalizer_version": llm_contract.get("normalizer_version")},
            )
            db.add(invocation); db.flush()
            append_mapping_attempt_event(
                db, task, attempt_number=attempt_no, invocation=invocation,
                event_type="invocation_started", status="running", phase="ontology_mapping",
            )
            db.commit()
            raw = _call_llm(kwargs["provider"], kwargs["api_key"], kwargs["api_base"], kwargs["model"], [
                {"role": "system", "content": mapping_system_prompt},
                {"role": "user", "content": mapping_message},
            ], json_mode=True)
            raw_response_hash = _stable_digest(raw)
            try:
                raw_mapping = _parse_response(raw)
            except Exception as parse_error:
                raise MappingResponseValidationError("MiniMax M3 本体映射无法解析") from parse_error
            if not isinstance(raw_mapping, dict):
                raise MappingResponseValidationError("MiniMax M3 本体映射不是对象")
            invocation.status = "completed"; invocation.phase = "ontology_mapping"
            response_payload = json.dumps(raw_mapping, ensure_ascii=False, sort_keys=True, default=str)
            invocation.response_ciphertext = encryption_service.encrypt(response_payload)
            invocation.response_hash = hashlib.sha256(response_payload.encode("utf-8")).hexdigest()
            invocation.duration_ms = int((time.monotonic() - started) * 1000); invocation.completed_at = datetime.now(timezone.utc)
            append_mapping_attempt_event(
                db, task, attempt_number=attempt_no, invocation=invocation,
                event_type="provider_result", status="completed", phase="ontology_mapping",
                duration_ms=invocation.duration_ms, response_hash=raw_response_hash,
            )
            db.commit()
        validation = normalise_mapping(raw_mapping, data_class=draft.data_class)
        if validation.errors:
            raise MappingResponseValidationError("本体映射校验失败：" + "；".join(validation.errors))
        mapping = {"status": "ready", "engine": plan_engine, "model": MODEL_NAME if plan_engine == "m3" else "规则 + 人工", "ontology_mapping": validation.mapping, "suggestions": mapping_suggestions(validation.mapping, extractor="m3" if plan_engine == "m3" else "规则处理"), "source_plan": plan, "transfer_manifest": transfer_manifest, "trace": trace + [{"step": 5, "stage": "本体生成", "status": "completed", "detail": "映射结构已通过引用、数据类型和基数校验"}], "validation_warnings": validation.warnings, "confirmed": False}
        if task.cancel_requested:
            task.status = "cancelled"
            draft.status = "draft"
            append_mapping_attempt_event(
                db, task, attempt_number=attempt_no,
                event_type="attempt_result", status="cancelled",
                error_code="cancel_requested",
            )
            db.commit()
            return
        if input_manifest:
            draft = db.query(ConstructionDraft).filter(
                ConstructionDraft.id == task.draft_id,
            ).with_for_update().first()
            if not draft:
                raise ValueError("构筑草案不存在")
            dataset = db.query(Dataset).filter(Dataset.id == draft.dataset_id).first()
            source_version = _dataset_version(db, dataset, source_version_id) if dataset else None
            if not dataset or not source_version:
                raise ValueError("MAPPING_SOURCE_VERSION_MISSING: pinned DatasetVersion is unavailable")
            _, current_digest = _mapping_input_manifest(
                draft, dataset, source_version, model_config=config,
            )
            if current_digest != task.payload_hash:
                raise ValueError("MAPPING_INPUT_CHANGED: draft or source schema changed while mapping was running")
        task.result_json = mapping
        task.stage = "等待确认"
        task.progress = {"stage": "等待确认", "pct": 100}
        task.trace_json = mapping["trace"]
        task.source_plan_json = plan
        task.transfer_manifest_json = transfer_manifest
        task.status = "completed"
        task.completed_at = datetime.now(timezone.utc)
        task.error = None
        draft.mapping_json = mapping
        draft.mapping_task_id = task.id
        draft.status = "mapping_ready"
        draft.error = None
        # Private drafts are deliberately mapped by deterministic rules and
        # have no cloud ModelInvocation.  Only persist a model response when
        # an upstream model was actually called; otherwise the private path
        # would fail after producing a valid mapping.
        if invocation is not None:
            response_payload = json.dumps(mapping, ensure_ascii=False, sort_keys=True, default=str)
            invocation.status = "completed"
            invocation.response_ciphertext = encryption_service.encrypt(response_payload)
            invocation.response_hash = hashlib.sha256(response_payload.encode("utf-8")).hexdigest()
            invocation.duration_ms = int((time.monotonic() - started) * 1000)
            invocation.completed_at = datetime.now(timezone.utc)
            append_mapping_attempt_event(
                db, task, attempt_number=attempt_no, invocation=invocation,
                event_type="invocation_finalized", status="completed",
                phase=invocation.phase, duration_ms=invocation.duration_ms,
            )
        append_mapping_attempt_event(
            db, task, attempt_number=attempt_no,
            event_type="attempt_result", status="completed",
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        task = db.query(MappingTask).filter(MappingTask.id == task_id).first()
        if task:
            error_code = _mapping_failure_code(exc)
            task.status = "waiting_for_model" if error_code == "model_unavailable" else "failed"
            task.stage = "等待模型" if task.status == "waiting_for_model" else "失败"
            task.progress = {"stage": task.stage, "pct": 0}
            task.error = _mapping_error_message(error_code, status=task.status)
            task.completed_at = datetime.now(timezone.utc)
            draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == task.draft_id).first()
            if draft:
                draft.status = "waiting_for_model" if task.status == "waiting_for_model" else "failed"
                draft.error = task.error
            if invocation is not None:
                failed_invocation = db.query(ModelInvocation).filter(ModelInvocation.id == invocation.id).first()
                if failed_invocation:
                    duration_ms = int((time.monotonic() - started) * 1000)
                    failed_invocation.duration_ms = duration_ms
                    failed_invocation.completed_at = failed_invocation.completed_at or datetime.now(timezone.utc)
                    if isinstance(exc, MappingResponseValidationError):
                        failed_invocation.status = "invalid_response"
                        failed_invocation.error = exc.code
                        if raw_response_hash:
                            failed_invocation.response_hash = raw_response_hash
                        append_mapping_attempt_event(
                            db, task, attempt_number=attempt_no or 1,
                            invocation=failed_invocation,
                            event_type="response_validation_failed",
                            status="invalid_response", phase=failed_invocation.phase,
                            error_code=exc.code, duration_ms=duration_ms,
                        )
                    elif failed_invocation.status == "running":
                        failed_invocation.status = "failed"
                        failed_invocation.error = "provider_call_failed"
                        append_mapping_attempt_event(
                            db, task, attempt_number=attempt_no or 1,
                            invocation=failed_invocation, event_type="provider_result",
                            status="failed", phase=failed_invocation.phase,
                            error_code="provider_call_failed", duration_ms=duration_ms,
                        )
            if attempt_no is not None:
                append_mapping_attempt_event(
                    db, task, attempt_number=attempt_no,
                    event_type="attempt_result", status=task.status,
                    error_code=error_code,
                )
            db.commit()
    finally:
        db.close()


@router.post("/{draft_id}/generate-mapping", status_code=202)
def generate_mapping(draft_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == draft_id).with_for_update().first()
    if not draft:
        raise HTTPException(404, "构筑草案不存在")
    _require_draft_access(db, draft, current_user)
    _assert_draft_editable(draft)
    active = db.query(MappingTask).filter(MappingTask.draft_id == draft.id, MappingTask.status.in_(["queued", "running"])).order_by(MappingTask.created_at.desc()).first()
    if active:
        return {"draft": serialize_draft(draft), "mapping_task_id": active.id, "mapping": {"status": active.status, "stage": active.stage, "suggestions": []}}
    dataset, source_version = _pin_draft_dataset_version(db, draft)
    if source_version is None:
        raise HTTPException(409, "数据集没有可读取版本；请先导入数据再生成映射")
    config = None
    if draft.privacy_level != "private":
        preflight = draft.preflight_json or {}
        pinned_config_id = preflight.get("model_id")
        config = _select_m3_config(
            db,
            model_id=str(pinned_config_id) if pinned_config_id else None,
            multimodal=draft.data_class == "multimodal",
        ) if pinned_config_id else _select_m3_config(db, multimodal=draft.data_class == "multimodal")
        expected_config_digest = preflight.get("model_config_digest")
        current_config_digest = _mapping_model_config_snapshot(config)
        if expected_config_digest and (
            not current_config_digest
            or current_config_digest.get("config_digest") != expected_config_digest
        ):
            raise HTTPException(409, "预检后模型配置已变化；请重新预检后再生成映射")
    input_manifest, payload_hash = _mapping_input_manifest(
        draft, dataset, source_version, model_config=config,
    )
    task = MappingTask(
        draft_id=draft.id,
        source_version_id=source_version.id,
        input_manifest_json=input_manifest,
        status="queued",
        stage="排队",
        progress={"stage": "排队", "pct": 0},
        trace_json=[{"step": 0, "stage": "排队", "status": "queued", "detail": "映射任务已提交"}],
        source_plan_json={},
        transfer_manifest_json={},
        schema_version="ontology-mapping-v2",
        model_route=MODEL_NAME if draft.privacy_level != "private" else "rules",
        payload_hash=payload_hash,
        result_json={},
    )
    db.add(task)
    draft.mapping_task_id = task.id
    draft.status = "draft" if draft.privacy_level == "private" else "preflight_ready"
    draft.error = None
    db.commit()
    db.refresh(task)
    try:
        if os.getenv("CELERY_ENABLED", "").lower() in {"1", "true", "yes"}:
            from app.tasks.v2.workbench import run_mapping_task
            run_mapping_task.delay(task.id)
        else:
            _execute_mapping_task(task.id)
    except Exception as exc:
        task.status = "failed"
        task.error = _mapping_error_message("mapping_dispatch_failed", status="failed")
        draft.status = "failed"
        draft.error = task.error
        db.commit()
    return {"draft": serialize_draft(draft), "mapping_task_id": task.id, "mapping": {"status": task.status, "stage": task.stage, "suggestions": task.result_json.get("suggestions", []) if task.status == "completed" else [], "error": task.error}}


@mapping_tasks_router.get("/mapping-tasks/{task_id}")
def get_mapping_task(task_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    task = db.query(MappingTask).filter(MappingTask.id == task_id).first()
    if not task:
        raise HTTPException(404, "映射任务不存在")
    draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == task.draft_id).with_for_update().first()
    if not draft:
        raise HTTPException(404, "映射任务不存在")
    _require_draft_access(db, draft, current_user)
    manifest = task.input_manifest_json or {}
    dataset_manifest = manifest.get("dataset") or {}
    model_manifest = manifest.get("model_config") or {}
    model_invocations = db.query(ModelInvocation).filter(
        ModelInvocation.mapping_task_id == task.id,
    ).order_by(ModelInvocation.created_at.desc()).limit(20).all()
    attempt_event_query = db.query(ConstructionMappingAttemptEvent).filter(
        ConstructionMappingAttemptEvent.mapping_task_id == task.id,
    ).order_by(ConstructionMappingAttemptEvent.sequence.asc())
    has_attempt_events = db.query(ConstructionMappingAttemptEvent.id).filter(
        ConstructionMappingAttemptEvent.mapping_task_id == task.id,
    ).first() is not None
    event_chain_status = "unavailable" if not has_attempt_events else (
        "verified" if verify_mapping_attempt_event_chain(attempt_event_query.yield_per(500)) else "invalid"
    )
    attempt_events = db.query(ConstructionMappingAttemptEvent).filter(
        ConstructionMappingAttemptEvent.mapping_task_id == task.id,
    ).order_by(ConstructionMappingAttemptEvent.sequence.desc()).limit(200).all()
    attempt_events.reverse()
    latest_error_code = None
    if event_chain_status == "verified":
        latest_attempt_result = next((
            item for item in reversed(attempt_events)
            if item.event_type == "attempt_result"
        ), None)
        if latest_attempt_result and latest_attempt_result.status != "completed":
            candidate_error_code = latest_attempt_result.error_code
            if candidate_error_code in {*_MAPPING_ERROR_MESSAGES, "cancel_requested"}:
                latest_error_code = candidate_error_code
    safe_error = _mapping_error_message(latest_error_code, status=task.status)
    return {
        "id": task.id,
        "draft_id": task.draft_id,
        "status": task.status,
        "stage": task.stage,
        "progress": task.progress or {},
        "trace": task.trace_json or [],
        "source_plan": task.source_plan_json or {},
        "transfer_manifest": task.transfer_manifest_json or {},
        "input_summary": {
            "dataset_version_id": task.source_version_id,
            "dataset_version_no": dataset_manifest.get("version_no"),
            "dataset_checksum": dataset_manifest.get("checksum"),
            "schema_digest": dataset_manifest.get("schema_digest"),
            "input_digest": task.payload_hash,
            "model_config_id": model_manifest.get("config_id"),
            "model_provider": model_manifest.get("provider"),
            "model_name": (model_manifest.get("models") or [None])[0],
            "model_config_digest": model_manifest.get("config_digest"),
            "model_artifact_reproducibility": model_manifest.get("provider_model_artifact_reproducibility"),
            "llm_contract": manifest.get("llm_contract") or {},
        },
        "model_invocations": [{
            "id": invocation.id,
            "phase": invocation.phase,
            "status": invocation.status,
            "provider": invocation.provider,
            "model_name": invocation.model_name,
            "attempt": (invocation.metadata_json or {}).get("mapping_attempt"),
            "config_digest": (invocation.metadata_json or {}).get("model_config_digest"),
            "duration_ms": invocation.duration_ms,
            "created_at": invocation.created_at.isoformat() if invocation.created_at else None,
            "completed_at": invocation.completed_at.isoformat() if invocation.completed_at else None,
        } for invocation in reversed(model_invocations)],
        "attempt_event_chain_status": event_chain_status,
        "error_code": latest_error_code,
        "attempt_events": [{
            "id": item.id,
            "attempt_number": item.attempt_number,
            "sequence": item.sequence,
            "event_type": item.event_type,
            "status": item.status,
            "phase": item.phase,
            "source_dataset_version_id": item.source_dataset_version_id,
            "model_config_digest": item.model_config_digest,
            "prompt_digest": item.prompt_digest,
            "parser_version": item.parser_version,
            "normalizer_version": item.normalizer_version,
            "provider": item.provider,
            "model_name": item.model_name,
            "request_hash": item.request_hash,
            "response_hash": item.response_hash,
            "error_code": item.error_code,
            "duration_ms": item.duration_ms,
            "previous_digest": item.previous_digest,
            "event_digest": item.event_digest,
            "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
        } for item in attempt_events],
        "result": task.result_json or {},
        # Legacy rows may contain raw exception strings. Never return them.
        "error": safe_error,
        "cancel_requested": task.cancel_requested,
    }


@mapping_tasks_router.post("/mapping-tasks/{task_id}/retry")
def retry_mapping_task(task_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    task = db.query(MappingTask).filter(MappingTask.id == task_id).first()
    if not task:
        raise HTTPException(404, "映射任务不存在")
    draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == task.draft_id).with_for_update().first()
    if not draft:
        raise HTTPException(404, "映射任务不存在")
    _require_draft_access(db, draft, current_user)
    _assert_draft_editable(draft)
    if task.status in {"queued", "running"}:
        return {"id": task.id, "status": task.status}
    if task.status == "completed":
        raise HTTPException(409, "已完成的映射任务不可重跑；请生成新的映射任务以保留历史")
    manifest = task.input_manifest_json or {}
    if not manifest:
        raise HTTPException(409, "旧映射任务缺少固定输入清单；请重新预检并创建新任务")
    model_manifest = manifest.get("model_config") or {}
    if manifest and draft and draft.privacy_level != "private":
        pinned_config_id = model_manifest.get("config_id")
        pinned_config = _select_m3_config(
            db,
            model_id=str(pinned_config_id) if pinned_config_id else None,
            multimodal=draft.data_class == "multimodal",
        ) if pinned_config_id else None
        current_config = _mapping_model_config_snapshot(pinned_config)
        if not current_config or current_config.get("config_digest") != model_manifest.get("config_digest"):
            raise HTTPException(409, "原映射任务的模型配置未固定或已变化；请重新预检并生成新任务")
    if manifest and draft:
        dataset = db.query(Dataset).filter(Dataset.id == draft.dataset_id).first()
        source_version_id = task.source_version_id or draft.dataset_version_id
        source_version = _dataset_version(db, dataset, source_version_id) if dataset and source_version_id else None
        if not dataset or not source_version:
            raise HTTPException(409, "原映射任务固定的数据版本已不可用；请重新生成映射任务")
        _, current_digest = _mapping_input_manifest(
            draft, dataset, source_version,
            model_config=(pinned_config if draft.privacy_level != "private" else None),
        )
        if current_digest != task.payload_hash or current_digest != _stable_digest(manifest):
            raise HTTPException(409, "原映射任务的输入或配置已变化；请保留旧记录并生成新映射任务")
    task.status, task.stage, task.error, task.cancel_requested = "queued", "排队", None, False
    task.progress = {"stage": "排队", "pct": 0}
    trace = list(task.trace_json or [])
    retry_count = sum(1 for item in trace if isinstance(item, dict) and item.get("stage") == "重试") + 1
    trace.append({"step": 0, "stage": "重试", "status": "queued", "detail": f"第 {retry_count} 次重试已提交"})
    task.trace_json = trace
    db.commit()
    try:
        if os.getenv("CELERY_ENABLED", "").lower() in {"1", "true", "yes"}:
            from app.tasks.v2.workbench import run_mapping_task
            run_mapping_task.delay(task.id)
        else:
            _execute_mapping_task(task.id)
    except Exception as exc:
        task.status = "failed"
        task.error = _mapping_error_message("mapping_dispatch_failed", status="failed")
        draft.status = "failed"
        draft.error = task.error
        db.commit()
    return {"id": task.id, "status": task.status}


@mapping_tasks_router.post("/mapping-tasks/{task_id}/cancel")
def cancel_mapping_task(task_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    task = db.query(MappingTask).filter(MappingTask.id == task_id).first()
    if not task:
        raise HTTPException(404, "映射任务不存在")
    draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == task.draft_id).first()
    if not draft:
        raise HTTPException(404, "映射任务不存在")
    _require_draft_access(db, draft, current_user)
    _assert_draft_editable(draft)
    if task.status in {"queued", "running"}:
        task.cancel_requested = True
        db.commit()
    return {"id": task.id, "status": task.status, "cancel_requested": task.cancel_requested}


@router.post("/{draft_id}/build", status_code=202)
def build_draft(draft_id: str, body: BuildRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    draft = db.query(ConstructionDraft).filter(ConstructionDraft.id == draft_id).with_for_update().first()
    if not draft:
        raise HTTPException(404, "构筑草案不存在")
    _require_draft_access(db, draft, current_user)
    if draft.status in {"building", "built"} or draft.build_run_id:
        raise HTTPException(409, "该草案已有构建任务，不能重复提交")
    prepared = _prepare_build_review(db, draft, body)
    existing = db.query(ConstructionReview).filter(
        ConstructionReview.draft_id == draft.id,
        ConstructionReview.status == "pending_review",
        ConstructionReview.candidate_digest == prepared["candidate_digest"],
    ).order_by(ConstructionReview.created_at.desc()).first()
    if existing:
        return {"review": serialize_construction_review(db, existing), "status": existing.status}
    _assert_draft_editable(draft)

    review = ConstructionReview(
        draft_id=draft.id,
        dataset_id=prepared["dataset"].id,
        dataset_version_id=prepared["source_version"].id,
        ontology_id=draft.ontology_id,
        base_revision_id=prepared["manifest"].get("base_revision_id"),
        mapping_task_id=prepared["mapping_task"].id,
        candidate_digest=prepared["candidate_digest"],
        candidate_json=prepared["candidate"],
        impact_json=prepared["impact"],
        status="pending_review",
        submitted_by=str(current_user.id),
    )
    db.add(review)
    db.flush()
    draft.status = "pending_review"
    draft.error = None
    append_review_event(
        db, review, actor_id=current_user.id, event_type="submitted",
        detail={"candidate_digest": review.candidate_digest, "base_revision_id": review.base_revision_id},
    )
    db.commit()
    db.refresh(review)
    return {"review": serialize_construction_review(db, review), "status": review.status}


@review_router.get("")
def list_construction_reviews(
    status: str = "pending_review",
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor),
):
    if status not in {"pending_review", "approved", "rejected", "changes_requested", "all"}:
        raise HTTPException(422, "审阅状态无效")
    query = db.query(ConstructionReview)
    if status != "all":
        query = query.filter(ConstructionReview.status == status)
    rows = query.order_by(ConstructionReview.created_at.desc()).limit(max(1, min(limit, 200))).all()
    visible = []
    for review in rows:
        if status == "pending_review" and str(review.submitted_by or "") == str(current_user.id):
            continue
        try:
            _require_review_access(db, review, current_user)
        except HTTPException:
            continue
        visible.append(serialize_construction_review(db, review))
    return {"reviews": visible, "count": len(visible)}


@review_router.get("/{review_id}")
def get_construction_review(
    review_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    review = db.query(ConstructionReview).filter(ConstructionReview.id == review_id).first()
    if not review:
        raise HTTPException(404, "构建审阅不存在")
    _require_review_access(db, review, current_user)
    return serialize_construction_review(db, review)


@review_router.post("/{review_id}/decision")
def decide_construction_review(
    review_id: str,
    body: ConstructionReviewDecision,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor),
):
    review = db.query(ConstructionReview).filter(
        ConstructionReview.id == review_id,
    ).with_for_update().first()
    if not review:
        raise HTTPException(404, "构建审阅不存在")
    _require_review_access(db, review, current_user)
    if str(review.submitted_by or "") == str(current_user.id):
        raise HTTPException(403, "提交者不能批准或拒绝自己的构建候选")
    if review.status != "pending_review":
        if review.reviewed_by == str(current_user.id) and review.decision == body.decision:
            run = db.get(ConstructionRun, review.construction_run_id) if review.construction_run_id else None
            return {
                "review": serialize_construction_review(db, review),
                "run": serialize_run(run) if run else None,
            }
        raise HTTPException(409, "该构建审阅已经处理")
    if body.decision != "approved" and not (body.note or "").strip():
        raise HTTPException(422, "拒绝或要求修改时必须填写原因")

    draft = db.query(ConstructionDraft).filter(
        ConstructionDraft.id == review.draft_id,
    ).with_for_update().first()
    if not draft:
        raise HTTPException(409, "构筑草案已不存在，不能处理审阅")
    build_manifest = review.candidate_json.get("manifest") if isinstance(review.candidate_json, dict) else {}
    build_body = BuildRequest(
        mapping_confirmed=True,
        mode=str((build_manifest or {}).get("requested_build_mode") or draft.target_mode),
        model_id=(build_manifest or {}).get("requested_model_id"),
    )
    prepared = _prepare_build_review(db, draft, build_body)
    if prepared["candidate_digest"] != review.candidate_digest:
        raise HTTPException(409, "来源版本、映射或基准修订已变化，请重新提交候选审阅")

    review.reviewed_by = str(current_user.id)
    review.reviewed_at = datetime.now(timezone.utc)
    review.decision = body.decision
    review.decision_note = (body.note or "").strip() or None
    if body.decision != "approved":
        review.status = body.decision
        draft.status = "rejected" if body.decision == "rejected" else "mapping_ready"
        draft.error = review.decision_note
        append_review_event(
            db, review, actor_id=current_user.id, event_type=body.decision,
            detail={"note": review.decision_note, "candidate_digest": review.candidate_digest},
        )
        db.commit()
        db.refresh(review)
        return {"review": serialize_construction_review(db, review), "run": None}

    if draft.target_mode == "create":
        name = (draft.new_ontology_name or "").strip()
        if db.query(OntologyProject.id).filter(OntologyProject.name.ilike(name)).first():
            raise HTTPException(409, f"Ontology 名称「{name}」已存在；候选仍未写入本体")
        if not draft.created_by:
            raise HTTPException(409, "草案缺少创建者，拒绝为其发布 Ontology")
        project = OntologyProject(
            id=str(uuid.uuid4()), name=name,
            domain=draft.new_ontology_domain or _default_domain(draft.data_class),
            build_mode=_build_mode(draft.data_class), data_class=draft.data_class,
            created_by=draft.created_by,
        )
        db.add(project)
        db.flush()
        draft.ontology_id = project.id
        review.ontology_id = project.id
    if not draft.ontology_id:
        raise HTTPException(409, "构筑草案没有目标本体")

    dataset = prepared["dataset"]
    source_version = prepared["source_version"]
    mapping_task = prepared["mapping_task"]
    run_id = str(uuid.uuid4())
    run_config = {
        "draft_id": draft.id,
        "privacy_level": draft.privacy_level,
        "selection": draft.selection_json or {},
        "processing": draft.processing_json or {},
        "model_id": build_body.model_id,
        "mapping": {**(draft.mapping_json or {}), "ontology_mapping": prepared["mapping"]},
        "mode": build_body.mode,
        "dataset_version_id": source_version.id,
        "dataset_version_no": source_version.version_no,
        "dataset_checksum": source_version.checksum,
        "dataset_schema_digest": _stable_digest(dataset.schema_json if isinstance(dataset.schema_json, dict) else {}),
        "mapping_task_id": mapping_task.id,
        "mapping_task_input_digest": mapping_task.payload_hash,
        "construction_review_id": review.id,
        "construction_review_digest": review.candidate_digest,
        "construction_submitted_by": review.submitted_by,
        "construction_reviewed_by": str(current_user.id),
    }
    review.status = "approved"
    draft.build_run_id = run_id
    draft.status = "building"
    draft.error = None
    append_review_event(
        db, review, actor_id=current_user.id, event_type="approved",
        detail={"candidate_digest": review.candidate_digest, "construction_run_id": run_id},
    )
    review.construction_run_id = run_id
    run = create_run(
        db, run_id=run_id, ontology_id=draft.ontology_id, dataset_id=draft.dataset_id,
        mode=draft.data_class, model_name=(draft.preflight_json or {}).get("model"), config=run_config,
    )

    try:
        celery_enabled = os.getenv("CELERY_ENABLED", "").lower() in {"1", "true", "yes"}
        if draft.data_class == "multimodal":
            selection = draft.selection_json or {}
            args = (
                run.id, build_body.model_id, int(selection.get("sample_limit", 32)),
                selection.get("prompt"), selection.get("sample_ids", []),
                selection.get("selected_assets", []), draft.privacy_level,
                selection.get("send_fields", []),
            )
            if celery_enabled:
                from app.tasks.v2.workbench import run_multimodal_construction_task
                run_multimodal_construction_task.delay(*args)
            else:
                from app.routers.v2.multimodal import _execute_multimodal_run
                background_tasks.add_task(_execute_multimodal_run, *args)
        else:
            selection = draft.selection_json or {}
            sample_limit = int(selection.get("row_limit", selection.get("sample_limit", 5000)))
            if celery_enabled:
                from app.tasks.v2.workbench import run_regular_construction_task
                run_regular_construction_task.delay(run.id, sample_limit)
            else:
                background_tasks.add_task(_execute_regular_run, run.id, sample_limit)
    except Exception:
        update_run(db, run, status="failed", error="构建任务未能派发；请创建新的候选审阅后重试")
        draft.status = "failed"
        draft.error = "构建任务未能派发"
        append_review_event(
            db, review, actor_id=None, event_type="dispatch_failed",
            detail={"construction_run_id": run.id, "reason_code": "dispatch_unavailable"},
        )
        db.commit()
        raise HTTPException(503, "构建任务未能派发；审批记录已保留，未自动重复派发")
    return {
        "review": serialize_construction_review(db, review),
        "run": serialize_run(run),
    }
