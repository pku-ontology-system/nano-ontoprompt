"""Immutable ontology snapshots and small, reviewable revision diffs."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.action import Action
from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.logic import LogicRule
from app.models.ontology import OntologyProject
from app.models.ontology_revision import OntologyRevision
from app.models.relation import Relation
from app.services.storage_service import get_storage_service


def snapshot_ontology(db: Session, ontology_id: str) -> dict[str, Any]:
    all_entities = db.query(Entity).filter(
        Entity.ontology_id == ontology_id,
    ).order_by(Entity.id.asc()).all()
    entities = [item for item in all_entities if not item.is_source_deleted]
    id_to_name = {item.id: item.name_cn for item in entities}
    all_relations = db.query(Relation).filter(
        Relation.ontology_id == ontology_id,
    ).order_by(Relation.id.asc()).all()
    relations = [item for item in all_relations
                 if item.source_entity in id_to_name and item.target_entity in id_to_name]
    excluded_relations = [item for item in all_relations
                          if item.source_entity not in id_to_name or item.target_entity not in id_to_name]
    rules = db.query(LogicRule).filter(LogicRule.ontology_id == ontology_id).order_by(LogicRule.id.asc()).all()
    actions = db.query(Action).filter(Action.ontology_id == ontology_id).order_by(Action.id.asc()).all()
    all_instances = db.query(EntityInstance).filter(
        EntityInstance.ontology_id == ontology_id,
        EntityInstance.is_deleted.is_(False),
    ).order_by(EntityInstance.created_at.asc()).all()
    instances = [item for item in all_instances if item.entity_id in id_to_name]
    excluded_instances = [item for item in all_instances if item.entity_id not in id_to_name]
    instance_counts: dict[str, int] = {}
    instance_samples: dict[str, list[dict[str, Any]]] = {}
    for item in instances:
        instance_counts[item.entity_id] = instance_counts.get(item.entity_id, 0) + 1
        samples = instance_samples.setdefault(item.entity_id, [])
        if len(samples) < 3:
            samples.append({"id": item.id, "row_identity": item.row_identity, "row_data": item.row_data or {}})
    integrity = {
        "status": "degraded" if excluded_relations or excluded_instances else "clean",
        "source_deleted_entity_count": sum(1 for item in all_entities if item.is_source_deleted),
        "excluded_relation_count": len(excluded_relations),
        "excluded_instance_count": len(excluded_instances),
        "excluded_relation_samples": [
            {"id": item.id, "source_entity": item.source_entity, "target_entity": item.target_entity}
            for item in excluded_relations[:20]
        ],
        "excluded_instance_samples": [
            {"id": item.id, "entity_id": item.entity_id}
            for item in excluded_instances[:20]
        ],
    }
    return {
        "entities": [{"id": item.id, "name_cn": item.name_cn, "name_en": item.name_en, "name_abbr": item.name_abbr, "snomed_id": item.snomed_id, "canonical_id": item.canonical_id, "type": item.type, "description": item.description, "properties": item.properties or {}, "confidence": item.confidence, "version": item.version, "instance_count": instance_counts.get(item.id, 0), "instance_samples": instance_samples.get(item.id, [])} for item in entities],
        "relations": [{"id": item.id, "source_entity": item.source_entity, "target_entity": item.target_entity, "source_name": id_to_name.get(item.source_entity), "target_name": id_to_name.get(item.target_entity), "type": item.type, "properties": item.properties or {}, "confidence": item.confidence} for item in relations],
        "logic_rules": [{"id": item.id, "name_cn": item.name_cn, "name_en": item.name_en, "description": item.description, "formula": item.formula, "linked_entities": item.linked_entities, "condition": getattr(item, "condition_json", {}), "effect": getattr(item, "effect_json", {}), "evidence": getattr(item, "evidence_json", {}), "model_invocation_id": getattr(item, "model_invocation_id", None), "revision_id": getattr(item, "revision_id", None), "confidence": item.confidence, "version": item.version, "enabled": bool(item.enabled), "status": item.status} for item in rules],
        "actions": [{"id": item.id, "name_cn": item.name_cn, "name_en": item.name_en, "description": item.description, "execution_rule": item.execution_rule, "function_code": item.function_code, "linked_entities": item.linked_entities or [], "linked_logic_ids": item.linked_logic_ids or [], "confidence": item.confidence, "version": item.version, "enabled": bool(item.enabled), "status": item.status} for item in actions],
        "instance_count": len(instances),
        "observed_active_instance_count": len(all_instances),
        "integrity": integrity,
    }


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_revision(
    db: Session,
    ontology_id: str,
    *,
    source_run_id: str | None = None,
    snapshot: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    parent_revision_id: str | None = None,
    commit: bool = True,
    revision_id: str | None = None,
) -> OntologyRevision:
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise ValueError("Ontology not found")
    if snapshot is None:
        snapshot = snapshot_ontology(db, ontology_id)
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = snapshot_digest(snapshot)
    latest_no = db.query(OntologyRevision.revision_no).filter(OntologyRevision.ontology_id == ontology_id).order_by(OntologyRevision.revision_no.desc()).first()
    revision_no = int(latest_no[0]) + 1 if latest_no else 1
    current = db.query(OntologyRevision).filter(OntologyRevision.ontology_id == ontology_id, OntologyRevision.is_current.is_(True)).all()
    for item in current:
        item.is_current = False
        if item.status == "current":
            item.status = "superseded"
    storage_uri = get_storage_service().put_bytes("curated-datasets", f"ontologies/{ontology_id}/revisions/{revision_no}-{digest[:12]}.json", payload, content_type="application/json")
    revision = OntologyRevision(
        id=revision_id or str(uuid.uuid4()), ontology_id=ontology_id, revision_no=revision_no,
        parent_revision_id=parent_revision_id, source_run_id=source_run_id,
        graph_namespace=f"ontology:{ontology_id}:r{revision_no}", snapshot_uri=storage_uri,
        snapshot_json=snapshot, snapshot_hash=digest,
        summary=summary or {"entity_count": len(snapshot.get("entities", [])), "instance_count": int(snapshot.get("instance_count", 0)), "relation_count": len(snapshot.get("relations", [])), "logic_count": len(snapshot.get("logic_rules", [])), "action_count": len(snapshot.get("actions", []))},
        status="current", is_current=True, created_at=datetime.now(timezone.utc),
    )
    db.add(revision)
    project.current_revision_id = revision.id
    project.version = f"r{revision_no}"
    if commit:
        db.commit()
        db.refresh(revision)
    else:
        db.flush()
    return revision


def _validated_restore_snapshot(snapshot: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(snapshot, dict):
        raise ValueError("修订快照格式无效")
    integrity = snapshot.get("integrity") or {}
    if not isinstance(integrity, dict):
        raise ValueError("修订快照的完整性摘要无效")
    try:
        excluded_relations = int(integrity.get("excluded_relation_count", 0) or 0)
        excluded_instances = int(integrity.get("excluded_instance_count", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("修订快照的完整性摘要无效") from exc
    if (integrity.get("status") not in (None, "clean")
            or excluded_relations < 0 or excluded_instances < 0
            or excluded_relations > 0 or excluded_instances > 0):
        raise ValueError("拒绝恢复包含孤立关系或无主实例的不完整修订快照")

    entity_list = snapshot.get("entities", [])
    if (not isinstance(entity_list, list)
            or any(not isinstance(item, dict) or not item.get("id") for item in entity_list)):
        raise ValueError("修订快照的实体列表格式无效")
    entity_rows = {str(item["id"]): item for item in entity_list}
    if len(entity_rows) != len(entity_list):
        raise ValueError("修订快照包含重复实体 ID")

    snapshot_relations = snapshot.get("relations", [])
    if (not isinstance(snapshot_relations, list)
            or any(not isinstance(item, dict) for item in snapshot_relations)):
        raise ValueError("修订快照的关系列表格式无效")
    for row in snapshot_relations:
        if row.get("source_entity") not in entity_rows or row.get("target_entity") not in entity_rows:
            raise ValueError(f"关系 {row.get('type') or row.get('id')} 引用了不存在的实体类型")
    return entity_rows, snapshot_relations


def snapshot_restore_blockers(db: Session, ontology_id: str, snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Return entity deletions that would cascade or strand existing instance rows."""
    entity_rows, _ = _validated_restore_snapshot(snapshot)
    current_entities = {
        item.id: item for item in db.query(Entity).filter(Entity.ontology_id == ontology_id).all()
    }
    removing = set(current_entities) - set(entity_rows)
    if not removing:
        return []
    used = (
        db.query(EntityInstance.entity_id)
        .filter(EntityInstance.ontology_id == ontology_id, EntityInstance.entity_id.in_(removing))
        .distinct()
        .all()
    )
    return [
        {"entity_id": row[0], "name": current_entities[row[0]].name_cn}
        for row in used if row[0] in current_entities
    ]


