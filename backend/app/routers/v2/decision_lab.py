"""Read and run the bounded public benchmark decision demonstrations."""
from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_editor
from app.models.entity_instance import EntityInstance
from app.models.user import User
from app.models.v2.construction import EvidenceRef
from app.models.v2.dataset import Dataset, DatasetVersion, MultimodalSample
from app.models.v2.decision_lab import DecisionLabRun
from app.services.v2.authorization_service import field_permitted
from app.services.v2.decision_cases import CaseError, DecisionCaseService
from app.services.v2.decision_lab import (
    DESCRIPTIONS, ONTOLOGY_IDS, PRESETS, RULE_VERSION, calculate, canonical_sha, load_examples,
)
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError
from app.services.storage_service import get_storage_service


router = APIRouter()


class RunRequest(BaseModel):
    client_request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=120)
    case_key: str = Field(default="custom", min_length=1, max_length=100)
    case_name: str = Field(default="自定义方案", min_length=1, max_length=200)
    parameters: dict
    expected_source_digest: str | None = Field(default=None, min_length=64, max_length=64)


def _access(db: Session, user: User, domain: str) -> tuple[str, DatasetVersion]:
    if domain not in RULE_VERSION:
        raise HTTPException(404, "未知演示类型")
    ontology_id = ONTOLOGY_IDS[domain]
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise HTTPException(404, "演示本体尚未安装或当前用户无权查看") from None
    _, source, slice_digest = load_examples(domain)
    if domain == "ibadas":
        dataset = db.query(Dataset).filter(Dataset.schema_json["source_id"].as_string() == source["source_id"]).first()
        version = db.get(DatasetVersion, dataset.latest_version_id) if dataset and dataset.latest_version_id else None
    else:
        dataset = db.query(Dataset).filter(Dataset.schema_json["source_id"].as_string() == f"decision_lab_{domain}").first()
        version = db.get(DatasetVersion, dataset.latest_version_id) if dataset and dataset.latest_version_id else None
    expected_checksum = source["source_sha256"] if domain == "ibadas" else slice_digest
    if version is None or not version.storage_uri or version.checksum != expected_checksum:
        raise HTTPException(409, "演示来源版本不一致，请重新安装样例")
    if not get_storage_service().object_exists(version.storage_uri):
        raise HTTPException(409, "演示来源版本不可用，请重新安装样例")
    return ontology_id, version


def _summary(run: DecisionLabRun) -> dict:
    return {"id": run.id, "domain": run.domain, "case_key": run.case_key, "case_name": run.case_name,
            "rule_version": run.rule_version, "parameters": run.parameters,
            "metrics": (run.result_json or {}).get("metrics", {}), "source_digest": run.source_digest,
            "input_digest": run.input_digest, "output_digest": run.output_digest,
            "created_at": run.created_at.isoformat() if isinstance(run.created_at, datetime) else str(run.created_at),
            "created_by": run.created_by}


def _run_result_visible(db: Session, user: User, ontology_id: str, result: dict,
                        visibility_cache: dict[tuple[str, str], bool] | None = None) -> bool:
    """Aggregates can disclose hidden rows, so only release complete result sets."""
    service = DecisionCaseService(db, ontology_id, user)
    for item in result.get("rows", []):
        object_id = item.get("object_id") if isinstance(item, dict) else None
        if not isinstance(object_id, str):
            return False
        cache_key = (ontology_id, object_id)
        if visibility_cache is not None and cache_key in visibility_cache:
            if not visibility_cache[cache_key]:
                return False
            continue
        try:
            _, _, auth, _ = service._target(object_id)
        except CaseError:
            if visibility_cache is not None:
                visibility_cache[cache_key] = False
            return False
        allowed = auth["projection"].unrestricted
        if visibility_cache is not None:
            visibility_cache[cache_key] = allowed
        if not allowed:
            return False
    return True


def _restricted_summary(run: DecisionLabRun) -> dict:
    return {"id": run.id, "domain": run.domain, "case_key": run.case_key,
            "case_name": run.case_name, "rule_version": run.rule_version,
            "source_digest": run.source_digest, "access_state": "restricted",
            "created_at": run.created_at.isoformat() if isinstance(run.created_at, datetime) else str(run.created_at)}


