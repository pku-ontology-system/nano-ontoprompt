"""The analysis engines on the platform's own ontologies: the suite an object type implies, objects as a table, duplicate types."""
from types import SimpleNamespace

import pandas as pd

from app.services.v2.analysis import ontology_analysis, ontology_duplicates as dup, ontology_source
from app.services.v2.analysis.ontology_suite import build_ontology_suite, identity_property


def props(*items):
    return [{"api_name": name, "data_type": kind, "nullable": True, "is_array": False} for name, kind in items]


def by_column(suite):
    return {(e["expectation_type"].removeprefix("expect_"), e["kwargs"].get("column")): e for e in suite["expectations"]}


ASSET = props(("asset_id", "string"), ("name", "string"), ("resource_id", "string"), ("health_score", "double"), ("latest_cycle", "integer"),
              ("start", "timestamp"), ("ready", "boolean"), ("tags", "json"))


def test_identity_is_the_recorded_primary_key_or_the_property_named_after_the_type():
    assert identity_property("ProductionResource", ["resource_id", "resource_name"], ["resource_id"]) == ("resource_id", "Auto Ontology 记录的主键")
    assert identity_property("FactoryAsset", ["asset_id", "resource_id"], [])[0] == "asset_id"
    assert identity_property("ScheduleItem", ["schedule_id", "name"], [])[0] == "schedule_id"
    assert identity_property("Person", ["first_name", "surname"], []) == (None, "")
    assert identity_property("Thing", ["id", "x"], [])[0] == "id"


def test_suite_is_read_off_the_type_and_says_where_each_expectation_came_from():
    suite = build_ontology_suite("FactoryAsset", ASSET, references=[{"property": "resource_id", "target_type": "FactoryResource", "target_property": "resource_id",
                                                                    "basis": "declared_fk", "values": ["r2", "r1"]}])
    found = by_column(suite)
    assert suite["name"] == "FactoryAsset_ontology"
    assert found[("table_row_count_to_be_between", None)]["kwargs"] == {"min_value": 1}
    assert ("column_values_to_not_be_null", "asset_id") in found and ("column_values_to_be_unique", "asset_id") in found
    assert found[("column_values_to_match_json_schema", "health_score")]["kwargs"]["json_schema"] == {"type": "number"}
    assert found[("column_values_to_match_json_schema", "latest_cycle")]["kwargs"]["json_schema"] == {"type": "integer"}
    assert found[("column_values_to_match_json_schema", "ready")]["kwargs"]["json_schema"] == {"type": "boolean"}
    assert ("column_values_to_be_dateutil_parseable", "start") in found
    assert not any(column == "tags" for _, column in found), "json and array properties are not judged by type"
    reference = found[("column_values_to_be_in_set", "resource_id")]
    assert reference["kwargs"]["value_set"] == ["r1", "r2"] and reference["kwargs"]["mostly"] == 1.0
    assert "FactoryAsset.resource_id" in reference["meta"]["notes"] and "FactoryResource.resource_id" in reference["meta"]["notes"]
    assert {e["meta"]["source"] for e in suite["expectations"]} == {"本体"}


def test_required_unique_and_declared_constraints_become_expectations():
    properties = [{**p, "nullable": p["api_name"] != "name"} for p in ASSET]
    raw = {"health_score": {"id": "health_score", "type": "double", "minimum": 0, "maximum": 100}, "name": {"pattern": "^EQ"},
           "ready": {"enum": [True]}, "latest_cycle": {"unique": True}}
    suite = build_ontology_suite("FactoryAsset", properties, raw_definitions=raw, unique_columns=["latest_cycle", "ghost"])
    found = by_column(suite)
    assert ("column_values_to_not_be_null", "name") in found
    assert found[("column_values_to_be_between", "health_score")]["kwargs"] == {"column": "health_score", "min_value": 0, "max_value": 100}
    assert found[("column_values_to_match_regex", "name")]["kwargs"]["regex"] == "^EQ"
    assert found[("column_values_to_be_in_set", "ready")]["kwargs"]["value_set"] == [True]
    assert [k for k in found if k[0] == "column_values_to_be_unique"].count(("column_values_to_be_unique", "latest_cycle")) == 1, "one expectation per column and kind"
    assert ("column_values_to_be_unique", "ghost") not in found, "a column the type does not have is ignored"


