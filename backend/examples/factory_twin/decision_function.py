"""Factory schedule decision, written as a user Function (``decision/v1``).

This file is user content: it is submitted through the Function API and runs
only in the isolated function runner.  The backend never imports it.

Inputs (Object Query records): ``schedule_items``, ``assets`` and, optionally,
``resources`` (for display names).  Optional settings: ``maintenance_minutes``
(default: median length of the planned maintenance tasks, else 45),
``snap_minutes`` (default 15), ``modify_action`` / ``maintenance_action``
(names of the published scenario Actions the candidates compile through).

The model is deterministic and reads everything from the data; no task, asset
or resource id is written into this file.

* Baseline.  Each work order's finish against its due date; "risk exposure":
  production minutes that run on a resource holding a high-risk asset that has
  not been maintained before the task starts; maintenance coverage of the
  high-risk assets; unscheduled production tasks.
* Candidates.  Schedule the missing predictive maintenance of every uncovered
  high-risk asset in the earliest free slot before its resource's first
  production run; place the unscheduled production tasks in the earliest free
  compatible slot; both together; and the naive "take the line now" placement.
* Every candidate is re-simulated and re-validated with the rules of
  ``schedule_validation.py`` (resource compatibility, material, quality hold,
  precedence, capacity) plus maintenance-versus-production capacity and the
  resource operating window.  A candidate is applicable exactly when it adds no
  hard violation, which is also the ``decision/v1`` invariant.

Every figure carries a basis: 观测 (schedule fields, due dates and sensor
readings from the fixture / NASA C-MAPSS subset), 推断 (risk level and health
score derived from sensors) or 模拟 (candidate results).  Missing due dates or
health data are reported as evidence gaps and never guessed.
"""
import json
import re
from datetime import datetime, timedelta, timezone

HIGH_RISK_SCORE = 65          # used only when an asset has a health score but no risk level
DEFAULT_WINDOW_HOURS = (6, 18)  # used only for resources without availability rows
DELIVERY_RISK = ("未排产", "延误")


def _dt(value):
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hm(value):
    return value.astimezone(timezone.utc).strftime("%H:%M") if value else "—"


def _minutes(start, end):
    return int(round((end - start).total_seconds() / 60))


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _list(value):
    if value in (None, ""):
        return []
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def _records(rows):
    return [(row["object_id"], {key: val for key, val in (row.get("properties") or {}).items() if val is not None})
            for row in rows or []]


def _tasks(rows):
    tasks = {}
    for object_id, props in _records(rows):
        task_id = str(props.get("schedule_id") or object_id)
        duration = _number(props.get("duration_minutes"))
        tasks[task_id] = {
            "object_id": object_id, "object_type": str(props.get("object_type") or "ScheduleItem"),
            "id": task_id, "name": str(props.get("name") or task_id), "layer": str(props.get("layer") or "production"),
            "resources": _list(props.get("resource")), "resource_is_list": isinstance(props.get("resource"), list),
            "start": _dt(props.get("start")), "end": _dt(props.get("end")),
            "predecessors": _list(props.get("predecessors")), "compatible": _list(props.get("compatible_resources")) or None,
            "material_ready": props.get("material_ready"), "quality_hold": props.get("quality_hold"),
            "order_id": str(props.get("order_id") or task_id), "due": _dt(props.get("due")),
            "duration": timedelta(minutes=duration) if duration and duration > 0 else None,
        }
    return tasks


def _assets(rows, gaps):
    assets = []
    for object_id, props in _records(rows):
        asset_id = str(props.get("asset_id") or object_id)
        level, score = props.get("risk_level"), _number(props.get("health_score"))
        if level:
            high = str(level).lower() == "high"
        elif score is not None:
            high = score < HIGH_RISK_SCORE
        else:
            high = False
            gaps.append(f"{asset_id} 缺少风险等级和健康评分，无法判断是否需要检修")
        if not props.get("resource_id"):
            gaps.append(f"{asset_id} 没有所属资源，无法关联到排程")
        assets.append({"id": asset_id, "name": str(props.get("name") or asset_id), "resource_id": str(props.get("resource_id") or ""),
                       "high": high, "risk_level": level, "health_score": score, "source_unit": props.get("source_unit"),
                       "latest_cycle": props.get("latest_cycle"),
                       "sensors": {key: val for key, val in sorted(props.items()) if key.startswith("sensor_")}})
    return assets


