"""Three-way Tracking Scenario rebase and authorized conflict resolution."""
from copy import deepcopy
from uuid import uuid4

from app.models.v2.query_view import QueryDataView
from app.models.v2.scenario import (
    ScenarioAudit,
    ScenarioChangeSet,
    ScenarioResource,
    ScenarioRevision,
)
from app.schemas.v2.scenario import Edit, LinkTarget, RevisionRequest, Target
from app.services.v2.object_query.core import FalkorReadAdapter
from app.services.v2.object_query.data_views import build_live_view, now, require_ready
from app.services.v2.object_query.metadata import load_sql_metadata
from app.services.v2.object_query.normalize import stable_hash
from app.services.v2.scenarios import ScenarioError


def _graph_digest(graph):
    return stable_hash({"objects": sorted(graph.objects.items()), "edges": sorted(graph.edges)})


def _packed(value, missing):
    return {"present": False} if value is missing else {"present": True, "value": deepcopy(value)}


def rebase_edits(ontology_id, base, candidate, live, preserved=(), *,
                 context_digest="", resolutions=None):
    """Keep non-overlapping deltas and expose opaque IDs for overlapping edits."""
    missing = object()
    edits, conflicts = [], []
    resolutions = resolutions or {}
    discarded_created_objects = set()

    def target(key):
        return Target(ontology_id=ontology_id, concrete_type=key[0], object_id=key[1])

    def add(**kwargs):
        edits.append(Edit(sequence=len(edits), **kwargs))

    def public(values):
        return {key: value for key, value in values.items() if not str(key).startswith("_")}

    def conflict(reason, identity, details, choices):
        conflict_id = stable_hash({"context_digest": context_digest,
                                   "reason": reason, "identity": identity})[:32]
        record = {"conflict_id": conflict_id, "reason": reason,
                  "allowed_choices": list(choices), **details}
        choice = resolutions.get(conflict_id)
        if choice is None or choice not in choices:
            conflicts.append(record)
            return None
        return choice

    def set_property(key, property_name, value, old):
        op = "unset_property" if value is missing else "set_property"
        add(op=op, target=target(key), property=property_name,
            value=None if value is missing else deepcopy(value),
            expected_old_value=None if old is missing else deepcopy(old))

    preserved_properties = {
        ((item.get("target") or {}).get("concrete_type"),
         (item.get("target") or {}).get("object_id"), item.get("property")): item
        for item in preserved if item.get("op") in {"set_property", "unset_property"}
    }

    for key in sorted(set(base.objects) | set(candidate.objects)):
        before = public(base.objects[key]) if key in base.objects else missing
        after = public(candidate.objects[key]) if key in candidate.objects else missing
        current = public(live.objects[key]) if key in live.objects else missing
        if before == after and not any(item[:2] == key for item in preserved_properties):
            continue
        if before is missing:
            if current is not missing:
                choice = conflict(
                    "created_object_collision",
                    {"object_type": key[0], "object_id": key[1],
                     "scenario": _packed(after, missing), "live": _packed(current, missing)},
                    {"object_type": key[0], "object_id": key[1],
                     "base_value": _packed(before, missing),
                     "scenario_value": _packed(after, missing),
                     "live_value": _packed(current, missing)},
                    ["keep_live"],
                )
                if choice == "keep_live":
                    discarded_created_objects.add(key)
            else:
                add(op="create_object", target=target(key), object_type=key[0],
                    properties=deepcopy(after))
        elif after is missing:
            if current is missing:
                continue
            incoming = {edge for edge in live.edges if key in (edge[0], edge[2])}
            original = {edge for edge in base.edges if key in (edge[0], edge[2])}
            if current != before or incoming != original:
                choice = conflict(
                    "deleted_object_changed",
                    {"object_type": key[0], "object_id": key[1],
                     "base": _packed(before, missing), "live": _packed(current, missing),
                     "base_edges": sorted(original, key=repr),
                     "live_edges": sorted(incoming, key=repr)},
                    {"object_type": key[0], "object_id": key[1],
                     "base_value": _packed(before, missing),
                     "scenario_value": _packed(after, missing),
                     "live_value": _packed(current, missing)},
                    ["keep_scenario", "keep_live"],
                )
                if choice == "keep_scenario":
                    add(op="delete_object", target=target(key))
            else:
                add(op="delete_object", target=target(key))
        elif current is missing:
            choice = conflict(
                "target_deleted",
                {"object_type": key[0], "object_id": key[1],
                 "base": _packed(before, missing), "scenario": _packed(after, missing)},
                {"object_type": key[0], "object_id": key[1],
                 "base_value": _packed(before, missing),
                 "scenario_value": _packed(after, missing),
                 "live_value": _packed(current, missing)},
                ["keep_scenario", "keep_live"],
            )
            if choice == "keep_scenario":
                add(op="create_object", target=target(key), object_type=key[0],
                    properties=deepcopy(after))
        else:
            for prop in sorted(set(before) | set(after) | {
                item[2] for item in preserved_properties if item[:2] == key
            }):
                old = before.get(prop, missing)
                new = after.get(prop, missing)
                actual = current.get(prop, missing)
                explicit = preserved_properties.get((key[0], key[1], prop))
                if old == new and not explicit:
                    continue
                if actual == new:
                    if explicit:
                        set_property(key, prop, new, actual)
                    continue
                if actual != old:
                    choice = conflict(
                        "property_changed",
                        {"object_type": key[0], "object_id": key[1], "property": prop,
                         "base": _packed(old, missing), "scenario": _packed(new, missing),
                         "live": _packed(actual, missing)},
                        {"object_type": key[0], "object_id": key[1], "property": prop,
                         "base_value": _packed(old, missing),
                         "scenario_value": _packed(new, missing),
                         "live_value": _packed(actual, missing)},
                        ["keep_scenario", "keep_live"],
                    )
                    if choice == "keep_scenario":
                        set_property(key, prop, new, actual)
                else:
                    set_property(key, prop, new, actual)

    base_edges, candidate_edges, live_edges = set(base.edges), set(candidate.edges), set(live.edges)
    created = {key for key in candidate.objects if key not in base.objects}
    deleted = {key for key in base.objects if key not in candidate.objects}
    available = (set(live.objects) | created) - deleted
    for op, edges in (("add_link", candidate_edges - base_edges),
                      ("remove_link", base_edges - candidate_edges)):
        for source, relation, dest in sorted(edges, key=repr):
            edge = (source, relation, dest)
            if source in discarded_created_objects or dest in discarded_created_objects:
                continue
            if (op == "add_link" and edge in live_edges) or (op == "remove_link" and edge not in live_edges):
                continue
            if source in deleted or dest in deleted:
                continue
            if source not in available or dest not in available:
                conflict(
                    "link_endpoint_deleted",
                    {"relation": relation, "source": source, "target": dest,
                     "operation": op, "base_present": edge in base_edges,
                     "scenario_present": edge in candidate_edges,
                     "live_present": edge in live_edges},
                    {"relation_type": relation, "operation": op},
                    ["keep_live"],
                )
            else:
                add(op=op, link=LinkTarget(
                    ontology_id=ontology_id, relation_type=relation,
                    source=target(source), target=target(dest),
                ))
    return edits, conflicts


