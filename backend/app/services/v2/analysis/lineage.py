"""Data lineage from OpenLineage run events, with a graph and impact view like Marquez's.

OpenLineage (Apache-2.0, https://github.com/OpenLineage/OpenLineage) fixes the event: a job run that starts or ends, with the
datasets it read and wrote. Marquez (Apache-2.0, https://github.com/MarquezProject/marquez) is its reference store and UI. Here the
events are kept as they arrive and the graph is derived on read: a job's inputs and outputs are those of its latest completed run,
datasets carry the newest facets they were reported with (schema, description, data quality), and lineage is every path between them.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.v2.analysis import LineageEvent

EVENT_TYPES = {"START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER"}
FINAL = {"COMPLETE", "ABORT", "FAIL"}
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_DATASETS_PER_EVENT = 200
MAX_EVENTS_PER_REQUEST = 500
PRODUCER = "https://github.com/MarquezProject/marquez/blob/main/docker/metadata.json"


class LineageError(ValueError):
    """The event is not a valid OpenLineage run event."""


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise LineageError(f"{what} must be a non-empty string of at most {limit} characters")
    return value


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise LineageError("eventTime must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LineageError(f"eventTime is not ISO 8601: {value}") from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _datasets(items: Any, what: str) -> list[dict]:
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > MAX_DATASETS_PER_EVENT:
        raise LineageError(f"{what} must be a list of at most {MAX_DATASETS_PER_EVENT} datasets")
    for item in items:
        if not isinstance(item, dict):
            raise LineageError(f"every entry of {what} must be an object")
        _text(item.get("namespace"), f"{what}[].namespace", 200)
        _text(item.get("name"), f"{what}[].name", 400)
    return items


def validate_event(payload: Any) -> dict:
    """Check the parts of an OpenLineage RunEvent the graph depends on; everything else is stored untouched."""
    if not isinstance(payload, dict):
        raise LineageError("an event must be a JSON object")
    if len(json.dumps(payload, default=str)) > MAX_PAYLOAD_BYTES:
        raise LineageError(f"an event can be at most {MAX_PAYLOAD_BYTES // 1024} KB")
    event_type = _text(payload.get("eventType"), "eventType", 16).upper()
    if event_type not in EVENT_TYPES:
        raise LineageError(f"eventType must be one of {sorted(EVENT_TYPES)}")
    run, job = payload.get("run"), payload.get("job")
    if not isinstance(run, dict) or not isinstance(job, dict):
        raise LineageError("run and job are required")
    return {
        "event_type": event_type, "event_time": parse_time(payload.get("eventTime")),
        "run_id": _text(run.get("runId"), "run.runId", 64), "job_namespace": _text(job.get("namespace"), "job.namespace", 200),
        "job_name": _text(job.get("name"), "job.name", 400), "inputs": _datasets(payload.get("inputs"), "inputs"), "outputs": _datasets(payload.get("outputs"), "outputs"),
    }


def ingest(db: Session, payloads: Any, user_id: str, *, source: str = "api") -> list[LineageEvent]:
    items = payloads if isinstance(payloads, list) else [payloads]
    if not items or len(items) > MAX_EVENTS_PER_REQUEST:
        raise LineageError(f"send between 1 and {MAX_EVENTS_PER_REQUEST} events")
    rows = []
    for payload in items:
        meta = validate_event(payload)
        rows.append(LineageEvent(job_namespace=meta["job_namespace"], job_name=meta["job_name"], run_id=meta["run_id"], event_type=meta["event_type"],
                                 event_time=meta["event_time"], payload=payload, source=source, created_by=user_id))
    db.add_all(rows)
    db.commit()
    return rows


def reset(db: Session, *, namespace: str | None = None, source: str | None = None) -> int:
    query = db.query(LineageEvent)
    if namespace:
        query = query.filter(LineageEvent.job_namespace == namespace)
    if source:
        query = query.filter(LineageEvent.source == source)
    count = query.delete(synchronize_session=False)
    db.commit()
    return count


# ---- the graph -------------------------------------------------------------------------------------------------------

def _node_id(kind: str, namespace: str, name: str) -> str:
    return f"{kind}:{namespace}:{name}"


def _custom(facets: dict, name: str) -> dict | None:
    """A facet's own fields, without the producer and schema bookkeeping."""
    body = facets.get(name)
    if not isinstance(body, dict):
        return None
    return {k: v for k, v in body.items() if not k.startswith("_")} or None


