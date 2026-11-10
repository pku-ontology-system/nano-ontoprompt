"""What the database knows about an ontology's object types, beyond the query catalog: property definitions, where Auto Ontology got a
type from (source table, primary key, columns), and the foreign keys relations are built on."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.entity import Entity
from app.models.relation import Relation
from app.models.v2.ontology_candidates import OntologyCandidateItem, OntologyCandidateSet
from app.services.v2.object_query.metadata import _property_definitions, _type_api_name


def type_entities(db: Session, ontology_id: str) -> dict[str, Entity]:
    """Object type api name -> its Entity row (instances and deleted entities are left out)."""
    out: dict[str, Entity] = {}
    for entity in db.query(Entity).filter(Entity.ontology_id == ontology_id, Entity.is_source_deleted.is_(False)).all():
        props = entity.properties if isinstance(entity.properties, dict) else {}
        if props.get("is_instance") or str(entity.canonical_id or "").startswith("instance:"):
            continue
        out.setdefault(_type_api_name(entity), entity)
    return out


def raw_definitions(entity: Entity | None) -> dict[str, dict]:
    """Property id -> its definition as stored (type, display name, and anything else the definition declares)."""
    if entity is None:
        return {}
    out = {}
    for definition in _property_definitions(entity):
        key = str(definition.get("id") or definition.get("name") or definition.get("api_name") or definition.get("source_field") or "").strip()
        if key:
            out[key] = definition
    return out


def candidate_evidence(db: Session, ontology_id: str) -> dict[str, dict]:
    """Type api name -> evidence of the newest accepted Auto Ontology candidate that was published for it."""
    rows = (db.query(OntologyCandidateItem, OntologyCandidateSet)
            .join(OntologyCandidateSet, OntologyCandidateItem.set_id == OntologyCandidateSet.id)
            .filter(OntologyCandidateSet.ontology_id == ontology_id, OntologyCandidateSet.status == "published", OntologyCandidateItem.kind == "entity_type",
                    OntologyCandidateItem.decision.in_(("accepted", "modified")))
            .order_by(OntologyCandidateSet.created_at.desc(), OntologyCandidateItem.ordinal).all())
    out: dict[str, dict] = {}
    for item, candidate_set in rows:
        draft = item.final or item.draft or {}
        name = draft.get("name_en") or item.name
        if name and name not in out:
            evidence = item.evidence or {}
            out[name] = {"source_table": evidence.get("source_table"), "primary_key": list(evidence.get("primary_key") or []), "columns": evidence.get("columns") or [],
                         "set_id": candidate_set.id, "set_title": candidate_set.title, "generator": candidate_set.generator or {}, "item_id": item.id}
    return out


def candidate_items(db: Session, ontology_id: str, set_id: str) -> list[OntologyCandidateItem]:
    """The entity types one candidate set proposes, in review order, without the ones a reviewer rejected."""
    return (db.query(OntologyCandidateItem).join(OntologyCandidateSet, OntologyCandidateItem.set_id == OntologyCandidateSet.id)
            .filter(OntologyCandidateSet.id == set_id, OntologyCandidateSet.ontology_id == ontology_id, OntologyCandidateItem.kind == "entity_type",
                    OntologyCandidateItem.decision != "rejected").order_by(OntologyCandidateItem.ordinal).all())


def _column(field: Any) -> tuple[str | None, str | None]:
    """'table.column' -> (table, column)."""
    if not isinstance(field, str) or "." not in field:
        return None, None
    table, column = field.rsplit(".", 1)
    return table, column


def relations(db: Session, ontology_id: str) -> list[dict]:
    """Every relation once, with the object types it connects and the table columns it was built on. Bridge tables are kept but marked."""
    names = {e.id: api for api, e in type_entities(db, ontology_id).items()}
    # Mapping also publishes row entities that point at a type; they resolve to the type they belong to.
    for entity in db.query(Entity).filter(Entity.ontology_id == ontology_id, Entity.is_source_deleted.is_(False)).all():
        props = entity.properties if isinstance(entity.properties, dict) else {}
        concept = props.get("concept_id")
        if (props.get("is_instance") or str(entity.canonical_id or "").startswith("instance:")) and concept in names:
            names[entity.id] = names[concept]
    out, seen = [], set()
    for relation in db.query(Relation).filter(Relation.ontology_id == ontology_id).all():
        source, target = names.get(relation.source_entity), names.get(relation.target_entity)
        props = relation.properties or {}
        if source is None or target is None:
            continue
        source_table, source_column = _column(props.get("source_field"))
        target_table, target_column = _column(props.get("target_field"))
        key = (relation.type, source, target, props.get("source_field"), props.get("target_field"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"relation": str(props.get("api_name") or props.get("name") or relation.type), "source_type": source, "target_type": target, "basis": props.get("basis"),
                    "cardinality": props.get("cardinality"), "via_table": props.get("via_table"), "source_table": source_table, "source_column": source_column,
                    "target_table": target_table, "target_column": target_column, "provenance": props.get("provenance") or {}})
    return out


def foreign_keys(db: Session, ontology_id: str, object_type: str) -> list[dict]:
    """The foreign keys of one type: its property, the type and property it points at, and how the key was found."""
    out, seen = [], set()
    for rel in relations(db, ontology_id):
        if rel["source_type"] != object_type or rel["basis"] == "bridge_table" or not rel["source_column"] or not rel["target_column"]:
            continue
        key = (rel["source_column"], rel["target_type"], rel["target_column"])
        if key not in seen:
            seen.add(key)
            out.append({"property": rel["source_column"], "target_type": rel["target_type"], "target_property": rel["target_column"], "basis": rel["basis"], "relation": rel["relation"]})
    return out


def source_tables(db: Session, ontology_id: str) -> dict[str, set[str]]:
    """Type api name -> the source tables it was built from: the candidate's table and the tables its relations were declared on."""
    evidence = candidate_evidence(db, ontology_id)
    tables: dict[str, set[str]] = {api: ({ev["source_table"]} if ev.get("source_table") else set()) for api, ev in evidence.items()}
    for rel in relations(db, ontology_id):
        if rel["basis"] == "bridge_table":
            continue
        if rel["source_table"]:
            tables.setdefault(rel["source_type"], set()).add(rel["source_table"])
        if rel["target_table"]:
            tables.setdefault(rel["target_type"], set()).add(rel["target_table"])
    return tables
