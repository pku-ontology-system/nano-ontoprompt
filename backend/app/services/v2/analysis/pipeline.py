"""One dataset through all three engines: read it, check its health, resolve its entities, and record each step as OpenLineage events.

The lineage graph this leaves behind is the point: the raw table, the expectation report and the resolved entities hang off the jobs
that produced them, and the data-quality results sit on the dataset they were measured on, as the OpenLineage facets define.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.services.v2.analysis import demos, health, lineage, resolution

NAMESPACE = "workbench"
PRODUCER = "urn:workbench:analysis-pipeline"
FACET_URL = "https://openlineage.io/spec/facets/1-0-0/{}.json"
WORKBENCH_FACET_URL = "urn:workbench:facet:{}"


def _slug(dataset_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", dataset_id.lower()).strip("_")


def _facet(schema: str, body: dict[str, Any], *, custom: bool = False) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": (WORKBENCH_FACET_URL if custom else FACET_URL).format(schema), **body}


def _sql_type(dtype: Any) -> str:
    if pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(dtype):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        return "DOUBLE"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "TIMESTAMP"
    return "VARCHAR"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _event(kind: str, job: str, run_id: str, when: datetime, *, inputs: list | None = None, outputs: list | None = None,
           run_facets: dict | None = None, job_facets: dict | None = None) -> dict[str, Any]:
    return {"eventType": kind, "eventTime": _stamp(when), "run": {"runId": run_id, "facets": run_facets or {}},
            "job": {"namespace": NAMESPACE, "name": job, "facets": job_facets or {}}, "inputs": inputs or [], "outputs": outputs or [], "producer": PRODUCER}


def _assertions(report: dict) -> list[dict]:
    out = []
    for result in report["results"]:
        kwargs = result["kwargs"]
        column = kwargs.get("column") or (", ".join(kwargs["column_list"]) if kwargs.get("column_list") else None)
        out.append({"assertion": result["expectation_type"], "success": bool(result["success"]), "column": column})
    return out


def _column_metrics(frame: pd.DataFrame) -> dict[str, Any]:
    return {str(c): {"nullCount": int(frame[c].isna().sum()), "distinctCount": int(frame[c].nunique(dropna=True))} for c in list(frame.columns)[:30]}


def run(db: Session, user_id: str, dataset_id: str = "splink-fake-1000") -> dict[str, Any]:
    item = demos.entry(dataset_id)
    frame = demos.load_frame(dataset_id)
    suite = demos.health_suite(dataset_id)
    settings = demos.resolution_settings(dataset_id)
    slug = _slug(dataset_id)
    names = {"raw": f"{slug}.raw", "report": f"{slug}.health_report", "pairs": f"{slug}.match_pairs", "entities": f"{slug}.entities"}
    jobs = {"ingest": f"{slug}.ingest", "validate": f"{slug}.validate", "resolve": f"{slug}.resolve"}
    sha = demos.sha256_of(demos.path_of(item))
    clock = datetime.now(timezone.utc)
    events: list[dict[str, Any]] = []

    def span(started: float) -> timedelta:
        return timedelta(seconds=max(time.perf_counter() - started, 0.001))

    def dataset(key: str, **facets: Any) -> dict[str, Any]:
        return {"namespace": NAMESPACE, "name": names[key], "facets": {k: v for k, v in facets.items() if v is not None}}

    # 1. read
    run_id, began = lineage.new_run_id(), time.perf_counter()
    schema = _facet("SchemaDatasetFacet", {"fields": [{"name": str(c), "type": _sql_type(frame[c].dtype)} for c in frame.columns]})
    raw = dataset("raw", schema=schema, dataSource=_facet("DatasourceDatasetFacet", {"name": item["file"], "uri": f"file://backend/examples/analysis/{item['file']}"}),
                  documentation=_facet("DocumentationDatasetFacet", {"description": f"{item['title']}，SHA-256 {sha[:12]}…"}))
    raw["outputFacets"] = {"outputStatistics": _facet("OutputStatisticsOutputDatasetFacet", {"rowCount": int(len(frame))})}
    end = clock + span(began)
    events += [_event("START", jobs["ingest"], run_id, clock), _event("COMPLETE", jobs["ingest"], run_id, end, outputs=[raw],
                                                                      job_facets={"documentation": _facet("DocumentationJobFacet", {"description": "把演示数据读成一张表"})})]
    clock = end

    # 2. health
    run_id, began = lineage.new_run_id(), time.perf_counter()
    report = health.validate(frame, suite, dataset_id=dataset_id)
    quality = {"dataQualityAssertions": _facet("DataQualityAssertionsDatasetFacet", {"assertions": _assertions(report)}),
               "dataQualityMetrics": _facet("DataQualityMetricsInputDatasetFacet", {"rowCount": int(len(frame)), "columnMetrics": _column_metrics(frame)})}
    checked = {"namespace": NAMESPACE, "name": names["raw"], "inputFacets": quality}
    produced = dataset("report", documentation=_facet("DocumentationDatasetFacet", {"description": f"{suite.get('name')}：{report['statistics']['successful_expectations']}/{report['statistics']['evaluated_expectations']} 条期望通过"}))
    end = clock + span(began)
    events += [_event("START", jobs["validate"], run_id, clock),
               _event("COMPLETE", jobs["validate"], run_id, end, inputs=[checked], outputs=[produced],
                      job_facets={"documentation": _facet("DocumentationJobFacet", {"description": "按期望逐条检查数据（照 Great Expectations）"})},
                      run_facets={"validation": _facet("ValidationRunFacet", {"success": report["success"], **report["statistics"]}, custom=True)})]
    clock = end

    # 3. resolve
    run_id, began = lineage.new_run_id(), time.perf_counter()
    result = resolution.run(frame, settings)
    used = [settings["unique_id"]] + [c["column"] for c in settings["comparisons"]]
    entity_schema = _facet("SchemaDatasetFacet", {"fields": [{"name": "entity_id", "type": "BIGINT"}] + [{"name": c, "type": "VARCHAR"} for c in used]})
    lineage_of_entity = _facet("ColumnLineageDatasetFacet", {"fields": {"entity_id": {"inputFields": [{"namespace": NAMESPACE, "name": names["raw"], "field": c} for c in used],
                                                                                      "transformationDescription": "匹配概率高于阈值的记录对，取连通分量"}}})
    summary, evaluation = result["summary"], result["evaluation"] or {}
    pairs = dataset("pairs", documentation=_facet("DocumentationDatasetFacet", {"description": "候选记录对及匹配权重"}))
    pairs["outputFacets"] = {"outputStatistics": _facet("OutputStatisticsOutputDatasetFacet", {"rowCount": summary["candidate_pairs"]})}
    entities = dataset("entities", schema=entity_schema, columnLineage=lineage_of_entity,
                       documentation=_facet("DocumentationDatasetFacet", {"description": f"消歧后的 {summary['entities_found']} 个实体"}))
    entities["outputFacets"] = {"outputStatistics": _facet("OutputStatisticsOutputDatasetFacet", {"rowCount": summary["entities_found"]})}
    end = clock + span(began)
    events += [_event("START", jobs["resolve"], run_id, clock),
               _event("COMPLETE", jobs["resolve"], run_id, end, inputs=[{"namespace": NAMESPACE, "name": names["raw"]}], outputs=[pairs, entities],
                      job_facets={"documentation": _facet("DocumentationJobFacet", {"description": "概率式记录匹配，识别同一实体的多条记录（照 Splink）"})},
                      run_facets={"resolution": _facet("ResolutionRunFacet", {"threshold": result["settings"]["threshold_match_probability"], **{k: evaluation[k] for k in ("precision", "recall", "f1") if k in evaluation},
                                                                              "entities_found": summary["entities_found"], "candidate_pairs": summary["candidate_pairs"]}, custom=True)})]

    rows = lineage.ingest(db, events, user_id, source="pipeline")
    return {
        "dataset_id": dataset_id, "title": item["title"], "sha256": sha,
        "steps": [
            {"key": "ingest", "title": "读入数据", "node": f"job:{NAMESPACE}:{jobs['ingest']}", "summary": {"rows": int(len(frame)), "columns": int(frame.shape[1])}},
            {"key": "validate", "title": "数据健康检查", "node": f"job:{NAMESPACE}:{jobs['validate']}", "summary": {**report["statistics"], "success": report["success"], "suite": suite.get("name")}},
            {"key": "resolve", "title": "实体消歧", "node": f"job:{NAMESPACE}:{jobs['resolve']}", "summary": {**{k: summary[k] for k in ("records", "candidate_pairs", "entities_found")}, **{k: evaluation[k] for k in ("precision", "recall", "f1") if k in evaluation}}},
        ],
        "health": report, "resolution": result,
        "lineage": {"namespace": NAMESPACE, "focus": f"dataset:{NAMESPACE}:{names['raw']}", "events": len(rows), "datasets": {k: f"dataset:{NAMESPACE}:{v}" for k, v in names.items()}},
    }
