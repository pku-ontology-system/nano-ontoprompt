"""The analysis API on a platform ontology. The object query is stood in by fake objects; the metadata, the stored runs and the lineage are real."""
import uuid
from types import SimpleNamespace

import pandas as pd
import pytest

from app.main import app
from app.models.user import User
from app.models.v2.ontology_candidates import OntologyCandidateItem, OntologyCandidateSet
from app.models.v2.rule_runtime import RuleResource, RuleVersion
from app.routers.v2 import analysis as router
from app.services.auth_service import hash_password
from app.services.v2.analysis import ontology_source
from tests.test_analysis_resolution import noisy_people

BASE = "/api/v2/analysis"
COLUMNS = (("unique_id", "integer"), ("first_name", "string"), ("surname", "string"), ("dob", "date"), ("city", "string"), ("email", "string"), ("cluster", "integer"))
PERSON = {"api_name": "Person", "properties": [{"api_name": c, "data_type": t, "nullable": True, "is_array": False} for c, t in COLUMNS]}
GADGET = {"api_name": "Gadget", "properties": [{"api_name": "gadget_id", "data_type": "string", "nullable": True, "is_array": False}]}


def person_objects():
    frame = noisy_people(people=70)
    clean = lambda v: None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)  # noqa: E731
    return [SimpleNamespace(object_id=f"p{int(row['unique_id'])}", properties={k: clean(v) for k, v in row.items()}) for row in frame.to_dict("records")]


@pytest.fixture
def objects(monkeypatch):
    """Two types, one with 70 people's records and one with none, behind the same functions the object query would answer."""
    people = person_objects()
    monkeypatch.setattr(ontology_source, "object_types", lambda db, user, oid: [GADGET, PERSON])
    monkeypatch.setattr(ontology_source, "object_counts", lambda *a, **k: {"Person": {"count": len(people), "capped": True}, "Gadget": {"count": 0, "capped": False}})
    monkeypatch.setattr(ontology_source, "load_objects", lambda db, user, graph, oid, otype, limit=10_000: (people if otype == "Person" else [], {"object_type": otype, "snapshot": False, "truncated": False}))
    app.dependency_overrides[router.get_falkordb] = lambda: object()
    yield people
    app.dependency_overrides.pop(router.get_falkordb, None)


@pytest.fixture
def viewer_headers(client, db):
    db.add(User(id=str(uuid.uuid4()), username="viewer", email="viewer@test.com", password_hash=hash_password("view123"), role="viewer"))
    db.commit()
    token = client.post("/api/v1/auth/login", json={"username": "viewer", "password": "view123"}).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_types_come_with_their_properties_and_object_counts(client, auth_headers, ontology, objects):
    body = client.get(f"{BASE}/ontologies/{ontology['id']}/object-types", headers=auth_headers).json()
    assert [t["api_name"] for t in body["types"]] == ["Gadget", "Person"]
    person = body["types"][1]
    assert person["object_count"] == len(objects) and person["object_count_capped"] is True and person["properties"][0]["api_name"] == "unique_id"
    assert body["types"][0]["object_count"] == 0 and body["types"][0]["object_count_capped"] is False


def test_the_suite_a_type_implies_is_shown_before_it_is_run(client, auth_headers, ontology, objects):
    body = client.get(f"{BASE}/ontologies/{ontology['id']}/health/suites/Person?baseline=true", headers=auth_headers).json()
    kinds = {e["expectation_type"] for e in body["suite"]["expectations"]}
    sources = {e["meta"]["source"] for e in body["suite"]["expectations"]}
    assert {"expect_table_row_count_to_be_between", "expect_column_values_to_match_json_schema", "expect_column_values_to_be_dateutil_parseable"} <= kinds
    assert sources == {"本体", "数据现状"} and body["read"]["row_count"] == len(objects) and body["read"]["counts"]["ontology"] > 5
    assert "expect_column_values_to_match_json_schema" in body["expectation_types"]
    assert client.get(f"{BASE}/ontologies/{ontology['id']}/health/suites/Nope", headers=auth_headers).status_code == 404


