"""PostgreSQL-authoritative reviewed Action path for local ontology properties.

This is the first P0a execution core. It deliberately rejects effect kinds
which cannot yet be committed atomically with the ActionRun. The graph is a
retryable projection of the committed SQL fact, never the source of approval.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite
from types import SimpleNamespace

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.models.user import User
from app.models.v2.action import OntologyActionRun, OntologyActionType
from app.models.v2.construction import EvidenceRef
from app.models.v2.dataset import MediaItem, MultimodalSample
from app.models.v2.decision_case import DecisionCase, DecisionCaseEvent
from app.models.v2.decision_lab import DecisionLabRun
from app.models.v2.temporal_replay import TemporalReplay, TemporalStreamEvent
from app.services.storage_service import get_storage_service
from app.services.v2.authorization_service import AuthorizationContext, redact_record, resolve_projection
from app.services.v2.action_authorization import (
    action_field_permitted,
    action_properties_view,
    action_property_name_allowed,
)
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.action_runtime import (
    ActionRequest,
    complete_action_run,
    compile_action_definitions,
    create_action_run_record,
    find_idempotent_action_run,
    transition_action_run,
)
from app.services.v2.scenarios import ScenarioError


KINDS = {"quality_review", "dynamic_event", "maintenance_review", "general"}
ALLOWED_EDIT_OPS = {"set_property", "unset_property"}
ACTION_PROJECTION_MAX_ATTEMPTS = 8
ACTION_PROJECTION_RETRY_BASE_SECONDS = 30
ACTION_PROJECTION_RETRY_MAX_SECONDS = 3600


def _projection_retry_at(attempts: int, *, now: datetime | None = None) -> datetime | None:
    if attempts >= ACTION_PROJECTION_MAX_ATTEMPTS:
        return None
    delay = min(
        ACTION_PROJECTION_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)),
        ACTION_PROJECTION_RETRY_MAX_SECONDS,
    )
    return (now or datetime.now(timezone.utc)) + timedelta(seconds=delay)


class CaseError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


def _action_runtime_call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except ScenarioError as exc:
        raise CaseError(exc.code, exc.message, exc.status) from exc


def digest(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def object_etag(row: EntityInstance) -> str:
    return digest({"properties": row.row_data or {}, "source_version": row.source_version,
                   "revision_id": row.revision_id, "object_type_resource_id": row.object_type_resource_id})


def case_input_digest(case: DecisionCase) -> str:
    return digest({"kind": case.kind, "title": case.title,
                   "action_type_id": case.action_type_id, "target_object_id": case.target_object_id,
                   "target_object_type": case.target_object_type, "parameters": case.parameters,
                   "evidence_refs": case.evidence_refs, "reason": case.reason,
                   "context": case.context_json, "expected_etag": case.expected_etag})


def _event(db: Session, case: DecisionCase, name: str, actor_id: str | None,
           payload: dict, trace_id: str) -> DecisionCaseEvent:
    # Serialize per-case sequence allocation on PostgreSQL so concurrent run
    # projections cannot allocate the same append-only event sequence.
    db.query(DecisionCase.id).filter_by(id=case.id).with_for_update().first()
    sequence = (db.query(func.max(DecisionCaseEvent.sequence)).filter_by(case_id=case.id).scalar() or 0) + 1
    event = DecisionCaseEvent(case_id=case.id, sequence=sequence, event_type=name,
                              actor_id=actor_id, payload=payload, trace_id=trace_id)
    db.add(event)
    # The application session does not autoflush.  Without this, two events added in one unit of work (for example
    # "projection_synced" and "projection_attempt_finished") would both read the same max sequence and the commit
    # would fail on the unique (case, sequence) key, leaving the projection attempt unfinished forever.
    db.flush()
    return event


@contextmanager
def _action_projection_lock(db: Session, run_id: str):
    """Serialize immediate, scheduled and manual projection reconciliation."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        yield True
        return
    key = int.from_bytes(
        hashlib.sha256(f"action-projection:{run_id}".encode()).digest()[:8],
        "big", signed=True,
    )
    with bind.connect() as connection:
        acquired = bool(connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key},
        ).scalar())
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