def _maintenance_for(tasks, asset):
    marker = asset["id"].lower()
    found = []
    for task in tasks.values():
        if task["layer"] != "maintenance" or asset["resource_id"] not in task["resources"]:
            continue
        tokens = set(re.split(r"[^0-9a-z]+", task["name"].lower()))
        if task["order_id"].lower() == f"m-{marker}" or task["id"].lower() == f"maint-{marker}" or marker in tokens:
            found.append(task)
    return found


def _default_window(day):
    midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(hours=DEFAULT_WINDOW_HOURS[0]), midnight + timedelta(hours=DEFAULT_WINDOW_HOURS[1])


def _windows(tasks):
    windows = {}
    for task in tasks.values():
        if task["layer"] == "constraint" and task["start"] and task["end"]:
            for resource in task["resources"]:
                low, high = windows.get(resource, (task["start"], task["end"]))
                windows[resource] = (min(low, task["start"]), max(high, task["end"]))
    return windows


def _evaluate(tasks, assets):
    high = [asset for asset in assets if asset["high"]]
    production = [task for task in tasks.values() if task["layer"] == "production"]
    timed = [task for task in production if task["start"] and task["end"]]
    maintained, first_use = {}, {}
    for asset in high:
        done = [task["end"] for task in _maintenance_for(tasks, asset) if task["start"] and task["end"]]
        maintained[asset["id"]] = min(done) if done else None
        uses = [task["start"] for task in timed if asset["resource_id"] in task["resources"]]
        first_use[asset["id"]] = min(uses) if uses else None
    exposure = {}
    for task in timed:
        hit = sorted(asset["id"] for asset in high if asset["resource_id"] in task["resources"]
                     and (maintained[asset["id"]] is None or maintained[asset["id"]] > task["start"]))
        if hit:
            exposure[task["id"]] = (_minutes(task["start"], task["end"]), hit)
    by_order = {}
    for task in production:
        by_order.setdefault(task["order_id"], []).append(task)
    orders = {}
    for order_id, items in sorted(by_order.items()):
        scheduled = [task for task in items if task["start"] and task["end"]]
        missing = [task for task in items if not (task["start"] and task["end"])]
        dues = [task["due"] for task in items if task["due"]]
        due = min(dues) if dues else None
        finish = max((task["end"] for task in scheduled), default=None)
        exposed = sum(exposure[task["id"]][0] for task in items if task["id"] in exposure)
        if missing:
            status, late = "未排产", 0
        elif due is None:
            status, late = "缺少交期", 0
        else:
            late = max(0, _minutes(due, finish))
            status = "延误" if late else "按期"
        orders[order_id] = {"status": status, "late": late, "due": due, "finish": finish, "exposed": exposed, "missing": missing,
                            "exposure": [(task, exposure[task["id"]]) for task in items if task["id"] in exposure],
                            "at_risk": status in DELIVERY_RISK or exposed > 0}
    covered = [asset for asset in high if maintained[asset["id"]] is not None
               and (first_use[asset["id"]] is None or maintained[asset["id"]] <= first_use[asset["id"]])]
    return {"orders": orders, "exposure_total": sum(minutes for minutes, _ in exposure.values()),
            "delivery_risk": sum(1 for row in orders.values() if row["status"] in DELIVERY_RISK),
            "tardiness": sum(row["late"] for row in orders.values()),
            "unscheduled": sum(1 for task in production if not (task["start"] and task["end"])),
            "high": high, "covered": covered, "coverage": round(len(covered) / len(high), 4) if high else 1.0,
            "latest_finish": max((task["end"] for task in timed), default=None), "first_use": first_use}


def _violations(tasks, windows, day):
    found = {}

    def add(code, task_ids, detail, message):
        found.setdefault((code, tuple(task_ids)), {"code": code, "task_ids": list(task_ids), "detail": detail, "message": message})

    active = [task for task in tasks.values() if task["layer"] != "constraint" and task["start"] and task["end"]]
    for task in active:
        if task["compatible"]:
            bad = sorted(set(task["resources"]) - set(task["compatible"]))
            if bad:
                add("incompatible_resource", [task["id"]], ", ".join(bad), f"{task['name']} 使用了不兼容的资源 {', '.join(bad)}")
        if task["material_ready"] is False:
            add("material_not_ready", [task["id"]], "", f"{task['name']} 物料尚未就绪")
        if task["quality_hold"] is True:
            add("quality_hold", [task["id"]], "", f"{task['name']} 处于质量冻结")
        for predecessor_id in task["predecessors"]:
            predecessor = tasks.get(predecessor_id)
            if predecessor and predecessor["end"] and predecessor["end"] > task["start"]:
                add("precedence", [predecessor_id, task["id"]], "", f"{task['name']} 在前置任务 {predecessor['name']} 完成前开始")
        for resource in task["resources"]:
            low, high = windows.get(resource) or _default_window(day)
            if task["start"] < low or task["end"] > high:
                add("outside_operating_window", [task["id"]], resource, f"{task['name']} 超出 {resource} 的可用时段 {_hm(low)}–{_hm(high)}")
    for index, left in enumerate(active):
        for right in active[index + 1:]:
            shared = sorted(set(left["resources"]) & set(right["resources"]))
            if shared and left["start"] < right["end"] and right["start"] < left["end"]:
                pair = sorted([left["id"], right["id"]])
                add("capacity_collision", pair, ", ".join(shared), f"{left['name']} 与 {right['name']} 在 {', '.join(shared)} 上时间重叠")
    return found


