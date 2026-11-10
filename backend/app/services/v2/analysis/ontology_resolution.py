"""Entity resolution on the objects of one type: which objects are the same real-world thing.

The comparison settings are drafted from the type's properties, the way Splink's tutorials choose them by hand: names are compared by
similarity, dates by edit distance, e-mails by user name, places exactly with term-frequency adjustment. The draft is shown with the
reason for each choice and can be edited before it is run; nothing here writes back to the ontology.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.services.v2.analysis import resolution
from app.services.v2.analysis.ontology_duplicates import words
from app.services.v2.analysis.ontology_source import OBJECT_ID
from app.services.v2.analysis.ontology_suite import identity_property

TRUTH_NAMES = {"cluster", "ground_truth", "truth", "true_entity", "true_id", "entity_cluster"}
NAME_WORDS = {"name", "surname", "forename", "given", "family", "first", "last", "full"}
DATE_WORDS = {"dob", "birth", "born", "date"}
PLACE_WORDS = {"city", "town", "place", "country", "state", "region", "location"}
ADDRESS_WORDS = {"address", "street", "addr"}
POSTCODE_WORDS = {"postcode", "zip", "postal"}
PHONE_WORDS = {"phone", "tel", "mobile", "cell"}
NUMERIC = {"integer", "long", "double", "decimal", "boolean", "json", "array", "object"}
MAX_RULE_PAIRS = 3_000_000
MAX_TOTAL_PAIRS = 8_000_000
MAX_COMPARISONS = 8


def _part(column: str, start: int, length: int) -> dict[str, Any]:
    return {"column": column, "substr": [start, length]}


def kind_of(column: str, data_type: str) -> str | None:
    """What a property is for the purpose of comparing records, or None when it is not worth comparing."""
    kind, tokens = str(data_type).lower(), set(words(column))
    if kind in NUMERIC:
        return None
    if tokens & {"email", "mail"}:
        return "email"
    if kind in ("date", "timestamp", "datetime") or tokens & DATE_WORDS:
        return "date"
    if tokens & PHONE_WORDS:
        return "phone"
    if tokens & POSTCODE_WORDS:
        return "postcode"
    if tokens & ADDRESS_WORDS:
        return "address"
    if tokens & PLACE_WORDS:
        return "place"
    if tokens & NAME_WORDS:
        return "name"
    return "text"


def truth_candidates(columns: list[str]) -> list[str]:
    return [c for c in columns if c.lower() in TRUTH_NAMES]


def _clean(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().str.lower().replace("", pd.NA)


def _pairs(frame: pd.DataFrame, rule: list) -> int:
    """How many record pairs a blocking rule would compare: records are paired when every term of the rule agrees and is present."""
    terms = []
    for term in rule:
        column = term if isinstance(term, str) else term["column"]
        values = _clean(frame[column])
        if isinstance(term, dict) and term.get("substr"):
            start, length = term["substr"]
            values = values.str.slice(start - 1, start - 1 + length)
        terms.append(values.replace("", pd.NA))
    keyed = pd.concat(terms, axis=1).dropna()
    if keyed.empty:
        return 0
    sizes = keyed.groupby(list(keyed.columns)).size().to_numpy(dtype="int64")
    return int((sizes * (sizes - 1) // 2).sum())


def suggest(frame: pd.DataFrame, description: dict, *, ground_truth: str | None = None) -> dict[str, Any]:
    """Settings for `resolution.run` drafted from the type's properties, with the reason for each choice and what was left out."""
    properties = {p["api_name"]: p for p in description["properties"]}
    names = [p["api_name"] for p in description["properties"]]
    identity, _ = identity_property(description["api_name"], names, [])
    if ground_truth is None:  # not said: use a column that is plainly one; "" means score nothing
        truth = (truth_candidates(names) or [None])[0]
    else:
        truth = ground_truth if ground_truth in names else None
    notes: list[str] = []
    skipped: list[dict] = []
    chosen: list[tuple[str, str]] = []
    rows = max(len(frame), 1)
    for column in names:
        if column == identity or column == truth:
            skipped.append({"column": column, "reason": "对象标识" if column == identity else "真实分组列，只用来给结果打分"})
            continue
        kind = kind_of(column, properties[column].get("data_type", "string"))
        if kind is None:
            skipped.append({"column": column, "reason": f"{properties[column].get('data_type', '')} 类型的属性不用来比较"})
            continue
        present = _clean(frame[column]).dropna()
        if len(present) / rows < 0.1:
            skipped.append({"column": column, "reason": "九成以上为空"})
        elif present.nunique() <= 1:
            skipped.append({"column": column, "reason": "所有对象取值相同，区分不了"})
        elif kind == "text" and present.nunique() / len(present) > 0.9:
            skipped.append({"column": column, "reason": "几乎每个对象一个值，像标识或自由文本"})
        else:
            chosen.append((column, kind))
    chosen = chosen[:MAX_COMPARISONS]
    if not chosen:
        raise resolution.ResolutionError("这个对象类型没有适合比较的属性（需要字符串、日期等能说明“是不是同一个”的属性）")

    comparisons = []
    for column, kind in chosen:
        if kind == "name":
            comparisons.append({"column": column, "type": "name", "tf": True})
            notes.append(f"{column}：名字类，按 Jaro-Winkler 相似度分级比较，常见名字的证据更弱（词频调整）")
        elif kind == "date":
            comparisons.append({"column": column, "type": "levenshtein", "thresholds": [1, 2]})
            notes.append(f"{column}：日期，按编辑距离 ≤1、≤2 分级，容忍录入错一两位")
        elif kind == "email":
            comparisons.append({"column": column, "type": "email"})
            notes.append(f"{column}：邮箱，分别看完整地址和用户名")
        elif kind == "phone":
            comparisons.append({"column": column, "type": "levenshtein", "thresholds": [1, 2]})
            notes.append(f"{column}：电话，按编辑距离分级")
        elif kind == "postcode":
            comparisons.append({"column": column, "type": "levenshtein", "thresholds": [2]})
            notes.append(f"{column}：邮编，按编辑距离 ≤2")
        elif kind == "address":
            comparisons.append({"column": column, "type": "jaro_winkler", "thresholds": [0.9, 0.7], "tf": True})
            notes.append(f"{column}：地址，按 Jaro-Winkler 相似度分级")
        else:
            comparisons.append({"column": column, "type": "exact", "tf": True})
            notes.append(f"{column}：{'地点' if kind == 'place' else '文本'}，只看是否一致，常见取值的证据更弱（词频调整）")

    by_kind: dict[str, list[str]] = {}
    for column, kind in chosen:
        by_kind.setdefault(kind, []).append(column)
    wanted: list[list] = [[c] for kind in ("name", "date", "email", "phone") for c in by_kind.get(kind, [])][:4]
    if len(by_kind.get("name", [])) >= 2:
        first, second = by_kind["name"][:2]
        wanted.append([_part(first, 1, 3), _part(second, 1, 3)])
    if by_kind.get("date") and by_kind.get("place"):
        wanted.append([_part(by_kind["date"][0], 1, 4), by_kind["place"][0]])
    if not wanted:
        wanted = [[chosen[0][0]]]
    rules, total = [], 0
    for rule in wanted:
        count = _pairs(frame, rule)
        label = " + ".join(t if isinstance(t, str) else f"{t['column']}[{t['substr'][0]}:{t['substr'][0] + t['substr'][1] - 1}]" for t in rule)
        if count > MAX_RULE_PAIRS or total + count > MAX_TOTAL_PAIRS:
            notes.append(f"粗筛规则 {label} 会产生 {count:,} 个记录对，太多，没有采用")
            continue
        rules.append(rule)
        total += count
        notes.append(f"粗筛规则 {label}：约 {count:,} 个记录对")
    if not rules:
        raise resolution.ResolutionError("粗筛规则都会产生过多的记录对；先缩小对象范围，或手工写粗筛规则")

    names_first = by_kind.get("name", [])
    deterministic = [names_first[:2]] if len(names_first) >= 2 else [[names_first[0], by_kind["date"][0]]] if names_first and by_kind.get("date") else [rules[0]]
    used = {c for rule in deterministic for c in rule}
    em = [[c] for kind in ("date", "email", "phone", "place", "name") for c in by_kind.get(kind, []) if c not in used][:2] or rules[:2]
    em = [rule for rule in em if _pairs(frame, rule) <= MAX_RULE_PAIRS] or rules[:2]
    settings = {"unique_id": OBJECT_ID, "ground_truth": truth, "threshold_match_probability": 0.5, "comparisons": comparisons, "blocking_rules": rules,
                "training": {"deterministic_rules": deterministic, "recall": 0.7, "em_blocking": em},
                "display_columns": [OBJECT_ID] + [c for c, _ in chosen][:7]}
    notes.append("匹配概率达到 0.5（比不匹配更可能）就算同一个；调高更保守，漏掉更多，调低会多并")
    return {"settings": settings, "notes": notes, "skipped": skipped, "truth_candidates": truth_candidates(names), "estimated_pairs": total}


def run(frame: pd.DataFrame, settings: dict) -> dict:
    """The engine on the objects. Records are identified by their object id, so every cluster member can be opened in the object explorer."""
    return resolution.run(frame, {**settings, "unique_id": OBJECT_ID})
