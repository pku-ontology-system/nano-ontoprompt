"""Apply the canonical object, field and link policy to compiled Action edits."""
from __future__ import annotations

from sqlalchemy import or_

from app.models.entity import Entity
from app.models.ontology import OntologyProject
from app.models.relation import Relation
from app.models.v2.semantic_core import OntologySemanticResource, OntologySemanticResourceVersion
from app.schemas.v2.scenario import LinkTarget, Target
from app.services.v2.authorization_service import (
    AuthorizationContext,
    field_permitted,
    load_applicable_policies,
    redact_link_record,
    redact_record,
    resolve_projection,
)
from app.services.v2.object_query.core import GraphData
from app.services.v2.scenarios import ScenarioError


# No underscore-prefixed property is currently approved for Action access.
# A field may be added here only after its non-secret business contract is
# documented and its read/write surface is reviewed end to end.
ACTION_INTERNAL_PROPERTY_ALLOWLIST: frozenset[str] = frozenset()


def action_property_name_allowed(field_name: object) -> bool:
    return isinstance(field_name, str) and bool(field_name) and (
        not field_name.startswith("_") or field_name in ACTION_INTERNAL_PROPERTY_ALLOWLIST
    )


def action_properties_view(properties: dict | None) -> dict:
    if not isinstance(properties, dict):
        return {}
    return {name: value for name, value in properties.items()
            if action_property_name_allowed(name)}


def action_field_permitted(projection, record: dict, field_name: str) -> bool:
    """Combine normal field policy with the Action internal-property allowlist."""
    return action_property_name_allowed(field_name) and field_permitted(
        projection, record, field_name)


def _authorization_state(db, ontology_id: str, actor):
    project = db.get(OntologyProject, ontology_id)
    if project is None:
        raise ScenarioError("not_found", "Ontology not found", 404)
    context = AuthorizationContext(
        ontology_id=ontology_id,
        principal_id=str(actor.id),
        principal_role=actor.role,
        metadata_revision_id=project.current_revision_id,
    )
    policies = load_applicable_policies(db, context, actor.role)
    return project, context, policies


def _projection(db, context, policies, records):
    result = resolve_projection(db, context, preloaded_policies=policies, records=records)
    if not result.allowed:
        raise ScenarioError("forbidden", "当前用户无权访问 Action 涉及的数据", 403)
    return result


def _entity_for_type(db, ontology_id, object_type, entity_aliases=None):
    if entity_aliases is not None:
        rows = entity_aliases.get(str(object_type), [])
    else:
        rows = db.query(Entity).filter(
            Entity.ontology_id == ontology_id,
            or_(Entity.id == object_type, Entity.name_en == object_type,
                Entity.name_cn == object_type, Entity.semantic_resource_id == object_type),
        ).limit(2).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise ScenarioError("ambiguous_object_type", "Action 对象类型标识不唯一", 409)
    return rows[0]