def _quality(facets: dict) -> dict | None:
    assertions = (facets.get("dataQualityAssertions") or {}).get("assertions")
    metrics = facets.get("dataQualityMetrics")
    if not assertions and not metrics:
        return None
    out: dict[str, Any] = {}
    if assertions:
        out["assertions"] = [{"assertion": a.get("assertion"), "success": bool(a.get("success")), "column": a.get("column")} for a in assertions if isinstance(a, dict)]
        out["passed"] = sum(1 for a in out["assertions"] if a["success"])
        out["failed"] = len(out["assertions"]) - out["passed"]
    if metrics:
        out["metrics"] = {k: v for k, v in metrics.items() if not k.startswith("_")}
    return out


def build_graph(events: list[LineageEvent]) -> dict:
    jobs: dict[tuple[str, str], dict] = {}
    datasets: dict[tuple[str, str], dict] = {}
    for event in sorted(events, key=lambda e: (e.event_time, e.created_at or e.event_time)):
        payload = event.payload or {}
        key = (event.job_namespace, event.job_name)
        job = jobs.setdefault(key, {"runs": {}, "inputs": [], "outputs": [], "io_final": False, "facets": {}, "run_facets": {}})
        run = job["runs"].setdefault(event.run_id, {"run_id": event.run_id, "started": None, "ended": None, "state": "RUNNING", "facets": {}})
        run["facets"].update((payload.get("run") or {}).get("facets") or {})
        if event.event_type in FINAL:
            job["run_facets"] = {k: _custom(run["facets"], k) for k in run["facets"] if _custom(run["facets"], k)}
        job["facets"].update((payload.get("job") or {}).get("facets") or {})
        if event.event_type == "START":
            run["started"] = event.event_time
        elif event.event_type in FINAL:
            run["ended"], run["state"] = event.event_time, event.event_type
        inputs, outputs = payload.get("inputs") or [], payload.get("outputs") or []
        if (inputs or outputs) and (event.event_type == "COMPLETE" or not job["io_final"]):
            job["inputs"], job["outputs"] = inputs, outputs
            job["io_final"] = event.event_type == "COMPLETE"
        for side, items in (("in", inputs), ("out", outputs)):
            for item in items:
                ds = datasets.setdefault((item["namespace"], item["name"]), {"facets": {}, "updated": None})
                for field in ("facets", "inputFacets", "outputFacets"):
                    ds["facets"].update(item.get(field) or {})
                if side == "out" and event.event_type == "COMPLETE":
                    ds["updated"] = event.event_time

    nodes, edges = [], []
    for (namespace, name), job in jobs.items():
        runs = sorted(job["runs"].values(), key=lambda r: r["started"] or r["ended"] or datetime.min.replace(tzinfo=timezone.utc))
        latest = runs[-1] if runs else None
        state = {"COMPLETE": "success", "FAIL": "failed", "ABORT": "aborted"}.get(latest["state"], "running") if latest else "unknown"
        duration = None
        if latest and latest["started"] and latest["ended"]:
            duration = round((latest["ended"] - latest["started"]).total_seconds(), 3)
        nodes.append({
            "id": _node_id("job", namespace, name), "kind": "job", "namespace": namespace, "name": name, "label": name, "status": state, "run_count": len(runs),
            "last_run_at": (latest["ended"] or latest["started"]).isoformat() if latest and (latest["ended"] or latest["started"]) else None, "last_duration_seconds": duration,
            "sql": (job["facets"].get("sql") or {}).get("query"), "description": (job["facets"].get("documentation") or {}).get("description"),
            "layer": (_custom(job["facets"], "workbenchLayer") or {}).get("layer"), "ref": _custom(job["facets"], "workbenchRef"),
            "run_facets": job["run_facets"] or None,
            "runs": [{"run_id": r["run_id"], "state": r["state"], "started_at": r["started"].isoformat() if r["started"] else None,
                      "ended_at": r["ended"].isoformat() if r["ended"] else None} for r in reversed(runs[-10:])],
        })
        for item in job["inputs"]:
            datasets.setdefault((item["namespace"], item["name"]), {"facets": {}, "updated": None})
            edges.append({"source": _node_id("dataset", item["namespace"], item["name"]), "target": _node_id("job", namespace, name)})
        for item in job["outputs"]:
            datasets.setdefault((item["namespace"], item["name"]), {"facets": {}, "updated": None})
            edges.append({"source": _node_id("job", namespace, name), "target": _node_id("dataset", item["namespace"], item["name"])})
    for (namespace, name), ds in datasets.items():
        facets = ds["facets"]
        nodes.append({
            "id": _node_id("dataset", namespace, name), "kind": "dataset", "namespace": namespace, "name": name, "label": name, "status": None,
            "description": (facets.get("documentation") or {}).get("description"), "fields": [{"name": f.get("name"), "type": f.get("type"), "description": f.get("description")}
                                                                                           for f in (facets.get("schema") or {}).get("fields", []) if isinstance(f, dict)],
            "source": (facets.get("dataSource") or {}).get("name"), "updated_at": ds["updated"].isoformat() if ds["updated"] else None, "quality": _quality(facets),
            "layer": (_custom(facets, "workbenchLayer") or {}).get("layer"), "ref": _custom(facets, "workbenchRef"),
            "column_lineage": {col: [f"{i.get('name')}.{i.get('field')}" for i in (spec.get("inputFields") or [])]
                               for col, spec in ((facets.get("columnLineage") or {}).get("fields") or {}).items()} or None,
        })
    nodes.sort(key=lambda n: (n["kind"], n["namespace"], n["name"]))
    unique_edges = {(e["source"], e["target"]): e for e in edges}
    column_edges = []
    for (namespace, name), ds in datasets.items():
        for column, spec in ((ds["facets"].get("columnLineage") or {}).get("fields") or {}).items():
            for item in spec.get("inputFields") or []:
                column_edges.append({"from": {"dataset": _node_id("dataset", item.get("namespace"), item.get("name")), "field": item.get("field")},
                                     "to": {"dataset": _node_id("dataset", namespace, name), "field": column},
                                     "transformation": spec.get("transformationDescription")})
    column_edges.sort(key=lambda e: (e["from"]["dataset"], e["from"]["field"] or "", e["to"]["dataset"], e["to"]["field"]))
    return {"nodes": nodes, "edges": sorted(unique_edges.values(), key=lambda e: (e["source"], e["target"])), "column_edges": column_edges,
            "namespaces": sorted({n["namespace"] for n in nodes})}


