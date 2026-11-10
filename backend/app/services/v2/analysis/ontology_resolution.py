"""Entity resolution on the objects of one type: which objects are the same real-world thing.

The comparison settings are drafted from the type's properties, the way Splink's tutorials choose them by hand: names are compared by
similarity, dates by edit distance, e-mails by user name, places exactly with term-frequency adjustment. The draft is shown with the
reason for each choice and can be edited before it is run; nothing here writes back to the ontology.
"""
from __future__ import annotations

from typing import Any, Iterable

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
SOURCE_WORDS = {"source", "catalog", "catalogue", "dataset", "origin", "src", "system", "feed"}
AMOUNT_WORDS = {"price", "amount", "cost", "salary", "income", "balance", "total"}
CODE_WORDS = {"model", "sku", "code", "serial", "isbn", "barcode", "upc", "ean", "mpn", "part"}
NUMERIC = {"integer", "long", "double", "decimal", "boolean", "json", "array", "object"}
MAX_RULE_PAIRS = 3_000_000
MAX_TOTAL_PAIRS = 8_000_000
MAX_COMPARISONS = 8
IDENTIFYING = {"name", "email", "phone", "code", "address"}  # what can tell one individual from another; dates, places, amounts and categories only describe
MIN_COVERAGE = 0.15  # below this fewer than one pair in seven has the value on both sides, so it can hardly say anything about "same or not"
MIN_TRAINING_PAIRS = 50


def _part(column: str, start: int, length: int) -> dict[str, Any]:
    return {"column": column, "substr": [start, length]}


def kind_of(column: str, data_type: str) -> str | None:
    """What a property is for the purpose of comparing records, or None when it is not worth comparing."""
    kind, tokens = str(data_type).lower(), set(words(column))
    if kind in ("double", "decimal", "integer", "long") and tokens & AMOUNT_WORDS:
        return "amount"
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
    if tokens & CODE_WORDS:
        return "code"
    return "text"


def truth_candidates(columns: list[str]) -> list[str]:
    return [c for c in columns if c.lower() in TRUTH_NAMES]


