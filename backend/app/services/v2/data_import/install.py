"""Write an import into the platform: the ontology and its revision, then the objects and the relationship instances.

The schema goes through the Auto Ontology candidate path (see `schema.py`); the objects are written the way the platform's other
seeders write them: rows in `entity_instances` and nodes in the ontology's graph, with one graph edge per link.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

import pandas as pd
from sqlalchemy.orm import Session

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.models.relation import Relation
from app.models.user import User
from app.models.v2.ontology_candidates import OntologyCandidateItem, OntologyCandidateSet
from app.services.v2 import ontology_candidates as candidates
from app.services.v2.analysis import ontology_metadata
from app.services.v2.data_import import schema as schema_mod
from app.services.v2.data_import.model import Bundle, Link, Table
from app.services.v2.data_import.profile import key_parts, key_text, object_id, platform_type, python_value

CHUNK = 2000
Progress = Callable[[str, dict], None]


class ImportConflict(Exception):
    """An ontology with this name exists."""

    def __init__(self, ontology_id: str, name: str):
        super().__init__(f"本体“{name}”已经存在")
        self.ontology_id = ontology_id


class ImportTooLarge(ValueError):
    pass


def remove_ontology(db: Session, project: OntologyProject) -> None:
    """Delete an ontology and its graph, the way the ontology API does."""
    from app.services.v2.graph.falkordb_service import FalkorDBService

    try:
        FalkorDBService().delete_graph(project.id)
    except Exception:  # noqa: BLE001  the database stays authoritative; a missing graph is reported by the health tooling
        pass
    _drop_snapshots(db, project.id)
    db.delete(project)
    db.commit()


def _drop_snapshots(db: Session, ontology_id: str) -> None:
    """Pinned data views (the analysis pages create them to read big types) and what hangs off them reference the ontology without a cascade, so they go first.
    Anything that records a decision or a run (cases, reviews, mapping runs) is not touched: if one exists the delete is refused, and that is right."""
    from sqlalchemy import text

    from app.services.v2.graph.falkordb_service import FalkorDBService

    graph = FalkorDBService()
    views = [row[0] for row in db.execute(text("SELECT id FROM v2_query_data_views WHERE ontology_id = :o"), {"o": ontology_id})]
    for view_id in views:
        try:
            graph.delete_graph(view_id)
        except Exception:  # noqa: BLE001
            pass
    db.execute(text("DELETE FROM v2_time_series_syncs WHERE ontology_id = :o"), {"o": ontology_id})
    db.execute(text("DELETE FROM v2_query_jobs WHERE ontology_id = :o"), {"o": ontology_id})
    db.execute(text("UPDATE v2_query_data_views SET base_view_id = NULL WHERE ontology_id = :o"), {"o": ontology_id})
    db.execute(text("DELETE FROM v2_query_data_views WHERE ontology_id = :o"), {"o": ontology_id})
    db.commit()


def create_ontology(db: Session, user: User, bundle: Bundle, *, replace: bool) -> OntologyProject:
    existing = db.query(OntologyProject).filter(OntologyProject.name.ilike(bundle.ontology_name)).first()
    if existing is not None:
        if not replace:
            raise ImportConflict(existing.id, existing.name)
        remove_ontology(db, existing)
    project = OntologyProject(id=str(uuid.uuid4()), name=bundle.ontology_name, domain=bundle.domain, description=bundle.summary, build_mode="data_import", data_class="regular",
                              created_by=user.id)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def check_size(bundle: Bundle) -> None:
    items = sum(1 for t in bundle.tables if t.kind not in ("bridge", "stage")) + len(bundle.links)
    if items > candidates.MAX_PUBLISH_ITEMS:
        raise ImportTooLarge(f"一次最多导入 {candidates.MAX_PUBLISH_ITEMS} 个对象类型和关系，这批有 {items} 个；请拆成几次导入")


def publish_schema(db: Session, user: User, ontology: OntologyProject, bundle: Bundle, provenance: dict[str, Any]) -> OntologyCandidateSet:
    """Import the interchange document as a candidate set, accept every item and publish them in one revision."""
    cset = candidates.import_document(db, ontology.id, user, text=schema_mod.interchange(bundle), example_id=None, title=bundle.title)
    cset.generator = {"tool": "数据导入", "model_label": "未使用模型", "locality": "local", "source_label": bundle.title, "sample": bundle.id.startswith("demo-"),
                      "reviewed": True, "import": provenance}
    db.commit()
    items = db.query(OntologyCandidateItem).filter_by(set_id=cset.id).order_by(OntologyCandidateItem.ordinal).all()
    labels = {t.type_name: t.label for t in bundle.tables}
    for item in items:
        if item.kind == "entity_type" and labels.get(item.name):
            candidates.review_item(db, cset, item, user, decision="modified", final={"name_cn": labels[item.name]})
        else:
            candidates.review_item(db, cset, item, user, decision="accepted")
    items = db.query(OntologyCandidateItem).filter_by(set_id=cset.id).order_by(OntologyCandidateItem.ordinal).all()
    candidates.publish(db, cset, items, user, None)
    for relation in db.query(Relation).filter(Relation.ontology_id == ontology.id).all():
        provenance_of = (relation.properties or {}).get("provenance") or {}
        if provenance_of.get("candidate_set") == cset.id:  # the candidate path labels its relations with the tool that usually makes them
            relation.properties = {**relation.properties, "provenance": {**provenance_of, "tool": "数据导入"}}
    db.commit()
    return cset


def apply_constraints(db: Session, ontology_id: str, bundle: Bundle) -> int:
    """The rules a data steward states about a column (range, allowed values, format) go into the property definition."""
    entities = ontology_metadata.type_entities(db, ontology_id)
    applied = 0
    for table in bundle.tables:
        entity = entities.get(table.type_name)
        if entity is None or not table.constraints:
            continue
        properties = dict(entity.properties or {})
        definitions = [dict(d) for d in properties.get("property_definitions", [])]
        for definition in definitions:
            rule = table.constraints.get(definition.get("id"))
            if rule:
                definition.update(rule)
                applied += 1
        properties["property_definitions"] = definitions
        entity.properties = properties
    db.commit()
    return applied


def _cells(frame: pd.DataFrame) -> list[dict[str, Any]]:
    columns = list(frame.columns)
    dates = {c for c in columns if platform_type(frame[c]) == "date"}
    rows = []
    for values in frame.itertuples(index=False, name=None):
        row = {}
        for column, value in zip(columns, values):
            cooked = python_value(value)
            if cooked is not None:
                row[str(column)] = cooked[:10] if column in dates and isinstance(cooked, str) else cooked
        rows.append(row)
    return rows


def object_ids(ontology_id: str, table: Table, frame: pd.DataFrame | None = None) -> list[str]:
    frame = table.frame if frame is None else frame
    if table.primary_key:
        keys = [key_parts(row, table.primary_key) for row in frame[table.primary_key].to_dict("records")]
    else:
        keys = [str(i) for i in frame.index]
    return [object_id(ontology_id, table.type_name, k) for k in keys]


def _graph_props(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in row.items()}


def install_table(db: Session, graph: Any, ontology_id: str, entity: Entity, table: Table, frame: pd.DataFrame, progress: Progress | None = None) -> int:
    """Rows of one table as objects: SQL rows and graph nodes, in chunks."""
    ids = object_ids(ontology_id, table, frame)
    rows = _cells(frame)
    written = 0
    for start in range(0, len(rows), CHUNK):
        part_rows, part_ids = rows[start:start + CHUNK], ids[start:start + CHUNK]
        db.bulk_save_objects([EntityInstance(id=oid, entity_id=entity.id, ontology_id=ontology_id, row_identity=oid.split(":", 2)[-1][:200], row_data=row)
                              for oid, row in zip(part_ids, part_rows)])
        db.commit()
        graph.upsert_instances(ontology_id, [{"id": oid, "entity_type": table.type_name, "properties": _graph_props(row)} for oid, row in zip(part_ids, part_rows)])
        written += len(part_rows)
        if progress:
            progress(table.name, {"rows": written, "of": len(rows)})
    return written


def relation_types(db: Session, ontology_id: str) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """Relation type by (source field, target field) for foreign keys, and by bridge table name for link tables."""
    by_fields: dict[tuple[str, str], str] = {}
    by_bridge: dict[str, str] = {}
    for relation in db.query(Relation).filter(Relation.ontology_id == ontology_id).all():
        props = relation.properties or {}
        if props.get("via_table"):
            by_bridge[str(props["via_table"])] = relation.type
        else:
            by_fields[(str(props.get("source_field")), str(props.get("target_field")))] = relation.type
    return by_fields, by_bridge


def install_links(db: Session, graph: Any, ontology_id: str, bundle: Bundle, slices: dict[str, pd.DataFrame] | None = None, *, only_slices: bool = False) -> int:
    """One graph edge per link and row. `slices` restricts the source side of a table to the rows just installed (a replay step); with `only_slices`
    the links of every other table are left alone."""
    by_fields, by_bridge = relation_types(db, ontology_id)
    slices = slices or {}
    edges: list[dict[str, Any]] = []

    def lookup(table_name: str, column: str) -> dict[str, str]:
        table = bundle.table(table_name)
        ids = object_ids(ontology_id, table)
        return dict(zip((key_text(v) for v in table.frame[column]), ids))

    bridges: dict[str, list[Link]] = {}
    for link in bundle.links:
        source = bundle.table(link.source_table)
        if source.kind == "bridge":
            bridges.setdefault(source.name, []).append(link)
            continue
        relation = by_fields.get((f"{link.source_table}.{link.source_column}", f"{link.target_table}.{link.target_column}"))
        if relation is None or (only_slices and source.name not in slices):
            continue
        frame = slices.get(source.name, source.frame)
        targets = lookup(link.target_table, link.target_column)
        for sid, value in zip(object_ids(ontology_id, source, frame), frame[link.source_column]):
            if pd.isna(value):
                continue
            tid = targets.get(key_text(value))
            if tid:
                edges.append({"source": sid, "target": tid, "type": relation})
    for name, pair in bridges.items():
        relation = by_bridge.get(name)
        if relation is None or len(pair) != 2 or (only_slices and name not in slices):
            continue
        first, second = pair
        a, b = lookup(first.target_table, first.target_column), lookup(second.target_table, second.target_column)
        for x, y in zip(bundle.table(name).frame[first.source_column], bundle.table(name).frame[second.source_column]):
            if pd.isna(x) or pd.isna(y):
                continue
            if a.get(key_text(x)) and b.get(key_text(y)):
                edges.append({"source": a[key_text(x)], "target": b[key_text(y)], "type": relation})
    for start in range(0, len(edges), CHUNK * 2):
        graph.upsert_relations(ontology_id, edges[start:start + CHUNK * 2])
    return len(edges)
