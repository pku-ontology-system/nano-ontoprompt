"""Entity resolution on catalogues: word overlap for names, codes, amounts, linking across sources, and the settings drafted for each kind of table."""
import duckdb
import pandas as pd
import pytest

from app.services.v2.analysis import ontology_resolution as drafter, resolution


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, list):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    return "'" + value.replace("'", "''") + "'" if isinstance(value, str) else repr(value)


def level(spec: dict, left: dict, right: dict) -> int:
    """The agreement level a comparison gives one pair of records."""
    comparison = resolution.compile_comparison(spec, set(left) - {"__tok0"}, 0)
    side = lambda alias, row: f"(SELECT {', '.join(f'{_literal(v)} AS \"{k}\"' for k, v in row.items())}) {alias}"  # noqa: E731
    return duckdb.sql(f"SELECT {comparison['case']} FROM {side('l', left)}, {side('r', right)}").fetchone()[0]


def catalogue(copies: int = 40) -> pd.DataFrame:
    """Two shops list the same products under different names; some products differ only by colour (a different model code)."""
    rows, uid = [], 0
    brands = ["acme", "borea", "cirrus", "delta"]
    kinds = ["turntable", "camera", "dryer", "monitor", "speaker"]
    for i in range(copies):
        brand, kind, code = brands[i % 4], kinds[i % 5], f"{brands[i % 4][:2]}{100 + i}x"
        price = 20.0 + i * 3
        rows.append({"unique_id": uid, "shop": "abt", "name": f"{brand.title()} {kind.title()} - {code.upper()}", "model": code, "price": price, "truth": i})
        uid += 1
        rows.append({"unique_id": uid, "shop": "buy", "name": f"{brand} {kind} deluxe edition ({code})", "model": code if i % 5 else None, "price": price * 1.03 if i % 3 else None, "truth": i})
        uid += 1
    # a colour variant in the same shop: a near-identical name and a model that differs in one character
    rows.append({"unique_id": uid, "shop": "buy", "name": "acme turntable deluxe edition (ac100y)", "model": "ac100y", "price": 20.0, "truth": 1000})
    return pd.DataFrame(rows)


def settings(**extra) -> dict:
    return {"unique_id": "unique_id", "ground_truth": "truth", "threshold_match_probability": 0.5,
            "comparisons": [{"column": "name", "type": "tokens"}, {"column": "model", "type": "code", "tf": True}, {"column": "price", "type": "ratio"}],
            "blocking_rules": [["model"], [{"column": "name", "substr": [1, 4]}]],
            "training": {"deterministic_rules": [["model"]], "recall": 0.7, "em_blocking": [["model"], [{"column": "name", "substr": [1, 4]}]]}, **extra}


def test_word_overlap_grades_names_that_share_most_of_their_words():
    spec = {"column": "name", "type": "tokens"}
    pair = lambda a, b: level(spec, {"name": " ".join(a), "__tok0": sorted(set(a))}, {"name": " ".join(b), "__tok0": sorted(set(b))})  # noqa: E731
    assert pair(["acme", "turntable", "x100"], ["acme", "turntable", "x100"]) == 4, "identical"
    assert pair(["acme", "turntable", "x100"], ["acme", "turntable", "deluxe", "x100"]) == 2, "three of four words are shared"
    assert pair(["acme", "turntable", "x100"], ["acme", "camera", "y200", "black", "kit"]) == 0
    assert level(spec, {"name": None, "__tok0": None}, {"name": "a", "__tok0": ["a"]}) == -1
    with pytest.raises(resolution.ResolutionError):
        resolution.compile_comparison({"column": "name", "type": "tokens", "thresholds": [1.5]}, {"name"})


def test_a_code_matches_up_to_punctuation_and_suffix_but_a_different_code_is_a_different_thing():
    spec = {"column": "model", "type": "code"}
    pair = lambda a, b: level(spec, {"model": a}, {"model": b})  # noqa: E731
    assert pair("DC-50W", "dc50w") == 2, "punctuation and case do not matter"
    assert pair("DC50W", "DC50WX") == 1, "one is the other with a suffix"
    assert pair("DC50W", "DC50B") == 0, "a code that differs by one character is another product, not a typo"
    assert pair("ab", "abc") == 0, "a short code is not a prefix of anything"
    assert pair(None, "dc50w") == -1


def test_amounts_are_graded_by_relative_difference():
    spec = {"column": "price", "type": "ratio"}
    pair = lambda a, b: level(spec, {"price": a}, {"price": b})  # noqa: E731
    assert [pair(100.0, b) for b in (100.0, 101.0, 108.0, 125.0, 200.0)] == [4, 3, 2, 1, 0]
    assert pair(None, 5.0) == -1
    with pytest.raises(resolution.ResolutionError):
        resolution.compile_comparison({"column": "price", "type": "ratio", "thresholds": [0]}, {"price"})