def _clean(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().str.lower().replace("", pd.NA)


def _words(series: pd.Series) -> float:
    """The average number of words in the values of a column."""
    present = _clean(series).dropna()
    return float(present.str.split().str.len().mean()) if len(present) else 0.0


def _pairs(frame: pd.DataFrame, rule: list, across: str | None = None) -> int:
    """How many record pairs a blocking rule would compare: records are paired when every term of the rule agrees and is present
    (and, when linking across sources, when they come from different sources)."""
    terms = []
    for term in rule:
        column = term if isinstance(term, str) else term["column"]
        values = _clean(frame[column])
        if isinstance(term, dict) and term.get("substr"):
            start, length = term["substr"]
            values = values.str.slice(start - 1, start - 1 + length)
        terms.append(values.replace("", pd.NA))
    keys = [f"k{i}" for i in range(len(terms))]
    keyed = pd.concat(terms, axis=1)
    keyed.columns = keys
    if across:
        keyed["__source"] = _clean(frame[across])
    keyed = keyed.dropna()
    if keyed.empty:
        return 0
    sizes = keyed.groupby(keys).size().to_numpy(dtype="int64")
    total = int((sizes * (sizes - 1) // 2).sum())
    if across:  # pairs inside one source are not compared
        inside = keyed.groupby(keys + ["__source"]).size().to_numpy(dtype="int64")
        total -= int((inside * (inside - 1) // 2).sum())
    return total


def _distinct_share(series: pd.Series) -> float:
    present = _clean(series).dropna()
    return present.nunique() / len(present) if len(present) else 0.0


def _pair_coverage(frame: pd.DataFrame, column: str, across: str | None) -> float:
    """The share of comparable pairs (all pairs, or pairs from different sources when linking) that have a value on both sides."""
    present = _clean(frame[column]).notna()
    if across:
        source = _clean(frame[across])
        has = present.groupby(source).sum().to_numpy(dtype=float)
        size = source.value_counts().to_numpy(dtype=float)
        return float((has.sum() ** 2 - (has ** 2).sum()) / max(size.sum() ** 2 - (size ** 2).sum(), 1.0))
    has, size = float(present.sum()), float(len(frame))
    return has * (has - 1) / max(size * (size - 1), 1.0)


def _link_column(frame: pd.DataFrame, names: list[str], properties: dict[str, dict]) -> str | None:
    """A column that says which source a record came from: named like one, a few values, nearly always filled."""
    for column in names:
        if not set(words(column)) & SOURCE_WORDS or str(properties[column].get("data_type", "string")).lower() in NUMERIC:
            continue
        present = _clean(frame[column]).dropna()
        if 2 <= present.nunique() <= 4 and len(present) >= 0.9 * max(len(frame), 1) and present.value_counts().min() >= 0.05 * len(present):
            return column
    return None


def _label(term: Any) -> str:
    return term if isinstance(term, str) else f"{term['column']}[{term['substr'][0]}:{term['substr'][0] + term['substr'][1] - 1}]"


def _rule_columns(rule: list) -> set[str]:
    return {t if isinstance(t, str) else t["column"] for t in rule}


def _deterministic_rule(frame: pd.DataFrame, by_kind: dict[str, list[str]], across: str | None = None) -> list | None:
    """A rule whose agreement almost certainly means the same thing, used to estimate how common matches are.

    Such a rule pairs some records but not many: about one pair per record at most. Fewer pairs than a fiftieth of the records and the
    assumed share of matches it catches is far off; more than three per record and it is not specific enough to be read as "the same".
    """
    rows = len(frame)
    low, high = max(2, rows // 50), 3 * rows
    names = by_kind.get("name", [])
    candidates: list[list] = []
    if len(names) >= 2:
        candidates.append(names[:2])
    if names and by_kind.get("date"):
        candidates.append([names[0], by_kind["date"][0]])
    candidates += [[column] for column in by_kind.get("code", [])]
    candidates += [[column] for kind in ("name", "text", "address") for column in by_kind.get(kind, [])]
    for rule in candidates:
        if low <= _pairs(frame, rule, across) <= high:
            return rule
    return None


def _em_rules(frame: pd.DataFrame, rules: list[list], deterministic: list, across: str | None = None) -> list[list]:
    """Pairs to train the field probabilities on: one block where nearly everything is a match, one where the other fields can be learned.

    A field blocked on in a session cannot be estimated in it, so the second rule shares no column with the first. Among those, the one
    with fewer pairs (but enough to learn from) is mostly made of near misses rather than of strangers, which is where the fields matter.
    """
    chosen: list[list] = []
    if _pairs(frame, deterministic, across) >= MIN_TRAINING_PAIRS:
        chosen.append(deterministic)
    used = set().union(*(_rule_columns(rule) for rule in chosen)) if chosen else set()
    others = [(rule, _pairs(frame, rule, across)) for rule in rules if not _rule_columns(rule) & used]
    others = [(rule, count) for rule, count in others if count >= MIN_TRAINING_PAIRS]
    plenty = [item for item in others if item[1] >= 1000]
    pool = plenty or others
    if pool:
        chosen.append(min(pool, key=lambda item: item[1])[0] if plenty else max(pool, key=lambda item: item[1])[0])
    return chosen or rules[:2]


def suggest(frame: pd.DataFrame, description: dict, *, ground_truth: str | None = None, references: Iterable[str] = ()) -> dict[str, Any]:
    """Settings for `resolution.run` drafted from the type's properties, with the reason for each choice and what was left out.
    `references` are the properties that are foreign keys: they say which other object this one belongs to, and are often a coarse grouping read off another field."""
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
    foreign = set(references)
    link = _link_column(frame, names, properties)
    for column in names:
        if column == identity or column == truth:
            skipped.append({"column": column, "reason": "对象标识" if column == identity else "真实分组列，只用来给结果打分"})
            continue
        if column in foreign:
            skipped.append({"column": column, "reason": "外键：说明它属于另一个对象，已由关系表达，常常是从别的字段归出来的粗分类；要当证据可以手工加进比较"})
            continue
        kind = kind_of(column, properties[column].get("data_type", "string"))
        if kind is None:
            skipped.append({"column": column, "reason": f"{properties[column].get('data_type', '')} 类型的属性不用来比较"})
            continue
        present = _clean(frame[column]).dropna()
        coverage = _pair_coverage(frame, column, link)
        if len(present) / rows < 0.1:
            skipped.append({"column": column, "reason": "九成以上为空"})
        elif coverage < MIN_COVERAGE:
            skipped.append({"column": column, "reason": f"只有 {coverage:.0%} 的记录对两边都有值，多数记录对在这一列上没法比较"})
        elif present.nunique() <= 1:
            skipped.append({"column": column, "reason": "所有对象取值相同，区分不了"})
        elif kind in ("text", "code") and present.nunique() / len(present) > 0.9:
            skipped.append({"column": column, "reason": "几乎每个对象一个值，像标识或自由文本"})
        elif kind == "text" and present.nunique() <= 2:
            skipped.append({"column": column, "reason": "只有两个取值，区分不了"})
        else:
            chosen.append((column, kind))
    chosen = chosen[:MAX_COMPARISONS]
    if not any(kind in IDENTIFYING or (kind == "text" and _distinct_share(frame[column]) >= 0.3) for column, kind in chosen):
        raise resolution.NotApplicable("这个对象类型没有能区分个体的属性（名称、编号、邮箱、电话、地址这一类）：剩下的只有日期、金额和分类，它们只能描述一个对象，说不出“是不是同一个”，所以实体消歧不适用")

    comparisons = []
    for column, kind in chosen:
        if kind == "name" and _words(frame[column]) >= 3:
            comparisons.append({"column": column, "type": "tokens"})
            notes.append(f"{column}：名称由多个词组成，按共同词占比分级比较（≥0.8、≥0.5、≥0.3）")
        elif kind == "name":
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
        elif kind == "amount":
            comparisons.append({"column": column, "type": "ratio"})
            notes.append(f"{column}：金额，按相对差距 ≤2%、≤10%、≤30% 分级；同一样东西的标价通常很接近")
        elif kind == "code":
            comparisons.append({"column": column, "type": "code", "tf": True})
            notes.append(f"{column}：编号或型号，去掉符号后一致、或只是多了后缀就算吻合；差一两个字符通常是另一个东西，不当作相近；常见取值的证据更弱（词频调整）")
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
    wanted: list[list] = []
    for column, kind in sorted(chosen, key=lambda item: item[1] != "code"):  # identifiers first: the most precise way to pair records
        if kind == "amount":
            continue  # amounts grade the pairs, they do not pick them
        wanted.append([column])
        if kind in ("name", "text", "address", "code") and float(_clean(frame[column]).dropna().str.len().mean() or 0) >= 5:
            wanted.append([_part(column, 1, 4)])  # the first letters: written differently later, a product or a person usually starts the same way
    if len(by_kind.get("name", [])) >= 2:
        first, second = by_kind["name"][:2]
        wanted.append([_part(first, 1, 3), _part(second, 1, 3)])
    if by_kind.get("date") and by_kind.get("place"):
        wanted.append([_part(by_kind["date"][0], 1, 4), by_kind["place"][0]])
    wanted = wanted[:12]
    rules, total = [], 0
    for rule in wanted:
        count = _pairs(frame, rule, link)
        label = " + ".join(_label(term) for term in rule)
        if count > MAX_RULE_PAIRS or total + count > MAX_TOTAL_PAIRS:
            notes.append(f"粗筛规则 {label} 会产生 {count:,} 个记录对，太多，没有采用")
            continue
        rules.append(rule)
        total += count
        notes.append(f"粗筛规则 {label}：约 {count:,} 个记录对")
    if not rules:
        raise resolution.ResolutionError("粗筛规则都会产生过多的记录对；先缩小对象范围，或手工写粗筛规则")

    deterministic = _deterministic_rule(frame, by_kind, link)
    if deterministic is None:  # nothing agrees often enough, nor rarely enough, to be read as "the same": fall back on the broadest-looking choice
        names_first = by_kind.get("name", [])
        deterministic = names_first[:2] if len(names_first) >= 2 else [names_first[0], by_kind["date"][0]] if names_first and by_kind.get("date") else rules[0]
        notes.append("没有找到“几乎可以肯定是同一个”的一致性规则，先用 " + " + ".join(_label(t) for t in deterministic) + " 估计先验，结果请人工看一眼")
    else:
        notes.append("先验用 " + " + ".join(_label(t) for t in deterministic) + " 完全一致的记录对估计（这样的记录对几乎都是同一个）")
    em = _em_rules(frame, rules, deterministic, link)
    settings = {"unique_id": OBJECT_ID, "ground_truth": truth, "threshold_match_probability": 0.5, "comparisons": comparisons, "blocking_rules": rules,
                "training": {"deterministic_rules": [deterministic], "recall": 0.7, "em_blocking": em},
                "display_columns": [OBJECT_ID] + [c for c, _ in chosen][:7]}
    if link:
        notes.append(f"{link} 区分记录来自哪个来源：只在不同来源之间配对（关联），同一来源里的记录不互相比较；要在来源内部找重复，去掉 link_across")
        settings["link_across"] = link
    notes.append("匹配概率达到 0.5（比不匹配更可能）就算同一个；调高更保守，漏掉更多，调低会多并")
    return {"settings": settings, "notes": notes, "skipped": skipped, "truth_candidates": truth_candidates(names), "estimated_pairs": total}


def run(frame: pd.DataFrame, settings: dict) -> dict:
    """The engine on the objects. Records are identified by their object id, so every cluster member can be opened in the object explorer."""
    return resolution.run(frame, {**settings, "unique_id": OBJECT_ID})
