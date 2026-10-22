"""Small, explainable what-if calculations over pinned benchmark slices."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3] / "data" / "decision_lab"
RULE_VERSION = {"ai4i": "maintenance_triage_v1", "factorynet": "velocity_residual_v1", "ibadas": "evidence_budget_v1"}
ONTOLOGY_IDS = {domain: f"decision-lab-{domain}-v1" for domain in RULE_VERSION}
DATASET_IDS = {domain: f"decision-lab-{domain}-dataset-v1" for domain in ("ai4i", "factorynet")}
DATASET_VERSION_IDS = {domain: f"decision-lab-{domain}-version-v1" for domain in ("ai4i", "factorynet")}

PRESETS: dict[str, list[dict[str, Any]]] = {
    "ai4i": [
        {"key": "broad", "name": "高覆盖复核", "parameters": {"threshold": 25, "review_minutes": 8}},
        {"key": "balanced", "name": "平衡复核", "parameters": {"threshold": 40, "review_minutes": 8}},
        {"key": "focused", "name": "低工作量复核", "parameters": {"threshold": 60, "review_minutes": 8}},
    ],
    "factorynet": [
        {"key": "sensitive", "name": "敏感阈值", "parameters": {"threshold": 0.25, "review_minutes": 30}},
        {"key": "balanced", "name": "平衡阈值", "parameters": {"threshold": 0.5, "review_minutes": 30}},
        {"key": "conservative", "name": "保守阈值", "parameters": {"threshold": 1.0, "review_minutes": 30}},
    ],
    "ibadas": [
        {"key": "rgb", "name": "RGB 快审", "parameters": {"policy": "rgb", "budget_mb": 12}},
        {"key": "rgb_depth", "name": "RGB + 深度", "parameters": {"policy": "rgb_depth", "budget_mb": 12}},
        {"key": "complete", "name": "四模态复核", "parameters": {"policy": "complete", "budget_mb": 12}},
    ],
}

POLICY_ROLES = {"rgb": ("rgb",), "rgb_depth": ("rgb", "depth"),
                "complete": ("rgb", "depth", "mask", "point_cloud")}

DESCRIPTIONS = {
    "ai4i": {"title": "AI4I · 维护优先级", "data_class": "regular", "summary": "调整复核阈值，比较工作量与样例故障标签覆盖。",
              "formula": "规则分数 = min(100, min(40, 磨损分钟/250×40) + max(0, 10−温差K)×7 + 功率越界20 + 磨损×扭矩>11000时20)。功率=扭矩×转速×2π/60。",
              "caveat": "AI4I 是合成数据；切片按故障模式特意抽样。覆盖率只是这 29 条的回看值，不能当预测准确率。"},
    "factorynet": {"title": "FactoryNet · CNC 时序偏差", "data_class": "temporal", "summary": "调整速度偏差阈值，比较告警点、需复核 episode 和人工时长。",
                   "formula": "速度偏差 = |feedback_vel_0 − setpoint_vel_0|；任一采样点达到阈值时将该 episode 列入人工复核。",
                   "caveat": "每个 episode 只保留 64 个等距点；采样并非连续窗口，且原数据的速度单位未在此换算。磨损和目检结果只用于回看。"},
    "ibadas": {"title": "I-BADAS · 证据预算", "data_class": "multimodal", "summary": "在固定传输预算下比较 RGB、RGB+深度和四模态复核的样本覆盖。",
               "formula": "按样本键 SHA-256 排序，不读取标签决定顺序；逐个累加所选模态原始文件字节数，预算不足时跳过该样本。",
               "caveat": "异常标签来自官方标注，仅作回看覆盖统计；此处没有训练或运行缺陷识别模型。元数据和可见掩码不计入传输预算。"},
}


def canonical_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_examples(domain: str) -> tuple[list[dict], dict, str]:
    if domain not in RULE_VERSION:
        raise ValueError("Unknown decision-lab domain")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    filename = f"{domain}_examples.json"
    raw = (ROOT / filename).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest["file_sha256"][filename]:
        raise ValueError(f"Pinned {domain} example checksum mismatch")
    return json.loads(raw), manifest["sources"][domain], digest


def validate_parameters(domain: str, parameters: dict) -> dict:
    try:
        if domain == "ai4i":
            threshold = int(parameters.get("threshold", 40))
            minutes = int(parameters.get("review_minutes", 8))
            if not 0 <= threshold <= 100 or not 1 <= minutes <= 120:
                raise ValueError("维护阈值须为 0–100，单条复核分钟须为 1–120")
            return {"threshold": threshold, "review_minutes": minutes}
        if domain == "factorynet":
            threshold = float(parameters.get("threshold", 0.5))
            minutes = int(parameters.get("review_minutes", 30))
            if not math.isfinite(threshold) or not 0.01 <= threshold <= 20 or not 1 <= minutes <= 240:
                raise ValueError("偏差阈值须为 0.01–20，单 episode 复核分钟须为 1–240")
            return {"threshold": threshold, "review_minutes": minutes}
        if domain == "ibadas":
            policy = str(parameters.get("policy", "rgb_depth"))
            budget = float(parameters.get("budget_mb", 12))
            if policy not in POLICY_ROLES or not math.isfinite(budget) or not 1 <= budget <= 200:
                raise ValueError("证据策略须为 RGB、RGB+深度或四模态，预算须为 1–200 MB")
            return {"policy": policy, "budget_mb": budget}
    except (TypeError, OverflowError, ValueError) as exc:
        raise ValueError(f"分析参数无效：{exc}") from exc
    raise ValueError("Unknown decision-lab domain")


def _ai4i(rows: list[dict], params: dict) -> dict:
    output = []
    for item in rows:
        gap = item["process_temp_k"] - item["air_temp_k"]
        power = item["torque_nm"] * item["speed_rpm"] * 2 * math.pi / 60
        wear = min(40, item["tool_wear_min"] / 250 * 40)
        heat = max(0, 10 - gap) * 7
        power_outside = power < 3500 or power > 9000
        overload = item["tool_wear_min"] * item["torque_nm"] > 11000
        score = min(100, round(wear + heat + (20 if power_outside else 0) + (20 if overload else 0)))
        output.append({"object_id": f"decision-lab:ai4i:record:{item['source_row_id']}",
                       "source_row_id": item["source_row_id"], "name": item["product_id"],
                       "score": score, "review": score >= params["threshold"],
                       "official_failure": bool(item["machine_failure"]), "failure_modes": item["failure_modes"],
                       "details": {"wear_min": item["tool_wear_min"], "temp_gap_k": round(gap, 2),
                                   "power_w": round(power, 1), "power_outside": power_outside, "overload": overload}})
    review = sum(item["review"] for item in output)
    failures = sum(item["official_failure"] for item in output)
    covered = sum(item["review"] and item["official_failure"] for item in output)
    return {"metrics": {"review_count": review, "review_minutes": review * params["review_minutes"],
                        "observed_failures_covered": covered, "observed_failures_total": failures,
                        "observed_coverage_pct": round(100 * covered / failures, 1) if failures else 0},
            "rows": output}


def _factorynet(rows: list[dict], params: dict) -> dict:
    grouped: dict[str, list[dict]] = {}
    for item in rows:
        grouped.setdefault(item["episode_id"], []).append(item)
    output = []
    alerted_points = 0
    for episode, items in grouped.items():
        points = [{"time_s": item["time_s"], "source_row_id": item["source_row_id"],
                   "residual": round(abs(item["feedback_vel_0"] - item["setpoint_vel_0"]), 5)} for item in items]
        hits = sum(point["residual"] >= params["threshold"] for point in points)
        alerted_points += hits
        output.append({"object_id": f"decision-lab:factorynet:episode:{episode}", "source_row_id": items[0]["source_row_id"],
                       "name": episode, "review": hits > 0, "alert_points": hits, "sample_points": len(points),
                       "mean_residual": round(sum(point["residual"] for point in points) / len(points), 3),
                       "max_residual": round(max(point["residual"] for point in points), 3),
                       "tool_condition": items[0]["tool_condition"], "visual_inspection": items[0]["visual_inspection"],
                       "points": points})
    review = sum(item["review"] for item in output)
    failed = sum(item["visual_inspection"] == "no" for item in output)
    covered = sum(item["review"] and item["visual_inspection"] == "no" for item in output)
    return {"metrics": {"alert_points": alerted_points, "review_episodes": review,
                        "review_minutes": review * params["review_minutes"],
                        "observed_failed_inspections_covered": covered, "observed_failed_inspections_total": failed},
            "rows": output}


def _ibadas(rows: list[dict], params: dict) -> dict:
    roles = POLICY_ROLES[params["policy"]]
    budget_bytes = int(params["budget_mb"] * 1_000_000)
    consumed = 0
    output = []
    for item in sorted(rows, key=lambda row: hashlib.sha256(row["sample_key"].encode()).hexdigest()):
        grouped: dict[str, list[dict]] = {}
        for asset in item["assets"]:
            grouped.setdefault(asset["role"], []).append(asset)
        missing = [role for role in roles if role not in grouped]
        size = sum(asset["size"] for role in roles for asset in grouped.get(role, []))
        review = not missing and consumed + size <= budget_bytes
        if review:
            consumed += size
        output.append({"object_id": f"decision-lab:ibadas:sample:{item['sample_key']}",
                       "source_row_id": item["sample_key"], "name": item["sample_key"], "scene_id": item["scene_id"],
                       "official_label": item["label"], "review": review, "required_roles": list(roles),
                       "available_roles": sorted(grouped), "transfer_mb": round(size / 1_000_000, 3),
                       "skip_reason": ", ".join(missing) if missing else ("超出预算" if not review else None)})
    review = sum(item["review"] for item in output)
    anomalies = sum(item["official_label"] == "anomaly" for item in output)
    covered = sum(item["review"] and item["official_label"] == "anomaly" for item in output)
    return {"metrics": {"reviewed_samples": review, "total_samples": len(output),
                        "transfer_mb": round(consumed / 1_000_000, 3), "budget_mb": params["budget_mb"],
                        "evidence_roles": len(roles), "observed_anomalies_covered": covered,
                        "observed_anomalies_total": anomalies}, "rows": output}


CALCULATORS = {"ai4i": _ai4i, "factorynet": _factorynet, "ibadas": _ibadas}


def calculate(domain: str, parameters: dict) -> dict:
    params = validate_parameters(domain, parameters)
    rows, source, slice_digest = load_examples(domain)
    result = CALCULATORS[domain](rows, params)
    return {"domain": domain, "rule_version": RULE_VERSION[domain], "parameters": params,
            "source_digest": slice_digest, "source": source, "sample_count": len(rows),
            "formula": DESCRIPTIONS[domain]["formula"], "caveat": DESCRIPTIONS[domain]["caveat"],
            **result}
