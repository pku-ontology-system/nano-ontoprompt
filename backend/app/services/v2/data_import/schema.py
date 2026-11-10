"""The tables of a bundle as an Auto Ontology model-interchange document, and the provenance the lineage draws from.

Writing the schema this way (rather than inserting types directly) means an import goes through the same reviewed path as a generated
candidate set: the types and relationships are published in one revision, with the table, the key and the columns each came from
recorded as evidence.
"""
from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import yaml

from app.services.v2.data_import.model import Bundle, Table
from app.services.v2.data_import.profile import platform_type, python_value

NAMESPACE = uuid.UUID("5d0c3a2e-8d0c-4c58-9f0e-7a41b6c2d9f1")
SQL_TYPES = {"string": "text", "integer": "bigint", "double": "double precision", "boolean": "boolean", "date": "date", "timestamp": "timestamp without time zone", "json": "json"}
MAX_COLUMNS_IN_PROVENANCE = 80


def ident(bundle: Bundle, *parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, ":".join([bundle.id, *parts])))


def _samples(series) -> list:
    values = series.dropna().drop_duplicates().head(3).tolist()
    out = []
    for value in values:
        cooked = python_value(value)
        out.append(cooked if isinstance(cooked, (str, int, float, bool)) else str(cooked))
    return out


def _column(bundle: Bundle, table: Table, name: str) -> dict[str, Any]:
    series = table.frame[name]
    return {"id": ident(bundle, "column", table.name, name), "name": str(name), "description": table.column_labels.get(name, ""),
            "type": SQL_TYPES[platform_type(series)], "sample_values": _samples(series), "is_nullable": bool(series.isna().any()), "is_unique": bool(series.notna().all() and series.is_unique)}


def interchange(bundle: Bundle) -> str:
    """One database, one schema; a table per bundle table; a term per table that becomes an object type; the links as foreign keys."""
    tables, terms = [], []
    for table in bundle.tables:
        if table.kind == "stage":
            continue
        columns = [_column(bundle, table, c) for c in table.frame.columns]
        tables.append({"id": ident(bundle, "table", table.name), "name": table.name, "description": table.description or table.label, "pk": list(table.primary_key),
                       "type": "base table", "columns": columns})
        if table.kind == "bridge":
            continue
        attributes = [{"id": ident(bundle, "attribute", table.name, c["name"]), "column_id": c["id"], "name": table.column_labels.get(c["name"], c["name"]),
                       "description": table.column_labels.get(c["name"], "")} for c in columns]
        terms.append({"id": ident(bundle, "term", table.name), "name": table.type_name, "description": table.description or table.label,
                      "represents": [ident(bundle, "table", table.name)], "columns_attributes": attributes})
    declared, semantic = [], []
    for link in bundle.links:
        source = ident(bundle, "column", link.source_table, link.source_column)
        target = ident(bundle, "column", link.target_table, link.target_column)
        if link.basis == "declared_fk":
            declared.append({"source_column_id": source, "target_column_id": target})
        else:
            semantic.append({"column_id": source, "column_attribute_id": ident(bundle, "attribute", link.target_table, link.target_column), "use_in_join_paths": True})
    document = {"data_layer": {"databases": [{"id": ident(bundle, "database"), "dialect": "duckdb", "schemas": [{"id": ident(bundle, "schema"), "name": "public",
                                                                                                                 "database_name": bundle.id, "tables": tables}]}],
                               "foreign_keys": declared},
                "semantic_layer": {"terms": terms, "semantic_fks": semantic}}
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def provenance(bundle: Bundle, report: dict[str, Any]) -> dict[str, Any]:
    """Where everything came from, in a form the lineage can draw: sources, the tables read or derived from them, and the links."""
    tables = []
    for table in bundle.tables:
        tables.append({"name": table.name, "type_name": table.type_name, "label": table.label, "kind": table.kind, "rows": int(len(table.frame)), "primary_key": list(table.primary_key),
                       "source": table.source, "columns": [{"name": str(c), "type": platform_type(table.frame[c]), "label": table.column_labels.get(c, "")}
                                                           for c in list(table.frame.columns)[:MAX_COLUMNS_IN_PROVENANCE]],
                       "derived": dataclasses.asdict(table.derived) if table.derived else None})
    out = {"bundle": bundle.id, "kind": bundle.kind, "title": bundle.title, "sources": [dataclasses.asdict(s) for s in bundle.sources], "tables": tables,
           "links": [dataclasses.asdict(l) for l in bundle.links], "notes": report.get("notes", [])}
    if bundle.replay:
        out["replay"] = {**dataclasses.asdict(bundle.replay), "cursor": bundle.replay.initial}
    return out
