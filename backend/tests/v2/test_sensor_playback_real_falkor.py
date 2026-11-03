"""Sensor playback on the real FalkorDB read path: each step pins a view, evaluates the rule and opens alerts once."""
import csv
import json
from pathlib import Path

import pytest

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.models.v2.action import OntologyActionType
from app.models.v2.query_view import QueryDataView
from app.services.v2 import sensor_playback
from app.services.v2.graph.falkordb_service import FalkorDBService

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "factory_twin"
THRESHOLD = 1412.0
WINDOW, CONSECUTIVE = 10, 3


def crossing_cycle(values: list[float]) -> int | None:
    """The cycle at which the third consecutive breaching window of the rule ends, computed independently of the service."""
    run = 0
    for end in range(WINDOW - 1, len(values)):
        run = run + 1 if sum(values[end - WINDOW + 1:end + 1]) / WINDOW >= THRESHOLD else 0
        if run >= CONSECUTIVE:
            return end + 1
    return None


@pytest.fixture
def factory(db, admin_user):
    graph = FalkorDBService()
    if not graph.available:
        pytest.fail("Real FalkorDB is required for the sensor playback acceptance test")
    fixture = json.loads((EXAMPLES / "data.json").read_text(encoding="utf-8"))
    ontology = OntologyProject(name="工厂传感回放", domain="manufacturing", created_by=admin_user.id)
    db.add(ontology)
    db.flush()
    entity = Entity(ontology_id=ontology.id, name_cn="设备资产", name_en="FactoryAsset", canonical_id=f"ontology:FactoryAsset", type="EntityType",
                    properties={"property_definitions": [{"id": key, "type": kind} for key, kind in (
                        ("asset_id", "string"), ("name", "string"), ("risk_level", "string"), ("health_score", "double"), ("t50_series", "string"))]})
    db.add(entity)
    db.flush()
    nodes = []
    for row in fixture["assets"]:
        properties = {"asset_id": row["id"], "name": row["name"], "risk_level": row["risk_level"], "health_score": row["health_score"],
                      "t50_series": sensor_playback.series_id_for(ontology.id, row["id"], "sensor_4")}
        object_id = f"{ontology.id[:8]}:FactoryAsset:{row['id']}"
        db.add(EntityInstance(id=object_id, entity_id=entity.id, ontology_id=ontology.id, row_identity=row["id"], row_data=properties))
        nodes.append({"id": object_id, "entity_type": "FactoryAsset", "properties": properties})
    action = OntologyActionType(ontology_id=ontology.id, name="标记高风险", action_category="update", status="published", enabled=True,
                                target_entity_type="FactoryAsset", parameters=[{"name": "asset", "type": "object", "object_type": "FactoryAsset"}],
                                effects=[{"op": "set_property", "target_parameter": "asset", "property": "risk_level", "static_value": "High"}])
    db.add(action)
    db.commit()
    graph.upsert_instances(ontology.id, nodes)
    try:
        yield ontology, action, fixture
    finally:
        for view in db.query(QueryDataView).filter_by(ontology_id=ontology.id).all():
            graph.delete_graph(view.graph_key)
        graph.delete_graph(ontology.id)


def make_rule(client, auth_headers, ontology, action):
    url = f"/api/v2/ontologies/{ontology.id}/rules"
    logic = {"input": {"kind": "base", "type_ref": {"api_name": "FactoryAsset"}},
             "blocks": [{"kind": "filter", "where": {"kind": "comparison", "property": {"api_name": "risk_level"}, "op": "ne", "value": "High"}},
                        {"kind": "series_window", "series_property": "t50_series", "window_points": WINDOW, "aggregate": "avg", "op": "gte",
                         "threshold": THRESHOLD, "min_consecutive_windows": CONSECUTIVE}],
             "output": {"severity": "high", "properties": {"component": "T50"}, "evidence_properties": ["name", "risk_level"]},
             "action": {"action_id": action.id, "object_parameter": "asset"}}
    created = client.post(url, headers=auth_headers, json={"api_name": "t50_high", "display_name": "T50 温度持续偏高", "logic": logic}).json()
    client.post(f"{url}/{created['rule']['id']}/proposals/{created['proposal']['id']}:review", headers=auth_headers, json={"decision": "approve"})
    return created["rule"]


