"""Data health: declarative expectations over a table.

Modelled on Great Expectations (Apache-2.0, https://github.com/great-expectations/great_expectations). Expectation names,
the `mostly` tolerance and the layout of each result follow its documented behaviour, so a suite written for it reads the
same here. Only the pandas execution is reimplemented; row conditions, result-format switches and data-source plumbing
are left out on purpose.

A suite is `{"name": str, "expectations": [{"expectation_type": str, "kwargs": {...}, "meta": {...}}]}`.
"""
from __future__ import annotations

import inspect
import math
import re
import time
from typing import Any, Callable

import numpy as np
import pandas as pd

PARTIAL = 20  # how many unexpected values a result carries, as in Great Expectations' summary format
MAX_REGEX = 300  # patterns come from a user-edited suite and Python's re has no timeout
IGNORED_KWARGS = {"result_format", "catch_exceptions", "include_config", "meta", "notes"}


class ExpectationError(ValueError):
    """The expectation itself is wrong (unknown type, missing column, bad argument), not the data."""


def _py(value: Any) -> Any:
    """JSON-friendly scalar."""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if (isinstance(value, float) and math.isnan(value)) or (isinstance(value, np.floating) and np.isnan(value)) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if value is pd.NaT:
        return None
    return value


def _pct(part: int, whole: int) -> float:
    return 0.0 if whole == 0 else round(100.0 * part / whole, 4)


def _column(df: pd.DataFrame, name: str) -> pd.Series:
    if name not in df.columns:
        raise ExpectationError(f"column '{name}' does not exist")
    return df[name]


def _mostly(value: Any) -> float:
    if value is None:
        return 1.0
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
        raise ExpectationError("mostly must be a number between 0 and 1")
    return float(value)


def _map_result(series: pd.Series, bad: pd.Series, mostly: float, *, missing_is_unexpected: bool = False) -> dict:
    """Result of an expectation that judges every value. `bad` is True where a value fails; nulls are judged separately."""
    element_count = int(len(series))
    missing = series.isna()
    missing_count = int(missing.sum())
    nonmissing = element_count - missing_count
    unexpected = missing if missing_is_unexpected else (bad.fillna(False).astype(bool) & ~missing)
    unexpected_count = int(unexpected.sum())
    denominator = element_count if missing_is_unexpected else nonmissing
    fraction_ok = 1.0 if denominator == 0 else 1.0 - unexpected_count / denominator
    values = series[unexpected]
    head = values.head(PARTIAL)
    return {
        "success": bool(fraction_ok >= mostly - 1e-12),
        "result": {
            "element_count": element_count,
            "unexpected_count": unexpected_count,
            "unexpected_percent": _pct(unexpected_count, denominator),
            "unexpected_percent_total": _pct(unexpected_count, element_count),
            "unexpected_percent_nonmissing": _pct(unexpected_count, nonmissing),
            "missing_count": missing_count,
            "missing_percent": _pct(missing_count, element_count),
            "partial_unexpected_list": [_py(v) for v in head.tolist()],
            "partial_unexpected_index_list": [int(i) for i in head.index],
            "partial_unexpected_counts": [{"value": _py(v), "count": int(c)} for v, c in values.value_counts(dropna=False).head(PARTIAL).items()],
        },
    }


def _aggregate(observed: Any, success: bool) -> dict:
    return {"success": bool(success), "result": {"observed_value": _py(observed)}}


def _within(value: float | None, low: float | None, high: float | None, strict_min: bool = False, strict_max: bool = False) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    if low is not None and (value <= low if strict_min else value < low):
        return False
    if high is not None and (value >= high if strict_max else value > high):
        return False
    return True


def _is_dateish(*bounds: Any) -> bool:
    return any(isinstance(b, str) for b in bounds)


