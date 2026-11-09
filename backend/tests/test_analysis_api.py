"""The analysis API: demo datasets, the three engines behind it, and the pipeline that chains them. Demo files are stood in by small synthetic ones."""
import json
import uuid

import pytest

from app.models.user import User
from app.routers.v2 import analysis as router
from app.services.auth_service import hash_password
from app.services.v2.analysis import demos
from tests.test_analysis_resolution import noisy_people

BASE = "/api/v2/analysis"


@pytest.fixture
def demo_dir(tmp_path, monkeypatch):
    noisy_people(people=70).to_csv(tmp_path / "fake_1000.csv", index=False)
    template = [{"eventType": "START", "eventTime": "{{RUN_START_TIME}}", "run": {"runId": "aaaaaaaa-0000-0000-0000-000000000001"}, "job": {"namespace": "food", "name": "etl_menus"}, "producer": "t"},
                {"eventType": "COMPLETE", "eventTime": "{{RUN_END_TIME_AFTER_5_MINUTES}}", "run": {"runId": "aaaaaaaa-0000-0000-0000-000000000001"}, "job": {"namespace": "food", "name": "etl_menus"},
                 "inputs": [{"namespace": "food", "name": "tmp_menus"}], "outputs": [{"namespace": "food", "name": "public.menus"}], "producer": "t"}]
    (tmp_path / "marquez_metadata.template.json").write_text(json.dumps(template))
    monkeypatch.setattr(demos, "DEMO_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def viewer_headers(client, db):
    db.add(User(id=str(uuid.uuid4()), username="viewer", email="viewer@test.com", password_hash=hash_password("view123"), role="viewer"))
    db.commit()
    token = client.post("/api/v1/auth/login", json={"username": "viewer", "password": "view123"}).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_demo_list_says_what_is_missing_and_what_is_there(client, auth_headers, tmp_path, monkeypatch, demo_dir):
    listed = {d["id"]: d for d in client.get(f"{BASE}/demos", headers=auth_headers).json()["datasets"]}
    assert listed["splink-fake-1000"]["available"] is True and listed["splink-fake-1000"]["rows"] > 100
    assert "first_name" in listed["splink-fake-1000"]["columns"] and listed["splink-fake-1000"]["source"]["license"] == "MIT"
    assert listed["splink-historical-50k"]["available"] is False and listed["splink-historical-50k"]["bytes"] is None
    assert {d["tool"] for d in listed.values()} == {"splink", "great_expectations", "marquez"}
    missing = client.post(f"{BASE}/health/run", headers=auth_headers, json={"dataset_id": "splink-historical-50k"})
    assert missing.status_code == 409 and missing.json()["detail"]["code"] == "demo_unavailable"
    assert client.post(f"{BASE}/health/run", headers=auth_headers, json={"dataset_id": "nope"}).status_code == 404


def test_a_changed_file_is_refused(client, auth_headers, demo_dir, monkeypatch):
    entry = demos.entry("splink-fake-1000")
    monkeypatch.setitem(entry, "sha256", "0" * 64)
    refused = client.post(f"{BASE}/health/run", headers=auth_headers, json={"dataset_id": "splink-fake-1000"})
    assert refused.status_code == 409 and "SHA-256" in refused.json()["detail"]["message"]


def test_preview_returns_rows_and_columns(client, auth_headers, demo_dir):
    body = client.get(f"{BASE}/demos/splink-fake-1000/preview?limit=5", headers=auth_headers).json()
    assert len(body["rows"]) == 5 and body["columns"][0] == "unique_id" and body["total"] > 100


def test_health_run_is_stored_and_listed(client, auth_headers, demo_dir):
    suite = {"name": "mine", "expectations": [{"expectation_type": "expect_column_values_to_be_unique", "kwargs": {"column": "unique_id"}},
                                              {"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": "email"}}]}
    run = client.post(f"{BASE}/health/run", headers=auth_headers, json={"dataset_id": "splink-fake-1000", "suite": suite}).json()
    assert run["kind"] == "health" and run["summary"]["evaluated_expectations"] == 2
    assert run["result"]["results"][1]["success"] is False and run["params"]["custom_suite"] is True
    assert client.get(f"{BASE}/runs/{run['id']}", headers=auth_headers).json()["result"]["meta"]["suite_name"] == "mine"
    listed = client.get(f"{BASE}/runs?kind=health", headers=auth_headers).json()["runs"]
    assert [r["id"] for r in listed] == [run["id"]] and "result" not in listed[0]
    default = client.get(f"{BASE}/health/suites/splink-fake-1000", headers=auth_headers).json()
    assert default["suite"]["name"] == "fake_1000_basic" and "expect_column_values_to_be_unique" in default["expectation_types"]


