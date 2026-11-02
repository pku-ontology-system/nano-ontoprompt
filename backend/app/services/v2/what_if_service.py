"""Version-pinned temporal What-If compiler and isolated rule-scenario runner.

The runner operates on a JSON context copied from a selected temporal object
and never writes to FalkorDB or the published ontology.  FactoryNet Observation
rules remain available as a domain-specific example.  The reasoning engine is
deliberately bounded; when the optional Semantica package is installed the
engine marker is retained so the serialized result records which
implementation was used.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, or_

from app.models.entity import Entity
from app.models.logic import LogicRule
from app.models.ontology import OntologyProject
from app.models.ontology_revision import OntologyRevision
from app.models.v2.dataset import DatasetVersion
from app.models.v2.dynamic_ontology import WhatIfRun, WhatIfScenario
from app.models.v2.temporal_replay import DataModelSnapshot, TemporalFact, TemporalReplay, TemporalStreamEvent
from app.schemas.v2.scenario_context import (
    ScenarioContext,
    scenario_inputs_digest,
    seal_scenario_context,
    verify_scenario_context,
)
from app.services.v2.temporal_source_adapters import (
    FACTORYNET_OBSERVATION_PREFIX,
    TemporalSourceAdapter,
    TemporalSourceAdapterError,
    adapter_for_replay,
)
from app.services.v2.vector.chroma_service import MAX_TEMPORAL_INDEX_FACTS, temporal_replay_index_source_revision

ENGINE_COMMIT = "3a69721abf72d7188a0d6fd72c8462261b2c44eb"
MAX_NODES = 500
MAX_FACTS = 2000
MAX_RULES = 50
MAX_PREMISES = 4
MAX_ITERATIONS = 50
SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_:./#-]+")

_WHAT_IF_RUN_ERROR_MESSAGES = {
    "WHAT_IF_CANCELLED": "推演任务已取消。",
    "WHAT_IF_ACTOR_UNAVAILABLE": "运行提交者已不可用或已失去本体访问权限。",
    "WHAT_IF_SCENARIO_UNAVAILABLE": "推演情景已不可用。",
    "WHAT_IF_OWNER_MISMATCH": "推演情景所有权已变化，任务未执行。",
    "WHAT_IF_INPUT_CHANGED": "情景输入在排队后发生变化，请重新提交。",
    "WHAT_IF_INVALID": "推演输入或情景数据未通过校验。",
    "WHAT_IF_RUN_FAILED": "推演执行失败，请检查任务状态后重试。",
    "WHAT_IF_AS_OF_UNPUBLISHED": "所选时间位置尚未包含在已发布的时序快照中。",
    "WHAT_IF_DATASET_VERSION_INVALID": "推演固定的数据版本不可用或不完整。",
    "WHAT_IF_DATASET_VERSION_MISSING": "时序回放未固定数据版本，无法执行推演。",
    "WHAT_IF_REPLAY_REQUIRED": "请先选择已发布的时序回放。",
    "WHAT_IF_SNAPSHOT_HASH_MISMATCH": "已发布时序快照校验失败。",
    "WHAT_IF_SNAPSHOT_INVALID": "已发布时序快照无效或版本不匹配。",
    "WHAT_IF_SNAPSHOT_MISSING": "尚无可用的已发布时序快照。",
    "WHAT_IF_SNAPSHOT_STALE": "已发布时序快照已过期，请重新发布。",
    "WHAT_IF_SOURCE_ADAPTER_UNAVAILABLE": "该时序来源缺少可用的固定映射。",
    "WHAT_IF_VECTOR_INDEXING": "时序证据索引仍在构建，请完成后重试。",
    "WHAT_IF_VECTOR_INDEX_FAILED": "时序证据索引构建失败。",
    "WHAT_IF_VECTOR_INDEX_STALE": "时序证据索引已过期，请重新构建。",
    "WHAT_IF_VECTOR_SEARCH_FAILED": "时序证据检索失败。",
    "WHAT_IF_VECTOR_UNAVAILABLE": "时序证据检索当前不可用。",
    "WHAT_IF_WATERMARK_MISSING": "时序回放缺少完整水位，无法执行推演。",
}

_WHAT_IF_RUN_LEGACY_ERRORS = {
    "用户取消了推演任务": "WHAT_IF_CANCELLED",
    "运行前复核失败：提交者已不存在或本体访问权限已撤销": "WHAT_IF_ACTOR_UNAVAILABLE",
    "情景不存在": "WHAT_IF_SCENARIO_UNAVAILABLE",
    "运行前复核失败：提交者不是情景所有者": "WHAT_IF_OWNER_MISMATCH",
    "情景内容在排队后发生变化，请重新提交运行": "WHAT_IF_INPUT_CHANGED",
}


def what_if_run_error_code(error: str | None) -> str | None:
    """Return an allowlisted category for persisted What-if run errors."""
    if error is None or not str(error).strip():
        return None
    value = str(error).strip()
    if value in _WHAT_IF_RUN_ERROR_MESSAGES:
        return value
    if value in _WHAT_IF_RUN_LEGACY_ERRORS:
        return _WHAT_IF_RUN_LEGACY_ERRORS[value]
    return "WHAT_IF_RUN_FAILED"


class WhatIfError(ValueError):
    def __init__(self, message: str, *, status_code: int = 422, code: str = "WHAT_IF_INVALID", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}


class WhatIfCancelled(WhatIfError):
    """Cooperative cancellation raised between bounded reasoning stages."""

    pass


def context_content_digest(context: dict[str, Any]) -> str:
    """Hash a frozen What-If input, excluding its self-referential digest."""
    payload = copy.deepcopy(context)
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("context_digest", None)
        if not provenance:
            payload.pop("provenance", None)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal_context(context: dict[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(context)
    provenance = dict(sealed.get("provenance") or {})
    provenance["context_digest"] = ""
    sealed["provenance"] = provenance
    provenance["context_digest"] = context_content_digest(sealed)
    return sealed


def verify_context_digest(context: dict[str, Any]) -> bool:
    """Verify a sealed context; return False for pre-lineage legacy rows."""
    provenance = context.get("provenance")
    expected = provenance.get("context_digest") if isinstance(provenance, dict) else None
    if not expected:
        return False
    return hmac.compare_digest(str(expected), context_content_digest(context))


def _sha256_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def bind_scenario_branch(baseline: dict[str, Any], branch_id: str) -> dict[str, Any]:
    """Bind a pre-create baseline to its durable What-if scenario branch."""
    sealed = copy.deepcopy(baseline)
    context_value = sealed.get("scenario_context")
    if not verify_scenario_context(context_value):
        raise WhatIfError("推演基线缺少有效的 ScenarioContext")
    context = ScenarioContext.model_validate(context_value)
    values = context.model_dump(mode="python", exclude={"context_digest"})
    values["scenario_branch_id"] = str(branch_id)
    sealed["scenario_context"] = seal_scenario_context(values).model_dump(mode="json")
    return seal_context(sealed)


def _fixed_temporal_versions(
    db,
    replay: TemporalReplay,
    snapshot: DataModelSnapshot | None,
    point: Decimal | float | None,
) -> tuple[DatasetVersion, DataModelSnapshot]:
    """Require one published snapshot and immutable source versions for a run."""
    if not replay.dataset_version_id:
        raise WhatIfError(
            "时序回放未固定 DatasetVersion，无法建立可重放的推演基线",
            status_code=409,
            code="WHAT_IF_DATASET_VERSION_MISSING",
        )
    version = db.query(DatasetVersion).filter(
        DatasetVersion.id == replay.dataset_version_id,
        DatasetVersion.dataset_id == replay.dataset_id,
    ).first()
    if version is None or not _sha256_digest(version.checksum):
        raise WhatIfError(
            "时序回放的 DatasetVersion 不存在或缺少 SHA-256 摘要",
            status_code=409,
            code="WHAT_IF_DATASET_VERSION_INVALID",
        )
    if snapshot is None:
        raise WhatIfError(
            "时序回放尚无已发布 DataModelSnapshot，请先发布固定快照",
            status_code=409,
            code="WHAT_IF_SNAPSHOT_MISSING",
        )
    raw_snapshot_hash_version = getattr(snapshot, "snapshot_hash_version", None)
    snapshot_hash_version = int(raw_snapshot_hash_version) if raw_snapshot_hash_version is not None else 1
    if (
        replay.status != "published"
        or snapshot.status != "published"
        or snapshot.ontology_id != replay.ontology_id
        or snapshot.replay_id != replay.id
        or snapshot.schema_revision_id != replay.schema_revision_id
        or snapshot.dataset_version_id != replay.dataset_version_id
        or not _sha256_digest(snapshot.snapshot_hash)
        or snapshot_hash_version not in {1, 2}
        or (replay.graph_namespace and snapshot.graph_namespace != replay.graph_namespace)
    ):
        raise WhatIfError(
            "TemporalReplay 或 DataModelSnapshot 未发布，或其固定版本不一致",
            status_code=409,
            code="WHAT_IF_SNAPSHOT_INVALID",
        )
    if replay.watermark_ordinal is None or snapshot.through_ordinal is None or replay.watermark_sequence is None:
        raise WhatIfError(
            "时序回放或发布快照缺少完整 watermark，无法固定时间上下文",
            status_code=409,
            code="WHAT_IF_WATERMARK_MISSING",
        )
    watermark = Decimal(str(replay.watermark_ordinal))
    snapshot_watermark = Decimal(str(snapshot.through_ordinal))
    committed_event_count = int(db.query(func.count(TemporalStreamEvent.id)).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
    ).scalar() or 0)
    fact_count = int(db.query(func.count(TemporalFact.id)).filter(
        TemporalFact.replay_id == replay.id,
    ).scalar() or 0)
    if snapshot_hash_version == 2:
        from app.models.v2.construction import EvidenceRef

        construction_run_id = (replay.config or {}).get("construction_run_id")
        evidence_count = int(db.query(func.count(EvidenceRef.id)).filter(
            EvidenceRef.construction_run_id == construction_run_id,
        ).scalar() or 0)
    else:
        evidence_count = None
    latest_event = db.query(TemporalStreamEvent).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
    ).order_by(
        TemporalStreamEvent.ordinal.desc(),
        TemporalStreamEvent.source_sequence.desc(),
        TemporalStreamEvent.id.desc(),
    ).first()
    if (
        snapshot_watermark != watermark
        or (snapshot_hash_version == 2 and (
            snapshot.watermark_sequence is None
            or int(snapshot.watermark_sequence) != int(replay.watermark_sequence)
            or int(snapshot.evidence_count or 0) != int(evidence_count or 0)
        ))
        or int(snapshot.event_count or 0) != committed_event_count
        or committed_event_count != int(replay.committed_events or 0)
        or int(snapshot.fact_count or 0) != fact_count
        or latest_event is None
        or int(latest_event.source_sequence) != int(replay.watermark_sequence)
        or Decimal(str(latest_event.ordinal)) != watermark
    ):
        raise WhatIfError(
            "已发布 DataModelSnapshot 落后于回放水位，请重新发布快照",
            status_code=409,
            code="WHAT_IF_SNAPSHOT_STALE",
        )
    if snapshot_hash_version == 2:
        from app.services.v2.temporal_stream_service import (
            temporal_snapshot_content_digest,
            temporal_snapshot_manifest,
        )

        events = db.query(TemporalStreamEvent).filter(
            TemporalStreamEvent.replay_id == replay.id,
            TemporalStreamEvent.status == "committed",
        ).order_by(
            TemporalStreamEvent.ordinal.asc(),
            TemporalStreamEvent.source_sequence.asc(),
            TemporalStreamEvent.id.asc(),
        ).all()
        facts = db.query(TemporalFact).filter(
            TemporalFact.replay_id == replay.id,
        ).order_by(
            TemporalFact.valid_from_ordinal.asc(),
            TemporalFact.subject_id.asc(),
            TemporalFact.predicate.asc(),
            TemporalFact.object_id.asc(),
            TemporalFact.state_key.asc(),
            TemporalFact.id.asc(),
        ).all()
        manifest = temporal_snapshot_manifest(
            replay,
            watermark_ordinal=snapshot.through_ordinal,
            watermark_sequence=snapshot.watermark_sequence,
            event_count=snapshot.event_count,
            node_count=snapshot.node_count,
            edge_count=snapshot.edge_count,
            fact_count=snapshot.fact_count,
            evidence_count=snapshot.evidence_count,
        )
        current_digest = temporal_snapshot_content_digest(
            replay,
            events,
            facts,
            float(snapshot.through_ordinal),
            manifest=manifest,
        )
        if not hmac.compare_digest(current_digest, str(snapshot.snapshot_hash)):
            raise WhatIfError(
                "已发布 DataModelSnapshot 的完整性摘要不匹配",
                status_code=409,
                code="WHAT_IF_SNAPSHOT_HASH_MISMATCH",
            )
    if point is None or Decimal(str(point)) > snapshot_watermark:
        raise WhatIfError(
            "所选时间位置超出已发布 Temporal snapshot 水位",
            status_code=409,
            code="WHAT_IF_AS_OF_UNPUBLISHED",
        )
    return version, snapshot


def _vector_query_from_context(context: dict[str, Any]) -> str | None:
    vector_evidence = context.get("vector_evidence")
    if vector_evidence is None:
        return None
    if not isinstance(vector_evidence, dict):
        raise WhatIfError("情景基线的向量证据快照格式无效")
    query = vector_evidence.get("query")
    if not isinstance(query, str) or not query.strip():
        raise WhatIfError("情景基线缺少有效的向量证据检索词")
    return query


def _resolve_temporal_source(
    db,
    ontology_id: str,
    target_instance_id: str,
    episode_id: str | None,
    replay_id: str | None,
    at: str | float | Decimal | None = None,
    mode: str = "cumulative",
) -> tuple[TemporalReplay | None, TemporalStreamEvent | None, str | None, TemporalSourceAdapter | None]:
    is_factorynet_target = target_instance_id.startswith(FACTORYNET_OBSERVATION_PREFIX)
    parsed_episode = None
    if is_factorynet_target:
        tail = target_instance_id[len(FACTORYNET_OBSERVATION_PREFIX):]
        if ":" in tail:
            parsed_episode = tail.rsplit(":", 1)[0]
    if parsed_episode and episode_id and parsed_episode != episode_id:
        raise WhatIfError("选定 Observation 与 episode_id 不一致")
    resolved_episode = episode_id or parsed_episode
    try:
        requested_point = Decimal(str(at)) if at not in (None, "") else None
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WhatIfError("时间位置必须是有限数值") from exc
    if requested_point is not None and not requested_point.is_finite():
        raise WhatIfError("时间位置必须是有限数值")
    if mode not in {"cumulative", "window"}:
        raise WhatIfError("推演时间模式无效")

    replay_query = db.query(TemporalReplay).filter(TemporalReplay.ontology_id == ontology_id)
    if replay_id:
        replay_query = replay_query.filter(TemporalReplay.id == replay_id)
    elif is_factorynet_target:
        replay_query = replay_query.filter(TemporalReplay.source_id == "factorynet_cnc")
    replays = replay_query.order_by(TemporalReplay.created_at.desc()).limit(100).all()
    if replay_id and not replays:
        raise WhatIfError("指定时序回放不存在或不属于当前本体")
    if replay_id and adapter_for_replay(replays[0]) is None:
        raise WhatIfError(
            "指定回放没有可用的版本化 TemporalSourceAdapter 或固定映射",
            status_code=409,
            code="WHAT_IF_SOURCE_ADAPTER_UNAVAILABLE",
        )

    matches: list[tuple[TemporalReplay, TemporalStreamEvent, TemporalSourceAdapter]] = []
    for replay in replays:
        adapter = adapter_for_replay(replay)
        if adapter is None or adapter.is_factorynet != is_factorynet_target:
            continue
        target_episode, source_row_id = adapter.parse_target(target_instance_id)
        if target_episode and resolved_episode and target_episode != resolved_episode:
            raise WhatIfError("选定 Observation 与 episode_id 不一致")
        event = adapter.resolve_event(
            db,
            replay,
            target_instance_id,
            resolved_episode or target_episode,
            source_row_id,
            requested_point,
        )
        if event:
            matches.append((replay, event, adapter))
    if not matches:
        if replay_id:
            if is_factorynet_target:
                raise WhatIfError("指定回放中没有找到已提交的目标 Observation")
            raise WhatIfError("指定回放在该时间位置没有找到已提交的目标对象")
        if not is_factorynet_target:
            raise WhatIfError("请选择一个有已提交事件的时序对象，并指定对应回放")
        return None, None, resolved_episode, None
    if len(matches) > 1 and not replay_id:
        raise WhatIfError("该对象存在多个时序回放，请显式选择 replay_id")
    replay, event, adapter = matches[0]
    return replay, event, resolved_episode or event.episode_id, adapter


def _token(value: Any) -> str:
    text = SAFE_TOKEN.sub("_", str(value or "")).strip("_")
    return text or "unknown"


def _term(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return _token(value)


def _fact(predicate: Any, args: list[Any] | tuple[Any, ...]) -> tuple[str, tuple[str, ...]]:
    return _token(predicate).upper(), tuple(str(item) for item in args)


def _fact_text(fact: tuple[str, tuple[str, ...]]) -> str:
    return f"{fact[0]}({', '.join(fact[1])})"


def _is_var(value: str) -> bool:
    return str(value).startswith("?")


def _condition_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("all", "conditions", "items", "and"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
        return [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _as_atom(item: dict[str, Any]) -> tuple[str, list[str]] | None:
    kind = str(item.get("kind") or item.get("type") or "relationship").lower()
    if kind in {"relationship", "relation", "edge"} or item.get("predicate") or item.get("relation"):
        predicate = item.get("predicate") or item.get("relation") or item.get("type")
        source = item.get("source") or item.get("source_var") or item.get("from") or "?source"
        target = item.get("target") or item.get("target_var") or item.get("to") or "?target"
        args = item.get("args") if isinstance(item.get("args"), list) else [source, target]
        return _token(predicate).upper(), [str(arg) if _is_var(str(arg)) else _term(arg) for arg in args]
    return None


def _compile_rule(rule: dict[str, Any], rule_id: str) -> dict[str, Any] | None:
    conditions = _condition_items(rule.get("condition") or rule.get("conditions") or rule.get("condition_json"))
    atoms: list[tuple[str, list[str]]] = []
    property_specs: list[dict[str, Any]] = []
    for item in conditions:
        # Property comparisons are compiled into deterministic unary atoms.
        if str(item.get("kind") or item.get("type") or "").lower() in {"property", "attribute"} or item.get("property_id") or item.get("property"):
            prop = item.get("property_id") or item.get("property")
            operator = str(item.get("operator") or "=")
            value = item.get("value") if "value" in item else item.get("comparison_value")
            predicate = f"PROP_{_token(prop)}_{_token(operator)}_{_term(value)}"
            entity_var = item.get("entity_var") or item.get("instance") or item.get("source") or "?source"
            atoms.append((predicate.upper(), [str(entity_var) if _is_var(str(entity_var)) else _term(entity_var)]))
            property_specs.append({"predicate": predicate.upper(), "property": str(prop), "operator": operator, "value": value, "entity_var": str(entity_var)})
            continue
        atom = _as_atom(item)
        if atom:
            atoms.append(atom)
    effect = rule.get("effect") or rule.get("result") or {}
    if isinstance(effect, list):
        effect = effect[0] if effect else {}
    conclusion = _as_atom(effect if isinstance(effect, dict) else {})
    if not conclusion or not atoms:
        return None
    if len(atoms) > MAX_PREMISES:
        raise WhatIfError(f"规则 {rule_id} 前提超过 {MAX_PREMISES} 个")
    bound = {term for _, args in atoms for term in args if _is_var(term)}
    if any(_is_var(term) and term not in bound for term in conclusion[1]):
        raise WhatIfError(f"规则 {rule_id} 的结论包含未绑定变量")
    return {"id": rule_id, "conditions": atoms, "conclusion": conclusion, "property_specs": property_specs}


def _match_condition(condition: tuple[str, list[str]], fact: tuple[str, tuple[str, ...]], bindings: dict[str, str]) -> dict[str, str] | None:
    predicate, terms = condition
    if fact[0] != predicate or len(terms) != len(fact[1]):
        return None
    next_bindings = dict(bindings)
    for expected, actual in zip(terms, fact[1]):
        if _is_var(expected):
            if expected in next_bindings and next_bindings[expected] != actual:
                return None
            next_bindings[expected] = actual
        elif expected != actual:
            return None
    return next_bindings


def _parse_fact_text(value: Any) -> tuple[str, tuple[str, ...]] | None:
    """Parse the deliberately small atom format returned by Semantica."""
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\((.*)\)\s*", str(value or ""))
    if not match:
        return None
    inside = match.group(2).strip()
    args = tuple(part.strip() for part in inside.split(",")) if inside else ()
    return match.group(1).upper(), args


def _semantica_infer(
    facts: list[tuple[str, tuple[str, ...]]], rules: list[dict[str, Any]]
) -> tuple[set[tuple[str, tuple[str, ...]]], dict[tuple[str, tuple[str, ...]], dict[str, Any]]] | None:
    """Run the pinned Semantica engine and keep one grounded proof per result.

    The What-If runner still has a deterministic compatibility implementation
    for installations where the optional package is unavailable.  When the
    package is present, however, the actual result set—not just a health
    probe—is used for the scenario diff.  Rule objects retain our stable rule
    IDs so the UI can link a proof back to the saved scenario rule.
    """
    try:
        from semantica.reasoning import Reasoner
        from semantica.reasoning.reasoner import Rule

        semantica_rules = []
        for item in rules:
            conditions = [
                f"{predicate}({', '.join(terms)})"
                for predicate, terms in item["conditions"]
            ]
            predicate, terms = item["conclusion"]
            semantica_rules.append(
                Rule(
                    rule_id=str(item["id"]),
                    name=str(item["id"]),
                    conditions=conditions,
                    conclusion=f"{predicate}({', '.join(terms)})",
                )
            )
        results = Reasoner(max_iterations=MAX_ITERATIONS).infer_with_results(
            [_fact_text(item) for item in facts], semantica_rules
        )
        known = set(facts)
        proofs: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        for result in results:
            conclusion = _parse_fact_text(getattr(result, "conclusion", ""))
            if conclusion is None or conclusion in known:
                continue
            rule_used = getattr(result, "rule_used", None)
            premises = [str(item) for item in (getattr(result, "premises", None) or [])]
            proofs[conclusion] = {
                "fact": _fact_text(conclusion),
                "rule_id": str(getattr(rule_used, "rule_id", "") or "unknown"),
                "premises": premises,
                "bindings": {},
                "proof_scope": "one_grounded_derivation",
            }
            known.add(conclusion)
        return known, proofs
    except Exception:
        return None


def infer_facts(facts: list[tuple[str, tuple[str, ...]]], rules: list[dict[str, Any]]) -> dict[str, Any]:
    if len(facts) > MAX_FACTS:
        raise WhatIfError(f"事实超过 {MAX_FACTS} 条限制")
    if len(rules) > MAX_RULES:
        raise WhatIfError(f"规则超过 {MAX_RULES} 条限制")
    semantica_result = _semantica_infer(facts, rules) if rules else None
    if semantica_result is not None:
        known, proofs = semantica_result
        return {
            "facts": [_fact_text(item) for item in sorted(known)],
            "derived": [proofs[item] for item in sorted(proofs)],
            "iterations": 1,
            "engine": "semantica",
        }
    known = set(facts)
    proofs: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for iteration in range(MAX_ITERATIONS):
        added = 0
        for rule in rules:
            matches = [({}, [])]
            for condition in rule["conditions"]:
                next_matches = []
                for bindings, premises in matches:
                    for fact in list(known):
                        result = _match_condition(condition, fact, bindings)
                        if result is not None:
                            next_matches.append((result, premises + [fact]))
                matches = next_matches
                if not matches:
                    break
            for bindings, premises in matches:
                predicate, terms = rule["conclusion"]
                grounded = tuple(bindings.get(term, term) if _is_var(term) else term for term in terms)
                conclusion = (predicate, grounded)
                if conclusion not in known:
                    known.add(conclusion); added += 1
                    proofs[conclusion] = {"fact": _fact_text(conclusion), "rule_id": rule["id"], "premises": [_fact_text(item) for item in premises], "bindings": bindings, "proof_scope": "one_grounded_derivation"}
                    if len(known) > MAX_FACTS:
                        raise WhatIfError(f"推演事实超过 {MAX_FACTS} 条限制")
        if not added:
            return {"facts": [_fact_text(item) for item in sorted(known)], "derived": [proofs[item] for item in sorted(proofs)], "iterations": iteration + 1, "engine": "deterministic-compatible"}
    raise WhatIfError(f"推演超过 {MAX_ITERATIONS} 次迭代限制，未收敛")


def _context_facts(
    context: dict[str, Any],
    *,
    rules: list[dict[str, Any]] | None = None,
    assumptions: list[dict[str, Any]] | None = None,
) -> tuple[list[tuple[str, tuple[str, ...]]], dict[str, list[dict[str, Any]]]]:
    """Compile only facts that the selected scenario can actually use.

    A FactoryNet observation carries dozens of sensor columns.  Copying every
    scalar column into a symbolic fact multiplies a two-hop neighbourhood into
    tens of thousands of facts even though the rules normally use only one or
    two fields.  The context JSON remains intact for the inspector, while the
    reasoning input is reduced deterministically to:

    * all bounded relationship edges;
    * type atoms only when a rule explicitly asks for a ``TYPE_*`` predicate;
    * properties referenced by a rule comparison or an explicit scenario
      property override.

    This is a semantic projection, not an arbitrary truncation.  The hard
    ``MAX_FACTS`` guard in ``infer_facts`` still rejects a genuinely oversized
    structural context.
    """
    facts: list[tuple[str, tuple[str, ...]]] = []
    evidence: dict[str, list[dict[str, Any]]] = {}
    executable_rules = rules or []
    required_predicates = {
        str(predicate).upper()
        for rule in executable_rules
        for predicate, _terms in rule.get("conditions", [])
    }
    required_type_predicates = {
        predicate for predicate in required_predicates if predicate.startswith("TYPE_")
    }
    required_properties = {
        str(spec.get("property"))
        for rule in executable_rules
        for spec in rule.get("property_specs", [])
        if spec.get("property")
    }
    for item in assumptions or []:
        kind = str(item.get("kind") or item.get("type") or "").lower()
        if kind in {"set_property", "override_property", "property"}:
            property_name = item.get("property") or item.get("property_id")
            if property_name:
                required_properties.add(str(property_name))
    nodes = context.get("nodes") or []
    edges = context.get("edges") or []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        type_fact = _fact(f"TYPE_{node.get('entity_type') or 'Entity'}", [node_id])
        if not required_type_predicates or type_fact[0] in required_type_predicates:
            # Do not add all type atoms by default: no current FactoryNet rule
            # consumes them, and the static episode hub would otherwise spend
            # most of the fact budget before relationship inference starts.
            if required_type_predicates:
                facts.append(type_fact)
                evidence.setdefault(_fact_text(type_fact), []).extend(node.get("evidence") or [])
        for key, value in (node.get("properties") or {}).items():
            if key not in required_properties or isinstance(value, (dict, list)) or value in (None, ""):
                continue
            prop_name = f"PROP_{_token(key)}"
            prop_fact = _fact(prop_name, [node_id, _term(value)])
            facts.append(prop_fact)
            # Equality predicates are useful for the built-in property rule
            # compiler and remain deterministic; non-equality is evaluated by
            # the scenario compiler before facts are sent to the engine.
            evidence.setdefault(_fact_text(prop_fact), []).extend(node.get("evidence") or [])
    for edge in edges:
        source, target = str(edge.get("source") or ""), str(edge.get("target") or "")
        if not source or not target:
            continue
        fact = _fact(edge.get("type") or edge.get("label") or "RELATED", [source, target])
        facts.append(fact); evidence.setdefault(_fact_text(fact), []).extend(edge.get("evidence") or [])
    return list(dict.fromkeys(facts)), evidence


def _compare(value: Any, operator: str, expected: Any) -> bool:
    try:
        if operator in {"=", "=="}: return value == expected or str(value) == str(expected)
        if operator in {"≠", "!="}: return value != expected and str(value) != str(expected)
        if operator in {">", "≥", ">=", "<", "≤", "<="}:
            left, right = float(value), float(expected)
            return {">": left > right, "≥": left >= right, ">=": left >= right, "<": left < right, "≤": left <= right, "<=": left <= right}[operator]
        if operator in {"属于", "in"}:
            choices = expected if isinstance(expected, (list, tuple, set)) else [expected]
            return value in choices or str(value) in {str(item) for item in choices}
    except (TypeError, ValueError):
        return False
    return False


def _augment_property_facts(facts: list[tuple[str, tuple[str, ...]]], context: dict[str, Any], rules: list[dict[str, Any]]) -> list[tuple[str, tuple[str, ...]]]:
    augmented = list(facts)
    for rule in rules:
        for spec in rule.get("property_specs", []):
            for node in context.get("nodes") or []:
                properties = node.get("properties") or {}
                if spec["property"] in properties and _compare(properties.get(spec["property"]), spec["operator"], spec.get("value")):
                    augmented.append((spec["predicate"], (str(node.get("id")),)))
    return list(dict.fromkeys(augmented))


def _apply_assumptions(context: dict[str, Any], assumptions: list[dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(context)
    nodes = {str(node.get("id")): node for node in result.get("nodes") or [] if node.get("id")}
    edges = list(result.get("edges") or [])
    for item in assumptions:
        kind = str(item.get("kind") or item.get("type") or "").lower()
        if kind in {"set_property", "override_property", "property"}:
            node = nodes.get(str(item.get("instance_id") or item.get("node_id") or ""))
            if not node:
                raise WhatIfError(f"情景属性覆盖目标不存在：{item.get('instance_id') or item.get('node_id')}")
            property_name = str(item.get("property") or item.get("property_id"))
            value = item.get("value")
            node.setdefault("properties", {})[property_name] = value
            # FactoryNet stores tool condition as a synchronized relationship
            # to a ToolCondition instance.  Keep the relationship projection
            # consistent with the overridden scalar so the demo rule produces
            # a meaningful added/removed conclusion.
            if property_name in {"ctx_tool_condition", "tool_condition"}:
                source_id = str(node.get("id"))
                edges = [edge for edge in edges if not (str(edge.get("source")) == source_id and str(edge.get("type")) == "HAS_TOOL_CONDITION")]
                target_id = next((candidate_id for candidate_id, candidate in nodes.items() if str(candidate.get("entity_type")) == "ToolCondition" and str((candidate.get("properties") or {}).get("value")) == str(value)), None)
                if not target_id:
                    target_id = f"scenario:ToolCondition:{_token(value)}"
                    nodes[target_id] = {"id": target_id, "entity_type": "ToolCondition", "properties": {"value": value}, "evidence": [{"source": "情景假设"}]}
                edges.append({"id": f"scenario:{source_id}:HAS_TOOL_CONDITION:{target_id}", "source": source_id, "target": target_id, "type": "HAS_TOOL_CONDITION", "properties": {"source": "情景假设"}, "evidence": [{"source": "情景假设"}]})
        elif kind in {"add_relation", "add_relationship"}:
            source, target = str(item.get("source") or ""), str(item.get("target") or "")
            if source not in nodes or target not in nodes:
                raise WhatIfError("情景新增关系的端点不在两跳范围内")
            edges.append({"id": f"scenario:{uuid.uuid4().hex}", "source": source, "target": target, "type": str(item.get("type") or item.get("predicate") or "RELATED"), "properties": {"source": "情景假设"}, "evidence": [{"source": "情景假设"}]})
        elif kind in {"remove_relation", "remove_relationship"}:
            source, target, predicate = str(item.get("source") or ""), str(item.get("target") or ""), str(item.get("type") or item.get("predicate") or "")
            edges = [edge for edge in edges if not (str(edge.get("source")) == source and str(edge.get("target")) == target and (not predicate or str(edge.get("type")) == predicate))]
        else:
            raise WhatIfError(f"不支持的情景假设：{kind or '空'}")
    result["nodes"] = list(nodes.values()); result["edges"] = edges
    return result


def _rules_for_context(
    db,
    ontology_id: str,
    rule_overrides: dict[str, Any] | None = None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    overrides = rule_overrides or {}
    disabled = {str(item) for item in overrides.get("disabled", [])}
    enabled = {str(item) for item in overrides.get("enabled", [])}
    if disabled & enabled:
        raise WhatIfError("同一条规则不能同时启用和停用")
    if snapshot is not None:
        rows = [
            {
                "id": item.get("id"),
                "enabled": bool(item.get("enabled", True)),
                "condition": item.get("condition") or item.get("conditions") or {},
                "effect": item.get("effect") or item.get("result") or {},
            }
            for item in snapshot.get("logic_rules", [])
            if isinstance(item, dict)
        ]
    else:
        rows = db.query(LogicRule).filter(LogicRule.ontology_id == ontology_id).all()
    compiled: list[dict[str, Any]] = []
    for row in rows:
        row_id = str(row.get("id") if isinstance(row, dict) else row.id)
        row_enabled = bool(row.get("enabled", True) if isinstance(row, dict) else row.enabled)
        if not row_enabled and row_id not in enabled:
            continue
        if row_id in disabled:
            continue
        try:
            condition = row.get("condition", {}) if isinstance(row, dict) else row.condition_json or {}
            effect = row.get("effect", {}) if isinstance(row, dict) else row.effect_json or {}
            rule = _compile_rule({"condition": condition, "effect": effect}, row_id)
        except WhatIfError:
            rule = None
        if rule:
            compiled.append(rule)
    temporary = overrides.get("temporary", []) or []
    if len(temporary) > MAX_RULES:
        raise WhatIfError(f"情景规则超过 {MAX_RULES} 条限制")
    for index, item in enumerate(temporary):
        rule = _compile_rule(item, str(item.get("id") or f"scenario-rule-{index + 1}"))
        if rule:
            compiled.append(rule)
    if len(compiled) > MAX_RULES:
        raise WhatIfError(f"可执行规则超过 {MAX_RULES} 条限制")
    return compiled


def _property_definitions(entity: Entity) -> list[dict[str, Any]]:
    raw = entity.properties or {}
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        values = raw.get("property_definitions") or raw.get("properties")
        if isinstance(values, list):
            return [item for item in values if isinstance(item, dict)]
        return [
            {"id": str(key), "name": str(key), "type": "string"}
            for key in raw
            if key not in {"schema_version", "source_fields", "evidence", "color", "icon", "data_class"}
        ]
    return []


def _validate_assumptions(db, scenario: WhatIfScenario, context: dict[str, Any]) -> None:
    """Validate scenario edits before any reasoning engine is invoked."""
    nodes = {
        str(node.get("id")): node
        for node in context.get("nodes") or []
        if node.get("id")
    }
    entities = db.query(Entity).filter(
        Entity.ontology_id == scenario.ontology_id,
        Entity.is_source_deleted.is_(False),
    ).all()
    entity_by_name: dict[str, Entity] = {}
    for entity in entities:
        for key in (entity.id, entity.canonical_id, entity.name_en, entity.name_cn, entity.type):
            if key:
                entity_by_name[str(key)] = entity
    for item in list(scenario.assumptions_json or []):
        kind = str(item.get("kind") or item.get("type") or "").lower()
        if kind in {"set_property", "override_property", "property"}:
            node_id = str(item.get("instance_id") or item.get("node_id") or "")
            node = nodes.get(node_id)
            if node is None:
                raise WhatIfError(f"情景属性覆盖目标不存在：{node_id or '空'}")
            property_name = str(item.get("property") or item.get("property_id") or "")
            if not property_name:
                raise WhatIfError("情景属性覆盖缺少属性标识")
            provenance = context.get("provenance") if isinstance(context.get("provenance"), dict) else {}
            permitted_by_object = provenance.get("authorized_properties_by_object")
            if not isinstance(permitted_by_object, dict) or property_name not in set(permitted_by_object.get(node_id) or []):
                raise WhatIfError(f"当前用户无权在情景中修改属性：{property_name}")
            if property_name in {"ctx_tool_condition", "tool_condition"} and "HAS_TOOL_CONDITION" not in set(provenance.get("authorized_link_types") or []):
                raise WhatIfError("当前用户无权在情景中修改 HAS_TOOL_CONDITION 关系")
            entity = entity_by_name.get(str(node.get("entity_type") or ""))
            definition = None
            if entity:
                definition = next(
                    (
                        prop for prop in _property_definitions(entity)
                        if str(prop.get("id") or prop.get("name")) == property_name
                    ),
                    None,
                )
            expected_type = str((definition or {}).get("type") or "").lower()
            value = item.get("value")
            if expected_type in {"integer", "int"} and (isinstance(value, bool) or not isinstance(value, int)):
                raise WhatIfError(f"属性 {property_name} 需要整数值")
            if expected_type in {"decimal", "float", "number"} and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise WhatIfError(f"属性 {property_name} 需要数值")
            if expected_type in {"boolean", "bool"} and not isinstance(value, bool):
                raise WhatIfError(f"属性 {property_name} 需要布尔值")
            enum_values = (definition or {}).get("enum") if definition else None
            if isinstance(enum_values, list) and value not in enum_values and str(value) not in {str(item) for item in enum_values}:
                raise WhatIfError(f"属性 {property_name} 的值不在枚举范围内")
        elif kind in {"add_relation", "add_relationship", "remove_relation", "remove_relationship"}:
            source = str(item.get("source") or "")
            target = str(item.get("target") or "")
            if source not in nodes or target not in nodes:
                raise WhatIfError("情景关系端点不在两跳范围内")
            relation_type = str(item.get("type") or item.get("predicate") or "RELATED")
            if kind in {"add_relation", "add_relationship"}:
                provenance = context.get("provenance") if isinstance(context.get("provenance"), dict) else {}
                if relation_type not in set(provenance.get("authorized_link_types") or []):
                    raise WhatIfError(f"当前用户无权在情景中创建关系：{relation_type}")
            elif not any(
                str(edge.get("source") or "") == source
                and str(edge.get("target") or "") == target
                and str(edge.get("type") or edge.get("label") or "RELATED") == relation_type
                for edge in context.get("edges") or []
            ):
                raise WhatIfError("当前用户无权移除该关系或该关系已不在基线中")
        else:
            raise WhatIfError(f"不支持的情景假设：{kind or '空'}")


def run_preview(
    db,
    scenario: WhatIfScenario,
    run: WhatIfRun,
    *,
    on_stage=None,
) -> dict[str, Any]:
    context = copy.deepcopy(scenario.baseline_json or {})
    provenance = context.get("provenance") if isinstance(context.get("provenance"), dict) else {}
    expected_context_digest = provenance.get("context_digest")
    if not expected_context_digest or not verify_context_digest(context):
        raise WhatIfError("推演基线摘要校验失败或缺失，请重新读取并创建情景")
    if not verify_scenario_context(context.get("scenario_context")):
        raise WhatIfError("推演基线缺少有效的 ScenarioContext，请重新读取并创建情景")
    scenario_context = ScenarioContext.model_validate(context["scenario_context"])
    if (
        scenario_context.scenario_branch_id != scenario.id
        or scenario_context.ontology_id != scenario.ontology_id
        or scenario_context.schema_revision_id != scenario.base_revision_id
        or scenario_context.dataset_version_id != scenario.dataset_version_id
        or scenario_context.replay_id != provenance.get("replay_id")
        or scenario_context.permission_digest != provenance.get("permission_digest")
    ):
        raise WhatIfError("ScenarioContext 与情景固定版本、分支或权限摘要不一致")
    if context.get("base_revision_id") != scenario.base_revision_id:
        raise WhatIfError("推演基线本体修订与情景固定修订不一致")
    if context.get("dataset_version_id") != scenario.dataset_version_id:
        raise WhatIfError("推演基线数据版本与情景固定数据版本不一致")
    if len(context.get("nodes") or []) > MAX_NODES:
        raise WhatIfError(f"推演节点超过 {MAX_NODES} 个限制")
    revision_snapshot = None
    if scenario.base_revision_id:
        revision = db.query(OntologyRevision).filter(
            OntologyRevision.id == scenario.base_revision_id,
            OntologyRevision.ontology_id == scenario.ontology_id,
        ).first()
        if not revision:
            raise WhatIfError("情景固定的本体修订不存在")
        revision_snapshot = revision.snapshot_json or {}
    rules = _rules_for_context(
        db,
        scenario.ontology_id,
        scenario.rule_overrides_json or {},
        snapshot=revision_snapshot,
    )
    assumptions = list(scenario.assumptions_json or [])
    _validate_assumptions(db, scenario, context)
    facts, evidence = _context_facts(context, rules=rules, assumptions=assumptions)
    facts = _augment_property_facts(facts, context, rules)
    if len(facts) > MAX_FACTS:
        raise WhatIfError(
            f"当前选定范围包含 {len(facts)} 条可推理事实，超过 {MAX_FACTS} 条限制；"
            "请切换为“当前位置窗口”或降低 Ordinal 后重试"
        )
    baseline = infer_facts(facts, rules)
    if on_stage:
        on_stage("apply_assumption", 50)
    changed = _apply_assumptions(context, assumptions)
    if on_stage:
        on_stage("scenario_reasoning", 65)
    scenario_facts, scenario_evidence = _context_facts(changed, rules=rules, assumptions=assumptions)
    scenario_facts = _augment_property_facts(scenario_facts, changed, rules)
    if len(scenario_facts) > MAX_FACTS:
        raise WhatIfError(
            f"应用情景后包含 {len(scenario_facts)} 条可推理事实，超过 {MAX_FACTS} 条限制；"
            "请缩小基线范围或切换为“当前位置窗口”"
        )
    scenario_result = infer_facts(scenario_facts, rules)
    base_set, scenario_set = set(baseline["facts"]), set(scenario_result["facts"])
    diff = {"added": sorted(scenario_set - base_set), "removed": sorted(base_set - scenario_set), "unchanged": sorted(base_set & scenario_set), "added_count": len(scenario_set - base_set), "removed_count": len(base_set - scenario_set), "proofs": scenario_result.get("derived", [])}
    for item in diff["proofs"]:
        item["evidence"] = [e for premise in item.get("premises", []) for e in scenario_evidence.get(premise, [])] or [{"source": "情景假设"}]
    if on_stage:
        on_stage("generate_diff", 85)
    engine = "semantica" if baseline.get("engine") == "semantica" and scenario_result.get("engine") == "semantica" else "deterministic-compatible (Semantica package unavailable or rejected input)"
    return {"baseline": baseline, "scenario": scenario_result, "diff": diff, "evidence": {**evidence, **scenario_evidence}, "engine": engine, "engine_version": ENGINE_COMMIT}


def revalidate_scenario_context(db, scenario: WhatIfScenario, user) -> dict[str, Any]:
    """Rebuild the pinned baseline for the current actor before a run or result commit."""
    baseline = scenario.baseline_json if isinstance(scenario.baseline_json, dict) else {}
    provenance = baseline.get("provenance") if isinstance(baseline.get("provenance"), dict) else {}
    expected_digest = provenance.get("context_digest")
    if not expected_digest or not verify_context_digest(baseline):
        raise WhatIfError("情景基线缺少有效完整性摘要，请重新创建情景")
    current = build_context(
        db,
        scenario.ontology_id,
        target_instance_id=baseline.get("target_instance_id"),
        episode_id=baseline.get("episode_id"),
        at=baseline.get("at"),
        mode=str(baseline.get("mode") or "cumulative"),
        replay_id=provenance.get("replay_id"),
        evidence_query=_vector_query_from_context(baseline),
        user=user,
    )
    current = bind_scenario_branch(current, scenario.id)
    current_digest = (current.get("provenance") or {}).get("context_digest")
    if not current_digest or not hmac.compare_digest(str(expected_digest), str(current_digest)):
        raise WhatIfError("数据、规则、本体或权限已变化，请重新读取并创建推演情景")
    return current


def _authorize_context_graph(db, ontology_id: str, revision_id: str | None, context: dict[str, Any], user) -> tuple[dict[str, Any], str, dict[str, list[str]], dict[str, dict[str, str]], list[str]]:
    """Project a What-if graph through the same object, property, and link policy as Object Query."""
    if user is None or not getattr(user, "id", None):
        raise WhatIfError("缺少可验证的用户身份，无法构造推演基线")

    from app.services.v2.authorization_service import (
        field_permitted,
        redact_link_record,
        redact_record,
        resolve_projection,
    )
    from app.services.v2.object_query.authorization import policies_from_projection, require_ontology_access
    from app.services.v2.object_query.errors import ObjectQueryError
    from app.models.relation import Relation

    try:
        access = require_ontology_access(
            db,
            ontology_id,
            str(user.id),
            str(user.role or ""),
            metadata_revision_id=revision_id,
        )
    except ObjectQueryError as exc:
        raise WhatIfError("本体不存在或当前用户无权访问") from exc

    entities = db.query(Entity).filter(
        Entity.ontology_id == ontology_id,
        Entity.is_source_deleted.is_(False),
    ).all()
    entity_by_type: dict[str, Entity] = {}
    semantic_id_by_type: dict[str, str] = {}
    for entity in entities:
        for key in (entity.id, entity.canonical_id, entity.name_en, entity.name_cn, entity.type):
            if not key:
                continue
            entity_by_type.setdefault(str(key), entity)
            if entity.semantic_resource_id:
                semantic_id_by_type.setdefault(str(key), str(entity.semantic_resource_id))

    raw_nodes = list(context.get("nodes") or [])
    node_records: list[dict[str, Any]] = []
    node_types: list[str] = []
    for node in raw_nodes:
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        object_type = str(node.get("entity_id") or node.get("entity_type") or properties.get("entity_id") or "Entity")
        entity = entity_by_type.get(object_type)
        node_types.append(object_type)
        node_records.append({
            "id": node.get("id"),
            "entity_id": entity.id if entity else object_type,
            "entity_type": object_type,
            "semantic_resource_id": node.get("semantic_resource_id") or properties.get("semantic_resource_id") or semantic_id_by_type.get(object_type),
            "object_type_resource_id": node.get("object_type_resource_id") or properties.get("object_type_resource_id"),
            "properties": properties,
        })

    projection = resolve_projection(
        db,
        access.context,
        preloaded_policies=policies_from_projection(access),
        records=node_records,
    )
    if not projection.allowed:
        raise WhatIfError("当前用户没有可用的对象访问策略")

    visible_nodes: list[dict[str, Any]] = []
    visible_ids: set[str] = set()
    allowed_properties: dict[str, list[str]] = {}
    property_types: dict[str, dict[str, str]] = {}
    deleted_node_ids = {
        str(node.get("id"))
        for node in raw_nodes
        if node.get("id") and isinstance(node.get("properties"), dict) and node["properties"].get("_deleted")
    }
    reserved = {
        "object_ref_id", "object_type_resource_id", "semantic_resource_id",
        "episode_id", "source_row_id", "replay_id", "event_id", "event_ordinal",
    }
    for node, object_type, record in zip(raw_nodes, node_types, node_records):
        redacted = redact_record(projection, record)
        if redacted is None:
            continue
        node_id = str(node.get("id") or "")
        next_node = dict(node)
        redacted_properties = redacted.get("properties") if isinstance(redacted.get("properties"), dict) else {}
        safe_properties = {
            key: value for key, value in redacted_properties.items()
            if not str(key).startswith("_") and str(key) not in reserved
        }
        next_node["properties"] = safe_properties
        visible_nodes.append(next_node)
        visible_ids.add(node_id)

        candidates = set(str(key) for key in (record.get("properties") or {}))
        entity = entity_by_type.get(object_type)
        if entity:
            for definition in _property_definitions(entity):
                for key in (definition.get("id"), definition.get("name"), definition.get("source_field"), definition.get("canonical_id")):
                    if key not in (None, ""):
                        candidates.add(str(key))
        allowed = sorted(
            key for key in candidates
            if not key.startswith("_") and key not in reserved and field_permitted(projection, record, key)
        )
        allowed_properties[node_id] = allowed
        definitions = _property_definitions(entity) if entity else []
        types: dict[str, str] = {}
        for definition in definitions:
            declared_type = str(definition.get("type") or "").strip().lower()
            if not declared_type:
                continue
            for key in (definition.get("id"), definition.get("name"), definition.get("source_field"), definition.get("canonical_id")):
                if key is not None and str(key) in allowed:
                    types[str(key)] = declared_type
        safe_properties = next_node.get("properties") if isinstance(next_node.get("properties"), dict) else {}
        for key in allowed:
            if key not in types and key in safe_properties:
                value = safe_properties[key]
                if isinstance(value, bool):
                    types[key] = "boolean"
                elif isinstance(value, int):
                    types[key] = "integer"
                elif isinstance(value, float):
                    types[key] = "number"
                elif isinstance(value, str):
                    types[key] = "string"
        property_types[node_id] = types

    if not visible_nodes or str(context.get("target_instance_id") or "") not in visible_ids:
        raise WhatIfError("选定时序对象不存在或当前用户无权访问")

    visible_edges: list[dict[str, Any]] = []
    for edge in context.get("edges") or []:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in visible_ids or target not in visible_ids:
            continue
        properties = edge.get("properties") if isinstance(edge.get("properties"), dict) else {}
        link_type = str(properties.get("semantic_resource_id") or properties.get("api_name") or edge.get("type") or edge.get("label") or "")
        link_record = {
            "id": edge.get("id"),
            "link_type_id": link_type,
            "type": edge.get("type") or edge.get("label"),
            "source": source,
            "target": target,
            "properties": properties,
        }
        redacted = redact_link_record(projection, link_record, link_type_id=link_type)
        if redacted is None:
            continue
        safe_edge = dict(edge)
        safe_edge["properties"] = redacted.get("properties", properties)
        visible_edges.append(safe_edge)

    # New relation assumptions may only use a link type covered by the active
    # link policy (or an existing authorized edge). Narrow conditional policies
    # remain fail-closed because an invented edge has no trustworthy source data.
    candidate_link_types = {
        str(edge.get("type") or edge.get("label") or "") for edge in visible_edges
        if edge.get("type") or edge.get("label")
    }
    candidate_link_types.update(
        str(value) for (value,) in db.query(Relation.type).filter(Relation.ontology_id == ontology_id).all() if value
    )
    authorized_link_types: set[str] = set()
    for link_type in candidate_link_types:
        synthetic = {"id": f"what-if-link:{link_type}", "link_type_id": link_type, "type": link_type, "properties": {}}
        if redact_link_record(projection, synthetic, link_type_id=link_type) is not None:
            authorized_link_types.add(link_type)

    safe_context = {**context, "nodes": visible_nodes, "edges": visible_edges}
    return safe_context, projection.permission_digest, allowed_properties, property_types, sorted(authorized_link_types)


def _temporal_vector_evidence(
    db,
    replay: TemporalReplay | None,
    point: Decimal | float | None,
    query: str | None,
    *,
    episode_id: str | None,
    context: dict[str, Any],
    permission_digest: str,
    authorized_properties: dict[str, list[str]],
    authorized_link_types: list[str],
    dataset_version_id: str | None,
    snapshot_id: str | None,
    snapshot_hash: str | None,
    schema_revision_id: str,
    schema_revision_hash: str,
    time_value: str | float | None,
    time_zone: str | None,
    requested_by: str | None,
) -> dict[str, Any] | None:
    if query is not None and not isinstance(query, str):
        raise WhatIfError("证据检索词必须是文本")
    normalized_query = (query or "").strip()
    if not normalized_query:
        return None
    if len(normalized_query) > 1000:
        raise WhatIfError("证据检索词最多 1,000 个字符")
    if replay is None or point is None or not episode_id:
        raise WhatIfError("时序向量证据需要固定的 TemporalReplay、episode 和 as-of 时间位置")

    from app.services.v2.vector.chroma_service import ChromaService

    chroma = ChromaService()
    if not chroma.available:
        raise WhatIfError(
            "时序证据检索服务暂不可用，请稍后重试",
            status_code=503,
            code="WHAT_IF_VECTOR_UNAVAILABLE",
        )

    revision = temporal_replay_index_source_revision(replay)
    fact_count = int(db.query(func.count(TemporalFact.id)).filter(TemporalFact.replay_id == replay.id).scalar() or 0)
    if chroma.temporal_index_revision(replay.id, revision) != revision:
        if fact_count > MAX_TEMPORAL_INDEX_FACTS:
            from app.services.v2.temporal_vector_index import request_temporal_vector_index, serialize_temporal_vector_index_job

            job = request_temporal_vector_index(db, replay, requested_by)
            raise WhatIfError(
                "该回放超过同步证据索引上限，已提交后台分批索引；完成后请重新检索",
                status_code=202,
                code="WHAT_IF_VECTOR_INDEXING",
                details={"job": serialize_temporal_vector_index_job(job)},
            )
        fact_rows = db.query(TemporalFact).filter(
            TemporalFact.replay_id == replay.id
        ).order_by(TemporalFact.id.asc()).all()
        fact_payloads = [
            {
                "id": fact.id,
                "subject_id": fact.subject_id,
                "predicate": fact.predicate,
                "object_id": fact.object_id,
                "object_value": fact.object_value,
                "state_key": fact.state_key,
                "valid_from_ordinal": float(fact.valid_from_ordinal),
                "valid_to_ordinal": float(fact.valid_to_ordinal) if fact.valid_to_ordinal is not None else None,
            }
            for fact in fact_rows
        ]
        if not chroma.upsert_temporal_facts(replay.id, fact_payloads, revision):
            raise WhatIfError(
                "时序证据索引未能更新，请稍后重试",
                status_code=503,
                code="WHAT_IF_VECTOR_INDEX_FAILED",
            )

    db.refresh(replay)
    if temporal_replay_index_source_revision(replay) != revision or chroma.temporal_index_revision(replay.id, revision) != revision:
        raise WhatIfError(
            "时序数据或证据索引在准备期间发生变化，请重新读取基线",
            status_code=409,
            code="WHAT_IF_VECTOR_INDEX_STALE",
        )
    from app.services.v2.temporal_vector_index import record_temporal_vector_index_ready

    record_temporal_vector_index_ready(db, replay, fact_count, requested_by)

    try:
        candidates = chroma.temporal_semantic_search(replay.id, normalized_query, float(point), 50, source_revision=revision)
    except RuntimeError as exc:
        raise WhatIfError(
            "时序证据检索执行失败，请稍后重试",
            status_code=503,
            code="WHAT_IF_VECTOR_SEARCH_FAILED",
        ) from exc

    candidate_ids = [str(item.get("fact_id") or "") for item in candidates if item.get("fact_id")]
    current_facts: dict[str, TemporalFact] = {}
    if candidate_ids:
        point_decimal = Decimal(str(point))
        rows = db.query(TemporalFact).filter(
            TemporalFact.replay_id == replay.id,
            TemporalFact.id.in_(candidate_ids),
            TemporalFact.valid_from_ordinal <= point_decimal,
            or_(TemporalFact.valid_to_ordinal.is_(None), TemporalFact.valid_to_ordinal > point_decimal),
        ).all()
        current_facts = {row.id: row for row in rows}
    source_event_ids = {fact.source_event_id for fact in current_facts.values() if fact.source_event_id}
    events_by_id: dict[str, TemporalStreamEvent] = {}
    if source_event_ids:
        events_by_id = {
            event.id: event
            for event in db.query(TemporalStreamEvent).filter(
                TemporalStreamEvent.replay_id == replay.id,
                TemporalStreamEvent.episode_id == episode_id,
                TemporalStreamEvent.status == "committed",
                TemporalStreamEvent.id.in_(sorted(source_event_ids)),
            ).all()
        }

    visible_nodes = {str(node.get("id")): node for node in context.get("nodes") or [] if node.get("id")}
    allowed_links = set(authorized_link_types)
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        fact = current_facts.get(str(candidate.get("fact_id") or ""))
        if (
            fact is None
            or fact.subject_id not in visible_nodes
            or fact.state_key == "__deleted__"
            or fact.source_event_id not in events_by_id
        ):
            continue
        score = round(float(candidate.get("score") or 0.0), 6)
        if fact.object_id:
            if fact.object_id not in visible_nodes or fact.predicate not in allowed_links:
                continue
            result = {
                "kind": "relation",
                "fact_id": fact.id,
                "subject_id": fact.subject_id,
                "predicate": fact.predicate,
                "object_id": fact.object_id,
                "score": score,
            }
        else:
            allowed_fields = set(authorized_properties.get(fact.subject_id) or [])
            field_name = str(fact.state_key or fact.predicate or "")
            safe_properties = visible_nodes[fact.subject_id].get("properties")
            if not isinstance(safe_properties, dict) or field_name not in safe_properties or field_name not in allowed_fields:
                continue
            result = {
                "kind": "property",
                "fact_id": fact.id,
                "subject_id": fact.subject_id,
                "predicate": field_name,
                "value": safe_properties[field_name],
                "score": score,
            }
        result.update({
            "source_event_id": fact.source_event_id,
            "evidence_ref_id": fact.evidence_ref_id,
            "valid_from_ordinal": str(fact.valid_from_ordinal),
            "valid_to_ordinal": str(fact.valid_to_ordinal) if fact.valid_to_ordinal is not None else None,
        })
        results.append(result)
        if len(results) >= 10:
            break

    db.refresh(replay)
    if temporal_replay_index_source_revision(replay) != revision or chroma.temporal_index_revision(replay.id, revision) != revision:
        raise WhatIfError(
            "时序数据或证据索引在检索期间发生变化，请重新读取基线",
            status_code=409,
            code="WHAT_IF_VECTOR_INDEX_STALE",
        )

    query_digest = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()
    return {
        "query": normalized_query,
        "query_digest": query_digest,
        "index_revision": revision,
        "replay_id": replay.id,
        "episode_id": episode_id,
        "dataset_version_id": dataset_version_id,
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "schema_revision_id": schema_revision_id,
        "schema_revision_hash": schema_revision_hash,
        "permission_digest": permission_digest,
        "as_of_ordinal": str(point),
        "time_kind": replay.time_kind,
        "time_value": time_value,
        "time_zone": time_zone,
        "results": results,
        "result_count": len(results),
        "use_as_authoritative_fact": False,
    }


def build_context(db, ontology_id: str, *, target_instance_id: str | None = None, episode_id: str | None = None, at: str | None = None, mode: str = "cumulative", replay_id: str | None = None, evidence_query: str | None = None, user=None) -> dict[str, Any]:
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise WhatIfError("本体不存在")
    if project.data_class != "temporal":
        raise WhatIfError("What-If 仅开放时序本体")
    if not target_instance_id:
        raise WhatIfError("请先从时序数据模型中选择一个对象，再开始推演")
    replay, source_event, episode_id, source_adapter = _resolve_temporal_source(
        db, ontology_id, str(target_instance_id), episode_id, replay_id, at=at, mode=mode
    )
    if replay is None or source_event is None or source_adapter is None:
        raise WhatIfError(
            "What-if 需要已提交的 TemporalReplay 事件；当前图邻域不能替代固定时序快照",
            status_code=409,
            code="WHAT_IF_REPLAY_REQUIRED",
        )
    current_revision = None
    revision_id = replay.schema_revision_id
    if not revision_id:
        raise WhatIfError("时序回放未固定本体修订，无法建立可重放的推演基线")
    current_revision = db.query(OntologyRevision).filter(
        OntologyRevision.id == revision_id,
        OntologyRevision.ontology_id == ontology_id,
    ).first()
    if current_revision is None:
        raise WhatIfError("推演固定的本体修订不存在，请重新读取基线")
    revision_snapshot = current_revision.snapshot_json
    if not isinstance(revision_snapshot, dict):
        raise WhatIfError("推演固定的本体修订快照无效")
    revision_snapshot_bytes = json.dumps(
        revision_snapshot,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    revision_snapshot_hash = hashlib.sha256(revision_snapshot_bytes).hexdigest()
    if current_revision.snapshot_hash and not hmac.compare_digest(
        str(current_revision.snapshot_hash), revision_snapshot_hash
    ):
        raise WhatIfError("推演固定的本体修订摘要不匹配，无法建立可信基线")
    generic_replay = not source_adapter.is_factorynet
    try:
        if generic_replay:
            point = Decimal(str(at)) if at not in (None, "") else (source_event.ordinal if source_event else None)
        else:
            point = float(at) if at not in (None, "") else (float(source_event.ordinal) if source_event else None)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WhatIfError("时间位置必须是有限数值") from exc
    if point is not None and not (point.is_finite() if isinstance(point, Decimal) else math.isfinite(point)):
        raise WhatIfError("时间位置必须是有限数值")
    point_for_storage = str(point) if isinstance(point, Decimal) else point
    time_value = point_for_storage
    time_zone = (source_event.source_ref or {}).get("time_zone") if source_event else None
    if replay and replay.time_kind == "event_time" and source_event:
        source_time_value = (source_event.source_ref or {}).get("event_time")
        try:
            source_time = datetime.fromisoformat(str(source_time_value).replace("Z", "+00:00"))
            if source_time.tzinfo is None or source_time.utcoffset() is None or point is None:
                raise ValueError("event_time or selected cursor has no timezone")
            selected_time = datetime.fromtimestamp(float(point), tz=source_time.tzinfo)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise WhatIfError("事件时间上下文缺少有效时区或游标") from exc
        time_value = selected_time.isoformat()
        time_zone = (source_event.source_ref or {}).get("time_zone") or str(selected_time.tzinfo)
    source_snapshot = None
    if replay.published_snapshot_id:
        source_snapshot = db.query(DataModelSnapshot).filter(
            DataModelSnapshot.id == replay.published_snapshot_id,
            DataModelSnapshot.replay_id == replay.id,
            DataModelSnapshot.ontology_id == ontology_id,
        ).first()
    dataset_version, source_snapshot = _fixed_temporal_versions(db, replay, source_snapshot, point)
    try:
        source_adapter.validate_pins(db, replay, dataset_version)
    except TemporalSourceAdapterError as exc:
        raise WhatIfError(
            str(exc),
            status_code=409,
            code=exc.code,
        ) from exc
    dataset_version_id = dataset_version.id
    graph_namespace = replay.graph_namespace
    # The SQL event/fact tables are authoritative.  The graph projection may
    # accelerate this read, but it is constrained to a published, versioned
    # replay whose snapshot pins were checked above.
    from app.services.v2.temporal_stream_service import stream_graph

    snapshot = stream_graph(
        db,
        replay,
        limit=MAX_NODES,
        offset=0,
        at=point,
        mode=mode,
        relation_state="all",
        episode_id=episode_id,
    )
    nodes_by_id = {
        str(node.get("id")): node
        for node in snapshot.get("nodes", [])
        if node.get("id") is not None
    }
    if int(snapshot.get("total_instances") or len(nodes_by_id)) > len(nodes_by_id):
        raise WhatIfError(
            f"当前回放时间范围超过 {MAX_NODES} 个节点，无法安全构造完整 What-if 基线；"
            "请缩小 episode 或选择更窄的时间窗口"
        )
    if str(target_instance_id) not in nodes_by_id:
        raise WhatIfError("选定 Observation 不在该回放时间范围内，请调整时间位置后重试")

    adjacency: dict[str, set[str]] = {}
    for edge in snapshot.get("edges", []):
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in nodes_by_id or target not in nodes_by_id:
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)
    included = {str(target_instance_id)}
    frontier = set(included)
    for _ in range(2):
        next_frontier = {
            neighbor
            for node_id in frontier
            for neighbor in adjacency.get(node_id, set())
            if neighbor not in included
        }
        included.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break
    context = {
        "available": bool(snapshot.get("available", True)),
        "target_instance_id": str(target_instance_id),
        "nodes": [node for node_id, node in nodes_by_id.items() if node_id in included],
        "edges": [
            edge for edge in snapshot.get("edges", [])
            if str(edge.get("source") or "") in included
            and str(edge.get("target") or "") in included
        ],
        "graph_namespace": snapshot.get("graph_namespace") or replay.graph_namespace,
        "graph_backend": snapshot.get("graph_backend"),
        "graph_recovered": bool(snapshot.get("graph_recovered")),
        "replay_id": replay.id,
    }
    if not context.get("nodes"):
        raise WhatIfError("未找到选定时序对象，请先从数据模型选择一个对象")
    context, permission_digest, authorized_properties, property_types, authorized_link_types = _authorize_context_graph(
        db, ontology_id, revision_id, context, user
    )
    selected = next(
        (node for node in context.get("nodes", []) if str(node.get("id")) == str(context.get("target_instance_id") or target_instance_id)),
        None,
    )
    if selected is None:
        raise WhatIfError("选定时序对象不存在或当前用户无权访问")
    if str(selected.get("id") or "") in deleted_node_ids:
        raise WhatIfError("选定时序对象在该时间位置已删除，无法作为推演基线")
    is_factorynet_replay = source_adapter.is_factorynet
    if is_factorynet_replay and "observation" not in str(selected.get("entity_type") or "").casefold():
        raise WhatIfError("What-If 基线必须是 Observation，请从数据模型选择观测实例")
    vector_evidence = _temporal_vector_evidence(
        db,
        replay,
        point,
        evidence_query,
        episode_id=episode_id,
        context=context,
        permission_digest=permission_digest,
        authorized_properties=authorized_properties,
        authorized_link_types=authorized_link_types,
        dataset_version_id=dataset_version_id,
        snapshot_id=replay.published_snapshot_id if replay else None,
        snapshot_hash=source_snapshot.snapshot_hash if source_snapshot else None,
        schema_revision_id=current_revision.id,
        schema_revision_hash=revision_snapshot_hash,
        time_value=time_value,
        time_zone=time_zone,
        requested_by=str(user.id) if user is not None and getattr(user, "id", None) is not None else None,
    )
    refs = {}
    from app.models.v2.construction import EvidenceRef
    authorized_assertions = {str(node.get("id")) for node in context.get("nodes", []) if node.get("id")}
    authorized_assertions.update(str(edge.get("id")) for edge in context.get("edges", []) if edge.get("id"))
    if authorized_assertions:
        for row in db.query(EvidenceRef).filter(
            EvidenceRef.ontology_id == ontology_id,
            EvidenceRef.assertion_id.in_(sorted(authorized_assertions)),
        ).order_by(EvidenceRef.id).all():
            # The scenario needs a traceable source pointer, not the raw evidence
            # body. Keep content hashes and row references; evidence text stays
            # behind its own authorized evidence reader.
            refs.setdefault(row.assertion_id, []).append({"evidence_ref_id": row.id, "source_file": row.source_file, "source_row": row.source_row_id, "source_sample_id": row.source_sample_id, "content_hash": row.content_hash})
    for node in context.get("nodes", []): node["evidence"] = refs.get(str(node.get("id")), [])
    for edge in context.get("edges", []): edge["evidence"] = refs.get(str(edge.get("id")), [])
    nodes = sorted(context.get("nodes", []), key=lambda item: str(item.get("id") or ""))
    edges = sorted(
        context.get("edges", []),
        key=lambda item: (
            str(item.get("source") or ""),
            str(item.get("target") or ""),
            str(item.get("type") or item.get("label") or ""),
            str(item.get("id") or ""),
        ),
    )
    revision_rules = revision_snapshot.get("logic_rules")
    if not isinstance(revision_rules, list):
        raise WhatIfError("推演固定的本体修订缺少规则快照")
    rule_descriptors: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    for item in revision_rules:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            raise WhatIfError("推演固定的本体修订包含无效规则快照")
        rule_id = str(item["id"]).strip()
        if rule_id in seen_rule_ids:
            raise WhatIfError("推演固定的本体修订包含重复规则")
        seen_rule_ids.add(rule_id)
        rule_descriptors.append({
            "id": rule_id,
            "name_cn": str(item.get("name_cn") or ""),
            "name_en": str(item.get("name_en") or ""),
            "version": str(item.get("version") or ""),
            "status": str(item.get("status") or ""),
            "enabled": bool(item.get("enabled", True)),
            "condition": item.get("condition") or {},
            "effect": item.get("effect") or {},
        })
    rule_descriptors.sort(key=lambda item: item["id"])
    rule_set_digest = hashlib.sha256(json.dumps(
        revision_rules,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    target_id = target_instance_id or context.get("target_instance_id")
    baseline = {
        "ontology_id": ontology_id,
        "data_class": project.data_class,
        "base_revision_id": current_revision.id if current_revision else None,
        "dataset_version_id": dataset_version_id,
        "target_instance_id": target_id,
        "episode_id": episode_id,
        "at": point_for_storage if point is not None else at,
        "mode": mode,
        "nodes": nodes,
        "edges": edges,
        "rules": rule_descriptors,
        "vector_evidence": vector_evidence,
        "limits": {"hops": 2, "nodes": MAX_NODES, "facts": MAX_FACTS, "rules": MAX_RULES, "premises": MAX_PREMISES, "iterations": MAX_ITERATIONS},
        "provenance": {
            "source_kind": ("factorynet_temporal_replay" if replay and replay.source_id == "factorynet_cnc" else "temporal_replay_object") if replay else "falkordb_neighborhood_snapshot",
            "source_id": replay.source_id if replay else None,
            "source_adapter_id": source_adapter.adapter_id if source_adapter else None,
            "source_adapter_version": source_adapter.version if source_adapter else None,
            "source_mapping_digest": source_adapter.mapping_digest(replay) if source_adapter and replay else None,
            "source_mode": replay.source_mode if replay else None,
            "replay_id": replay.id if replay else context.get("replay_id"),
            "source_event_id": source_event.id if source_event else context.get("event_id"),
            "source_event_ordinal": (str(source_event.ordinal) if generic_replay else float(source_event.ordinal)) if source_event else context.get("event_ordinal"),
            "source_sequence": source_event.source_sequence if source_event else None,
            "source_row_id": source_event.source_row_id if source_event else None,
            "entity_key": source_event.entity_key if source_event else None,
            "source_payload_hash": source_event.payload_hash if source_event else None,
            "source_snapshot_id": replay.published_snapshot_id if replay else None,
            "source_snapshot_hash": source_snapshot.snapshot_hash if source_snapshot else None,
            "source_snapshot_through_ordinal": (str(source_snapshot.through_ordinal) if generic_replay else float(source_snapshot.through_ordinal)) if source_snapshot and source_snapshot.through_ordinal is not None else None,
            "source_snapshot_event_count": source_snapshot.event_count if source_snapshot else None,
            "time_kind": replay.time_kind if replay else "ordinal",
            "time_value": time_value,
            "time_zone": time_zone,
            "watermark_ordinal": (str(replay.watermark_ordinal) if generic_replay else float(replay.watermark_ordinal)) if replay and replay.watermark_ordinal is not None else None,
            "watermark_sequence": replay.watermark_sequence if replay else None,
            "graph_namespace": graph_namespace or context.get("graph_namespace"),
            "graph_backend": context.get("graph_backend"),
            "graph_recovered": bool(context.get("graph_recovered")),
            "schema_revision_id": current_revision.id if current_revision else None,
            "schema_revision_hash": revision_snapshot_hash,
            "rule_set_digest": rule_set_digest,
            "vector_index_revision": vector_evidence.get("index_revision") if vector_evidence else None,
            "vector_query_digest": vector_evidence.get("query_digest") if vector_evidence else None,
            "vector_result_count": vector_evidence.get("result_count") if vector_evidence else None,
            "dataset_version_id": dataset_version_id,
            "dataset_version_hash": dataset_version.checksum,
            "permission_digest": permission_digest,
            "authorized_properties_by_object": authorized_properties,
            "property_types_by_object": property_types,
            "authorized_link_types": authorized_link_types,
            "target_instance_id": target_id,
            "episode_id": episode_id,
            "at": point_for_storage if point is not None else at,
            "mode": mode,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
    }
    selection_descriptor = {
        "ontology_id": ontology_id,
        "target_instance_id": str(target_id),
        "replay_id": replay.id,
        "source_adapter_id": source_adapter.adapter_id,
        "source_adapter_version": source_adapter.version,
        "source_mapping_digest": source_adapter.mapping_digest(replay),
        "episode_id": episode_id,
        "mode": mode,
        "time_kind": replay.time_kind,
        "time_value": str(time_value),
        "time_zone": time_zone,
        "vector_query_digest": vector_evidence.get("query_digest") if vector_evidence else None,
    }
    evidence_manifest = {
        "vector_evidence": vector_evidence,
        "node_evidence": [
            {"object_id": node.get("id"), "refs": node.get("evidence") or []}
            for node in nodes
        ],
        "edge_evidence": [
            {"edge_id": edge.get("id"), "refs": edge.get("evidence") or []}
            for edge in edges
        ],
    }
    query_digest = hashlib.sha256(json.dumps(
        selection_descriptor,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    evidence_digest = hashlib.sha256(json.dumps(
        evidence_manifest,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    checkpoint_digest = context_content_digest(baseline)
    baseline["scenario_context"] = seal_scenario_context({
        "ontology_id": ontology_id,
        "schema_revision_id": current_revision.id,
        "schema_revision_hash": revision_snapshot_hash,
        "data_model_snapshot_id": source_snapshot.id,
        "data_model_snapshot_hash": source_snapshot.snapshot_hash,
        "dataset_version_id": dataset_version.id,
        "dataset_version_hash": dataset_version.checksum,
        "replay_id": replay.id,
        "source_id": replay.source_id,
        "episode_id": str(episode_id),
        "time_kind": replay.time_kind,
        "time_value": str(time_value),
        "time_zone": time_zone,
        "watermark_ordinal": str(replay.watermark_ordinal),
        "watermark_sequence": replay.watermark_sequence,
        "scenario_branch_id": None,
        "checkpoint_digest": checkpoint_digest,
        "principal_id": str(user.id),
        "permission_digest": permission_digest,
        "query_digest": query_digest,
        "evidence_digest": evidence_digest,
        "vector_index_revision": vector_evidence.get("index_revision") if vector_evidence else None,
        "source_adapter_id": source_adapter.adapter_id,
        "source_adapter_version": source_adapter.version,
        "source_mapping_digest": source_adapter.mapping_digest(replay),
    }).model_dump(mode="json")
    return seal_context(baseline)


def serialize_scenario(item: WhatIfScenario) -> dict[str, Any]:
    baseline = item.baseline_json or {}
    assumptions = item.assumptions_json or []
    rule_overrides = item.rule_overrides_json or {}
    return {"id": item.id, "ontology_id": item.ontology_id, "name": item.name, "status": item.status, "base_revision_id": item.base_revision_id, "dataset_version_id": item.dataset_version_id, "baseline": baseline, "provenance": baseline.get("provenance") or {}, "assumptions": assumptions, "rule_overrides": rule_overrides, "scenario_input_digest": scenario_inputs_digest(assumptions, rule_overrides), "created_at": item.created_at.isoformat() if item.created_at else None, "updated_at": item.updated_at.isoformat() if item.updated_at else None}


def serialize_run(item: WhatIfRun) -> dict[str, Any]:
    error_code = what_if_run_error_code(item.error)
    return {"id": item.id, "scenario_id": item.scenario_id, "ontology_id": item.ontology_id, "status": item.status, "stage": item.stage, "progress": item.progress, "engine": item.engine, "engine_version": item.engine_version, "context": item.context_json or {}, "baseline": item.baseline_result_json or {}, "scenario": item.scenario_result_json or {}, "diff": item.diff_json or {}, "input_hash": item.input_hash, "error": _WHAT_IF_RUN_ERROR_MESSAGES.get(error_code) if error_code else None, "error_code": error_code, "cancel_requested": bool(item.cancel_requested), "created_at": item.created_at.isoformat() if item.created_at else None, "started_at": item.started_at.isoformat() if item.started_at else None, "completed_at": item.completed_at.isoformat() if item.completed_at else None}