def _snap_up(value, snap):
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    steps = -(-int((value - midnight).total_seconds()) // (snap * 60))
    return midnight + timedelta(minutes=steps * snap)


def _free_slot(tasks, resource, duration, windows, snap, day, latest_end=None):
    low, high = windows.get(resource) or _default_window(day)
    if latest_end is not None:
        high = min(high, latest_end)
    busy = sorted((task["start"], task["end"]) for task in tasks.values()
                  if task["layer"] != "constraint" and task["start"] and task["end"] and resource in task["resources"])
    cursor = _snap_up(low, snap)
    for start, end in busy:
        if end <= cursor:
            continue
        if start >= cursor + duration:
            break
        cursor = _snap_up(max(cursor, end), snap)
    return cursor if cursor + duration <= high else None


def _apply(base, edits):
    tasks = {key: dict(value) for key, value in base.items()}
    for edit in edits:
        task = tasks.get(edit["task_id"])
        if task is None:
            task = {"object_id": None, "object_type": "ScheduleItem", "id": edit["task_id"], "name": edit["name"],
                    "layer": "maintenance", "resource_is_list": False, "predecessors": [], "compatible": list(edit["resources"]),
                    "material_ready": True, "quality_hold": None, "order_id": edit["order_id"], "due": None, "duration": None}
            tasks[edit["task_id"]] = task
        task.update(start=edit["start"], end=edit["end"], resources=list(edit["resources"]))
    return tasks


def run(inputs):
    gaps = []
    base = _tasks(inputs.get("schedule_items"))
    assets = _assets(inputs.get("assets"), gaps)
    labels = {}
    for object_id, props in _records(inputs.get("resources")):
        labels[str(props.get("resource_id") or object_id)] = str(props.get("name") or props.get("label") or object_id)
    label = lambda resource: labels.get(resource, resource)  # noqa: E731
    snap = int(_number(inputs.get("snap_minutes")) or 15)
    modify_action = str(inputs.get("modify_action") or "修改排程项")
    maintenance_action = str(inputs.get("maintenance_action") or "安排维护任务")
    windows = _windows(base)
    day = min((task["start"] for task in base.values() if task["start"]), default=datetime(2026, 1, 1, tzinfo=timezone.utc))
    before = _evaluate(base, assets)
    base_violations = _violations(base, windows, day)
    by_asset = {asset["id"]: asset for asset in assets}
    for order_id, row in before["orders"].items():
        if row["status"] == "缺少交期":
            gaps.append(f"{order_id} 缺少交期，无法判断是否延误")

    planned = sorted(_minutes(task["start"], task["end"]) for task in base.values()
                     if task["layer"] == "maintenance" and task["start"] and task["end"])
    maintenance_minutes = _number(inputs.get("maintenance_minutes")) or (planned[len(planned) // 2] if planned else 45)
    maintenance_length = timedelta(minutes=maintenance_minutes)

    def maintenance_edits(state):
        edits = []
        for asset in before["high"]:
            if asset in before["covered"] or not asset["resource_id"]:
                continue
            current = _apply(state, edits)
            start = _free_slot(current, asset["resource_id"], maintenance_length, windows, snap, day,
                               latest_end=before["first_use"][asset["id"]])
            if start is None:
                start = _free_slot(current, asset["resource_id"], maintenance_length, windows, snap, day)
            if start is None:
                continue
            existing = base.get(f"maint-{asset['id']}")
            if existing and existing["object_id"]:  # a planned but unscheduled maintenance object: move it, do not duplicate it
                edits.append({"kind": "move", "task_id": existing["id"], "start": start, "end": start + maintenance_length,
                              "resources": [asset["resource_id"]]})
            else:
                edits.append({"kind": "maintenance", "task_id": f"maint-{asset['id']}", "asset_id": asset["id"],
                              "name": f"Predictive inspection · {asset['id']}", "order_id": f"M-{asset['id']}",
                              "start": start, "end": start + maintenance_length, "resources": [asset["resource_id"]]})
        return edits

    waiting = sorted((task for task in base.values() if task["layer"] == "production" and not (task["start"] and task["end"])),
                     key=lambda task: (task["due"] or datetime.max.replace(tzinfo=timezone.utc), task["id"]))

    def placement_edits(state):
        edits = []
        for task in waiting:
            current = _apply(state, edits)
            length = task["duration"] or timedelta(hours=1)
            options = []
            for resource in task["compatible"] or task["resources"]:
                start = _free_slot(current, resource, length, windows, snap, day)
                if start is not None:
                    options.append((start, resource))
            if options:
                start, resource = min(options)
                edits.append({"kind": "move", "task_id": task["id"], "start": start, "end": start + length, "resources": [resource]})
        return edits

    def naive_edits():
        for task in waiting:
            length = task["duration"] or timedelta(hours=1)
            for resource in task["compatible"] or task["resources"]:
                busy = sorted(other["start"] for other in base.values() if other["layer"] == "production"
                              and other["start"] and other["end"] and resource in other["resources"])
                if busy:
                    return [{"kind": "move", "task_id": task["id"], "start": busy[0], "end": busy[0] + length, "resources": [resource]}]
        return []

    def describe(edit):
        if edit["kind"] == "maintenance":
            return f"{edit['asset_id']} 预测性检修 {label(edit['resources'][0])} {_hm(edit['start'])}–{_hm(edit['end'])}"
        return f"{base[edit['task_id']]['name']} 排入 {label(edit['resources'][0])} {_hm(edit['start'])}–{_hm(edit['end'])}"

    def action(edit):
        if edit["kind"] == "maintenance":
            return {"action_type": maintenance_action, "parameters": {
                "schedule_id": edit["task_id"], "name": edit["name"], "order_id": edit["order_id"],
                "start": _iso(edit["start"]), "end": _iso(edit["end"]), "resource": edit["resources"][0]}}
        task = base[edit["task_id"]]
        resource = list(edit["resources"]) if task["resource_is_list"] else edit["resources"][0]
        return {"action_type": modify_action, "parameters": {
            "task": {"object_type": task["object_type"], "object_id": task["object_id"]},
            "start": _iso(edit["start"]), "end": _iso(edit["end"]), "resource": resource}}

    def candidate(candidate_id, title, edits):
        tasks = _apply(base, edits)
        after = _evaluate(tasks, assets)
        added = _violations(tasks, windows, day)
        hard = [{"code": row["code"], "task_ids": row["task_ids"], "message": row["message"]}
                for key, row in sorted(added.items()) if key not in base_violations]
        protected = [order_id for order_id, row in before["orders"].items() if row["at_risk"] and not after["orders"][order_id]["at_risk"]]
        harmed = [order_id for order_id, row in before["orders"].items() if not row["at_risk"] and after["orders"][order_id]["at_risk"]]
        return {"id": candidate_id, "label": title, "applicable": not hard, "hard_constraints": hard,
                "metric_after": after["exposure_total"], "cost": sum(_minutes(edit["start"], edit["end"]) for edit in edits),
                "protected": protected, "harmed": harmed, "actions": [action(edit) for edit in edits],
                "delivery_risk_after": after["delivery_risk"], "tardiness_after": after["tardiness"],
                "coverage_after": after["coverage"], "unscheduled_after": after["unscheduled"],
                "latest_finish_after": _hm(after["latest_finish"]), "changes": [describe(edit) for edit in edits], "basis": "模拟"}

    candidates = []
    maintain = maintenance_edits(base)
    place = placement_edits(base)
    if maintain:
        candidates.append(candidate("maintain", "安排高风险设备检修：" + "；".join(describe(edit) for edit in maintain), maintain))
    if place:
        candidates.append(candidate("place", "加急工单排产：" + "；".join(describe(edit) for edit in place), place))
    if maintain and place:
        combined = maintain + placement_edits(_apply(base, maintain))
        candidates.append(candidate("combined", "组合：" + "；".join(describe(edit) for edit in combined), combined))
    naive = naive_edits()
    if naive:
        task = base[naive[0]["task_id"]]
        candidates.append(candidate("naive", f"{task['name']} 直接插入 {label(naive[0]['resources'][0])} {_hm(naive[0]['start'])}（不调整其他任务）", naive))
    candidates.sort(key=lambda row: (not row["applicable"], row["metric_after"], row["delivery_risk_after"], row["cost"], row["id"]))
    for index, row in enumerate(candidates):
        row["recommended"] = index == 0 and row["applicable"]
        row["verdict"] = "推荐" if index == 0 and row["applicable"] else ("可行" if row["applicable"] else "不可行：" + "、".join(
            sorted({item["code"] for item in row["hard_constraints"]})))

    subjects = []
    for order_id, row in before["orders"].items():
        reasons, evidence, path = [], [], []
        for task, (minutes, hit) in row["exposure"]:
            for asset_id in hit:
                asset = by_asset[asset_id]
                reasons.append(f"{task['name']} 在 {label(asset['resource_id'])} 运行 {minutes} 分钟，该资源上的高风险设备 {asset_id}"
                               f"（健康分 {asset['health_score'] if asset['health_score'] is not None else '未知'}）尚未检修（推断）")
                if not path:
                    path = [f"{asset_id} · {asset['name']}", label(asset["resource_id"]), task["name"], order_id]
        if row["status"] == "未排产":
            reasons.append(f"{'、'.join(task['name'] for task in row['missing'])} 尚未排产，交期 {_hm(row['due'])}（观测）")
        elif row["status"] == "延误":
            reasons.append(f"计划 {_hm(row['finish'])} 完工，晚于交期 {_hm(row['due'])} {row['late']} 分钟（观测·当前计划）")
        elif row["status"] == "缺少交期":
            reasons.append("缺少交期，无法判断是否延误（证据不足）")
        else:
            reasons.append(f"计划 {_hm(row['finish'])} 完工，交期 {_hm(row['due'])}，按期（观测·当前计划）")
        evidence.append({"label": "交期", "value": _hm(row["due"]), "basis": "观测·演示 fixture"})
        evidence.append({"label": "计划完工", "value": _hm(row["finish"]), "basis": "观测·当前计划"})
        evidence.append({"label": "高风险暴露（分钟）", "value": row["exposed"], "basis": "推断"})
        for asset_id in sorted({asset_id for _, (_, hit) in row["exposure"] for asset_id in hit}):
            asset = by_asset[asset_id]
            source = f"NASA C-MAPSS FD001 unit {asset['source_unit']} 第 {asset['latest_cycle']} 周期"
            evidence.append({"label": f"{asset_id} 健康分", "value": asset["health_score"], "basis": f"推断·{source}"})
            for sensor, value in asset["sensors"].items():
                evidence.append({"label": f"{asset_id} {sensor}", "value": value, "basis": f"观测·{source}"})
        subjects.append({"id": order_id, "label": order_id, "status": row["status"], "at_risk": row["at_risk"],
                         "metric_value": row["exposed"], "reasons": reasons, "path": path or [order_id], "evidence": evidence})

    decision = {
        "schema": "decision/v1", "title": "设备健康风险与工单交付协同决策",
        "subject_type": "WorkOrder", "subject_label": "工单",
        "metric": {"key": "risk_exposure_minutes", "label": "高风险设备暴露时长", "unit": "分钟", "direction": "minimize",
                   "baseline": before["exposure_total"]},
        "kpis": [{"label": "延误或未排工单", "value": before["delivery_risk"], "basis": "观测"},
                 {"label": "总延误（分钟）", "value": before["tardiness"], "basis": "观测"},
                 {"label": "高风险设备", "value": len(before["high"]), "basis": "推断"},
                 {"label": "维护覆盖率", "value": before["coverage"], "basis": "推断"},
                 {"label": "未排任务", "value": before["unscheduled"], "basis": "观测"},
                 {"label": "最晚完工", "value": _hm(before["latest_finish"]), "basis": "观测"}],
        "subjects": subjects, "candidates": candidates,
        "evidence_gaps": gaps,
        "provenance": {"basis": {"观测": "排程字段、交期与传感器读数（演示 fixture、NASA C-MAPSS FD001 子集）",
                                 "推断": "风险等级与健康评分（由传感器派生的健康指标）",
                                 "模拟": "候选方案的完工时间、暴露时长与维护覆盖率"},
                       "maintenance_minutes": maintenance_minutes, "snap_minutes": snap,
                       "rules": ["incompatible_resource", "material_not_ready", "quality_hold", "precedence",
                                 "capacity_collision", "outside_operating_window"]},
    }
    return {"decision": decision}
