"""Durable SQL-authoritative rebuilds for replay-scoped FalkorDB graphs."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models.entity_instance import EntityInstance
from app.models.v2.temporal_replay import (
    DataModelSnapshot,
    TemporalFact,
    TemporalGraphProjectionJob,
    TemporalReplay,
    TemporalStreamEvent,
)
from app.services.v2.datasets.factorynet_installer import FACTORYNET_SOURCE_ID
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.temporal_service import (
    FactoryNetIncrementalState,
    TemporalConfig,
    build_factorynet_instances_incremental,
    normalize_temporal_rows,
)

logger = logging.getLogger(__name__)

DISPATCH_LEASE_SECONDS = 90
RUN_LEASE_SECONDS = 180
QUEUED_REDELIVERY_SECONDS = 180
MAX_DISPATCH_ATTEMPTS = 12
MAX_EXECUTION_ATTEMPTS = 5
GRAPH_WRITE_BATCH_SIZE = 250


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _fact_id(replay_id: str, subject_id: str, predicate: str, object_id: str | None, state_key: str, ordinal: float) -> str:
    identity = [replay_id, subject_id, predicate, object_id, state_key, str(ordinal)]
    return "fact:" + hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()


def _observation_object_ref_id(ontology_id: str, episode_id: str, source_row_id: Any) -> str:
    identity = f"{episode_id}:{source_row_id}"
    digest = hashlib.sha256(f"{ontology_id}:Observation:{identity}".encode("utf-8")).hexdigest()[:20]
    return f"temporal:{ontology_id}:Observation:{digest}"


def _source_revision(db, replay: TemporalReplay) -> tuple[str, list[TemporalStreamEvent], list[TemporalFact]]:
    events = db.query(TemporalStreamEvent).filter(
        TemporalStreamEvent.replay_id == replay.id,
        TemporalStreamEvent.status == "committed",
    ).order_by(
        TemporalStreamEvent.ordinal.asc(),
        TemporalStreamEvent.source_sequence.asc(),
        TemporalStreamEvent.id.asc(),
    ).all()
    facts = db.query(TemporalFact).filter(
        TemporalFact.replay_id == replay.id,
    ).order_by(
        TemporalFact.valid_from_ordinal.asc(),
        TemporalFact.subject_id.asc(),
        TemporalFact.predicate.asc(),
        TemporalFact.state_key.asc(),
        TemporalFact.id.asc(),
    ).all()
    payload = {
        "replay_id": replay.id,
        "ontology_id": replay.ontology_id,
        "schema_revision_id": replay.schema_revision_id,
        "dataset_version_id": replay.dataset_version_id,
        "source_id": replay.source_id,
        "source_mode": replay.source_mode,
        "time_kind": replay.time_kind,
        "config": replay.config or {},
        "watermark_ordinal": str(replay.watermark_ordinal),
        "watermark_sequence": replay.watermark_sequence,
        "events": [
            [event.id, event.event_key, event.operation, event.episode_id, event.entity_key,
             str(event.ordinal), event.source_sequence, event.payload_hash, event.payload_hash_version,
             event.payload, event.source_ref]
            for event in events
        ],
        "facts": [
            [fact.id, fact.subject_id, fact.predicate, fact.object_id, fact.object_value,
             fact.state_key, str(fact.valid_from_ordinal), str(fact.valid_to_ordinal), fact.status,
             fact.source_event_id, fact.evidence_ref_id]
            for fact in facts
        ],
    }
    return _digest(payload), events, facts


def _node_type_from_id(instance_id: str, default: str = "Entity") -> str:
    parts = str(instance_id).split(":", 3)
    return parts[1] if len(parts) > 2 and parts[0] == "FactoryNet" else default


def _merge_node(node_map: dict[str, dict[str, Any]], node_id: str, entity_type: str, props: dict[str, Any]) -> None:
    node = node_map.get(node_id)
    if node is None:
        node_map[node_id] = {"id": node_id, "entity_type": entity_type or "Entity", "properties": dict(props)}
    else:
        node["entity_type"] = entity_type or node.get("entity_type") or "Entity"
        node["properties"].update(props)


def _build_projection_rows(
    db,
    replay: TemporalReplay,
    events: list[TemporalStreamEvent],
    facts: list[TemporalFact],
    namespace: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    node_map: dict[str, dict[str, Any]] = {}
    factorynet_structural_edge_props: dict[str, dict[str, Any]] = {}

    if replay.source_id == FACTORYNET_SOURCE_ID:
        builder = FactoryNetIncrementalState()
        observation_refs: dict[str, str] = {}
        for event in events:
            source_row_id = event.source_row_id or event.source_sequence
            observation_refs[f"FactoryNet:Observation:{event.episode_id}:{source_row_id}"] = _observation_object_ref_id(
                replay.ontology_id, event.episode_id, source_row_id
            )
        ref_ids = list(set(observation_refs.values()))
        object_refs: dict[str, EntityInstance] = {}
        for start in range(0, len(ref_ids), 1000):
            rows = db.query(EntityInstance).filter(
                EntityInstance.ontology_id == replay.ontology_id,
                EntityInstance.id.in_(ref_ids[start:start + 1000]),
            ).all()
            object_refs.update({row.id: row for row in rows})

        incident_ids = [f"dynamic-incident:{replay.id}:{event.id}" for event in events]
        incidents = db.query(EntityInstance).filter(
            EntityInstance.ontology_id == replay.ontology_id,
            EntityInstance.id.in_(incident_ids),
        ).all() if incident_ids else []
        incident_by_id = {row.id: row for row in incidents}

        time_column = replay.time_column or "time_s"
        for event in events:
            payload = dict(event.payload or {})
            payload.setdefault("episode_id", event.episode_id)
            payload.setdefault("_source_row_index", event.source_row_id or event.source_sequence)
            payload[time_column] = payload.get(time_column, float(event.ordinal))
            normalized, issues = normalize_temporal_rows(
                [payload], TemporalConfig(time_kind="ordinal", sequence_column=time_column)
            )
            if issues or not normalized:
                raise ValueError(f"Committed event {event.id} cannot be normalized from SQL")
            row = normalized[0]
            row["_replay_event_seq"] = builder.sequence_by_episode.get(event.episode_id, 0)
            nodes, structural_edges, builder = build_factorynet_instances_incremental(
                [row], builder, sequence_column=time_column
            )
            for source_node in nodes:
                node_id = str(source_node.get("id") or "")
                if not node_id:
                    continue
                props = dict(source_node.get("properties") or {})
                props.update({
                    "_replay_id": replay.id,
                    "_event_id": event.id,
                    "event_ordinal": float(event.ordinal),
                    "_graph_namespace": namespace,
                })
                object_ref = object_refs.get(observation_refs.get(node_id, ""))
                if object_ref is not None:
                    props["object_ref_id"] = object_ref.id
                    if object_ref.object_type_resource_id:
                        props["object_type_resource_id"] = object_ref.object_type_resource_id
                _merge_node(node_map, node_id, str(source_node.get("entity_type") or "Entity"), props)
            for edge in structural_edges:
                source_id = str(edge["source"])
                target_id = str(edge["target"])
                predicate = str(edge.get("type") or "RELATED")
                fact_id = _fact_id(replay.id, source_id, predicate, target_id, "", float(event.ordinal))
                props = dict(edge.get("properties") or {})
                props.update({
                    "_fact_id": fact_id,
                    "_event_id": event.id,
                    "event_ordinal": float(event.ordinal),
                    "valid_from_ordinal": float(event.ordinal),
                    "_replay_id": replay.id,
                    "_graph_namespace": namespace,
                })
                factorynet_structural_edge_props[fact_id] = props

            incident_id = f"dynamic-incident:{replay.id}:{event.id}"
            incident = incident_by_id.get(incident_id)
            if incident is not None:
                incident_props = {
                    key: value for key, value in (incident.row_data or {}).items()
                    if value is None or isinstance(value, (str, int, float, bool))
                }
                incident_props.update({
                    "_replay_id": replay.id,
                    "_event_id": event.id,
                    "event_ordinal": float(event.ordinal),
                    "_graph_namespace": namespace,
                })
                _merge_node(node_map, incident_id, "DynamicIncident", incident_props)
    else:
        mapping = dict((replay.config or {}).get("mapping") or {})
        property_map = mapping.get("properties") if isinstance(mapping.get("properties"), dict) else {}
        entity_type = str(mapping.get("entity_type") or "Entity")
        for event in events:
            node_id = str(event.entity_key)
            props: dict[str, Any] = {}
            if str(event.operation or "upsert") == "delete":
                props["_deleted"] = True
                props["_deleted_at_ordinal"] = float(event.ordinal)
            else:
                payload = dict(event.payload or {})
                props.update({
                    str(target): payload[source]
                    for source, target in property_map.items()
                    if source in payload
                })
                props["_deleted"] = False
            props.update({
                "_replay_id": replay.id,
                "_event_id": event.id,
                "event_ordinal": float(event.ordinal),
                "_graph_namespace": namespace,
            })
            _merge_node(node_map, node_id, entity_type, props)

        # Facts carry any mapped state that predates the event envelope or was
        # not materialized in the graph before this rebuild was requested.
        latest_by_state: dict[tuple[str, str], TemporalFact] = {}
        for fact in facts:
            if fact.object_id is not None or not fact.state_key:
                continue
            key = (fact.subject_id, fact.state_key)
            previous = latest_by_state.get(key)
            if previous is None or (fact.valid_from_ordinal, fact.id) > (previous.valid_from_ordinal, previous.id):
                latest_by_state[key] = fact
        for (subject_id, state_key), fact in latest_by_state.items():
            node = node_map.setdefault(subject_id, {
                "id": subject_id, "entity_type": entity_type, "properties": {},
            })
            value = (fact.object_value or {}).get("value") if isinstance(fact.object_value, dict) else fact.object_value
            if state_key == "__deleted__":
                node["properties"]["_deleted"] = bool(value) if fact.status == "active" else False
                node["properties"]["_deleted_at_ordinal"] = float(fact.valid_from_ordinal)
            elif not state_key.startswith("link:"):
                node["properties"][fact.predicate] = value

    # TemporalFact is the canonical source of every graph edge and endpoint.
    # Unseen relation targets become typed placeholders so a rebuild never
    # silently drops a valid SQL fact when a source sends links out of order.
    for fact in facts:
        if fact.subject_id not in node_map:
            _merge_node(node_map, fact.subject_id, _node_type_from_id(fact.subject_id), {})
        if fact.object_id and fact.object_id not in node_map:
            _merge_node(node_map, fact.object_id, _node_type_from_id(fact.object_id), {})
    for node in node_map.values():
        node["properties"].update({"_replay_id": replay.id, "_graph_namespace": namespace})

    from app.services.v2.temporal_stream_service import _generic_link_specs

    mapping = dict((replay.config or {}).get("mapping") or {})
    link_resource_by_state_key = {
        f"link:{spec['source_field']}": spec.get("semantic_resource_id") or spec.get("link_type_id")
        for spec in _generic_link_specs(mapping)
    } if replay.source_id != FACTORYNET_SOURCE_ID else {}
    edges: list[dict[str, Any]] = []
    for fact in facts:
        if not fact.object_id:
            continue
        if replay.source_id == FACTORYNET_SOURCE_ID and fact.id in factorynet_structural_edge_props:
            props = dict(factorynet_structural_edge_props[fact.id])
        else:
            props = {
                "_fact_id": fact.id,
                "event_ordinal": float(fact.valid_from_ordinal),
                "valid_from_ordinal": float(fact.valid_from_ordinal),
                "_replay_id": replay.id,
                "_graph_namespace": namespace,
            }
            if fact.source_event_id:
                props["_event_id"] = fact.source_event_id
            if replay.source_id != FACTORYNET_SOURCE_ID and fact.evidence_ref_id:
                props["evidence_ref_id"] = fact.evidence_ref_id
        if fact.valid_to_ordinal is not None:
            props["valid_to_ordinal"] = float(fact.valid_to_ordinal)
            props["valid_to"] = float(fact.valid_to_ordinal)
        if fact.status == "expired":
            props["_fact_status"] = "expired"
        if replay.source_id == FACTORYNET_SOURCE_ID and fact.state_key in {"phase", "tool_condition", "inspection"} and isinstance(fact.object_value, dict) and "value" in fact.object_value:
            props["value"] = fact.object_value["value"]
        elif replay.source_id != FACTORYNET_SOURCE_ID and isinstance(fact.object_value, dict) and "value" in fact.object_value:
            props["value"] = fact.object_value["value"]
        semantic_resource_id = link_resource_by_state_key.get(fact.state_key)
        if semantic_resource_id:
            props["semantic_resource_id"] = semantic_resource_id
        edges.append({
            "id": fact.id,
            "source": fact.subject_id,
            "target": fact.object_id,
            "type": FalkorDBService._safe_relation_type(fact.predicate),
            "properties": props,
        })
    return list(node_map.values()), edges


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if item is not None}


def _expected_content_records(
    ontology_id: str,
    namespace: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    expected_nodes = []
    for node in nodes:
        props = _without_none(dict(node.get("properties") or {}))
        props.update({
            "_instance_id": str(node["id"]),
            "_type": str(node.get("entity_type") or "Entity"),
            "_ontology_id": ontology_id,
            "_graph_namespace": namespace,
        })
        expected_nodes.append({"id": str(node["id"]), "entity_type": str(node.get("entity_type") or "Entity"), "properties": props})
    expected_edges = []
    for edge in edges:
        props = _without_none(dict(edge.get("properties") or {}))
        props.setdefault("_ontology_id", ontology_id)
        props["_graph_namespace"] = namespace
        expected_edges.append({
            "id": str(edge["id"]),
            "type": str(edge["type"]),
            "source": str(edge["source"]),
            "target": str(edge["target"]),
            "properties": props,
        })
    return {"nodes": expected_nodes, "edges": expected_edges}


def _content_digest(records: dict[str, list[dict[str, Any]]]) -> str:
    nodes = sorted(records.get("nodes", []), key=lambda row: row["id"])
    edges = sorted(records.get("edges", []), key=lambda row: (row["id"], row["source"], row["target"]))
    return _digest({"nodes": nodes, "edges": edges})


def serialize_temporal_graph_projection_job(job: TemporalGraphProjectionJob) -> dict[str, Any]:
    safe_errors = {
        "dispatch_attempts_exhausted": "后台任务队列暂不可用，请稍后重试。",
        "execution_attempts_exhausted": "图投影重建多次失败，请检查服务后重试。",
        "graph_unavailable": "FalkorDB 当前不可用，SQL 事实仍可正常读取。",
        "graph_delete_failed": "无法清理隔离图空间，重建已停止。",
        "content_mismatch": "重建后的图内容与 SQL 权威投影不一致。",
        "source_projection_mismatch": "SQL 投影组装出的节点/关系数与权威事实计数不一致。",
        "stale_source_revision": "重建期间 SQL 来源发生变化，请重新提交。",
        "rebuild_failed": "图投影重建失败，页面继续读取 SQL 权威事实。",
        "worker_lease_expired": "工作进程租约过期，系统会重新派发重建。",
    }
    return {
        "id": job.id,
        "replay_id": job.replay_id,
        "graph_namespace": job.graph_namespace,
        "source_revision": job.source_revision,
        "status": job.status,
        "expected_nodes": int(job.expected_nodes or 0),
        "expected_edges": int(job.expected_edges or 0),
        "written_nodes": int(job.written_nodes or 0),
        "written_edges": int(job.written_edges or 0),
        "verified_nodes": int(job.verified_nodes or 0),
        "verified_edges": int(job.verified_edges or 0),
        "content_digest": job.content_digest,
        "dispatch_attempts": int(job.dispatch_attempts or 0),
        "execution_attempts": int(job.execution_attempts or 0),
        "next_attempt_at": job.next_attempt_at.isoformat() if job.next_attempt_at else None,
        "error_code": job.error_code,
        "error_message": safe_errors.get(job.error_code or "") if job.status == "failed" else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def dispatch_temporal_graph_projection_job(job_id: str) -> dict[str, Any]:
    now = _now()
    with SessionLocal() as db:
        job = db.query(TemporalGraphProjectionJob).filter(
            TemporalGraphProjectionJob.id == job_id,
        ).with_for_update().first()
        if job is None:
            return {"job_id": job_id, "state": "missing"}
        if job.status != "queued":
            return {"job_id": job_id, "state": job.status}
        lease = _aware(job.dispatch_lease_until)
        retry_at = _aware(job.next_attempt_at)
        if (lease and lease > now) or (retry_at and retry_at > now):
            return {"job_id": job_id, "state": "queued"}
        if int(job.dispatch_attempts or 0) >= MAX_DISPATCH_ATTEMPTS:
            job.status = "failed"
            job.error_code = "dispatch_attempts_exhausted"
            job.completed_at = now
            db.commit()
            return {"job_id": job_id, "state": "failed"}
        job.dispatch_attempts = int(job.dispatch_attempts or 0) + 1
        task_id = f"temporal-graph-projection-{job.id}-{job.dispatch_attempts}"
        job.celery_task_id = task_id
        job.dispatch_lease_until = now + timedelta(seconds=DISPATCH_LEASE_SECONDS)
        job.error_code = None
        job.error_message = None
        db.commit()
    try:
        from app.tasks.v2.temporal_graph_projection import run_temporal_graph_projection_task

        run_temporal_graph_projection_task.apply_async(args=[job_id], task_id=task_id)
    except Exception as exc:
        with SessionLocal() as db:
            job = db.query(TemporalGraphProjectionJob).filter(
                TemporalGraphProjectionJob.id == job_id,
            ).with_for_update().first()
            if job and job.status == "queued" and job.celery_task_id == task_id:
                job.dispatch_lease_until = None
                job.error_code = "dispatch_failed"
                job.error_message = None
                if int(job.dispatch_attempts or 0) >= MAX_DISPATCH_ATTEMPTS:
                    job.status = "failed"
                    job.error_code = "dispatch_attempts_exhausted"
                    job.completed_at = _now()
                else:
                    job.next_attempt_at = _now() + timedelta(
                        seconds=min(30 * (2 ** min(job.dispatch_attempts - 1, 5)), 900)
                    )
                db.commit()
        logger.warning("Unable to dispatch temporal graph projection job %s (exception=%s)", job_id, type(exc).__name__)
        return {"job_id": job_id, "state": "dispatch_failed"}
    with SessionLocal() as db:
        job = db.query(TemporalGraphProjectionJob).filter(
            TemporalGraphProjectionJob.id == job_id,
        ).with_for_update().first()
        if job and job.status == "queued" and job.celery_task_id == task_id:
            job.dispatch_lease_until = None
            job.next_attempt_at = _now() + timedelta(seconds=QUEUED_REDELIVERY_SECONDS)
            db.commit()
            return {"job_id": job_id, "state": "queued"}
    return {"job_id": job_id, "state": "claimed"}


def request_temporal_graph_projection_rebuild(db, replay: TemporalReplay, requested_by: str | None) -> TemporalGraphProjectionJob:
    if replay.status != "published" or not replay.published_snapshot_id:
        raise ValueError("只能重建已发布 TemporalReplay 的图投影")
    snapshot = db.query(DataModelSnapshot).filter(
        DataModelSnapshot.id == replay.published_snapshot_id,
        DataModelSnapshot.replay_id == replay.id,
        DataModelSnapshot.status == "published",
    ).first()
    if snapshot is None or snapshot.graph_namespace != replay.graph_namespace:
        raise ValueError("已发布快照与回放图空间不一致，不能安全重建")
    revision, _events, facts = _source_revision(db, replay)
    job = db.query(TemporalGraphProjectionJob).filter(
        TemporalGraphProjectionJob.replay_id == replay.id,
        TemporalGraphProjectionJob.source_revision == revision,
    ).with_for_update().first()
    now = _now()
    if job is None:
        node_query = db.query(TemporalFact.subject_id.label("node_id")).filter(TemporalFact.replay_id == replay.id)
        object_query = db.query(TemporalFact.object_id.label("node_id")).filter(
            TemporalFact.replay_id == replay.id,
            TemporalFact.object_id.is_not(None),
        )
        expected_nodes = int(db.query(func.count()).select_from(node_query.union(object_query).subquery()).scalar() or 0)
        expected_edges = sum(1 for fact in facts if fact.object_id)
        job = TemporalGraphProjectionJob(
            id=str(uuid.uuid4()),
            ontology_id=replay.ontology_id,
            replay_id=replay.id,
            source_revision=revision,
            graph_namespace=replay.graph_namespace or f"stream_{replay.id.replace('-', '')}",
            requested_by=requested_by,
            status="queued",
            expected_nodes=expected_nodes,
            expected_edges=expected_edges,
        )
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            job = db.query(TemporalGraphProjectionJob).filter(
                TemporalGraphProjectionJob.replay_id == replay.id,
                TemporalGraphProjectionJob.source_revision == revision,
            ).with_for_update().first()
            if job is None:
                raise
    else:
        lease = _aware(job.run_lease_until)
        dispatch_lease = _aware(job.dispatch_lease_until)
        retry_at = _aware(job.next_attempt_at)
        if job.status == "running" and lease and lease > now:
            return job
        if job.status == "queued" and ((dispatch_lease and dispatch_lease > now) or (retry_at and retry_at > now)):
            return job
        if job.status == "running":
            job.status = "queued"
            job.worker_token = None
            job.run_lease_until = None
        if job.status == "failed":
            job.status = "queued"
            job.execution_attempts = 0
            job.dispatch_attempts = 0
            job.completed_at = None
        elif job.status == "completed":
            # A user pressing rebuild explicitly requests a fresh full check,
            # including same-count property drift in the existing graph.
            job.status = "queued"
            job.execution_attempts = 0
            job.dispatch_attempts = 0
            job.completed_at = None
        job.requested_by = job.requested_by or requested_by
        job.graph_namespace = replay.graph_namespace or f"stream_{replay.id.replace('-', '')}"
        job.expected_edges = sum(1 for fact in facts if fact.object_id)
        node_query = db.query(TemporalFact.subject_id.label("node_id")).filter(TemporalFact.replay_id == replay.id)
        object_query = db.query(TemporalFact.object_id.label("node_id")).filter(
            TemporalFact.replay_id == replay.id,
            TemporalFact.object_id.is_not(None),
        )
        job.expected_nodes = int(db.query(func.count()).select_from(node_query.union(object_query).subquery()).scalar() or 0)
        job.next_attempt_at = None
        job.dispatch_lease_until = None
        job.run_lease_until = None
        job.worker_token = None
        job.error_code = None
        job.error_message = None
        db.commit()
    dispatch_temporal_graph_projection_job(job.id)
    db.refresh(job)
    return job


def _claim_job(job_id: str) -> tuple[str | None, str | None]:
    now = _now()
    with SessionLocal() as db:
        job = db.query(TemporalGraphProjectionJob).filter(
            TemporalGraphProjectionJob.id == job_id,
        ).with_for_update().first()
        if not job:
            return None, "missing"
        if job.status == "completed":
            return None, "completed"
        if job.status == "failed":
            return None, "failed"
        lease = _aware(job.run_lease_until)
        if job.status == "running" and lease and lease > now:
            return None, "already_running"
        if int(job.execution_attempts or 0) >= MAX_EXECUTION_ATTEMPTS:
            job.status = "failed"
            job.error_code = "execution_attempts_exhausted"
            job.completed_at = now
            db.commit()
            return None, "failed"
        token = str(uuid.uuid4())
        job.status = "running"
        job.execution_attempts = int(job.execution_attempts or 0) + 1
        job.worker_token = token
        job.run_lease_until = now + timedelta(seconds=RUN_LEASE_SECONDS)
        job.dispatch_lease_until = None
        job.next_attempt_at = None
        job.written_nodes = 0
        job.written_edges = 0
        job.verified_nodes = 0
        job.verified_edges = 0
        job.content_digest = None
        job.error_code = None
        job.error_message = None
        db.commit()
        return token, None


def _retry_or_fail(job_id: str, token: str, code: str, message: str) -> str:
    now = _now()
    with SessionLocal() as db:
        job = db.query(TemporalGraphProjectionJob).filter(
            TemporalGraphProjectionJob.id == job_id,
            TemporalGraphProjectionJob.status == "running",
            TemporalGraphProjectionJob.worker_token == token,
        ).with_for_update().first()
        if not job:
            return "superseded"
        job.error_code = code
        job.error_message = None
        job.worker_token = None
        job.run_lease_until = None
        if code in {"stale_source_revision", "source_projection_mismatch"} or int(job.execution_attempts or 0) >= MAX_EXECUTION_ATTEMPTS:
            job.status = "failed"
            if code != "stale_source_revision":
                job.error_code = "execution_attempts_exhausted"
            job.completed_at = now
            result = "failed"
        else:
            job.status = "queued"
            job.next_attempt_at = now + timedelta(seconds=min(20 * (2 ** min(job.execution_attempts - 1, 5)), 600))
            result = "retry_wait"
        db.commit()
        return result


def _heartbeat(job_id: str, token: str, *, written_nodes: int | None = None, written_edges: int | None = None) -> bool:
    with SessionLocal() as db:
        job = db.query(TemporalGraphProjectionJob).filter(
            TemporalGraphProjectionJob.id == job_id,
            TemporalGraphProjectionJob.status == "running",
            TemporalGraphProjectionJob.worker_token == token,
        ).with_for_update().first()
        if not job:
            return False
        now = _now()
        job.run_lease_until = now + timedelta(seconds=RUN_LEASE_SECONDS)
        if written_nodes is not None:
            job.written_nodes = int(job.written_nodes or 0) + written_nodes
        if written_edges is not None:
            job.written_edges = int(job.written_edges or 0) + written_edges
        db.commit()
        return True


def build_temporal_graph_projection(job_id: str) -> dict[str, Any]:
    token, stopped = _claim_job(job_id)
    if token is None:
        return {"job_id": job_id, "state": stopped or "unknown"}
    try:
        with SessionLocal() as db:
            job = db.get(TemporalGraphProjectionJob, job_id)
            replay = db.get(TemporalReplay, job.replay_id) if job else None
            if not job or not replay or replay.status != "published":
                db.rollback()
                return {"job_id": job_id, "state": _retry_or_fail(job_id, token, "stale_source_revision", "Published replay no longer exists")}
            source_revision, events, facts = _source_revision(db, replay)
            if source_revision != job.source_revision or replay.graph_namespace != job.graph_namespace:
                db.rollback()
                return {"job_id": job_id, "state": _retry_or_fail(job_id, token, "stale_source_revision", "SQL replay source changed")}
            nodes, edges = _build_projection_rows(db, replay, events, facts, job.graph_namespace)
            if len(nodes) != int(job.expected_nodes or 0) or len(edges) != int(job.expected_edges or 0):
                db.rollback()
                return {"job_id": job_id, "state": _retry_or_fail(
                    job_id,
                    token,
                    "source_projection_mismatch",
                    "Canonical graph rows do not match TemporalFact endpoint and edge counts",
                )}
            expected = _expected_content_records(replay.ontology_id, job.graph_namespace, nodes, edges)
            expected_digest = _content_digest(expected)
            ontology_id = replay.ontology_id
            namespace = job.graph_namespace
            replay_id = replay.id

        graph = FalkorDBService()
        if not graph.available:
            return {"job_id": job_id, "state": _retry_or_fail(job_id, token, "graph_unavailable", "FalkorDB unavailable")}
        if not graph.delete_graph(ontology_id, namespace):
            return {"job_id": job_id, "state": _retry_or_fail(job_id, token, "graph_delete_failed", "Unable to clear isolated graph")}
        for start in range(0, len(nodes), GRAPH_WRITE_BATCH_SIZE):
            batch = nodes[start:start + GRAPH_WRITE_BATCH_SIZE]
            written = graph.upsert_instances(ontology_id, batch, graph_namespace=namespace)
            if int(written or 0) != len(batch):
                raise RuntimeError("FalkorDB accepted fewer nodes than the SQL projection batch")
            if not _heartbeat(job_id, token, written_nodes=len(batch)):
                return {"job_id": job_id, "state": "superseded"}
        for start in range(0, len(edges), GRAPH_WRITE_BATCH_SIZE):
            batch = edges[start:start + GRAPH_WRITE_BATCH_SIZE]
            written = graph.upsert_relations(ontology_id, batch, graph_namespace=namespace)
            if int(written or 0) != len(batch):
                raise RuntimeError("FalkorDB accepted fewer fact edges than the SQL projection batch")
            if not _heartbeat(job_id, token, written_edges=len(batch)):
                return {"job_id": job_id, "state": "superseded"}

        actual = graph.projection_content_records(ontology_id, replay_id, namespace)
        if actual is None:
            raise RuntimeError("FalkorDB content readback is unavailable")
        actual_digest = _content_digest(actual)
        if (
            len(actual["nodes"]) != len(expected["nodes"])
            or len(actual["edges"]) != len(expected["edges"])
            or actual_digest != expected_digest
        ):
            return {"job_id": job_id, "state": _retry_or_fail(job_id, token, "content_mismatch", "FalkorDB content differs from SQL-derived projection")}

        with SessionLocal() as db:
            replay = db.get(TemporalReplay, replay_id)
            if not replay:
                db.rollback()
                return {"job_id": job_id, "state": _retry_or_fail(job_id, token, "stale_source_revision", "Temporal replay disappeared before finalization")}
            current_revision, _current_events, _current_facts = _source_revision(db, replay)
            if current_revision != job.source_revision:
                db.rollback()
                return {"job_id": job_id, "state": _retry_or_fail(job_id, token, "stale_source_revision", "SQL replay changed before finalization")}
            current = db.query(TemporalGraphProjectionJob).filter(
                TemporalGraphProjectionJob.id == job_id,
                TemporalGraphProjectionJob.status == "running",
                TemporalGraphProjectionJob.worker_token == token,
            ).with_for_update().first()
            if not current:
                return {"job_id": job_id, "state": "superseded"}
            now = _now()
            current.status = "completed"
            current.verified_nodes = len(actual["nodes"])
            current.verified_edges = len(actual["edges"])
            current.content_digest = actual_digest
            current.worker_token = None
            current.run_lease_until = None
            current.completed_at = now
            current.error_code = None
            current.error_message = None
            db.query(TemporalStreamEvent).filter(
                TemporalStreamEvent.replay_id == replay_id,
                TemporalStreamEvent.status == "committed",
            ).update({
                TemporalStreamEvent.graph_projection_status: "projected",
                TemporalStreamEvent.graph_projected_at: now,
            }, synchronize_session=False)
            db.commit()
        return {"job_id": job_id, "state": "completed", "nodes": len(actual["nodes"]), "edges": len(actual["edges"]), "content_digest": actual_digest}
    except Exception as exc:
        logger.warning("Temporal graph projection rebuild failed for job %s (exception=%s)", job_id, type(exc).__name__)
        return {"job_id": job_id, "state": _retry_or_fail(job_id, token, "rebuild_failed", "Temporal graph projection rebuild failed")}


def reconcile_temporal_graph_projection_jobs(batch_size: int = 50) -> dict[str, Any]:
    now = _now()
    expired = 0
    with SessionLocal() as db:
        jobs = db.query(TemporalGraphProjectionJob).filter(
            TemporalGraphProjectionJob.status == "running",
            TemporalGraphProjectionJob.run_lease_until.is_not(None),
            TemporalGraphProjectionJob.run_lease_until <= now,
        ).order_by(TemporalGraphProjectionJob.updated_at.asc()).limit(max(1, min(int(batch_size), 200))).with_for_update(skip_locked=True).all()
        for job in jobs:
            job.worker_token = None
            job.run_lease_until = None
            if int(job.execution_attempts or 0) >= MAX_EXECUTION_ATTEMPTS:
                job.status = "failed"
                job.error_code = "execution_attempts_exhausted"
                job.completed_at = now
            else:
                job.status = "queued"
                job.error_code = "worker_lease_expired"
                job.next_attempt_at = now
            expired += 1
        db.commit()
        queued_ids = [job_id for (job_id,) in db.query(TemporalGraphProjectionJob.id).filter(
            TemporalGraphProjectionJob.status == "queued",
            or_(TemporalGraphProjectionJob.next_attempt_at.is_(None), TemporalGraphProjectionJob.next_attempt_at <= now),
        ).order_by(TemporalGraphProjectionJob.created_at.asc()).limit(max(1, min(int(batch_size), 200))).all()]
    states: dict[str, int] = {}
    for job_id in queued_ids:
        state = dispatch_temporal_graph_projection_job(job_id).get("state", "unknown")
        states[state] = states.get(state, 0) + 1
    return {"expired_workers": expired, "due_jobs": len(queued_ids), "dispatch_states": states}


def latest_temporal_graph_projection_job(db, replay_id: str) -> TemporalGraphProjectionJob | None:
    return db.query(TemporalGraphProjectionJob).filter(
        TemporalGraphProjectionJob.replay_id == replay_id,
    ).order_by(TemporalGraphProjectionJob.created_at.desc()).first()
