"""Data health on an ontology's objects: the suite the ontology implies, run on the objects the object query returns."""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.v2.analysis import health, ontology_metadata, ontology_source
from app.services.v2.analysis.ontology_source import OBJECT_ID
from app.services.v2.analysis.ontology_suite import MAX_REFERENCE_VALUES, build_ontology_suite

BASELINE = "数据现状"


def dataset_id(ontology_id: str, object_type: str) -> str:
    return f"ontology:{ontology_id}:{object_type}"[:80]


def _keys_from_database(db: Any, ontology_id: str, object_type: str, column: str) -> list | None:
    """The values of one property across the objects of a type, read from the stored rows: enough to check that a foreign key points at something
    that exists, and far cheaper than reading the whole type through the object query. None when the database has no rows for the type."""
    from app.models.entity import Entity
    from app.models.entity_instance import EntityInstance

    entity = ontology_metadata.type_entities(db, ontology_id).get(object_type)
    if entity is None:
        return None
    rows = db.query(EntityInstance.row_data).filter(EntityInstance.entity_id == entity.id, EntityInstance.is_deleted.is_(False)).all()
    return [row[0].get(column) for row in rows] if rows else None


def _references(db: Any, user: Any, graph: Any, ontology_id: str, object_type: str) -> list[dict]:
    """Foreign keys with the values they may take: the keys of the objects they point at."""
    out = []
    cache: dict[tuple[str, str], list | None] = {}
    for fk in ontology_metadata.foreign_keys(db, ontology_id, object_type):
        key = (fk["target_type"], fk["target_property"])
        if key not in cache:
            cache[key] = _keys_from_database(db, ontology_id, *key)
            if cache[key] is None:  # no stored rows: read the objects the way a person would see them
                try:
                    frame = ontology_source.load_frame(db, user, graph, ontology_id, fk["target_type"], limit=MAX_REFERENCE_VALUES + 1)[0]
                    cache[key] = frame[fk["target_property"]].tolist() if fk["target_property"] in frame.columns else None
                except Exception:  # noqa: BLE001  the target may be hidden from this user: then the key cannot be checked
                    cache[key] = None
        values = cache[key]
        if values is None:
            continue
        unique = list(dict.fromkeys(v for v in values if v is not None and not (isinstance(v, float) and pd.isna(v))))
        out.append({**fk, "values": unique if len(unique) <= MAX_REFERENCE_VALUES else None})
    return out


def suite_for(db: Any, user: Any, graph: Any, ontology_id: str, object_type: str, *, baseline: bool = False, loaded: tuple | None = None) -> tuple[dict, pd.DataFrame, dict]:
    """(suite, the objects as a table, how they were read). The baseline, when asked for, is added after the ontology's own expectations.
    `loaded` is a read already made ((frame, description, info)), so one run judges and groups the same objects."""
    frame, description, info = loaded or ontology_source.load_frame(db, user, graph, ontology_id, object_type)
    entity = ontology_metadata.type_entities(db, ontology_id).get(object_type)
    evidence = ontology_metadata.candidate_evidence(db, ontology_id).get(object_type, {})
    columns = [p["api_name"] for p in description["properties"]]
    unique = [c["name"] for c in evidence.get("columns", []) if c.get("unique") and c.get("name") in columns]
    suite = build_ontology_suite(object_type, description["properties"], raw_definitions=ontology_metadata.raw_definitions(entity), primary_keys=evidence.get("primary_key") or [],
                                 unique_columns=unique, references=_references(db, user, graph, ontology_id, object_type))
    counts = {"ontology": len(suite["expectations"]), "baseline": 0}
    if baseline and len(frame):
        taken = {(e["expectation_type"], str(e["kwargs"].get("column"))) for e in suite["expectations"]}
        extra = [e for e in health.suggest_suite(frame[columns], name="baseline")["expectations"] if (e["expectation_type"], str(e["kwargs"].get("column"))) not in taken
                 and e["expectation_type"] != "expect_table_row_count_to_be_between"]
        for item in extra:
            item["meta"] = {**(item.get("meta") or {}), "source": BASELINE}
        suite["expectations"] += extra
        counts["baseline"] = len(extra)
    info.update(counts=counts, columns=columns, source_table=evidence.get("source_table"), built_by=evidence.get("set_title"))
    return suite, frame, info


def run_health(frame: pd.DataFrame, suite: dict, *, dataset: str) -> dict:
    """Validate, and say which objects broke each expectation (the first few, by object id) so they can be opened in the object explorer."""
    columns = [c for c in frame.columns if c != OBJECT_ID]
    report = health.validate(frame[columns], suite, dataset_id=dataset)
    ids = frame[OBJECT_ID].tolist()
    for result in report["results"]:
        indexes = result["result"].get("partial_unexpected_index_list")
        if indexes:
            result["result"]["partial_unexpected_object_ids"] = [ids[i] for i in indexes if i < len(ids)]
    report["meta"]["object_count"] = int(len(frame))
    return report