def test_linking_across_sources_pairs_only_records_from_different_sources():
    frame = catalogue()
    shown = {"display_columns": ["unique_id", "shop", "name"]}
    within = resolution.run(frame, settings(**shown))
    across = resolution.run(frame, settings(link_across="shop", **shown))
    assert across["settings"]["link_across"] == "shop"
    assert across["summary"]["total_possible_pairs"] < within["summary"]["total_possible_pairs"]
    assert across["summary"]["candidate_pairs"] < within["summary"]["candidate_pairs"]
    pairs = [p for kind in ("borderline", "confident", "false_positives", "false_negatives") for p in across["examples"][kind]]
    assert pairs and all(p["left"]["shop"] != p["right"]["shop"] for p in pairs), "a pair inside one shop was compared"


def test_the_sibling_in_the_same_shop_is_not_merged_when_linking_across_sources():
    frame = catalogue()
    out = resolution.run(frame, settings(link_across="shop", display_columns=["unique_id", "name", "shop"]))
    sibling = int(frame["unique_id"].iloc[-1])
    members = [c for c in out["clusters"] if any(r.get("unique_id") == sibling for r in c["records"])]
    assert not members or all(c["true_entities_inside"] == 1 for c in members), "a colour variant is not the same product as its sibling"
    assert out["evaluation"]["precision"] >= 0.9 and out["evaluation"]["recall"] >= 0.7, out["evaluation"]


def test_the_link_column_must_exist():
    with pytest.raises(resolution.ResolutionError):
        resolution.run(catalogue(), settings(link_across="nope"))


# ---- the drafted settings --------------------------------------------------------------------------------------------

def description(**types) -> dict:
    return {"api_name": "Product", "properties": [{"api_name": name, "data_type": kind, "nullable": True, "is_array": False} for name, kind in types.items()]}


def products() -> tuple[pd.DataFrame, dict]:
    frame = catalogue().rename(columns={"shop": "catalog_id", "model": "model_no"})
    frame["__object_id"] = frame["unique_id"].map(lambda i: f"o:{i}")
    frame["brand_id"] = frame["name"].str.split().str[0].str.lower()
    frame["maker"] = frame["name"].where(frame["catalog_id"] == "buy").str.split().str[0]  # only one shop has it
    return frame, description(unique_id="integer", catalog_id="string", name="string", model_no="string", price="double", brand_id="string", maker="string", truth="string")


def test_the_draft_picks_words_for_names_codes_for_models_and_amounts_for_prices():
    frame, desc = products()
    drafted = drafter.suggest(frame, desc, references=["brand_id"])
    kinds = {c["column"]: c["type"] for c in drafted["settings"]["comparisons"]}
    assert kinds == {"name": "tokens", "model_no": "code", "price": "ratio"}
    assert drafted["settings"]["link_across"] == "catalog_id", "a column called catalog with two values says which source a record came from"
    assert drafted["settings"]["training"]["deterministic_rules"] == [["model_no"]], "models agree rarely enough, and surely enough, to read as the same"


def test_foreign_keys_and_thin_columns_are_left_out_and_the_reason_is_given():
    frame, desc = products()
    skipped = {s["column"]: s["reason"] for s in drafter.suggest(frame, desc, references=["brand_id"])["skipped"]}
    assert "外键" in skipped["brand_id"], "a foreign key says which other object this one belongs to; it is not evidence of being the same"
    assert "两边都有值" in skipped["maker"], "a value only one shop records cannot be compared across shops"
    assert "两个取值" in skipped["catalog_id"] and "不用来比较" in skipped["unique_id"]


def test_a_type_without_anything_that_tells_individuals_apart_is_not_resolvable():
    frame = pd.DataFrame({"__object_id": [f"o:{i}" for i in range(60)], "shipped": [f"2024-01-{1 + i % 28:02d}" for i in range(60)], "status": ["F", "O", "P"] * 20, "total": [10.0 + i for i in range(60)]})
    desc = description(shipped="date", status="string", total="double")
    with pytest.raises(resolution.NotApplicable) as caught:
        drafter.suggest(frame, desc)
    assert "不适用" in str(caught.value) and isinstance(caught.value, resolution.ResolutionError)


def test_the_drafted_settings_run_and_score_well_on_the_catalogue():
    frame, desc = products()
    drafted = drafter.suggest(frame, desc, references=["brand_id"])
    out = drafter.run(frame, drafted["settings"])
    assert out["evaluation"]["precision"] >= 0.9 and out["evaluation"]["recall"] >= 0.7, out["evaluation"]
    assert out["summary"]["run_time_ms"] < 20_000