def _compare_range(series: pd.Series, min_value: Any, max_value: Any, strict_min: bool, strict_max: bool) -> pd.Series:
    """True where a value is outside the range; values that cannot be read as numbers (or dates) are outside as well."""
    if min_value is None and max_value is None:
        raise ExpectationError("min_value or max_value is required")
    if _is_dateish(min_value, max_value):
        values = pd.to_datetime(series, errors="coerce", format="mixed")
        low = pd.to_datetime(min_value) if min_value is not None else None
        high = pd.to_datetime(max_value) if max_value is not None else None
    else:
        values = pd.to_numeric(series, errors="coerce")
        low, high = min_value, max_value
    bad = pd.Series(False, index=series.index)
    if low is not None:
        bad |= (values <= low) if strict_min else (values < low)
    if high is not None:
        bad |= (values >= high) if strict_max else (values > high)
    bad |= values.isna() & series.notna()
    return bad


# ---- table level ------------------------------------------------------------------------------------------------

def expect_table_row_count_to_be_between(df, min_value=None, max_value=None):
    return _aggregate(len(df), _within(len(df), min_value, max_value))


def expect_table_row_count_to_equal(df, value):
    return _aggregate(len(df), len(df) == value)


def expect_table_columns_to_match_ordered_list(df, column_list):
    observed = list(df.columns)
    return {"success": observed == list(column_list), "result": {"observed_value": observed}}


def expect_table_columns_to_match_set(df, column_set, exact_match=True):
    observed = set(df.columns)
    wanted = set(column_set)
    return {"success": observed == wanted if exact_match else wanted <= observed, "result": {"observed_value": sorted(observed)}}


def expect_column_to_exist(df, column):
    return {"success": column in df.columns, "result": {}}


# ---- column, value by value ------------------------------------------------------------------------------------------

def expect_column_values_to_not_be_null(df, column, mostly=None):
    series = _column(df, column)
    return _map_result(series, series.isna(), _mostly(mostly), missing_is_unexpected=True)


def expect_column_values_to_be_null(df, column, mostly=None):
    series = _column(df, column)
    return _map_result(series, series.notna(), _mostly(mostly))


def expect_column_values_to_be_unique(df, column, mostly=None):
    series = _column(df, column)
    return _map_result(series, series.duplicated(keep=False), _mostly(mostly))


def expect_column_values_to_be_between(df, column, min_value=None, max_value=None, strict_min=False, strict_max=False, mostly=None):
    series = _column(df, column)
    return _map_result(series, _compare_range(series, min_value, max_value, strict_min, strict_max), _mostly(mostly))


def expect_column_values_to_be_in_set(df, column, value_set, mostly=None):
    series = _column(df, column)
    return _map_result(series, ~series.isin(list(value_set)), _mostly(mostly))


def expect_column_values_to_not_be_in_set(df, column, value_set, mostly=None):
    series = _column(df, column)
    return _map_result(series, series.isin(list(value_set)), _mostly(mostly))


def _pattern(regex: str) -> re.Pattern:
    if not isinstance(regex, str) or not regex or len(regex) > MAX_REGEX:
        raise ExpectationError(f"regex must be a non-empty string of at most {MAX_REGEX} characters")
    try:
        return re.compile(regex)
    except re.error as error:
        raise ExpectationError(f"invalid regex: {error}") from error


def expect_column_values_to_match_regex(df, column, regex, mostly=None):
    series = _column(df, column)
    pattern = _pattern(regex)
    bad = series.map(lambda v: False if pd.isna(v) else pattern.search(str(v)) is None)
    return _map_result(series, bad, _mostly(mostly))


def expect_column_values_to_not_match_regex(df, column, regex, mostly=None):
    series = _column(df, column)
    pattern = _pattern(regex)
    bad = series.map(lambda v: False if pd.isna(v) else pattern.search(str(v)) is not None)
    return _map_result(series, bad, _mostly(mostly))


def expect_column_value_lengths_to_be_between(df, column, min_value=None, max_value=None, mostly=None):
    series = _column(df, column)
    lengths = series.map(lambda v: None if pd.isna(v) else len(str(v)))
    bad = pd.Series([False if pd.isna(n) else not _within(n, min_value, max_value) for n in lengths], index=series.index)
    return _map_result(series, bad, _mostly(mostly))


