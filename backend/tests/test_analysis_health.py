"""The data-health engine follows Great Expectations' semantics: what counts as unexpected, `mostly`, and the result layout."""
import pandas as pd
import pytest

from app.services.v2.analysis import health


@pytest.fixture
def table():
    return pd.DataFrame({
        "id": list(range(1, 11)),
        "name": ["a", "b", None, "d", "e", "f", "g", "h", "i", "j"],
        "age": [20, 25, 30, 35, 200, 45, 50, 55, 60, 65],
        "email": ["a@x.com", "bad", "c@x.com", "d@x.com", "e@x.com", "f@x.com", "g@x.com", "h@x.com", "i@x.com", "j@x.com"],
        "when": ["2020-01-01"] * 9 + ["nope"],
        "group": ["x", "x", "y", "y", "z", "z", "z", "w", "w", "w"],
        "start": [1] * 10,
        "end": [2] * 9 + [0],
    })


def one(table, kind, **kwargs):
    return health.run_expectation(table, {"expectation_type": kind, "kwargs": kwargs})


def test_not_null_counts_the_missing_value_and_mostly_tolerates_it(table):
    strict = one(table, "expect_column_values_to_not_be_null", column="name")
    assert strict["success"] is False
    assert strict["result"]["unexpected_count"] == 1 and strict["result"]["unexpected_percent"] == 10.0
    assert one(table, "expect_column_values_to_not_be_null", column="name", mostly=0.9)["success"] is True
    assert one(table, "expect_column_values_to_not_be_null", column="name", mostly=0.95)["success"] is False


def test_map_expectations_ignore_nulls_and_report_the_offending_values(table):
    result = one(table, "expect_column_values_to_be_between", column="age", min_value=0, max_value=120)
    assert result["success"] is False
    assert result["result"]["partial_unexpected_list"] == [200] and result["result"]["partial_unexpected_index_list"] == [4]
    # the null in `name` is not judged by a pattern expectation
    pattern = one(table, "expect_column_values_to_match_regex", column="name", regex="^[a-j]$")
    assert pattern["success"] is True and pattern["result"]["missing_count"] == 1


def test_strictness_of_range_bounds(table):
    assert one(table, "expect_column_values_to_be_between", column="start", min_value=1, max_value=1)["success"] is True
    assert one(table, "expect_column_values_to_be_between", column="start", min_value=1, max_value=5, strict_min=True)["success"] is False


def test_unique_reports_every_row_of_a_duplicated_value(table):
    result = one(table, "expect_column_values_to_be_unique", column="group")
    assert result["success"] is False and result["result"]["unexpected_count"] == 10
    assert one(table, "expect_column_values_to_be_unique", column="id")["success"] is True


def test_in_set_regex_lengths_and_dates(table):
    assert one(table, "expect_column_values_to_be_in_set", column="group", value_set=["x", "y", "z"])["result"]["unexpected_count"] == 3
    email = one(table, "expect_column_values_to_match_regex", column="email", regex=r"^[^@\s]+@[^@\s]+\.\w+$")
    assert email["result"]["partial_unexpected_list"] == ["bad"]
    assert one(table, "expect_column_value_lengths_to_be_between", column="email", min_value=5, max_value=10)["result"]["unexpected_count"] == 1
    parsed = one(table, "expect_column_values_to_be_dateutil_parseable", column="when")
    assert parsed["result"]["partial_unexpected_list"] == ["nope"]
    assert one(table, "expect_column_values_to_match_strftime_format", column="when", strftime_format="%Y-%m-%d")["result"]["unexpected_count"] == 1


