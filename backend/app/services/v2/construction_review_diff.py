"""Safe, deterministic schema diff previews for Dataset Construction review."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


CONSTRUCTION_SCHEMA_DIFF_VERSION = "construction-schema-diff-v1"
CONSTRUCTION_IMPACT_PREVIEW_VERSION = "construction-impact-preview-v2"
_DETAIL_LIMIT = 100


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _resource_key(kind: str, resource_id: Any) -> str:
    value = str(resource_id or "")
    marker = {
        "entity_types": ":type:",
        "relationships": ":relation:",
        "logic_rules": ":rule:",
    }[kind]
    if marker in value:
        return value.split(marker, 1)[1]
    return f"legacy:{value}"


def _schema_rows(snapshot: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    key = {
        "entity_types": "entities",
        "relationships": "relations",
        "logic_rules": "logic_rules",
    }[kind]
    value = snapshot.get(key)
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def schema_snapshot_digest(snapshot: dict[str, Any] | None) -> str:
    """Digest the schema plane while ignoring IDs scoped to one ontology and instance counts."""
    value = snapshot if isinstance(snapshot, dict) else {}
    schema: dict[str, list[dict[str, Any]]] = {}
    for field in ("entities", "relations", "logic_rules", "actions"):
        rows = value.get(field)
        safe_rows: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = deepcopy(row)
                if field == "entities":
                    item["id"] = _resource_key("entity_types", item.get("id"))
                    item.pop("instance_count", None)
                    item.pop("instance_samples", None)
                elif field == "relations":
                    item["id"] = _resource_key("relationships", item.get("id"))
                    item["source_entity"] = _resource_key("entity_types", item.get("source_entity"))
                    item["target_entity"] = _resource_key("entity_types", item.get("target_entity"))
                elif field == "logic_rules":
                    item["id"] = _resource_key("logic_rules", item.get("id"))
                    linked = item.get("linked_entities")
                    if isinstance(linked, list):
                        item["linked_entities"] = sorted(
                            _resource_key("entity_types", linked_id) for linked_id in linked
                        )
                safe_rows.append(item)
        schema[field] = sorted(safe_rows, key=lambda row: str(row.get("id") or ""))
    return _digest(schema)


def live_schema_snapshot(db: Any, ontology_id: str) -> dict[str, Any]:
    """Read current schema rows without loading instance payloads or sample values."""
    from app.models.action import Action
    from app.models.entity import Entity
    from app.models.logic import LogicRule
    from app.models.relation import Relation

    all_entities = db.query(Entity).filter(Entity.ontology_id == ontology_id).order_by(Entity.id.asc()).all()
    entities = [item for item in all_entities if not item.is_source_deleted]
    names = {item.id: item.name_cn for item in entities}
    relations = db.query(Relation).filter(Relation.ontology_id == ontology_id).order_by(Relation.id.asc()).all()
    rules = db.query(LogicRule).filter(LogicRule.ontology_id == ontology_id).order_by(LogicRule.id.asc()).all()
    actions = db.query(Action).filter(Action.ontology_id == ontology_id).order_by(Action.id.asc()).all()
    return {
        "entities": [
            {
                "id": item.id, "name_cn": item.name_cn, "name_en": item.name_en,
                "name_abbr": item.name_abbr, "snomed_id": item.snomed_id,
                "canonical_id": item.canonical_id, "type": item.type,
                "description": item.description, "properties": item.properties or {},
                "confidence": item.confidence, "version": item.version,
                "instance_count": 0, "instance_samples": [],
            }
            for item in entities
        ],
        "relations": [
            {
                "id": item.id, "source_entity": item.source_entity,
                "target_entity": item.target_entity,
                "source_name": names.get(item.source_entity),
                "target_name": names.get(item.target_entity), "type": item.type,
                "properties": item.properties or {}, "confidence": item.confidence,
            }
            for item in relations
            if item.source_entity in names and item.target_entity in names
        ],
        "logic_rules": [
            {
                "id": item.id, "name_cn": item.name_cn, "name_en": item.name_en,
                "description": item.description, "formula": item.formula,
                "linked_entities": item.linked_entities or [],
                "condition": getattr(item, "condition_json", {}),
                "effect": getattr(item, "effect_json", {}),
                "evidence": getattr(item, "evidence_json", {}),
                "model_invocation_id": getattr(item, "model_invocation_id", None),
                "revision_id": getattr(item, "revision_id", None),
                "confidence": item.confidence, "version": item.version,
                "enabled": bool(item.enabled), "status": item.status,
            }
            for item in rules
        ],
        "actions": [
            {
                "id": item.id, "name_cn": item.name_cn, "name_en": item.name_en,
                "description": item.description, "execution_rule": item.execution_rule,
                "function_code": item.function_code, "linked_entities": item.linked_entities or [],
                "linked_logic_ids": item.linked_logic_ids or [], "confidence": item.confidence,
                "version": item.version, "enabled": bool(item.enabled), "status": item.status,
            }
            for item in actions
        ],
    }


def _stored_property_summary(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {"name": str(value)}
    allowed = (
        "id", "name", "label", "type", "isIdentifier", "is_identifier", "unit",
        "description", "source_field", "is_required", "confidence",
    )
    summary = {key: item.get(key) for key in allowed if key in item}
    if "evidence" in item:
        summary["evidence_digest"] = _digest(item.get("evidence"))
    if "values" in item:
        summary["values_digest"] = _digest(item.get("values"))
    return summary


def _entity_property_definitions(entity: dict[str, Any]) -> list[dict[str, Any]]:
    properties = entity.get("properties")
    if not isinstance(properties, dict):
        return []
    definitions = properties.get("property_definitions")
    if isinstance(definitions, list):
        return sorted(
            (_stored_property_summary(item) for item in definitions),
            key=lambda item: str(item.get("id") or item.get("name") or ""),
        )
    # Legacy schemas may encode property names as object keys. Expose names,
    # never their stored values, in reviewer previews.
    reserved = {"schema_version", "source_fields", "color", "icon", "evidence", "data_class"}
    return [{"name": key, "type": "legacy"} for key in sorted(properties) if key not in reserved]


def _entity_summary(row: dict[str, Any]) -> dict[str, Any]:
    properties = row.get("properties") if isinstance(row.get("properties"), dict) else {}
    return {
        "key": _resource_key("entity_types", row.get("id")),
        "name": row.get("name_cn") or row.get("name_en") or row.get("id"),
        "api_name": row.get("name_en") or row.get("canonical_id") or row.get("id"),
        "description": row.get("description"),
        "entity_type": row.get("type"),
        "confidence": row.get("confidence"),
        "source_fields": sorted(str(value) for value in properties.get("source_fields", []) if value is not None)
        if isinstance(properties.get("source_fields"), list) else [],
        "properties": _entity_property_definitions(row),
    }


def _relation_summary(row: dict[str, Any], entity_names: dict[str, str]) -> dict[str, Any]:
    properties = row.get("properties") if isinstance(row.get("properties"), dict) else {}
    source_key = _resource_key("entity_types", row.get("source_entity"))
    target_key = _resource_key("entity_types", row.get("target_entity"))
    attributes = properties.get("attributes")
    if not isinstance(attributes, list):
        attributes = []
    safe_attributes = [
        {key: item.get(key) for key in ("id", "name", "label", "type", "unit") if key in item}
        for item in attributes if isinstance(item, dict)
    ]
    return {
        "key": _resource_key("relationships", row.get("id")),
        "name": properties.get("name") or row.get("type") or row.get("id"),
        "from": {"key": source_key, "name": entity_names.get(source_key) or row.get("source_name")},
        "to": {"key": target_key, "name": entity_names.get(target_key) or row.get("target_name")},
        "cardinality": properties.get("cardinality"),
        "description": properties.get("description"),
        "confidence": row.get("confidence"),
        "source_fields": sorted(str(value) for value in properties.get("source_fields", []) if value is not None)
        if isinstance(properties.get("source_fields"), list) else [],
        "attributes": safe_attributes,
        "attributes_digest": _digest(attributes),
        "evidence_digest": _digest(properties.get("evidence")),
    }


def _rule_summary(row: dict[str, Any], entity_names: dict[str, str]) -> dict[str, Any]:
    linked = row.get("linked_entities")
    linked = linked if isinstance(linked, list) else []
    linked_entities = []
    for value in linked:
        key = _resource_key("entity_types", value)
        linked_entities.append({"key": key, "name": entity_names.get(key) or key})
    return {
        "key": _resource_key("logic_rules", row.get("id")),
        "name": row.get("name_cn") or row.get("name_en") or row.get("id"),
        "description": row.get("description"),
        "formula": row.get("formula"),
        "condition": row.get("condition") or {},
        "effect": row.get("effect") or {},
        "linked_entities": sorted(linked_entities, key=lambda item: item["key"]),
        "enabled": bool(row.get("enabled")),
        "status": row.get("status"),
        "confidence": row.get("confidence"),
        "evidence_digest": _digest(row.get("evidence")),
    }


def _summaries(snapshot: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    rows = _schema_rows(snapshot, kind)
    entity_names = {
        _resource_key("entity_types", row.get("id")): str(row.get("name_cn") or row.get("name_en") or row.get("id"))
        for row in _schema_rows(snapshot, "entity_types")
    }
    summary = {
        "entity_types": _entity_summary,
        "relationships": lambda row: _relation_summary(row, entity_names),
        "logic_rules": lambda row: _rule_summary(row, entity_names),
    }[kind]
    return {str(item["key"]): item for item in (summary(row) for row in rows) if item.get("key")}


def _property_changes(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    def key(item: dict[str, Any]) -> str:
        return str(item.get("id") or item.get("name") or "")

    left = {key(item): item for item in before}
    right = {key(item): item for item in after}
    added = [right[item] for item in sorted(right.keys() - left.keys())]
    removed = [left[item] for item in sorted(left.keys() - right.keys())]
    changed = [
        {"key": item, "before": left[item], "after": right[item]}
        for item in sorted(left.keys() & right.keys()) if left[item] != right[item]
    ]
    return {"added": added, "changed": changed, "removed": removed}


def _diff_entries(before: dict[str, Any], after: dict[str, Any], kind: str) -> tuple[dict[str, Any], dict[str, int]]:
    left = _summaries(before, kind)
    right = _summaries(after, kind)
    added = [{"key": key, "after": right[key]} for key in sorted(right.keys() - left.keys())]
    removed = [{"key": key, "before": left[key]} for key in sorted(left.keys() - right.keys())]
    changed: list[dict[str, Any]] = []
    unchanged_count = 0
    for key in sorted(left.keys() & right.keys()):
        old, new = left[key], right[key]
        if old == new:
            unchanged_count += 1
            continue
        changed_fields = [field for field in sorted(set(old) | set(new)) if old.get(field) != new.get(field)]
        entry: dict[str, Any] = {"key": key, "name": new.get("name") or old.get("name"), "changed_fields": changed_fields}
        entry["before"] = old
        entry["after"] = new
        if kind == "entity_types" and "properties" in changed_fields:
            entry["property_changes"] = _property_changes(old.get("properties", []), new.get("properties", []))
        changed.append(entry)
    counts = {"added": len(added), "changed": len(changed), "removed": len(removed), "unchanged": unchanged_count}
    all_entries = {"added": added, "changed": changed, "removed": removed}
    return all_entries, counts


def _candidate_entity_row(definition: dict[str, Any], properties: list[dict[str, Any]], *, ontology_id: str, data_class: str, existing: dict[str, Any] | None) -> dict[str, Any]:
    stored_properties = [
        {
            "id": prop["id"], "name": prop["name"], "label": prop["label"], "type": prop["type"],
            "isIdentifier": prop["is_identifier"], "unit": prop["unit"], "values": prop["values"],
            "description": prop["description"], "source_field": prop["source_field"], "confidence": prop["confidence"],
            "evidence": prop["evidence"],
        }
        for prop in properties
    ]
    properties_json = {
        "schema_version": "ontology-mapping-v2",
        "property_definitions": stored_properties,
        "source_fields": definition["source_fields"],
        "color": definition.get("color"),
        "icon": definition.get("icon"),
        "evidence": definition.get("evidence") or {},
        "data_class": data_class,
    }
    entity_id = f"ontology:{ontology_id}:type:{definition['id']}"
    row = {
        "id": entity_id,
        "name_cn": definition["name_cn"],
        "name_en": definition.get("name_en"),
        "name_abbr": existing.get("name_abbr") if existing else (definition.get("name_en") or definition["id"])[:12],
        "snomed_id": existing.get("snomed_id") if existing else None,
        "canonical_id": existing.get("canonical_id") if existing else f"ontology:{definition['id']}",
        "type": "EntityType",
        "description": definition.get("description") or None,
        "properties": properties_json,
        "confidence": definition["confidence"],
        "version": "v2",
        "instance_count": existing.get("instance_count", 0) if existing else 0,
        "instance_samples": existing.get("instance_samples", []) if existing else [],
    }
    return row


def project_mapping_schema(
    base_snapshot: dict[str, Any] | None,
    mapping: dict[str, Any],
    *,
    ontology_id: str | None,
    data_class: str,
) -> dict[str, Any]:
    """Apply the Construction materializer's schema-only writes to a snapshot."""
    snapshot = deepcopy(base_snapshot) if isinstance(base_snapshot, dict) else {}
    for key in ("entities", "relations", "logic_rules", "actions"):
        if not isinstance(snapshot.get(key), list):
            snapshot[key] = []
    scope_id = str(ontology_id or "candidate")
    entity_ids = {item["id"]: f"ontology:{scope_id}:type:{item['id']}" for item in mapping.get("entity_types", [])}
    entity_rows = {str(item.get("id")): item for item in snapshot["entities"] if isinstance(item, dict) and item.get("id")}
    properties_by_entity: dict[str, list[dict[str, Any]]] = {}
    for prop in mapping.get("properties", []):
        properties_by_entity.setdefault(prop["entity_type"], []).append(prop)
    for definition in mapping.get("entity_types", []):
        entity_id = entity_ids[definition["id"]]
        existing = entity_rows.get(entity_id)
        row = _candidate_entity_row(
            definition, properties_by_entity.get(definition["id"], []),
            ontology_id=scope_id, data_class=data_class, existing=existing,
        )
        entity_rows[entity_id] = row
    snapshot["entities"] = list(entity_rows.values())

    relation_rows = {str(item.get("id")): item for item in snapshot["relations"] if isinstance(item, dict) and item.get("id")}
    for definition in mapping.get("relationships", []):
        relation_id = f"ontology:{scope_id}:relation:{definition['id']}:{definition['from']}:{definition['to']}"
        relation_rows[relation_id] = {
            "id": relation_id,
            "source_entity": entity_ids[definition["from"]],
            "target_entity": entity_ids[definition["to"]],
            "source_name": next((item["name_cn"] for item in mapping.get("entity_types", []) if item["id"] == definition["from"]), None),
            "target_name": next((item["name_cn"] for item in mapping.get("entity_types", []) if item["id"] == definition["to"]), None),
            "type": definition["name"],
            "properties": {
                "schema_version": "ontology-mapping-v2", "name": definition["name"],
                "description": definition.get("description") or "", "cardinality": definition["cardinality"],
                "cardinality_raw": definition.get("cardinality_raw"),
                "optional_from": bool(definition.get("optional_from")),
                "optional_to": bool(definition.get("optional_to")),
                "attributes": definition.get("attributes") or [], "source_fields": definition.get("source_fields") or [],
                "evidence": definition.get("evidence") or {}, "data_class": data_class,
            },
            "confidence": definition["confidence"],
        }
    snapshot["relations"] = list(relation_rows.values())

    rule_rows = {str(item.get("id")): item for item in snapshot["logic_rules"] if isinstance(item, dict) and item.get("id")}
    for definition in mapping.get("logic_rules", []):
        rule_id = f"ontology:{scope_id}:rule:{definition['id']}"
        rule_rows[rule_id] = {
            "id": rule_id,
            "name_cn": definition["name_cn"],
            "name_en": definition.get("name_en"),
            "description": definition.get("description") or None,
            "formula": definition["formula"],
            "linked_entities": [entity_ids[item] for item in definition["linked_entities"] if item in entity_ids],
            "condition": definition["condition"],
            "effect": definition["effect"],
            "evidence": definition["evidence"],
            "model_invocation_id": None,
            "revision_id": None,
            "confidence": definition["confidence"],
            "version": "v2",
            "enabled": bool(existing.get("enabled")) if existing else True,
            "status": "published",
            "model_invocation_id": None,
            "revision_id": existing.get("revision_id") if existing else None,
        }
    snapshot["logic_rules"] = list(rule_rows.values())
    return snapshot