def _link_access_record(db, ontology_id, project, link, *, entity_aliases=None,
                        relations_by_type=None, link_resources_by_api=None, link_versions=None,
                        link_versions_by_legacy_relation=None,
                        implementations_by_object=None):
    source_entity = _entity_for_type(db, ontology_id, link.source.concrete_type, entity_aliases)
    target_entity = _entity_for_type(db, ontology_id, link.target.concrete_type, entity_aliases)
    if source_entity is None or target_entity is None:
        raise ScenarioError("object_type_not_found", "Action 关系端点类型不在当前本体中", 409)

    if relations_by_type is None:
        relation_rows = db.query(Relation).filter_by(
            ontology_id=ontology_id, type=link.relation_type).all()
    else:
        relation_rows = relations_by_type.get(link.relation_type, [])

    # Legacy relation labels are not unique. Resolve a stored edge by its
    # source/target object types before deciding that the label is ambiguous.
    matching_relations = [
        row for row in relation_rows
        if str(row.source_entity or "") == str(source_entity.id)
        and str(row.target_entity or "") == str(target_entity.id)
    ]
    if len(matching_relations) > 1:
        raise ScenarioError("ambiguous_link_type", "Action 关系定义与端点组合不唯一", 409)
    if relation_rows and not matching_relations:
        raise ScenarioError("link_type_mismatch", "Action 关系的源/目标对象类型不匹配", 422)
    relation = matching_relations[0] if matching_relations else None

    if link_resources_by_api is None:
        link_resources_by_api = {row.api_name: row for row in db.query(OntologySemanticResource).filter_by(
            ontology_id=ontology_id, kind="link_type").all()}
    resource = link_resources_by_api.get(link.relation_type)

    if link_versions_by_legacy_relation is None:
        link_versions_by_legacy_relation = {}
        if project.current_revision_id:
            versions = db.query(OntologySemanticResourceVersion).filter_by(
                ontology_id=ontology_id, revision_id=project.current_revision_id,
                kind="link_type").all()
            for row in versions:
                legacy_id = (row.constraints_json or {}).get("legacy_relation_id")
                if legacy_id is not None:
                    link_versions_by_legacy_relation.setdefault(str(legacy_id), []).append(row)

    version = None
    if relation is not None and project.current_revision_id:
        mapped_versions = link_versions_by_legacy_relation.get(str(relation.id), [])
        if len(mapped_versions) > 1:
            raise ScenarioError("ambiguous_link_type", "当前修订中的关系映射不唯一", 409)
        if mapped_versions:
            version = mapped_versions[0]
        elif resource is not None:
            candidate = (link_versions or {}).get(resource.id)
            if candidate is None:
                candidate = db.query(OntologySemanticResourceVersion).filter_by(
                    ontology_id=ontology_id, revision_id=project.current_revision_id,
                    resource_id=resource.id, kind="link_type").first()
            candidate_legacy_id = (candidate.constraints_json or {}).get("legacy_relation_id") if candidate else None
            if candidate is not None and candidate_legacy_id is not None and str(candidate_legacy_id) != str(relation.id):
                raise ScenarioError("link_type_mismatch", "Action 关系标签指向了其他稳定关系定义", 409)
            version = candidate

    if relation is None and resource is None:
        return None
    if relation is None and project.current_revision_id:
        version = (link_versions or {}).get(resource.id)
        if version is None:
            version = db.query(OntologySemanticResourceVersion).filter_by(
                ontology_id=ontology_id, revision_id=project.current_revision_id,
                resource_id=resource.id, kind="link_type").first()
        if version is None:
            return None

    if relation is not None:
        expected_source = db.get(Entity, relation.source_entity)
        expected_target = db.get(Entity, relation.target_entity)
        if expected_source and expected_source.id != source_entity.id:
            raise ScenarioError("link_type_mismatch", "Action 关系的源对象类型不匹配", 422)
        if expected_target and expected_target.id != target_entity.id:
            raise ScenarioError("link_type_mismatch", "Action 关系的目标对象类型不匹配", 422)

    if version is not None:
        actual_source = source_entity.semantic_resource_id
        actual_target = target_entity.semantic_resource_id
        expected = (version.source_resource_id, version.target_resource_id)
        actual = (actual_source, actual_target)

        def implements(actual_resource_id, expected_resource_id):
            return actual_resource_id == expected_resource_id or expected_resource_id in (
                (implementations_by_object or {}).get(actual_resource_id, set()))

        compatible = implements(actual[0], expected[0]) and implements(actual[1], expected[1])
        reversed_compatible = version.direction == "undirected" and (
            implements(actual[0], expected[1]) and implements(actual[1], expected[0]))
        if not compatible and not reversed_compatible:
            raise ScenarioError("link_type_mismatch", "Action 关系端点与本体定义不匹配", 422)

    link_type_id = version.resource_id if version is not None else (resource.id if resource else link.relation_type)
    return {
        "id": f"{link.source.object_id}:{link.relation_type}:{link.target.object_id}",
        "link_type_id": link_type_id,
        "relation_type": link.relation_type,
        "type": link.relation_type,
        "source_id": link.source.object_id,
        "target_id": link.target.object_id,
        "properties": {},
    }