def test_a_bad_suite_is_a_422_with_the_reason(client, auth_headers, demo_dir):
    bad = {"expectations": [{"expectation_type": "expect_column_values_to_be_unique", "kwargs": {"column": "unique_id"}}] * 201}
    response = client.post(f"{BASE}/health/run", headers=auth_headers, json={"dataset_id": "splink-fake-1000", "suite": bad})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_suite"


def test_resolution_run_scores_the_result(client, auth_headers, demo_dir):
    run = client.post(f"{BASE}/resolution/run", headers=auth_headers, json={"dataset_id": "splink-fake-1000"}).json()
    assert run["kind"] == "resolution" and run["summary"]["f1"] > 0.8
    assert run["result"]["evaluation"]["precision"] >= 0.9 and run["result"]["model"]["comparisons"]
    bad = client.post(f"{BASE}/resolution/run", headers=auth_headers, json={"dataset_id": "splink-fake-1000", "settings": {"unique_id": "nope"}})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_resolution_settings"
    assert client.get(f"{BASE}/resolution/presets/splink-fake-1000", headers=auth_headers).json()["settings"]["blocking_rules"]


def test_lineage_events_graph_impact_and_reset(client, auth_headers, demo_dir):
    def event(kind, job, run, time, inputs=(), outputs=()):
        return {"eventType": kind, "eventTime": time, "run": {"runId": run}, "job": {"namespace": "t", "name": job}, "inputs": [{"namespace": "t", "name": n} for n in inputs],
                "outputs": [{"namespace": "t", "name": n} for n in outputs], "producer": "x"}
    events = [event("COMPLETE", "a", "1", "2026-01-01T00:00:00Z", outputs=["d1"]), event("COMPLETE", "b", "2", "2026-01-01T00:01:00Z", inputs=["d1"], outputs=["d2"])]
    assert client.post(f"{BASE}/lineage/events", headers=auth_headers, json=events).json() == {"accepted": 2}
    graph = client.get(f"{BASE}/lineage/graph?namespace=t", headers=auth_headers).json()
    assert {n["id"] for n in graph["nodes"]} == {"job:t:a", "job:t:b", "dataset:t:d1", "dataset:t:d2"} and graph["event_count"] == 2
    focused = client.get(f"{BASE}/lineage/graph?node=dataset:t:d1&depth=1&direction=downstream", headers=auth_headers).json()
    assert {n["id"] for n in focused["nodes"]} == {"dataset:t:d1", "job:t:b"} and focused["focus"] == "dataset:t:d1"
    assert client.get(f"{BASE}/lineage/graph?node=dataset:t:nope", headers=auth_headers).status_code == 404
    impact = client.get(f"{BASE}/lineage/impact?node=job:t:a", headers=auth_headers).json()
    assert [a["id"] for a in impact["downstream"]] == ["dataset:t:d1", "job:t:b", "dataset:t:d2"]
    invalid = client.post(f"{BASE}/lineage/events", headers=auth_headers, json={"eventType": "START"})
    assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "invalid_lineage_event"
    assert client.delete(f"{BASE}/lineage", headers=auth_headers).status_code == 400
    assert client.delete(f"{BASE}/lineage?namespace=t", headers=auth_headers).json() == {"deleted": 2}
    assert client.get(f"{BASE}/lineage/graph", headers=auth_headers).json()["nodes"] == []


