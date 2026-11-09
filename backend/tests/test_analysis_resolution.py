"""Entity resolution: the Fellegi-Sunter pipeline finds the duplicates in a noisy table and explains its decisions."""
import random

import pandas as pd
import pytest

from app.services.v2.analysis import resolution

FIRST = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi", "ivan", "judy", "kevin", "laura", "mallory", "nina", "oscar", "peggy", "quinn", "rupert", "sybil", "trent"]
LAST = ["smith", "jones", "brown", "taylor", "wilson", "davies", "evans", "thomas", "roberts", "johnson", "walker", "wright", "robinson", "thompson", "white", "hughes", "edwards", "green", "hall", "wood"]
CITIES = ["london", "leeds", "bath", "york", "derby", "hull", "wigan", "luton"]


def typo(text, rng):
    if len(text) < 4:
        return text
    i = rng.randrange(1, len(text) - 1)
    return text[:i] + text[i + 1:]  # drop one letter


def noisy_people(seed=7, people=150):
    rng = random.Random(seed)
    rows, uid = [], 0
    for cluster in range(people):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        dob = f"19{rng.randrange(50, 99)}-{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d}"
        city = rng.choice(CITIES)
        for _ in range(rng.choice([1, 2, 2, 3])):
            f, s, d, c = first, last, dob, city
            if rng.random() < 0.25:
                f = typo(f, rng)
            if rng.random() < 0.25:
                s = typo(s, rng)
            if rng.random() < 0.12:
                d = d[:-1] + str((int(d[-1]) + 1) % 10)
            mail = f"{first}.{last}{cluster}@example.com" if rng.random() > 0.15 else None
            rows.append({"unique_id": uid, "first_name": f, "surname": s, "dob": d, "city": c if rng.random() > 0.1 else None, "email": mail, "cluster": cluster})
            uid += 1
    return pd.DataFrame(rows)


SETTINGS = {
    "unique_id": "unique_id", "ground_truth": "cluster", "threshold_match_probability": 0.9,
    "comparisons": [{"column": "first_name", "type": "name"}, {"column": "surname", "type": "name"}, {"column": "dob", "type": "levenshtein", "thresholds": [1, 2]},
                    {"column": "city", "type": "exact"}, {"column": "email", "type": "email"}],
    "blocking_rules": [["first_name"], ["surname"], ["dob"]],
    "training": {"deterministic_rule": ["first_name", "surname"], "recall": 0.7, "em_blocking": [["dob"], ["email"]]},
}


@pytest.fixture(scope="module")
def result():
    return resolution.run(noisy_people(), SETTINGS)


def test_finds_nearly_all_duplicates_without_inventing_many(result):
    evaluation = result["evaluation"]
    assert evaluation["precision"] >= 0.9, evaluation
    assert evaluation["recall"] >= 0.85, evaluation
    assert evaluation["blocking_recall"] >= 0.9
    summary = result["summary"]
    assert summary["entities_found"] < summary["records"]
    assert summary["candidate_pairs"] < summary["total_possible_pairs"] / 4  # blocking did its job


def test_learned_weights_reward_agreement_and_punish_disagreement(result):
    for comparison in result["model"]["comparisons"]:
        levels = [x for x in comparison["levels"] if x["weight"] is not None]
        top, bottom = levels[-1], levels[0]
        # a level that no true match ever lands on can get a very low weight, so only the two ends are ordered
        assert top["weight"] == max(x["weight"] for x in levels), comparison
        assert top["weight"] > 0 > bottom["weight"], comparison
        assert all(0 < x["m"] <= 1 and 0 < x["u"] <= 1 for x in levels)
    assert 0 < result["model"]["lambda"] < 0.5


def test_missing_values_carry_no_weight(result):
    levels = next(c for c in result["model"]["comparisons"] if c["column"] == "email")["levels"]
    missing = next(x for x in levels if x["level"] == -1)
    assert missing["weight"] is None


def test_pairs_are_explained_field_by_field(result):
    example = result["examples"]["confident"][0]
    assert example["probability"] > 0.99 and example["same_entity"] is True
    assert example["contributions"][0]["column"] == "先验"
    total = sum(x["weight"] for x in example["contributions"])
    assert total == pytest.approx(example["weight"], abs=0.01)
    assert set(result["examples"]) == {"borderline", "false_positives", "false_negatives", "confident"}


def test_clusters_and_histogram_are_reported(result):
    assert result["clusters"] and result["clusters"][0]["size"] >= result["clusters"][-1]["size"] >= 2
    histogram = result["histogram"]
    assert len(histogram["match"]) == len(histogram["non_match"]) == len(histogram["bin_start"])
    assert sum(histogram["match"]) > 0 and sum(histogram["non_match"]) > 0