def _read_view(service, view_id):
    view = service.db.get(QueryDataView, view_id)
    if not view or view.ontology_id != service.ontology_id:
        raise ScenarioError("view_unavailable", "Tracking Scenario snapshot is unavailable", 409)
    try:
        view = require_ready(service.db, view.id, service.ontology_id,
                             principal_id=view.created_by, is_admin=False)
    except Exception as exc:
        raise ScenarioError("view_unavailable", "Tracking Scenario snapshot is unavailable", 409) from exc
    if not service.graph.available:
        raise ScenarioError("graph_unavailable", "FalkorDB is unavailable", 503)
    data = FalkorReadAdapter(
        service.graph._graph(view.graph_key), graph_ontology_id=view.graph_key,
    ).read(service.ontology_id)
    return view, data


def _head_view(service, scenario):
    if scenario.head_revision == 0:
        return _read_view(service, scenario.base_view_id)
    revision = service.db.query(ScenarioRevision).filter_by(
        scenario_id=scenario.id, revision=scenario.head_revision, status="ready",
    ).first()
    if revision is None:
        raise ScenarioError("revision_not_found", "Tracking Scenario head is unavailable", 409)
    return _read_view(service, revision.result_view_id)


def _context_digest(scenario, base_view, candidate_view, live_view):
    return stable_hash({
        "scenario_id": scenario.id,
        "scenario_etag": scenario.etag,
        "head_revision": scenario.head_revision,
        "old_base_view_id": base_view.id,
        "candidate_view_id": candidate_view.id,
        "live_view_id": live_view.id,
        "base_content_digest": base_view.content_digest,
        "candidate_content_digest": candidate_view.content_digest,
        "live_content_digest": live_view.content_digest,
    })