def expect_column_values_to_be_dateutil_parseable(df, column, mostly=None):
    series = _column(df, column)
    parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    return _map_result(series, parsed.isna(), _mostly(mostly))


def expect_column_values_to_match_strftime_format(df, column, strftime_format, mostly=None):
    series = _column(df, column)
    parsed = pd.to_datetime(series.astype("string"), errors="coerce", format=strftime_format, exact=True)
    return _map_result(series, parsed.isna(), _mostly(mostly))


def _orderable(a: pd.Series, b: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Both columns as numbers when every value is one, otherwise as timestamps; a value that is neither stays missing."""
    left, right = pd.to_numeric(a, errors="coerce"), pd.to_numeric(b, errors="coerce")
    if (left.notna() | a.isna()).all() and (right.notna() | b.isna()).all():
        return left, right
    return pd.to_datetime(a, errors="coerce", format="mixed"), pd.to_datetime(b, errors="coerce", format="mixed")


def expect_column_pair_values_a_to_be_greater_than_b(df, column_A, column_B, or_equal=False, mostly=None):
    a, b = _column(df, column_A), _column(df, column_B)
    left, right = _orderable(a, b)
    both = left.notna() & right.notna()
    bad = (left < right) if or_equal else (left <= right)
    bad = (bad & both) | ((a.notna() & left.isna()) | (b.notna() & right.isna()))  # a value that cannot be read at all is unexpected too
    present = a.notna() & b.notna()
    shown = pd.Series([f"{x} / {y}" if m else None for x, y, m in zip(a, b, present)], index=a.index)
    return _map_result(shown, bad & present, _mostly(mostly))


def expect_compound_columns_to_be_unique(df, column_list, mostly=None):
    columns = [_column(df, c) for c in column_list]
    complete = pd.concat(columns, axis=1).notna().all(axis=1)
    duplicated = df.duplicated(subset=list(column_list), keep=False)
    shown = columns[0].astype("string")
    for column in columns[1:]:
        shown = shown + " | " + column.astype("string")  # missing stays missing, so incomplete rows are not judged
    return _map_result(shown.where(complete), duplicated & complete, _mostly(mostly))


_TYPE_CHECKS: dict[str, Callable[[pd.Series], bool]] = {
    "int": pd.api.types.is_integer_dtype, "integer": pd.api.types.is_integer_dtype, "int64": pd.api.types.is_integer_dtype,
    "float": pd.api.types.is_float_dtype, "float64": pd.api.types.is_float_dtype,
    "number": pd.api.types.is_numeric_dtype, "numeric": pd.api.types.is_numeric_dtype,
    "bool": pd.api.types.is_bool_dtype, "boolean": pd.api.types.is_bool_dtype,
    "datetime": pd.api.types.is_datetime64_any_dtype, "datetime64": pd.api.types.is_datetime64_any_dtype,
    "str": lambda s: pd.api.types.is_string_dtype(s) and not pd.api.types.is_bool_dtype(s),
    "string": lambda s: pd.api.types.is_string_dtype(s) and not pd.api.types.is_bool_dtype(s),
    "object": lambda s: pd.api.types.is_string_dtype(s) or pd.api.types.is_object_dtype(s),
}


def expect_column_values_to_be_of_type(df, column, type_):
    series = _column(df, column)
    check = _TYPE_CHECKS.get(str(type_).lower())
    if check is None:
        raise ExpectationError(f"unknown type '{type_}'; use one of {sorted(_TYPE_CHECKS)}")
    return {"success": bool(check(series)), "result": {"observed_value": str(series.dtype)}}


# ---- column, one number for the whole column --------------------------------------------------------------------------------

def _numeric(df, column) -> pd.Series:
    return pd.to_numeric(_column(df, column), errors="coerce").dropna()


def _stat(name: str, fn: Callable[[pd.Series], Any]):
    def expectation(df, column, min_value=None, max_value=None, strict_min=False, strict_max=False):
        values = _numeric(df, column)
        observed = fn(values) if len(values) else None
        return _aggregate(observed, _within(None if observed is None else float(observed), min_value, max_value, strict_min, strict_max))
    expectation.__name__ = f"expect_column_{name}_to_be_between"
    return expectation


expect_column_mean_to_be_between = _stat("mean", lambda s: s.mean())
expect_column_median_to_be_between = _stat("median", lambda s: s.median())
expect_column_min_to_be_between = _stat("min", lambda s: s.min())
expect_column_max_to_be_between = _stat("max", lambda s: s.max())
expect_column_sum_to_be_between = _stat("sum", lambda s: s.sum())
expect_column_stdev_to_be_between = _stat("stdev", lambda s: s.std())


def expect_column_unique_value_count_to_be_between(df, column, min_value=None, max_value=None):
    count = int(_column(df, column).nunique(dropna=True))
    return _aggregate(count, _within(count, min_value, max_value))


def expect_column_proportion_of_unique_values_to_be_between(df, column, min_value=None, max_value=None):
    series = _column(df, column).dropna()
    share = float(series.nunique() / len(series)) if len(series) else 0.0
    return _aggregate(round(share, 6), _within(share, min_value, max_value))


def expect_column_distinct_values_to_be_in_set(df, column, value_set):
    distinct = _column(df, column).dropna().unique().tolist()
    stray = [v for v in distinct if v not in set(value_set)]
    return {"success": not stray, "result": {"observed_value": [_py(v) for v in distinct[:PARTIAL]], "unexpected": [_py(v) for v in stray[:PARTIAL]]}}


def expect_column_most_common_value_to_be_in_set(df, column, value_set):
    counts = _column(df, column).dropna().value_counts()
    top = counts.index[0] if len(counts) else None
    return _aggregate(top, top is not None and top in set(value_set))


EXPECTATIONS: dict[str, Callable[..., dict]] = {
    name: fn for name, fn in sorted(globals().items()) if name.startswith("expect_") and callable(fn)
}

LABELS = {  # short Chinese names for the report; the type names themselves stay as in Great Expectations
    "expect_column_values_to_not_be_null": "不为空", "expect_column_values_to_be_null": "必须为空", "expect_column_values_to_be_unique": "取值唯一",
    "expect_column_values_to_be_between": "取值在范围内", "expect_column_values_to_be_in_set": "取值在给定集合内", "expect_column_values_to_not_be_in_set": "取值不在给定集合内",
    "expect_column_values_to_match_regex": "符合正则", "expect_column_values_to_not_match_regex": "不符合正则", "expect_column_value_lengths_to_be_between": "长度在范围内",
    "expect_column_values_to_be_dateutil_parseable": "可解析为日期", "expect_column_values_to_match_strftime_format": "符合日期格式",
    "expect_column_pair_values_a_to_be_greater_than_b": "A 列大于 B 列", "expect_compound_columns_to_be_unique": "组合列唯一", "expect_column_values_to_be_of_type": "字段类型",
    "expect_column_mean_to_be_between": "均值在范围内", "expect_column_median_to_be_between": "中位数在范围内", "expect_column_min_to_be_between": "最小值在范围内",
    "expect_column_max_to_be_between": "最大值在范围内", "expect_column_sum_to_be_between": "合计在范围内", "expect_column_stdev_to_be_between": "标准差在范围内",
    "expect_column_unique_value_count_to_be_between": "不同取值个数在范围内", "expect_column_proportion_of_unique_values_to_be_between": "不同取值占比在范围内",
    "expect_column_distinct_values_to_be_in_set": "出现过的取值都在集合内", "expect_column_most_common_value_to_be_in_set": "最常见取值在集合内",
    "expect_table_row_count_to_be_between": "行数在范围内", "expect_table_row_count_to_equal": "行数等于", "expect_table_columns_to_match_ordered_list": "列名与顺序一致",
    "expect_table_columns_to_match_set": "列名集合一致", "expect_column_to_exist": "列存在",
}


def _check_kwargs(fn: Callable, kwargs: dict) -> dict:
    accepted = set(inspect.signature(fn).parameters) - {"df"}
    unknown = set(kwargs) - accepted - IGNORED_KWARGS
    if unknown:
        raise ExpectationError(f"unknown argument(s) {sorted(unknown)}; accepted: {sorted(accepted)}")
    return {k: v for k, v in kwargs.items() if k in accepted}


def run_expectation(df: pd.DataFrame, expectation: dict) -> dict:
    """One expectation against a table. A mistake in the expectation is reported in `exception_info`, never raised."""
    kind = expectation.get("expectation_type")
    kwargs = dict(expectation.get("kwargs") or {})
    entry = {"expectation_type": kind, "label": LABELS.get(kind, kind), "kwargs": kwargs, "meta": expectation.get("meta") or {}}
    fn = EXPECTATIONS.get(kind)
    try:
        if fn is None:
            raise ExpectationError(f"unknown expectation '{kind}'")
        outcome = fn(df, **_check_kwargs(fn, kwargs))
    except (ExpectationError, TypeError, ValueError) as error:
        return {**entry, "success": False, "result": {}, "exception_info": {"raised_exception": True, "exception_message": str(error)}}
    return {**entry, "success": bool(outcome["success"]), "result": outcome["result"], "exception_info": {"raised_exception": False}}


def validate(df: pd.DataFrame, suite: dict, *, dataset_id: str = "") -> dict:
    """Run a whole suite; the layout mirrors a Great Expectations validation result."""
    started = time.perf_counter()
    expectations = suite.get("expectations")
    if not isinstance(expectations, list) or not expectations:
        raise ExpectationError("a suite needs a non-empty 'expectations' list")
    if len(expectations) > 200:
        raise ExpectationError("a suite can hold at most 200 expectations")
    results = [run_expectation(df, item) for item in expectations]
    ok = sum(1 for r in results if r["success"])
    errored = sum(1 for r in results if r["exception_info"]["raised_exception"])
    return {
        "success": ok == len(results),
        "statistics": {"evaluated_expectations": len(results), "successful_expectations": ok, "unsuccessful_expectations": len(results) - ok,
                       "errored_expectations": errored, "success_percent": round(100.0 * ok / len(results), 2)},
        "results": results,
        "meta": {"suite_name": suite.get("name") or "suite", "dataset_id": dataset_id, "row_count": int(len(df)), "column_count": int(df.shape[1]),
                 "run_time_ms": int((time.perf_counter() - started) * 1000)},
    }


def suggest_suite(df: pd.DataFrame, *, name: str = "suggested", max_set: int = 12) -> dict:
    """A baseline suite read off the data itself: what is never null, what is unique, ranges, small value sets.

    It describes this table as it is, so it is useful for spotting drift in the next batch rather than for judging this one.
    """
    rows = len(df)
    items: list[dict] = [{"expectation_type": "expect_table_row_count_to_be_between", "kwargs": {"min_value": max(1, rows // 2), "max_value": rows * 2},
                          "meta": {"notes": "行数不应相差一倍以上"}}]
    for column in df.columns:
        series = df[column]
        nulls = int(series.isna().sum())
        share = nulls / rows if rows else 0.0
        if nulls == 0:
            items.append({"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": column}})
        elif share <= 0.1:
            items.append({"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": column, "mostly": round(max(0.0, 1 - share - 0.02), 2)},
                          "meta": {"notes": f"现有数据中 {share:.1%} 为空，留 2 个百分点余量"}})
        values = series.dropna()
        if len(values) >= 20 and values.nunique() == len(values) and not pd.api.types.is_float_dtype(series):
            items.append({"expectation_type": "expect_column_values_to_be_unique", "kwargs": {"column": column}})
        if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series) and len(values):
            items.append({"expectation_type": "expect_column_values_to_be_between", "kwargs": {"column": column, "min_value": _py(values.min()), "max_value": _py(values.max())},
                          "meta": {"notes": "取现有数据的最小值与最大值"}})
        elif len(values) >= 50 and 1 < values.nunique() <= max_set:
            items.append({"expectation_type": "expect_column_values_to_be_in_set", "kwargs": {"column": column, "value_set": sorted(_py(v) for v in values.unique())}})
    return {"name": name, "expectations": items}
