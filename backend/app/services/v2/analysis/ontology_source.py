"""The platform's own ontology as a source for the analysis engines.

Objects are read through the same object query the object explorer uses, so permissions and property redaction apply exactly as
they do there; this module only pages through the result and shapes it as a table. Reading more than one page needs a pinned
snapshot (the object query refuses to continue a live read), so a data view is created when a type has more than 200 objects.
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

OBJECT_ID = "__object_id"
PAGE = 200
MAX_OBJECTS = 10_000
INTERNAL = {"_instance_id"}


class OntologySourceError(ValueError):
    """The object type is not there, or cannot be read as a table."""


def _object_query():
    from app.routers.v2 import object_query  # imported late: the router imports services of its own

    return object_query


def object_types(db: Any, user: Any, ontology_id: str) -> list[dict]:
    """The object types the caller may see, with their properties (data type and whether a value is required)."""
    catalog = _object_query().query_catalog(ontology_id, db, user)
    out = []
    for item in catalog.get("types", []):
        if item.get("kind") not in (None, "object"):
            continue
        properties = [{"api_name": p["api_name"], "data_type": p.get("data_type", "string"), "nullable": bool(p.get("nullable", True)), "is_array": bool(p.get("is_array", False))}
                      for p in item.get("properties", []) if p["api_name"] not in INTERNAL]
        out.append({"api_name": item["api_name"], "properties": properties})
    return sorted(out, key=lambda t: t["api_name"])


def object_counts(db: Any, user: Any, graph_service: Any, ontology_id: str, names: list[str]) -> dict[str, dict | None]:
    """{type: {count, capped}} from one page of each type: exact up to a page, `capped` when there are more. Exact totals need a pinned
    snapshot (the object query refuses them on a live read), which is not worth a data view for a picker. A type that cannot be read is None."""
    module, out = _object_query(), {}
    for name in names:
        try:
            page = _load(module, ontology_id, name, {"ontology_id": ontology_id, "consistency": "live"}, {"page_size": PAGE}, db, user, graph_service)
            out[name] = {"count": len(page.objects), "capped": bool(page.page.has_more)}
        except Exception:  # noqa: BLE001  a count is a convenience; the caller shows "unknown"
            out[name] = None
    return out


def _load(module: Any, ontology_id: str, object_type: str, context: dict, read: dict, db: Any, user: Any, graph_service: Any) -> Any:
    from app.schemas.v2.object_query import LoadObjectSetRequest

    request = LoadObjectSetRequest.model_validate({"expression": {"kind": "base", "type_ref": {"api_name": object_type}}, "context": context, "read": read})
    return module.load_object_query(ontology_id, request, db, user, graph_service)


def _cell(value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list, tuple)) else value


def frame_from_objects(objects: list[Any], properties: list[str]) -> pd.DataFrame:
    """One row per object; objects and arrays become JSON text so every column can be compared and counted."""
    rows = [{OBJECT_ID: obj.object_id, **{name: _cell(obj.properties.get(name)) for name in properties}} for obj in objects]
    return pd.DataFrame(rows, columns=[OBJECT_ID, *properties])


def load_objects(db: Any, user: Any, graph_service: Any, ontology_id: str, object_type: str, *, limit: int = MAX_OBJECTS) -> tuple[list[Any], dict]:
    """All objects of one type, up to `limit`. Errors from the object query pass through unchanged."""
    from app.schemas.v2.object_query import QueryDataViewCreate

    module = _object_query()
    first = _load(module, ontology_id, object_type, {"ontology_id": ontology_id, "consistency": "live"}, {"page_size": PAGE}, db, user, graph_service)
    objects, info = list(first.objects), {"object_type": object_type, "snapshot": False, "truncated": False}
    if first.page.has_more:
        view = module.create_data_view(ontology_id, QueryDataViewCreate(source_ontology_id=ontology_id, source_manifest_digest=f"analysis:{object_type}", retention_seconds=3600), db, user, graph_service)
        context = {"ontology_id": ontology_id, "consistency": "snapshot", "data_view_id": view.view_id}
        objects, token = [], None
        while True:
            read = {"page_size": PAGE, **({"page_token": token} if token else {})}
            page = _load(module, ontology_id, object_type, context, read, db, user, graph_service)
            objects += list(page.objects)
            if not page.page.has_more:
                break
            if len(objects) >= limit:
                info["truncated"] = True
                break
            token = page.page.next_page_token
        info.update(snapshot=True, data_view_id=view.view_id)
    return objects[:limit], info


def load_frame(db: Any, user: Any, graph_service: Any, ontology_id: str, object_type: str, *, limit: int = MAX_OBJECTS) -> tuple[pd.DataFrame, dict, dict]:
    """(table of the objects, the type's description, how it was read)."""
    types = {t["api_name"]: t for t in object_types(db, user, ontology_id)}
    if object_type not in types:
        raise OntologySourceError(f"object type '{object_type}' does not exist in this ontology, or you may not see it")
    description = types[object_type]
    objects, info = load_objects(db, user, graph_service, ontology_id, object_type, limit=limit)
    frame = frame_from_objects(objects, [p["api_name"] for p in description["properties"]])
    info["row_count"] = int(len(frame))
    return frame, description, info
