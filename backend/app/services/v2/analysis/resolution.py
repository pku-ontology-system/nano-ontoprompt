"""Entity resolution: probabilistic record linkage (Fellegi-Sunter), modelled on Splink.

Splink (MIT, https://github.com/moj-analytical-services/splink) estimates, for every field and every level of agreement, how
likely that level is among true matches (m) and among random pairs (u); a pair's match weight is the sum of log2(m / u) over its
fields plus a prior. The same pipeline is rebuilt here on DuckDB, which Splink also uses for the pair comparison:

1. blocking rules decide which pairs are compared at all;
2. u comes from random pairs, the prior (lambda) from a deterministic rule and an assumed recall, m from expectation maximisation
   run on pairs that share a blocking rule (the blocked fields carry no information there, so they are left out of that session);
3. every candidate pair gets a match weight and probability, pairs above a threshold are linked and connected components are the
   resolved entities;
4. when the table carries the true entity id, the result is scored (pairwise precision / recall) and mistakes are explained
   field by field.

Only structured settings are accepted (column names, `substr` terms), never SQL text, because the settings come from the browser.
"""
from __future__ import annotations

import math
import time
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

EPS = 1e-6
SMOOTHING = 1.0  # one pseudo-match per field, spread over its levels like random pairs, so a level never seen among matches is a veto of bounded size
MAX_ROWS = 200_000
MAX_CANDIDATE_PAIRS = 12_000_000
EXAMPLES = 8


class ResolutionError(ValueError):
    """The settings or the data cannot be used for linkage."""


def _ident(name: Any, columns: set[str]) -> str:
    if not isinstance(name, str) or name not in columns:
        raise ResolutionError(f"column '{name}' does not exist")
    return '"' + name.replace('"', '""') + '"'


def _clean(side: str, col: str) -> str:
    return f"lower(trim(CAST({side}.{col} AS VARCHAR)))"


def compile_comparison(spec: dict, columns: set[str]) -> dict:
    """One field's comparison as a SQL CASE that returns an agreement level. -1 means a value is missing (no evidence)."""
    kind = spec.get("type")
    col = _ident(spec.get("column"), columns)
    sl, sr = _clean("l", col), _clean("r", col)
    null = f"(l.{col} IS NULL OR r.{col} IS NULL OR {sl} = '' OR {sr} = '')"
    levels = [(-1, "缺失"), (0, "其他")]
    if kind == "exact":
        levels = [(-1, "缺失"), (0, "不一致"), (1, "完全一致")]
        case = f"CASE WHEN {null} THEN -1 WHEN {sl} = {sr} THEN 1 ELSE 0 END"
    elif kind in ("name", "jaro_winkler"):
        raw = spec.get("thresholds") or ([0.92, 0.88, 0.7] if kind == "name" else [0.9])
        thresholds = sorted({float(t) for t in raw}, reverse=True)
        if not thresholds or any(not 0 < t < 1 for t in thresholds):
            raise ResolutionError("Jaro-Winkler thresholds must lie between 0 and 1")
        n = len(thresholds)
        whens = f"WHEN {sl} = {sr} THEN {n + 1} " + " ".join(f"WHEN jaro_winkler_similarity({sl}, {sr}) >= {t} THEN {n - i}" for i, t in enumerate(thresholds))
        case = f"CASE WHEN {null} THEN -1 {whens} ELSE 0 END"
        levels += [(lvl, f"相似度 ≥ {thresholds[n - lvl]}") for lvl in range(1, n + 1)] + [(n + 1, "完全一致")]
    elif kind == "levenshtein":
        raw = spec.get("thresholds") or [1, 2]
        thresholds = sorted({int(t) for t in raw})
        if not thresholds or thresholds[0] < 1:
            raise ResolutionError("edit-distance thresholds must be positive integers")
        n = len(thresholds)
        whens = f"WHEN {sl} = {sr} THEN {n + 1} " + " ".join(f"WHEN levenshtein({sl}, {sr}) <= {t} THEN {n - j}" for j, t in enumerate(thresholds))
        case = f"CASE WHEN {null} THEN -1 {whens} ELSE 0 END"
        levels += [(n - j, f"编辑距离 ≤ {t}") for j, t in reversed(list(enumerate(thresholds)))] + [(n + 1, "完全一致")]
    elif kind == "email":
        ul, ur = f"split_part({sl}, '@', 1)", f"split_part({sr}, '@', 1)"
        case = (f"CASE WHEN {null} THEN -1 WHEN {sl} = {sr} THEN 4 WHEN {ul} = {ur} THEN 3 "
                f"WHEN jaro_winkler_similarity({sl}, {sr}) >= 0.88 THEN 2 WHEN jaro_winkler_similarity({ul}, {ur}) >= 0.88 THEN 1 ELSE 0 END")
        levels += [(1, "用户名相似度 ≥ 0.88"), (2, "邮箱相似度 ≥ 0.88"), (3, "用户名一致"), (4, "邮箱完全一致")]
    else:
        raise ResolutionError(f"unknown comparison type '{kind}'; use exact, name, jaro_winkler, levenshtein or email")
    return {"column": spec["column"], "type": kind, "case": case, "tf": bool(spec.get("tf")), "top": max(a for a, _ in levels),
            "levels": [{"level": a, "label": b} for a, b in sorted(levels)]}


