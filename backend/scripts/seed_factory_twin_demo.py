"""Install the Factory Digital Twin showcase from data and public contracts.

The platform runtime is not extended by this script. It creates ordinary
Ontology types/objects, three user Functions (schedule search, schedule
validation and a ``decision/v1`` plan comparison), three Scenario Action types,
one tracking Scenario, a published decision Study with a completed baseline
run, and a Workshop definition through the same reusable contracts used by any
other domain.  It also seeds the sensor loop: a Time Series Property on every
asset, the live Action "标记高风险", a rule that watches the T50 temperature of
assets not yet marked high risk, and a playback of the recorded NASA C-MAPSS
trajectories of the same five engines.

Under JWT auth it also prepares a reviewer account, because a Decision Package
author cannot approve their own package.  Its credentials are written only to
``backend/.factory-twin-demo.users.json`` (mode 600, ignored by Git); the
public manifest ``backend/.factory-twin-demo.json`` names the account only.

Run inside the backend container:
  python scripts/seed_factory_twin_demo.py --api http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.v2.logic import OntologyStateMachine  # noqa: E402,F401  (the Action type's foreign key needs its table registered)
from app.database import SessionLocal  # noqa: E402
from app.models.entity import Entity  # noqa: E402
from app.models.entity_instance import EntityInstance  # noqa: E402
from app.models.ontology import OntologyProject  # noqa: E402
from app.models.v2.action import OntologyActionType  # noqa: E402
from app.models.v2.workshop_app import WorkshopApp  # noqa: E402
from app.services.v2.graph.falkordb_service import FalkorDBService  # noqa: E402
from app.services.v2.sensor_playback import series_id_for  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "factory_twin"
NAME = "工厂数字孪生控制塔"
BACKEND_DIR = Path(__file__).resolve().parents[1]
MANIFEST = BACKEND_DIR / ".factory-twin-demo.json"
USERS_FILE = BACKEND_DIR / ".factory-twin-demo.users.json"
DECISION_KEY = "factory_schedule_decision"


def unwrap(response: httpx.Response, expected: int = 200):
    if response.status_code != expected:
        raise RuntimeError(f"{response.request.method} {response.request.url}: {response.status_code} {response.text[:1200]}")
    body = response.json()
    return body.get("data", body) if isinstance(body, dict) else body


def login(client: httpx.Client, username: str, password: str) -> dict:
    body = unwrap(client.post("/api/v1/auth/login", json={"username": username, "password": password}))
    token = body.get("access_token") or (body.get("data") or {}).get("access_token")
    if not token:
        raise RuntimeError("Login response did not contain an access token")
    return {"Authorization": f"Bearer {token}"}


def properties(definitions: list[tuple[str, str, str]]) -> dict:
    return {"property_definitions": [{"id": key, "type": kind, "name": label} for key, kind, label in definitions]}


TYPE_DEFINITIONS = {
    "FactoryResource": properties([
        ("resource_id", "string", "资源 ID"), ("name", "string", "资源名称"), ("capability", "string", "能力")]),
    "FactoryAsset": properties([
        ("asset_id", "string", "资产 ID"), ("name", "string", "资产名称"), ("resource_id", "string", "所属资源"),
        ("risk_level", "string", "风险等级"), ("health_score", "double", "健康评分"), ("latest_cycle", "integer", "最新周期"),
        ("sensor_4", "double", "传感器 4"), ("sensor_11", "double", "传感器 11"),
        ("maintenance_status", "string", "维护状态"), ("source_unit", "integer", "源设备编号"),
        ("t50_series", "string", "T50 时间序列")]),
    "ScheduleItem": properties([
        ("schedule_id", "string", "任务 ID"), ("name", "string", "任务名称"), ("resource", "json", "资源链接"),
        ("start", "timestamp", "开始时间"), ("end", "timestamp", "结束时间"), ("layer", "string", "排程层"),
        ("predecessors", "json", "前置任务"), ("compatible_resources", "json", "兼容资源"),
        ("material_ready", "boolean", "物料就绪"), ("quality_hold", "boolean", "质量冻结"),
        ("order_id", "string", "工单"), ("status", "string", "状态"), ("priority", "integer", "优先级"),
        ("object_type", "string", "对象类型"), ("due", "timestamp", "交期"), ("duration_minutes", "integer", "计划时长（分钟）")]),
}


def fields(api_name: str) -> list[str]:
    return [item["id"] for item in TYPE_DEFINITIONS[api_name]["property_definitions"]]


def install_objects(ontology_id: str, fixture: dict) -> int:
    graph = FalkorDBService()
    if not graph.available:
        raise RuntimeError("FalkorDB is unavailable")
    rows_by_type = {
        "FactoryResource": [{**row, "resource_id": row["id"], "name": row["label"]} for row in fixture["resources"]],
        "FactoryAsset": [{**row, "asset_id": row["id"], "t50_series": series_id_for(ontology_id, row["id"], "sensor_4")} for row in fixture["assets"]],
        "ScheduleItem": [{**row, "schedule_id": row["id"], "object_type": "ScheduleItem"} for row in fixture["schedule_items"]],
    }
    nodes = []
    with SessionLocal() as db:
        entities = {}
        for api_name, prop_defs in TYPE_DEFINITIONS.items():
            entity = Entity(ontology_id=ontology_id, name_cn={"FactoryResource": "工厂资源", "FactoryAsset": "设备资产", "ScheduleItem": "排程项"}[api_name],
                            name_en=api_name, canonical_id=f"factory-twin:{api_name}", type="EntityType", properties=prop_defs)
            db.add(entity); db.flush(); entities[api_name] = entity
        for api_name, rows in rows_by_type.items():
            for row in rows:
                source_id = row.pop("id")
                object_id = f"{ontology_id[:8]}:{api_name}:{source_id}"
                clean = {key: value for key, value in row.items() if key in {item["id"] for item in TYPE_DEFINITIONS[api_name]["property_definitions"]}}
                db.add(EntityInstance(id=object_id, entity_id=entities[api_name].id, ontology_id=ontology_id,
                                      row_identity=source_id, row_data=clean))
                nodes.append({"id": object_id, "entity_type": api_name, "properties": clean})
        db.commit()
    graph.upsert_instances(ontology_id, nodes)
    return len(nodes)


def user_function(client, root: str, headers: dict, *, key: str, name: str, source_file: str, contract: str):
    output_schema = {"type": "object", "required": ["schema"], "properties": {
        "schema": {"const": contract}, "row_group": {"type": ["object", "null"]},
        "source_puck_ids": {"type": "array"}, "violations": {"type": "array"}, "error": {"type": "string"}}}
    return unwrap(client.post(f"{root}/logic-assets/user-functions", headers=headers, json={
        "asset_key": key, "name": name, "description": "Generic contract-driven scheduling capability",
        "version": "1.0.0", "source": (EXAMPLE / source_file).read_text(encoding="utf-8"),
        "input_schema": {"type": "object", "required": ["schema", "schedule_objects", "domain_start", "domain_end"],
                         "properties": {"schema": {"type": "string"}, "schedule_objects": {"type": "array"},
                                        "domain_start": {"type": "string"}, "domain_end": {"type": "string"},
                                        "trigger": {"type": "object"}}},
        "output_schema": output_schema, "limits": {"timeout_seconds": 10, "memory_mb": 256}}), 201)


def action_type(client, root: str, headers: dict, body: dict) -> dict:
    created = unwrap(client.post(f"{root}/scenario-action-types", headers=headers, json=body), 201)
    return unwrap(client.post(f"{root}/scenario-action-types/{created['id']}:publish", headers=headers))


def accepted(response: httpx.Response):
    """Like ``unwrap`` for endpoints that answer 200, 201 or 202."""
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"{response.request.method} {response.request.url}: {response.status_code} {response.text[:1200]}")
    body = response.json()
    return body.get("data", body) if isinstance(body, dict) and "data" in body else body


def decision_function(client, root: str, headers: dict) -> dict:
    return unwrap(client.post(f"{root}/logic-assets/user-functions", headers=headers, json={
        "asset_key": DECISION_KEY, "name": "工厂排程与检修决策", "version": "1.0.0",
        "description": "decision/v1: risk exposure, delivery risk and maintenance coverage of candidate schedule plans",
        "source": (EXAMPLE / "decision_function.py").read_text(encoding="utf-8"),
        "input_schema": {"type": "object", "required": ["schedule_items", "assets"], "properties": {
            "schedule_items": {"type": "array"}, "assets": {"type": "array"}, "resources": {"type": "array"}}},
        "output_schema": {"type": "object", "required": ["decision"], "properties": {"decision": {"type": "object"}}},
        "limits": {"timeout_seconds": 20, "memory_mb": 256}}), 201)


def decision_study(client, root: str, headers: dict, ontology_id: str, wait_seconds: int = 180) -> dict:
    """Publish the plan-comparison Study, pin a data view and run its baseline case."""
    reads = [("read_schedule", "ScheduleItem"), ("read_assets", "FactoryAsset"), ("read_resources", "FactoryResource")]
    spec = {"spec_version": "scenario-study/v1", "context_policy": {"scenario_mode": "pinned_on_run", "time_required": False},
            "parameters": [],
            "nodes": [{"key": key, "kind": "object_query", "outputs": {"objects": "object_set"},
                       "config": {"type": api_name, "select": fields(api_name)}} for key, api_name in reads] + [
                {"key": "decide", "kind": "function", "implementation": {"key": DECISION_KEY, "version": "1.0.0"},
                 "inputs": {"schedule_items": {"from": "read_schedule.objects"}, "assets": {"from": "read_assets.objects"},
                            "resources": {"from": "read_resources.objects"}},
                 "outputs": {"decision": "decision"}}],
            "edges": [{"source": "read_schedule.objects", "target": "decide.schedule_items"},
                      {"source": "read_assets.objects", "target": "decide.assets"},
                      {"source": "read_resources.objects", "target": "decide.resources"}],
            "metrics": [], "constraints": [],
            "budgets": {"max_nodes": 10, "max_objects": 10000, "max_wall_seconds": 300, "max_artifact_bytes": 10485760}}
    study = accepted(client.post(f"{root}/scenario-study-definitions", headers=headers,
                                 json={"name": "工厂排程与检修决策", "draft_spec": spec}))
    version = accepted(client.post(f"{root}/scenario-study-definitions/{study['id']}:publish", headers=headers,
                                   json={"expected_etag": study["etag"]}))
    view = accepted(client.post(f"{root}/object-query/data-views", headers=headers,
                                json={"source_ontology_id": ontology_id, "source_manifest_digest": "factory-twin-decision"}))
    scenario = accepted(client.post(f"{root}/scenarios", headers=headers,
                                    json={"name": "工厂决策基线", "base_view_id": view["view_id"]}))
    cases = f"{root}/scenario-study-definitions/{study['id']}/cases"
    case = accepted(client.post(cases, headers=headers, json={"study_version": version["version"], "scenario_id": scenario["id"],
                                                               "name": "基线", "case_kind": "baseline"}))
    accepted(client.post(f"{cases}/{case['id']}:compile", headers=headers))
    run = accepted(client.post(f"{cases}/{case['id']}/runs", headers=headers,
                               json={"expected_etag": case["etag"], "client_request_id": f"factory-seed-{ontology_id[:8]}"}))
    state = run
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        state = accepted(client.get(f"{root}/scenario-study-runs/{run['id']}", headers=headers))
        if state["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(2)
    if state["status"] != "completed":
        raise RuntimeError(f"Decision study run {run['id']} ended as {state['status']}: {state.get('error')}")
    return {"study_id": study["id"], "run_id": run["id"], "scenario_id": scenario["id"]}


def reviewer_account() -> dict | None:
    """Return a second JWT identity for package review, reusing the one already prepared."""
    from app.config import settings
    from app.models.user import User
    from app.services.auth_service import hash_password
    if settings.auth_mode != "jwt":
        return None
    with SessionLocal() as db:
        if USERS_FILE.exists():
            saved = json.loads(USERS_FILE.read_text(encoding="utf-8")).get("reviewer") or {}
            if saved.get("username") and db.query(User).filter_by(username=saved["username"]).first():
                return saved
        username = f"factory-reviewer-{secrets.token_hex(3)}"
        password = secrets.token_urlsafe(18)
        db.add(User(username=username, email=f"{username}@demo.ontoact.invalid",
                    password_hash=hash_password(password), role="admin"))
        db.commit()
    reviewer = {"username": username, "password": password}
    USERS_FILE.touch(mode=0o600, exist_ok=True)
    USERS_FILE.chmod(0o600)
    USERS_FILE.write_text(json.dumps({"reviewer": reviewer}, ensure_ascii=False, indent=2), encoding="utf-8")
    return reviewer


# T50 threshold: mean + 3 standard deviations of the first 30 cycles of the five engines (1398.9 + 3 x 4.26), rounded.
T50_THRESHOLD = 1412.0


def mark_high_risk_action(ontology_id: str) -> str:
    """The live Action a rule alert suggests: one property, on one asset, through review like any live change."""
    with SessionLocal() as db:
        action = OntologyActionType(
            ontology_id=ontology_id, name="标记高风险", description="把设备的风险等级标为 High，使检修排程把它当作高风险设备", action_category="update",
            status="published", enabled=True, target_entity_type="FactoryAsset",
            parameters=[{"name": "asset", "type": "object", "object_type": "FactoryAsset"}],
            effects=[{"op": "set_property", "target_parameter": "asset", "property": "risk_level", "static_value": "High"}])
        db.add(action)
        db.commit()
        return action.id


def sensor_rule(client, root: str, headers: dict, action_id: str) -> dict:
    """Assets not yet marked high risk whose 10-cycle mean T50 stays above the threshold for three windows in a row."""
    logic = {"input": {"kind": "base", "type_ref": {"api_name": "FactoryAsset"}},
             "blocks": [{"kind": "filter", "where": {"kind": "comparison", "property": {"api_name": "risk_level"}, "op": "ne", "value": "High"}},
                        {"kind": "series_window", "series_property": "t50_series", "window_points": 10, "aggregate": "avg", "op": "gte",
                         "threshold": T50_THRESHOLD, "min_consecutive_windows": 3}],
             "output": {"severity": "high", "properties": {"component": "低压涡轮出口温度 T50"}, "evidence_properties": ["name", "risk_level", "health_score"]},
             "action": {"action_id": action_id, "object_parameter": "asset"}}
    created = unwrap(client.post(f"{root}/rules", headers=headers, json={
        "api_name": "t50_sustained_high", "display_name": "T50 温度持续偏高",
        "description": f"低压涡轮出口温度 10 个周期均值连续 3 个窗口不低于 {T50_THRESHOLD:g} °R（5 台机组前 30 个周期均值 + 3σ），且设备尚未标为高风险", "logic": logic}), 201)
    unwrap(client.post(f"{root}/rules/{created['rule']['id']}/proposals/{created['proposal']['id']}:review", headers=headers, json={"decision": "approve"}))
    return created["rule"]


def sensor_playback(client, root: str, headers: dict, ontology_id: str, rule_id: str) -> dict:
    mapping = [{"asset_id": asset_id, "object_id": f"{ontology_id[:8]}:FactoryAsset:{asset_id}", "unit": unit}
               for unit, asset_id in enumerate(["EQ001", "EQ002", "EQ003", "EQ004", "EQ005"], start=1)]
    return unwrap(client.post(f"{root}/sensor-playbacks", headers=headers, json={
        "name": "NASA C-MAPSS FD001 传感回放", "dataset_id": "cmapss_fd001_t50", "rule_id": rule_id, "mapping": mapping,
        "start_cycle": 100, "step_cycles": 5}), 201)


def sensor_page(playback_id: str, decision_page_id: str | None) -> dict:
    return {"id": "sensors", "title": "传感预警与处置", "template": "free", "widgets": [
        {"id": "playback", "type": "sensor_playback", "title": "", "source": None, "path": "", "props": {"playback_id": playback_id}},
        {"id": "alerts", "type": "alert_action", "title": "", "source": None, "path": "",
         "props": {"playback_id": playback_id, **({"navigate": decision_page_id} if decision_page_id else {})}}],
        "layout": [{"i": "playback", "x": 0, "y": 0, "w": 24, "h": 15}, {"i": "alerts", "x": 0, "y": 15, "w": 24, "h": 14}]}


def decision_page() -> dict:
    """Plan comparison and governed approval, built only from generic study_run widgets."""
    return {"id": "decision", "title": "方案对比与决策", "template": "free", "widgets": [
        {"id": "decision_kpis", "type": "kpi_strip", "title": "", "source": "decision", "path": "", "props": {"items": [
            {"label": "高风险设备暴露（分钟）", "path": "decision.metric.baseline", "format": "number", "note": "推断：生产在未检修的高风险设备上运行的时长"},
            {"label": "延误或未排工单", "path": "decision.kpis.0.value", "format": "number", "note": "观测：当前计划与交期"},
            {"label": "维护覆盖率", "path": "decision.kpis.3.value", "format": "percent", "note": "推断：高风险设备在首个生产任务前完成检修的比例"},
            {"label": "最晚完工", "path": "decision.kpis.5.value", "format": "text", "note": "观测：当前计划"}]}},
        {"id": "orders", "type": "table", "title": "工单风险与依据", "source": "decision", "path": "decision.subjects", "props": {"columns": [
            {"label": "工单", "field": "label", "format": "text"}, {"label": "状态", "field": "status", "format": "text", "badge": True},
            {"label": "高风险暴露（分钟）", "field": "metric_value", "format": "number", "tone_rule": "gt", "tone_value": 0},
            {"label": "依据（观测 / 推断）", "field": "reasons", "format": "text"}]}},
        {"id": "plans", "type": "action_list", "title": "候选方案（模拟结果）", "source": "decision", "path": "decision.candidates", "props": {
            "label_field": "label", "variable": "plan",
            "subtitle": "{verdict} · 暴露 {metric_after|number} 分钟 · 交付风险 {delivery_risk_after|number} · 新增占用 {cost|number} 分钟"}},
        {"id": "plan_detail", "type": "action_detail", "title": "方案详情与审批", "source": "decision", "path": "decision.candidates", "props": {
            "variable": "plan", "explain": False, "metrics": [
                {"label": "高风险暴露（分钟）", "before_path": "decision.metric.baseline", "after_field": "metric_after", "format": "number"},
                {"label": "延误或未排工单", "before_path": "decision.kpis.0.value", "after_field": "delivery_risk_after", "format": "number"},
                {"label": "维护覆盖率", "before_path": "decision.kpis.3.value", "after_field": "coverage_after", "format": "percent"},
                {"label": "新增占用产能（分钟）", "after_field": "cost", "format": "number"}]}},
        {"id": "replan", "type": "rerun_button", "title": "", "source": "decision", "path": "", "props": {"label": "用最新本体数据重跑", "steps": [
            {"label": "高风险暴露（分钟）", "path": "decision.metric.baseline", "format": "number"},
            {"label": "延误或未排工单", "path": "decision.kpis.0.value", "format": "number"}]}}],
        "layout": [{"i": "decision_kpis", "x": 0, "y": 0, "w": 24, "h": 3},
                   {"i": "orders", "x": 0, "y": 3, "w": 24, "h": 7},
                   {"i": "plans", "x": 0, "y": 10, "w": 9, "h": 14}, {"i": "plan_detail", "x": 9, "y": 10, "w": 15, "h": 19},
                   {"i": "replan", "x": 0, "y": 24, "w": 9, "h": 2}]}


def app_definition(fixture: dict, scenario_id: str, modify_action: str, create_action: str,
                   search_asset: str, validation_asset: str, study_id: str | None = None, playback_id: str | None = None) -> dict:
    resources = [{"id": row["id"], "label": row["label"]} for row in fixture["resources"]]
    sources = [
        {"id": "schedule", "kind": "object_set", "label": "排程对象", "object_type": "ScheduleItem", "limit": 2000},
        {"id": "assets", "kind": "object_set", "label": "设备资产", "object_type": "FactoryAsset", "limit": 1000},
        {"id": "schedule_status", "kind": "aggregate", "label": "任务状态", "object_type": "ScheduleItem", "group_by": ["status"],
         "metrics": [{"key": "tasks", "op": "count"}]},
        {"id": "asset_risk", "kind": "aggregate", "label": "资产风险", "object_type": "FactoryAsset", "group_by": ["risk_level"],
         "metrics": [{"key": "assets", "op": "count"}, {"key": "avg_health", "op": "avg", "property": "health_score"}]},
    ]
    gantt_props = {
        "id_field": "schedule_id", "label_field": "name", "resource_field": "resource", "start_field": "start", "end_field": "end",
        "dependency_field": "predecessors", "layer_field": "layer", "object_type_field": "object_type", "object_id_field": "id",
        "resources": resources, "resource_cardinality": "many", "resource_drop_behavior": "replace", "snap_minutes": 15,
        "allocation_behavior": "allocate", "enforce_rules": True, "operating_start_minute": 360, "operating_end_minute": 1080,
        "layers": [{"id": "constraint", "label": "可用产能", "style": "background", "color": "#94a3b8"},
                   {"id": "production", "label": "生产工单", "style": "standard", "color": "#2563eb"},
                   {"id": "maintenance", "label": "预测性维护", "style": "standard", "color": "#7c3aed"}],
        "scenario_variable": "scenario", "action_type_id": modify_action, "target_parameter": "task",
        "start_parameter": "start", "end_parameter": "end", "resource_parameter": "resource",
        "create_action_type_id": create_action, "create_id_parameter": "schedule_id", "create_label_parameter": "name",
        "create_start_parameter": "start", "create_end_parameter": "end", "create_resource_parameter": "resource",
        "create_default_label": "Operator-created task", "create_layer": "production",
        "recommendation_asset_id": search_asset, "validation_asset_id": validation_asset, "enforce_business_rules": True,
    }
    table_columns = [{"label": "任务", "field": "name", "format": "text"}, {"label": "工单", "field": "order_id", "format": "text"},
                     {"label": "状态", "field": "status", "format": "text", "badge": True}, {"label": "开始", "field": "start", "format": "date"},
                     {"label": "资源", "field": "resource", "format": "text"}]
    if study_id:
        sources.append({"id": "decision", "kind": "study_run", "label": "排程与检修决策", "study_id": study_id, "node": "decide"})
    definition = {"schema": "workshop-app/v1", "data_sources": sources,
            "variables": [{"id": "scenario", "label": "Scenario", "default": scenario_id},
                          {"id": "risk", "label": "风险等级"}, {"id": "plan", "label": "候选方案"}],
            "settings": {"time_range": "all", "refresh_minutes": 0}, "pages": [
        {"id": "command", "title": "工厂控制塔", "template": "free", "widgets": [
            {"id": "kpis", "type": "stat_row", "title": "运营态势", "source": "schedule", "path": "objects", "props": {"items": [
                {"label": "排程对象", "agg": "count", "format": "number"},
                {"label": "已发布工单", "agg": "count", "where_field": "status", "where_equals": "Released", "format": "number"},
                {"label": "未排任务", "agg": "count", "where_field": "status", "where_equals": "Unscheduled", "format": "number"}]}},
            {"id": "status", "type": "donut_chart", "title": "任务状态", "source": "schedule_status", "path": "groups",
             "props": {"category_field": "status", "value_field": "tasks", "center_label": "任务"}},
            {"id": "risk", "type": "donut_chart", "title": "设备健康风险", "source": "asset_risk", "path": "groups",
             "props": {"category_field": "risk_level", "value_field": "assets", "center_label": "设备", "variable": "risk"}},
            {"id": "health", "type": "rank_bars", "title": "设备健康评分", "source": "assets", "path": "objects",
             "props": {"label_field": "name", "value_field": "health_score", "max_rows": 8, "format": "number", "color": "#0f766e", "filter_field": "risk_level", "filter_variable": "risk"}},
            {"id": "work", "type": "table", "title": "当前工单与维护任务", "source": "schedule", "path": "objects", "props": {"columns": table_columns, "sort_field": "priority", "max_rows": 20}}],
         "layout": [{"i": "kpis", "x": 0, "y": 0, "w": 24, "h": 3}, {"i": "status", "x": 0, "y": 3, "w": 7, "h": 7},
                    {"i": "risk", "x": 7, "y": 3, "w": 7, "h": 7}, {"i": "health", "x": 14, "y": 3, "w": 10, "h": 7},
                    {"i": "work", "x": 0, "y": 10, "w": 24, "h": 11}]},
        {"id": "schedule", "title": "生产与维护协同排程", "template": "free", "widgets": [
            {"id": "gantt", "type": "schedule_gantt", "title": "Factory Twin · Dynamic Schedule", "source": "schedule", "path": "objects", "props": gantt_props},
            {"id": "task_table", "type": "table", "title": "排程对象明细", "source": "schedule", "path": "objects", "props": {"columns": table_columns, "max_rows": 30}}],
         "layout": [{"i": "gantt", "x": 0, "y": 0, "w": 24, "h": 18}, {"i": "task_table", "x": 0, "y": 18, "w": 24, "h": 10}]},
        {"id": "assets", "title": "设备数字孪生", "template": "free", "widgets": [
            {"id": "asset_table", "type": "table", "title": "NASA C-MAPSS 设备健康快照", "source": "assets", "path": "objects", "props": {"columns": [
                {"label": "设备", "field": "name", "format": "text"}, {"label": "资源", "field": "resource_id", "format": "text"},
                {"label": "风险", "field": "risk_level", "format": "text", "badge": True}, {"label": "健康评分", "field": "health_score", "format": "number", "tone_rule": "lt", "tone_value": 65},
                {"label": "维护状态", "field": "maintenance_status", "format": "text"}, {"label": "Sensor 4", "field": "sensor_4", "format": "number"},
                {"label": "Sensor 11", "field": "sensor_11", "format": "number"}], "sort_field": "health_score", "max_rows": 20}},
            {"id": "asset_health", "type": "rank_bars", "title": "健康评分排序", "source": "assets", "path": "objects",
             "props": {"label_field": "name", "value_field": "health_score", "max_rows": 10, "format": "number", "color": "#7c3aed"}}],
         "layout": [{"i": "asset_table", "x": 0, "y": 0, "w": 16, "h": 13}, {"i": "asset_health", "x": 16, "y": 0, "w": 8, "h": 9}]}
    ]}
    if study_id:
        definition["pages"].insert(2, decision_page())
    if playback_id:
        definition["pages"].insert(2, sensor_page(playback_id, "decision" if study_id else None))
    return definition


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=os.environ.get("FACTORY_TWIN_PASSWORD", ""))
    parser.add_argument("--force-new", action="store_true")
    args = parser.parse_args()
    if not args.password:
        raise SystemExit("Set FACTORY_TWIN_PASSWORD or pass --password")
    fixture = json.loads((EXAMPLE / "data.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url=args.api, timeout=120) as client:
        headers = login(client, args.username, args.password)
        with SessionLocal() as db:
            existing = db.query(OntologyProject).filter_by(name=NAME).first()
            if existing and not args.force_new:
                app = db.query(WorkshopApp).filter_by(ontology_id=existing.id).order_by(WorkshopApp.updated_at.desc()).first()
                if app:
                    result = {"ontology_id": existing.id, "app_id": app.id,
                              "url": f"/apps/workshop/{app.id}?ontology={existing.id}", "reused": True}
                    print(json.dumps(result, ensure_ascii=False)); return
        name = NAME if not args.force_new else f"{NAME} · {datetime.now():%m%d-%H%M%S}"
        ontology = unwrap(client.post("/api/v1/ontologies", headers=headers, json={
            "name": name, "domain": "制造", "description": "Config-driven production scheduling and asset-health digital twin showcase"}), 201)
        ontology_id = ontology["id"]
        object_count = install_objects(ontology_id, fixture)
        root = f"/api/v2/ontologies/{ontology_id}"
        search = user_function(client, root, headers, key="generic_schedule_gap_search", name="通用排程空档推荐",
                               source_file="schedule_search.py", contract="schedule-search/v1")
        validation = user_function(client, root, headers, key="generic_schedule_validation", name="通用排程约束校验",
                                   source_file="schedule_validation.py", contract="schedule-validation/v1")
        modify = action_type(client, root, headers, {"name": "修改排程项", "description": "Update the bound time and resource fields",
            "parameters": [{"name": "task", "type": "object", "object_type": "ScheduleItem"}, {"name": "start", "type": "string"},
                           {"name": "end", "type": "string"}, {"name": "resource", "type": "string"}],
            "rules": [{"op": "set_property", "target_parameter": "task", "property": "start", "value_parameter": "start"},
                      {"op": "set_property", "target_parameter": "task", "property": "end", "value_parameter": "end"},
                      {"op": "set_property", "target_parameter": "task", "property": "resource", "value_parameter": "resource"}]})
        create = action_type(client, root, headers, {"name": "创建排程项", "description": "Create a scenario-local schedule object",
            "parameters": [{"name": "schedule_id", "type": "string"}, {"name": "name", "type": "string"},
                           {"name": "start", "type": "string"}, {"name": "end", "type": "string"}, {"name": "resource", "type": "string"}],
            "rules": [{"op": "create_object", "object_type": "ScheduleItem", "properties": {
                "schedule_id": {"parameter": "schedule_id"}, "name": {"parameter": "name"}, "start": {"parameter": "start"},
                "end": {"parameter": "end"}, "resource": {"parameter": "resource"}, "layer": {"static": "production"},
                "material_ready": {"static": True}, "status": {"static": "Proposed"}, "priority": {"static": 3}}}]})
        # Decision candidates compile through published Actions by name; this one plans a missing inspection.
        maintenance = action_type(client, root, headers, {"name": "安排维护任务",
            "description": "Create a planned predictive-maintenance task on a resource",
            "parameters": [{"name": "schedule_id", "type": "string"}, {"name": "name", "type": "string"}, {"name": "order_id", "type": "string"},
                           {"name": "start", "type": "string"}, {"name": "end", "type": "string"}, {"name": "resource", "type": "string"}],
            "rules": [{"op": "create_object", "object_type": "ScheduleItem", "properties": {
                "schedule_id": {"parameter": "schedule_id"}, "name": {"parameter": "name"}, "order_id": {"parameter": "order_id"},
                "start": {"parameter": "start"}, "end": {"parameter": "end"}, "resource": {"parameter": "resource"},
                "layer": {"static": "maintenance"}, "status": {"static": "Planned"}, "material_ready": {"static": True},
                "priority": {"static": 1}, "object_type": {"static": "ScheduleItem"}}}]})
        decision = decision_function(client, root, headers)
        scenario = unwrap(client.post(f"{root}/scenarios:from-live", headers=headers, json={
            "name": "Factory Twin · Shift Recovery", "description": "Isolated operator proposals before controlled merge", "mode": "tracking"}), 201)
        study = decision_study(client, root, headers, ontology_id)
        mark_high_risk = mark_high_risk_action(ontology_id)
        rule = sensor_rule(client, root, headers, mark_high_risk)
        playback = sensor_playback(client, root, headers, ontology_id, rule["id"])
        definition = app_definition(fixture, scenario["id"], modify["id"], create["id"], search["id"], validation["id"],
                                    study_id=study["study_id"], playback_id=playback["id"])
        app = unwrap(client.post(f"{root}/workshop-apps", headers=headers, json={"name": "Factory Twin Operations",
            "description": "Asset health, production/maintenance schedule, plan comparison, constraints, recommendations and governed decisions",
            "definition": definition}), 201)
        reviewer = reviewer_account()
        result = {"ontology_id": ontology_id, "app_id": app["id"], "scenario_id": scenario["id"],
                  "search_asset_id": search["id"], "validation_asset_id": validation["id"], "decision_asset_id": decision["id"],
                  "maintenance_action_id": maintenance["id"], "mark_high_risk_action_id": mark_high_risk, "rule_id": rule["id"],
                  "playback_id": playback["id"], "decision_study_id": study["study_id"],
                  "decision_run_id": study["run_id"], "decision_scenario_id": study["scenario_id"], "objects": object_count,
                  "reviewer": reviewer["username"] if reviewer else None,
                  "url": f"/apps/workshop/{app['id']}?ontology={ontology_id}", "reused": False}
        MANIFEST.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
