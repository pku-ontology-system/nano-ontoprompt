"""Object types that describe the same thing twice.

Auto Ontology builds a type from a table, a person adds one by hand, a second run builds it again under a slightly different name:
the ontology ends up with FactoryResource and ProductionResource, ScheduleItem and ScheduledItem. There are too few types to train a
matcher on, so this is a transparent score: how alike the names are, how many properties they share once the type's own name is taken
off them (`resource_name` of ProductionResource is `name`), and whether they were built from the same source table. Every pair says
which of the three reasons it found, so a person can overrule it.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from app.services.v2.analysis import ontology_metadata, ontology_source
from app.services.v2.analysis.ontology_suite import identity_property, snake

WEIGHTS = {"name": 0.35, "properties": 0.35, "table": 0.30}
LIKELY, POSSIBLE = 0.7, 0.5
SUFFIXES = ("ing", "ed", "es", "s", "d")
TIME_WORDS = {"at", "time", "date"}


def words(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", snake(name)) if w]


def stem(word: str) -> str:
    """Enough of a stemmer to treat schedule/scheduled and resource/resources as one word. Only ever used to compare."""
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 3 else word


def name_similarity(a: str, b: str) -> float:
    stems_a, stems_b = [stem(w) for w in words(a)], [stem(w) for w in words(b)]
    if not stems_a or not stems_b:
        return 0.0
    overlap = len(set(stems_a) & set(stems_b)) / len(set(stems_a) | set(stems_b))
    sequence = SequenceMatcher(None, "".join(stems_a), "".join(stems_b)).ratio()
    return round(max(overlap, 0.9 * sequence), 3)


def property_keys(type_name: str, names: list[str], identity: str | None) -> dict[str, str]:
    """Property name -> a key that does not depend on the type it belongs to: the identity is `id`, the type's own words are dropped,
    and `start_at` is `start`."""
    own = {stem(w) for w in words(type_name)}
    keys = {}
    for name in names:
        if name == identity:
            keys[name] = "id"
            continue
        parts = [stem(w) for w in words(name)]
        parts = [p for p in parts if p not in own] or parts
        if len(parts) > 1 and parts[-1] in TIME_WORDS:
            parts = parts[:-1]
        keys[name] = "_".join(parts)
    return keys


def describe(api_name: str, properties: list[str], *, tables: set[str] | None = None, primary_keys: list[str] | None = None, objects: int | None = None,
             origin: str = "type", **extra: Any) -> dict[str, Any]:
    identity, _ = identity_property(api_name, properties, primary_keys or [])
    return {"api_name": api_name, "properties": properties, "keys": property_keys(api_name, properties, identity), "identity": identity,
            "tables": sorted(tables or []), "objects": objects, "origin": origin, **extra}


def _by_key(descriptor: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, key in descriptor["keys"].items():
        out.setdefault(key, []).append(name)
    return out


def compare(a: dict, b: dict) -> dict[str, Any]:
    by_a, by_b = _by_key(a), _by_key(b)
    shared = sorted(set(by_a) & set(by_b))
    union, smaller = len(set(by_a) | set(by_b)), min(len(by_a), len(by_b))
    jaccard, containment = (len(shared) / union if union else 0.0), (len(shared) / smaller if smaller else 0.0)
    properties = round((jaccard + containment) / 2, 3)
    name = name_similarity(a["api_name"], b["api_name"])
    tables = sorted(set(a["tables"]) & set(b["tables"]))
    score = round(WEIGHTS["name"] * name + WEIGHTS["properties"] * properties + WEIGHTS["table"] * (1.0 if tables else 0.0), 3)
    pairs = [{"a": by_a[k][0], "b": by_b[k][0]} for k in shared]
    reasons = []
    if name >= 0.5:
        reasons.append(f"名字相近（{a['api_name']} / {b['api_name']}）")
    if tables:
        reasons.append(f"来自同一张表 {', '.join(tables)}")
    if pairs and properties >= 0.4:
        shown = ", ".join(p["a"] if p["a"] == p["b"] else f"{p['a']}≈{p['b']}" for p in pairs[:5])
        reasons.append(f"属性重合 {len(pairs)} 个（{shown}{'…' if len(pairs) > 5 else ''}）")
    return {"a": a["api_name"], "b": b["api_name"], "score": score, "verdict": "likely" if score >= LIKELY else "possible" if score >= POSSIBLE else "unlikely",
            "name_similarity": name, "property_overlap": {"score": properties, "jaccard": round(jaccard, 3), "containment": round(containment, 3), "shared": pairs,
                                                          "only_a": sorted(n for k, ns in by_a.items() if k not in by_b for n in ns),
                                                          "only_b": sorted(n for k, ns in by_b.items() if k not in by_a for n in ns)},
            "shared_tables": tables, "reasons": reasons, "objects": {"a": a["objects"], "b": b["objects"]}, "origin": {"a": a["origin"], "b": b["origin"]},
            "suggestion": suggest(a, b)}


def suggest(a: dict, b: dict) -> str:
    count_a, count_b = a["objects"], b["objects"]
    if count_a is None or count_b is None:
        return "对象数量未知，先核对两个类型的含义，再决定是否合并"
    if count_a and count_b:
        return f"两个类型都有对象（{count_a} / {count_b}）；合并前先用实体消歧确认对象是否重复"
    if count_a or count_b:
        keep, drop = (a, b) if count_a else (b, a)
        return f"建议保留 {keep['api_name']}（已有 {keep['objects']} 个对象）；{drop['api_name']} 还没有对象，核对后可以并入或删除"
    keep, drop = (a, b) if len(a["properties"]) >= len(b["properties"]) else (b, a)
    return f"两个类型都还没有对象；建议保留属性更完整的 {keep['api_name']}（{len(keep['properties'])} 个属性），{drop['api_name']} 可以并入"


def find(descriptors: list[dict], *, minimum: float = POSSIBLE, others: list[dict] | None = None) -> list[dict]:
    """Every pair at or above `minimum`, strongest first. With `others`, each descriptor is compared against those instead of each other."""
    pairs = []
    if others is None:
        pairs = [(descriptors[i], descriptors[j]) for i in range(len(descriptors)) for j in range(i + 1, len(descriptors))]
    else:
        pairs = [(d, o) for d in descriptors for o in others if d["api_name"] != o["api_name"]]
    found = [c for c in (compare(a, b) for a, b in pairs) if c["score"] >= minimum]
    return sorted(found, key=lambda c: (-c["score"], c["a"], c["b"]))


def type_descriptors(db: Any, user: Any, graph: Any, ontology_id: str) -> list[dict]:
    """The types of one ontology the caller may see, with the source tables they were built from and how many objects they have."""
    types = ontology_source.object_types(db, user, ontology_id)
    counts = ontology_source.object_counts(db, user, graph, ontology_id, [t["api_name"] for t in types])
    evidence, tables = ontology_metadata.candidate_evidence(db, ontology_id), ontology_metadata.source_tables(db, ontology_id)
    return [describe(t["api_name"], [p["api_name"] for p in t["properties"]], tables=tables.get(t["api_name"]), primary_keys=evidence.get(t["api_name"], {}).get("primary_key"),
                     objects=(counts.get(t["api_name"]) or {}).get("count"), built_by=evidence.get(t["api_name"], {}).get("set_title")) for t in types]


def candidate_descriptors(db: Any, ontology_id: str, set_id: str) -> list[dict]:
    """The entity types a candidate set proposes (rejected ones left out), described the same way."""
    out = []
    for item in ontology_metadata.candidate_items(db, ontology_id, set_id):
        draft = item.final or item.draft or {}
        evidence = item.evidence or {}
        names = [str(p.get("id") or p.get("name")) for p in draft.get("properties", []) if isinstance(p, dict) and (p.get("id") or p.get("name"))]
        out.append(describe(draft.get("name_en") or item.name, names, tables={evidence["source_table"]} if evidence.get("source_table") else set(),
                            primary_keys=list(evidence.get("primary_key") or []), objects=0, origin="candidate", item_id=item.id, decision=item.decision))
    return out


def analyse(db: Any, user: Any, graph: Any, ontology_id: str, *, candidate_set: str | None = None, minimum: float = POSSIBLE) -> dict[str, Any]:
    """Duplicates among the ontology's types and, when a candidate set is given, between its candidates and those types."""
    existing = type_descriptors(db, user, graph, ontology_id)
    result = {"ontology_id": ontology_id, "types_compared": len(existing), "minimum": minimum, "weights": WEIGHTS, "pairs": find(existing, minimum=minimum)}
    if candidate_set:
        candidates = candidate_descriptors(db, ontology_id, candidate_set)
        result["candidate_set"] = candidate_set
        result["candidates_compared"] = len(candidates)
        known = {d["api_name"].lower() for d in existing}
        # A candidate that carries the name of an existing type is that type again, which the review already shows as "已存在".
        result["candidate_pairs"] = find([c for c in candidates if c["api_name"].lower() not in known], minimum=minimum, others=existing) \
            + find([c for c in candidates if c["api_name"].lower() not in known], minimum=minimum)
    return result