def _term_sql(term: Any, side: str, columns: set[str]) -> str:
    if isinstance(term, str):
        return f"NULLIF(CAST({side}.{_ident(term, columns)} AS VARCHAR), '')"
    if isinstance(term, dict) and "column" in term:
        col = f"CAST({side}.{_ident(term['column'], columns)} AS VARCHAR)"
        window = term.get("substr")
        if window is None:
            return f"NULLIF({col}, '')"
        try:
            start, length = int(window[0]), int(window[1])
        except (TypeError, ValueError, IndexError) as error:
            raise ResolutionError("substr must be [start, length]") from error
        if start == 0 or length < 1:
            raise ResolutionError("substr start cannot be 0 and length must be at least 1")
        first = str(start) if start > 0 else f"length({col}) + {start} + 1"
        return f"NULLIF(substr({col}, {first}, {length}), '')"
    raise ResolutionError("a blocking term is a column name or {column, substr: [start, length]}")


def _rule_sql(rule: Any, columns: set[str]) -> str:
    if not isinstance(rule, list) or not rule:
        raise ResolutionError("a blocking rule is a non-empty list of terms")
    return " AND ".join(f"{_term_sql(t, 'l', columns)} = {_term_sql(t, 'r', columns)}" for t in rule)


def _rule_columns(rule: list) -> set[str]:
    return {t if isinstance(t, str) else t.get("column") for t in rule}