def test_pair_and_compound_expectations(table):
    pair = one(table, "expect_column_pair_values_a_to_be_greater_than_b", column_A="end", column_B="start")
    assert pair["result"]["unexpected_count"] == 1
    # timestamps are compared as timestamps, not skipped because they are not numbers
    trips = pd.DataFrame({"pickup": ["2019-01-01 10:00:00", "2019-01-01 10:05:00", "2019-01-01 10:09:00", "garbled"],
                          "dropoff": ["2019-01-01 10:10:00", "2019-01-01 10:05:00", "2019-01-01 10:08:00", "2019-01-01 11:00:00"]})
    strict = one(trips, "expect_column_pair_values_a_to_be_greater_than_b", column_A="dropoff", column_B="pickup")
    assert strict["result"]["unexpected_count"] == 3 and strict["success"] is False  # same instant, earlier, and one that cannot be read
    relaxed = one(trips, "expect_column_pair_values_a_to_be_greater_than_b", column_A="dropoff", column_B="pickup", or_equal=True)
    assert relaxed["result"]["unexpected_count"] == 2
    assert one(table, "expect_compound_columns_to_be_unique", column_list=["group", "id"])["success"] is True
    assert one(table, "expect_compound_columns_to_be_unique", column_list=["group", "start"])["success"] is False


def test_type_and_aggregate_expectations(table):
    assert one(table, "expect_column_values_to_be_of_type", column="age", type_="int")["success"] is True
    assert one(table, "expect_column_values_to_be_of_type", column="email", type_="str")["success"] is True
    assert one(table, "expect_column_values_to_be_of_type", column="age", type_="str")["success"] is False
    mean = one(table, "expect_column_mean_to_be_between", column="age", min_value=40, max_value=60)
    assert mean["success"] is True and mean["result"]["observed_value"] == pytest.approx(58.5)
    assert one(table, "expect_column_max_to_be_between", column="age", max_value=100)["success"] is False
    assert one(table, "expect_column_unique_value_count_to_be_between", column="group", min_value=4, max_value=4)["success"] is True
    assert one(table, "expect_column_proportion_of_unique_values_to_be_between", column="id", min_value=1, max_value=1)["success"] is True
    assert one(table, "expect_column_most_common_value_to_be_in_set", column="group", value_set=["z", "w"])["success"] is True


def test_table_expectations(table):
    assert one(table, "expect_table_row_count_to_be_between", min_value=10, max_value=10)["success"] is True
    assert one(table, "expect_table_row_count_to_equal", value=9)["success"] is False
    assert one(table, "expect_table_columns_to_match_set", column_set=list(table.columns))["success"] is True
    assert one(table, "expect_table_columns_to_match_ordered_list", column_list=["id", "name"])["success"] is False
    assert one(table, "expect_column_to_exist", column="nope")["success"] is False


def test_a_wrong_expectation_is_reported_not_raised(table):
    unknown = one(table, "expect_something_else")
    assert unknown["success"] is False and "unknown expectation" in unknown["exception_info"]["exception_message"]
    missing = one(table, "expect_column_values_to_not_be_null", column="nope")
    assert "does not exist" in missing["exception_info"]["exception_message"]
    typo = one(table, "expect_column_values_to_not_be_null", column="name", mosly=0.9)
    assert "unknown argument" in typo["exception_info"]["exception_message"]
    bad_regex = one(table, "expect_column_values_to_match_regex", column="name", regex="(")
    assert "invalid regex" in bad_regex["exception_info"]["exception_message"]
    assert one(table, "expect_column_values_to_match_regex", column="name", regex="a" * 400)["exception_info"]["raised_exception"] is True
    # Great Expectations' own switches are accepted and ignored
    assert one(table, "expect_column_values_to_not_be_null", column="id", result_format="SUMMARY", catch_exceptions=True)["success"] is True


def test_validate_summarises_the_suite(table):
    suite = {"name": "demo", "expectations": [
        {"expectation_type": "expect_column_values_to_be_unique", "kwargs": {"column": "id"}},
        {"expectation_type": "expect_column_values_to_not_be_null", "kwargs": {"column": "name"}},
        {"expectation_type": "expect_column_values_to_be_between", "kwargs": {"column": "age", "min_value": 0, "max_value": 120, "mostly": 0.9}},
    ]}
    report = health.validate(table, suite, dataset_id="demo")
    assert report["success"] is False
    assert report["statistics"] == {"evaluated_expectations": 3, "successful_expectations": 2, "unsuccessful_expectations": 1, "errored_expectations": 0, "success_percent": 66.67}
    assert report["meta"]["row_count"] == 10 and report["meta"]["suite_name"] == "demo"
    with pytest.raises(health.ExpectationError):
        health.validate(table, {"expectations": []})