def test_a_foreign_key_with_too_many_targets_or_none_is_not_checked():
    many = build_ontology_suite("FactoryAsset", ASSET, references=[{"property": "resource_id", "target_type": "T", "target_property": "id", "values": list(range(3000))}])
    none = build_ontology_suite("FactoryAsset", ASSET, references=[{"property": "resource_id", "target_type": "T", "target_property": "id", "values": None},
                                                                  {"property": "resource_id", "target_type": "T", "target_property": "id", "values": []}])
    assert ("column_values_to_be_in_set", "resource_id") not in by_column(many)
    assert ("column_values_to_be_in_set", "resource_id") not in by_column(none)


def objects(*rows):
    return [SimpleNamespace(object_id=f"o{i}", properties=row) for i, row in enumerate(rows, 1)]


def test_objects_become_a_table_with_the_object_id_first_and_containers_as_text():
    frame = ontology_source.frame_from_objects(objects({"a": 1, "tags": ["x", "y"], "box": {"k": 1}}, {"a": 2}), ["a", "tags", "box", "missing"])
    assert list(frame.columns) == ["__object_id", "a", "tags", "box", "missing"]
    assert frame["__object_id"].tolist() == ["o1", "o2"]
    assert frame["tags"].iloc[0] == '["x", "y"]' and frame["box"].iloc[0] == '{"k": 1}'
    assert pd.isna(frame["tags"].iloc[1]) and frame["missing"].isna().all()


def test_failures_point_back_to_the_objects_that_broke_them():
    rows = [{"asset_id": "a", "name": "n", "resource_id": "r1", "health_score": 1.5, "latest_cycle": 4, "start": "2020-01-01", "ready": True, "tags": None},
            {"asset_id": "a", "name": "n", "resource_id": "r9", "health_score": "bad", "latest_cycle": 5, "start": "2020-01-02", "ready": False, "tags": None},
            {"asset_id": None, "name": "n", "resource_id": "r2", "health_score": 2.0, "latest_cycle": 6, "start": "not a date", "ready": True, "tags": None}]
    frame = ontology_source.frame_from_objects(objects(*rows), [p["api_name"] for p in ASSET])
    suite = build_ontology_suite("FactoryAsset", ASSET, references=[{"property": "resource_id", "target_type": "FactoryResource", "target_property": "resource_id",
                                                                    "basis": "declared_fk", "values": ["r1", "r2"]}])
    report = ontology_analysis.run_health(frame, suite, dataset="ontology:x:FactoryAsset")
    assert report["meta"]["object_count"] == 3 and report["statistics"]["errored_expectations"] == 0
    failed = {(r["expectation_type"].removeprefix("expect_"), r["kwargs"].get("column")): r for r in report["results"] if not r["success"]}
    assert failed[("column_values_to_not_be_null", "asset_id")]["result"]["partial_unexpected_object_ids"] == ["o3"]
    assert failed[("column_values_to_be_unique", "asset_id")]["result"]["partial_unexpected_object_ids"] == ["o1", "o2"]
    assert failed[("column_values_to_match_json_schema", "health_score")]["result"]["partial_unexpected_object_ids"] == ["o2"]
    assert failed[("column_values_to_be_in_set", "resource_id")]["result"]["partial_unexpected_object_ids"] == ["o2"]
    assert failed[("column_values_to_be_dateutil_parseable", "start")]["result"]["partial_unexpected_object_ids"] == ["o3"]
    assert ("column_values_to_not_be_null", "name") not in failed


def test_a_type_without_objects_fails_the_row_count_and_nothing_else_is_judged():
    frame = ontology_source.frame_from_objects([], ["asset_id", "name"])
    report = ontology_analysis.run_health(frame, build_ontology_suite("FactoryAsset", props(("asset_id", "string"), ("name", "string"))), dataset="d")
    assert report["meta"]["object_count"] == 0
    assert [r["expectation_type"] for r in report["results"] if not r["success"]] == ["expect_table_row_count_to_be_between"]
    assert isinstance(frame, pd.DataFrame)


def descriptor(name, properties, tables=(), objects=None, keys=()):
    return dup.describe(name, properties, tables=set(tables), primary_keys=list(keys), objects=objects)


