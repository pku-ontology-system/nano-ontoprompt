"""Builders for the OpenLineage documents this platform writes: events, facets, and the two facets of its own (layer and reference)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

PRODUCER = "urn:workbench:analysis"
FACET_URL = "https://openlineage.io/spec/facets/1-0-0/{}.json"
WORKBENCH_FACET_URL = "urn:workbench:facet:{}"


def facet(schema: str, body: dict[str, Any], *, custom: bool = False, producer: str = PRODUCER) -> dict[str, Any]:
    return {"_producer": producer, "_schemaURL": (WORKBENCH_FACET_URL if custom else FACET_URL).format(schema), **body}


def layer(name: str, producer: str = PRODUCER) -> dict[str, Any]:
    """Which part of the chain a job or dataset belongs to (source, build, object, rule, alert, decision, analysis); the lineage view colours by it."""
    return {"workbenchLayer": facet("WorkbenchLayerFacet", {"layer": name}, custom=True, producer=producer)}


def ref(kind: str, **values: Any) -> dict[str, Any]:
    """Where in the platform the thing lives (an object type, a rule, a candidate set, a run), so the lineage view can link to it."""
    return {"workbenchRef": facet("WorkbenchRefFacet", {"kind": kind, **values}, custom=True)}


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def event(kind: str, namespace: str, job: str, run_id: str, when: datetime, *, inputs: list | None = None, outputs: list | None = None,
          run_facets: dict | None = None, job_facets: dict | None = None, producer: str = PRODUCER) -> dict[str, Any]:
    return {"eventType": kind, "eventTime": stamp(when), "run": {"runId": run_id, "facets": run_facets or {}},
            "job": {"namespace": namespace, "name": job, "facets": job_facets or {}}, "inputs": inputs or [], "outputs": outputs or [], "producer": producer}


def assertions(report: dict) -> list[dict]:
    """The results of a health report as the `dataQualityAssertions` facet lists them."""
    out = []
    for result in report["results"]:
        kwargs = result["kwargs"]
        column = kwargs.get("column") or (", ".join(kwargs["column_list"]) if kwargs.get("column_list") else None)
        out.append({"assertion": result["expectation_type"], "success": bool(result["success"]), "column": column})
    return out