def normalise_settings(df: pd.DataFrame, settings: dict) -> dict:
    columns = {str(c) for c in df.columns}
    uid = settings.get("unique_id") or "unique_id"
    if uid not in columns:
        raise ResolutionError(f"unique id column '{uid}' does not exist")
    if not df[uid].is_unique or df[uid].isna().any():
        raise ResolutionError(f"'{uid}' must be unique and never empty")
    if len(df) > MAX_ROWS:
        raise ResolutionError(f"at most {MAX_ROWS} rows can be linked")
    comparisons = [compile_comparison(c, columns) for c in settings.get("comparisons") or []]
    if not comparisons:
        raise ResolutionError("at least one comparison is needed")
    rules = settings.get("blocking_rules") or []
    for rule in rules:
        _rule_sql(rule, columns)
    if not rules:
        raise ResolutionError("at least one blocking rule is needed")
    training = settings.get("training") or {}
    deterministic = training.get("deterministic_rules") or [training.get("deterministic_rule") or rules[0]]
    for rule in deterministic:
        _rule_sql(rule, columns)
    em_rules = training.get("em_blocking") or rules[:2]
    for rule in em_rules:
        _rule_sql(rule, columns)
    truth = settings.get("ground_truth")
    if truth and truth not in columns:
        raise ResolutionError(f"ground truth column '{truth}' does not exist")
    threshold = float(settings.get("threshold_match_probability", 0.9))
    if not 0 < threshold < 1:
        raise ResolutionError("threshold_match_probability must lie between 0 and 1")
    recall = float(training.get("recall", 0.7))
    if not 0 < recall <= 1:
        raise ResolutionError("training.recall must lie in (0, 1]")
    return {
        "unique_id": uid, "comparisons": comparisons, "blocking_rules": rules, "ground_truth": truth, "threshold_match_probability": threshold,
        "display_columns": [c for c in (settings.get("display_columns") or []) if c in columns] or [c for c in df.columns if str(c) != str(truth)][:8],
        "training": {"deterministic_rules": deterministic, "recall": recall, "em_blocking": em_rules, "u_max_pairs": int(training.get("u_max_pairs", 1_000_000)),
                     "em_iterations": int(training.get("em_iterations", 25)), "seed": int(training.get("seed", 7))},
    }


def _gamma_select(comparisons: list[dict], *, with_tf: bool = False) -> str:
    """Agreement levels, plus (for comparisons with term-frequency adjustment) how common the agreed value is."""
    parts = [f"CAST({c['case']} AS TINYINT) AS g{i}" for i, c in enumerate(comparisons)]
    if with_tf:
        parts += [f"CAST(CASE WHEN {c['case']} = {c['top']} THEN l.\"__tf{i}\" END AS FLOAT) AS tf{i}" for i, c in enumerate(comparisons) if c["tf"]]
    return ", ".join(parts)


def _pairs_query(table: str, rules: list[list], columns: set[str], select: str) -> str:
    """Candidate pairs (by row number) from blocking rules; a pair found by an earlier rule is not produced again by a later one."""
    parts = []
    for i, rule in enumerate(rules):
        skip = "".join(f" AND NOT COALESCE(({_rule_sql(prev, columns)}), FALSE)" for prev in rules[:i])
        parts.append(f"SELECT l.__rid AS id_l, r.__rid AS id_r, {select} FROM {table} l JOIN {table} r ON l.__rid < r.__rid AND {_rule_sql(rule, columns)}{skip}")
    return " UNION ALL ".join(parts)


def _level_index(level_values: np.ndarray) -> np.ndarray:
    return level_values.astype(np.int64) + 1  # -1 (missing) -> 0


def _initial_m(levels: list[dict]) -> np.ndarray:
    """Starting point for EM: true matches mostly land on the highest levels. Index 0 is the missing-value slot and stays 1."""
    real = sum(1 for x in levels if x["level"] >= 0)
    shares = np.array([0.5 ** (real - 1 - i) for i in range(real)], dtype=float)  # level 0 first, the top level last and largest
    arr = np.ones(real + 1)
    arr[1:] = shares / shares.sum()
    return arr


def _random_u(con, comparisons: list[dict], sample: pd.DataFrame, select: str) -> tuple[list[np.ndarray], int]:
    con.register("sample_df", pa.Table.from_pandas(sample, preserve_index=False))  # Arrow: DuckDB 1.2 cannot read pandas 3's new str dtype
    frame = con.execute(f"SELECT {select} FROM sample_df l JOIN sample_df r ON l.__rid < r.__rid").df()
    total = len(frame)
    out = []
    for i, comp in enumerate(comparisons):
        size = len(comp["levels"])
        counts = np.bincount(_level_index(frame[f"g{i}"].to_numpy()), minlength=size).astype(float)
        counts[0] = 0.0
        denominator = counts.sum()
        u = (counts + 0.5) / (denominator + 0.5 * (size - 1)) if denominator else np.full(size, 1.0 / max(1, size - 1))
        u = np.clip(u, EPS, 1 - EPS)
        u[0] = 1.0
        out.append(u)
    return out, total


