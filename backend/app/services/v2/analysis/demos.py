"""Demo datasets bundled with the analysis engines, each taken from the open-source project the engine follows.

The files live in backend/examples/analysis/ with their origin, licence and SHA-256 recorded in README.md there and below. A file
whose hash differs from the recorded one is refused, so a result can always be traced to the exact data it was computed on.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

DEMO_DIR = Path(__file__).resolve().parents[4] / "examples" / "analysis"
PREVIEW_ROWS = 50

# id, which engine it is for, where it comes from. `sha256` is filled in when the file is added to the repository.
CATALOG: list[dict[str, Any]] = [
    {"id": "splink-fake-1000", "tool": "splink", "default": True, "title": "Splink 默认演示：fake_1000",
     "description": "1000 条虚构人员记录，来自 250 个人，同一个人的多条记录里有错拼、缺失和日期出入；带真实分组列 cluster，可以直接给结果打分。",
     "file": "fake_1000.csv", "format": "csv", "ground_truth": "cluster", "sha256": "66f9b8f92b9b082ef982fe18c77030fbed8780f06ce1d40d7106be5a72392e2a",
     "source": {"project": "Splink", "url": "https://github.com/moj-analytical-services/splink_datasets", "license": "MIT"}},
    {"id": "splink-historical-50k", "tool": "splink", "default": False, "title": "Splink 效果最好的演示：5 万条历史人物",
     "description": "5 万条带人为错误的历史人物记录（约 5000 个真实人物，来自 Wikidata），Splink 官方用它展示去重效果；带真实分组列 cluster。",
     "file": "historical_figures_with_errors_50k.parquet", "format": "parquet", "ground_truth": "cluster", "sha256": "b8b748402ff37b15c2d5e8a5b9374abe269b9d297aacb7f4c9498365b7cace43",
     "source": {"project": "Splink", "url": "https://github.com/moj-analytical-services/splink_datasets", "license": "MIT"}},
    {"id": "gx-taxi-2019-01", "tool": "great_expectations", "default": True, "title": "Great Expectations 默认演示：纽约出租车 2019-01",
     "description": "纽约出租车行程样例（约 1 万行），Great Expectations 入门教程用的数据；里面有乘客数为 0、金额为负等真实的脏数据。",
     "file": "yellow_tripdata_sample_2019-01.csv", "format": "csv", "ground_truth": None, "sha256": "8e9d0d319960bddc1d63b006e2de90f3d8eddd28a8cdcff6f53c1033e744df18",
     "source": {"project": "Great Expectations（gx_tutorials）", "url": "https://github.com/great-expectations/gx_tutorials", "license": "数据：NYC TLC 公开数据；仓库 Apache-2.0"}},
    {"id": "marquez-food-delivery", "tool": "marquez", "default": True, "title": "Marquez 默认演示：外卖业务血缘",
     "description": "Marquez 的 seed 数据：一家虚构外卖公司的 13 个作业、13 个数据集、26 条 OpenLineage 事件。",
     "file": "marquez_metadata.template.json", "format": "openlineage-events", "ground_truth": None, "sha256": "f40f076faec0305a3bc3c03e7ceeedce1cdecf7b7e58df94d84b3dae6dba0073",
     "source": {"project": "Marquez", "url": "https://github.com/MarquezProject/marquez/blob/main/docker/metadata.template.json", "license": "Apache-2.0"}},
]

# What each demo is run with. They follow what the source project's own documentation runs on the same data.
def _part(column: str, start: int, length: int) -> dict[str, Any]:
    return {"column": column, "substr": [start, length]}


# Splink's fake_1000 tutorial compares names, date of birth, city and email; the blocking rules here also cover rows whose names are
# missing or misspelt, which is what limits how many true pairs are ever compared.
FAKE_1000_SETTINGS: dict[str, Any] = {
    "unique_id": "unique_id", "ground_truth": "cluster", "threshold_match_probability": 0.5,
    "comparisons": [{"column": "first_name", "type": "name", "tf": True}, {"column": "surname", "type": "name", "tf": True},
                    {"column": "dob", "type": "levenshtein", "thresholds": [1, 2]}, {"column": "city", "type": "exact", "tf": True}, {"column": "email", "type": "email"}],
    "blocking_rules": [["first_name"], ["surname"], ["dob"], ["email"], [_part("first_name", 1, 3), _part("surname", 1, 3)], [_part("dob", 1, 4), "city"]],
    "training": {"deterministic_rules": [["first_name", "surname"]], "recall": 0.7, "em_blocking": [["dob"], ["email"]]},
    "display_columns": ["unique_id", "first_name", "surname", "dob", "city", "email"],
}

# Adapted from Splink's "linking a dataset of real historical persons" tutorial: the same fields, ten blocking rules, three deterministic
# rules for the prior, term-frequency adjustment on the fields where common values are weak evidence.
HISTORICAL_50K_SETTINGS: dict[str, Any] = {
    "unique_id": "unique_id", "ground_truth": "cluster", "threshold_match_probability": 0.9,
    "comparisons": [{"column": "first_name", "type": "name", "tf": True}, {"column": "surname", "type": "name", "tf": True},
                    {"column": "dob", "type": "levenshtein", "thresholds": [1, 2]}, {"column": "postcode_fake", "type": "levenshtein", "thresholds": [2]},
                    {"column": "birth_place", "type": "jaro_winkler", "thresholds": [0.9, 0.7], "tf": True}, {"column": "occupation", "type": "exact", "tf": True}],
    "blocking_rules": [[_part("first_name", 1, 3), _part("surname", 1, 4)], ["surname", "dob"], ["first_name", "dob"], ["postcode_fake", "first_name"], ["postcode_fake", "surname"],
                       ["dob", "birth_place"], [_part("postcode_fake", 1, 3), "dob"], [_part("postcode_fake", 1, 3), "first_name"], [_part("postcode_fake", 1, 3), "surname"],
                       [_part("first_name", 1, 2), _part("surname", 1, 2), _part("dob", 1, 4)]],
    "training": {"deterministic_rules": [["first_name", "surname", "dob"], ["surname", "dob", "postcode_fake"], ["first_name", "dob", "occupation"]], "recall": 0.7,
                 "em_blocking": [["first_name", "surname"], ["dob"]], "u_max_pairs": 2_000_000},
    "display_columns": ["unique_id", "full_name", "dob", "birth_place", "postcode_fake", "occupation"],
}
RESOLUTION_SETTINGS: dict[str, dict[str, Any]] = {"splink-fake-1000": FAKE_1000_SETTINGS, "splink-historical-50k": HISTORICAL_50K_SETTINGS}

FAKE_1000_SUITE: dict[str, Any] = {"name": "fake_1000_basic", "expectations": [
    {"expectation_type": "expect_table_row_count_to_equal", "kwargs": {"value": 1000}},
    {"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": "unique_id"}},
    {"expectation_type": "expect_column_values_to_be_unique", "kwargs": {"column": "unique_id"}},
    {"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": "first_name"}},
    {"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": "surname", "mostly": 0.95}},
    {"expectation_type": "expect_column_values_to_match_regex", "kwargs": {"column": "dob", "regex": r"^\d{4}-\d{2}-\d{2}$"}},
    {"expectation_type": "expect_column_values_to_be_dateutil_parseable", "kwargs": {"column": "dob"}},
    {"expectation_type": "expect_column_values_to_match_regex", "kwargs": {"column": "email", "regex": r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", "mostly": 0.9}},
    {"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": "city", "mostly": 0.9}},
    {"expectation_type": "expect_compound_columns_to_be_unique", "kwargs": {"column_list": ["first_name", "surname", "dob"]}, "meta": {"notes": "同名同生日的记录就是重复的候选"}},
]}
def _expect(kind: str, notes: str | None = None, **kwargs: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"expectation_type": f"expect_{kind}", "kwargs": kwargs}
    if notes:
        item["meta"] = {"notes": notes}
    return item


# What Great Expectations' getting-started tutorial checks on this file (non-null passenger_count, a sane range) plus the checks the data
# dictionary of the NYC TLC suggests. Several of them fail on purpose: the sample carries real errors.
TAXI_SUITE: dict[str, Any] = {"name": "taxi_2019_01", "expectations": [
    _expect("table_row_count_to_be_between", min_value=9000, max_value=11000),
    _expect("table_columns_to_match_ordered_list", column_list=["vendor_id", "pickup_datetime", "dropoff_datetime", "passenger_count", "trip_distance", "rate_code_id", "store_and_fwd_flag",
                                                                  "pickup_location_id", "dropoff_location_id", "payment_type", "fare_amount", "extra", "mta_tax", "tip_amount",
                                                                  "tolls_amount", "improvement_surcharge", "total_amount", "congestion_surcharge"]),
    _expect("column_values_to_not_be_null", column="pickup_datetime"),
    _expect("column_values_to_not_be_null", column="dropoff_datetime"),
    _expect("column_values_to_not_be_null", column="passenger_count"),
    _expect("column_values_to_be_between", "教程里的检查：乘客数 1 到 6", column="passenger_count", min_value=1, max_value=6),
    _expect("column_values_to_be_dateutil_parseable", column="pickup_datetime"),
    _expect("column_values_to_be_between", "样本取自 2019 年 1 月", column="pickup_datetime", min_value="2019-01-01", max_value="2019-01-31 23:59:59"),
    _expect("column_pair_values_a_to_be_greater_than_b", "下车时间不早于上车时间", column_A="dropoff_datetime", column_B="pickup_datetime", or_equal=True),
    _expect("column_pair_values_a_to_be_greater_than_b", "下车时间应晚于上车时间：有 8 条上下车同一秒", column_A="dropoff_datetime", column_B="pickup_datetime"),
    _expect("column_values_to_be_between", "里程为 0 的行程有 64 条（0.6%），允许 0.5% 以内", column="trip_distance", min_value=0, max_value=100, strict_min=True, mostly=0.995),
    _expect("column_mean_to_be_between", column="trip_distance", min_value=2, max_value=4),
    _expect("column_values_to_be_in_set", "TLC 数据字典：1 为 Creative Mobile，2 为 VeriFone；样本里还有 4", column="vendor_id", value_set=[1, 2]),
    _expect("column_values_to_be_in_set", "费率代码只有 1 到 6；样本里混进了一个 99", column="rate_code_id", value_set=[1, 2, 3, 4, 5, 6]),
    _expect("column_values_to_be_in_set", column="payment_type", value_set=[1, 2, 3, 4, 5, 6]),
    _expect("column_values_to_be_in_set", column="store_and_fwd_flag", value_set=["Y", "N"]),
    _expect("column_values_to_be_between", column="pickup_location_id", min_value=1, max_value=265),
    _expect("column_values_to_be_between", "车费不应为负，也不应高到离谱：有 7 条为负、2 条超过 500", column="fare_amount", min_value=0, max_value=500),
    _expect("column_values_to_be_between", column="total_amount", min_value=0, max_value=500),
    _expect("column_mean_to_be_between", column="fare_amount", min_value=10, max_value=15),
    _expect("column_values_to_be_in_set", "过路费附加税只有 0 和 0.5；样本里有 -0.5 和 37.51", column="mta_tax", value_set=[0, 0.5]),
    _expect("column_values_to_be_between", column="tip_amount", min_value=0, max_value=200),
    _expect("column_values_to_not_be_null", "2019 年才新增的字段，样本里 63% 为空", column="congestion_surcharge", mostly=0.9),
]}
HEALTH_SUITES: dict[str, dict[str, Any]] = {"splink-fake-1000": FAKE_1000_SUITE, "gx-taxi-2019-01": TAXI_SUITE}


class DemoError(ValueError):
    """The demo dataset is unknown, missing or no longer matches its recorded hash."""


def entry(dataset_id: str) -> dict[str, Any]:
    for item in CATALOG:
        if item["id"] == dataset_id:
            return item
    raise DemoError(f"unknown demo dataset '{dataset_id}'")


def path_of(item: dict[str, Any]) -> Path:
    return DEMO_DIR / item["file"]


_HASHES: dict[tuple[str, float, int], str] = {}


def sha256_of(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_mtime, stat.st_size)
    if key not in _HASHES:
        _HASHES[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _HASHES[key]


def _verified(item: dict[str, Any]) -> Path:
    path = path_of(item)
    if not path.exists():
        raise DemoError(f"the demo file {item['file']} has not been added to backend/examples/analysis/ yet")
    recorded = item.get("sha256")
    if recorded and sha256_of(path) != recorded:
        raise DemoError(f"{item['file']} does not match the recorded SHA-256; it was changed after it was added")
    return path


_FRAMES: dict[tuple[str, float, int], pd.DataFrame] = {}


def load_frame(dataset_id: str) -> pd.DataFrame:
    """The table behind a demo, read once per file version."""
    item = entry(dataset_id)
    if item["format"] not in ("csv", "parquet"):
        raise DemoError(f"'{dataset_id}' is not a table")
    path = _verified(item)
    stat = path.stat()
    key = (str(path), stat.st_mtime, stat.st_size)
    if key not in _FRAMES:
        _FRAMES.clear()
        _FRAMES[key] = pd.read_parquet(path) if item["format"] == "parquet" else pd.read_csv(path)
    return _FRAMES[key].copy()


def load_events_text(dataset_id: str) -> str:
    item = entry(dataset_id)
    if item["format"] != "openlineage-events":
        raise DemoError(f"'{dataset_id}' is not a lineage seed")
    return _verified(item).read_text(encoding="utf-8")


def health_suite(dataset_id: str) -> dict[str, Any]:
    """The suite a demo is run with; for a dataset without one, a baseline read off the data."""
    from app.services.v2.analysis import health

    if dataset_id in HEALTH_SUITES:
        return json.loads(json.dumps(HEALTH_SUITES[dataset_id]))
    return health.suggest_suite(load_frame(dataset_id), name=f"{dataset_id}_suggested")


def resolution_settings(dataset_id: str) -> dict[str, Any]:
    if dataset_id not in RESOLUTION_SETTINGS:
        raise DemoError(f"'{dataset_id}' has no entity-resolution preset")
    return json.loads(json.dumps(RESOLUTION_SETTINGS[dataset_id]))


def describe(item: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in item.items() if k not in ("sha256",)}
    path = path_of(item)
    out["available"] = path.exists()
    out["bytes"] = path.stat().st_size if path.exists() else None
    out["has_health_suite"] = item["id"] in HEALTH_SUITES or item["format"] in ("csv", "parquet")
    out["has_resolution_preset"] = item["id"] in RESOLUTION_SETTINGS
    if out["available"] and item["format"] in ("csv", "parquet"):
        try:
            frame = load_frame(item["id"])
            out["rows"], out["columns"] = int(len(frame)), [str(c) for c in frame.columns]
        except DemoError as error:
            out["available"], out["problem"] = False, str(error)
    return out


def list_demos() -> list[dict[str, Any]]:
    return [describe(item) for item in CATALOG]


def preview(dataset_id: str, limit: int = 20) -> dict[str, Any]:
    frame = load_frame(dataset_id)
    head = frame.head(max(1, min(limit, PREVIEW_ROWS)))
    cells = head.astype(object).where(head.notna(), None)
    return {"columns": [str(c) for c in frame.columns], "rows": cells.values.tolist(), "total": int(len(frame))}
