"""Apply compiled live edits to the canonical instance projection.

The graph is the query surface and EntityInstance is its SQL companion.  A
failed SQL commit is compensated in the graph so Action runs do not report a
successful write to a different object store.
"""
from __future__ import annotations

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.services.v2.object_query.core import FalkorReadAdapter
from app.services.v2.scenarios import ScenarioError
from app.services.v2.action_runtime import reauthorize_edit_batch


def apply_live_edits(
    db,
    ontology_id,
    edits,
    graph_service,
    *,
    actor,
    expected_permission_digest: str | None,
):
    if not graph_service.available:
        raise ScenarioError("graph_unavailable", "FalkorDB is unavailable", 503, "execution")
    graph = graph_service._graph(ontology_id)
    snapshot = FalkorReadAdapter(graph).read(ontology_id)
    reauthorize_edit_batch(
        db,
        ontology_id=ontology_id,
        actor=actor,
        snapshot=snapshot,
        edits=edits,
        expected_permission_digest=expected_permission_digest,
    )
    objects = {key: dict(props) for key, props in snapshot.objects.items()}
    edges = set(snapshot.edges)
    before, after, results, undo = {}, {}, [], []

    def set_property(kind, object_id, name, value):
        graph.query(
            "MATCH (n:Instance {_instance_id: $id, _type: $kind, _ontology_id: $ontology}) SET n += $patch",
            params={"id": object_id, "kind": kind, "ontology": ontology_id, "patch": {name: value}},
        )

    def delete_object(kind, object_id):
        graph.query(
            "MATCH (n:Instance {_instance_id: $id, _type: $kind, _ontology_id: $ontology}) DETACH DELETE n",
            params={"id": object_id, "kind": kind, "ontology": ontology_id},
        )

    def add_link(link):
        graph_service.upsert_relations(ontology_id, [{"source": link.source.object_id,
            "target": link.target.object_id, "type": link.relation_type}])

    def remove_link(link):
        relation = graph_service._safe_relation_type(link.relation_type)
        graph.query(
            "MATCH (a:Instance {_instance_id: $source, _ontology_id: $ontology})"
            f"-[r:{relation}]->(b:Instance {{_instance_id: $target, _ontology_id: $ontology}}) DELETE r",
            params={"source": link.source.object_id, "target": link.target.object_id, "ontology": ontology_id},
        )

    def restore():
        for item in reversed(undo):
            try:
                op, args = item
                if op == "set":
                    set_property(*args)
                elif op == "delete":
                    delete_object(*args)
                elif op == "restore":
                    kind, object_id, props, edges = args
                    graph_service.upsert_instances(ontology_id, [{"id": object_id, "entity_type": kind, "properties": props}])
                    for source, relation, target in edges:
                        graph_service.upsert_relations(ontology_id, [{"source": source[1], "target": target[1], "type": relation}])
                elif op == "add_link":
                    add_link(args)
                elif op == "remove_link":
                    remove_link(args)
            except Exception:
                # Original failure is retained; an operator can replay the
                # recorded Action run if graph compensation also fails.
                pass

    try:
        for edit in edits:
            op = edit.op
            if op == "invoke_action":
                continue
            target = edit.target
            key = (target.concrete_type, target.object_id) if target else None
            if op in {"set_property", "unset_property", "delete_object"}:
                if key not in objects:
                    raise ScenarioError("object_not_found", "Action target is not in the live object projection", 404, "edits")
                row = db.query(EntityInstance).filter_by(
                    ontology_id=ontology_id,
                    id=target.object_id,
                    is_deleted=False,
                ).with_for_update().first()
                if not row:
                    raise ScenarioError("projection_conflict", "Live object has no SQL instance", 409, "edits")
                props = dict(row.row_data or {})
                before.setdefault(target.object_id, dict(props))
                if op in {"set_property", "unset_property"}:
                    old = props.get(edit.property)
                    if "expected_old_value" in edit.model_fields_set and old != edit.expected_old_value:
                        raise ScenarioError("edit_conflict", "Target property changed since Action preview", 409, "edits")
                    if objects[key].get(edit.property) != old:
                        raise ScenarioError("projection_conflict", "Graph and SQL disagree on target property", 409, "edits")
                    undo.append(("set", (target.concrete_type, target.object_id, edit.property, objects[key].get(edit.property))))
                    if op == "set_property":
                        props[edit.property] = edit.value
                    else:
                        props.pop(edit.property, None)
                    row.row_data = props
                    set_property(target.concrete_type, target.object_id, edit.property, edit.value if op == "set_property" else None)
                    objects[key][edit.property] = edit.value if op == "set_property" else None
                    after[target.object_id] = props
                else:
                    incident = [edge for edge in edges if key in (edge[0], edge[2])]
                    undo.append(("restore", (target.concrete_type, target.object_id, objects[key], incident)))
                    db.delete(row)
                    delete_object(target.concrete_type, target.object_id)
                    objects.pop(key)
                    edges.difference_update(incident)
                results.append({"op": op, "target_id": target.object_id})
            elif op == "create_object":
                if key in objects or db.get(EntityInstance, target.object_id):
                    raise ScenarioError("edit_conflict", "Object ID already exists", 409, "edits")
                entity = db.query(Entity).filter(Entity.ontology_id == ontology_id,
                    (Entity.name_en == edit.object_type) | (Entity.name_cn == edit.object_type)).first()
                if not entity:
                    raise ScenarioError("object_type_not_found", "Published object type is unavailable", 409, "edits")
                props = dict(edit.properties or {})
                db.add(EntityInstance(id=target.object_id, entity_id=entity.id, ontology_id=ontology_id,
                                      row_identity=target.object_id, row_data=props))
                undo.append(("delete", (edit.object_type, target.object_id)))
                graph_service.upsert_instances(ontology_id, [{"id": target.object_id, "entity_type": edit.object_type, "properties": props}])
                objects[key] = props
                after[target.object_id] = props
                results.append({"op": op, "target_id": target.object_id})
            elif op in {"add_link", "remove_link"}:
                link = edit.link
                source = (link.source.concrete_type, link.source.object_id)
                target_key = (link.target.concrete_type, link.target.object_id)
                if source not in objects or target_key not in objects:
                    raise ScenarioError("object_not_found", "Link endpoint is not in the live projection", 404, "edits")
                edge = (source, graph_service._safe_relation_type(link.relation_type), target_key)
                exists = edge in edges
                if op == "add_link" and not exists:
                    undo.append(("remove_link", link)); add_link(link); edges.add(edge)
                elif op == "remove_link" and exists:
                    undo.append(("add_link", link)); remove_link(link); edges.discard(edge)
                results.append({"op": op, "relation_type": link.relation_type})
            else:
                raise ScenarioError("unsupported_edit", f"Unsupported compiled edit: {op}", 422, "edits")
        return before, after, results, restore
    except Exception:
        restore()
        raise


