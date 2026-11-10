"""Read a set of tables for what the platform needs: the type of each column, the key of each table, and the links between tables.

Keys and links that a bundle declares (a SQL schema, an RDF model, a spec) are kept as given and only checked. When nothing is declared
they are inferred the way a person would: a column that is unique and never empty, named like a key, is the key; a column whose
name points at another table's key and whose values are all found in that key is a link (an inclusion dependency).
"""
from __future__ import annotations

import datetime as dt
import decimal
import math
import re
from typing import Any

import numpy as np
import pandas as pd

from app.services.v2.data_import.model import Bundle, Link, Table, snake

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
MIN_COVERAGE = 0.95
MIN_DISTINCT = 2


def platform_type(series: pd.Series) -> str:
    """string, integer, double, boolean, date, timestamp or json: the property types the ontology editor knows."""
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_integer_dtype(dtype):
        return "integer"
    if pd.api.types.is_float_dtype(dtype):
        return "double"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        present = series.dropna()
        return "date" if len(present) and (present.dt.normalize() == present).all() else "timestamp"
    values = series.dropna()
    if values.empty:
        return "string"
    sample = values.iloc[:200]
    if sample.map(lambda v: isinstance(v, (dict, list, tuple))).any():
        return "json"
    if sample.map(lambda v: isinstance(v, (dt.datetime, pd.Timestamp))).all():
        return "timestamp"
    text = sample.astype(str)
    if text.str.match(ISO_TIMESTAMP).all():
        return "timestamp"
    if text.str.match(ISO_DATE).all():
        return "date"
    return "string"


def python_value(value: Any) -> Any:
    """A cell as a JSON value: numpy numbers become numbers, times become ISO text, a missing value becomes None."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    if isinstance(value, (pd.Timestamp, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (list, tuple, dict)):
        return value
    return str(value) if not isinstance(value, (int, str)) else value


def key_text(value: Any) -> str:
    """The text that identifies a key value: 7 and 7.0 are the same key."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def key_parts(row: pd.Series | dict, columns: list[str]) -> str:
    return "-".join(key_text(row[c]) for c in columns)


def object_id(ontology_id: str, type_name: str, key: str) -> str:
    return f"{ontology_id[:8]}:{type_name}:{key}"


def infer_primary_key(table: Table) -> list[str]:
    frame, name = table.frame, snake(table.name)
    stem = name[:-1] if name.endswith("s") else name
    usable = [c for c in frame.columns if frame[c].notna().all() and frame[c].is_unique and platform_type(frame[c]) in ("string", "integer")]
    if not usable:
        return []
    wanted = ["id", f"{name}_id", f"{stem}_id", f"{name}id", f"{stem}id", f"{name}_key", f"{stem}_key", f"{name}_code", f"{stem}_code"]
    for target in wanted:
        for column in usable:
            if str(column).lower() == target:
                return [column]
    for column in usable:
        if re.search(r"(^|_)(id|key|code|no|number)$", str(column).lower()) or str(column).lower().endswith(("key", "id")):
            return [column]
    return []


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _points_at(column: str, target: Table, key: str) -> bool:
    """Whether a column's name says it holds the key of `target`."""
    col, k = _norm(column), _norm(key)
    base = snake(target.name)
    names = {_norm(base), _norm(base[:-1] if base.endswith("s") else base), _norm(target.type_name)}
    if col == k and len(k) > 2:
        return True
    if k == "id":
        return any(col in (n + "id", n + "key", n + "code", n + "no") for n in names)
    return (col.endswith(k) and len(k) >= 4) or (k.endswith(col) and len(col) >= 4)


def coverage(source: pd.Series, target: pd.Series) -> tuple[float, int]:
    """(share of the non-empty source values found among the target values, distinct source values)."""
    present = source.dropna()
    if present.empty:
        return 0.0, 0
    known = {key_text(v) for v in target.dropna().unique()}
    found = present.map(lambda v: key_text(v) in known)
    return float(found.mean()), int(present.nunique())


def infer_links(bundle: Bundle) -> list[Link]:
    declared = {(l.source_table, l.source_column) for l in bundle.links}
    joined = {frozenset([(l.source_table, l.source_column), (l.target_table, l.target_column)]) for l in bundle.links}  # a link that is declared in the other direction is already there
    entities = [t for t in bundle.tables if t.kind in ("entity", "event", "asset") and len(t.primary_key) == 1]
    out: list[Link] = []
    taken = {l.name for l in bundle.links}
    for table in bundle.tables:
        if table.kind in ("bridge", "stage"):
            continue
        for column in table.frame.columns:
            if (table.name, column) in declared or [column] == table.primary_key:
                continue
            kind = platform_type(table.frame[column])
            if kind not in ("string", "integer"):
                continue
            for target in entities:
                key = "file_name" if target.kind == "asset" and "file_name" in target.frame.columns and target.frame["file_name"].is_unique else target.primary_key[0]
                if target.name == table.name or platform_type(target.frame[key]) != kind:
                    continue
                if not (target.kind == "asset" and key == "file_name") and not _points_at(column, target, key):
                    continue
                if frozenset([(table.name, column), (target.name, key)]) in joined:
                    continue
                share, distinct = coverage(table.frame[column], target.frame[key])
                if share >= MIN_COVERAGE and distinct >= MIN_DISTINCT:
                    name = snake(f"{table.name}_{column}")
                    while name in taken:
                        name += "_2"
                    taken.add(name)
                    out.append(Link(name=name, source_table=table.name, source_column=column, target_table=target.name, target_column=key, basis="semantic_fk",
                                    label=f"{table.label}→{target.label}", coverage=round(share, 4)))
                    break
    return out


def finalize(bundle: Bundle, *, infer: bool = True) -> dict[str, Any]:
    """Fill in the keys and links a bundle did not declare, check the ones it did, and say what was done."""
    notes: list[str] = []
    for table in bundle.tables:
        table.frame = table.frame.reset_index(drop=True)
        if table.kind in ("bridge", "stage"):
            continue
        if table.primary_key:
            frame = table.frame
            missing = [c for c in table.primary_key if c not in frame.columns]
            if missing or frame[table.primary_key].isna().any().any() or frame.duplicated(table.primary_key).any():
                notes.append(f"{table.name}：声明的主键 {table.primary_key} 不成立（缺列、有空值或有重复），改为推断")
                table.primary_key = []
        if not table.primary_key:
            table.primary_key = infer_primary_key(table)
            notes.append(f"{table.name}：{'推断主键 ' + ', '.join(table.primary_key) if table.primary_key else '找不到唯一且不为空的键，按行号编号'}")
    kept = []
    for link in bundle.links:
        source, target = bundle.table(link.source_table), bundle.table(link.target_table)
        if link.source_column not in source.frame.columns or link.target_column not in target.frame.columns:
            notes.append(f"{link.name}：列不存在，已忽略")
            continue
        share, _ = coverage(source.frame[link.source_column], target.frame[link.target_column])
        link.coverage = round(share, 4)
        kept.append(link)
    bundle.links = kept
    if infer:
        found = infer_links(bundle)
        bundle.links += found
        notes += [f"{l.name}：按名字和取值推断出的关系（{l.coverage:.0%} 的值在 {l.target_table}.{l.target_column} 里找得到）" for l in found]
    return {"tables": len(bundle.tables), "rows": int(sum(len(t.frame) for t in bundle.tables if t.kind not in ("bridge", "stage"))), "links": len(bundle.links), "notes": notes}