def materialize_snapshot(db: Session, ontology_id: str, snapshot: dict[str, Any]) -> None:
    """Restore schema objects without deleting source-generated instances.

    This function deliberately does not commit.  Callers can materialise the
    schema, create a new immutable revision and append provenance in one
    transaction.  Removing an entity that still owns real instances is
    rejected instead of cascading those instances away.
    """
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not project:
        raise ValueError("Ontology not found")
    entity_rows, snapshot_relations = _validated_restore_snapshot(snapshot)

    current_entities = {
        item.id: item for item in db.query(Entity).filter(Entity.ontology_id == ontology_id).all()
    }
    blockers = snapshot_restore_blockers(db, ontology_id, snapshot)
    if blockers:
        names = [item["name"] for item in blockers]
        raise ValueError(f"恢复会删除仍有真实实例的实体类型：{'、'.join(names)}")
    removing = set(current_entities) - set(entity_rows)

    # Relations must be removed before an entity.  Instances remain untouched.
    db.query(Relation).filter(Relation.ontology_id == ontology_id).delete(synchronize_session=False)
    for entity_id in removing:
        db.delete(current_entities[entity_id])
    db.flush()

    allowed_entity_fields = {
        "name_cn", "name_en", "name_abbr", "snomed_id", "canonical_id", "type",
        "description", "properties", "confidence", "version",
    }
    for entity_id, row in entity_rows.items():
        entity = current_entities.get(entity_id)
        values = {key: row.get(key) for key in allowed_entity_fields if key in row}
        if entity is None:
            entity = Entity(id=entity_id, ontology_id=ontology_id, name_cn=str(row.get("name_cn") or entity_id))
            db.add(entity)
        for key, value in values.items():
            setattr(entity, key, value)
    db.flush()

    for row in snapshot_relations:
        db.add(Relation(
            id=str(row.get("id") or uuid.uuid4()), ontology_id=ontology_id,
            source_entity=str(row["source_entity"]), target_entity=str(row["target_entity"]),
            type=str(row.get("type") or "RELATED"), properties=row.get("properties") or {},
            confidence=float(row.get("confidence") or 1.0),
        ))

    db.query(LogicRule).filter(LogicRule.ontology_id == ontology_id).delete(synchronize_session=False)
    for row in snapshot.get("logic_rules", []):
        rule = LogicRule(
            id=str(row.get("id") or uuid.uuid4()), ontology_id=ontology_id,
            name_cn=str(row.get("name_cn") or "未命名规则"), name_en=row.get("name_en"),
            description=row.get("description"), formula=row.get("formula"),
            condition_json=row.get("condition") or {}, effect_json=row.get("effect") or {},
            evidence_json=row.get("evidence") or {}, model_invocation_id=row.get("model_invocation_id"),
            revision_id=row.get("revision_id"), confidence=float(row.get("confidence") or 1.0),
            version=str(row.get("version") or "v0.1"), enabled=bool(row.get("enabled", True)),
            status=str(row.get("status") or "draft"),
        )
        rule.linked_entities = list(row.get("linked_entities") or [])
        db.add(rule)

    db.query(Action).filter(Action.ontology_id == ontology_id).delete(synchronize_session=False)
    for row in snapshot.get("actions", []):
        db.add(Action(
            id=str(row.get("id") or uuid.uuid4()), ontology_id=ontology_id,
            name_cn=str(row.get("name_cn") or "未命名动作"), name_en=row.get("name_en"),
            description=row.get("description"), execution_rule=row.get("execution_rule"),
            function_code=row.get("function_code"), linked_entities=row.get("linked_entities") or [],
            linked_logic_ids=row.get("linked_logic_ids") or [], confidence=float(row.get("confidence") or 1.0),
            version=str(row.get("version") or "v0.1"), enabled=bool(row.get("enabled", True)),
            status=str(row.get("status") or "draft"),
        ))
    db.flush()


