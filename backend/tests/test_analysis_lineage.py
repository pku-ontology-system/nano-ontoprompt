"""Lineage: OpenLineage run events become a graph of jobs and datasets with statuses, schema, quality and impact."""
import json

import pytest

from app.models.v2.analysis import LineageEvent
from app.services.v2.analysis import lineage

NS = "shop"


def event(kind, job, run, time, inputs=(), outputs=(), run_facets=None, job_facets=None):
    return {"eventType": kind, "eventTime": time, "run": {"runId": run, "facets": run_facets or {}}, "job": {"namespace": NS, "name": job, "facets": job_facets or {}},
            "inputs": list(inputs), "outputs": list(outputs), "producer": "test"}


def ds(name, **facets):
    return {"namespace": NS, "name": name, "facets": facets}


SCHEMA = {"schema": {"fields": [{"name": "id", "type": "INT"}, {"name": "email", "type": "VARCHAR"}]}}
CHECKS = {"inputFacets": {"dataQualityAssertions": {"assertions": [{"assertion": "not_null", "success": True, "column": "id"}, {"assertion": "unique", "success": False, "column": "email"}]}}}


def pipeline():
    return [
        event("START", "load", "r1", "2026-01-01T10:00:00Z"),
        event("COMPLETE", "load", "r1", "2026-01-01T10:01:00Z", outputs=[ds("raw", **SCHEMA)], job_facets={"documentation": {"description": "Loads the raw table"}}),
        event("START", "clean", "r2", "2026-01-01T10:02:00Z"),
        event("COMPLETE", "clean", "r2", "2026-01-01T10:03:00Z", inputs=[{"namespace": NS, "name": "raw", **CHECKS}], outputs=[ds("clean")], job_facets={"sql": {"query": "select 1"}}),
        event("START", "report", "r3", "2026-01-01T10:04:00Z"),
        event("FAIL", "report", "r3", "2026-01-01T10:04:30Z", inputs=[ds("clean")], outputs=[ds("report")]),
    ]


def graph_of(db, admin_user, events=None):
    lineage.ingest(db, events or pipeline(), admin_user.id)
    return lineage.graph_from_db(db)


def test_events_become_jobs_datasets_and_edges(db, admin_user):
    graph = graph_of(db, admin_user)
    ids = {n["id"] for n in graph["nodes"]}
    assert ids == {f"job:{NS}:load", f"job:{NS}:clean", f"job:{NS}:report", f"dataset:{NS}:raw", f"dataset:{NS}:clean", f"dataset:{NS}:report"}
    edges = {(e["source"], e["target"]) for e in graph["edges"]}
    assert (f"job:{NS}:load", f"dataset:{NS}:raw") in edges and (f"dataset:{NS}:raw", f"job:{NS}:clean") in edges
    assert (f"dataset:{NS}:clean", f"job:{NS}:report") in edges and (f"job:{NS}:report", f"dataset:{NS}:report") in edges
    assert graph["namespaces"] == [NS] and graph["event_count"] == 6


def test_job_status_duration_and_facets(db, admin_user):
    nodes = {n["id"]: n for n in graph_of(db, admin_user)["nodes"]}
    load = nodes[f"job:{NS}:load"]
    assert load["status"] == "success" and load["last_duration_seconds"] == 60.0 and load["description"] == "Loads the raw table"
    assert nodes[f"job:{NS}:report"]["status"] == "failed"
    assert nodes[f"job:{NS}:clean"]["sql"] == "select 1"
    assert load["runs"][0]["run_id"] == "r1" and load["runs"][0]["state"] == "COMPLETE"


def test_dataset_schema_and_quality_come_from_facets(db, admin_user):
    nodes = {n["id"]: n for n in graph_of(db, admin_user)["nodes"]}
    raw = nodes[f"dataset:{NS}:raw"]
    assert [f["name"] for f in raw["fields"]] == ["id", "email"]
    assert raw["quality"]["passed"] == 1 and raw["quality"]["failed"] == 1
    assert nodes[f"dataset:{NS}:clean"]["quality"] is None
    assert raw["updated_at"].startswith("2026-01-01T10:01")


def test_a_later_completed_run_replaces_a_jobs_inputs_and_outputs(db, admin_user):
    more = pipeline() + [event("COMPLETE", "load", "r9", "2026-01-02T10:00:00Z", outputs=[ds("raw"), ds("raw_archive")])]
    graph = graph_of(db, admin_user, more)
    assert f"dataset:{NS}:raw_archive" in {n["id"] for n in graph["nodes"]}
    load = next(n for n in graph["nodes"] if n["id"] == f"job:{NS}:load")
    assert load["run_count"] == 2


