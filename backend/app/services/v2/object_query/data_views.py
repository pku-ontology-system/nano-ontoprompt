"""Manifest and immutable view helpers for Object Query."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from app.models.v2.query_view import QueryDataView
from .core import FalkorReadAdapter, metadata_digest
from .errors import ObjectQueryError
from .normalize import stable_hash


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def retention_pin_reason(db, view, at=None):
    """Return the live resource that pins an expired view's backing graph."""
    from app.models.v2.scenario import (
        ScenarioResource, ScenarioRevision, ScenarioRun, ScenarioRunArtifact,
        ScenarioStudy,
    )
    from app.models.v2.query_view import QueryViewRetentionEvent
    from app.services.v2.scenarios import scenario_is_expired

    run_at = aware(at) if at else now()
    if db.query(ScenarioStudy.id).filter(ScenarioStudy.base_view_id == view.id).first():
        return "scenario_study"
    if db.query(ScenarioRunArtifact.id).filter(ScenarioRunArtifact.data_view_id == view.id).first():
        return "scenario_run_artifact"
    if db.query(ScenarioRun.id).filter(ScenarioRun.result_view_id == view.id).first():
        return "scenario_run_result"

    base_scenarios = db.query(ScenarioResource).filter(
        ScenarioResource.base_view_id == view.id,
        ScenarioResource.status == "active",
    ).all()
    if any(not scenario_is_expired(row, at=run_at) for row in base_scenarios):
        return "active_scenario_base"

    revision_scenarios = (db.query(ScenarioResource)
        .join(ScenarioRevision, ScenarioRevision.scenario_id == ScenarioResource.id)
        .filter(ScenarioRevision.result_view_id == view.id,
                ScenarioResource.status == "active").all())
    if any(not scenario_is_expired(row, at=run_at) for row in revision_scenarios):
        return "active_scenario_revision"

    children = db.query(QueryDataView).filter(QueryDataView.base_view_id == view.id).all()
    for child in children:
        if child.status in {"building", "validating"}:
            return "child_view_in_progress"
        if child.status == "ready":
            child_deadline = child.retention_until
            if child_deadline is None or aware(child_deadline) > run_at:
                return "retained_child_view"
            child_cleanup = db.query(QueryViewRetentionEvent.id).filter_by(
                view_id=child.id, event_type="graph_deleted",
            ).first()
            if child_cleanup is None:
                return "expired_child_view_pending_cleanup"
    return None


def as_response(view):
    return {
        "view_id": view.id,
        "ontology_id": view.ontology_id,
        "status": view.status,
        "metadata_digest": view.metadata_digest,
        "source_manifest_digest": view.source_manifest_digest,
        "source_snapshot_id": view.source_snapshot_id,
        "base_view_id": view.base_view_id,
        "changeset_digest": view.changeset_digest,
        "changeset_version": view.changeset_version,
        "builder_version": view.builder_version,
        "index_version": view.index_version,
        "object_count": view.object_count,
        "edge_count": view.edge_count,
        "content_digest": view.content_digest,
        "created_at": view.created_at.isoformat(),
        "retention_until": view.retention_until.isoformat() if view.retention_until else None,
    }


def require_ready(db, view_id, ontology_id, *, principal_id=None, is_admin=False):
    # Serialize view reads with the retention worker. Once a caller owns this
    # row lock, cleanup cannot remove the backing graph until the transaction
    # finishes; if cleanup wins first, the expired status makes this fail closed.
    view = db.query(QueryDataView).filter_by(id=view_id).with_for_update().first()
    if view is None or view.ontology_id != ontology_id:
        raise ObjectQueryError("not_found", "context.data_view_id", "Data view not found")
    if principal_id is not None and not is_admin and view.created_by != principal_id:
        raise ObjectQueryError("forbidden", "context.data_view_id", "Data view is not authorized for this principal")
    if view.retention_until and aware(view.retention_until) <= now():
        if view.status == "ready" and retention_pin_reason(db, view) is None:
            view.status = "expired"
            db.commit()
            raise ObjectQueryError("view_expired", "context.data_view_id", "Data view has expired")
    if view.status != "ready":
        raise ObjectQueryError("view_unavailable", "context.data_view_id", "Data view is not ready", status=view.status)
    return view


def build_live_view(db, graph_service, metadata, ontology_id, principal_id, source_manifest_digest, retention_seconds, *, source_graph_key=None, source_snapshot_id=None):
    """Copy a bounded live graph into an isolated immutable graph and publish atomically."""
    source_key = source_graph_key or ontology_id
    source = graph_service._graph(source_key)
    source_data = FalkorReadAdapter(source, max_objects=100000, max_edges=500000).read(ontology_id)
    view = QueryDataView(
        ontology_id=ontology_id,
        source_manifest_digest=source_manifest_digest,
        source_snapshot_id=source_snapshot_id,
        metadata_digest=metadata_digest(metadata),
        graph_key=f"view_{uuid4().hex}",
        status="building",
        created_by=principal_id,
        retention_until=now() + timedelta(seconds=retention_seconds),
    )
    db.add(view)
    db.flush()
    instances = [{"id": object_id, "entity_type": type_name,
                  "properties": {k: v for k, v in props.items() if not k.startswith("_")}}
                 for (type_name, object_id), props in source_data.objects.items()]
    relations = [{"source": source_id[1], "target": target_id[1], "type": relation}
                 for source_id, relation, target_id in source_data.edges]
    try:
        view.status = "validating"
        graph_service.upsert_instances(view.graph_key, instances)
        graph_service.upsert_relations(view.graph_key, relations)
        # A live graph has no MVCC snapshot API.  Read it again after the copy
        # and refuse publication if the source changed during capture.  This
        # prevents a mixed object/relation cut from being advertised as a
        # strict snapshot; a future coordinated source adapter can replace
        # this bounded consistency check.
        source_after = FalkorReadAdapter(source, max_objects=100000, max_edges=500000).read(ontology_id)
        after_digest = stable_hash({"objects": sorted(source_after.objects.items()), "edges": sorted(source_after.edges)})
        before_digest = stable_hash({"objects": sorted(source_data.objects.items()), "edges": sorted(source_data.edges)})
        if before_digest != after_digest:
            view.status = "failed"
            db.commit()
            raise ObjectQueryError("snapshot_unavailable", "data_view", "Source graph changed during snapshot capture")
        view.object_count = len(instances)
        view.edge_count = len(relations)
        view.content_digest = before_digest
        view.status = "ready"
        db.commit()
    except ObjectQueryError:
        if db.is_active:
            db.commit()
        raise
    except Exception:
        view.status = "failed"
        db.commit()
        raise ObjectQueryError("view_build_failed", "data_view", "Data view build failed")
    return view