FACTORY = [
    descriptor("FactoryAsset", ["asset_id", "name", "resource_id", "risk_level", "health_score"], ["factory_asset"], 5),
    descriptor("FactoryResource", ["resource_id", "name", "capability"], ["factory_resource"], 4),
    descriptor("ProductionResource", ["resource_id", "resource_name", "capability"], ["factory_resource"], 0, ["resource_id"]),
    descriptor("ScheduleItem", ["schedule_id", "name", "resource", "start", "end", "layer", "order_id", "status", "priority", "due", "duration_minutes", "material_ready"],
               ["schedule_item"], 13),
    descriptor("ScheduledItem", ["item_id", "item_name", "primary_resource_id", "order_id", "layer", "start_at", "end_at", "status", "priority", "material_ready",
                                 "duration_minutes"], ["schedule_item"], 0, ["item_id"]),
    descriptor("WorkOrder", ["order_id", "order_type", "due_at"], ["work_order"], 0, ["order_id"]),
]


def test_words_stems_and_names_are_compared_without_the_type_in_the_way():
    assert dup.words("ScheduledItem") == ["scheduled", "item"] and dup.words("resource_id") == ["resource", "id"]
    assert dup.stem("scheduled") == dup.stem("schedule") and dup.stem("resources") == dup.stem("resource")
    assert dup.name_similarity("ScheduleItem", "ScheduledItem") == 1.0
    assert dup.name_similarity("FactoryAsset", "WorkOrder") < 0.3
    keys = dup.property_keys("ProductionResource", ["resource_id", "resource_name", "capability", "start_at"], "resource_id")
    plain = dup.property_keys("FactoryResource", ["name", "start"], None)
    assert keys["resource_id"] == "id" and keys["resource_name"] == plain["name"] and keys["start_at"] == plain["start"] and keys["capability"] == dup.stem("capability")


def test_two_types_for_one_table_are_found_and_the_others_are_left_alone():
    pairs = dup.find(FACTORY)
    assert [(p["a"], p["b"]) for p in pairs] == [("ScheduleItem", "ScheduledItem"), ("FactoryResource", "ProductionResource")]
    schedule, resource = pairs
    assert schedule["verdict"] == "likely" and resource["verdict"] == "likely"
    assert schedule["shared_tables"] == ["schedule_item"] and resource["shared_tables"] == ["factory_resource"]
    assert {"a": "name", "b": "resource_name"} in resource["property_overlap"]["shared"] and resource["property_overlap"]["only_a"] == []
    assert {"a": "schedule_id", "b": "item_id"} in schedule["property_overlap"]["shared"] and schedule["property_overlap"]["only_b"] == ["primary_resource_id"]
    assert any("同一张表" in reason for reason in schedule["reasons"]) and any("名字相近" in reason for reason in resource["reasons"])
    assert "保留 ScheduleItem（已有 13 个对象）" in schedule["suggestion"] and "ScheduledItem 还没有对象" in schedule["suggestion"]
    assert "保留 FactoryResource" in resource["suggestion"]
    everything = dup.find(FACTORY, minimum=0.0)
    assert len(everything) == 15 and everything == sorted(everything, key=lambda c: (-c["score"], c["a"], c["b"]))


def test_the_score_is_the_stated_weighted_sum():
    pair = dup.compare(FACTORY[1], FACTORY[2])
    expected = dup.WEIGHTS["name"] * pair["name_similarity"] + dup.WEIGHTS["properties"] * pair["property_overlap"]["score"] + dup.WEIGHTS["table"] * 1.0
    assert abs(pair["score"] - expected) < 0.002
    assert dup.compare(FACTORY[0], FACTORY[5])["score"] < dup.POSSIBLE


def test_suggestion_depends_on_which_side_has_objects():
    both = dup.compare(descriptor("A", ["x"], objects=3), descriptor("B", ["x"], objects=2))
    neither = dup.compare(descriptor("A", ["x"], objects=0), descriptor("B", ["x", "y"], objects=0))
    unknown = dup.compare(descriptor("A", ["x"], objects=None), descriptor("B", ["x"], objects=0))
    assert "实体消歧" in both["suggestion"] and "保留属性更完整的 B" in neither["suggestion"] and "对象数量未知" in unknown["suggestion"]


def test_a_candidate_is_compared_with_the_types_that_exist():
    candidate = descriptor("ProductionResource", ["resource_id", "resource_name", "capability"], ["factory_resource"], 0, ["resource_id"])
    candidate["origin"] = "candidate"
    pairs = dup.find([candidate], others=FACTORY[:2] + FACTORY[3:])
    assert [(p["a"], p["b"]) for p in pairs] == [("ProductionResource", "FactoryResource")]
    assert pairs[0]["origin"] == {"a": "candidate", "b": "type"}