def _em_session(patterns: np.ndarray, counts: np.ndarray, comparisons: list[dict], u: list[np.ndarray], m0: list[np.ndarray], lam0: float,
                fixed: set[int], iterations: int) -> tuple[list[np.ndarray | None], float, int]:
    m = [x.copy() for x in m0]
    lam = lam0
    live = [k for k in range(len(comparisons)) if k not in fixed]
    idx = [_level_index(patterns[:, k]) for k in range(len(comparisons))]
    used = 0
    for used in range(1, iterations + 1):
        log_match, log_non = np.full(len(counts), math.log(lam)), np.full(len(counts), math.log(1 - lam))
        for k in live:
            log_match += np.log(m[k][idx[k]])
            log_non += np.log(u[k][idx[k]])
        p = 1.0 / (1.0 + np.exp(np.clip(log_non - log_match, -50, 50)))
        weighted = p * counts
        change = 0.0
        for k in live:
            size = len(m[k])
            numer = np.bincount(idx[k], weights=weighted, minlength=size)
            numer[0] = 0.0
            prior = u[k].copy()
            prior[0] = 0.0
            numer += SMOOTHING * prior / prior.sum()
            denom = numer.sum()
            if denom <= 0:
                continue
            new = np.clip(numer / denom, EPS, 1 - EPS)
            new[0] = 1.0
            change = max(change, float(np.abs(new - m[k]).max()))
            m[k] = new
        lam = float(np.clip(weighted.sum() / counts.sum(), EPS, 1 - EPS))
        if change < 1e-4:
            break
    return [m[k] if k in live else None for k in range(len(comparisons))], lam, used


def _contribution_table(m: list[np.ndarray], u: list[np.ndarray]) -> list[np.ndarray]:
    return [np.where(np.arange(len(mk)) == 0, 0.0, np.log2(mk / uk)) for mk, uk in zip(m, u)]


def _tf_adjustment(frame: pd.DataFrame, k: int, top: int, u_top: float) -> np.ndarray:
    """log2(u / tf) for pairs that agree exactly on a value: Splink replaces the average u of an exact match by the value's own frequency."""
    tf = frame[f"tf{k}"].to_numpy().astype(float)
    hit = (frame[f"g{k}"].to_numpy() == top) & ~np.isnan(tf)
    out = np.zeros(len(frame))
    out[hit] = np.log2(u_top / np.clip(tf[hit], EPS, 1.0))
    return out


def _score(frame: pd.DataFrame, weights: list[np.ndarray], prior_weight: float, comparisons: list[dict] | None = None, u: list[np.ndarray] | None = None) -> pd.DataFrame:
    w = np.full(len(frame), prior_weight)
    for k, table in enumerate(weights):
        w += table[_level_index(frame[f"g{k}"].to_numpy())]
        if comparisons and comparisons[k]["tf"]:
            w += _tf_adjustment(frame, k, comparisons[k]["top"], float(u[k][comparisons[k]["top"] + 1]))
    out = frame[["id_l", "id_r"]].copy()
    out["weight"] = w
    out["probability"] = 1.0 / (1.0 + np.exp2(-np.clip(w, -60, 60)))
    return out