def _neighbours(graph: dict) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    down: dict[str, list[str]] = {}
    up: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        down.setdefault(edge["source"], []).append(edge["target"])
        up.setdefault(edge["target"], []).append(edge["source"])
    return down, up


def _walk(start: str, step: dict[str, list[str]], depth: int) -> dict[str, int]:
    seen = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if seen[node] >= depth:
            continue
        for nxt in step.get(node, []):
            if nxt not in seen:
                seen[nxt] = seen[node] + 1
                queue.append(nxt)
    return seen


def subgraph(graph: dict, node_id: str, *, depth: int = 3, direction: str = "both") -> dict:
    """The part of the graph within `depth` hops of a node, upstream, downstream or both (Marquez's lineage view)."""
    index = {n["id"]: n for n in graph["nodes"]}
    if node_id not in index:
        raise LineageError(f"unknown node '{node_id}'")
    if direction not in ("both", "upstream", "downstream"):
        raise LineageError("direction must be both, upstream or downstream")
    down, up = _neighbours(graph)
    keep = {node_id}
    if direction in ("both", "downstream"):
        keep |= set(_walk(node_id, down, depth))
    if direction in ("both", "upstream"):
        keep |= set(_walk(node_id, up, depth))
    return {"nodes": [n for n in graph["nodes"] if n["id"] in keep], "edges": [e for e in graph["edges"] if e["source"] in keep and e["target"] in keep],
            "column_edges": [e for e in graph.get("column_edges", []) if e["from"]["dataset"] in keep and e["to"]["dataset"] in keep],
            "namespaces": graph["namespaces"], "focus": node_id}


def impact(graph: dict, node_id: str) -> dict:
    """Everything downstream of a node: what would be affected if its data or code changed, nearest first."""
    index = {n["id"]: n for n in graph["nodes"]}
    if node_id not in index:
        raise LineageError(f"unknown node '{node_id}'")
    down, up = _neighbours(graph)
    reach = _walk(node_id, down, 50)
    reach.pop(node_id)
    affected = [{"id": nid, "kind": index[nid]["kind"], "label": index[nid]["label"], "namespace": index[nid]["namespace"], "distance": dist, "status": index[nid].get("status"),
                 "quality_failed": (index[nid].get("quality") or {}).get("failed", 0)} for nid, dist in sorted(reach.items(), key=lambda kv: (kv[1], kv[0]))]
    upstream = _walk(node_id, up, 50)
    upstream.pop(node_id)
    return {"node": node_id, "downstream": affected, "downstream_jobs": sum(1 for a in affected if a["kind"] == "job"), "downstream_datasets": sum(1 for a in affected if a["kind"] == "dataset"),
            "upstream_count": len(upstream)}