def _load_conflict_context(service, scenario, state):
    if (state.get("scenario_etag") != scenario.etag
            or state.get("head_revision") != scenario.head_revision
            or state.get("old_base_view_id") != scenario.base_view_id):
        raise ScenarioError("rebase_conflict_stale", "Scenario changed; run rebase again before resolving conflicts", 409)
    base_view, base = _read_view(service, state["old_base_view_id"])
    if scenario.head_revision:
        head = service.db.query(ScenarioRevision).filter_by(
            scenario_id=scenario.id, revision=scenario.head_revision, status="ready",
        ).first()
        if head is None:
            raise ScenarioError("revision_not_found", "Tracking Scenario head is unavailable", 409)
        candidate_view, candidate = _read_view(service, head.result_view_id)
        changeset = service.db.get(ScenarioChangeSet, head.changeset_id) if head.changeset_id else None
    else:
        candidate_view, candidate, changeset = base_view, base, None
    live_view, live = _read_view(service, state["live_view_id"])
    context_digest = _context_digest(scenario, base_view, candidate_view, live_view)
    if context_digest != state.get("context_digest"):
        raise ScenarioError("rebase_conflict_stale", "Pinned rebase inputs changed; run rebase again", 409)
    preserved = changeset.ordered_edits if changeset and (changeset.validation_report or {}).get("replace_all") else []
    edits, conflicts = rebase_edits(
        service.ontology_id, base, candidate, live, preserved,
        context_digest=context_digest,
    )
    conflict_digest = stable_hash({
        "context_digest": context_digest,
        "conflict_ids": sorted(item["conflict_id"] for item in conflicts),
    })
    if conflict_digest != state.get("conflict_digest"):
        raise ScenarioError("rebase_conflict_stale", "Conflict set changed; run rebase again", 409)
    return base_view, base, candidate_view, candidate, live_view, live, edits, conflicts, context_digest


def _scenario_side_edit(ontology_id, conflict):
    reason = conflict["reason"]
    if reason == "property_changed":
        target = Target(ontology_id=ontology_id,
                        concrete_type=conflict["object_type"],
                        object_id=conflict["object_id"])
        candidate = conflict["scenario_value"]
        actual = conflict["live_value"]
        return Edit(
            sequence=0,
            op="set_property" if candidate["present"] else "unset_property",
            target=target,
            property=conflict["property"],
            value=candidate.get("value"),
            expected_old_value=actual.get("value") if actual.get("present") else None,
        )
    if reason == "deleted_object_changed":
        return Edit(sequence=0, op="delete_object", target=Target(
            ontology_id=ontology_id, concrete_type=conflict["object_type"],
            object_id=conflict["object_id"],
        ))
    if reason == "target_deleted":
        candidate = conflict["scenario_value"].get("value") or {}
        return Edit(sequence=0, op="create_object", target=Target(
            ontology_id=ontology_id, concrete_type=conflict["object_type"],
            object_id=conflict["object_id"],
        ), object_type=conflict["object_type"], properties=candidate)
    return None