def test_subgraph_follows_direction_and_depth(db, admin_user):
    graph = graph_of(db, admin_user)
    focus = f"dataset:{NS}:clean"
    both = {n["id"] for n in lineage.subgraph(graph, focus, depth=1)["nodes"]}
    assert both == {focus, f"job:{NS}:clean", f"job:{NS}:report"}
    up = {n["id"] for n in lineage.subgraph(graph, focus, depth=9, direction="upstream")["nodes"]}
    assert up == {focus, f"job:{NS}:clean", f"dataset:{NS}:raw", f"job:{NS}:load"}
    down = lineage.subgraph(graph, focus, depth=9, direction="downstream")
    assert {n["id"] for n in down["nodes"]} == {focus, f"job:{NS}:report", f"dataset:{NS}:report"} and down["focus"] == focus
    with pytest.raises(lineage.LineageError):
        lineage.subgraph(graph, "dataset:nope:x")
    with pytest.raises(lineage.LineageError):
        lineage.subgraph(graph, focus, direction="sideways")


def test_impact_lists_everything_downstream_nearest_first(db, admin_user):
    graph = graph_of(db, admin_user)
    result = lineage.impact(graph, f"dataset:{NS}:raw")
    assert [a["id"] for a in result["downstream"]] == [f"job:{NS}:clean", f"dataset:{NS}:clean", f"job:{NS}:report", f"dataset:{NS}:report"]
    assert [a["distance"] for a in result["downstream"]] == [1, 2, 3, 4]
    assert result["downstream_jobs"] == 2 and result["downstream_datasets"] == 2 and result["upstream_count"] == 1
    failed = next(a for a in result["downstream"] if a["id"] == f"job:{NS}:report")
    assert failed["status"] == "failed"


def test_namespace_filter_and_reset(db, admin_user):
    lineage.ingest(db, pipeline(), admin_user.id)
    other = event("COMPLETE", "other", "x1", "2026-01-01T10:00:00Z", outputs=[{"namespace": "else", "name": "t"}])
    other["job"]["namespace"] = "else"
    lineage.ingest(db, [other], admin_user.id, source="demo:x")
    assert lineage.graph_from_db(db)["namespaces"] == ["else", NS]
    only = lineage.graph_from_db(db, namespace="else")
    assert {n["namespace"] for n in only["nodes"]} == {"else"} and only["namespaces"] == ["else", NS]
    assert lineage.reset(db, source="demo:x") == 1
    assert lineage.graph_from_db(db)["namespaces"] == [NS]


@pytest.mark.parametrize("mutate, message", [
    (lambda e: e.pop("eventType"), "eventType"),
    (lambda e: e.update(eventType="EXPLODE"), "eventType must be one of"),
    (lambda e: e.update(eventTime="yesterday"), "ISO 8601"),
    (lambda e: e["run"].pop("runId"), "run.runId"),
    (lambda e: e["job"].update(name=""), "job.name"),
    (lambda e: e.update(inputs="x"), "inputs"),
    (lambda e: e.update(outputs=[{"namespace": "a"}]), "name"),
])
def test_invalid_events_are_rejected_with_the_reason(db, admin_user, mutate, message):
    bad = event("START", "load", "r1", "2026-01-01T10:00:00Z")
    mutate(bad)
    with pytest.raises(lineage.LineageError, match=message):
        lineage.ingest(db, [bad], admin_user.id)
    assert db.query(LineageEvent).count() == 0


def test_seed_template_gets_times_and_can_be_loaded_twice(db, admin_user, tmp_path):
    template = [event("START", "etl", "11111111-1111-1111-1111-111111111111", "{{RUN_START_TIME}}"),
                event("COMPLETE", "etl", "11111111-1111-1111-1111-111111111111", "{{RUN_END_TIME_AFTER_5_MINUTES}}", outputs=[ds("t")])]
    path = tmp_path / "metadata.template.json"
    path.write_text(json.dumps(template))
    assert lineage.seed_from_file(db, path, admin_user.id) == 2
    assert lineage.seed_from_file(db, path, admin_user.id) == 2
    assert db.query(LineageEvent).count() == 2  # not 4
    job = next(n for n in lineage.graph_from_db(db)["nodes"] if n["kind"] == "job")
    assert job["status"] == "success" and job["last_duration_seconds"] == 300.0
    with pytest.raises(lineage.LineageError, match="list"):
        lineage.render_template("{}")
