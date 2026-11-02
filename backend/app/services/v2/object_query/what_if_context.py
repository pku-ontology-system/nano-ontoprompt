"""Read-only Object Query access to a sealed What-if baseline."""
from __future__ import annotations

from copy import deepcopy

from app.models.entity import Entity
from app.models.relation import Relation
from app.models.v2.dynamic_ontology import WhatIfScenario
from app.models.ontology import OntologyProject
from app.schemas.v2.object_query import ExecutionContext
from app.schemas.v2.scenario_context import scenario_inputs_digest, verify_scenario_context
from app.services.v2.object_query.core import GraphData
from app.services.v2.object_query.errors import ObjectQueryError
from app.services.v2.object_query.metadata import _type_api_name
from app.services.v2.what_if_service import WhatIfError, revalidate_scenario_context


class WhatIfReadAdapter:
    """Expose the bounded, revalidated scenario baseline as a frozen graph read."""

    def __init__(self, data: GraphData):
        self._data = data

    def read(self, _ontology_id: str) -> GraphData:
        return GraphData(deepcopy(self._data.objects), deepcopy(self._data.edges))


def resolve_what_if_context(db, context: ExecutionContext, ontology_id: str, user):
    """Return a normalized query context and the caller-authorized current baseline."""
    if not context.what_if_scenario_id:
        return context, None
    if context.ontology_id != ontology_id:
        raise ObjectQueryError("ontology_context_mismatch", "context.ontology_id", "Path ontology and execution context ontology must match")
    scenario = db.query(WhatIfScenario).filter(
        WhatIfScenario.id == context.what_if_scenario_id,
        WhatIfScenario.ontology_id == ontology_id,
    ).first()
    # Scenarios are private to their creator, including for query-context use.
    if scenario is None or str(scenario.created_by or "") != str(getattr(user, "id", "")):
        raise ObjectQueryError("not_found", "context.what_if_scenario_id", "What-if scenario not found or inaccessible")
    try:
        baseline = revalidate_scenario_context(db, scenario, user)
    except WhatIfError as exc:
        if exc.status_code == 404:
            code = "not_found"
        elif exc.status_code == 403:
            code = "forbidden"
        elif exc.status_code == 410:
            code = "expired_reference"
        elif exc.status_code == 409:
            code = "stale_reference"
        else:
            code = "scenario_unavailable"
        raise ObjectQueryError(code, "context.what_if_scenario_id", str(exc), what_if_code=exc.code) from exc

    typed_context = baseline.get("scenario_context") if isinstance(baseline, dict) else None
    if not verify_scenario_context(typed_context):
        raise ObjectQueryError("stale_reference", "context.what_if_context_digest", "What-if baseline context is missing or invalid")
    expected_context_digest = str(typed_context["context_digest"])
    expected_input_digest = scenario_inputs_digest(
        scenario.assumptions_json if isinstance(scenario.assumptions_json, list) else [],
        scenario.rule_overrides_json if isinstance(scenario.rule_overrides_json, dict) else {},
    )
    if context.what_if_context_digest != expected_context_digest:
        raise ObjectQueryError("stale_reference", "context.what_if_context_digest", "What-if baseline changed; refresh the scenario selection")
    if context.what_if_input_digest != expected_input_digest:
        raise ObjectQueryError("stale_reference", "context.what_if_input_digest", "What-if assumptions or rule overrides changed; refresh the scenario selection")
    if context.revision_id != typed_context.get("schema_revision_id"):
        raise ObjectQueryError("metadata_mismatch", "context.revision_id", "What-if scenario is pinned to another ontology revision")
    project = db.get(OntologyProject, ontology_id)
    if project is None or str(project.current_revision_id or "") != str(typed_context.get("schema_revision_id") or ""):
        raise ObjectQueryError("metadata_mismatch", "context.revision_id", "What-if scenario ontology revision is no longer the current query catalog")
    return context, baseline


def graph_data_from_baseline(db, ontology_id: str, metadata, baseline: dict) -> GraphData:
    """Translate What-if's safe temporal node/edge records into Object Query keys."""
    entities = db.query(Entity).filter(
        Entity.ontology_id == ontology_id,
        Entity.is_source_deleted.is_(False),
    ).all()
    type_aliases: dict[str, str] = {}
    for entity in entities:
        try:
            api_name = _type_api_name(entity)
        except Exception:
            continue
        for value in (entity.id, entity.canonical_id, entity.name_en, entity.name_cn, entity.type, api_name):
            if value:
                type_aliases.setdefault(str(value), api_name)

    relations = db.query(Relation).filter(Relation.ontology_id == ontology_id).all()
    link_alias_candidates: dict[str, set[str]] = {}
    for relation in relations:
        props = relation.properties if isinstance(relation.properties, dict) else {}
        api_name = str(props.get("api_name") or props.get("name") or relation.type or relation.id)
        aliases = (relation.id, relation.type, props.get("api_name"), props.get("name"), props.get("semantic_resource_id"))
        for alias in aliases:
            if alias:
                link_alias_candidates.setdefault(str(alias), set()).add(api_name)
    link_aliases = {key: next(iter(values)) for key, values in link_alias_candidates.items() if len(values) == 1}
    known_links = {item.api_name for item in metadata.links}

    objects: dict[tuple[str, str], dict] = {}
    for node in baseline.get("nodes", []) if isinstance(baseline.get("nodes"), list) else []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        raw_type = str(node.get("entity_type") or node.get("entity_id") or "")
        type_name = type_aliases.get(raw_type, raw_type)
        try:
            metadata.resolve_type("object", type_name, "context.what_if_scenario_id")
        except ObjectQueryError as exc:
            raise ObjectQueryError("metadata_mismatch", "context.what_if_scenario_id", "What-if baseline contains an object type outside the pinned query catalog", object_type=raw_type) from exc
        key = (type_name, str(node["id"]))
        if key in objects:
            raise ObjectQueryError("invalid_projection_record", "context.what_if_scenario_id", "What-if baseline contains a duplicate object identity")
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        objects[key] = {**properties, "_type": type_name, "_instance_id": key[1]}

    edges: list[tuple[tuple[str, str], str, tuple[str, str]]] = []
    for edge in baseline.get("edges", []) if isinstance(baseline.get("edges"), list) else []:
        if not isinstance(edge, dict):
            continue
        source_id, target_id = str(edge.get("source") or ""), str(edge.get("target") or "")
        source_type, target_type = type_aliases.get(str(edge.get("source_type") or "")), type_aliases.get(str(edge.get("target_type") or ""))
        # Baseline edges do not carry endpoint types; resolve by unique instance id.
        if source_type is None:
            matches = [kind for kind, object_id in objects if object_id == source_id]
            source_type = matches[0] if len(matches) == 1 else None
        if target_type is None:
            matches = [kind for kind, object_id in objects if object_id == target_id]
            target_type = matches[0] if len(matches) == 1 else None
        source, target = (source_type, source_id), (target_type, target_id)
        if source_type is None or target_type is None or source not in objects or target not in objects:
            continue
        props = edge.get("properties") if isinstance(edge.get("properties"), dict) else {}
        raw_link = str(props.get("semantic_resource_id") or props.get("api_name") or edge.get("type") or edge.get("label") or "")
        relation = link_aliases.get(raw_link, raw_link)
        if relation in known_links:
            edges.append((source, relation, target))
    return GraphData(objects, edges)