def _visible_conflicts(service, live, conflicts):
    from app.services.v2.action_authorization import authorize_compiled_edits

    visible = []
    for conflict in conflicts:
        choices = list(conflict["allowed_choices"])
        edit = _scenario_side_edit(service.ontology_id, conflict) if "keep_scenario" in choices else None
        can_show_scenario = edit is not None
        if edit is not None:
            try:
                authorize_compiled_edits(service.db, service.ontology_id,
                                         service.user, live, [edit])
            except ScenarioError:
                can_show_scenario = False
        if not can_show_scenario and "keep_scenario" in choices:
            choices.remove("keep_scenario")
        if not can_show_scenario:
            visible.append({
                "conflict_id": conflict["conflict_id"],
                "reason": conflict["reason"],
                "summary": "情景侧内容当前不可展示或不可应用；可以保留实时状态。",
                "allowed_choices": choices,
            })
            continue
        visible.append({
            "conflict_id": conflict["conflict_id"],
            "reason": conflict["reason"],
            "summary": (
                "对象 ID 已被实时数据占用；保留实时侧会放弃情景新对象及依赖它的新关系。"
                if conflict["reason"] == "created_object_collision"
                else "基础、情景与实时状态在此处有冲突。"
            ),
            "allowed_choices": choices,
            **{key: conflict[key] for key in (
                "object_type", "object_id", "property", "relation_type", "operation",
                "base_value", "scenario_value", "live_value",
            ) if key in conflict},
        })
    return visible