def _py(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if np.isnan(value) else float(value)
    if value is pd.NaT or value is None:
        return None
    return value if isinstance(value, (str, int, bool)) else str(value)


def _sig(value: float) -> float:
    """Four significant digits: small probabilities must not round to zero."""
    return float(f"{value:.4g}")


def _record(row: pd.Series, columns: list[str]) -> dict:
    return {c: _py(row[c]) for c in columns}


def _pair_count(sizes: np.ndarray) -> int:
    return int((sizes * (sizes - 1) // 2).sum())


def _true_pair_keys(truth: np.ndarray, n: int) -> np.ndarray:
    """Every pair of rows that share a true entity, as one integer each (row_a * n + row_b with a < b)."""
    order = np.argsort(truth, kind="stable")
    sorted_truth = truth[order]
    keys = []
    for group, label in zip(np.split(order, np.flatnonzero(np.diff(sorted_truth)) + 1), [sorted_truth[0]] + sorted_truth[np.flatnonzero(np.diff(sorted_truth)) + 1].tolist() if n else []):
        if label >= 0 and len(group) > 1:
            group = np.sort(group)
            a, b = np.triu_indices(len(group), k=1)
            keys.append(group[a].astype(np.int64) * n + group[b])
    return np.concatenate(keys) if keys else np.array([], dtype=np.int64)


def run(df: pd.DataFrame, settings: dict) -> dict:
    started = time.perf_counter()
    cfg = normalise_settings(df, settings)
    work = df.reset_index(drop=True).copy()
    work["__rid"] = np.arange(len(work), dtype=np.int64)
    columns = {str(c) for c in df.columns}
    uid_name = cfg["unique_id"]
    comps, n, train = cfg["comparisons"], len(work), cfg["training"]
    for k, comp in enumerate(comps):
        if comp["tf"]:  # share of the (non-empty) records that carry each value
            cleaned = work[comp["column"]].astype("string").str.strip().str.lower().replace("", pd.NA)
            work[f"__tf{k}"] = (cleaned.map(cleaned.value_counts()).astype(float) / max(1, int(cleaned.notna().sum()))).astype("float32")
    con = duckdb.connect(":memory:")
    try:
        con.register("df", pa.Table.from_pandas(work, preserve_index=False))
        try:
            con.execute("SET enable_external_access = false")
        except duckdb.Error:
            pass
        select = _gamma_select(comps)
        select_with_tf = _gamma_select(comps, with_tf=True)

        # u: how often each level occurs among random pairs (assumed to be almost all non-matches)
        sample_rows = min(n, int(math.ceil(math.sqrt(2 * train["u_max_pairs"]))) + 1)
        sample = work if sample_rows >= n else work.sample(n=sample_rows, random_state=train["seed"])
        u, u_pairs = _random_u(con, comps, sample, select)

        # prior: pairs that agree on a deterministic rule, corrected for the share of true matches those rules are assumed to catch
        total_pairs = n * (n - 1) // 2
        rule_hits = int(con.execute(f"SELECT count(*) FROM ({_pairs_query('df', train['deterministic_rules'], columns, '1 AS one')})").fetchone()[0])
        lam = float(np.clip(rule_hits / train["recall"] / max(1, total_pairs), 1e-9, 0.5))

        # m: EM on pairs that share a blocking rule, one session per rule, averaged over the sessions that could estimate a field
        m_start = [_initial_m(c["levels"]) for c in comps]
        estimates: list[list[np.ndarray]] = [[] for _ in comps]
        sessions = []
        for rule in train["em_blocking"]:
            blocked = _rule_columns(rule)
            fixed = {k for k, c in enumerate(comps) if c["column"] in blocked}
            patterns_sql = f"SELECT {', '.join(f'g{k}' for k in range(len(comps)))}, count(*) AS n FROM ({_pairs_query('df', [rule], columns, select)}) GROUP BY ALL"
            table = con.execute(patterns_sql).df()
            if table.empty:
                sessions.append({"rule": rule, "pairs": 0, "iterations": 0, "lambda": None})
                continue
            patterns = table[[f"g{k}" for k in range(len(comps))]].to_numpy()
            counts = table["n"].to_numpy().astype(float)
            m_new, lam_session, used = _em_session(patterns, counts, comps, u, m_start, max(lam, 1e-4), fixed, train["em_iterations"])
            for k, arr in enumerate(m_new):
                if arr is not None:
                    estimates[k].append(arr)
            sessions.append({"rule": rule, "pairs": int(counts.sum()), "iterations": used, "lambda": round(lam_session, 6), "fixed": sorted(comps[k]["column"] for k in fixed)})
        m = [np.mean(est, axis=0) if est else m_start[k] for k, est in enumerate(estimates)]
        for arr in m:
            arr[0] = 1.0
        weights = _contribution_table(m, u)
        prior_weight = math.log2(lam / (1 - lam))

        # candidate pairs for prediction
        candidates = int(con.execute(f"SELECT count(*) FROM ({_pairs_query('df', cfg['blocking_rules'], columns, '1 AS one')})").fetchone()[0])
        if candidates > MAX_CANDIDATE_PAIRS:
            raise ResolutionError(f"the blocking rules produce {candidates:,} pairs (limit {MAX_CANDIDATE_PAIRS:,}); make them stricter")
        pairs = con.execute(_pairs_query("df", cfg["blocking_rules"], columns, select_with_tf)).df()
    finally:
        con.close()

    scored = _score(pairs, weights, prior_weight, comps, u) if len(pairs) else pd.DataFrame({"id_l": [], "id_r": [], "weight": [], "probability": []})
    threshold = cfg["threshold_match_probability"]
    left_all, right_all = scored["id_l"].to_numpy().astype(np.int64), scored["id_r"].to_numpy().astype(np.int64)
    above = scored["probability"].to_numpy() >= threshold
    if above.any():
        graph = coo_matrix((np.ones(int(above.sum())), (left_all[above], right_all[above])), shape=(n, n))
        n_clusters, labels = connected_components(graph, directed=False)
    else:
        n_clusters, labels = n, np.arange(n)
    sizes = np.bincount(labels)

    truth_name = cfg["ground_truth"]
    truth_labels = pd.factorize(work[truth_name])[0] if truth_name else None
    same = None
    evaluation = None
    if truth_labels is not None:
        known = truth_labels >= 0
        true_sizes = np.bincount(truth_labels[known])
        joint = pd.Series(1, index=pd.MultiIndex.from_arrays([labels[known], truth_labels[known]])).groupby(level=[0, 1]).sum().to_numpy()
        true_pairs, predicted_pairs, tp = _pair_count(true_sizes), _pair_count(sizes), _pair_count(joint)
        precision = tp / predicted_pairs if predicted_pairs else 1.0
        recall = tp / true_pairs if true_pairs else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        wanted = _true_pair_keys(truth_labels, n)
        in_blocks = int(np.isin(wanted, left_all * n + right_all).sum()) if len(wanted) and len(scored) else 0
        same = (truth_labels[left_all] == truth_labels[right_all]) & (truth_labels[left_all] >= 0)
        evaluation = {
            "ground_truth_column": truth_name, "true_entities": int(len(true_sizes)), "true_pairs": true_pairs, "predicted_pairs": predicted_pairs, "true_positive_pairs": tp,
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "blocking_recall": round(in_blocks / true_pairs, 4) if true_pairs else 1.0, "threshold": threshold,
        }

    shown = cfg["display_columns"]
    by_row = lambda i: _record(work.iloc[int(i)], shown)  # noqa: E731
    clusters = []
    for c in [int(c) for c in np.argsort(-sizes, kind="stable")[:30] if sizes[c] >= 2]:
        members = np.flatnonzero(labels == c)
        item = {"cluster_id": c, "size": int(len(members)), "records": [by_row(i) for i in members[:EXAMPLES]]}
        if truth_labels is not None:
            item["true_entities_inside"] = int(len(set(truth_labels[members].tolist())))
        clusters.append(item)

    def explain(pos: int) -> dict:
        row, score = pairs.iloc[pos], scored.iloc[pos]
        i, j = int(row["id_l"]), int(row["id_r"])
        parts = [{"column": "先验", "label": f"随机两条记录是同一实体的概率 λ = {lam:.2e}", "weight": round(prior_weight, 3)}]
        for k, comp in enumerate(comps):
            level = int(row[f"g{k}"])
            label = next(x["label"] for x in comp["levels"] if x["level"] == level)
            weight = float(weights[k][level + 1])
            if comp["tf"] and level == comp["top"] and not np.isnan(row[f"tf{k}"]):
                share = float(row[f"tf{k}"])
                weight += math.log2(float(u[k][level + 1]) / max(share, EPS))
                label += f"（该值占 {share:.2%}，按词频校正）"
            parts.append({"column": comp["column"], "label": label, "weight": round(weight, 3)})
        return {"id_l": _py(work[uid_name].iloc[i]), "id_r": _py(work[uid_name].iloc[j]), "left": by_row(i), "right": by_row(j), "weight": round(float(score["weight"]), 3),
                "probability": round(float(score["probability"]), 4), "same_entity": None if truth_labels is None else bool(same[pos]), "contributions": parts}

    examples: dict[str, list] = {"borderline": [], "false_positives": [], "false_negatives": [], "confident": []}
    if len(scored):
        examples["borderline"] = [explain(int(p)) for p in (scored["probability"] - 0.5).abs().nsmallest(EXAMPLES).index]
        examples["confident"] = [explain(int(p)) for p in scored["weight"].nlargest(4).index]
        if same is not None:
            examples["false_positives"] = [explain(int(p)) for p in scored["weight"].where(above & ~same).dropna().nlargest(EXAMPLES).index]  # dropna: nlargest of an all-NaN series returns NaN rows
            examples["false_negatives"] = [explain(int(p)) for p in scored["weight"].where(~above & same).dropna().nlargest(EXAMPLES).index]

    edges = np.arange(-30, 32, 2)
    histogram = {"bin_start": edges[:-1].tolist(), "bin_width": 2}
    if len(scored):
        clipped = scored["weight"].clip(-29.999, 29.999).to_numpy()
        if same is not None:
            histogram["match"] = np.histogram(clipped[same], bins=edges)[0].tolist()
            histogram["non_match"] = np.histogram(clipped[~same], bins=edges)[0].tolist()
        else:
            histogram["all"] = np.histogram(clipped, bins=edges)[0].tolist()

    return {
        "settings": {"unique_id": uid_name, "comparisons": [{"column": c["column"], "type": c["type"], "tf": c["tf"]} for c in comps], "blocking_rules": cfg["blocking_rules"],
                     "threshold_match_probability": threshold, "ground_truth": truth_name, "training": train},
        "summary": {"records": n, "total_possible_pairs": total_pairs, "candidate_pairs": int(len(pairs)), "pairs_above_threshold": int(above.sum()), "entities_found": int(n_clusters),
                    "records_in_clusters": int(sizes[sizes >= 2].sum()), "singletons": int((sizes == 1).sum()), "largest_cluster": int(sizes.max()) if n else 0,
                    "run_time_ms": int((time.perf_counter() - started) * 1000)},
        "evaluation": evaluation,
        "model": {"lambda": lam, "prior_weight": round(prior_weight, 3), "random_pairs_for_u": int(u_pairs), "sessions": sessions,
                  "comparisons": [{"column": c["column"], "type": c["type"], "term_frequency_adjusted": c["tf"], "levels": [
                      {"level": x["level"], "label": x["label"], "m": None if x["level"] < 0 else _sig(float(m[k][x["level"] + 1])),
                       "u": None if x["level"] < 0 else _sig(float(u[k][x["level"] + 1])), "weight": None if x["level"] < 0 else round(float(weights[k][x["level"] + 1]), 3)}
                      for x in c["levels"]]} for k, c in enumerate(comps)]},
        "histogram": histogram, "clusters": clusters, "examples": examples,
    }