def authorize_action_snapshot(db, ontology_id: str, actor, snapshot):
    """Return a graph snapshot containing only objects, fields and links the actor can read.

    This projection must happen before Action compilation so rules and bound
    Functions cannot use hidden values to derive otherwise-visible edits.
    """
    project, context, policies = _authorization_state(db, ontology_id, actor)
    entities = db.query(Entity).filter(
        Entity.ontology_id == ontology_id,
        Entity.is_source_deleted.is_(False),
    ).all()
    entity_aliases = {}
    for entity in entities:
        for alias in (entity.id, entity.name_en, entity.name_cn, entity.semantic_resource_id):
            if alias:
                bucket = entity_aliases.setdefault(str(alias), [])
                if all(row.id != entity.id for row in bucket):
                    bucket.append(entity)
    relations_by_type = {}
    for relation in db.query(Relation).filter_by(ontology_id=ontology_id).all():
        relations_by_type.setdefault(relation.type, []).append(relation)
    link_resources_by_api = {
        row.api_name: row for row in db.query(OntologySemanticResource).filter_by(
            ontology_id=ontology_id, kind="link_type").all()
    }
    link_versions_by_legacy_relation = {}
    link_versions = {}
    if project.current_revision_id:
        link_versions = {
            row.resource_id: row for row in db.query(OntologySemanticResourceVersion).filter_by(
                ontology_id=ontology_id, revision_id=project.current_revision_id,
                kind="link_type").all()
        }
        for version in link_versions.values():
            legacy_id = (version.constraints_json or {}).get("legacy_relation_id")
            if legacy_id is not None:
                link_versions_by_legacy_relation.setdefault(str(legacy_id), []).append(version)
    implementations_by_object = {}
    if project.current_revision_id:
        for row in db.query(OntologySemanticResourceVersion).filter_by(
            ontology_id=ontology_id, revision_id=project.current_revision_id,
            kind="interface_implementation").all():
            if row.parent_resource_id and row.interface_resource_id:
                implementations_by_object.setdefault(row.parent_resource_id, set()).add(
                    row.interface_resource_id)
    records_by_key = {}
    for key, raw_properties in snapshot.objects.items():
        object_type, object_id = key
        entity = _entity_for_type(db, ontology_id, object_type, entity_aliases)
        if entity is None:
            # Unknown graph labels are excluded rather than offered to the compiler.
            continue
        resource = db.get(OntologySemanticResource, entity.semantic_resource_id) if entity.semantic_resource_id else None
        if resource and resource.kind == "interface":
            continue
        properties = action_properties_view(raw_properties)
        records_by_key[key] = {
            "id": f"{object_type}:{object_id}",
            "entity_id": entity.id,
            "entity_type": entity.name_en or entity.name_cn,
            "semantic_resource_id": entity.semantic_resource_id,
            "object_type_resource_id": entity.semantic_resource_id,
            "properties": properties,
        }

    projection = _projection(db, context, policies, list(records_by_key.values()))
    visible_objects = {}
    for key, record in records_by_key.items():
        redacted = redact_record(projection, record)
        if redacted is None:
            continue
        # Build the compiler input only from the policy projection. Copying
        # the raw graph properties here would reintroduce internal fields
        # filtered above (for example, underscore-prefixed graph metadata).
        visible_objects[key] = dict(redacted.get("properties") or {})

    visible_edges = []
    for source, relation_type, target in snapshot.edges:
        if source not in visible_objects or target not in visible_objects:
            continue
        link = LinkTarget(
            ontology_id=ontology_id,
            relation_type=relation_type,
            source=Target(ontology_id=ontology_id, concrete_type=source[0], object_id=source[1]),
            target=Target(ontology_id=ontology_id, concrete_type=target[0], object_id=target[1]),
        )
        record = _link_access_record(db, ontology_id, project, link,
            entity_aliases=entity_aliases, relations_by_type=relations_by_type,
            link_resources_by_api=link_resources_by_api, link_versions=link_versions,
            link_versions_by_legacy_relation=link_versions_by_legacy_relation,
            implementations_by_object=implementations_by_object)
        if record is not None and redact_link_record(
            projection, record, link_type_id=record["link_type_id"],
        ) is not None:
            visible_edges.append((source, relation_type, target))

    return GraphData(visible_objects, visible_edges), projection.permission_digest