def rebase(service, scenario_id, expected_etag):
    db = service.db
    scenario = service.get(scenario_id, write=True)
    if scenario.mode != "tracking" or scenario.protected_demo or scenario.status != "active":
        raise ScenarioError("rebase_unavailable", "Only active Tracking Scenarios can rebase", 409)
    if scenario.etag != expected_etag:
        raise ScenarioError("etag_conflict", "Scenario changed before rebase", 409)
    if not service.graph.available:
        raise ScenarioError("graph_unavailable", "FalkorDB is unavailable", 503)

    old_base_id, old_revision = scenario.base_view_id, scenario.head_revision
    base_view, base = _read_view(service, old_base_id)
    candidate_view, candidate = _head_view(service, scenario)
    live = FalkorReadAdapter(service.graph._graph(service.ontology_id)).read(service.ontology_id)
    if base.objects == live.objects and set(base.edges) == set(live.edges):
        from app.services.v2.action_authorization import authorize_compiled_edits
        permission_digest = authorize_compiled_edits(
            db, service.ontology_id, service.user, live, [])
        scenario.last_rebased_at = now()
        scenario.rebase_error = None
        db.add(ScenarioAudit(
            scenario_id=scenario.id,
            actor_kind=service.execution_actor_kind,
            actor_id=service.execution_actor_id,
            operation="rebase_unchanged",
            summary={"base_view_id": old_base_id,
                     "authorization": service.authorization_provenance(
                         scenario.id, permission_digest)},
        ))
        db.commit()
        return {"status": "unchanged", "revision": old_revision,
                "base_view_id": old_base_id, "conflicts": []}

    live_view = build_live_view(
        db, service.graph, load_sql_metadata(db, service.ontology_id),
        service.ontology_id, service.user.id,
        f"tracking:{scenario.id}:{uuid4()}", scenario.ttl_seconds or 30 * 86400,
    )
    scenario = (db.query(ScenarioResource).filter_by(id=scenario_id)
                .populate_existing().with_for_update().one())
    service.get(scenario_id, write=True)
    if (scenario.etag != expected_etag or scenario.base_view_id != old_base_id
            or scenario.head_revision != old_revision):
        raise ScenarioError("etag_conflict", "Scenario changed during live capture", 409)
    live_view, live_snapshot = _read_view(service, live_view.id)
    current_head = db.query(ScenarioRevision).filter_by(
        scenario_id=scenario.id, revision=old_revision,
    ).first() if old_revision else None
    previous = db.get(ScenarioChangeSet, current_head.changeset_id) if current_head and current_head.changeset_id else None
    preserved = previous.ordered_edits if previous and (previous.validation_report or {}).get("replace_all") else []
    context_digest = _context_digest(scenario, base_view, candidate_view, live_view)
    edits, conflicts = rebase_edits(
        service.ontology_id, base, candidate, live_snapshot, preserved,
        context_digest=context_digest,
    )
    if conflicts:
        from app.services.v2.action_authorization import authorize_compiled_edits
        permission_digest = authorize_compiled_edits(
            db, service.ontology_id, service.user, live_snapshot, [])
        conflict_digest = stable_hash({
            "context_digest": context_digest,
            "conflict_ids": sorted(item["conflict_id"] for item in conflicts),
        })
        conflict_ids = sorted(item["conflict_id"] for item in conflicts)
        scenario.rebase_error = {
            "code": "rebase_conflict",
            "conflict_digest": conflict_digest,
            "context_digest": context_digest,
            "conflict_count": len(conflicts),
            "conflict_ids": conflict_ids,
            "scenario_etag": scenario.etag,
            "head_revision": scenario.head_revision,
            "old_base_view_id": old_base_id,
            "live_view_id": live_view.id,
            "live_content_digest": live_view.content_digest,
        }
        db.add(ScenarioAudit(
            scenario_id=scenario.id,
            actor_kind=service.execution_actor_kind,
            actor_id=service.execution_actor_id,
            operation="rebase_conflict",
            summary={"conflict_digest": conflict_digest,
                     "context_digest": context_digest,
                     "conflict_count": len(conflicts),
                     "live_view_id": live_view.id,
                     "authorization": service.authorization_provenance(
                         scenario.id, permission_digest)},
        ))
        db.commit()
        return {"status": "conflict", "conflict_digest": conflict_digest,
                "conflict_count": len(conflicts), "revision": old_revision}

    if not edits:
        edits = [Edit(sequence=0, op="invoke_action", action_key="scenario_reset")]

    def finalize_rebase_sql():
        locked = db.query(ScenarioResource).filter_by(
            id=scenario.id, ontology_id=service.ontology_id,
        ).populate_existing().with_for_update().first()
        if (
            locked is None
            or locked.base_view_id != old_base_id
            or locked.etag != expected_etag + 1
            or locked.head_revision != old_revision + 1
        ):
            raise ScenarioError("rebase_context_changed", "Scenario changed before rebase publication", 409)
        if not db.query(ScenarioRevision).filter_by(
            scenario_id=locked.id, revision=0,
        ).first():
            db.add(ScenarioRevision(
                scenario_id=locked.id, revision=0, result_view_id=old_base_id,
                content_digest=base_view.content_digest or base_view.source_manifest_digest,
                created_by=service.user.id,
            ))
        locked.base_view_id = live_view.id
        locked.last_rebased_at = now()
        locked.rebase_error = None

    revision = service.publish_revision(
        scenario,
        RevisionRequest(base_revision=old_revision, expected_etag=expected_etag,
                        client_request_id=f"rebase:{uuid4()}", edits=edits),
        replace_all=True,
        source="scenario_rebase",
        parent_view_id_override=live_view.id,
        operation_context={
            "audit_operation": "rebase",
            "old_base_view_id": old_base_id,
            "base_view_id": live_view.id,
            "context_digest": context_digest,
        },
        before_sql_commit=finalize_rebase_sql,
    )
    return {"status": "rebased", "revision": revision.revision,
            "base_view_id": live_view.id, "conflicts": []}