def schema_diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    left = before if isinstance(before, dict) else {}
    right = after if isinstance(after, dict) else {}
    kinds = ("entity_types", "relationships", "logic_rules")
    full_entries: dict[str, dict[str, Any]] = {}
    counts: dict[str, dict[str, int]] = {}
    current_counts: dict[str, int] = {}
    proposed_counts: dict[str, int] = {}
    for kind in kinds:
        full_entries[kind], counts[kind] = _diff_entries(left, right, kind)
        current_counts[kind] = len(_summaries(left, kind))
        proposed_counts[kind] = len(_summaries(right, kind))
    current_counts["properties"] = sum(len(item.get("properties") or []) for item in _summaries(left, "entity_types").values())
    proposed_counts["properties"] = sum(len(item.get("properties") or []) for item in _summaries(right, "entity_types").values())
    digest_payload = {"version": CONSTRUCTION_SCHEMA_DIFF_VERSION, "counts": counts, "entries": full_entries}
    digest = _digest(digest_payload)
    entries: dict[str, Any] = {}
    for kind, groups in full_entries.items():
        entries[kind] = {name: rows[:_DETAIL_LIMIT] for name, rows in groups.items()}
        entries[kind]["omitted"] = {name: max(0, len(rows) - _DETAIL_LIMIT) for name, rows in groups.items()}
    return {
        "schema_version": CONSTRUCTION_SCHEMA_DIFF_VERSION,
        "basis": "schema_baseline",
        "digest": digest,
        "counts": counts,
        "current_counts": current_counts,
        "proposed_counts": proposed_counts,
        "entries": entries,
        "detail_limit_per_change_type": _DETAIL_LIMIT,
    }