def test_health_on_objects_names_the_objects_that_fail(client, auth_headers, ontology, objects):
    suite = {"name": "mine", "expectations": [{"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": "email"}},
                                              {"expectation_type": "expect_column_values_to_match_json_schema", "kwargs": {"column": "city", "json_schema": {"type": "string"}}}]}
    run = client.post(f"{BASE}/ontologies/{ontology['id']}/health/run", headers=auth_headers, json={"object_type": "Person", "suite": suite}).json()
    assert run["kind"] == "health" and run["params"]["object_type"] == "Person" and run["params"]["custom_suite"] is True
    assert run["dataset_id"] == f"ontology:{ontology['id']}:Person" and run["summary"]["object_count"] == len(objects)
    nulls, types = run["result"]["results"]
    assert nulls["success"] is False and nulls["result"]["unexpected_count"] > 0
    known = {o.object_id for o in objects}
    assert nulls["result"]["partial_unexpected_object_ids"] and set(nulls["result"]["partial_unexpected_object_ids"]) <= known
    assert types["success"] is True and run["result"]["source"] == {"kind": "ontology", "ontology_id": ontology["id"], "object_type": "Person", "read": run["result"]["source"]["read"]}
    assert client.get(f"{BASE}/runs/{run['id']}", headers=auth_headers).json()["result"]["source"]["object_type"] == "Person"
    assert client.get(f"{BASE}/runs?kind=health&dataset_id={run['dataset_id']}", headers=auth_headers).json()["runs"][0]["id"] == run["id"]


def test_a_type_without_objects_fails_its_row_count(client, auth_headers, ontology, objects):
    run = client.post(f"{BASE}/ontologies/{ontology['id']}/health/run", headers=auth_headers, json={"object_type": "Gadget"}).json()
    failed = [r for r in run["result"]["results"] if not r["success"]]
    assert [r["expectation_type"] for r in failed] == ["expect_table_row_count_to_be_between"] and run["summary"]["object_count"] == 0


def test_an_unknown_type_is_a_404_and_a_viewer_cannot_run(client, auth_headers, viewer_headers, ontology, objects):
    url = f"{BASE}/ontologies/{ontology['id']}"
    missing = client.post(f"{url}/health/run", headers=auth_headers, json={"object_type": "Nope"})
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "object_type_not_found"
    assert client.post(f"{url}/health/run", headers=viewer_headers, json={"object_type": "Person"}).status_code == 403
    assert client.post(f"{url}/resolution/run", headers=viewer_headers, json={"object_type": "Person"}).status_code == 403
    assert client.post(f"{url}/lineage/generate", headers=viewer_headers).status_code == 403
    assert client.get(f"{url}/health/suites/Person", headers=viewer_headers).status_code == 200


def test_resolution_settings_are_drafted_from_the_properties_and_explained(client, auth_headers, ontology, objects):
    body = client.get(f"{BASE}/ontologies/{ontology['id']}/resolution/presets/Person", headers=auth_headers).json()
    settings = body["settings"]
    assert settings["unique_id"] == "__object_id" and settings["ground_truth"] == "cluster" and body["truth_candidates"] == ["cluster"]
    assert {c["column"]: c["type"] for c in settings["comparisons"]} == {"first_name": "name", "surname": "name", "dob": "levenshtein", "city": "exact", "email": "email"}
    assert {s["column"] for s in body["skipped"]} == {"unique_id", "cluster"} and any(note.startswith("first_name：") for note in body["notes"])
    assert settings["blocking_rules"] and body["estimated_pairs"] > 0 and body["read"]["row_count"] == len(objects)
    none = client.get(f"{BASE}/ontologies/{ontology['id']}/resolution/presets/Person?ground_truth=", headers=auth_headers).json()
    assert none["settings"]["ground_truth"] is None
    assert client.get(f"{BASE}/ontologies/{ontology['id']}/resolution/presets/Gadget", headers=auth_headers).status_code == 422