def test_seeding_the_marquez_demo_twice_does_not_duplicate_it(client, auth_headers, demo_dir):
    assert client.post(f"{BASE}/lineage/seed", headers=auth_headers, json={}).json()["events"] == 2
    assert client.post(f"{BASE}/lineage/seed", headers=auth_headers, json={}).json()["events"] == 2
    graph = client.get(f"{BASE}/lineage/graph?namespace=food", headers=auth_headers).json()
    assert graph["event_count"] == 2
    job = next(n for n in graph["nodes"] if n["kind"] == "job")
    assert job["status"] == "success" and job["last_duration_seconds"] == 300.0


def test_the_pipeline_chains_the_engines_and_leaves_a_lineage_graph(client, auth_headers, demo_dir):
    run = client.post(f"{BASE}/pipeline/run", headers=auth_headers, json={}).json()
    assert run["kind"] == "pipeline" and list(run["summary"]) == ["ingest", "validate", "resolve"] and len(run["result"]["steps"]) == 3
    assert run["result"]["resolution"]["evaluation"]["f1"] > 0.8 and run["result"]["health"]["statistics"]["evaluated_expectations"] >= 8
    graph = client.get(f"{BASE}/lineage/graph?namespace=workbench", headers=auth_headers).json()
    nodes = {n["id"]: n for n in graph["nodes"]}
    raw = nodes["dataset:workbench:splink_fake_1000.raw"]
    assert raw["quality"]["failed"] >= 1 and raw["quality"]["passed"] >= 1  # the demo suite's row-count check fails on the small stand-in
    assert [f["name"] for f in raw["fields"]][:2] == ["unique_id", "first_name"]
    entities = nodes["dataset:workbench:splink_fake_1000.entities"]
    assert "entity_id" in entities["column_lineage"] and any(x.startswith("splink_fake_1000.raw.") for x in entities["column_lineage"]["entity_id"])
    assert all(nodes[f"job:workbench:splink_fake_1000.{step}"]["status"] == "success" for step in ("ingest", "validate", "resolve"))
    upstream = client.get(f"{BASE}/lineage/graph?node=dataset:workbench:splink_fake_1000.entities&direction=upstream&depth=5", headers=auth_headers).json()
    assert {n["id"] for n in upstream["nodes"]} == {"dataset:workbench:splink_fake_1000.entities", "job:workbench:splink_fake_1000.resolve", "dataset:workbench:splink_fake_1000.raw", "job:workbench:splink_fake_1000.ingest"}
    again = client.post(f"{BASE}/pipeline/run", headers=auth_headers, json={}).json()
    graph = client.get(f"{BASE}/lineage/graph?namespace=workbench", headers=auth_headers).json()
    assert next(n for n in graph["nodes"] if n["id"] == "job:workbench:splink_fake_1000.resolve")["run_count"] == 2 and again["id"] != run["id"]


def test_only_editors_run_things_but_everyone_signed_in_can_look(client, viewer_headers, auth_headers, demo_dir):
    assert client.get(f"{BASE}/demos", headers=viewer_headers).status_code == 200
    assert client.get(f"{BASE}/lineage/graph", headers=viewer_headers).status_code == 200
    for method, path, body in [("post", "/health/run", {"dataset_id": "splink-fake-1000"}), ("post", "/resolution/run", {"dataset_id": "splink-fake-1000"}),
                               ("post", "/pipeline/run", {}), ("post", "/lineage/seed", {}), ("post", "/lineage/events", []), ("delete", "/lineage?namespace=t", None)]:
        response = getattr(client, method)(f"{BASE}{path}", headers=viewer_headers, **({} if body is None else {"json": body}))
        assert response.status_code == 403, (path, response.status_code)
    assert client.get(f"{BASE}/demos").status_code in (401, 403)


def test_only_one_heavy_run_at_a_time(client, auth_headers, demo_dir):
    assert router._BUSY.acquire(blocking=False)
    try:
        response = client.post(f"{BASE}/resolution/run", headers=auth_headers, json={"dataset_id": "splink-fake-1000"})
    finally:
        router._BUSY.release()
    assert response.status_code == 409 and response.json()["detail"]["code"] == "busy"