@router.get("")
def catalog(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    domains = []
    for domain in RULE_VERSION:
        try:
            ontology_id, version = _access(db, user, domain)
            rows, source, digest = load_examples(domain)
        except (HTTPException, OSError, ValueError):
            continue
        domains.append({"id": domain, **DESCRIPTIONS[domain], "ontology_id": ontology_id,
                        "dataset_id": version.dataset_id, "dataset_version_id": version.id,
                        "sample_count": len(rows), "source_digest": digest, "source_url": source["url"],
                        "license": source.get("license"), "selection": source["selection"],
                        "presets": PRESETS[domain]})
    return {"domains": domains, "wood_path": "/what-if", "wood_name": "木材供应韧性演示"}


@router.get("/{domain}/runs")
def list_runs(domain: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _access(db, user, domain)
    runs = db.query(DecisionLabRun).filter_by(domain=domain).order_by(DecisionLabRun.created_at.desc()).limit(30).all()
    visibility_cache: dict[tuple[str, str], bool] = {}
    return {"runs": [_summary(run) if _run_result_visible(db, user, run.ontology_id,
                                                         run.result_json or {}, visibility_cache)
                     else _restricted_summary(run) for run in runs]}


@router.get("/{domain}/runs/{run_id}")
def get_run(domain: str, run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _access(db, user, domain)
    run = db.get(DecisionLabRun, run_id)
    if run is None or run.domain != domain:
        raise HTTPException(404, "计算记录不存在")
    if not _run_result_visible(db, user, run.ontology_id, run.result_json or {}):
        raise HTTPException(403, "计算结果包含当前不可访问的对象或字段")
    return {**_summary(run), "result": run.result_json, "ontology_id": run.ontology_id,
            "dataset_version_id": run.dataset_version_id}


@router.post("/{domain}/runs", status_code=201)
def create_run(domain: str, body: RunRequest, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    ontology_id, version = _access(db, user, domain)
    existing = db.query(DecisionLabRun).filter_by(domain=domain, created_by=user.id,
                                                  client_request_id=body.client_request_id).first()
    if existing:
        if existing.parameters != body.parameters or existing.case_key != body.case_key:
            raise HTTPException(409, "同一请求 ID 已用于另一组参数")
        if not _run_result_visible(db, user, existing.ontology_id, existing.result_json or {}):
            raise HTTPException(403, "计算结果包含当前不可访问的对象或字段")
        return {**_summary(existing), "result": existing.result_json, "ontology_id": existing.ontology_id,
                "dataset_version_id": existing.dataset_version_id}
    try:
        result = calculate(domain, body.parameters)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.expected_source_digest and body.expected_source_digest != result["source_digest"]:
        raise HTTPException(409, "来源切片已变化，请刷新页面重新确认参数")
    if not _run_result_visible(db, user, ontology_id, result):
        raise HTTPException(403, "计算结果包含当前不可访问的对象或字段")
    row = DecisionLabRun(domain=domain, ontology_id=ontology_id, dataset_version_id=version.id,
                         case_key=body.case_key, case_name=body.case_name, rule_version=RULE_VERSION[domain],
                         parameters=result["parameters"], result_json=result, source_digest=result["source_digest"],
                         input_digest=canonical_sha({"domain": domain, "rule": RULE_VERSION[domain],
                                                     "source": result["source_digest"], "parameters": result["parameters"]}),
                         output_digest=canonical_sha(result), client_request_id=body.client_request_id, created_by=user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {**_summary(row), "result": row.result_json, "ontology_id": ontology_id,
            "dataset_version_id": version.id}


@router.get("/{domain}/objects/{object_id}/evidence")
def object_evidence(domain: str, object_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ontology_id, _ = _access(db, user, domain)
    instance = db.get(EntityInstance, object_id)
    if instance is None or instance.ontology_id != ontology_id:
        raise HTTPException(404, "样例对象不存在")
    ref = db.get(EvidenceRef, f"{ontology_id}:evidence:{object_id}")
    if ref is None:
        raise HTTPException(404, "来源证据不存在")
    try:
        _, object_type, auth, _ = DecisionCaseService(db, ontology_id, user)._target(object_id)
    except CaseError:
        raise HTTPException(404, "样例对象不存在或不可访问") from None
    sample = db.get(MultimodalSample, ref.source_sample_id) if ref.source_sample_id else None
    try:
        source_properties = json.loads(ref.evidence_text) if ref.evidence_text else {}
    except (TypeError, ValueError):
        source_properties = {}
    current_properties = auth["record"]["properties"]
    if isinstance(source_properties, dict):
        record = {"id": instance.id, "entity_id": instance.entity_id, "entity_type": object_type,
                  "object_type_resource_id": instance.object_type_resource_id,
                  "properties": instance.row_data or {}}
        source_properties = {key: value for key, value in source_properties.items()
                             if field_permitted(auth["projection"], record, key)}
    return {"object_id": object_id, "ontology_id": ontology_id, "properties": current_properties,
            "evidence_ref_id": ref.id,
            "source_properties": source_properties if isinstance(source_properties, dict) else {},
            "current_properties": current_properties,
            "source_dataset_id": ref.source_dataset_id, "source_version": ref.source_version,
            "source_file": ref.source_file, "source_row_id": ref.source_row_id,
            "source_sample_id": sample.id if sample else None, "source_media_id": ref.source_media_id,
            "evidence_text": json.dumps(source_properties, ensure_ascii=False, sort_keys=True),
            "content_hash": ref.content_hash,
            "construction_run_id": ref.construction_run_id}
