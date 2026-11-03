"""Sensor playback: recorded trajectories replayed as if they arrived live.

Every step reveals the next cycles of each mapped engine, pins the live ontology in a new immutable data view carrying
the series revealed so far, and evaluates the playback's rule on that view.  Alerts therefore come from the same governed
rule runtime as any other (deduplicated across views, with a suggested Action that still needs review); the playback
only supplies the data and the clock.  Cycle k of an engine is stamped ``base_time + (k - 1) hours``: C-MAPSS has no
calendar, so this is a replay clock, not real time, and the UI says so.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.user import User
from app.models.v2.decision_case import DecisionCase
from app.models.v2.rule_runtime import RuleAlert, RuleResource, RuleVersion
from app.models.v2.sensor_playback import SensorPlayback, SensorPlaybackTick
from app.models.v2.time_series import TimeSeriesSync
from app.schemas.v2.object_query import ExecutionContext
from app.schemas.v2.rule_runtime import EvaluateRuleRequest
from app.services.v2.object_query.authorization import build_query_policy, policies_from_projection, require_ontology_access
from app.services.v2.object_query.core import FalkorReadAdapter, QueryCore
from app.services.v2.object_query.data_views import build_live_view
from app.services.v2.object_query.errors import ObjectQueryError
from app.services.v2.object_query.metadata import load_sql_metadata
from app.services.v2.rule_runtime import RuleService, _rolling, series_loader

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "factory_twin"
BASE_TIME = datetime(2026, 11, 3, tzinfo=timezone.utc)  # the instant of cycle 1
VIEW_RETENTION_SECONDS = 24 * 3600
MAX_STEPS = 20

DATASETS: dict[str, dict[str, str]] = {
    "cmapss_fd001_t50": {
        "label": "NASA C-MAPSS FD001 · T50", "file": "sensor_trajectories.csv", "channel": "sensor_4", "channel_label": "T50 低压涡轮出口温度",
        "unit": "°R", "source": "NASA C-MAPSS FD001，机组 1–5，从首个周期到失效", "clock": "1 个周期 = 1 小时（回放时钟，不是真实时间）"},
}


class PlaybackError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@lru_cache(maxsize=4)
def load_trajectories(dataset_id: str) -> dict[int, list[float]]:
    """Engine unit -> channel value of cycle 1, 2, ... (cycles are contiguous in the recorded file)."""
    dataset = DATASETS[dataset_id]
    by_unit: dict[int, dict[int, float]] = {}
    with (EXAMPLES / dataset["file"]).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            by_unit.setdefault(int(row["unit"]), {})[int(row["cycle"])] = float(row[dataset["channel"]])
    return {unit: [cycles[cycle] for cycle in sorted(cycles)] for unit, cycles in by_unit.items()}


def series_id_for(ontology_id: str, asset_id: str, channel: str) -> str:
    """Deterministic, so the object can carry it as its Time Series Property value before any playback exists."""
    return "series:" + sha256(f"{ontology_id}:{asset_id}:{channel}".encode()).hexdigest()[:40]


def cycle_time(base: datetime, cycle: int) -> datetime:
    return base + timedelta(hours=cycle - 1)


def cycle_of(base: datetime, stamp: str | None) -> int | None:
    if not stamp:
        return None
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return int(round((moment - base).total_seconds() / 3600)) + 1


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _logic(db: Session, rule: RuleResource) -> dict:
    version = db.query(RuleVersion).filter_by(rule_id=rule.id, version=rule.current_version).first()
    if rule.current_version < 1 or version is None:
        raise PlaybackError("rule_not_approved", "规则还没有批准的版本，不能用于回放", 409)
    return version.logic or {}


def _window_block(logic: dict) -> dict | None:
    return next((block for block in logic.get("blocks", []) if block.get("kind") == "series_window"), None)


# --------------------------------------------------------------------------------------------------------- creation
def create_playback(db: Session, ontology_id: str, user: User, *, name: str, dataset_id: str, rule_id: str, mapping: list[dict],
                    start_cycle: int, step_cycles: int) -> SensorPlayback:
    dataset = DATASETS.get(dataset_id)
    if dataset is None:
        raise PlaybackError("dataset_not_found", "没有这个回放数据集", 404)
    trajectories = load_trajectories(dataset_id)
    rule = db.query(RuleResource).filter_by(id=rule_id, ontology_id=ontology_id).first()
    if rule is None:
        raise PlaybackError("rule_not_found", "规则不存在", 404)
    block = _window_block(_logic(db, rule))
    if block is None:
        raise PlaybackError("rule_without_series", "规则没有时间窗口条件，不能用于回放")
    series_property = block["series_property"]
    if not mapping:
        raise PlaybackError("mapping_required", "请至少映射一台设备")
    resolved = []
    for row in mapping:
        unit, object_id = row.get("unit"), row.get("object_id")
        if unit not in trajectories:
            raise PlaybackError("unit_not_found", f"数据集里没有机组 {unit}")
        instance = db.query(EntityInstance).filter_by(id=object_id, ontology_id=ontology_id).first()
        if instance is None:
            raise PlaybackError("object_not_found", f"对象不存在：{object_id}", 404)
        asset_id = str(row.get("asset_id") or instance.row_identity)
        if (instance.row_data or {}).get(series_property) != series_id_for(ontology_id, asset_id, dataset["channel"]):
            raise PlaybackError("series_not_linked", f"对象 {asset_id} 的 {series_property} 没有指向这条回放序列", 409)
        entity = db.get(Entity, instance.entity_id)
        resolved.append({"asset_id": asset_id, "object_id": object_id, "unit": unit, "name": (instance.row_data or {}).get("name") or asset_id,
                         "object_type": (entity.name_en if entity and entity.name_en else None) or "FactoryAsset"})
    max_cycle = max(len(trajectories[row["unit"]]) for row in resolved)
    if not 1 <= start_cycle <= max_cycle or not 1 <= step_cycles <= 50:
        raise PlaybackError("invalid_range", f"起始周期须在 1–{max_cycle} 之间，步长须在 1–50 之间")
    playback = SensorPlayback(ontology_id=ontology_id, name=name, dataset_id=dataset_id, rule_id=rule.id, series_property=series_property,
                              mapping=resolved, base_time=BASE_TIME, start_cycle=start_cycle, cursor=start_cycle, step_cycles=step_cycles,
                              max_cycle=max_cycle, created_by=user.id)
    db.add(playback)
    db.commit()
    return playback


# -------------------------------------------------------------------------------------------------------- stepping
def _points(playback: SensorPlayback, unit: int, cursor: int) -> list[dict]:
    values = load_trajectories(playback.dataset_id)[unit][:cursor]
    return [{"timestamp": cycle_time(_utc(playback.base_time), cycle).isoformat(), "value": value} for cycle, value in enumerate(values, start=1)]


def _pin_view(db: Session, graph, ontology_id: str, user: User, playback: SensorPlayback, cursor: int):
    """A new immutable view of the live ontology; a write racing the copy fails it, so try again."""
    last: ObjectQueryError | None = None
    for _ in range(3):
        try:
            return build_live_view(db, graph, load_sql_metadata(db, ontology_id), ontology_id, user.id,
                                   f"playback:{playback.id}:{cursor}", VIEW_RETENTION_SECONDS)
        except ObjectQueryError as exc:
            if exc.code != "snapshot_unavailable":
                raise
            last = exc
    raise PlaybackError("view_unavailable", "本体正在变化，暂时无法固定数据视图，请重试", 409) from last


def _evaluate(db: Session, ontology_id: str, user: User, playback: SensorPlayback, view, graph) -> dict:
    service = RuleService(db, ontology_id, user, load_sql_metadata(db, ontology_id))
    service.access = require_ontology_access(db, ontology_id, str(user.id), user.role)
    policy = build_query_policy(db, ontology_id, str(user.id), user.role, preloaded_policies=policies_from_projection(service.access))
    core = QueryCore(service.metadata, FalkorReadAdapter(graph._graph(view.graph_key), graph_ontology_id=view.graph_key), policy)
    request = EvaluateRuleRequest(context=ExecutionContext(ontology_id=ontology_id, consistency="snapshot", data_view_id=view.id), mode="commit")
    return service.evaluate(playback.rule_id, request, core, series_loader(db, view))


def advance(db: Session, graph, playback_id: str, ontology_id: str, user: User, *, steps: int = 1, until_alert: bool = False) -> list[SensorPlaybackTick]:
    if not graph.available:
        raise PlaybackError("graph_unavailable", "FalkorDB 不可用", 503)
    steps = max(1, min(steps, MAX_STEPS))
    ticks: list[SensorPlaybackTick] = []
    for _ in range(steps):
        playback = db.query(SensorPlayback).filter_by(id=playback_id, ontology_id=ontology_id).with_for_update().first()
        if playback is None:
            raise PlaybackError("playback_not_found", "回放不存在", 404)
        if playback.cursor >= playback.max_cycle:
            break
        cursor = min(playback.cursor + playback.step_cycles, playback.max_cycle)
        try:
            view = _pin_view(db, graph, ontology_id, user, playback, cursor)
            for entry in playback.mapping:
                dataset = DATASETS[playback.dataset_id]
                points = _points(playback, entry["unit"], cursor)
                db.add(TimeSeriesSync(ontology_id=ontology_id, data_view_id=view.id, series_id=series_id_for(ontology_id, entry["asset_id"], dataset["channel"]),
                                      root_type=entry["object_type"], root_id=entry["object_id"], sensor_type="Sensor", sensor_id=f"unit{entry['unit']}:{dataset['channel']}",
                                      property_api_name=playback.series_property, unit=dataset["unit"], interpolation="none", points=points, point_count=len(points)))
            db.commit()
            summary = _evaluate(db, ontology_id, user, playback, view, graph)
        except ObjectQueryError as exc:
            db.rollback()
            raise PlaybackError(exc.code, str(exc), 409) from exc
        new_alerts = [row.id for row in db.query(RuleAlert).filter_by(rule_id=playback.rule_id, first_evaluation_id=summary["id"]).all()]
        sequence = db.query(SensorPlaybackTick).filter_by(playback_id=playback.id).count()
        tick = SensorPlaybackTick(playback_id=playback.id, sequence=sequence, cursor=cursor, view_id=view.id, evaluation_id=summary["id"],
                                  summary={key: summary.get(key) for key in ("input_count", "output_count", "triggered_count", "recovered_count", "recurring_count")},
                                  new_alert_ids=new_alerts, created_by=user.id)
        db.add(tick)
        playback.cursor, playback.latest_view_id, playback.etag = cursor, view.id, playback.etag + 1
        db.commit()
        ticks.append(tick)
        if until_alert and new_alerts:
            break
    return ticks


# --------------------------------------------------------------------------------------------------------- reading
def _alert_view(alert: RuleAlert, playback: SensorPlayback, entry: dict | None, case: DecisionCase | None) -> dict:
    base = _utc(playback.base_time)
    evidence = alert.evidence or {}
    return {"id": alert.id, "subject_id": alert.subject_id, "asset_id": entry["asset_id"] if entry else None, "name": entry["name"] if entry else alert.subject_id,
            "status": alert.status, "severity": alert.severity, "window_start": alert.window_start, "window_end": alert.window_end,
            "start_cycle": cycle_of(base, alert.window_start), "end_cycle": cycle_of(base, alert.window_end),
            "peak": evidence.get("peak"), "breaching_windows": evidence.get("breaching_windows"), "ongoing": evidence.get("ongoing"),
            "occurrence_count": alert.occurrence_count, "recovered": alert.recovered_at is not None, "suggested_action": alert.suggested_action,
            "case": {"id": case.id, "status": case.status, "created_by": case.created_by, "reviewed_by": case.reviewed_by, "action_run_id": case.action_run_id} if case else None}


def detail(db: Session, playback: SensorPlayback) -> dict[str, Any]:
    dataset = DATASETS[playback.dataset_id]
    rule = db.get(RuleResource, playback.rule_id)
    try:
        block = _window_block(_logic(db, rule)) if rule else None
    except PlaybackError:
        block = None
    window = int(block["window_points"]) if block else 1
    rows = {row.series_id: row for row in db.query(TimeSeriesSync).filter_by(data_view_id=playback.latest_view_id).all()} if playback.latest_view_id else {}
    instances = {row.id: row for row in db.query(EntityInstance).filter(EntityInstance.id.in_([entry["object_id"] for entry in playback.mapping])).all()}
    base = _utc(playback.base_time)
    series = []
    for entry in playback.mapping:
        sync = rows.get(series_id_for(playback.ontology_id, entry["asset_id"], dataset["channel"]))
        values = [point["value"] for point in (sync.points if sync else [])]
        average = [None] * (window - 1) + _rolling(values, window, block["aggregate"]) if block and len(values) >= window else [None] * len(values)
        properties = (instances[entry["object_id"]].row_data or {}) if entry["object_id"] in instances else {}
        series.append({**entry, "risk_level": properties.get("risk_level"), "health_score": properties.get("health_score"),
                       "cycles": list(range(1, len(values) + 1)), "values": values, "average": average,
                       "total_cycles": len(load_trajectories(playback.dataset_id)[entry["unit"]])})
    alerts = db.query(RuleAlert).filter_by(rule_id=playback.rule_id).order_by(RuleAlert.created_at).all()
    cases = {row.client_request_id: row for row in db.query(DecisionCase).filter(DecisionCase.ontology_id == playback.ontology_id,
             DecisionCase.client_request_id.in_([f"alert:{alert.id}" for alert in alerts] or [""])).all()}
    by_object = {entry["object_id"]: entry for entry in playback.mapping}
    ticks = db.query(SensorPlaybackTick).filter_by(playback_id=playback.id).order_by(SensorPlaybackTick.sequence).all()
    return {
        "id": playback.id, "ontology_id": playback.ontology_id, "name": playback.name, "etag": playback.etag,
        "dataset": {"id": playback.dataset_id, **dataset}, "cursor": playback.cursor, "start_cycle": playback.start_cycle,
        "step_cycles": playback.step_cycles, "max_cycle": playback.max_cycle, "finished": playback.cursor >= playback.max_cycle,
        "base_time": base.isoformat(), "rule": {"id": playback.rule_id, "display_name": rule.display_name if rule else "", "status": rule.status if rule else None,
                                                "window_points": window, "aggregate": block["aggregate"] if block else None, "op": block["op"] if block else None,
                                                "threshold": block["threshold"] if block else None,
                                                "min_consecutive_windows": block["min_consecutive_windows"] if block else None},
        "series": series,
        "alerts": [_alert_view(alert, playback, by_object.get(alert.subject_id), cases.get(f"alert:{alert.id}")) for alert in alerts],
        "ticks": [{"sequence": tick.sequence, "cursor": tick.cursor, "view_id": tick.view_id, "evaluation_id": tick.evaluation_id,
                   "summary": tick.summary, "new_alert_ids": tick.new_alert_ids, "created_at": tick.created_at.isoformat()} for tick in ticks]}
