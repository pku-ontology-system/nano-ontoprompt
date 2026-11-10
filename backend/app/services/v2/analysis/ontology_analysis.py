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


def _references(db: Any, user: Any, graph: Any, ontology_id: str, object_type: str) -> list[dict]:
    """Foreign keys with the values they may take: the keys of the objects they point at."""
    out = []
    cache: dict[str, pd.DataFrame | None] = {}
    for fk in ontology_metadata.foreign_keys(db, ontology_id, object_type):
        target = fk["target_type"]
        if target not in cache:
            try:
                cache[target] = ontology_source.load_frame(db, user, graph, ontology_id, target, limit=MAX_REFERENCE_VALUES + 1)[0]
            except (ontology_source.OntologySourceError, Exception):  # the target may be hidden from this user: then the key cannot be checked
                cache[target] = None
        frame = cache[target]
        if frame is None or fk["target_property"] not in frame.columns:
            continue
        values = [v for v in frame[fk["target_property"]].dropna().unique().tolist()]
        out.append({**fk, "values": values if len(values) <= MAX_REFERENCE_VALUES else None})
    return out


def suite_for(db: Any, user: Any, graph: Any, ontology_id: str, object_type: str, *, baseline: bool = False) -> tuple[dict, pd.DataFrame, dict]:
    """(suite, the objects as a table, how they were read). The baseline, when asked for, is added after the ontology's own expectations."""
    frame, description, info = ontology_source.load_frame(db, user, graph, ontology_id, object_type)
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