def test_the_run_is_repeatable():
    first = resolution.run(noisy_people(), SETTINGS)
    second = resolution.run(noisy_people(), SETTINGS)
    assert first["evaluation"] == second["evaluation"]
    assert first["model"]["lambda"] == second["model"]["lambda"]


def test_without_ground_truth_there_is_no_evaluation():
    settings = {k: v for k, v in SETTINGS.items() if k != "ground_truth"}
    out = resolution.run(noisy_people(people=40), settings)
    assert out["evaluation"] is None and "all" in out["histogram"]


def test_settings_are_checked_before_anything_runs():
    frame = noisy_people(people=10)
    with pytest.raises(resolution.ResolutionError, match="does not exist"):
        resolution.run(frame, {**SETTINGS, "comparisons": [{"column": "nope", "type": "exact"}]})
    with pytest.raises(resolution.ResolutionError, match="unknown comparison type"):
        resolution.run(frame, {**SETTINGS, "comparisons": [{"column": "city", "type": "sql"}]})
    with pytest.raises(resolution.ResolutionError, match="blocking rule"):
        resolution.run(frame, {**SETTINGS, "blocking_rules": [[]]})
    with pytest.raises(resolution.ResolutionError, match="at least one blocking rule"):
        resolution.run(frame, {**SETTINGS, "blocking_rules": []})
    with pytest.raises(resolution.ResolutionError, match="unique"):
        resolution.run(pd.concat([frame, frame.head(1)]), SETTINGS)
    injected = {**SETTINGS, "blocking_rules": [["first_name\" = '' OR 1=1 --"]]}
    with pytest.raises(resolution.ResolutionError, match="does not exist"):
        resolution.run(frame, injected)


def test_blocking_terms_can_use_part_of_a_value():
    frame = noisy_people(people=60)
    settings = {**SETTINGS, "blocking_rules": [[{"column": "surname", "substr": [1, 3]}], [{"column": "first_name", "substr": [-3, 3]}]],
                "training": {"deterministic_rule": ["first_name", "surname"], "em_blocking": [[{"column": "dob", "substr": [1, 4]}]]}}
    out = resolution.run(frame, settings)
    assert out["summary"]["candidate_pairs"] > 0
    with pytest.raises(resolution.ResolutionError, match="substr"):
        resolution.run(frame, {**SETTINGS, "blocking_rules": [[{"column": "surname", "substr": [0, 3]}]]})


def test_too_many_candidate_pairs_are_refused(monkeypatch):
    monkeypatch.setattr(resolution, "MAX_CANDIDATE_PAIRS", 10)
    with pytest.raises(resolution.ResolutionError, match="stricter"):
        resolution.run(noisy_people(people=40), SETTINGS)


def test_term_frequency_adjustment_discounts_agreement_on_common_values():
    import numpy as np
    frame = pd.DataFrame({"g0": [4, 4, 4, 0], "tf0": [0.5, 0.01, np.nan, np.nan]})
    adjustment = resolution._tf_adjustment(frame, 0, 4, 0.05)
    assert adjustment[0] == pytest.approx(-3.3219, abs=1e-3)  # a value on half the records says less than the average exact match
    assert adjustment[1] == pytest.approx(2.3219, abs=1e-3)   # a rare value says more
    assert adjustment[2] == 0 and adjustment[3] == 0           # no adjustment without a value frequency, or without an exact match


def test_term_frequency_adjusted_comparisons_are_flagged_and_still_link():
    settings = {**SETTINGS, "comparisons": [{**c, "tf": True} if c["column"] in ("first_name", "city") else c for c in SETTINGS["comparisons"]]}
    out = resolution.run(noisy_people(), settings)
    flagged = {c["column"] for c in out["model"]["comparisons"] if c["term_frequency_adjusted"]}
    assert flagged == {"first_name", "city"}
    assert out["evaluation"]["precision"] >= 0.9 and out["evaluation"]["recall"] >= 0.85
    adjusted = [x for ex in out["examples"]["confident"] for x in ex["contributions"] if "词频校正" in x["label"]]
    assert adjusted, "an exact agreement on a tf-adjusted field should say how common the value is"


def test_error_examples_really_are_errors(result):
    threshold = result["settings"]["threshold_match_probability"]
    assert all(e["same_entity"] is False and e["probability"] >= threshold for e in result["examples"]["false_positives"])
    assert all(e["same_entity"] is True and e["probability"] < threshold for e in result["examples"]["false_negatives"])
    if result["evaluation"]["precision"] == 1.0:
        assert result["examples"]["false_positives"] == [], "with no wrong link there is nothing to show under 'wrongly merged'"
