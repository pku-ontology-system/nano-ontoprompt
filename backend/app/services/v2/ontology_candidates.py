"""AI ontology candidates: read an Auto Ontology export, review it item by item, publish the accepted items.

Auto Ontology (NVIDIA, Apache-2.0) compiles a relational source into terms, column attributes and foreign keys.
This module turns its model-interchange YAML into reviewable candidates, keeps the evidence each one came from
(source table and column, sample values, how it was found), records the human decision, and publishes only the
accepted items as one batch revision through the ontology editor.  Generated candidates never change the ontology.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.models.entity import Entity
from app.models.user import User
from app.models.v2.ontology_candidates import OntologyCandidateItem, OntologyCandidateSet
from app.services.v2.auto_ontology_runner import RunnerError, RunnerUnavailable, get_runner_client
from app.services.v2.dynamic_ontology_service import (
    SAFE_ID,
    OntologyEditError,
    apply_batch_changes,
    normalize_cardinality,
    validate_batch_changes,
)

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_PUBLISH_ITEMS = 50  # the editor's batch limit
EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "auto_ontology"

PROPERTY_TYPES = {"string", "integer", "double", "boolean", "date", "timestamp", "json"}
COLUMN_TYPES = {"text": "string", "character varying": "string", "varchar": "string", "uuid": "string", "char": "string",
                "integer": "integer", "bigint": "integer", "smallint": "integer", "int": "integer",
                "double precision": "double", "real": "double", "numeric": "double", "decimal": "double", "float": "double",
                "boolean": "boolean", "date": "date", "timestamp with time zone": "timestamp",
                "timestamp without time zone": "timestamp", "timestamptz": "timestamp", "json": "json", "jsonb": "json"}
REJECT_CATEGORIES = {"wrong_meaning": "含义不符", "no_evidence": "缺少依据", "duplicate": "与现有内容重复",
                     "out_of_scope": "不在本体范围内", "other": "其他"}
BASIS_LABELS = {"term": "术语", "declared_fk": "声明外键", "semantic_fk": "模型推断外键", "bridge_table": "桥表合成"}
DECISIONS = ("pending", "accepted", "modified", "rejected")


class CandidateError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details or {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------------------------- parsing
def api_name(text: str | None) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^0-9A-Za-z]+", text or "") if part)


def _unique(name: str, used: set[str]) -> str:
    candidate, index = name, 2
    while candidate.lower() in used:
        candidate, index = f"{name}{index}", index + 1
    used.add(candidate.lower())
    return candidate


def parse_interchange(text: str) -> dict[str, Any]:
    """Convert an Auto Ontology model-interchange document into candidate dicts (no database access)."""
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise CandidateError("document_too_large", "文档超过 2 MB", 413)
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CandidateError("invalid_document", "不是有效的 YAML 文档") from exc
    if not isinstance(document, dict) or not isinstance(document.get("data_layer"), dict):
        raise CandidateError("invalid_document", "缺少 data_layer，不是 Auto Ontology 的模型交换文档")
    try:
        return _convert(document)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise CandidateError("invalid_document", f"文档结构不符合 Auto Ontology 的模型交换格式（{type(exc).__name__}）") from exc


def _column_view(column: dict) -> dict:
    return {"name": column["name"], "type": column.get("type"), "unique": bool(column.get("is_unique")),
            "sample_values": [str(v) for v in (column.get("sample_values") or [])[:3]]}


def _convert(document: dict) -> dict[str, Any]:
    data, semantic = document["data_layer"], document.get("semantic_layer") or {}
    tables: dict[str, dict] = {}
    columns: dict[str, tuple[dict, dict]] = {}
    for database in data.get("databases") or []:
        for schema in database.get("schemas") or []:
            for table in schema.get("tables") or []:
                tables[table["id"]] = table
                for column in table.get("columns") or []:
                    columns[column["id"]] = (table, column)

    used_entities: set[str] = set()
    entity_of_table: dict[str, str] = {}
    entities: list[dict] = []
    for term in semantic.get("terms") or []:
        represented = [tables[t] for t in term.get("represents") or [] if t in tables]
        for table in represented:
            if table["id"] in entity_of_table:
                continue
            label = term.get("name") if len(represented) == 1 else table["name"]
            name = _unique(api_name(label) or api_name(table["name"]) or "Entity", used_entities)
            attributes = {a.get("column_id"): a for a in term.get("columns_attributes") or [] if isinstance(a, dict)}
            primary_key = [str(c) for c in table.get("pk") or []]
            properties = []
            for column in table.get("columns") or []:
                attribute = attributes.get(column["id"], {})
                properties.append({"id": column["name"], "name": attribute.get("name") or column["name"],
                                   "type": COLUMN_TYPES.get(str(column.get("type") or "").lower(), "string"),
                                   "description": attribute.get("description") or column.get("description") or "",
                                   "source_field": f"{table['name']}.{column['name']}", "is_identifier": column["name"] in primary_key})
            entity_of_table[table["id"]] = name
            entities.append({
                "kind": "entity_type", "key": f"entity:{name}", "name": name, "basis": "term",
                "draft": {"name_cn": term.get("name") or table["name"], "name_en": name, "type": "EntityType",
                          "description": term.get("description") or table.get("description") or "", "properties": properties},
                "evidence": {"source_table": table["name"], "primary_key": primary_key, "term": term.get("name"),
                             "columns": [_column_view(c) for c in table.get("columns") or []]}})

    attribute_column = {a["id"]: a.get("column_id") for t in semantic.get("terms") or [] for a in t.get("columns_attributes") or []
                        if isinstance(a, dict) and a.get("id")}
    raw_edges = [(fk["source_column_id"], fk["target_column_id"], "declared_fk", True) for fk in data.get("foreign_keys") or []]
    raw_edges += [(fk.get("column_id"), attribute_column.get(fk.get("column_attribute_id")), "semantic_fk", bool(fk.get("use_in_join_paths", True)))
                  for fk in semantic.get("semantic_fks") or []]
    edges, seen = [], set()
    for source_id, target_id, basis, joinable in raw_edges:
        if source_id not in columns or target_id not in columns or source_id == target_id or (source_id, target_id) in seen:
            continue
        seen.add((source_id, target_id))
        edges.append((source_id, target_id, basis, joinable))

    used_names: set[str] = set()
    relationships: list[dict] = []
    warnings: list[dict] = []
    by_source_table: dict[str, list[tuple]] = {}
    for edge in edges:
        by_source_table.setdefault(columns[edge[0]][0]["id"], []).append(edge)

    def edge_view(edge):
        (source_table, source_column), (target_table, target_column) = columns[edge[0]], columns[edge[1]]
        return {"source_table": source_table["name"], "source_column": source_column["name"], "target_table": target_table["name"],
                "target_column": target_column["name"], "basis": edge[2], "used_in_join_paths": edge[3],
                "sample_values": {"source": _column_view(source_column)["sample_values"], "target": _column_view(target_column)["sample_values"]}}

    for table_id, table_edges in by_source_table.items():
        table = tables[table_id]
        if table_id in entity_of_table:
            for edge in table_edges:
                target_table, target_column = columns[edge[1]]
                if target_table["id"] not in entity_of_table:
                    warnings.append({"code": "target_without_object", "table": table["name"],
                                     "message": f"{table['name']}.{columns[edge[0]][1]['name']} 指向 {target_table['name']}，但它没有对应的对象类型"})
                    continue
                source_column = columns[edge[0]][1]
                name = _unique(f"{table['name']}_{source_column['name']}", used_names)
                cardinality = "one-to-one" if source_column.get("is_unique") else "many-to-one"
                evidence = edge_view(edge)
                relationships.append({
                    "kind": "relationship", "key": f"fk:{table['name']}.{source_column['name']}->{target_table['name']}.{target_column['name']}",
                    "name": name, "basis": edge[2],
                    "draft": {"type": name, "source": entity_of_table[table_id], "target": entity_of_table[target_table["id"]],
                              "cardinality": cardinality, "description": "",
                              "properties": {"cardinality": cardinality, "source_field": f"{table['name']}.{source_column['name']}",
                                             "target_field": f"{target_table['name']}.{target_column['name']}", "basis": edge[2]}},
                    "evidence": evidence})
            continue
        # A table without an object type is an association table: two usable foreign keys make one many-to-many relationship.
        usable = [edge for edge in table_edges if columns[edge[1]][0]["id"] in entity_of_table]
        if len(usable) != 2:
            warnings.append({"code": "bridge_unresolved", "table": table["name"],
                             "message": f"{table['name']} 没有对象类型，且只识别出 {len(usable)} 个可用外键，无法合成多对多关系"})
            continue
        first, second = usable
        name = _unique(table["name"], used_names)
        source_name, target_name = entity_of_table[columns[first[1]][0]["id"]], entity_of_table[columns[second[1]][0]["id"]]
        relationships.append({
            "kind": "relationship", "key": f"bridge:{table['name']}", "name": name, "basis": "bridge_table",
            "draft": {"type": name, "source": source_name, "target": target_name, "cardinality": "many-to-many", "description": "",
                      "properties": {"cardinality": "many-to-many", "via_table": table["name"], "basis": "bridge_table",
                                     "source_field": f"{table['name']}.{columns[first[0]][1]['name']}",
                                     "target_field": f"{table['name']}.{columns[second[0]][1]['name']}"}},
            "evidence": {"via_table": table["name"], "edges": [edge_view(first), edge_view(second)]}})

    return {"entities": entities, "relationships": relationships, "warnings": warnings, "table_count": len(tables),
            "column_count": len(columns)}


# ----------------------------------------------------------------------------------------------------- persistence
def _existing_names(db: Session, ontology_id: str) -> dict[str, str]:
    rows = db.query(Entity).filter(Entity.ontology_id == ontology_id, Entity.is_source_deleted.is_(False)).all()
    names: dict[str, str] = {}
    for entity in rows:
        for label in (entity.name_en, str(entity.canonical_id or "").split(":")[-1], entity.name_cn):
            if label:
                names[str(label).lower()] = entity.id
    return names


def load_document(db: Session, cset: OntologyCandidateSet, text: str, generator: dict | None = None) -> OntologyCandidateSet:
    """Parse ``text`` into items of ``cset`` and open it for review."""
    parsed = parse_interchange(text)
    existing = _existing_names(db, cset.ontology_id)
    ordinal = 0
    for row in parsed["entities"] + parsed["relationships"]:
        db.add(OntologyCandidateItem(
            set_id=cset.id, ordinal=ordinal, kind=row["kind"], key=row["key"], name=row["name"], basis=row["basis"],
            draft=row["draft"], evidence=row["evidence"], decision="pending",
            exists_in_ontology=row["kind"] == "entity_type" and row["name"].lower() in existing, history=[]))
        ordinal += 1
    cset.generator = {**(cset.generator or {}), **(generator or {}), "table_count": parsed["table_count"], "column_count": parsed["column_count"]}
    cset.warnings = parsed["warnings"]
    cset.document_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cset.status, cset.imported_at, cset.error = "review", utcnow(), None
    db.flush()
    return cset


def list_examples() -> list[dict]:
    try:
        return json.loads((EXAMPLES_DIR / "index.json").read_text(encoding="utf-8"))["examples"]
    except (OSError, ValueError, KeyError):
        return []


def import_document(db: Session, ontology_id: str, user: User, *, text: str | None, example_id: str | None, title: str | None) -> OntologyCandidateSet:
    generator: dict[str, Any] = {"tool": "NVIDIA Auto Ontology", "reviewed": False}
    origin, label = "file", (title or "导入的 Auto Ontology 文件").strip()[:200]
    if example_id:
        example = next((row for row in list_examples() if row["id"] == example_id), None)
        if example is None:
            raise CandidateError("example_not_found", "没有这个内置样例", 404)
        text = (EXAMPLES_DIR / example["yaml"]).read_text(encoding="utf-8")
        run = json.loads((EXAMPLES_DIR / example["run"]).read_text(encoding="utf-8"))
        generator.update(chat_model=run.get("chat_model"), embed_model=run.get("embed_model"), locality=example["locality"],
                         source_label=example["source_label"], model_label=example["model_label"],
                         seconds=run.get("total_seconds") or run.get("compile_seconds"), llm_calls=run.get("llm_calls"), sample=True)
        origin, label = "example", title or example["label"]
    if not text or not text.strip():
        raise CandidateError("document_required", "请提供 YAML 文档或选择内置样例")
    cset = OntologyCandidateSet(ontology_id=ontology_id, origin=origin, status="review", title=label[:200], generator=generator,
                                progress={}, warnings=[], created_by=user.id)
    db.add(cset)
    db.flush()
    load_document(db, cset, text)
    db.commit()
    return cset


# ------------------------------------------------------------------------------------------------ one-click generation
def _compact(status: dict) -> dict:
    keep = ("status", "stage", "stages", "tables", "llm_calls", "detail", "log_tail", "error", "created_at", "started_at", "finished_at")
    return {key: status.get(key) for key in keep}


def runner_overview() -> dict:
    client = get_runner_client()
    if client is None:
        return {"configured": False, "reachable": False, "reason": "平台没有配置 Auto Ontology 运行器，可以导入导出文件"}
    try:
        health = client.health()
        return {"configured": True, "reachable": True, "health": health, "models": client.models(), "sources": client.sources()}
    except RunnerUnavailable as exc:
        return {"configured": True, "reachable": False, "reason": str(exc)}
    except RunnerError as exc:
        return {"configured": True, "reachable": False, "reason": exc.message}


def start_generation(db: Session, ontology_id: str, user: User, *, source_id: str, model_id: str, acknowledge_cloud: bool) -> OntologyCandidateSet:
    client = get_runner_client()
    if client is None:
        raise CandidateError("runner_not_configured", "平台没有配置 Auto Ontology 运行器", 503)
    try:
        models = {row["id"]: row for row in client.models()}
        sources = {row["id"]: row for row in client.sources()}
        model, source = models.get(model_id), sources.get(source_id)
        if model is None or source is None:
            raise CandidateError("unknown_selection", "没有这个来源或模型")
        if not model.get("available"):
            raise CandidateError("model_unavailable", f"模型“{model['label']}”当前不可用", 409)
        cloud = model.get("locality") != "local"
        if cloud and source.get("privacy_level") == "private":
            raise CandidateError("privacy_blocked", "私有来源只能使用本机模型", 403)
        if cloud and not acknowledge_cloud:
            raise CandidateError("cloud_acknowledgement_required", f"“{model['label']}”是云端模型，需要确认将发送的内容后才能运行", 422,
                                 {"outbound": model.get("outbound") or [], "model": model["label"]})
        version = (client.health().get("auto_ontology") or {}).get("version")
        run = client.start(source_id, model_id, user.username)
    except RunnerUnavailable as exc:
        raise CandidateError("runner_unavailable", str(exc), 503) from exc
    except RunnerError as exc:
        raise CandidateError(exc.code, exc.message, exc.status if exc.status in {403, 409, 422} else 502) from exc
    cset = OntologyCandidateSet(
        ontology_id=ontology_id, origin="runner", status="generating", title=f"{source['label']} · {model['label']}"[:200],
        generator={"tool": "NVIDIA Auto Ontology", "version": version,
                   "chat_model": model["chat_model"], "embed_model": model["embed_model"], "locality": model["locality"],
                   "source_id": source_id, "source_label": source["label"], "model_id": model_id, "model_label": model["label"],
                   "cloud_acknowledged": bool(cloud and acknowledge_cloud), "reviewed": False},
        runner_run_id=run["id"], progress=_compact(run), warnings=[], created_by=user.id)
    db.add(cset)
    db.commit()
    return cset


def sync_generation(db: Session, cset: OntologyCandidateSet) -> OntologyCandidateSet:
    """Bring a running generation up to date; import the export exactly once when the run has finished."""
    if cset.status != "generating" or not cset.runner_run_id:
        return cset
    client = get_runner_client()
    if client is None:
        return cset
    try:
        status = client.status(cset.runner_run_id)
    except RunnerUnavailable as exc:
        cset.progress = {**(cset.progress or {}), "runner_unreachable": str(exc)}
        db.commit()
        return cset
    except RunnerError as exc:
        cset.status, cset.error = "failed", exc.message
        db.commit()
        return cset
    cset.progress = _compact(status)
    if status["status"] == "succeeded":
        locked = db.query(OntologyCandidateSet).filter_by(id=cset.id).with_for_update().first()
        if locked is not None and locked.status == "generating":
            try:
                export = client.export(locked.runner_run_id)
                run = export.get("run") or {}
                load_document(db, locked, export["yaml"], {"seconds": run.get("total_seconds"), "llm_calls": run.get("llm_calls"),
                                                           "commit": (export.get("generator") or {}).get("commit")})
            except (RunnerUnavailable, RunnerError) as exc:
                locked.progress = {**locked.progress, "runner_unreachable": str(exc)}
            except CandidateError as exc:
                db.rollback()
                locked = db.query(OntologyCandidateSet).filter_by(id=cset.id).first()
                locked.status, locked.error = "failed", exc.message
            cset = locked
    elif status["status"] in {"failed", "cancelled"}:
        cset.status = "failed" if status["status"] == "failed" else "cancelled"
        cset.error = status.get("error")
    db.commit()
    return cset


def cancel_generation(db: Session, cset: OntologyCandidateSet) -> OntologyCandidateSet:
    if cset.status != "generating":
        raise CandidateError("not_generating", "候选集不在生成中", 409)
    client = get_runner_client()
    if client is not None and cset.runner_run_id:
        try:
            client.cancel(cset.runner_run_id)
        except (RunnerUnavailable, RunnerError):
            pass
    cset.status, cset.error = "cancelled", "已取消"
    db.commit()
    return cset


# ---------------------------------------------------------------------------------------------------------- review
def _text(value: Any, limit: int, *, required: bool = False, label: str = "内容") -> str:
    if not isinstance(value, str):
        raise CandidateError("invalid_final", f"{label}必须是文本")
    value = value.strip()
    if required and not value:
        raise CandidateError("invalid_final", f"{label}不能为空")
    if len(value) > limit:
        raise CandidateError("invalid_final", f"{label}不能超过 {limit} 个字符")
    return value


def clean_final(item: OntologyCandidateItem, final: dict) -> dict:
    """The reviewer may rename and describe things; endpoints, source fields and ids stay as generated."""
    if not isinstance(final, dict):
        raise CandidateError("invalid_final", "修改内容必须是对象")
    out = copy.deepcopy(item.draft)
    if item.kind == "entity_type":
        if "name_cn" in final:
            out["name_cn"] = _text(final["name_cn"], 200, required=True, label="显示名称")
        if "description" in final:
            out["description"] = _text(final["description"], 2000, label="说明")
        if "properties" in final:
            by_id = {row["id"]: row for row in out["properties"]}
            if not isinstance(final["properties"], list) or not final["properties"]:
                raise CandidateError("invalid_final", "至少保留一个属性")
            properties = []
            for row in final["properties"]:
                base = by_id.get(row.get("id")) if isinstance(row, dict) else None
                if base is None:
                    raise CandidateError("invalid_final", f"不能新增属性：{row.get('id') if isinstance(row, dict) else row}")
                merged = dict(base)
                if "name" in row:
                    merged["name"] = _text(row["name"], 200, required=True, label="属性名称")
                if "description" in row:
                    merged["description"] = _text(row["description"], 1000, label="属性说明")
                if "type" in row:
                    if row["type"] not in PROPERTY_TYPES:
                        raise CandidateError("invalid_final", f"属性类型无效：{row['type']}")
                    merged["type"] = row["type"]
                properties.append(merged)
            out["properties"] = properties
        return out
    if "type" in final:
        out["type"] = _text(final["type"], 100, required=True, label="关系名称")
    if "cardinality" in final:
        try:
            out["cardinality"] = normalize_cardinality(final["cardinality"])
        except OntologyEditError as exc:
            raise CandidateError("invalid_final", str(exc)) from exc
        out["properties"] = {**out.get("properties", {}), "cardinality": out["cardinality"]}
    if "description" in final:
        out["description"] = _text(final["description"], 1000, label="说明")
    return out


def review_item(db: Session, cset: OntologyCandidateSet, item: OntologyCandidateItem, user: User, *, decision: str,
                final: dict | None = None, reject_category: str | None = None, reject_reason: str | None = None) -> OntologyCandidateItem:
    if cset.status != "review":
        raise CandidateError("not_reviewable", "候选集当前不能审阅", 409)
    if decision not in DECISIONS:
        raise CandidateError("invalid_decision", "决定必须是 pending、accepted、modified 或 rejected")
    if item.exists_in_ontology and decision in {"accepted", "modified"}:
        raise CandidateError("already_exists", "本体里已经有同名对象类型，不能再采纳这条候选", 409)
    merged = None
    if decision == "modified":
        merged = clean_final(item, final or {})
        if merged == item.draft:
            decision, merged = "accepted", None
    if decision == "rejected":
        if reject_category not in REJECT_CATEGORIES:
            raise CandidateError("reject_category_required", "驳回时请选择原因类别")
        reject_reason = (reject_reason or "").strip()
        if len(reject_reason) < 2:
            raise CandidateError("reject_reason_required", "驳回时请写明理由")
    item.decision, item.final = decision, merged
    item.reject_category = reject_category if decision == "rejected" else None
    item.reject_reason = reject_reason if decision == "rejected" else None
    item.reviewed_by, item.reviewed_at = user.id, utcnow()
    item.history = [*(item.history or []), {"at": item.reviewed_at.isoformat(), "actor": user.username, "decision": decision,
                                              "category": item.reject_category, "reason": item.reject_reason}]
    db.commit()
    return item


# --------------------------------------------------------------------------------------------------------- publish
def _entity_slug(cset: OntologyCandidateSet, name: str) -> str:
    entity_id = f"ao_{cset.id.split('-')[0]}_{name}"
    if not SAFE_ID.match(entity_id):
        raise CandidateError("invalid_name", f"对象类型名称不能用作标识：{name}")
    return entity_id


def build_operations(db: Session, cset: OntologyCandidateSet, chosen: list[OntologyCandidateItem]) -> tuple[list[dict], list[OntologyCandidateItem]]:
    """Operations in editor order (object types first) and the items they belong to, one to one."""
    operations: list[dict] = []
    ordered: list[OntologyCandidateItem] = []
    new_ids: dict[str, str] = {}
    for item in (row for row in chosen if row.kind == "entity_type"):
        payload = copy.deepcopy(item.final or item.draft)
        payload["canonical_id"] = new_ids[item.name] = _entity_slug(cset, item.name)
        operations.append({"target_kind": "entity_type", "operation": "add", "payload": payload})
        ordered.append(item)
    existing = _existing_names(db, cset.ontology_id)
    for item in (row for row in chosen if row.kind == "relationship"):
        payload = copy.deepcopy(item.final or item.draft)
        endpoints = {}
        for side in ("source", "target"):
            name = payload[side]
            endpoint = new_ids.get(name) or existing.get(str(name).lower())
            if endpoint is None:
                raise CandidateError("endpoint_not_accepted", f"关系“{payload['type']}”的{'起点' if side == 'source' else '终点'}“{name}”没有被采纳，也不在现有本体中",
                                     409, {"item_id": item.id, "endpoint": name})
            endpoints[side] = endpoint
        payload.update(endpoints)
        payload["properties"] = {**payload.get("properties", {}), "provenance": {"tool": "NVIDIA Auto Ontology", "candidate_set": cset.id, "item": item.key,
                                                                                 "decision": item.decision}}
        if payload.get("description"):
            payload["properties"]["description"] = payload["description"]
        operations.append({"target_kind": "relationship", "operation": "add", "payload": payload})
        ordered.append(item)
    return operations, ordered


def _chosen(cset: OntologyCandidateSet, items: list[OntologyCandidateItem]) -> list[OntologyCandidateItem]:
    if cset.status != "review":
        raise CandidateError("not_reviewable", "候选集当前不能发布", 409)
    pending = [i for i in items if i.decision == "pending" and not i.exists_in_ontology]
    if pending:
        raise CandidateError("pending_items", f"还有 {len(pending)} 条候选没有处理", 409, {"pending": [i.id for i in pending]})
    chosen = [i for i in items if i.decision in {"accepted", "modified"}]
    if not chosen:
        raise CandidateError("nothing_to_publish", "没有已采纳的候选", 422)
    if len(chosen) > MAX_PUBLISH_ITEMS:
        raise CandidateError("too_many_items", f"一次最多发布 {MAX_PUBLISH_ITEMS} 条，请先驳回一部分", 422)
    return chosen


def _editor_error(exc: OntologyEditError) -> CandidateError:
    status = 404 if exc.code == "NOT_FOUND" else 409 if exc.code in {"REVISION_CONFLICT", "CHANGE_BLOCKED"} else 422
    return CandidateError(f"editor_{exc.code.lower()}", str(exc), status, dict(exc.details or {}))


def validate_publish(db: Session, cset: OntologyCandidateSet, items: list[OntologyCandidateItem], base_revision_id: str | None) -> dict:
    operations, _ = build_operations(db, cset, _chosen(cset, items))
    try:
        return validate_batch_changes(db, cset.ontology_id, {"operations": operations, "base_revision_id": base_revision_id})
    except OntologyEditError as exc:
        raise _editor_error(exc) from exc


def publish(db: Session, cset: OntologyCandidateSet, items: list[OntologyCandidateItem], user: User, base_revision_id: str | None) -> dict:
    operations, ordered = build_operations(db, cset, _chosen(cset, items))
    try:
        result = apply_batch_changes(db, cset.ontology_id, {"operations": operations, "base_revision_id": base_revision_id,
                                                           "note": f"AI 本体候选发布：{cset.title}"}, user_id=user.id)
    except OntologyEditError as exc:
        raise _editor_error(exc) from exc
    revision = result["revision"]
    applied = result.get("operations") or []
    for item, operation in zip(ordered, applied):
        item.publish_result = {"revision_id": revision["id"], "revision_no": revision["revision_no"], "target_id": operation.get("target_id")}
    cset.status, cset.published_revision_id = "published", revision["id"]
    cset.published_by, cset.published_at = user.id, utcnow()
    db.commit()
    return result


# ------------------------------------------------------------------------------------------------------ serialization
def _counts() -> dict:
    return {"total": 0, "accepted": 0, "modified": 0, "rejected": 0, "pending": 0}


def stats(items: list[OntologyCandidateItem]) -> dict:
    by_kind, by_basis, reasons = {}, {}, {}
    totals = _counts()
    for item in items:
        for bucket in (totals, by_kind.setdefault(item.kind, _counts()), by_basis.setdefault(item.basis, _counts())):
            bucket["total"] += 1
            bucket[item.decision] += 1
        if item.decision == "rejected" and item.reject_category:
            reasons[item.reject_category] = reasons.get(item.reject_category, 0) + 1
    decided = totals["accepted"] + totals["modified"] + totals["rejected"]
    return {**totals, "decided": decided, "by_kind": by_kind, "by_basis": by_basis, "reject_reasons": reasons,
            "accept_rate": round((totals["accepted"] + totals["modified"]) / decided, 3) if decided else None,
            "edits": totals["modified"]}


def serialize_item(item: OntologyCandidateItem) -> dict:
    return {"id": item.id, "ordinal": item.ordinal, "kind": item.kind, "key": item.key, "name": item.name, "basis": item.basis,
            "basis_label": BASIS_LABELS.get(item.basis, item.basis), "draft": item.draft, "evidence": item.evidence, "final": item.final,
            "effective": item.final or item.draft, "decision": item.decision, "reject_category": item.reject_category,
            "reject_reason": item.reject_reason, "exists_in_ontology": item.exists_in_ontology, "history": item.history or [],
            "reviewed_by": item.reviewed_by, "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
            "publish_result": item.publish_result}


def serialize_set(cset: OntologyCandidateSet, items: list[OntologyCandidateItem] | None = None) -> dict:
    payload = {
        "id": cset.id, "ontology_id": cset.ontology_id, "origin": cset.origin, "status": cset.status, "title": cset.title,
        "generator": cset.generator or {}, "progress": cset.progress or {}, "warnings": cset.warnings or [], "error": cset.error,
        "document_digest": cset.document_digest, "published_revision_id": cset.published_revision_id, "created_by": cset.created_by,
        "published_by": cset.published_by, "created_at": cset.created_at.isoformat() if cset.created_at else None,
        "imported_at": cset.imported_at.isoformat() if cset.imported_at else None,
        "published_at": cset.published_at.isoformat() if cset.published_at else None}
    if items is not None:
        payload["items"] = [serialize_item(item) for item in items]
        payload["stats"] = stats(items)
    return payload