def serialize_revision(revision: OntologyRevision) -> dict[str, Any]:
    return {
        "id": revision.id, "ontology_id": revision.ontology_id, "revision_no": revision.revision_no,
        "parent_revision_id": revision.parent_revision_id, "source_run_id": revision.source_run_id,
        "graph_namespace": revision.graph_namespace, "snapshot_uri": revision.snapshot_uri,
        "snapshot_hash": revision.snapshot_hash, "summary": revision.summary or {},
        "metadata_digest": getattr(revision, "metadata_digest", None),
        "metadata_schema_version": getattr(revision, "metadata_schema_version", None),
        "status": revision.status, "is_current": bool(revision.is_current),
        "created_at": revision.created_at.isoformat() if revision.created_at else None,
    }


def compare_revisions(left: OntologyRevision, right: OntologyRevision) -> dict[str, Any]:
    def keyed(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
        return {str(item.get(key)): item for item in items}

    sections = [("entities", "id"), ("relations", "id"), ("logic_rules", "id"), ("actions", "id")]
    diff: dict[str, Any] = {}
    for name, key in sections:
        before = keyed((left.snapshot_json or {}).get(name, []), key)
        after = keyed((right.snapshot_json or {}).get(name, []), key)
        added = [after[item] for item in sorted(set(after) - set(before))]
        removed = [before[item] for item in sorted(set(before) - set(after))]
        changed = [{"before": before[item], "after": after[item]} for item in sorted(set(before) & set(after)) if before[item] != after[item]]
        diff[name] = {"added": added, "removed": removed, "changed": changed, "added_count": len(added), "removed_count": len(removed), "changed_count": len(changed)}
    return {"left": serialize_revision(left), "right": serialize_revision(right), "diff": diff}