def reconcile_live_edits(
    db,
    ontology_id,
    edits,
    graph_service,
    base_snapshot,
    *,
    actor,
    expected_permission_digest: str | None,
):
    """Idempotently converge SQL object rows and the graph to a pinned edit batch.

    A Scenario merge may stop after a graph mutation but before its SQL commit.
    This routine accepts only states that can be produced by a prefix of the
    immutable edit list, then writes the batch's final state to both stores.
    Unrelated fields on existing objects are preserved.
    """
    if not graph_service.available:
        raise ScenarioError("graph_unavailable", "FalkorDB is unavailable", 503, "execution")

    def object_key(target):
        if target is None or target.ontology_id != ontology_id:
            raise ScenarioError("invalid_target", "Action target does not belong to this Ontology", 409, "edits")
        return target.concrete_type, target.object_id

    object_edits = {}
    edge_edits = {}
    object_ids = set()
    for edit in sorted(edits, key=lambda item: item.sequence):
        if edit.op in {"set_property", "unset_property", "create_object", "delete_object"}:
            key = object_key(edit.target)
            object_edits.setdefault(key, []).append(edit)
            object_ids.add(key[1])
        elif edit.op in {"add_link", "remove_link"}:
            if edit.link is None or edit.link.ontology_id != ontology_id:
                raise ScenarioError("invalid_target", "Action link does not belong to this Ontology", 409, "edits")
            source = object_key(edit.link.source)
            target = object_key(edit.link.target)
            relation = graph_service._safe_relation_type(edit.link.relation_type)
            key = (source, relation, target)
            edge_edits.setdefault(key, []).append(edit)
        elif edit.op != "invoke_action":
            raise ScenarioError("unsupported_edit", f"Unsupported recovery edit: {edit.op}", 422, "edits")

    # Acquire canonical row locks in a stable order before reading the graph.
    rows = db.query(EntityInstance).filter(
        EntityInstance.id.in_(sorted(object_ids)) if object_ids else False,
    ).order_by(EntityInstance.id).with_for_update().all()
    rows_by_id = {row.id: row for row in rows}
    if any(row.ontology_id != ontology_id for row in rows):
        raise ScenarioError("invalid_target", "Action target belongs to another Ontology", 409, "edits")
    graph = graph_service._graph(ontology_id)
    live = FalkorReadAdapter(graph).read(ontology_id)
    # Recovery may observe a valid prefix that already removed a target from
    # the graph. Re-evaluate current policy against the pinned pre-write base,
    # then validate the live graph against the allowed prefixes below.
    reauthorize_edit_batch(
        db,
        ontology_id=ontology_id,
        actor=actor,
        snapshot=base_snapshot,
        edits=edits,
        expected_permission_digest=expected_permission_digest,
    )

    def public(properties):
        return {key: value for key, value in (properties or {}).items() if not key.startswith("_")}

    def same_value(left, right):
        # Action edits intentionally use dict.get semantics for null/missing.
        return left == right

    plans = {}
    for key, items in object_edits.items():
        state = dict(base_snapshot.objects[key]) if key in base_snapshot.objects else None
        allowed_object_states = [None if state is None else public(state)]
        allowed_property_values = {}
        final_property_ops = {}
        for edit in items:
            if edit.op == "create_object":
                if state is not None:
                    raise ScenarioError("merge_manifest_conflict", "Create target existed in the pinned Scenario base", 409, "edits")
                state = dict(edit.properties or {})
                allowed_object_states.append(public(state))
            elif edit.op in {"set_property", "unset_property"}:
                if state is None:
                    raise ScenarioError("merge_manifest_conflict", "Property edit targets an object before it is created", 409, "edits")
                prop = edit.property
                if not prop:
                    raise ScenarioError("merge_manifest_conflict", "Property edit has no property name", 409, "edits")
                current = state.get(prop)
                if not same_value(current, edit.expected_old_value):
                    raise ScenarioError("merge_manifest_conflict", "Pinned edit sequence has inconsistent old values", 409, "edits")
                allowed_property_values.setdefault((key, prop), [current])
                if edit.op == "set_property":
                    state[prop] = edit.value
                else:
                    state.pop(prop, None)
                allowed_property_values[(key, prop)].append(state.get(prop))
                final_property_ops[prop] = edit.op
                allowed_object_states.append(public(state))
            elif edit.op == "delete_object":
                if state is None:
                    raise ScenarioError("merge_manifest_conflict", "Delete target is absent from the pinned Scenario state", 409, "edits")
                state = None
                allowed_object_states.append(None)
        plans[key] = {
            "items": items,
            "initial_exists": key in base_snapshot.objects,
            "final": None if state is None else dict(state),
            "allowed_object_states": allowed_object_states,
            "allowed_property_values": allowed_property_values,
            "final_property_ops": final_property_ops,
        }

    def edge_key(source, relation, target):
        return (source, graph_service._safe_relation_type(relation), target)

    live_edges = {edge_key(source, relation, target) for source, relation, target in live.edges}
    base_edges = {edge_key(source, relation, target) for source, relation, target in base_snapshot.edges}
    final_edges = set(live_edges)
    allowed_edge_states = {}
    for key, items in edge_edits.items():
        state = key in base_edges
        allowed = [state]
        for edit in items:
            if edit.op == "add_link":
                state = True
            else:
                state = False
            allowed.append(state)
        if state and (plans.get(key[0], {}).get("final") is None or plans.get(key[2], {}).get("final") is None):
            raise ScenarioError("merge_manifest_conflict", "Final link points to an object deleted by the same Action", 409, "edits")
        allowed_edge_states[key] = set(allowed)
        if state:
            final_edges.add(key)
        else:
            final_edges.discard(key)

    # First validate every current state. No graph write starts until all
    # manifest targets, versions, and partial-effect prefixes are understood.
    entity_by_type = {}
    for key, plan in plans.items():
        object_id = key[1]
        row = rows_by_id.get(object_id)
        create_type = next((edit.object_type for edit in plan["items"]
                            if edit.op == "create_object" and edit.object_type), key[0])
        lookup_type = create_type if not plan["initial_exists"] else key[0]
        if lookup_type not in entity_by_type:
            entity_rows = db.query(Entity).filter(
                Entity.ontology_id == ontology_id,
                (Entity.id == lookup_type) | (Entity.name_en == lookup_type)
                | (Entity.name_cn == lookup_type) | (Entity.semantic_resource_id == lookup_type),
            ).limit(2).all()
            if len(entity_rows) != 1:
                code = "object_type_not_found" if not entity_rows else "ambiguous_object_type"
                raise ScenarioError(code, "Recovery object type is unavailable or ambiguous", 409, "edits")
            entity_by_type[lookup_type] = entity_rows[0]
        entity = entity_by_type[lookup_type]
        if row is not None and row.entity_id != entity.id:
            raise ScenarioError(
                "merge_reconciliation_conflict",
                "Canonical SQL row belongs to a different object type",
                409,
                "edits",
            )
        if row is not None and row.is_deleted and not plan["initial_exists"]:
            raise ScenarioError(
                "merge_reconciliation_conflict",
                "A tombstoned object ID cannot be reused during Action recovery",
                409,
                "edits",
            )
        if row is not None and row.is_deleted:
            row = None
        graph_props = live.objects.get(key)
        final = plan["final"]
        if plan["initial_exists"] and final is not None:
            if row is None or graph_props is None:
                raise ScenarioError("merge_reconciliation_conflict", "A live object disappeared during Action recovery", 409, "edits")
            sql_props = dict(row.row_data or {})
            projected = dict(graph_props or {})
            for (prop_key, prop), allowed in plan["allowed_property_values"].items():
                if prop_key != key:
                    continue
                if sql_props.get(prop) not in allowed or projected.get(prop) not in allowed:
                    raise ScenarioError("merge_reconciliation_conflict", "An edited property changed after the merge intent", 409, "edits")
        elif final is not None:
            allowed_states = plan["allowed_object_states"]
            if row is not None and public(row.row_data or {}) not in allowed_states:
                raise ScenarioError("merge_reconciliation_conflict", "A created object has an unrecognized SQL state", 409, "edits")
            if graph_props is not None and public(graph_props) not in allowed_states:
                raise ScenarioError("merge_reconciliation_conflict", "A created object has an unrecognized graph state", 409, "edits")
        else:
            # Deletes are approved only when the full business object matched
            # the pinned base. A remaining copy must still be a valid prefix.
            for properties in ((row.row_data or {}) if row is not None else None, graph_props):
                if properties is not None and public(properties) not in plan["allowed_object_states"]:
                    raise ScenarioError("merge_reconciliation_conflict", "A deleted object changed after the merge intent", 409, "edits")

    for key, allowed in allowed_edge_states.items():
        if (key in live_edges) not in allowed:
            raise ScenarioError("merge_reconciliation_conflict", "A linked edge changed after the merge intent", 409, "edits")

    # Converge object rows first so links can safely target newly created nodes.
    for key, plan in plans.items():
        final = plan["final"]
        row = rows_by_id.get(key[1])
        if row is not None and row.is_deleted:
            row = None
        if final is None:
            continue
        if plan["initial_exists"]:
            if row is None:
                raise ScenarioError(
                    "merge_reconciliation_conflict",
                    "A live object disappeared before Action recovery completed",
                    409,
                    "edits",
                )
            row_data = dict(row.row_data or {})
            for prop, operation in plan["final_property_ops"].items():
                if operation == "unset_property":
                    row_data.pop(prop, None)
                else:
                    row_data[prop] = final.get(prop)
            row.row_data = row_data
        else:
            entity_type = create_type
            if row is None:
                row = EntityInstance(
                    id=key[1], entity_id=entity.id, ontology_id=ontology_id,
                    row_identity=key[1], row_data=dict(final),
                )
                db.add(row)
                rows_by_id[key[1]] = row
            else:
                row.row_data = dict(final)
            plan["entity_type"] = entity_type

    # Each graph operation is set-to-state and can be safely repeated after a
    # worker/request crash. SQL mutations remain in the caller's transaction.
    for key, plan in plans.items():
        final = plan["final"]
        row = rows_by_id.get(key[1])
        if row is not None and row.is_deleted:
            row = None
        if final is not None:
            if plan["initial_exists"]:
                for prop, operation in plan["final_property_ops"].items():
                    graph.query(
                        "MATCH (n:Instance {_instance_id: $id, _type: $kind, _ontology_id: $ontology}) SET n += $patch",
                        params={"id": key[1], "kind": key[0], "ontology": ontology_id,
                                "patch": {prop: None if operation == "unset_property" else final.get(prop)}},
                    )
            else:
                if graph_service.upsert_instances(ontology_id, [{
                    "id": key[1], "entity_type": plan.get("entity_type", key[0]),
                    "properties": dict(final),
                }]) != 1:
                    raise ScenarioError("projection_failed", "Graph did not accept a created object", 503, "execution")

    for key, allowed in allowed_edge_states.items():
        source, relation, target = key
        final_exists = key in final_edges
        exists = key in live_edges
        if exists == final_exists:
            continue
        if final_exists:
            if graph_service.upsert_relations(ontology_id, [{
                "source": source[1], "target": target[1], "type": relation,
            }]) != 1:
                raise ScenarioError("projection_failed", "Graph did not accept a created relationship", 503, "execution")
        else:
            graph.query(
                "MATCH (a:Instance {_instance_id: $source, _ontology_id: $ontology})"
                f"-[r:{relation}]->(b:Instance {{_instance_id: $target, _ontology_id: $ontology}}) DELETE r",
                params={"source": source[1], "target": target[1], "ontology": ontology_id},
            )

    # Deletions run last, matching the terminal desired state after all links.
    for key, plan in plans.items():
        if plan["final"] is not None:
            continue
        row = rows_by_id.get(key[1])
        if row is not None and not row.is_deleted:
            db.delete(row)
        graph.query(
            "MATCH (n:Instance {_instance_id: $id, _type: $kind, _ontology_id: $ontology}) DETACH DELETE n",
            params={"id": key[1], "kind": key[0], "ontology": ontology_id},
        )
    return {"objects_reconciled": len(plans), "relationships_reconciled": len(edge_edits)}