def get_rebase_conflicts(service, scenario_id):
    scenario = service.get(scenario_id, write=True)
    state = scenario.rebase_error or {}
    if state.get("code") != "rebase_conflict":
        raise ScenarioError("rebase_conflict_missing", "There is no pending rebase conflict", 404)
    if scenario.mode != "tracking" or scenario.status != "active":
        raise ScenarioError("rebase_unavailable", "Only active Tracking Scenarios can resolve conflicts", 409)
    context = _load_conflict_context(service, scenario, state)
    conflicts = context[7]
    live = context[5]
    return {
        "scenario_id": scenario.id,
        "scenario_etag": scenario.etag,
        "head_revision": scenario.head_revision,
        "conflict_digest": state["conflict_digest"],
        "conflicts": _visible_conflicts(service, live, conflicts),
    }


def resolve_rebase_conflicts(service, scenario_id, request):
    db = service.db
    scenario = (db.query(ScenarioResource).filter_by(
        id=scenario_id, ontology_id=service.ontology_id,
    ).populate_existing().with_for_update().first())
    if scenario is None:
        raise ScenarioError("not_found", "Scenario not found", 404)
    service.get(scenario_id, write=True)
    reason = request.reason.strip()
    if not reason:
        raise ScenarioError("resolution_reason_required", "Provide a non-empty reason for the rebase decision", 422, "reason")
    resolutions = [{"conflict_id": item.conflict_id, "choice": item.choice}
                   for item in request.resolutions]
    request_digest = stable_hash({
        "conflict_digest": request.conflict_digest,
        "reason": reason,
        "resolutions": sorted(resolutions, key=lambda item: item["conflict_id"]),
    })
    prior = db.query(ScenarioAudit).filter_by(
        scenario_id=scenario.id,
        operation="resolve_rebase_conflicts",
        request_id=request.client_request_id,
    ).first()
    if prior is not None:
        prior_context = (prior.summary or {}).get("operation_context") or {}
        if prior_context.get("request_digest") != request_digest:
            raise ScenarioError("idempotency_conflict", "Request id was used for another rebase decision", 409, "client_request_id")
        prior_revision = db.query(ScenarioRevision).filter_by(
            scenario_id=scenario.id, revision=prior.revision,
        ).first() if prior.revision else None
        if prior_revision is not None:
            return {"status": "resolved", "revision": prior_revision.revision,
                    "base_view_id": prior_context.get("resolved_base_view_id"),
                    "conflict_digest": request.conflict_digest,
                    "replayed": True}
    state = scenario.rebase_error or {}
    if state.get("code") != "rebase_conflict":
        raise ScenarioError("rebase_conflict_missing", "There is no pending rebase conflict", 409)
    if scenario.mode != "tracking" or scenario.status != "active":
        raise ScenarioError("rebase_unavailable", "Only active Tracking Scenarios can resolve conflicts", 409)
    if (scenario.etag != request.expected_etag
            or state.get("scenario_etag") != request.expected_etag
            or request.conflict_digest != state.get("conflict_digest")):
        raise ScenarioError("rebase_conflict_stale", "Scenario or conflict set changed; refresh before resolving", 409)

    context = _load_conflict_context(service, scenario, state)
    base_view, base, candidate_view, candidate, live_view, live, _, conflicts, context_digest = context
    current_live = FalkorReadAdapter(service.graph._graph(service.ontology_id)).read(service.ontology_id)
    if _graph_digest(current_live) != state.get("live_content_digest"):
        raise ScenarioError("live_state_changed", "Live data changed after conflict review; run rebase again", 409)

    conflict_by_id = {item["conflict_id"]: item for item in conflicts}
    submitted = {item.conflict_id: item.choice for item in request.resolutions}
    if len(submitted) != len(request.resolutions) or set(submitted) != set(conflict_by_id):
        raise ScenarioError("incomplete_conflict_resolution", "Resolve each current conflict exactly once", 422, "resolutions")
    visible = {item["conflict_id"]: item for item in _visible_conflicts(service, live, conflicts)}
    for conflict_id, choice in submitted.items():
        if choice not in visible[conflict_id]["allowed_choices"]:
            raise ScenarioError("forbidden_conflict_resolution", "Current policy does not allow this conflict choice", 403, "resolutions")

    previous = db.query(ScenarioChangeSet).filter_by(
        scenario_id=scenario.id, client_request_id=request.client_request_id,
    ).first()
    if previous is not None:
        raise ScenarioError("idempotency_conflict", "Request id was already used for a Scenario revision", 409, "client_request_id")
    head = db.query(ScenarioRevision).filter_by(
        scenario_id=scenario.id, revision=scenario.head_revision,
    ).first() if scenario.head_revision else None
    previous_changeset = db.get(ScenarioChangeSet, head.changeset_id) if head and head.changeset_id else None
    preserved = previous_changeset.ordered_edits if previous_changeset and (previous_changeset.validation_report or {}).get("replace_all") else []
    edits, unresolved = rebase_edits(
        service.ontology_id, base, candidate, live, preserved,
        context_digest=context_digest, resolutions=submitted,
    )
    if unresolved:
        raise ScenarioError("incomplete_conflict_resolution", "Some rebase conflicts remain unresolved", 422, "resolutions")
    if len(edits) > 500:
        raise ScenarioError("rebase_edit_limit_exceeded", "Resolved rebase exceeds the 500-edit publication limit", 413, "resolutions")
    if not edits:
        edits = [Edit(sequence=0, op="invoke_action", action_key="scenario_reset")]

    from app.services.v2.action_authorization import authorize_compiled_edits
    policy_scope_digest = authorize_compiled_edits(
        db, service.ontology_id, service.user, live, edits,
    )
    operation_context = {
        "audit_operation": "resolve_rebase_conflicts",
        "conflict_digest": state["conflict_digest"],
        "context_digest": context_digest,
        "request_digest": request_digest,
        "reason": reason,
        "policy_scope_digest": policy_scope_digest,
        "resolved_base_view_id": live_view.id,
        "resolution_count": len(submitted),
    }
    old_base_id = scenario.base_view_id
    old_revision = scenario.head_revision

    def finalize_rebase_resolution_sql():
        locked = db.query(ScenarioResource).filter_by(
            id=scenario.id, ontology_id=service.ontology_id,
        ).populate_existing().with_for_update().first()
        if (
            locked is None
            or locked.base_view_id != old_base_id
            or locked.etag != request.expected_etag + 1
            or locked.head_revision != old_revision + 1
        ):
            raise ScenarioError("rebase_context_changed", "Scenario changed before conflict resolution publication", 409)
        if not db.query(ScenarioRevision).filter_by(
            scenario_id=locked.id, revision=0,
        ).first():
            db.add(ScenarioRevision(
                scenario_id=locked.id, revision=0, result_view_id=base_view.id,
                content_digest=base_view.content_digest or base_view.source_manifest_digest,
                created_by=service.user.id,
            ))
        locked.base_view_id = live_view.id
        locked.last_rebased_at = now()
        locked.rebase_error = None

    revision = service.publish_revision(
        scenario,
        RevisionRequest(base_revision=scenario.head_revision,
                        expected_etag=request.expected_etag,
                        client_request_id=request.client_request_id,
                        permission_digest=policy_scope_digest,
                        edits=edits),
        replace_all=True,
        source="scenario_rebase_resolution",
        parent_view_id_override=live_view.id,
        operation_context=operation_context,
        before_sql_commit=finalize_rebase_resolution_sql,
    )
    return {"status": "resolved", "revision": revision.revision,
            "base_view_id": live_view.id,
            "conflict_digest": state["conflict_digest"],
            "policy_scope_digest": policy_scope_digest,
            "resolved_by": str(service.user.id),
            "reason": reason}
