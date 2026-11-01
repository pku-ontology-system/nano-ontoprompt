"""Known-answer tests for the factory schedule decision Function (decision/v1).

The Function is user content that normally runs in the isolated function
runner; here it is loaded from its file so the arithmetic and the contract can
be checked without services.
"""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.services.v2.decision_output import validate_decision

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "factory_twin"


def _load_function():
    spec = importlib.util.spec_from_file_location("factory_decision_function", EXAMPLE / "decision_function.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FUNCTION = _load_function()


def _records(kind, rows, key):
    return [{"object_id": f"test0001:{kind}:{row['id']}",
             "properties": {**{k: v for k, v in row.items() if k != "id"}, key: row["id"],
                            **({"object_type": kind} if kind == "ScheduleItem" else {})}} for row in rows]


def _run(fixture):
    return FUNCTION.run({
        "schedule_items": _records("ScheduleItem", fixture["schedule_items"], "schedule_id"),
        "assets": _records("FactoryAsset", fixture["assets"], "asset_id"),
        "resources": [{"object_id": f"test0001:FactoryResource:{row['id']}", "properties": {"resource_id": row["id"], "name": row["label"]}}
                      for row in fixture["resources"]],
    })["decision"]


@pytest.fixture
def fixture():
    return json.loads((EXAMPLE / "data.json").read_text(encoding="utf-8"))


def test_output_satisfies_decision_contract(fixture):
    decision = _run(fixture)
    assert validate_decision(decision) is decision
    for candidate in decision["candidates"]:
        assert candidate["applicable"] == (not candidate["hard_constraints"])
        assert candidate["basis"] == "模拟"


def test_baseline_known_answers(fixture):
    decision = _run(fixture)
    # CNC production runs before the high-risk EQ005 is inspected: WO-100 machining 120 + assembly 120 + WO-200 machining 90.
    assert decision["metric"]["baseline"] == 330
    kpis = {row["label"]: row["value"] for row in decision["kpis"]}
    assert kpis["延误或未排工单"] == 1 and kpis["未排任务"] == 1 and kpis["总延误（分钟）"] == 0
    assert kpis["高风险设备"] == 2 and kpis["维护覆盖率"] == 0.5 and kpis["最晚完工"] == "14:45"
    subjects = {row["id"]: row for row in decision["subjects"]}
    assert subjects["WO-100"]["metric_value"] == 240 and subjects["WO-200"]["metric_value"] == 90
    assert subjects["WO-300"]["status"] == "未排产"
    assert all(row["at_risk"] for row in subjects.values())
    assert subjects["WO-100"]["path"][0].startswith("EQ005")
    assert any(item["basis"].startswith("观测·NASA C-MAPSS") for item in subjects["WO-100"]["evidence"])


def test_recommended_plan_inspects_first_then_places_the_expedite_order(fixture):
    decision = _run(fixture)
    best = decision["candidates"][0]
    assert best["id"] == "combined" and best["verdict"] == "推荐"
    assert best["metric_after"] == 0 and best["delivery_risk_after"] == 0 and best["coverage_after"] == 1.0
    assert sorted(best["protected"]) == ["WO-100", "WO-200", "WO-300"] and best["harmed"] == []
    maintenance, move = best["actions"]
    assert maintenance == {"action_type": "安排维护任务", "parameters": {
        "schedule_id": "maint-EQ005", "name": "Predictive inspection · EQ005", "order_id": "M-EQ005",
        "start": "2026-10-26T07:00:00Z", "end": "2026-10-26T07:45:00Z", "resource": "cell-cnc"}}
    assert move == {"action_type": "修改排程项", "parameters": {
        "task": {"object_type": "ScheduleItem", "object_id": "test0001:ScheduleItem:wo-300-machine"},
        "start": "2026-10-26T14:00:00Z", "end": "2026-10-26T15:00:00Z", "resource": "cell-cnc"}}


def test_single_measures_show_the_trade_off(fixture):
    candidates = {row["id"]: row for row in _run(fixture)["candidates"]}
    assert candidates["maintain"]["metric_after"] == 0 and candidates["maintain"]["delivery_risk_after"] == 1
    # Placing the expedite order alone keeps it on time but runs it on the uninspected asset.
    assert candidates["place"]["metric_after"] == 390 and candidates["place"]["delivery_risk_after"] == 0


def test_naive_placement_is_blocked_by_the_same_rules(fixture):
    decision = _run(fixture)
    naive = next(row for row in decision["candidates"] if row["id"] == "naive")
    assert naive["applicable"] is False and naive["verdict"].startswith("不可行")
    assert {row["code"] for row in naive["hard_constraints"]} == {"capacity_collision"}
    assert decision["candidates"][-1]["id"] == "naive"


def test_actions_only_target_existing_objects(fixture):
    known = {row["object_id"] for row in _records("ScheduleItem", fixture["schedule_items"], "schedule_id")}
    for candidate in _run(fixture)["candidates"]:
        for action in candidate["actions"]:
            task = action["parameters"].get("task")
            if task:
                assert task["object_id"] in known


def test_missing_evidence_is_reported_not_guessed(fixture):
    for row in fixture["schedule_items"]:
        if row.get("order_id") == "WO-100":
            row.pop("due", None)
    asset = next(row for row in fixture["assets"] if row["id"] == "EQ003")
    asset.pop("risk_level")
    asset.pop("health_score")
    decision = _run(fixture)
    subjects = {row["id"]: row for row in decision["subjects"]}
    assert subjects["WO-100"]["status"] == "缺少交期"
    assert any("WO-100" in gap for gap in decision["evidence_gaps"])
    assert any("EQ003" in gap for gap in decision["evidence_gaps"])


def test_existing_unscheduled_maintenance_is_moved_not_duplicated(fixture):
    fixture["schedule_items"].append({
        "id": "maint-EQ005", "name": "Predictive inspection · EQ005", "resource": "", "start": None, "end": None,
        "layer": "maintenance", "predecessors": [], "compatible_resources": ["cell-cnc"], "material_ready": True,
        "order_id": "M-EQ005", "status": "Planned", "priority": 1})
    best = _run(fixture)["candidates"][0]
    assert [row["action_type"] for row in best["actions"]] == ["修改排程项", "修改排程项"]
    assert best["actions"][0]["parameters"]["task"]["object_id"] == "test0001:ScheduleItem:maint-EQ005"


def test_json_encoded_lists_are_accepted(fixture):
    for row in fixture["schedule_items"]:
        for key in ("resource", "predecessors", "compatible_resources"):
            if isinstance(row.get(key), list):
                row[key] = json.dumps(row[key])
    assert _run(fixture)["metric"]["baseline"] == 330


def test_result_is_deterministic(fixture):
    assert _run(copy.deepcopy(fixture)) == _run(copy.deepcopy(fixture))