def test_resolution_on_objects_groups_object_ids_and_scores_against_the_truth_column(client, auth_headers, ontology, objects):
    run = client.post(f"{BASE}/ontologies/{ontology['id']}/resolution/run", headers=auth_headers, json={"object_type": "Person"}).json()
    assert run["kind"] == "resolution" and run["summary"]["records"] == len(objects) and run["summary"]["entities_found"] < len(objects)
    assert run["summary"]["f1"] > 0.5 and run["result"]["source"]["object_type"] == "Person" and run["result"]["source"]["notes"]
    members = {record["__object_id"] for cluster in run["result"]["clusters"] for record in cluster["records"]}
    assert members and members <= {o.object_id for o in objects}
    unscored = client.post(f"{BASE}/ontologies/{ontology['id']}/resolution/run", headers=auth_headers, json={"object_type": "Person", "ground_truth": ""}).json()
    assert unscored["result"]["evaluation"] is None and "f1" not in unscored["summary"]
    mine = client.post(f"{BASE}/ontologies/{ontology['id']}/resolution/run", headers=auth_headers,
                       json={"object_type": "Person", "settings": {"comparisons": [{"column": "email", "type": "email"}], "blocking_rules": [["email"]]}}).json()
    assert mine["params"]["custom_settings"] is True and mine["result"]["source"]["notes"] == []


def test_two_types_for_one_thing_are_reported_with_their_reasons(client, auth_headers, ontology, monkeypatch, objects):
    def types(db, user, oid):
        names = lambda *items: [{"api_name": n, "data_type": "string", "nullable": True, "is_array": False} for n in items]  # noqa: E731
        return [{"api_name": "FactoryResource", "properties": names("resource_id", "name", "capability")},
                {"api_name": "ProductionResource", "properties": names("resource_id", "resource_name", "capability")},
                {"api_name": "WorkOrder", "properties": names("order_id", "order_type")}]

    monkeypatch.setattr(ontology_source, "object_types", types)
    monkeypatch.setattr(ontology_source, "object_counts", lambda *a, **k: {"FactoryResource": {"count": 4, "capped": False}, "ProductionResource": {"count": 0, "capped": False},
                                                                           "WorkOrder": {"count": 2, "capped": False}})
    body = client.get(f"{BASE}/ontologies/{ontology['id']}/duplicate-types", headers=auth_headers).json()
    assert body["types_compared"] == 3 and [(p["a"], p["b"]) for p in body["pairs"]] == [("FactoryResource", "ProductionResource")]
    pair = body["pairs"][0]
    assert pair["verdict"] == "possible" and pair["shared_tables"] == [], "no source table is recorded here, so name and properties alone make it only a possible duplicate"
    assert "保留 FactoryResource（已有 4 个对象）" in pair["suggestion"] and {"a": "name", "b": "resource_name"} in pair["property_overlap"]["shared"]
    strict = client.get(f"{BASE}/ontologies/{ontology['id']}/duplicate-types?minimum=0.95", headers=auth_headers).json()
    assert strict["pairs"] == []
    assert client.get(f"{BASE}/ontologies/{ontology['id']}/duplicate-types?minimum=0.05", headers=auth_headers).status_code == 422


def publish_person_build(db, admin_user, ontology_id):
    """What the candidate review leaves behind: a published set whose accepted item recorded the table and the column of every property."""
    cset = OntologyCandidateSet(ontology_id=ontology_id, origin="file", status="published", title="人员主数据表", created_by=admin_user.id,
                                generator={"model_label": "未使用模型", "source_label": "人员库", "locality": "local", "seconds": 3, "sample": True}, progress={}, warnings=[])
    db.add(cset)
    db.flush()
    properties = [{"id": c, "name": c, "type": t, "source_field": f"person_master.{c}"} for c, t in COLUMNS]
    db.add(OntologyCandidateItem(set_id=cset.id, ordinal=0, kind="entity_type", key="entity:Person", name="Person", basis="term", decision="accepted",
                                 draft={"name_en": "Person", "name_cn": "人员", "properties": properties},
                                 evidence={"source_table": "person_master", "primary_key": ["unique_id"], "columns": [{"name": c, "type": t, "unique": c == "unique_id"} for c, t in COLUMNS]}))
    version_logic = {"input": {"kind": "base", "type_ref": {"kind": "object", "api_name": "Person"}},
                     "blocks": [{"kind": "filter", "where": {"kind": "comparison", "property": {"api_name": "city"}, "op": "ne", "value": "London"}}],
                     "output": {"severity": "low", "evidence_properties": ["first_name", "surname", "ghost"]}}
    rule = RuleResource(ontology_id=ontology_id, api_name="not_london", display_name="不在伦敦的人员", description="示例规则", status="active", current_version=1, owner_id=admin_user.id)
    db.add(rule)
    db.flush()
    db.add(RuleVersion(rule_id=rule.id, version=1, logic=version_logic, logic_digest="0" * 64, proposal_id=str(uuid.uuid4())))
    db.commit()