def make_playback(client, auth_headers, ontology, rule, start=100):
    mapping = [{"asset_id": f"EQ00{unit}", "object_id": f"{ontology.id[:8]}:FactoryAsset:EQ00{unit}", "unit": unit} for unit in range(1, 6)]
    response = client.post(f"/api/v2/ontologies/{ontology.id}/sensor-playbacks", headers=auth_headers, json={
        "name": "回放", "dataset_id": "cmapss_fd001_t50", "rule_id": rule["id"], "mapping": mapping, "start_cycle": start, "step_cycles": 5})
    assert response.status_code == 201, response.text
    return response.json()


def advance(client, auth_headers, ontology, playback, **body):
    response = client.post(f"/api/v2/ontologies/{ontology.id}/sensor-playbacks/{playback['id']}:advance", headers=auth_headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ----------------------------------------------------------------------------------------------------- the data
def test_recorded_trajectories_run_from_the_first_cycle_to_failure():
    trajectories = sensor_playback.load_trajectories("cmapss_fd001_t50")
    assert {unit: len(values) for unit, values in trajectories.items()} == {1: 192, 2: 287, 3: 179, 4: 189, 5: 269}
    with (EXAMPLES / "sensor_trajectories.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1116 and all(1380 < float(row["sensor_4"]) < 1450 for row in rows)
    # Every engine runs hotter at the end of its life than at the start.
    assert all(sum(values[-10:]) / 10 > sum(values[:10]) / 10 + 15 for values in trajectories.values())


def test_series_ids_and_cycle_clock():
    first = sensor_playback.series_id_for("onto-1", "EQ001", "sensor_4")
    assert first == sensor_playback.series_id_for("onto-1", "EQ001", "sensor_4")
    assert len({first, sensor_playback.series_id_for("onto-1", "EQ002", "sensor_4"), sensor_playback.series_id_for("onto-2", "EQ001", "sensor_4")}) == 3
    stamp = sensor_playback.cycle_time(sensor_playback.BASE_TIME, 25).isoformat()
    assert sensor_playback.cycle_of(sensor_playback.BASE_TIME, stamp) == 25


# --------------------------------------------------------------------------------------------------- creation rules
def test_playback_needs_linked_series_and_an_approved_rule(client, auth_headers, factory, db):
    ontology, action, _ = factory
    url = f"/api/v2/ontologies/{ontology.id}/sensor-playbacks"
    rule = make_rule(client, auth_headers, ontology, action)
    object_id = f"{ontology.id[:8]}:FactoryAsset:EQ001"
    body = {"name": "x", "dataset_id": "cmapss_fd001_t50", "rule_id": rule["id"], "mapping": [{"asset_id": "EQ001", "object_id": object_id, "unit": 1}]}
    assert client.post(url, headers=auth_headers, json={**body, "dataset_id": "nope"}).json()["detail"]["code"] == "dataset_not_found"
    assert client.post(url, headers=auth_headers, json={**body, "rule_id": "missing"}).json()["detail"]["code"] == "rule_not_found"
    assert client.post(url, headers=auth_headers, json={**body, "mapping": [{"asset_id": "EQ001", "object_id": object_id, "unit": 9}]}).json()["detail"]["code"] == "unit_not_found"
    wrong = client.post(url, headers=auth_headers, json={**body, "mapping": [{"asset_id": "EQ002", "object_id": object_id, "unit": 1}]})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "series_not_linked"
    assert client.post(url, headers=auth_headers, json={**body, "start_cycle": 999}).json()["detail"]["code"] == "invalid_range"
    unapproved = client.post(f"/api/v2/ontologies/{ontology.id}/rules", headers=auth_headers, json={
        "api_name": "draft_rule", "display_name": "未批准", "logic": {"input": {"kind": "base", "type_ref": {"api_name": "FactoryAsset"}}, "blocks": [
            {"kind": "series_window", "series_property": "t50_series", "window_points": 5, "aggregate": "avg", "op": "gte", "threshold": 1.0}],
            "output": {"severity": "low"}}}).json()["rule"]
    assert client.post(url, headers=auth_headers, json={**body, "rule_id": unapproved["id"]}).json()["detail"]["code"] == "rule_not_approved"


# ------------------------------------------------------------------------------------------------ stepping and alerts
def test_each_step_pins_a_view_and_alerts_open_once(client, auth_headers, factory, db):
    ontology, action, _ = factory
    rule = make_rule(client, auth_headers, ontology, action)
    playback = make_playback(client, auth_headers, ontology, rule)
    assert playback["cursor"] == 100 and playback["alerts"] == [] and playback["max_cycle"] == 287
    assert playback["rule"]["threshold"] == THRESHOLD and playback["rule"]["window_points"] == WINDOW

    trajectories = sensor_playback.load_trajectories("cmapss_fd001_t50")
    crossings = {unit: crossing_cycle(values) for unit, values in trajectories.items()}
    # EQ002 and EQ005 are already high risk in the ledger, so the rule must never look at them.
    candidates = {unit: cycle for unit, cycle in crossings.items() if unit in (1, 3, 4)}
    first_unit = min(candidates, key=candidates.get)
    stop = -(-candidates[first_unit] // 5) * 5

    first = advance(client, auth_headers, ontology, playback, steps=20, until_alert=True)
    assert first["cursor"] == stop, "the replay stops at the first step that opens an alert"
    assert [tick["new_alert_ids"] != [] for tick in first["advanced"]][-1] is True
    assert len(first["advanced"]) == (stop - 100) // 5 and len(first["ticks"]) == len(first["advanced"])
    assert [alert["asset_id"] for alert in first["alerts"]] == [f"EQ00{first_unit}"]
    alert = first["alerts"][0]
    assert alert["status"] == "open" and alert["suggested_action"]["action_id"] == action.id
    assert alert["end_cycle"] is not None and alert["end_cycle"] <= stop and alert["breaching_windows"] >= CONSECUTIVE

    # No future data leaks into the chart: every series ends at the cursor.
    assert all(max(series["cycles"]) == stop for series in first["series"])
    series = next(row for row in first["series"] if row["asset_id"] == f"EQ00{first_unit}")
    assert series["average"][:WINDOW - 1] == [None] * (WINDOW - 1) and series["average"][-1] >= THRESHOLD

    views = {tick["view_id"] for tick in first["ticks"]}
    assert len(views) == len(first["ticks"]), "every step pins its own view"
    assert db.query(QueryDataView).filter_by(ontology_id=ontology.id).count() >= len(views)

    # The rest of the three assets cross a little later; an alert that is already open is updated, not duplicated.
    second = advance(client, auth_headers, ontology, playback, steps=3, until_alert=False)
    assert len(second["alerts"]) == len({alert["subject_id"] for alert in second["alerts"]})
    expected_by_now = {f"EQ00{unit}" for unit, cycle in candidates.items() if cycle <= second["cursor"]}
    assert {alert["asset_id"] for alert in second["alerts"]} == expected_by_now
    again = next(row for row in second["alerts"] if row["asset_id"] == f"EQ00{first_unit}")
    assert again["id"] == alert["id"] and again["occurrence_count"] > alert["occurrence_count"]


def test_assets_already_marked_high_risk_never_alert(client, auth_headers, factory):
    ontology, action, _ = factory
    playback = make_playback(client, auth_headers, ontology, make_rule(client, auth_headers, ontology, action), start=200)
    result = advance(client, auth_headers, ontology, playback, steps=20)
    assert result["cursor"] == 287 and result["finished"] is True
    trajectories = sensor_playback.load_trajectories("cmapss_fd001_t50")
    assert crossing_cycle(trajectories[2]) and crossing_cycle(trajectories[5]), "both high-risk engines do overheat in the recording"
    assert {alert["asset_id"] for alert in result["alerts"]} <= {"EQ001", "EQ003", "EQ004"}
    # Nothing left to play: another request is a no-op.
    assert advance(client, auth_headers, ontology, playback, steps=3)["advanced"] == []


def test_playback_endpoints_check_the_ontology_and_the_role(client, auth_headers, factory, db, editor_user):
    ontology, action, _ = factory
    playback = make_playback(client, auth_headers, ontology, make_rule(client, auth_headers, ontology, action))
    assert client.get(f"/api/v2/ontologies/{ontology.id}/sensor-playbacks/{playback['id']}", headers=auth_headers).status_code == 200
    assert client.get(f"/api/v2/ontologies/{ontology.id}/sensor-playbacks/missing", headers=auth_headers).status_code == 404
    other = OntologyProject(name="别的本体", domain="x", created_by=editor_user.id)
    db.add(other)
    db.commit()
    assert client.get(f"/api/v2/ontologies/{other.id}/sensor-playbacks/{playback['id']}", headers=auth_headers).status_code == 404
    listed = client.get(f"/api/v2/ontologies/{ontology.id}/sensor-playbacks", headers=auth_headers).json()["playbacks"]
    assert [row["id"] for row in listed] == [playback["id"]]


# --------------------------------------------------------------------------------------- alert -> reviewed change
def second_admin(client, db, name="reviewer"):
    from app.models.user import User
    from app.services.auth_service import hash_password
    db.add(User(username=name, email=f"{name}@test.com", password_hash=hash_password("review123"), role="admin"))
    db.commit()
    token = client.post("/api/v1/auth/login", json={"username": name, "password": "review123"}).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_an_alert_opens_one_reviewed_case_and_the_review_changes_the_asset(client, auth_headers, factory, db):
    ontology, action, _ = factory
    playback = make_playback(client, auth_headers, ontology, make_rule(client, auth_headers, ontology, action))
    opened = advance(client, auth_headers, ontology, playback, steps=20, until_alert=True)
    alert = opened["alerts"][0]
    api = f"/api/v2/ontologies/{ontology.id}"

    proposed = client.post(f"{api}/alerts/{alert['id']}:propose", headers=auth_headers)
    assert proposed.status_code == 201, proposed.text
    case = proposed.json()["case"]
    assert proposed.json()["created"] is True and case["status"] == "ready_for_review" and case["kind"] == "maintenance_review"
    assert case["target"]["object_id"] == alert["subject_id"] and case["action_type_id"] == action.id
    assert case["context"]["alert_id"] == alert["id"] and case["context"]["peak"] == alert["peak"]
    assert alert["name"] in case["reason"] and "持续越线" in case["reason"]

    again = client.post(f"{api}/alerts/{alert['id']}:propose", headers=auth_headers).json()
    assert again["created"] is False and again["case"]["id"] == case["id"], "asking twice opens one case"
    detail = client.get(f"{api}/sensor-playbacks/{playback['id']}", headers=auth_headers).json()
    shown = next(row for row in detail["alerts"] if row["id"] == alert["id"])
    assert shown["status"] == "acknowledged" and shown["case"]["id"] == case["id"] and shown["case"]["status"] == "ready_for_review"

    # A second person approves; the author cannot approve their own case outside the single-user demo mode.
    own = client.post(f"{api}/decision-cases/{case['id']}:review", headers=auth_headers, json={"decision": "approved"})
    assert own.status_code == 403 and own.json()["detail"]["code"] == "self_review_forbidden"
    reviewer = second_admin(client, db)
    approved = client.post(f"{api}/decision-cases/{case['id']}:review", headers=reviewer, json={"decision": "approved", "comment": "同意：EQ 的 T50 确实持续偏高"})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"

    before = db.query(EntityInstance).filter_by(id=alert["subject_id"]).first()
    assert before.row_data["risk_level"] != "High"
    executed = client.post(f"{api}/decision-cases/{case['id']}:execute", headers=auth_headers, json={})
    assert executed.status_code == 200, executed.text
    assert executed.json()["case"]["status"] == "completed" and executed.json()["run"]["requested_by"] != executed.json()["run"]["reviewed_by"]
    db.expire_all()
    assert db.query(EntityInstance).filter_by(id=alert["subject_id"]).first().row_data["risk_level"] == "High"

    # The next step's pinned view already shows the asset as high risk, so the rule lets go of it.
    after = advance(client, auth_headers, ontology, playback, steps=1)
    updated = next(row for row in after["alerts"] if row["id"] == alert["id"])
    assert updated["recovered"] is True
    assert next(row for row in after["series"] if row["object_id"] == alert["subject_id"])["risk_level"] == "High"
    resolved = client.post(f"{api}/alerts/{alert['id']}:resolve", headers=auth_headers, json={"note": "已处置", "action_run_id": executed.json()["case"]["action_run_id"]})
    assert resolved.status_code == 200, resolved.text
    final = client.get(f"{api}/sensor-playbacks/{playback['id']}", headers=auth_headers).json()
    assert next(row for row in final["alerts"] if row["id"] == alert["id"])["status"] == "resolved"


def test_propose_refuses_alerts_without_a_suggested_action_and_unknown_alerts(client, auth_headers, factory):
    ontology, action, _ = factory
    api = f"/api/v2/ontologies/{ontology.id}"
    assert client.post(f"{api}/alerts/missing:propose", headers=auth_headers).json()["detail"]["code"] == "alert_not_found"
