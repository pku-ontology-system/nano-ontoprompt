"""An expectation suite read off the ontology itself: what the object type promises about its data.

Sources, in the order they are used: the properties' data types and required flags, the identity property (the primary key an
Auto Ontology candidate recorded, else a property named after the type), uniqueness and allowed values that a property definition
declares, and the foreign keys of relations. Every expectation says where it came from in `meta.source`, so a failure can be traced
back to the part of the ontology that asked for it. A baseline read off the current data is kept apart (`suggest_suite`).
"""
from __future__ import annotations

import re
from typing import Any

NUMERIC = {"integer": "integer", "long": "integer", "double": "number", "decimal": "number"}
TEMPORAL = {"timestamp", "datetime", "date"}
OWN = "本体"
MAX_REFERENCE_VALUES = 2000


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def identity_property(object_type: str, names: list[str], primary_keys: list[str]) -> tuple[str | None, str]:
    """The property that identifies an object, and how it was found."""
    for key in primary_keys:
        if key in names:
            return key, "Auto Ontology 记录的主键"
    stem = snake(object_type)
    tokens = set(stem.split("_"))
    for name in names:
        if name == "id" or (name.endswith("_id") and name[:-3] in tokens | {stem}):
            return name, "按名字推断的标识属性"
    return None, ""


def _expect(kind: str, note: str, **kwargs: Any) -> dict[str, Any]:
    return {"expectation_type": f"expect_{kind}", "kwargs": kwargs, "meta": {"source": OWN, "notes": note}}


def build_ontology_suite(object_type: str, properties: list[dict], *, raw_definitions: dict[str, dict] | None = None, primary_keys: list[str] | None = None,
                         unique_columns: list[str] | None = None, references: list[dict] | None = None) -> dict[str, Any]:
    """`properties` are the catalog's ({api_name, data_type, nullable}); `references` are foreign keys ({property, target_type, target_property, basis, values})."""
    raw = raw_definitions or {}
    names = [p["api_name"] for p in properties]
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(item: dict[str, Any]) -> None:
        key = (item["expectation_type"], str(item["kwargs"].get("column")))
        if key not in seen:
            seen.add(key)
            items.append(item)

    add(_expect("table_row_count_to_be_between", f"本体里有 {object_type} 这个类型，应当已有对象", min_value=1))
    identity, how = identity_property(object_type, names, primary_keys or [])
    if identity:
        add(_expect("column_values_to_not_be_null", f"对象标识（{how}）不能为空", column=identity))
        add(_expect("column_values_to_be_unique", f"对象标识（{how}）不能重复", column=identity))
    for column in unique_columns or []:
        if column in names and column != identity:
            add(_expect("column_values_to_be_unique", "Auto Ontology 记录这一列取值唯一", column=column))
    for prop in properties:
        column = prop["api_name"]
        if not prop.get("nullable", True):
            add(_expect("column_values_to_not_be_null", "本体里标为必填", column=column))
    for prop in properties:
        column, kind = prop["api_name"], str(prop.get("data_type", "string")).lower()
        if prop.get("is_array") or kind in ("array", "json", "object"):
            continue
        if kind in NUMERIC:
            add(_expect("column_values_to_match_json_schema", f"属性类型是 {kind}", column=column, json_schema={"type": NUMERIC[kind]}))
        elif kind == "boolean":
            add(_expect("column_values_to_match_json_schema", "属性类型是 boolean", column=column, json_schema={"type": "boolean"}))
        elif kind == "string":
            add(_expect("column_values_to_match_json_schema", "属性类型是 string", column=column, json_schema={"type": "string"}))
        elif kind in TEMPORAL:
            add(_expect("column_values_to_be_dateutil_parseable", f"属性类型是 {kind}", column=column))
    for column, definition in raw.items():
        if column not in names:
            continue
        allowed = definition.get("enum") or definition.get("allowed_values")
        if isinstance(allowed, list) and allowed:
            add(_expect("column_values_to_be_in_set", "属性定义里列出的允许取值", column=column, value_set=allowed))
        low, high = definition.get("minimum", definition.get("min")), definition.get("maximum", definition.get("max"))
        if isinstance(low, (int, float)) or isinstance(high, (int, float)):
            add(_expect("column_values_to_be_between", "属性定义里写明的取值范围", column=column, min_value=low if isinstance(low, (int, float)) else None, max_value=high if isinstance(high, (int, float)) else None))
        pattern = definition.get("pattern") or definition.get("regex")
        if isinstance(pattern, str) and pattern:
            add(_expect("column_values_to_match_regex", "属性定义里写明的格式", column=column, regex=pattern))
        if definition.get("unique") is True and column != identity:
            add(_expect("column_values_to_be_unique", "属性定义里标为唯一", column=column))
    for ref in references or []:
        values = ref.get("values")
        if ref["property"] in names and isinstance(values, list) and 0 < len(values) <= MAX_REFERENCE_VALUES:
            basis = "Auto Ontology 推断的外键" if ref.get("basis") == "semantic_fk" else "声明的外键"
            add(_expect("column_values_to_be_in_set", f"{basis}：{object_type}.{ref['property']} 应指向 {ref['target_type']}.{ref['target_property']} 里已有的对象",
                        column=ref["property"], value_set=sorted(values, key=str), mostly=1.0))
    return {"name": f"{object_type}_ontology", "expectations": items}