def column_impact(graph: dict, dataset_id: str, field: str) -> dict:
    """What a change to one column of one dataset reaches: the columns derived from it, the jobs that derive them, nearest first.

    Column lineage is only as complete as the producers made it. A column that no job lists as an input has no downstream here, which says
    nothing about the table as a whole (`impact` follows that)."""
    index = {n["id"]: n for n in graph["nodes"]}
    if dataset_id not in index or index[dataset_id]["kind"] != "dataset":
        raise LineageError(f"unknown dataset '{dataset_id}'")
    forward: dict[tuple[str, str], list[dict]] = {}
    for edge in graph.get("column_edges", []):
        forward.setdefault((edge["from"]["dataset"], edge["from"]["field"]), []).append(edge)
    down, up = _neighbours(graph)
    seen: dict[tuple[str, str], int] = {(dataset_id, field): 0}
    via: dict[tuple[str, str], dict] = {}
    queue = deque([(dataset_id, field)])
    while queue:
        key = queue.popleft()
        for edge in forward.get(key, []):
            target = (edge["to"]["dataset"], edge["to"]["field"])
            if target not in seen:
                seen[target] = seen[key] + 1
                via[target] = {"from": key, "transformation": edge.get("transformation")}
                queue.append(target)
    columns, jobs = [], {}
    for (ds, column), distance in sorted(seen.items(), key=lambda kv: (kv[1], kv[0])):
        if distance == 0:
            continue
        node = index.get(ds, {})
        origin = via[(ds, column)]
        columns.append({"dataset": ds, "label": node.get("label"), "namespace": node.get("namespace"), "layer": node.get("layer"), "ref": node.get("ref"), "field": column,
                        "distance": distance, "derived_from": {"dataset": origin["from"][0], "field": origin["from"][1]}, "transformation": origin["transformation"]})
        for job_id in set(down.get(origin["from"][0], [])) & set(up.get(ds, [])):
            job = index.get(job_id, {})
            jobs.setdefault(job_id, {"id": job_id, "label": job.get("label"), "layer": job.get("layer"), "ref": job.get("ref"), "distance": distance, "description": job.get("description")})
    return {"node": dataset_id, "field": field, "columns": columns, "jobs": sorted(jobs.values(), key=lambda j: (j["distance"], j["id"])),
            "has_column_lineage": bool(graph.get("column_edges")), "layers": sorted({c["layer"] for c in columns if c["layer"]})}


def graph_from_db(db: Session, *, namespace: str | None = None, source: str | None = None, exclude_source_prefix: str | None = None) -> dict:
    query = db.query(LineageEvent)
    if source:
        query = query.filter(LineageEvent.source == source)
    if exclude_source_prefix:
        query = query.filter(~LineageEvent.source.startswith(exclude_source_prefix))
    if namespace:
        query = query.filter(LineageEvent.job_namespace == namespace)
    graph = build_graph(query.all())
    if namespace:
        names = db.query(LineageEvent.job_namespace).distinct()
        if exclude_source_prefix:
            names = names.filter(~LineageEvent.source.startswith(exclude_source_prefix))
        graph["namespaces"] = sorted({n for (n,) in names.all()})
    graph["event_count"] = query.count()
    return graph


# ---- demo events -----------------------------------------------------------------------------------------------------

def render_template(raw: str, now: datetime | None = None) -> list[dict]:
    """Marquez's seed file with its time placeholders filled in, the way its seed.sh does, but ending a few minutes ago."""
    start = (now or datetime.now(timezone.utc)) - timedelta(minutes=12)
    fmt = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
    text = raw.replace("{{RUN_START_TIME}}", fmt(start))
    text = re.sub(r"\{\{RUN_END_TIME_AFTER_(\d+)_MINUTES\}\}", lambda m: fmt(start + timedelta(minutes=int(m.group(1)))), text)
    events = json.loads(text)
    if not isinstance(events, list):
        raise LineageError("the seed file must hold a list of events")
    return events


def seed_from_text(db: Session, raw: str, user_id: str, *, source: str = "demo:marquez") -> int:
    """Replace the earlier copy of a demo seed with a fresh one (run ids in the file are fixed, so it must not be added twice)."""
    events = render_template(raw)
    reset(db, source=source)
    ingest(db, events, user_id, source=source)
    return len(events)


def seed_from_file(db: Session, path: Path, user_id: str, *, source: str = "demo:marquez") -> int:
    return seed_from_text(db, path.read_text(encoding="utf-8"), user_id, source=source)


def new_run_id() -> str:
    return str(uuid.uuid4())