def authorize_compiled_edits(
    db,
    ontology_id: str,
    actor,
    snapshot,
    edits,
    *,
    allow_create_collisions: bool = False,
) -> str:
    """Recheck every touched object, property, and relation before a Scenario write.

    Scenario snapshots are isolated, but their data still carries the live
    ontology's access policy.  The compiler can therefore prepare an edit only
    after the same server-side projection used by Object Query allows it.
    """
    project, context, policies = _authorization_state(db, ontology_id, actor)

    def projection(records):
        result = resolve_projection(db, context, preloaded_policies=policies, records=records)
        if not result.allowed:
            raise ScenarioError("forbidden", "当前用户无权访问 Action 涉及的数据", 403)
        return result

    def object_record(key, props=None):
        object_type, object_id = key
        entity = _entity_for_type(db, ontology_id, object_type)
        if entity is None:
            raise ScenarioError("object_type_not_found", "Action 对象类型不在当前本体中", 409)
        resource = db.get(OntologySemanticResource, entity.semantic_resource_id) if entity.semantic_resource_id else None
        if resource and resource.kind == "interface":
            raise ScenarioError("interface_not_instantiable", "Interface 不能作为 Scenario 对象", 422)
        properties = dict(props if props is not None else snapshot.objects.get(key, {}))
        record = {"id": str(object_id), "entity_id": entity.id,
                  "entity_type": entity.name_en or entity.name_cn,
                  "object_type_resource_id": entity.semantic_resource_id,
                  "properties": properties}
        current = projection([record])
        if redact_record(current, record) is None:
            raise ScenarioError("forbidden", "Action 对象对当前用户不可见", 403)
        return record, current

    def link_record(link):
        record = _link_access_record(db, ontology_id, project, link)
        if record is None:
            raise ScenarioError("link_type_not_found", "Action 关系类型不在当前本体中", 409)
        return record

    working = {key: dict(value) for key, value in snapshot.objects.items()}
    edges = set(snapshot.edges)
    permission_digest = projection([]).permission_digest
    for edit in sorted(edits, key=lambda item: item.sequence):
        if edit.op == "invoke_action":
            continue
        if edit.op == "create_object":
            if edit.target is None or edit.object_type != edit.target.concrete_type:
                raise ScenarioError("invalid_object_type", "新对象的 Action 类型与目标类型不一致", 422)
            key = (edit.object_type or "", edit.target.object_id if edit.target else "")
            if key in working:
                # Apply the read policy before returning a collision error so
                # a guessed ID cannot reveal a hidden object's existence.
                object_record(key, working[key])
                if not allow_create_collisions:
                    raise ScenarioError(
                        "object_already_exists",
                        "Action cannot create an object over an existing object",
                        409,
                    )
            record, current = object_record(key, edit.properties or {})
            for name in (record["properties"] or {}):
                if not action_field_permitted(current, record, str(name)):
                    raise ScenarioError("forbidden_field", "Action 无权写入目标对象字段", 403)
            working[key] = dict(record["properties"])
            continue

        if edit.op in {"set_property", "unset_property", "delete_object"}:
            key = (edit.target.concrete_type, edit.target.object_id)
            if key not in working:
                raise ScenarioError("object_not_found", "Action 对象对当前用户不可见", 404)
            record, current = object_record(key, working[key])
            if edit.op in {"set_property", "unset_property"}:
                if not action_field_permitted(current, record, edit.property):
                    raise ScenarioError("forbidden_field", "Action 无权读取或写入目标字段", 403)
                if edit.op == "set_property":
                    working[key][edit.property] = edit.value
                else:
                    working[key].pop(edit.property, None)
            else:
                for name in working[key]:
                    if not action_property_name_allowed(name):
                        continue
                    if not action_field_permitted(current, record, str(name)):
                        raise ScenarioError("forbidden_field", "Action 无权删除包含受限字段的对象", 403)
                incident = [edge for edge in edges if key in (edge[0], edge[2])]
                for source, relation_type, target in incident:
                    link = LinkTarget(
                        ontology_id=ontology_id, relation_type=relation_type,
                        source=Target(ontology_id=ontology_id, concrete_type=source[0], object_id=source[1]),
                        target=Target(ontology_id=ontology_id, concrete_type=target[0], object_id=target[1]),
                    )
                    link_projection = projection([object_record(source)[0], object_record(target)[0]])
                    record = link_record(link)
                    if redact_link_record(link_projection, record, link_type_id=record["link_type_id"]) is None:
                        raise ScenarioError("forbidden_link", "Action 无权删除对象关联的受限关系", 403)
                working.pop(key)
                edges.difference_update(incident)
            permission_digest = current.permission_digest
            continue

        if edit.op in {"add_link", "remove_link"}:
            link = edit.link
            source_key = (link.source.concrete_type, link.source.object_id)
            target_key = (link.target.concrete_type, link.target.object_id)
            if source_key not in working or target_key not in working:
                raise ScenarioError("object_not_found", "Action 关系端点对当前用户不可见", 404)
            source_record, source_projection = object_record(source_key, working[source_key])
            target_record, target_projection = object_record(target_key, working[target_key])
            link_projection = projection([source_record, target_record])
            record = link_record(link)
            if redact_link_record(link_projection, record, link_type_id=record["link_type_id"]) is None:
                raise ScenarioError("forbidden_link", "当前用户无权查看或修改此关系", 403)
            edge = (source_key, link.relation_type, target_key)
            if edit.op == "add_link":
                edges.add(edge)
            else:
                edges.discard(edge)
            permission_digest = link_projection.permission_digest
            continue

        raise ScenarioError("unsupported_edit", f"Unsupported compiled Action edit: {edit.op}", 422)

    return permission_digest