def test_suggested_suite_describes_the_table_and_passes_on_it():
    n = 60
    frame = pd.DataFrame({"id": range(n), "age": [20 + i % 40 for i in range(n)], "group": [("x", "y", "z")[i % 3] for i in range(n)],
                          "note": [None if i % 20 == 0 else "t" for i in range(n)]})
    suite = health.suggest_suite(frame)
    kinds = {(item["expectation_type"], item["kwargs"].get("column")): item for item in suite["expectations"]}
    assert ("expect_column_values_to_be_unique", "id") in kinds
    assert ("expect_column_values_to_be_in_set", "group") in kinds
    assert kinds[("expect_column_values_to_not_be_null", "note")]["kwargs"]["mostly"] == 0.93  # 5% empty, two points of slack
    report = health.validate(frame, suite)
    assert report["success"] is True, [r for r in report["results"] if not r["success"]]
    # the same suite catches a drifted batch
    assert health.validate(frame.assign(age=frame["age"] + 500), suite)["success"] is False


def test_compound_uniqueness_copes_with_missing_values():
    frame = pd.DataFrame({"first": ["ann", "ann", None, "bob", "bob"], "last": ["lee", "lee", "x", "ray", None], "dob": ["1990", "1990", "1990", "1980", "1980"]})
    result = one(frame, "expect_compound_columns_to_be_unique", column_list=["first", "last", "dob"])
    assert result["exception_info"]["raised_exception"] is False
    assert result["success"] is False and result["result"]["unexpected_count"] == 2  # the two ann/lee/1990 rows; rows with a missing part are not judged
    assert health.validate(frame, {"expectations": [{"expectation_type": "expect_compound_columns_to_be_unique", "kwargs": {"column_list": ["first", "last", "dob"]}}]})["statistics"]["errored_expectations"] == 0


def test_json_schema_check_judges_every_value_by_type_and_bounds():
    frame = pd.DataFrame({"count": [1, 2.0, None, 3.5, True, "7"], "name": ["a", "bb", None, "ccc", 4, "dddd"], "status": ["open", "closed", "open", "lost", None, "open"]})
    ints = one(frame, "expect_column_values_to_match_json_schema", column="count", json_schema={"type": "integer"})
    assert ints["result"]["partial_unexpected_list"] == [3.5, True, "7"] and ints["success"] is False
    assert one(frame, "expect_column_values_to_match_json_schema", column="count", json_schema={"type": "number", "minimum": 0, "maximum": 3})["result"]["unexpected_count"] == 3  # 3.5 is too large, True and "7" are not numbers
    strings = one(frame, "expect_column_values_to_match_json_schema", column="name", json_schema={"type": "string", "maxLength": 3})
    assert strings["result"]["partial_unexpected_list"] == [4, "dddd"]
    assert one(frame, "expect_column_values_to_match_json_schema", column="status", json_schema={"enum": ["open", "closed"]})["result"]["partial_unexpected_list"] == ["lost"]
    assert one(frame, "expect_column_values_to_match_json_schema", column="status", json_schema={"type": "string", "pattern": "^o"})["result"]["unexpected_count"] == 2
    assert one(frame, "expect_column_values_to_match_json_schema", column="count", json_schema={"type": "integer"}, mostly=0.4)["success"] is True
    for bad in ({"type": "bigint"}, {"format": "date"}, {"pattern": "a" * 400}):
        assert one(frame, "expect_column_values_to_match_json_schema", column="count", json_schema=bad)["exception_info"]["raised_exception"] is True
