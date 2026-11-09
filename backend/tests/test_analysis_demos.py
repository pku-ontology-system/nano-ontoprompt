"""The bundled demo datasets run end to end and keep the effect the pages show."""
import time

import pytest

from app.services.v2.analysis import demos, health, lineage, resolution


def test_every_demo_file_matches_its_recorded_hash():
    for item in demos.CATALOG:
        assert demos.path_of(item).exists(), item["id"]
        assert demos.sha256_of(demos.path_of(item)) == item["sha256"], item["id"]
    assert {d["id"]: d["rows"] for d in demos.list_demos() if "rows" in d} == {"splink-fake-1000": 1000, "splink-historical-50k": 50578, "gx-taxi-2019-01": 10000}


def test_a_changed_demo_file_is_refused(monkeypatch):
    monkeypatch.setitem(demos.entry("splink-fake-1000"), "sha256", "f" * 64)
    with pytest.raises(demos.DemoError, match="SHA-256"):
        demos.load_frame("splink-fake-1000")


def test_fake_1000_is_linked_without_false_merges():
    out = resolution.run(demos.load_frame("splink-fake-1000"), demos.resolution_settings("splink-fake-1000"))
    evaluation = out["evaluation"]
    assert evaluation["true_entities"] == 251 and out["summary"]["records"] == 1000
    assert evaluation["precision"] >= 0.99 and evaluation["recall"] >= 0.88 and evaluation["f1"] >= 0.93, evaluation
    assert out["examples"]["false_negatives"], "the pairs it still misses are shown with their field-by-field reasons"


def test_the_50k_historical_persons_are_linked_in_reasonable_time():
    started = time.time()
    out = resolution.run(demos.load_frame("splink-historical-50k"), demos.resolution_settings("splink-historical-50k"))
    evaluation, summary = out["evaluation"], out["summary"]
    assert summary["records"] == 50578 and evaluation["true_entities"] == 5156
    assert evaluation["precision"] >= 0.93 and evaluation["recall"] >= 0.78 and evaluation["f1"] >= 0.85, evaluation
    assert summary["largest_cluster"] <= 100, "term-frequency adjustment keeps common values from chaining records into huge clusters"
    assert time.time() - started < 90


def test_taxi_suite_finds_the_known_errors_in_the_real_sample():
    report = health.validate(demos.load_frame("gx-taxi-2019-01"), demos.health_suite("gx-taxi-2019-01"), dataset_id="gx-taxi-2019-01")
    assert report["statistics"]["evaluated_expectations"] == 23
    failed = {(r["expectation_type"].removeprefix("expect_"), r["kwargs"].get("column") or r["kwargs"].get("column_A")): r["result"]["unexpected_count"] for r in report["results"] if not r["success"]}
    assert failed == {("column_values_to_be_between", "trip_distance"): 64, ("column_values_to_be_in_set", "vendor_id"): 96, ("column_values_to_be_in_set", "rate_code_id"): 1,
                      ("column_pair_values_a_to_be_greater_than_b", "dropoff_datetime"): 8, ("column_values_to_be_between", "fare_amount"): 9,
                      ("column_values_to_be_between", "total_amount"): 9, ("column_values_to_be_in_set", "mta_tax"): 6, ("column_values_to_not_be_null", "congestion_surcharge"): 6327}


def test_fake_1000_health_shows_the_gaps_that_make_linkage_necessary():
    report = health.validate(demos.load_frame("splink-fake-1000"), demos.health_suite("splink-fake-1000"), dataset_id="splink-fake-1000")
    failed = {r["expectation_type"].removeprefix("expect_") + ":" + str(r["kwargs"].get("column") or r["kwargs"].get("column_list")) for r in report["results"] if not r["success"]}
    assert failed == {"column_values_to_not_be_null:first_name", "column_values_to_not_be_null:surname", "column_values_to_not_be_null:city",
                      "compound_columns_to_be_unique:['first_name', 'surname', 'dob']"}


def test_marquez_seed_file_has_the_documented_shape(db, admin_user):
    events = lineage.render_template(demos.load_events_text("marquez-food-delivery"))
    assert len(events) == 26
    lineage.ingest(db, events, admin_user.id, source="demo:marquez")
    graph = lineage.graph_from_db(db, namespace="food_delivery")
    jobs = [n for n in graph["nodes"] if n["kind"] == "job"]
    datasets = [n for n in graph["nodes"] if n["kind"] == "dataset"]
    assert len(jobs) == 13 and len(datasets) == 13 and all(j["status"] == "success" for j in jobs)
    assert any(j["sql"] for j in jobs) and any(d["fields"] for d in datasets), "SQL and schema facets come through"
    impact = lineage.impact(graph, "dataset:food_delivery:public.menus")
    assert impact["downstream_jobs"] >= 1