def candidate_schema_diff(
    base_snapshot: dict[str, Any] | None,
    mapping: dict[str, Any],
    *,
    ontology_id: str | None,
    data_class: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    projected = project_mapping_schema(
        base_snapshot, mapping, ontology_id=ontology_id, data_class=data_class,
    )
    diff = schema_diff(base_snapshot, projected)
    diff["projected_schema_digest"] = schema_snapshot_digest(projected)
    diff["base_schema_digest"] = schema_snapshot_digest(base_snapshot)
    return projected, diff


def _snapshot_query_metadata_digest(snapshot: dict[str, Any] | None) -> str:
    """Build the same query metadata fingerprint as the live SQL adapter."""
    from app.services.v2.object_query.core import metadata_digest
    from app.services.v2.object_query.metadata import (
        ORDERED_TYPES,
        NUMERIC_TYPES,
        LinkMetadata,
        OntologyMetadata,
        PropertyMetadata,
        TypeMetadata,
        normalize_data_type,
    )

    value = snapshot if isinstance(snapshot, dict) else {}
    entities = _schema_rows(value, "entity_types")
    api_names: dict[str, str] = {}
    types: list[TypeMetadata] = []
    for entity in entities:
        entity_id = str(entity.get("id") or "")
        if not entity_id:
            continue
        raw_properties = entity.get("properties")
        properties = raw_properties if isinstance(raw_properties, dict) else {}
        canonical_id = str(entity.get("canonical_id") or "")
        if properties.get("is_instance") or canonical_id.startswith("instance:"):
            continue
        if canonical_id.startswith(("ontology:", "concept:")):
            api_name = canonical_id.split(":", 1)[1]
        elif entity.get("name_en"):
            api_name = str(entity["name_en"])
        elif entity.get("type") and entity.get("type") != "EntityType":
            api_name = str(entity["type"])
        else:
            api_name = str(entity.get("name_cn") or entity_id)
        api_names[entity_id] = api_name

        definitions = properties.get("property_definitions") or properties.get("properties")
        if isinstance(raw_properties, list):
            definitions = raw_properties
        if not isinstance(definitions, list):
            reserved = {"schema_version", "source_fields", "evidence", "color", "icon", "data_class", "is_concept", "source"}
            definitions = [
                {"id": str(key), "name": str(key), "type": item if isinstance(item, str) else "string"}
                for key, item in properties.items() if key not in reserved
            ]

        query_properties: dict[str, PropertyMetadata] = {
            "_instance_id": PropertyMetadata("_instance_id", "string", False, True, True, False),
        }
        for raw in definitions:
            if not isinstance(raw, dict):
                continue
            prop_name = str(raw.get("id") or raw.get("name") or raw.get("api_name") or raw.get("source_field") or "").strip()
            if not prop_name:
                continue
            data_type = normalize_data_type(raw.get("type") or raw.get("data_type"))
            is_array = data_type == "array" or bool(raw.get("is_array"))
            query_properties[prop_name] = PropertyMetadata(
                api_name=prop_name,
                data_type=data_type,
                nullable=bool(raw.get("nullable", True)),
                searchable=bool(raw.get("searchable", True)),
                sortable=bool(raw.get("sortable", data_type in ORDERED_TYPES)),
                aggregatable=bool(raw.get("aggregatable", data_type in ORDERED_TYPES or data_type in NUMERIC_TYPES or data_type == "boolean")),
                is_array=is_array,
            )
        types.append(TypeMetadata(api_name=api_name, properties=query_properties))

    # Materialized instance rows resolve to their owning object type, just as
    # load_sql_metadata does, so relations attached to instances are mapped
    # with the same endpoint names as the live catalog.
    for entity in entities:
        entity_id = str(entity.get("id") or "")
        properties = entity.get("properties") if isinstance(entity.get("properties"), dict) else {}
        canonical_id = str(entity.get("canonical_id") or "")
        if properties.get("is_instance") or canonical_id.startswith("instance:"):
            concept_type = api_names.get(str(properties.get("concept_id") or ""))
            if entity_id and concept_type:
                api_names[entity_id] = concept_type

    links: list[LinkMetadata] = []
    for relation in _schema_rows(value, "relationships"):
        source = api_names.get(str(relation.get("source_entity") or ""))
        target = api_names.get(str(relation.get("target_entity") or ""))
        if source is None or target is None:
            continue
        properties = relation.get("properties") if isinstance(relation.get("properties"), dict) else {}
        link = LinkMetadata(
            api_name=str(properties.get("api_name") or properties.get("name") or relation.get("type") or relation.get("id")),
            source_type=source,
            target_type=target,
            cardinality=str(properties.get("cardinality") or "many"),
        )
        if link not in links:
            links.append(link)
    return metadata_digest(OntologyMetadata(types, links))


def _reference_tokens(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        return {token for item in value for token in _reference_tokens(item)}
    token = str(value).strip().casefold()
    return {token} if token else set()


def _entity_reference_tokens(row: dict[str, Any]) -> set[str]:
    values = [row.get("id"), _resource_key("entity_types", row.get("id"))]
    values.extend(row.get(key) for key in ("name_cn", "name_en", "name_abbr", "canonical_id"))
    canonical_id = str(row.get("canonical_id") or "")
    if canonical_id.startswith(("ontology:", "concept:")):
        values.append(canonical_id.split(":", 1)[1])
    if row.get("name_en"):
        values.append(row.get("name_en"))
    return _reference_tokens(values)


def _rule_reference_tokens(row: dict[str, Any]) -> set[str]:
    values = [row.get("id"), _resource_key("logic_rules", row.get("id"))]
    values.extend(row.get(key) for key in ("name_cn", "name_en"))
    return _reference_tokens(values)


def _query_row_identity_digest(query: Any, *order_by: Any) -> tuple[int, str]:
    """Hash an ordered query result without retaining the raw identifiers."""
    digest = hashlib.sha256()
    count = 0
    for row in query.order_by(*order_by).yield_per(1000):
        encoded = json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str)
        digest.update(encoded.encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def construction_impact_preview(
    db: Any,
    ontology_id: str | None,
    before_snapshot: dict[str, Any] | None,
    after_snapshot: dict[str, Any] | None,
    diff: dict[str, Any],
) -> dict[str, Any]:
    """Summarize exact schema-linked rules/actions and metadata-bound query resources.

    Query resource names, definitions, function bodies, instance values, and
    query payloads are deliberately omitted. Counts cover only resources
    bound to the currently published query metadata and still usable today.
    """
    before = before_snapshot if isinstance(before_snapshot, dict) else {}
    after = after_snapshot if isinstance(after_snapshot, dict) else {}
    entries = diff.get("entries") if isinstance(diff.get("entries"), dict) else {}

    changed_entity_keys: set[str] = set()
    changed_entity_rows: list[dict[str, Any]] = []
    for change in ("added", "changed", "removed"):
        for item in (entries.get("entity_types") or {}).get(change, []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if key:
                changed_entity_keys.add(key)
            for row in (item.get("before"), item.get("after")):
                if isinstance(row, dict):
                    changed_entity_rows.append(row)

    for change in ("added", "changed", "removed"):
        for item in (entries.get("relationships") or {}).get(change, []):
            if not isinstance(item, dict):
                continue
            for row in (item.get("before"), item.get("after")):
                if isinstance(row, dict):
                    for endpoint in (row.get("from"), row.get("to")):
                        if isinstance(endpoint, dict):
                            endpoint_key = str(endpoint.get("key") or "")
                            if endpoint_key:
                                changed_entity_keys.add(endpoint_key)
                            changed_entity_rows.append({
                                "id": endpoint_key,
                                "name_cn": endpoint.get("name"),
                            })

    entity_rows = [row for row in _schema_rows(before, "entity_types") + _schema_rows(after, "entity_types")]
    changed_entity_tokens: set[str] = set()
    changed_entity_names: dict[str, str] = {}
    for row in entity_rows:
        key = _resource_key("entity_types", row.get("id"))
        if key in changed_entity_keys or str(row.get("id") or "") in changed_entity_keys:
            changed_entity_tokens.update(_entity_reference_tokens(row))
            changed_entity_names[key] = str(row.get("name_cn") or row.get("name_en") or key)
    for row in changed_entity_rows:
        changed_entity_tokens.update(_entity_reference_tokens(row))
        key = str(row.get("key") or row.get("id") or "")
        if key:
            changed_entity_names.setdefault(key, str(row.get("name") or row.get("name_cn") or key))

    changed_rule_keys: set[str] = set()
    changed_rule_tokens: set[str] = set()
    for change in ("added", "changed", "removed"):
        for item in (entries.get("logic_rules") or {}).get(change, []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if key:
                changed_rule_keys.add(key)
            for row in (item.get("before"), item.get("after")):
                if isinstance(row, dict):
                    changed_rule_tokens.update(_rule_reference_tokens(row))

    rule_rows = _schema_rows(before, "logic_rules") + _schema_rows(after, "logic_rules")
    dependent_rules: dict[str, dict[str, Any]] = {}
    for row in rule_rows:
        linked = _reference_tokens(row.get("linked_entities"))
        if not changed_entity_tokens.intersection(linked):
            continue
        rule_id = str(row.get("id") or "")
        key = _resource_key("logic_rules", rule_id)
        tokens = _rule_reference_tokens(row)
        changed_rule_tokens.update(tokens)
        dependent_rules[key or rule_id] = {
            "id": rule_id,
            "name": row.get("name_cn") or row.get("name_en") or key or rule_id,
            "reason": "linked_entity_schema_changed",
        }
    changed_rule_entity_tokens: set[str] = set()
    for row in rule_rows:
        if _resource_key("logic_rules", row.get("id")) in changed_rule_keys:
            changed_rule_tokens.update(_rule_reference_tokens(row))
            # The ontology UI also treats actions that share an entity link
            # with a rule as related, even when linked_logic_ids is absent.
            changed_rule_entity_tokens.update(_reference_tokens(row.get("linked_entities")))

    action_rows = _schema_rows(before, "actions") + _schema_rows(after, "actions")
    affected_actions: dict[str, dict[str, Any]] = {}
    for row in action_rows:
        reasons: set[str] = set()
        if changed_entity_tokens.intersection(_reference_tokens(row.get("linked_entities"))):
            reasons.add("linked_entity_schema_changed")
        if changed_rule_tokens.intersection(_reference_tokens(row.get("linked_logic_ids"))):
            reasons.add("linked_logic_rule_changed")
        if changed_rule_entity_tokens.intersection(_reference_tokens(row.get("linked_entities"))):
            reasons.add("shares_entity_with_changed_logic_rule")
        if not reasons:
            continue
        action_id = str(row.get("id") or "")
        existing_action = affected_actions.get(action_id)
        merged_reasons = set(reasons)
        if existing_action:
            merged_reasons.update(existing_action.get("reasons") or [])
        affected_actions[action_id] = {
            "id": action_id,
            "name": row.get("name_cn") or row.get("name_en") or action_id,
            "reasons": sorted(merged_reasons),
        }

    metadata_before_digest = _snapshot_query_metadata_digest(before)
    metadata_after_digest = _snapshot_query_metadata_digest(after)
    metadata_changed = metadata_before_digest != metadata_after_digest
    schema_before_digest = schema_snapshot_digest(before)
    schema_after_digest = schema_snapshot_digest(after)
    schema_changed = schema_before_digest != schema_after_digest
    bound_counts = {"object_sets": 0, "object_views": 0, "query_data_views": 0}
    scenario_counts = {
        "scenario_resources": 0,
        "scenario_studies": 0,
        "scenario_run_artifacts": 0,
        "scenario_run_results": 0,
        "what_if_scenarios": 0,
        "what_if_runs": 0,
        "decision_cases": 0,
        "agent_context_items": 0,
        "agent_sessions": 0,
        "agent_runs": 0,
    }
    inventory_identity_digests: dict[str, str] = {}
    inventory_status = "no_existing_target" if not ontology_id else "unchanged"
    if ontology_id and metadata_changed:
        from datetime import datetime, timezone
        from sqlalchemy import and_, func, or_
        from app.models.v2.object_set import ObjectSetResource, ObjectSetVersion
        from app.models.v2.object_view import ObjectViewConfiguration
        from app.models.v2.query_view import QueryDataView
        from app.services.v2.object_query.data_views import retention_pin_reason

        now = datetime.now(timezone.utc)
        object_set_query = db.query(
            ObjectSetResource.id,
            ObjectSetResource.head_version,
            ObjectSetVersion.definition_hash,
            ObjectSetVersion.metadata_digest,
        ).join(
            ObjectSetVersion,
            (ObjectSetVersion.resource_id == ObjectSetResource.id)
            & (ObjectSetVersion.version == ObjectSetResource.head_version),
        ).filter(
            ObjectSetResource.ontology_id == ontology_id,
            ObjectSetResource.status == "active",
            or_(ObjectSetResource.expires_at.is_(None), ObjectSetResource.expires_at > now),
            ObjectSetVersion.metadata_digest == metadata_before_digest,
        )
        bound_counts["object_sets"], inventory_identity_digests["object_sets"] = _query_row_identity_digest(
            object_set_query, ObjectSetResource.id,
        )

        latest_views = db.query(
            ObjectViewConfiguration.object_type.label("object_type"),
            func.max(ObjectViewConfiguration.version).label("version"),
        ).filter(
            ObjectViewConfiguration.ontology_id == ontology_id,
        ).group_by(ObjectViewConfiguration.object_type).subquery()
        object_view_query = db.query(
            ObjectViewConfiguration.id,
            ObjectViewConfiguration.object_type,
            ObjectViewConfiguration.version,
            ObjectViewConfiguration.metadata_digest,
        ).join(
            latest_views,
            and_(
                latest_views.c.object_type == ObjectViewConfiguration.object_type,
                latest_views.c.version == ObjectViewConfiguration.version,
            ),
        ).filter(
            ObjectViewConfiguration.ontology_id == ontology_id,
            ObjectViewConfiguration.metadata_digest == metadata_before_digest,
        )
        bound_counts["object_views"], inventory_identity_digests["object_views"] = _query_row_identity_digest(
            object_view_query, ObjectViewConfiguration.object_type,
        )

        from app.models.v2.scenario import (
            ScenarioCheckpoint,
            ScenarioResource,
            ScenarioRevision,
            ScenarioRun,
            ScenarioRunArtifact,
            ScenarioStudy,
        )
        from app.services.v2.scenarios import scenario_is_expired

        retained_query = db.query(QueryDataView).filter(
            QueryDataView.ontology_id == ontology_id,
            QueryDataView.status == "ready",
            QueryDataView.metadata_digest == metadata_before_digest,
        )
        retained_query_rows = retained_query.with_entities(
            QueryDataView.id,
            QueryDataView.source_manifest_digest,
            QueryDataView.content_digest,
            QueryDataView.metadata_digest,
        ).filter(
            or_(QueryDataView.retention_until.is_(None), QueryDataView.retention_until > now),
        )
        retained_digest_count, retained_digest = _query_row_identity_digest(retained_query_rows, QueryDataView.id)
        expired_query_views = retained_query.filter(QueryDataView.retention_until <= now).all()
        pinned_expired = [
            row for row in expired_query_views if retention_pin_reason(db, row, at=now) is not None
        ]
        pinned_expired_digest = _digest(sorted(
            (row.id, row.source_manifest_digest, row.content_digest, row.metadata_digest)
            for row in pinned_expired
        ))
        bound_counts["query_data_views"] = retained_digest_count + len(pinned_expired)
        inventory_identity_digests["query_data_views"] = _digest({
            "retained": retained_digest,
            "pinned_expired": pinned_expired_digest,
        })

        stale_view_ids = db.query(QueryDataView.id).filter(
            QueryDataView.ontology_id == ontology_id,
            QueryDataView.status == "ready",
            QueryDataView.metadata_digest == metadata_before_digest,
        ).subquery()
        active_scenarios = [
            row for row in db.query(ScenarioResource).filter(
                ScenarioResource.ontology_id == ontology_id,
                ScenarioResource.status == "active",
            ).all()
            if not scenario_is_expired(row, at=now)
        ]
        active_scenario_ids = [row.id for row in active_scenarios]
        scenario_view_refs: set[tuple[str, str, str]] = set()
        if active_scenario_ids:
            for scenario_id, view_id in db.query(
                ScenarioResource.id, ScenarioResource.base_view_id,
            ).filter(
                ScenarioResource.id.in_(active_scenario_ids),
                ScenarioResource.base_view_id.in_(stale_view_ids),
            ).all():
                scenario_view_refs.add(("base", scenario_id, view_id))
            for scenario_id, view_id in db.query(
                ScenarioRevision.scenario_id, ScenarioRevision.result_view_id,
            ).filter(
                ScenarioRevision.scenario_id.in_(active_scenario_ids),
                ScenarioRevision.status == "ready",
                ScenarioRevision.result_view_id.in_(stale_view_ids),
            ).all():
                scenario_view_refs.add(("revision", scenario_id, view_id))
            for scenario_id, view_id in db.query(
                ScenarioCheckpoint.scenario_id, ScenarioCheckpoint.result_view_id,
            ).filter(
                ScenarioCheckpoint.scenario_id.in_(active_scenario_ids),
                ScenarioCheckpoint.result_view_id.in_(stale_view_ids),
            ).all():
                scenario_view_refs.add(("checkpoint", scenario_id, view_id))
        scenario_ids = {scenario_id for _kind, scenario_id, _view_id in scenario_view_refs}
        scenario_counts["scenario_resources"] = len(scenario_ids)
        inventory_identity_digests["scenario_resources"] = _digest(sorted(scenario_view_refs))

        study_rows = db.query(ScenarioStudy.id, ScenarioStudy.base_view_id).filter(
            ScenarioStudy.ontology_id == ontology_id,
            ScenarioStudy.base_view_id.in_(stale_view_ids),
        ).order_by(ScenarioStudy.id).all()
        scenario_counts["scenario_studies"] = len(study_rows)
        inventory_identity_digests["scenario_studies"] = _digest([list(row) for row in study_rows])

        artifact_rows = db.query(
            ScenarioRunArtifact.id,
            ScenarioRunArtifact.run_id,
            ScenarioRunArtifact.data_view_id,
        ).join(
            ScenarioRun, ScenarioRun.id == ScenarioRunArtifact.run_id,
        ).filter(
            ScenarioRunArtifact.data_view_id.in_(stale_view_ids),
            ScenarioRunArtifact.data_view_id.is_not(None),
            ScenarioRun.scenario_id.in_(db.query(ScenarioResource.id).filter(
                ScenarioResource.ontology_id == ontology_id,
            )),
        ).order_by(ScenarioRunArtifact.id).all()
        scenario_counts["scenario_run_artifacts"] = len(artifact_rows)
        inventory_identity_digests["scenario_run_artifacts"] = _digest([list(row) for row in artifact_rows])

        run_rows = db.query(ScenarioRun.id, ScenarioRun.result_view_id).join(
            ScenarioResource, ScenarioResource.id == ScenarioRun.scenario_id,
        ).filter(
            ScenarioResource.ontology_id == ontology_id,
            ScenarioRun.result_view_id.in_(stale_view_ids),
        ).order_by(ScenarioRun.id).all()
        scenario_counts["scenario_run_results"] = len(run_rows)
        inventory_identity_digests["scenario_run_results"] = _digest([list(row) for row in run_rows])
        inventory_status = "will_require_revalidation"

    if ontology_id and schema_changed:
        from app.models.ontology import OntologyProject
        from app.models.ontology_revision import OntologyRevision
        from app.models.v2.agent import AgentContextItem, AgentRun, AgentSession
        from app.models.v2.decision_case import DecisionCase
        from app.models.v2.dynamic_ontology import WhatIfRun, WhatIfScenario

        project = db.get(OntologyProject, ontology_id)
        current_revision_id = project.current_revision_id if project else None
        current_revision = None
        if current_revision_id:
            current_revision = db.query(OntologyRevision).filter(
                OntologyRevision.id == current_revision_id,
                OntologyRevision.ontology_id == ontology_id,
                OntologyRevision.is_current.is_(True),
            ).first()
        if not current_revision:
            current_revision = db.query(OntologyRevision).filter(
                OntologyRevision.ontology_id == ontology_id,
                OntologyRevision.is_current.is_(True),
            ).order_by(OntologyRevision.revision_no.desc()).first()
            current_revision_id = current_revision.id if current_revision else None

        pinned_what_if_scenarios: list[tuple[str, str, str, str, str]] = []
        if current_revision_id and current_revision and current_revision.snapshot_hash:
            for scenario in db.query(WhatIfScenario).filter(
                WhatIfScenario.ontology_id == ontology_id,
                WhatIfScenario.base_revision_id == current_revision_id,
            ).order_by(WhatIfScenario.id).yield_per(500):
                baseline = scenario.baseline_json if isinstance(scenario.baseline_json, dict) else {}
                context = baseline.get("scenario_context")
                if not isinstance(context, dict):
                    continue
                if (str(context.get("schema_revision_id") or "") != str(current_revision_id)
                        or str(context.get("schema_revision_hash") or "") != str(current_revision.snapshot_hash)):
                    continue
                pinned_what_if_scenarios.append((
                    str(scenario.id),
                    str(context.get("context_digest") or ""),
                    str(context.get("permission_digest") or ""),
                    str(scenario.status or ""),
                    str(scenario.updated_at or ""),
                ))

        what_if_contexts = {
            scenario_id: (context_digest, permission_digest)
            for scenario_id, context_digest, permission_digest, _status, _updated_at
            in pinned_what_if_scenarios
        }
        what_if_scenario_ids = set(what_if_contexts)
        scenario_counts["what_if_scenarios"] = len(pinned_what_if_scenarios)
        inventory_identity_digests["what_if_scenarios"] = _digest(pinned_what_if_scenarios)

        what_if_run_rows: list[tuple[str, str, str, str, str]] = []
        if what_if_scenario_ids:
            ordered_scenario_ids = sorted(what_if_scenario_ids)
            for offset in range(0, len(ordered_scenario_ids), 500):
                scenario_id_chunk = ordered_scenario_ids[offset:offset + 500]
                for run_id, scenario_id, status, input_hash, updated_at in db.query(
                    WhatIfRun.id,
                    WhatIfRun.scenario_id,
                    WhatIfRun.status,
                    WhatIfRun.input_hash,
                    WhatIfRun.updated_at,
                ).filter(
                    WhatIfRun.ontology_id == ontology_id,
                    WhatIfRun.scenario_id.in_(scenario_id_chunk),
                ).order_by(WhatIfRun.id).yield_per(500):
                    what_if_run_rows.append((
                        str(run_id), str(scenario_id), str(status or ""),
                        str(input_hash or ""), str(updated_at or ""),
                    ))
        scenario_counts["what_if_runs"] = len(what_if_run_rows)
        inventory_identity_digests["what_if_runs"] = _digest(what_if_run_rows)

        decision_case_rows: list[tuple[str, str, str, str, tuple[str, ...], tuple[str, ...]]] = []
        if what_if_scenario_ids:
            for case_id, status, context_digest, action_run_id, context_json in db.query(
                DecisionCase.id,
                DecisionCase.status,
                DecisionCase.context_digest,
                DecisionCase.action_run_id,
                DecisionCase.context_json,
            ).filter(
                DecisionCase.ontology_id == ontology_id,
            ).order_by(DecisionCase.id).yield_per(500):
                context = context_json if isinstance(context_json, dict) else {}
                refs = context.get("what_if_scenarios")
                if not isinstance(refs, list):
                    continue
                matching_refs = [
                    ref for ref in refs
                    if isinstance(ref, dict)
                    and isinstance(ref.get("ref_id"), str)
                    and ref.get("ref_id") in what_if_scenario_ids
                ]
                if not matching_refs:
                    continue
                referenced_scenario_ids = tuple(sorted({str(ref["ref_id"]) for ref in matching_refs}))
                reference_digests = tuple(sorted(_digest(ref) for ref in matching_refs))
                decision_case_rows.append((
                    str(case_id),
                    str(status or ""),
                    str(context_digest or ""),
                    str(action_run_id or ""),
                    referenced_scenario_ids,
                    reference_digests,
                ))
        scenario_counts["decision_cases"] = len(decision_case_rows)
        inventory_identity_digests["decision_cases"] = _digest(decision_case_rows)

        agent_context_rows: list[tuple[str, ...]] = []
        agent_session_rows: dict[str, tuple[str, ...]] = {}
        if what_if_scenario_ids:
            for (
                item_id, session_id, ref_id, source_version, item_permission_digest,
                details, item_created_at, session_status, session_version,
                session_revision_id, session_permission_digest, pending_case_id, session_updated_at,
            ) in db.query(
                AgentContextItem.id,
                AgentContextItem.session_id,
                AgentContextItem.ref_id,
                AgentContextItem.source_version,
                AgentContextItem.permission_digest,
                AgentContextItem.details,
                AgentContextItem.created_at,
                AgentSession.status,
                AgentSession.version,
                AgentSession.metadata_revision_id,
                AgentSession.permission_digest,
                AgentSession.pending_case_id,
                AgentSession.updated_at,
            ).join(
                AgentSession, AgentSession.id == AgentContextItem.session_id,
            ).filter(
                AgentSession.ontology_id == ontology_id,
                AgentContextItem.kind == "what_if",
                AgentContextItem.active.is_(True),
            ).order_by(AgentContextItem.id).yield_per(500):
                if str(ref_id or "") not in what_if_scenario_ids:
                    continue
                agent_context_rows.append((
                    str(item_id), str(session_id), str(ref_id),
                    str(source_version or ""), str(item_permission_digest or ""),
                    _digest(details if isinstance(details, dict) else {}),
                    str(item_created_at or ""),
                ))
                agent_session_rows[str(session_id)] = (
                    str(session_id), str(session_status or ""), str(session_version or ""),
                    str(session_revision_id or ""), str(session_permission_digest or ""),
                    str(pending_case_id or ""), str(session_updated_at or ""),
                )
        agent_session_ids = sorted(agent_session_rows)
        scenario_counts["agent_context_items"] = len(agent_context_rows)
        scenario_counts["agent_sessions"] = len(agent_session_rows)
        inventory_identity_digests["agent_context_items"] = _digest(agent_context_rows)
        inventory_identity_digests["agent_sessions"] = _digest(
            [agent_session_rows[session_id] for session_id in agent_session_ids]
        )

        agent_run_rows: list[tuple[str, ...]] = []
        for offset in range(0, len(agent_session_ids), 500):
            session_id_chunk = agent_session_ids[offset:offset + 500]
            for run_id, session_id, status, input_digest, output_digest, generation, created_at, completed_at in db.query(
                AgentRun.id,
                AgentRun.session_id,
                AgentRun.status,
                AgentRun.input_digest,
                AgentRun.output_digest,
                AgentRun.lease_generation,
                AgentRun.created_at,
                AgentRun.completed_at,
            ).filter(
                AgentRun.ontology_id == ontology_id,
                AgentRun.session_id.in_(session_id_chunk),
            ).order_by(AgentRun.id).yield_per(500):
                agent_run_rows.append((
                    str(run_id), str(session_id), str(status or ""),
                    str(input_digest or ""), str(output_digest or ""),
                    str(generation or 0), str(created_at or ""), str(completed_at or ""),
                ))
        scenario_counts["agent_runs"] = len(agent_run_rows)
        inventory_identity_digests["agent_runs"] = _digest(agent_run_rows)
        inventory_status = "will_require_revalidation"

    preview: dict[str, Any] = {
        "schema_version": CONSTRUCTION_IMPACT_PREVIEW_VERSION,
        "basis": "exact_schema_references_metadata_inventory_and_pinned_context",
        "metadata": {
            "current_digest": metadata_before_digest,
            "projected_digest": metadata_after_digest,
            "changes": metadata_changed,
            "inventory_status": inventory_status,
        },
        "schema": {
            "current_digest": schema_before_digest,
            "projected_digest": schema_after_digest,
            "changes": schema_changed,
        },
        "changed_entities": {
            "count": len(changed_entity_names),
            "items": [
                {"key": key, "name": name}
                for key, name in sorted(changed_entity_names.items())[:_DETAIL_LIMIT]
            ],
            "omitted": max(0, len(changed_entity_names) - _DETAIL_LIMIT),
        },
        "dependent_logic_rules": {
            "count": len(dependent_rules),
            "items": [dependent_rules[key] for key in sorted(dependent_rules)[:_DETAIL_LIMIT]],
            "omitted": max(0, len(dependent_rules) - _DETAIL_LIMIT),
        },
        "affected_actions": {
            "count": len(affected_actions),
            "items": [affected_actions[key] for key in sorted(affected_actions)[:_DETAIL_LIMIT]],
            "omitted": max(0, len(affected_actions) - _DETAIL_LIMIT),
        },
        "metadata_bound_query_resources": {
            "status": inventory_status,
            "counts": bound_counts,
            "resource_names_included": False,
        },
        "scenario_impact": {
            "status": inventory_status if not ontology_id else (
                "will_require_revalidation" if schema_changed or metadata_changed else inventory_status
            ),
            "counts": scenario_counts,
            "resource_names_included": False,
        },
        "resource_inventory_digest": _digest(inventory_identity_digests),
        "detail_limit": _DETAIL_LIMIT,
    }
    preview["digest"] = _digest(preview)
    return preview