def test_rule_logic_is_read_for_the_properties_it_touches():
    from app.services.v2.analysis.ontology_lineage import rule_properties

    logic = {"blocks": [{"kind": "filter", "where": {"kind": "and", "terms": [{"property": {"api_name": "risk_level"}, "op": "ne", "value": "High"}, {"property": "status", "op": "eq"}]}},
                        {"kind": "series_window", "series_property": "t50_series", "window_points": 10}],
             "output": {"evidence_properties": ["name", "health_score", "name"]}}
    assert rule_properties(logic) == {"filters": ["risk_level", "status"], "series": ["t50_series"], "evidence": ["name", "health_score"]}
    assert rule_properties({}) == {"filters": [], "series": [], "evidence": []}


def test_lineage_of_an_ontology_is_written_from_what_the_platform_recorded(client, auth_headers, ontology, db, admin_user, objects):
    oid, url = ontology["id"], f"{BASE}/ontologies/{ontology['id']}"
    empty = client.post(f"{url}/lineage/generate", headers=auth_headers)
    assert empty.status_code == 422 and "没有可以画出血缘的内容" in empty.json()["detail"]["message"]
    publish_person_build(db, admin_user, oid)
    client.post(f"{url}/health/run", headers=auth_headers, json={"object_type": "Person"})
    client.post(f"{url}/resolution/run", headers=auth_headers, json={"object_type": "Person"})
    written = client.post(f"{url}/lineage/generate", headers=auth_headers).json()
    assert written["jobs"] == {"build": 1, "rule": 1, "analysis": 2} and written["events"] == 8 and written["replaced"] == 0
    again = client.post(f"{url}/lineage/generate", headers=auth_headers).json()
    assert again["replaced"] == 8, "regenerating replaces the earlier copy instead of adding to it"

    graph = client.get(f"{url}/lineage/graph", headers=auth_headers).json()
    by_name = {n["name"]: n for n in graph["nodes"]}
    assert by_name["person_master"]["layer"] == "source" and by_name["Person"]["layer"] == "object" and by_name["alerts.not_london"]["layer"] == "alert"
    assert by_name["rule.not_london"]["layer"] == "rule" and by_name["entities.Person"]["layer"] == "analysis" and by_name["health.Person"]["layer"] == "analysis"
    assert by_name["Person"]["ref"] == {"kind": "object_type", "ontology_id": oid, "api_name": "Person"}
    assert by_name["rule.not_london"]["ref"]["api_name"] == "not_london" and by_name["validate.Person"]["ref"]["run_kind"] == "health"
    assert graph["layers"]["alert"] == "告警" and graph["namespaces"] and graph["event_count"] == 8
    edges = {(e["source"].split(":")[-1], e["target"].split(":")[-1]) for e in graph["edges"]}
    assert ("person_master", next(n for n in by_name if n.startswith("autoontology."))) in edges and ("Person", "rule.not_london") in edges
    assert ("rule.not_london", "alerts.not_london") in edges and ("Person", "validate.Person") in edges and ("resolve.Person", "entities.Person") in edges
    columns = {(e["from"]["dataset"].split(":")[-1], e["from"]["field"], e["to"]["dataset"].split(":")[-1], e["to"]["field"]) for e in graph["column_edges"]}
    assert ("person_master", "email", "Person", "email") in columns and ("Person", "first_name", "alerts.not_london", "evidence.first_name") in columns
    assert ("Person", "city", "alerts.not_london", "subject") in columns and ("Person", "unique_id", "alerts.not_london", "subject") in columns
    assert not any(c[3] == "evidence.ghost" for c in columns), "a property the type does not have is not drawn"

    source = by_name["person_master"]["id"]
    impact = client.get(f"{url}/lineage/impact", params={"node": source, "field": "city"}, headers=auth_headers).json()
    reached = {(c["label"], c["field"]) for c in impact["column"]["columns"]}
    assert reached == {("Person", "city"), ("alerts.not_london", "subject"), ("entities.Person", "entity_id")}
    labels = {j["label"] for j in impact["column"]["jobs"]}
    assert {"rule.not_london", "resolve.Person"} <= labels and any(label.startswith("autoontology.") for label in labels) and "validate.Person" not in labels
    assert set(impact["column"]["layers"]) == {"object", "alert", "analysis"} and {j["layer"] for j in impact["column"]["jobs"]} == {"build", "rule", "analysis"}
    assert impact["downstream_datasets"] >= 3
    unrelated = client.get(f"{url}/lineage/impact", params={"node": source, "field": "no_such_column"}, headers=auth_headers).json()
    assert unrelated["column"]["columns"] == [] and unrelated["downstream_datasets"] >= 3
    focus = client.get(f"{url}/lineage/graph", params={"node": by_name["alerts.not_london"]["id"], "depth": 1, "direction": "upstream"}, headers=auth_headers).json()
    assert {n["name"] for n in focus["nodes"]} == {"alerts.not_london", "rule.not_london"} and focus["layers"]["rule"] == "规则"

    general = client.get(f"{BASE}/lineage/graph", headers=auth_headers).json()
    assert general["nodes"] == [] and general["namespaces"] == [], "the general lineage views do not serve an ontology's private lineage"
    assert client.get(f"{BASE}/lineage/impact", params={"node": source}, headers=auth_headers).status_code == 404


