"""The platform's own ontology as a source for the analysis engines.

Objects are read through the same object query the object explorer uses, so permissions and property redaction apply exactly as
they do there; this module only pages through the result and shapes it as a table. Reading more than one page needs a pinned
snapshot (the object query refuses to continue a live read), so a data view is created when a type has more than 200 objects.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pandas as pd

_VIEWS: dict[tuple[str, str], tuple[str, float, tuple]] = {}   # (ontology, user) -> (data view id, expires at, what the ontology looked like)
VIEW_SECONDS = 300
OBJECT_ID = "__object_id"
PAGE = 200
MAX_OBJECTS = 10_000
INTERNAL = {"_instance_id"}


class OntologySourceError(ValueError):
    """The object type is not there, or cannot be read as a table."""


class SnapshotNotAllowed(OntologySourceError):
    """Reading more than a page needs a pinned snapshot, which only an editor may create."""


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
    """{type: {count, capped}}: how many objects each type has, counted in the database (exact, and cheap for ontologies too large to read live)."""
    from sqlalchemy import func

    from app.models.entity import Entity
    from app.models.entity_instance import EntityInstance

    rows = (db.query(Entity.name_en, func.count(EntityInstance.id)).join(EntityInstance, EntityInstance.entity_id == Entity.id)
            .filter(Entity.ontology_id == ontology_id, EntityInstance.is_deleted.is_(False)).group_by(Entity.name_en).all())
    counted = {name: int(count) for name, count in rows}
    return {name: {"count": counted.get(name, 0), "capped": False} for name in names}


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


def _snapshot_view(module: Any, db: Any, user: Any, graph_service: Any, ontology_id: str) -> str:
    """A pinned data view of the ontology for paged reads. Building one copies the whole graph, so one view serves every read for a few minutes
    as long as the ontology has not changed (same number of objects, same revision)."""
    from sqlalchemy import func

    from app.models.entity_instance import EntityInstance
    from app.models.ontology import OntologyProject
    from app.schemas.v2.object_query import QueryDataViewCreate

    if getattr(user, "role", None) == "viewer":
        raise SnapshotNotAllowed("这个类型的对象多于 200 个，要先固定一份快照才能读完；只读账号不能创建快照，请用有编辑权限的账号运行")
    project = db.get(OntologyProject, ontology_id)
    signature = (db.query(func.count(EntityInstance.id)).filter(EntityInstance.ontology_id == ontology_id).scalar(), getattr(project, "current_revision_id", None))
    key = (ontology_id, str(user.id))
    cached = _VIEWS.get(key)
    if cached and cached[1] > time.time() and cached[2] == signature:
        return cached[0]
    view = module.create_data_view(ontology_id, QueryDataViewCreate(source_ontology_id=ontology_id, source_manifest_digest="analysis", retention_seconds=3600), db, user, graph_service)
    view_id, status = (view["view_id"], view.get("status")) if isinstance(view, dict) else (view.view_id, getattr(view, "status", None))
    if status not in (None, "ready"):
        raise OntologySourceError(f"the snapshot of the ontology is {status}, so more than one page of objects cannot be read yet")
    _VIEWS[key] = (view_id, time.time() + VIEW_SECONDS, signature)
    return view_id


def _read_snapshot(module: Any, db: Any, user: Any, graph_service: Any, ontology_id: str, object_type: str, view_id: str, limit: int, info: dict) -> list[Any]:
    """Every page of one type from a pinned view. One query core serves all the pages (a page request on its own re-reads the whole view each time),
    with the permission policy applied exactly as the object query applies it; if the core cannot be built, the pages are requested one by one."""
    context = {"ontology_id": ontology_id, "consistency": "snapshot", "data_view_id": view_id}
    objects: list[Any] = []
    try:
        from app.schemas.v2.object_query import LoadObjectSetRequest

        access = module._authorize(ontology_id, db, user)
        metadata = module.load_sql_metadata(db, ontology_id)
        view = module.require_ready(db, view_id, ontology_id, principal_id=user.id, is_admin=user.role == "admin")
        request = LoadObjectSetRequest.model_validate({"expression": {"kind": "base", "type_ref": {"api_name": object_type}}, "context": context, "read": {"page_size": PAGE}})
        request = request.model_copy(update={"context": module._pin_context_to_view(request.context, view, access.context.metadata_revision_id)})
        core = module.QueryCore(metadata, module.FalkorReadAdapter(graph_service._graph(view.graph_key), graph_ontology_id=view.graph_key),
                                module._query_policy(db, ontology_id, user, metadata_revision_id=request.context.revision_id, access_projection=access))
        resources = module.ResourceService(db, ontology_id, user, metadata, authorization_projection=access)

        def page_of(token: str | None) -> Any:
            return core.load(request.model_copy(update={"read": request.read.model_copy(update={"page_size": PAGE, "page_token": token})}), resources)
    except (AttributeError, TypeError):
        def page_of(token: str | None) -> Any:
            return _load(module, ontology_id, object_type, context, {"page_size": PAGE, **({"page_token": token} if token else {})}, db, user, graph_service)

    token = None
    while True:
        page = page_of(token)
        objects += list(page.objects)
        if not page.page.has_more:
            break
        if len(objects) >= limit:
            info["truncated"] = True
            break
        token = page.page.next_page_token
    return objects


def load_objects(db: Any, user: Any, graph_service: Any, ontology_id: str, object_type: str, *, limit: int = MAX_OBJECTS) -> tuple[list[Any], dict]:
    """All objects of one type, up to `limit`. Errors from the object query pass through unchanged."""
    from app.schemas.v2.object_query import QueryDataViewCreate

    module = _object_query()
    try:
        first = _load(module, ontology_id, object_type, {"ontology_id": ontology_id, "consistency": "live"}, {"page_size": PAGE}, db, user, graph_service)
    except Exception as exc:  # noqa: BLE001
        if (getattr(exc, "detail", None) or {}).get("code") != "query_too_complex":
            raise
        first = None  # a live read scans the whole ontology and refuses past 10,000 objects; a pinned snapshot has no such limit
    objects, info = (list(first.objects) if first else []), {"object_type": object_type, "snapshot": False, "truncated": False}
    if first is None or first.page.has_more:
        view_id = _snapshot_view(module, db, user, graph_service, ontology_id)
        context = {"ontology_id": ontology_id, "consistency": "snapshot", "data_view_id": view_id}
        objects = _read_snapshot(module, db, user, graph_service, ontology_id, object_type, view_id, limit, info)
        info.update(snapshot=True, data_view_id=view_id)
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