class DecisionCaseService:
    def __init__(self, db: Session, ontology_id: str, user: User):
        self.db, self.ontology_id, self.user = db, ontology_id, user
        self.project = db.get(OntologyProject, ontology_id)
        if self.project is None:
            raise CaseError("not_found", "本体不存在或不可访问", 404)

    def _target(self, object_id: str, *, lock: bool = False,
                principal: User | None = None) -> tuple[EntityInstance, str, dict, str]:
        query = self.db.query(EntityInstance).filter_by(
            id=object_id,
            ontology_id=self.ontology_id,
            is_deleted=False,
        )
        row = query.with_for_update().first() if lock else query.first()
        if row is None:
            raise CaseError("not_found", "目标对象不存在或不可访问", 404)
        entity = self.db.get(Entity, row.entity_id)
        object_type = (entity.name_en or entity.name_cn) if entity else None
        if not object_type:
            raise CaseError("invalid_object_type", "目标对象缺少可执行的对象类型", 409)
        record = {"id": row.id, "entity_id": row.entity_id, "entity_type": object_type,
                  "object_type_resource_id": row.object_type_resource_id,
                  "properties": action_properties_view(row.row_data or {})}
        actor = principal or self.user
        context = AuthorizationContext(ontology_id=self.ontology_id, principal_id=str(actor.id),
                                       principal_role=actor.role,
                                       metadata_revision_id=self.project.current_revision_id)
        projection = resolve_projection(self.db, context, records=[record])
        visible = redact_record(projection, record)
        if visible is None:
            raise CaseError("not_found", "目标对象不存在或不可访问", 404)
        return row, object_type, {"projection": projection, "record": visible}, object_etag(row)

    def target_state(self, object_id: str) -> dict:
        row, object_type, auth, etag = self._target(object_id)
        return {"ontology_id": self.ontology_id, "object_id": row.id, "object_type": object_type,
                "properties": auth["record"]["properties"], "etag": etag,
                "source_version": row.source_version,
                "permission_digest": auth["projection"].permission_digest}

    def _action(self, action_id: str) -> OntologyActionType:
        row = self.db.query(OntologyActionType).filter_by(id=action_id, ontology_id=self.ontology_id).first()
        if row is None:
            raise CaseError("action_type_not_found", "Action 类型不存在", 404)
        if not row.enabled or row.status != "published":
            raise CaseError("action_type_unavailable", "Action 类型须先发布并启用", 422)
        external_effects = [item for item in (row.side_effects or [])
                            if not isinstance(item, dict) or item.get("type") != "review_note"]
        if row.backed_by_function or external_effects:
            raise CaseError("unsupported_effect", "此审核路径尚不支持函数或外部副作用", 422)
        return row

    def _evidence(self, refs: list[str | dict], target_id: str, *, ensure_available: bool = True,
                  allowed_temporal_evidence_ids: set[str] | None = None) -> list[dict]:
        allowed_temporal_evidence_ids = allowed_temporal_evidence_ids or set()
        if len(refs) > 30 or len({digest(ref) for ref in refs}) != len(refs):
            raise CaseError("invalid_evidence", "证据引用最多 30 条且不可重复")
        evidence = []
        for selection in refs:
            if isinstance(selection, str):
                ref_id, asset_id, asset_checksum, locator = selection, None, None, {"kind": "whole_sample"}
            elif isinstance(selection, dict):
                if set(selection) - {"evidence_ref_id", "asset_id", "asset_checksum", "locator"}:
                    raise CaseError("invalid_evidence", "证据选择包含未知字段", 422)
                ref_id = selection.get("evidence_ref_id")
                asset_id = selection.get("asset_id")
                asset_checksum = selection.get("asset_checksum")
                locator = selection.get("locator") or {"kind": "whole_sample"}
            else:
                raise CaseError("invalid_evidence", "证据选择格式无效", 422)
            if not isinstance(ref_id, str) or not ref_id:
                raise CaseError("invalid_evidence", "缺少证据引用 ID", 422)
            if not isinstance(locator, dict) or locator.get("kind") not in {
                "whole_sample", "whole_asset", "image_region", "point_cloud_view"}:
                raise CaseError("invalid_locator", "媒体定位方式无效", 422)
            if locator["kind"] == "image_region":
                if set(locator) != {"kind", "x", "y", "width", "height"}:
                    raise CaseError("invalid_locator", "图像区域须给出归一化坐标", 422)
                values = [locator.get(name) for name in ("x", "y", "width", "height")]
                if any(not isinstance(value, (int, float)) or isinstance(value, bool)
                       or not isfinite(value) for value in values):
                    raise CaseError("invalid_locator", "图像区域坐标必须是有限数字", 422)
                x, y, width, height = values
                if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 - x and 0 < height <= 1 - y):
                    raise CaseError("invalid_locator", "图像区域超出媒体边界", 422)
            elif locator["kind"] == "point_cloud_view":
                if set(locator) != {"kind", "yaw", "pitch", "zoom"}:
                    raise CaseError("invalid_locator", "点云视角须给出旋转和缩放", 422)
                values = [locator.get(name) for name in ("yaw", "pitch", "zoom")]
                if any(not isinstance(value, (int, float)) or isinstance(value, bool)
                       or not isfinite(value) for value in values) or not 0.1 <= values[2] <= 10:
                    raise CaseError("invalid_locator", "点云视角参数无效", 422)
            elif set(locator) != {"kind"}:
                raise CaseError("invalid_locator", "整件媒体引用不能附带其他定位参数", 422)
            row = self.db.get(EvidenceRef, ref_id)
            if row is None or row.ontology_id != self.ontology_id:
                raise CaseError("invalid_evidence", "证据引用与目标对象不匹配", 422)
            if row.assertion_id != target_id and row.id not in allowed_temporal_evidence_ids:
                target = self.db.get(EntityInstance, target_id)
                observation = self.db.get(EntityInstance, row.assertion_id)
                target_type = self.db.get(Entity, target.entity_id) if target else None
                observation_type = self.db.get(Entity, observation.entity_id) if observation else None
                if not (target and observation and target.ontology_id == self.ontology_id
                        and observation.ontology_id == self.ontology_id
                        and (target_type.name_en if target_type else None) == "Episode"
                        and (observation_type.name_en if observation_type else None) == "Observation"
                        and (target.row_data or {}).get("episode_id") == (observation.row_data or {}).get("episode_id")
                        and target.source_version == observation.source_version):
                    raise CaseError("invalid_evidence", "证据引用与目标对象不匹配", 422)
            selected = {"id": row.id, "source_version": row.source_version,
                             "content_hash": row.content_hash, "source_row_id": row.source_row_id,
                             "source_media_id": row.source_media_id, "locator": locator}
            if asset_id is not None:
                if not isinstance(asset_id, str) or not isinstance(asset_checksum, str) or len(asset_checksum) != 64:
                    raise CaseError("invalid_evidence", "媒体须携带资产 ID 和 SHA-256", 422)
                media = self.db.get(MediaItem, asset_id)
                sample = self.db.get(MultimodalSample, row.source_sample_id) if row.source_sample_id else None
                if not media or not sample or media.sample_id != sample.id or media.dataset_version_id != sample.dataset_version_id:
                    raise CaseError("invalid_evidence", "媒体不属于此样例或来源版本", 422)
                if media.checksum != asset_checksum:
                    raise CaseError("asset_version_conflict", "媒体摘要已变化，请重新选择证据", 409)
                if locator["kind"] == "image_region" and media.media_type not in {"image", "depth", "mask"}:
                    raise CaseError("invalid_locator", "此媒体不支持图像区域定位", 422)
                if locator["kind"] == "point_cloud_view" and media.media_type != "point_cloud":
                    raise CaseError("invalid_locator", "此媒体不是点云", 422)
                if ensure_available:
                    try:
                        present = get_storage_service().object_exists(media.storage_uri)
                    except Exception as exc:
                        raise CaseError("evidence_store_unavailable", "证据存储暂不可用", 503) from exc
                    if not present:
                        raise CaseError("evidence_unavailable", "证据媒体缺失，请补充或改选", 409)
                selected.update({"asset_id": media.id, "asset_role": media.asset_role,
                                 "asset_checksum": media.checksum,
                                 "dataset_version_id": media.dataset_version_id})
            elif locator["kind"] != "whole_sample":
                raise CaseError("invalid_locator", "媒体定位必须指定资产", 422)
            evidence.append(selected)
        return evidence

    def get_case(self, case_id: str, *, lock: bool = False) -> DecisionCase:
        query = self.db.query(DecisionCase).filter_by(id=case_id, ontology_id=self.ontology_id)
        case = query.with_for_update().first() if lock else query.first()
        if case is None:
            raise CaseError("not_found", "决策事项不存在或不可访问", 404)
        row, object_type, auth, _ = self._target(case.target_object_id)
        record = {"id": row.id, "entity_id": row.entity_id, "entity_type": object_type,
                  "object_type_resource_id": row.object_type_resource_id,
                  "properties": row.row_data or {}}
        action = self.db.get(OntologyActionType, case.action_type_id)
        fields = {effect.get("property") for effect in (action.effects or [])
                  if isinstance(effect, dict) and effect.get("property")} if action else set()
        fields.update(change.get("property") for change in
                      (case.preview_json or {}).get("diff", []) if isinstance(change, dict))
        if any(not action_field_permitted(auth["projection"], record, field)
               for field in fields if isinstance(field, str)):
            raise CaseError("not_found", "决策事项不存在或不可访问", 404)
        if self.user.role != "admin" and self.user.id not in {case.created_by, case.reviewed_by}:
            raise CaseError("not_found", "决策事项不存在或不可访问", 404)
        return case

    def _validate_window_context(self, *, kind: str, object_type: str, row: EntityInstance,
                                 context: dict, evidence_refs: list[str | dict],
                                 exclude_case_id: str | None = None) -> None:
        if kind != "maintenance_review" or object_type != "Episode":
            return
        required = {"window_start", "window_end", "rule_version", "analysis_run_id", "episode_id"}
        if not required.issubset(context):
            raise CaseError("window_required", "时序维护复核须绑定时间窗口、规则和分析运行", 422)
        try:
            window_start = float(context["window_start"])
            window_end = float(context["window_end"])
        except (TypeError, ValueError):
            raise CaseError("invalid_window", "时间窗口必须是数字", 422) from None
        if not isfinite(window_start) or not isfinite(window_end) or window_start > window_end:
            raise CaseError("invalid_window", "时间窗口起止无效", 422)
        analysis_run = self.db.get(DecisionLabRun, context["analysis_run_id"])
        if (analysis_run is None or analysis_run.domain != "factorynet"
                or analysis_run.ontology_id != self.ontology_id
                or analysis_run.rule_version != context["rule_version"]
                or analysis_run.source_digest != row.source_version
                or context["episode_id"] != (row.row_data or {}).get("episode_id")):
            raise CaseError("window_source_conflict", "窗口、规则或来源版本不匹配", 409)
        episode = next((item for item in (analysis_run.result_json or {}).get("rows", [])
                        if item.get("object_id") == row.id and item.get("name") == context["episode_id"]), None)
        points = episode.get("points", []) if episode else []
        if not points:
            raise CaseError("window_source_conflict", "分析运行没有此批次的采样点", 409)
        point_times = {str(point.get("source_row_id")): float(point["time_s"])
                       for point in points if point.get("source_row_id") is not None}
        selected_rows = {source_row_id for source_row_id, time_s in point_times.items()
                         if window_start <= time_s <= window_end}
        if not selected_rows:
            raise CaseError("window_empty", "时间窗口内没有分析运行的采样点", 422)
        observation_rows = set()
        for selection in evidence_refs:
            ref_id = selection if isinstance(selection, str) else selection.get("evidence_ref_id")
            evidence = self.db.get(EvidenceRef, ref_id)
            if evidence and evidence.assertion_id != row.id:
                observation_rows.add(str(evidence.source_row_id))
        if not observation_rows or not observation_rows.issubset(selected_rows):
            raise CaseError("window_evidence_conflict", "引用的时序采样点必须位于所选窗口内", 409)
        for prior in self.db.query(DecisionCase).filter_by(
                ontology_id=self.ontology_id, target_object_id=row.id,
                kind="maintenance_review").all():
            if prior.id == exclude_case_id or prior.status in {"rejected", "reverted", "failed"}:
                continue
            old = prior.context_json or {}
            if "window_start" not in old or "window_end" not in old:
                continue
            if window_start <= float(old["window_end"]) and float(old["window_start"]) <= window_end:
                raise CaseError("window_overlap_conflict",
                                f"时间窗口与已有事项 {prior.id} 重叠，请先处理或调整窗口", 409)

    @staticmethod
    def _validate_quality_context(kind: str, object_type: str, context: dict) -> None:
        if kind != "quality_review" or object_type != "InspectionSample":
            return
        if context.get("severity") not in {"low", "medium", "high", "unknown"}:
            raise CaseError("severity_required", "质量复核须选择严重度或未知", 422)
        pending = context.get("pending_evidence", [])
        if not isinstance(pending, list) or len(pending) > 10 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 300
                for item in pending):
            raise CaseError("invalid_pending_evidence", "待补证据最多 10 项，每项不超过 300 字", 422)

    def _validate_what_if_context(self, *, context: dict, target_id: str,
                                  principal: User | None = None) -> set[str]:
        """Revalidate pinned What-if inputs and authorize their evidence pointers."""
        if not isinstance(context, dict):
            raise CaseError("invalid_context", "决策上下文必须是对象", 422)
        if "what_if_scenarios" not in context:
            return set()
        refs = context.get("what_if_scenarios")
        if not isinstance(refs, list) or len(refs) > 30:
            raise CaseError("invalid_what_if_context", "What-if 情景引用最多 30 条", 422)
        if not refs:
            return set()
        try:
            serialized_refs = json.dumps(refs, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"), default=str, allow_nan=False)
        except (TypeError, ValueError):
            raise CaseError("invalid_what_if_context", "What-if 情景引用必须为有效 JSON", 422) from None
        if len(serialized_refs.encode("utf-8")) > 60_000:
            raise CaseError("what_if_context_too_large", "What-if 情景输入快照最多 60 KB", 422)

        from app.models.v2.dynamic_ontology import WhatIfScenario
        from app.schemas.v2.scenario_context import (
            ScenarioContext, scenario_inputs_digest, verify_scenario_context,
        )
        from app.services.v2.what_if_service import WhatIfError, revalidate_scenario_context

        actor = principal or self.user
        allowed_evidence_ids: set[str] = set()
        seen_scenario_ids: set[str] = set()
        for ref in refs:
            if (not isinstance(ref, dict)
                    or set(ref) != {"kind", "ref_id", "source_version", "permission_digest",
                                    "scenario_context", "scenario_input_digest", "assumptions",
                                    "rule_overrides"}
                    or ref.get("kind") != "what_if"
                    or not isinstance(ref.get("ref_id"), str)
                    or not isinstance(ref.get("source_version"), str)
                    or not isinstance(ref.get("permission_digest"), str)
                    or not isinstance(ref.get("scenario_context"), dict)
                    or not isinstance(ref.get("scenario_input_digest"), str)
                    or not isinstance(ref.get("assumptions"), list)
                    or not isinstance(ref.get("rule_overrides"), dict)):
                raise CaseError("invalid_what_if_context", "What-if 情景引用或情景输入快照格式无效", 422)
            try:
                scenario_input_digest = scenario_inputs_digest(
                    ref["assumptions"], ref["rule_overrides"])
            except (TypeError, ValueError):
                raise CaseError("invalid_what_if_context", "What-if 情景输入快照无法校验", 422) from None
            if scenario_input_digest != ref["scenario_input_digest"]:
                raise CaseError("what_if_input_snapshot_invalid", "What-if 情景输入摘要无效", 422)
            scenario_id = ref["ref_id"]
            if scenario_id in seen_scenario_ids:
                raise CaseError("invalid_what_if_context", "What-if 情景引用不可重复", 422)
            seen_scenario_ids.add(scenario_id)
            if not verify_scenario_context(ref["scenario_context"]):
                raise CaseError("invalid_what_if_context", "What-if ScenarioContext 合同摘要无效", 422)
            contract = ScenarioContext.model_validate(ref["scenario_context"])
            if (contract.ontology_id != self.ontology_id
                    or contract.scenario_branch_id != scenario_id
                    or contract.principal_id != str(actor.id)
                    or contract.context_digest != ref["source_version"]
                    or contract.permission_digest != ref["permission_digest"]):
                raise CaseError("what_if_context_scope_mismatch", "What-if 情景版本、本体或权限主体不匹配", 409)

            scenario = self.db.get(WhatIfScenario, scenario_id)
            if (not scenario or scenario.ontology_id != self.ontology_id
                    or str(scenario.created_by or "") != str(actor.id)):
                raise CaseError("not_found", "What-if 情景不存在或不可访问", 404)
            try:
                current_input_digest = scenario_inputs_digest(
                    scenario.assumptions_json or [], scenario.rule_overrides_json or {})
            except (TypeError, ValueError):
                raise CaseError("what_if_inputs_invalid", "What-if 情景当前输入格式无效", 409) from None
            if current_input_digest != ref["scenario_input_digest"]:
                raise CaseError("what_if_inputs_stale", "What-if 情景假设或规则覆盖已变化，请刷新 Agent 上下文", 409)
            try:
                baseline = revalidate_scenario_context(self.db, scenario, actor)
            except WhatIfError as exc:
                status = 409 if exc.status_code == 202 else exc.status_code
                raise CaseError(exc.code, str(exc), status) from exc
            current_contract = baseline.get("scenario_context")
            if (not verify_scenario_context(current_contract)
                    or current_contract.get("context_digest") != contract.context_digest):
                raise CaseError("what_if_context_stale", "What-if 情景基线或权限已变化，请重新读取情景", 409)
            baseline_nodes = baseline.get("nodes") if isinstance(baseline.get("nodes"), list) else []
            if not any(isinstance(node, dict) and str(node.get("id")) == str(target_id)
                       for node in baseline_nodes):
                raise CaseError("what_if_target_mismatch", "决策目标不在 What-if 授权基线范围内", 422)
            for record in [*baseline_nodes, *(baseline.get("edges") or [])]:
                if not isinstance(record, dict):
                    continue
                for evidence in record.get("evidence") or []:
                    if isinstance(evidence, dict) and isinstance(evidence.get("evidence_ref_id"), str):
                        allowed_evidence_ids.add(evidence["evidence_ref_id"])
        return allowed_evidence_ids

    def _validate_temporal_context(self, *, context: dict, target_id: str,
                                   target_type: str, evidence_refs: list[str | dict],
                                   principal: User | None = None) -> set[str]:
        if not isinstance(context, dict):
            raise CaseError("invalid_context", "决策上下文必须是对象", 422)
        if "temporal_events" not in context:
            return set()
        refs = context.get("temporal_events")
        if not isinstance(refs, list) or len(refs) > 30:
            raise CaseError("invalid_temporal_context", "时序事件引用最多 30 条", 422)
        if not refs:
            return set()

        target = self.db.query(EntityInstance).filter_by(
            id=target_id, ontology_id=self.ontology_id, is_deleted=False,
        ).first()
        target_entity = self.db.get(Entity, target.entity_id) if target else None
        current_target_type = (target_entity.name_en or target_entity.name_cn) if target_entity else None
        if not target or current_target_type != target_type:
            raise CaseError("temporal_target_unavailable", "时序决策目标不存在或类型已变化", 409)

        actor = principal or self.user
        temporal_evidence_ids: set[str] = set()
        for ref in refs:
            if (not isinstance(ref, dict)
                    or set(ref) != {"kind", "ref_id", "source_version", "permission_digest", "temporal"}
                    or ref.get("kind") != "temporal_event"
                    or not isinstance(ref.get("ref_id"), str)
                    or not isinstance(ref.get("source_version"), str)
                    or not isinstance(ref.get("permission_digest"), str)
                    or not isinstance(ref.get("temporal"), dict)):
                raise CaseError("invalid_temporal_context", "时序事件上下文格式无效", 422)

            event = self.db.get(TemporalStreamEvent, ref["ref_id"])
            replay = self.db.get(TemporalReplay, event.replay_id) if event else None
            if (not event or event.status != "committed" or not replay
                    or replay.ontology_id != self.ontology_id
                    or replay.source_id != "factorynet_cnc"):
                raise CaseError("temporal_source_unavailable", "时序事件或回放已不可用", 409)

            source_row_id = event.source_row_id or event.source_sequence
            row_identity = f"{event.episode_id}:{source_row_id}"
            instance = self.db.query(EntityInstance).filter_by(
                ontology_id=self.ontology_id, row_identity=row_identity, is_deleted=False,
            ).order_by(EntityInstance.created_at.asc(), EntityInstance.id.asc()).first()
            evidence_id = f"stream-evidence:{replay.id}:{event.id}"
            evidence = self.db.get(EvidenceRef, evidence_id)
            expected_evidence_source_version = str(
                (replay.config or {}).get("source_checksum") or replay.dataset_version_id or "")
            if (not instance or not evidence or evidence.ontology_id != self.ontology_id
                    or evidence.assertion_id != f"event:{event.event_key}"
                    or evidence.source_dataset_id != replay.dataset_id
                    or evidence.source_version != expected_evidence_source_version
                    or evidence.source_row_id != event.source_row_id
                    or evidence.revision_id != replay.schema_revision_id):
                raise CaseError("temporal_evidence_unavailable", "时序对象或来源证据无法核验", 409)

            expected_pin = {
                "replay_id": replay.id,
                "dataset_version_id": replay.dataset_version_id,
                "schema_revision_id": replay.schema_revision_id,
                "event_id": event.id,
                "event_key": event.event_key,
                "event_ordinal": float(event.ordinal) if event.ordinal is not None else None,
                "event_time": (event.source_ref or {}).get("event_time"),
                "episode_id": event.episode_id,
                "source_row_id": event.source_row_id,
                "source_sequence": event.source_sequence,
                "object_ref_id": instance.id,
                "evidence_ref_id": evidence.id,
                "evidence_content_hash": evidence.content_hash,
            }
            expected_source_version = digest({
                **expected_pin,
                "payload_hash": event.payload_hash,
                "payload_digest": digest(event.payload or {}),
                "instance_source_version": instance.source_version,
            })
            if (ref["temporal"] != expected_pin
                    or ref["source_version"] != expected_source_version):
                raise CaseError("temporal_source_changed", "时序事件、数据版本或证据已变化，请刷新上下文", 409)

            _, _, event_auth, _ = self._target(instance.id, principal=actor)
            if event_auth["projection"].permission_digest != ref["permission_digest"]:
                raise CaseError("temporal_permission_changed", "时序对象权限已变化，请重新固定上下文", 409)

            target_matches = (
                (target_type == "Observation" and target.id == instance.id)
                or (target_type == "Episode"
                    and (target.row_data or {}).get("episode_id") == event.episode_id
                    and target.source_version == instance.source_version)
                or (target_type == "DynamicIncident"
                    and (target.row_data or {}).get("event_id") == event.id
                    and (target.row_data or {}).get("run_id") == replay.id)
            )
            if not target_matches:
                raise CaseError("temporal_target_mismatch", "时序证据与决策目标没有可核验的对象关系", 422)
            temporal_evidence_ids.add(evidence.id)

        selected_evidence_ids = {
            selection if isinstance(selection, str) else selection.get("evidence_ref_id")
            for selection in evidence_refs if isinstance(selection, (str, dict))
        }
        if not temporal_evidence_ids.issubset(selected_evidence_ids):
            raise CaseError("temporal_evidence_required", "决策证据必须包含所有固定的时序事件证据", 422)
        return temporal_evidence_ids

    def create(self, *, kind: str, title: str, action_type_id: str, target_object_id: str,
               target_object_type: str, parameters: dict, evidence_refs: list[str | dict], reason: str,
               expected_etag: str, client_request_id: str, context: dict | None = None,
               commit: bool = True) -> DecisionCase:
        if self.user.role not in {"admin", "editor"}:
            raise CaseError("forbidden", "当前用户无权提交决策事项", 403)
        if kind not in KINDS:
            raise CaseError("invalid_kind", "未知决策事项类型")
        if context and context.get("scenario_id"):
            raise CaseError("unsupported_context", "Scenario 写回尚未接入此审核路径", 422)
        request = {"kind": kind, "title": title, "action_type_id": action_type_id,
                   "target_object_id": target_object_id, "target_object_type": target_object_type,
                   "parameters": parameters, "evidence_refs": evidence_refs, "reason": reason,
                   "expected_etag": expected_etag, "context": context or {}}
        request_digest = digest(request)
        context_digest = digest(context or {})
        existing = self.db.query(DecisionCase).filter_by(ontology_id=self.ontology_id,
            created_by=self.user.id, action_type_id=action_type_id,
            context_digest=context_digest, client_request_id=client_request_id).first()
        if existing:
            if existing.request_digest != request_digest:
                raise CaseError("idempotency_conflict", "请求编号已用于其他决策内容", 409)
            return self.get_case(existing.id)
        row, object_type, _, current_etag = self._target(target_object_id)
        if target_object_type != object_type:
            raise CaseError("object_type_mismatch", "对象类型与目标不一致", 422)
        if expected_etag != current_etag:
            raise CaseError("etag_conflict", "目标对象已变化，请重新读取", 409)
        self._action(action_type_id)
        selected_context = context or {}
        temporal_evidence_ids = self._validate_temporal_context(
            context=selected_context, target_id=row.id, target_type=object_type,
            evidence_refs=evidence_refs)
        what_if_evidence_ids = self._validate_what_if_context(
            context=selected_context, target_id=row.id)
        self._evidence(evidence_refs, target_object_id, ensure_available=False,
                       allowed_temporal_evidence_ids=temporal_evidence_ids | what_if_evidence_ids)
        self._validate_window_context(kind=kind, object_type=object_type, row=row,
                                      context=selected_context, evidence_refs=evidence_refs)
        self._validate_quality_context(kind, object_type, selected_context)
        if not evidence_refs and not reason.strip():
            raise CaseError("evidence_required", "须提供定位证据或人工例外理由", 422)
        case = DecisionCase(ontology_id=self.ontology_id, action_type_id=action_type_id,
            kind=kind, title=title, target_object_id=row.id, target_object_type=object_type,
            parameters=parameters, evidence_refs=evidence_refs, reason=reason,
            context_json=selected_context, context_digest=context_digest, expected_etag=expected_etag,
            client_request_id=client_request_id, request_digest=request_digest,
            status="draft", created_by=self.user.id)
        self.db.add(case)
        try:
            self.db.flush()
            _event(self.db, case, "created", self.user.id,
                   {"request_digest": request_digest, "target_object_id": row.id,
                    "what_if_context_digests": [ref["source_version"]
                        for ref in selected_context.get("what_if_scenarios", [])
                        if isinstance(ref, dict) and isinstance(ref.get("source_version"), str)],
                    "what_if_input_digests": [ref["scenario_input_digest"]
                        for ref in selected_context.get("what_if_scenarios", [])
                        if isinstance(ref, dict) and isinstance(ref.get("scenario_input_digest"), str)]},
                   str(uuid.uuid4()))
            if commit:
                self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.query(DecisionCase).filter_by(ontology_id=self.ontology_id,
                created_by=self.user.id, action_type_id=action_type_id,
                context_digest=context_digest, client_request_id=client_request_id).first()
            if existing and existing.request_digest == request_digest:
                return self.get_case(existing.id)
            raise CaseError("idempotency_conflict", "请求编号发生冲突", 409) from None
        return case

    def _preview(self, case: DecisionCase, *, lock: bool = False) -> tuple[dict, str, EntityInstance, dict]:
        action = self._action(case.action_type_id)
        initiator = self.db.get(User, case.created_by)
        if initiator is None or not initiator.is_active:
            raise CaseError("initiator_unavailable", "发起人已失去执行资格", 403)
        row, object_type, auth, etag = self._target(case.target_object_id, lock=lock, principal=initiator)
        if etag != case.expected_etag:
            raise CaseError("etag_conflict", "目标对象修订已变化，请重新预览", 409)
        if object_type != case.target_object_type:
            raise CaseError("object_type_mismatch", "目标对象类型已变化", 409)
        context = case.context_json or {}
        temporal_evidence_ids = self._validate_temporal_context(
            context=context, target_id=row.id, target_type=object_type,
            evidence_refs=case.evidence_refs or [], principal=initiator)
        what_if_evidence_ids = self._validate_what_if_context(
            context=context, target_id=row.id, principal=initiator)
        evidence = self._evidence(case.evidence_refs or [], row.id,
                                  allowed_temporal_evidence_ids=temporal_evidence_ids | what_if_evidence_ids)
        self._validate_window_context(kind=case.kind, object_type=object_type, row=row,
                                      context=context,
                                      evidence_refs=case.evidence_refs or [],
                                      exclude_case_id=case.id)
        self._validate_quality_context(case.kind, object_type, context)
        # Compile only against the caller's authorized property projection.
        # Bound Functions must not be able to derive visible writes from hidden
        # values in the underlying EntityInstance row.
        snapshot = SimpleNamespace(objects={(object_type, row.id): deepcopy(auth["record"]["properties"] or {})}, edges=[])
        invocation = {"action_type_id": action.id, "target_object_id": row.id,
                      "parameters": case.parameters}
        action_request = ActionRequest.from_invocations(
            ontology_id=self.ontology_id,
            compiler_scope="live",
            source="decision_case.preview",
            invocations=[invocation],
            execution_context=case.context_json or {},
            action_type_versions={action.id: action.version},
            expected_etag=case.expected_etag,
            client_request_id=case.client_request_id,
            preview_digest=case.preview_digest,
            permission_digest=case.permission_digest,
        )
        try:
            edits, runtime_permission_digest = compile_action_definitions(
                self.db, action_request, initiator, {action.id: action}, snapshot)
        except ScenarioError as exc:
            raise CaseError(exc.code, exc.message, exc.status) from exc
        if runtime_permission_digest != auth["projection"].permission_digest:
            raise CaseError("permission_changed", "执行期间权限发生变化；请重新预览", 409)
        changes = [edit for edit in edits if edit.op != "invoke_action"]
        if not changes or any(edit.op not in ALLOWED_EDIT_OPS for edit in changes):
            raise CaseError("unsupported_effect", "此审核路径当前只支持对象属性设置与清除", 422)
        before = deepcopy(row.row_data or {})
        after = deepcopy(before)
        visible_before = deepcopy(auth["record"]["properties"] or {})
        visible_after = deepcopy(visible_before)
        diff = []
        for edit in changes:
            if edit.target is None or edit.target.object_id != row.id or edit.target.concrete_type != object_type:
                raise CaseError("unsupported_effect", "Action 修改范围超出单一目标对象", 422)
            if not action_field_permitted(auth["projection"],
                {"id": row.id, "entity_id": row.entity_id, "entity_type": object_type,
                 "object_type_resource_id": row.object_type_resource_id, "properties": before}, edit.property):
                raise CaseError("forbidden_field", "目标字段不可见或无写入权限", 403)
            if edit.op == "set_property":
                after[edit.property] = edit.value
                visible_after[edit.property] = edit.value
            else:
                after.pop(edit.property, None)
                visible_after.pop(edit.property, None)
            diff.append({"op": edit.op, "property": edit.property,
                         "before": visible_before.get(edit.property), "after": visible_after.get(edit.property)})
        preview = {"case_id": case.id, "ontology_id": self.ontology_id, "action_type_id": action.id,
                   "action_version": action.version, "target": {"object_id": row.id, "object_type": object_type,
                   "expected_etag": etag}, "permission_decision": {"allowed": True,
                   "reason": auth["projection"].reason, "digest": auth["projection"].permission_digest},
                   "diff": diff, "before": visible_before, "after": visible_after, "evidence": evidence,
                   "before_digest": digest(before), "after_digest": digest(after),
                   "effect_class": "local_ontology_property", "reversible": True,
                   "projection": "async_reconcileable", "external_side_effects": False,
                   "source_version": row.source_version,
                   "context": case.context_json or {},
                   "ontology_revision_id": self.project.current_revision_id}
        return preview, digest(preview), row, after

    def update(self, case_id: str, *, title: str, parameters: dict,
               evidence_refs: list[str | dict], reason: str, expected_etag: str,
               context: dict | None = None) -> DecisionCase:
        case = self.get_case(case_id, lock=True)
        if case.created_by != self.user.id:
            raise CaseError("forbidden", "只有发起人可以修改事项", 403)
        if case.status not in {"draft", "changes_requested"}:
            raise CaseError("invalid_state", "当前事项不能修改", 409)
        row, object_type, _, current_etag = self._target(case.target_object_id)
        if current_etag != expected_etag or object_type != case.target_object_type:
            raise CaseError("etag_conflict", "目标对象已变化，请重新读取", 409)
        next_context = case.context_json or {} if context is None else context
        temporal_evidence_ids = self._validate_temporal_context(
            context=next_context, target_id=case.target_object_id,
            target_type=object_type, evidence_refs=evidence_refs)
        what_if_evidence_ids = self._validate_what_if_context(
            context=next_context, target_id=case.target_object_id)
        self._evidence(evidence_refs, case.target_object_id, ensure_available=False,
                       allowed_temporal_evidence_ids=temporal_evidence_ids | what_if_evidence_ids)
        self._validate_window_context(kind=case.kind, object_type=object_type, row=row,
                                      context=next_context, evidence_refs=evidence_refs,
                                      exclude_case_id=case.id)
        self._validate_quality_context(case.kind, object_type, next_context)
        if not evidence_refs and not reason.strip():
            raise CaseError("evidence_required", "须提供定位证据或人工例外理由", 422)
        previous = digest({"title": case.title, "parameters": case.parameters,
                           "evidence_refs": case.evidence_refs, "reason": case.reason,
                           "expected_etag": case.expected_etag, "context": case.context_json})
        case.title, case.parameters = title, parameters
        case.evidence_refs, case.reason = evidence_refs, reason
        # The idempotency scope records the creation request; edits bind the actual context in preview.
        case.context_json = next_context
        case.expected_etag = expected_etag
        case.status = "draft"
        case.preview_json = None
        case.preview_digest = None
        case.permission_digest = None
        case.action_version = None
        case.reviewed_by = None
        case.review_comment = None
        case.reviewed_at = None
        current = digest({"title": title, "parameters": parameters,
                          "evidence_refs": evidence_refs, "reason": reason,
                          "expected_etag": expected_etag, "context": next_context})
        _event(self.db, case, "updated", self.user.id,
               {"before_digest": previous, "after_digest": current}, str(uuid.uuid4()))
        self.db.commit()
        return case

    def preview(self, case_id: str) -> dict:
        case = self.get_case(case_id)
        preview, preview_digest, _, _ = self._preview(case)
        return {"preview": preview, "preview_digest": preview_digest,
                "status": case.status, "requires_review": True}

    def submit(self, case_id: str, expected_preview_digest: str) -> DecisionCase:
        case = self.get_case(case_id, lock=True)
        if case.created_by != self.user.id:
            raise CaseError("forbidden", "只有发起人可以提交审核", 403)
        if case.status not in {"draft", "changes_requested"}:
            raise CaseError("invalid_state", "当前事项不可提交审核", 409)
        preview, current_digest, _, _ = self._preview(case)
        if current_digest != expected_preview_digest:
            raise CaseError("preview_conflict", "预览已变化，请重新查看", 409)
        case.preview_json = preview
        case.preview_digest = current_digest
        case.permission_digest = preview["permission_decision"]["digest"]
        case.action_version = preview["action_version"]
        case.status = "ready_for_review"
        case.submitted_at = datetime.now(timezone.utc)
        _event(self.db, case, "submitted", self.user.id,
               {"preview_digest": current_digest, "permission_digest": case.permission_digest}, str(uuid.uuid4()))
        self.db.commit()
        return case

    def review(self, case_id: str, decision: str, comment: str) -> DecisionCase:
        case = self.get_case(case_id, lock=True)
        if self.user.role != "admin":
            raise CaseError("forbidden", "当前用户无审核权限", 403)
        if self.user.id == case.created_by and settings.auth_mode != "local_single_user":
            raise CaseError("self_review_forbidden", "正式多用户模式须由其他审核人批准", 403)
        if case.status != "ready_for_review":
            raise CaseError("invalid_state", "事项不在待审核状态", 409)
        if decision not in {"approved", "rejected", "changes_requested"}:
            raise CaseError("invalid_review", "审核决定无效")
        if decision == "approved":
            try:
                _, current_digest, _, _ = self._preview(case)
                if current_digest != case.preview_digest:
                    raise CaseError("preview_conflict", "目标、规则、权限或证据已变化，须重新预览", 409)
            except CaseError as exc:
                _event(self.db, case, "review_blocked", self.user.id,
                       {"code": exc.code, "status": exc.status}, str(uuid.uuid4()))
                self.db.commit()
                raise
        case.status = decision
        case.reviewed_by = self.user.id
        case.review_comment = comment
        case.reviewed_at = datetime.now(timezone.utc)
        _event(self.db, case, "reviewed", self.user.id,
               {"decision": decision, "preview_digest": case.preview_digest,
                "same_identity_demo": self.user.id == case.created_by,
                "comment": comment}, str(uuid.uuid4()))
        self.db.commit()
        return case

    def execute(self, case_id: str) -> tuple[DecisionCase, OntologyActionRun]:
        case = self.get_case(case_id, lock=True)
        if case.status == "completed" and case.action_run_id:
            run = self.db.get(OntologyActionRun, case.action_run_id)
            if run:
                return case, run
        if case.status != "approved":
            raise CaseError("invalid_state", "事项须先审核通过", 409)
        if self.user.role not in {"admin", "editor"}:
            raise CaseError("forbidden", "当前用户无执行权限", 403)
        try:
            preview, current_digest, row, after = self._preview(case, lock=True)
            if current_digest != case.preview_digest or preview["action_version"] != case.action_version:
                raise CaseError("preview_conflict", "审核后的预览已变化，须重新审核", 409)
            # Approval binds the initiator's policy digest; the actual executor
            # must independently retain Action and field permissions at commit.
            action = self._action(case.action_type_id)
            permission_rules = action.permission_rules or []
            if permission_rules and not any(
                ("role" not in rule or rule["role"] == self.user.role)
                and ("principal_id" not in rule or rule["principal_id"] == self.user.id)
                for rule in permission_rules if isinstance(rule, dict)
            ):
                raise CaseError("forbidden", "执行人无此 Action 的权限", 403)
            _, _, executor_auth, _ = self._target(row.id)
            for change in preview["diff"]:
                field = change["property"]
                if not action_field_permitted(executor_auth["projection"],
                    {"id": row.id, "entity_id": row.entity_id, "entity_type": case.target_object_type,
                     "object_type_resource_id": row.object_type_resource_id, "properties": row.row_data or {}}, field):
                    raise CaseError("forbidden_field", "执行人无目标字段权限", 403)
        except CaseError as exc:
            _event(self.db, case, "execution_blocked", self.user.id,
                   {"code": exc.code, "status": exc.status}, str(uuid.uuid4()))
            self.db.commit()
            raise
        trace_id = str(uuid.uuid4())
        before = deepcopy(row.row_data or {})
        changed_properties = {item["property"] for item in preview["diff"]}
        before_action_snapshot = {
            name: before[name] for name in changed_properties
            if name in before and action_property_name_allowed(name)
        }
        after_action_snapshot = {
            name: after[name] for name in changed_properties
            if name in after and action_property_name_allowed(name)
        }
        run = create_action_run_record(
            db=self.db,
            action_type_id=case.action_type_id,
            ontology_id=self.ontology_id,
            target_object_id=row.id,
            parameters=case.parameters,
            execution_context=case.context_json or {},
            initial_status="running",
            requester_id=case.created_by,
            reviewer_id=case.reviewed_by,
            executor_id=self.user.id,
            decision_case_id=case.id,
            client_request_id=case.client_request_id, input_digest=case_input_digest(case),
            preview_digest=case.preview_digest, permission_digest=case.permission_digest,
            before_digest=digest(before), after_digest=digest(after), expected_etag=case.expected_etag,
            action_version=case.action_version, trace_id=trace_id, projection_status="pending")
        _action_runtime_call(
            complete_action_run,
            self.db,
            run,
            # Keep only Action-touched, explicitly Action-visible fields in
            # the audit snapshots. The full-state digests still guard revert.
            before_snapshot={row.id: before_action_snapshot},
            after_snapshot={row.id: after_action_snapshot},
            side_effect_results=preview["diff"],
        )
        row.row_data = after
        self.db.add(run)
        self.db.flush()
        case.action_run_id = run.id
        case.status = "completed"
        _event(self.db, case, "executed", self.user.id,
               {"run_id": run.id, "before_digest": run.before_digest, "after_digest": run.after_digest,
                "preview_digest": case.preview_digest, "projection_status": "pending"}, trace_id)
        self.db.commit()
        self.sync_projection(run)
        return case, run

    def case_for_run(self, original: OntologyActionRun) -> DecisionCase:
        if original.decision_case_id:
            case_id = original.decision_case_id
        else:
            parameters = original.parameters if isinstance(original.parameters, dict) else {}
            original_run_id = parameters.get("revert_of")
            source = self.db.query(OntologyActionRun).filter_by(
                id=original_run_id, ontology_id=self.ontology_id,
            ).first() if isinstance(original_run_id, str) else None
            case_id = source.decision_case_id if source and source.status == "reverted" else None
        case = self.db.query(DecisionCase).filter_by(
            id=case_id, ontology_id=self.ontology_id,
        ).first() if case_id else None
        if case is None:
            raise CaseError("case_not_found", "Action 对应的决策记录不存在", 409)
        return case

    def _revert_plan(self, original: OntologyActionRun, *, lock: bool = False) -> dict:
        if original.decision_case_id is None or original.ontology_id != self.ontology_id:
            raise CaseError("unsupported_run", "此运行不属于可撤销的审核写入路径", 422)
        if self.user.role != "admin" and original.executed_by != self.user.id:
            raise CaseError("forbidden", "只有执行人或管理员可撤销此写回", 403)
        if original.status != "completed":
            raise CaseError("invalid_state", "此运行已撤销或未完成", 409)
        effects = original.side_effect_results or []
        if not effects or any(not isinstance(item, dict) or item.get("op") not in ALLOWED_EDIT_OPS for item in effects):
            raise CaseError("unsupported_effect", "仅支持本地属性编辑的自动撤销", 422)
        case = self.db.get(DecisionCase, original.decision_case_id)
        if case is None or case.status != "completed" or case.action_run_id != original.id:
            raise CaseError("invalid_state", "决策事项和执行记录不一致", 409)
        row, object_type, auth, etag = self._target(original.target_object_id, lock=lock)
        previous = (original.before_snapshot or {}).get(row.id)
        executed = (original.after_snapshot or {}).get(row.id)
        if not isinstance(previous, dict) or not isinstance(executed, dict):
            raise CaseError("invalid_snapshot", "执行前后快照不完整，需人工补偿", 422)
        if object_type != case.target_object_type or digest(row.row_data or {}) != original.after_digest:
            raise CaseError("etag_conflict", "对象在执行后已有其他修改，需人工处理", 409)
        record = {"id": row.id, "entity_id": row.entity_id, "entity_type": object_type,
                  "object_type_resource_id": row.object_type_resource_id, "properties": row.row_data or {}}
        if any(not isinstance(effect.get("property"), str)
               or not effect.get("property")
               or not action_property_name_allowed(effect.get("property"))
               for effect in effects):
            raise CaseError("invalid_snapshot", "执行记录缺少属性名称，需人工补偿", 422)
        effects_by_property: dict[str, list[dict]] = {}
        for effect in effects:
            effects_by_property.setdefault(effect["property"], []).append(effect)
        changed_properties = {
            prop for prop in set(previous) | set(executed)
            if previous.get(prop) != executed.get(prop)
            or (prop in previous) != (prop in executed)
        }
        if not changed_properties.issubset(effects_by_property):
            raise CaseError("invalid_snapshot", "执行记录与属性快照不一致，需人工补偿", 422)
        diff = []
        for prop, property_effects in effects_by_property.items():
            if not action_field_permitted(auth["projection"], record, prop):
                raise CaseError("forbidden_field", "当前用户无目标字段权限", 403)
            first_effect = property_effects[0]
            last_effect = property_effects[-1]
            if first_effect.get("before") != previous.get(prop):
                raise CaseError("invalid_snapshot", "执行记录与属性快照不一致，需人工补偿", 422)
            for earlier, later in zip(property_effects, property_effects[1:]):
                if earlier.get("after") != later.get("before"):
                    raise CaseError("invalid_snapshot", "执行记录中的属性变更顺序不一致，需人工补偿", 422)
            if last_effect.get("op") == "set_property":
                if prop not in executed or executed[prop] != last_effect.get("after"):
                    raise CaseError("invalid_snapshot", "执行记录与属性快照不一致，需人工补偿", 422)
            elif prop in executed:
                raise CaseError("invalid_snapshot", "执行记录与属性快照不一致，需人工补偿", 422)
            if prop not in changed_properties:
                continue
            diff.append({"op": "set_property" if prop in previous else "unset_property",
                         "property": prop, "before": executed.get(prop), "after": previous.get(prop)})
        permission_digest = auth["projection"].permission_digest
        preview_digest = digest({
            "ontology_id": self.ontology_id,
            "original_run_id": original.id,
            "decision_case_id": case.id,
            "target_object_id": row.id,
            "expected_etag": etag,
            "permission_digest": permission_digest,
            "action_version": original.action_version,
            "diff": diff,
        })
        return {
            "case": case,
            "row": row,
            "object_type": object_type,
            "previous": previous,
            "executed": executed,
            "diff": diff,
            "expected_etag": etag,
            "permission_digest": permission_digest,
            "preview_digest": preview_digest,
        }

    def preview_revert(self, original: OntologyActionRun) -> dict:
        plan = self._revert_plan(original)
        return {
            "original_run_id": original.id,
            "target": {"object_id": plan["row"].id, "object_type": plan["object_type"]},
            "expected_etag": plan["expected_etag"],
            "permission_digest": plan["permission_digest"],
            "preview_digest": plan["preview_digest"],
            "preview": {"diff": plan["diff"], "reversible": True},
        }

    def revert(
        self,
        original: OntologyActionRun,
        *,
        client_request_id: str,
        expected_preview_digest: str,
        reason: str,
    ) -> OntologyActionRun:
        if original.decision_case_id is None or original.ontology_id != self.ontology_id:
            raise CaseError("unsupported_run", "此运行不属于可撤销的审核写入路径", 422)
        request_id = client_request_id.strip()
        reason = reason.strip()
        if not request_id or len(request_id) > 120 or not reason or len(reason) > 4000:
            raise CaseError("invalid_revert_request", "回退请求编号和理由为必填项且不得超长", 422)
        if self.user.role != "admin" and original.executed_by != self.user.id:
            raise CaseError("forbidden", "只有执行人或管理员可撤销此写回", 403)

        scope = f"decision-case-revert:{original.id}"
        request_digest = digest({
            "ontology_id": self.ontology_id,
            "actor_id": str(self.user.id),
            "original_run_id": original.id,
            "preview_digest": expected_preview_digest,
            "reason": reason,
        })
        prior = _action_runtime_call(
            find_idempotent_action_run,
            self.db,
            ontology_id=self.ontology_id,
            actor_id=str(self.user.id),
            scope=scope,
            request_id=request_id,
            request_digest=request_digest,
        )
        if prior is not None:
            return prior

        case = self.db.get(DecisionCase, original.decision_case_id)
        if case is None:
            raise CaseError("case_not_found", "Action 对应的决策记录不存在", 409)
        reverted_event = self.db.query(DecisionCaseEvent).filter_by(
            case_id=case.id, event_type="reverted",
        ).order_by(DecisionCaseEvent.sequence.desc()).first()
        if reverted_event is not None:
            raise CaseError("invalid_state", "此运行已存在回退记录", 409)

        plan = self._revert_plan(original, lock=True)
        if plan["preview_digest"] != expected_preview_digest:
            raise CaseError("preview_conflict", "对象或权限已变化，请重新预览", 409)
        trace_id = str(uuid.uuid4())
        reversal = [{"op": item["op"], "property": item["property"]} for item in plan["diff"]]
        before_state = deepcopy(plan["row"].row_data or {})
        after_state = deepcopy(before_state)
        changed_properties = {item["property"] for item in plan["diff"]}
        for item in plan["diff"]:
            if item["op"] == "set_property":
                after_state[item["property"]] = item["after"]
            else:
                after_state.pop(item["property"], None)
        before_action_snapshot = {
            name: before_state[name] for name in changed_properties
            if name in before_state and action_property_name_allowed(name)
        }
        after_action_snapshot = {
            name: after_state[name] for name in changed_properties
            if name in after_state and action_property_name_allowed(name)
        }
        run = create_action_run_record(
            db=self.db,
            action_type_id=original.action_type_id,
            ontology_id=self.ontology_id,
            target_object_id=plan["row"].id,
            parameters={"revert_of": original.id, "reason": reason},
            execution_context=original.execution_context or {},
            initial_status="running",
            requester_id=self.user.id,
            executor_id=self.user.id,
            client_request_id=request_id,
            idempotency_scope=scope,
            input_digest=request_digest,
            preview_digest=plan["preview_digest"],
            permission_digest=plan["permission_digest"],
            before_digest=digest(before_state), after_digest=digest(after_state),
            expected_etag=plan["expected_etag"], action_version=original.action_version,
            trace_id=trace_id, projection_status="pending")
        _action_runtime_call(
            complete_action_run,
            self.db,
            run,
            before_snapshot={plan["row"].id: before_action_snapshot},
            after_snapshot={plan["row"].id: after_action_snapshot},
            side_effect_results=reversal,
        )
        plan["row"].row_data = after_state
        _action_runtime_call(
            transition_action_run,
            self.db,
            original,
            "reverted",
            actor_id=self.user.id,
            actor_kind="user",
        )
        original.projection_status = "superseded"
        case.status = "reverted"
        self.db.add(run)
        self.db.flush()
        _event(self.db, case, "reverted", self.user.id,
               {"original_run_id": original.id, "revert_run_id": run.id,
                "reason": reason,
                "before_digest": run.before_digest, "after_digest": run.after_digest}, trace_id)
        self.db.commit()
        self.sync_projection(run)
        return run

    def sync_projection(
        self,
        run: OntologyActionRun,
        *,
        manual_retry: bool = False,
        manual_reason: str | None = None,
        client_request_id: str | None = None,
        actor_kind: str = "user",
    ) -> OntologyActionRun:
        if run.status != "completed":
            raise CaseError("unsupported_run", "此运行不属于可修复的审核写入路径", 422)
        case = self.case_for_run(run)
        with _action_projection_lock(self.db, run.id) as acquired:
            if not acquired:
                raise CaseError("projection_attempt_in_progress", "该投影已有协调任务正在执行", 409)
            return self._sync_projection_locked(
                run,
                manual_retry=manual_retry,
                manual_reason=manual_reason,
                client_request_id=client_request_id,
                actor_kind=actor_kind,
            )

    def _sync_projection_locked(
        self,
        run: OntologyActionRun,
        *,
        manual_retry: bool = False,
        manual_reason: str | None = None,
        client_request_id: str | None = None,
        actor_kind: str = "user",
    ) -> OntologyActionRun:
        if run.status != "completed":
            raise CaseError("unsupported_run", "此运行不属于可修复的审核写入路径", 422)
        case = self.case_for_run(run)
        if manual_retry:
            reason = (manual_reason or "").strip()
            request_id = (client_request_id or "").strip()
            if not reason or not request_id:
                raise CaseError(
                    "manual_retry_audit_required",
                    "人工重放必须提供理由和请求编号",
                    422,
                )
            if len(reason) > 4000 or len(request_id) > 120:
                raise CaseError("manual_retry_audit_invalid", "人工重放审计字段超出长度限制", 422)
        else:
            reason = None
            request_id = None

        if actor_kind not in {"user", "system"}:
            raise CaseError("invalid_projection_actor", "投影操作者类型无效", 422)
        actor_id = getattr(self.user, "id", None) if actor_kind != "system" else None
        if manual_retry and actor_kind != "user":
            raise CaseError("manual_retry_actor_required", "人工重放必须由具名用户发起", 403)
        attempt_kind = "manual_retry" if manual_retry else (
            "automatic_retry" if actor_kind == "system" else "initial"
        )
        request_digest = digest({
            "run_id": run.id,
            "actor_id": str(actor_id) if actor_id is not None else None,
            "reason": reason,
        }) if manual_retry else None

        # A manual HTTP retry can be safely repeated after a lost response. The
        # case event stream is append-only and scoped to this ActionRun/actor.
        if manual_retry:
            events = self.db.query(DecisionCaseEvent).filter_by(
                case_id=case.id, event_type="projection_attempt_started",
            ).order_by(DecisionCaseEvent.sequence.desc()).all()
            starts = [event for event in events if (event.payload or {}).get("run_id") == run.id
                      and (event.payload or {}).get("attempt_kind") == "manual_retry"
                      and (event.payload or {}).get("client_request_id") == request_id
                      and event.actor_id == actor_id]
            if starts:
                start = starts[0]
                if (start.payload or {}).get("request_digest") != request_digest:
                    raise CaseError("idempotency_conflict", "人工重放请求编号已用于不同理由", 409)
                attempt_id = (start.payload or {}).get("attempt_id")
                finished = self.db.query(DecisionCaseEvent).filter_by(
                    case_id=case.id, event_type="projection_attempt_finished",
                ).order_by(DecisionCaseEvent.sequence.desc()).all()
                if any((event.payload or {}).get("attempt_id") == attempt_id for event in finished):
                    return run
                raise CaseError(
                    "projection_attempt_in_progress",
                    "该人工重放已有持久开始记录但尚无终态；请先查看运行事件",
                    409,
                )
            if run.projection_status == "synced":
                return run
            if run.projection_status not in {"pending", "failed"}:
                raise CaseError("projection_not_retryable", "只有待投影或失败的 ActionRun 可以人工重放", 409)

        # A later attempt may observe an older start without a finish event if
        # its worker exited between SQL and graph commits. Record that outcome
        # as unknown; the graph upsert below is idempotent for the pinned SQL row.
        prior_starts = self.db.query(DecisionCaseEvent).filter_by(
            case_id=case.id, event_type="projection_attempt_started",
        ).order_by(DecisionCaseEvent.sequence.asc()).all()
        finished_events = self.db.query(DecisionCaseEvent).filter_by(
            case_id=case.id, event_type="projection_attempt_finished",
        ).all()
        finished_ids = {(event.payload or {}).get("attempt_id") for event in finished_events}
        for prior in prior_starts:
            prior_payload = prior.payload or {}
            if prior_payload.get("run_id") != run.id or prior_payload.get("attempt_id") in finished_ids:
                continue
            _event(self.db, case, "projection_attempt_finished", actor_id,
                   {"run_id": run.id, "attempt_id": prior_payload.get("attempt_id"),
                    "attempt_kind": prior_payload.get("attempt_kind"),
                    "attempt_number": prior_payload.get("attempt_number"),
                    "status": "outcome_unknown",
                    "observed_by_kind": actor_kind,
                    "error": "A prior projection did not record a terminal result; it may have reached the graph."},
                   run.trace_id or str(uuid.uuid4()))

        if manual_retry:
            run.projection_manual_attempts = (run.projection_manual_attempts or 0) + 1
            attempt_number = run.projection_manual_attempts
        else:
            # Initial and automatic projection attempts share the bounded
            # automatic budget. Operator retries never reset or consume it.
            run.projection_attempts = (run.projection_attempts or 0) + 1
            attempt_number = run.projection_attempts

        attempt_id = str(uuid.uuid4())
        run.projection_last_attempt_at = datetime.now(timezone.utc)
        run.projection_next_attempt_at = None
        _event(self.db, case, "projection_attempt_started", actor_id,
               {"run_id": run.id, "attempt_id": attempt_id,
                "attempt_kind": attempt_kind, "attempt_number": attempt_number,
                "actor_kind": actor_kind,
                "reason": reason,
                "client_request_id": request_id,
                "request_digest": request_digest},
               run.trace_id or str(uuid.uuid4()))
        # Persist the counter and start event before touching the graph.
        self.db.commit()

        def finish_attempt(status: str, error: str | None = None) -> None:
            _event(self.db, case, "projection_attempt_finished", actor_id,
                   {"run_id": run.id, "attempt_id": attempt_id,
                    "attempt_kind": attempt_kind, "attempt_number": attempt_number,
                    "actor_kind": actor_kind, "status": status, "error": error,
                    "reason": reason, "client_request_id": request_id},
                   run.trace_id or str(uuid.uuid4()))

        row = self.db.query(EntityInstance).filter_by(id=run.target_object_id,
                                                       ontology_id=self.ontology_id,
                                                       is_deleted=False).first()
        if row is None or digest(row.row_data or {}) != run.after_digest:
            run.projection_status = "conflict"
            run.projection_error = "SQL object changed after this Action; reconcile requires current source"
            run.projection_next_attempt_at = None
            _event(self.db, case, "projection_conflict", actor_id,
                   {"run_id": run.id, "projection_status": "conflict", "attempt_id": attempt_id},
                   run.trace_id or str(uuid.uuid4()))
            finish_attempt("conflict", run.projection_error)
            self.db.commit()
            return run
        entity = self.db.get(Entity, row.entity_id)
        object_type = (entity.name_en or entity.name_cn) if entity else None
        if not object_type:
            run.projection_status = "failed"
            run.projection_error = "Object type unavailable"
            run.projection_next_attempt_at = _projection_retry_at(run.projection_attempts)
            finish_attempt("failed", run.projection_error)
            self.db.commit()
            return run
        graph = FalkorDBService()
        try:
            if not graph.available:
                raise RuntimeError("FalkorDB unavailable")
            props = {key: value for key, value in (row.row_data or {}).items()
                     if value is None or isinstance(value, (str, int, float, bool))}
            old = (run.before_snapshot or {}).get(row.id, {})
            props.update({key: None for key in old if key not in (row.row_data or {})})
            if graph.upsert_instances(self.ontology_id,
                [{"id": row.id, "entity_type": object_type, "properties": props}]) != 1:
                raise RuntimeError("Graph projection did not accept the object")
            run.projection_status = "synced"
            run.projection_error = None
            run.projection_next_attempt_at = None
        except Exception as exc:
            run.projection_status = "failed"
            run.projection_error = f"Projection retry required: {type(exc).__name__}"
            run.projection_next_attempt_at = _projection_retry_at(run.projection_attempts)
        _event(self.db, case, "projection_" + run.projection_status, actor_id,
               {"run_id": run.id, "projection_status": run.projection_status,
                "attempt_id": attempt_id}, run.trace_id or str(uuid.uuid4()))
        finish_attempt(run.projection_status, run.projection_error)
        self.db.commit()
        return run


def case_payload(case: DecisionCase, run: OntologyActionRun | None = None) -> dict:
    reviewed_preview = deepcopy(case.preview_json)
    if isinstance(reviewed_preview, dict):
        for state_name in ("before", "after"):
            if isinstance(reviewed_preview.get(state_name), dict):
                reviewed_preview[state_name] = action_properties_view(reviewed_preview[state_name])
        if isinstance(reviewed_preview.get("diff"), list):
            reviewed_preview["diff"] = [
                item for item in reviewed_preview["diff"]
                if isinstance(item, dict) and action_property_name_allowed(item.get("property"))
            ]
    return {"id": case.id, "ontology_id": case.ontology_id, "kind": case.kind,
            "title": case.title, "status": case.status, "action_type_id": case.action_type_id,
            "target": {"object_id": case.target_object_id, "object_type": case.target_object_type,
                       "expected_etag": case.expected_etag},
            "parameters": case.parameters, "evidence_refs": case.evidence_refs or [],
            "reason": case.reason, "context": case.context_json or {},
            "preview_digest": case.preview_digest, "permission_digest": case.permission_digest,
            "reviewed_preview": reviewed_preview,
            "action_version": case.action_version, "created_by": case.created_by,
            "reviewed_by": case.reviewed_by, "review_comment": case.review_comment,
            "action_run_actors": ({
                "requester_id": run.requested_by,
                "reviewer_id": run.reviewed_by,
                "executor_id": run.executed_by,
            } if run else None),
            "action_run_id": case.action_run_id,
            "projection_status": run.projection_status if run else None,
            "projection_attempts": run.projection_attempts if run else None,
            "projection_manual_attempts": run.projection_manual_attempts if run else None,
            "created_at": case.created_at.isoformat() if case.created_at else None,
            "updated_at": case.updated_at.isoformat() if case.updated_at else None}