def test_one_type_goes_through_all_three_and_its_lineage_is_rewritten(client, auth_headers, ontology, db, admin_user, objects):
    oid, url = ontology["id"], f"{BASE}/ontologies/{ontology['id']}"
    publish_person_build(db, admin_user, oid)
    run = client.post(f"{url}/pipeline/run", headers=auth_headers, json={"object_type": "Person"}).json()
    assert run["kind"] == "pipeline" and run["dataset_id"] == f"ontology:{oid}:Person"
    steps = {s["key"]: s for s in run["result"]["steps"]}
    assert steps["ingest"]["summary"] == {"rows": len(objects), "columns": 7} and steps["validate"]["summary"]["evaluated_expectations"] > 5
    assert steps["resolve"]["summary"]["records"] == len(objects) and steps["resolve"]["summary"]["f1"] > 0.5
    ns = run["result"]["lineage"]["namespace"]
    assert steps["validate"]["node"] == f"job:{ns}:validate.Person" and run["result"]["lineage"]["focus"] == f"dataset:{ns}:Person"
    graph = client.get(f"{url}/lineage/graph", params={"node": run["result"]["lineage"]["focus"], "depth": 1}, headers=auth_headers).json()
    assert {"validate.Person", "resolve.Person", "rule.not_london"} <= {n["name"] for n in graph["nodes"]}, "the two runs hang off the type"
    runs = {r["kind"]: r for r in client.get(f"{BASE}/runs?dataset_id=ontology:{oid}:Person", headers=auth_headers).json()["runs"]}
    assert set(runs) == {"health", "resolution", "pipeline"} and run["result"]["runs"] == {"health": runs["health"]["id"], "resolution": runs["resolution"]["id"]}
    assert client.post(f"{url}/pipeline/run", headers=auth_headers, json={"object_type": "Nope"}).status_code == 404
