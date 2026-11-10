"""Analysis after an ontology exists: data health, entity resolution, lineage, and the pipeline that chains them on one dataset."""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_editor
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError
from app.models.user import User
from app.models.v2.analysis import AnalysisRun
from app.routers.v2.object_query import get_falkordb
from app.services.v2.analysis import demos, health, lineage, ontology_analysis, ontology_duplicates, ontology_lineage, ontology_resolution, ontology_source, pipeline, resolution

router = APIRouter(dependencies=[Depends(get_current_user)])

# The engines run in the request on demo-sized data; one heavy run at a time keeps a second click from piling on.
_BUSY = threading.Lock()
SEED_SOURCE = "demo:marquez"


def fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def scope(db: Session, ontology_id: str, user: User) -> None:
    """The caller must be able to see the ontology; its lineage is as private as its objects."""
    try:
        require_ontology_access(db, ontology_id, str(user.id), user.role)
    except ObjectQueryError:
        raise fail(404, "ontology_not_found", "Ontology not found or inaccessible") from None


def translate(error: Exception) -> HTTPException:
    if isinstance(error, demos.DemoError):
        return fail(404 if str(error).startswith("unknown") else 409, "demo_unavailable", str(error))
    if isinstance(error, ontology_source.OntologySourceError):
        return fail(404, "object_type_not_found", str(error))
    if isinstance(error, health.ExpectationError):
        return fail(422, "invalid_suite", str(error))
    if isinstance(error, resolution.ResolutionError):
        return fail(422, "invalid_resolution_settings", str(error))
    return fail(422, "invalid_lineage_event", str(error))


KNOWN = (demos.DemoError, ontology_source.OntologySourceError, health.ExpectationError, resolution.ResolutionError, lineage.LineageError)


class HealthRunInput(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=80)
    suite: dict[str, Any] | None = None


class ResolutionRunInput(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=80)
    settings: dict[str, Any] | None = None


class PipelineRunInput(BaseModel):
    dataset_id: str = Field(default="splink-fake-1000", min_length=1, max_length=80)


class OntologyHealthInput(BaseModel):
    object_type: str = Field(min_length=1, max_length=120)
    suite: dict[str, Any] | None = None
    baseline: bool = False


class OntologyResolutionInput(BaseModel):
    object_type: str = Field(min_length=1, max_length=120)
    settings: dict[str, Any] | None = None
    ground_truth: str | None = None


class SeedInput(BaseModel):
    dataset_id: str = Field(default="marquez-food-delivery", min_length=1, max_length=80)


def run_payload(row: AnalysisRun, *, full: bool = True) -> dict[str, Any]:
    out = {"id": row.id, "kind": row.kind, "dataset_id": row.dataset_id, "title": row.title, "summary": row.summary, "params": row.params,
           "created_by": row.created_by, "created_at": row.created_at.isoformat() if row.created_at else None}
    if full:
        out["result"] = row.result
    return out


def store(db: Session, user: User, kind: str, dataset_id: str, title: str, params: dict, summary: dict, result: dict) -> AnalysisRun:
    row = AnalysisRun(kind=kind, dataset_id=dataset_id, title=title, params=params, summary=summary, result=result, created_by=user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class Busy:
    def __enter__(self):
        if not _BUSY.acquire(blocking=False):
            raise fail(409, "busy", "另一个分析还在运行，请等它结束")

    def __exit__(self, *exc):
        _BUSY.release()
        return False


# ---- demo datasets ---------------------------------------------------------------------------------------------------

@router.get("/demos")
def list_demos():
    return {"datasets": demos.list_demos()}


@router.get("/demos/{dataset_id}/preview")
def preview(dataset_id: str, limit: int = Query(20, ge=1, le=50)):
    try:
        return demos.preview(dataset_id, limit)
    except KNOWN as error:
        raise translate(error) from error


# ---- data health -----------------------------------------------------------------------------------------------------

@router.get("/health/suites/{dataset_id}")
def default_suite(dataset_id: str, baseline: bool = False):
    """The suite a demo is run with, or (baseline=true) one read off the data as it is now."""
    try:
        suite = health.suggest_suite(demos.load_frame(dataset_id), name=f"{dataset_id}_baseline") if baseline else demos.health_suite(dataset_id)
        return {"suite": suite, "expectation_types": sorted(health.EXPECTATIONS)}
    except KNOWN as error:
        raise translate(error) from error


@router.post("/health/run")
def run_health(body: HealthRunInput, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    try:
        frame = demos.load_frame(body.dataset_id)
        suite = body.suite or demos.health_suite(body.dataset_id)
        with Busy():
            report = health.validate(frame, suite, dataset_id=body.dataset_id)
    except KNOWN as error:
        raise translate(error) from error
    title = f"{demos.entry(body.dataset_id)['title']} · 数据健康"
    row = store(db, user, "health", body.dataset_id, title, {"suite_name": suite.get("name"), "custom_suite": body.suite is not None}, report["statistics"], report)
    return run_payload(row)


# ---- the platform's own ontologies -------------------------------------------------------------------------------------

@router.get("/ontologies/{ontology_id}/object-types")
def ontology_object_types(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user), graph=Depends(get_falkordb)):
    """The object types of one ontology that the caller may see, with their properties and how many objects each has."""
    types = ontology_source.object_types(db, user, ontology_id)
    counts = ontology_source.object_counts(db, user, graph, ontology_id, [t["api_name"] for t in types])
    return {"ontology_id": ontology_id, "types": [{**t, "object_count": (counts.get(t["api_name"]) or {}).get("count"), "object_count_capped": (counts.get(t["api_name"]) or {}).get("capped", False)}
                                                  for t in types]}


@router.get("/ontologies/{ontology_id}/health/suites/{object_type}")
def ontology_suite(ontology_id: str, object_type: str, baseline: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user), graph=Depends(get_falkordb)):
    """The suite the ontology implies for one object type (plus, with baseline=true, what the current data suggests)."""
    try:
        suite, _, info = ontology_analysis.suite_for(db, user, graph, ontology_id, object_type, baseline=baseline)
    except KNOWN as error:
        raise translate(error) from error
    return {"suite": suite, "read": info, "expectation_types": sorted(health.EXPECTATIONS)}


@router.post("/ontologies/{ontology_id}/health/run")
def run_ontology_health(ontology_id: str, body: OntologyHealthInput, db: Session = Depends(get_db), user: User = Depends(require_editor), graph=Depends(get_falkordb)):
    try:
        with Busy():
            suite, frame, info = ontology_analysis.suite_for(db, user, graph, ontology_id, body.object_type, baseline=body.baseline)
            suite = body.suite or suite
            report = ontology_analysis.run_health(frame, suite, dataset=ontology_analysis.dataset_id(ontology_id, body.object_type))
    except KNOWN as error:
        raise translate(error) from error
    report["source"] = {"kind": "ontology", "ontology_id": ontology_id, "object_type": body.object_type, "read": info}
    summary = {**report["statistics"], "object_count": report["meta"]["object_count"]}
    params = {"ontology_id": ontology_id, "object_type": body.object_type, "custom_suite": body.suite is not None, "baseline": body.baseline}
    row = store(db, user, "health", ontology_analysis.dataset_id(ontology_id, body.object_type), f"{body.object_type} · 数据健康（本体）", params, summary, report)
    return run_payload(row)


@router.get("/ontologies/{ontology_id}/duplicate-types")
def ontology_duplicate_types(ontology_id: str, candidate_set: str | None = None, minimum: float = Query(ontology_duplicates.POSSIBLE, ge=0.2, le=1.0),
                             db: Session = Depends(get_db), user: User = Depends(get_current_user), graph=Depends(get_falkordb)):
    """Object types that look like one thing described twice; with candidate_set, also the candidates that repeat an existing type."""
    try:
        return ontology_duplicates.analyse(db, user, graph, ontology_id, candidate_set=candidate_set, minimum=minimum)
    except KNOWN as error:
        raise translate(error) from error


# ---- entity resolution -----------------------------------------------------------------------------------------------

@router.get("/resolution/presets/{dataset_id}")
def resolution_preset(dataset_id: str):
    try:
        return {"settings": demos.resolution_settings(dataset_id)}
    except KNOWN as error:
        raise translate(error) from error


@router.post("/resolution/run")
def run_resolution(body: ResolutionRunInput, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    try:
        frame = demos.load_frame(body.dataset_id)
        settings = body.settings or demos.resolution_settings(body.dataset_id)
        with Busy():
            result = resolution.run(frame, settings)
    except KNOWN as error:
        raise translate(error) from error
    summary = {**result["summary"], **{k: result["evaluation"][k] for k in ("precision", "recall", "f1")} } if result["evaluation"] else dict(result["summary"])
    title = f"{demos.entry(body.dataset_id)['title']} · 实体消歧"
    row = store(db, user, "resolution", body.dataset_id, title, {"custom_settings": body.settings is not None}, summary, result)
    return run_payload(row)


@router.get("/ontologies/{ontology_id}/resolution/presets/{object_type}")
def ontology_resolution_preset(ontology_id: str, object_type: str, ground_truth: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),
                               graph=Depends(get_falkordb)):
    """Comparison settings drafted from the type's properties, with the reason for each choice. ground_truth="" scores nothing."""
    try:
        frame, description, info = ontology_source.load_frame(db, user, graph, ontology_id, object_type)
        return {**ontology_resolution.suggest(frame, description, ground_truth=ground_truth), "read": info}
    except KNOWN as error:
        raise translate(error) from error


@router.post("/ontologies/{ontology_id}/resolution/run")
def run_ontology_resolution(ontology_id: str, body: OntologyResolutionInput, db: Session = Depends(get_db), user: User = Depends(require_editor), graph=Depends(get_falkordb)):
    try:
        with Busy():
            frame, description, info = ontology_source.load_frame(db, user, graph, ontology_id, body.object_type)
            drafted = None if body.settings else ontology_resolution.suggest(frame, description, ground_truth=body.ground_truth)
            settings = body.settings or drafted["settings"]
            result = ontology_resolution.run(frame, settings)
    except KNOWN as error:
        raise translate(error) from error
    result["source"] = {"kind": "ontology", "ontology_id": ontology_id, "object_type": body.object_type, "read": info, "notes": drafted["notes"] if drafted else []}
    summary = {**result["summary"], **{k: result["evaluation"][k] for k in ("precision", "recall", "f1")}} if result["evaluation"] else dict(result["summary"])
    params = {"ontology_id": ontology_id, "object_type": body.object_type, "custom_settings": body.settings is not None}
    row = store(db, user, "resolution", ontology_analysis.dataset_id(ontology_id, body.object_type), f"{body.object_type} · 实体消歧（本体）", params, summary, result)
    return run_payload(row)


# ---- lineage ---------------------------------------------------------------------------------------------------------

@router.post("/lineage/events", status_code=201)
def post_events(payload: Any = Body(...), db: Session = Depends(get_db), user: User = Depends(require_editor)):
    """An OpenLineage run event, or a list of them."""
    try:
        rows = lineage.ingest(db, payload, user.id, source="api")
    except KNOWN as error:
        db.rollback()
        raise translate(error) from error
    return {"accepted": len(rows)}


@router.post("/lineage/seed")
def seed(body: SeedInput = Body(default_factory=SeedInput), db: Session = Depends(get_db), user: User = Depends(require_editor)):
    try:
        text = demos.load_events_text(body.dataset_id)
        count = lineage.seed_from_text(db, text, user.id, source=SEED_SOURCE)
    except KNOWN as error:
        db.rollback()
        raise translate(error) from error
    return {"events": count, "source": SEED_SOURCE, "namespace": "food_delivery"}


@router.get("/lineage/graph")
def lineage_graph(namespace: str | None = None, node: str | None = None, depth: int = Query(3, ge=1, le=12), direction: str = "both", db: Session = Depends(get_db)):
    graph = lineage.graph_from_db(db, namespace=namespace, exclude_source_prefix=ontology_lineage.SOURCE_PREFIX)
    if node:
        try:
            focused = lineage.subgraph(graph, node, depth=depth, direction=direction)
        except KNOWN as error:
            raise fail(404 if "unknown node" in str(error) else 422, "lineage_node", str(error)) from error
        focused["event_count"] = graph["event_count"]
        return focused
    return graph


@router.get("/lineage/impact")
def lineage_impact(node: str, namespace: str | None = None, db: Session = Depends(get_db)):
    try:
        return lineage.impact(lineage.graph_from_db(db, namespace=namespace, exclude_source_prefix=ontology_lineage.SOURCE_PREFIX), node)
    except KNOWN as error:
        raise fail(404, "lineage_node", str(error)) from error


@router.delete("/lineage")
def lineage_reset(namespace: str | None = None, source: str | None = None, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    if not namespace and not source:
        raise fail(400, "filter_required", "give a namespace or a source; deleting every event is not offered")
    return {"deleted": lineage.reset(db, namespace=namespace, source=source)}


@router.post("/ontologies/{ontology_id}/lineage/generate")
def generate_ontology_lineage(ontology_id: str, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    """Write the lineage of one ontology from its metadata (Auto Ontology builds, rules, alerts, decision cases, analysis runs), replacing the earlier copy."""
    scope(db, ontology_id, user)
    try:
        return ontology_lineage.generate(db, user, ontology_id)
    except KNOWN as error:
        db.rollback()
        raise translate(error) from error


@router.get("/ontologies/{ontology_id}/lineage/graph")
def ontology_lineage_graph(ontology_id: str, node: str | None = None, depth: int = Query(3, ge=1, le=12), direction: str = "both", db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    scope(db, ontology_id, user)
    graph = lineage.graph_from_db(db, source=ontology_lineage.source_tag(ontology_id))
    graph["layers"] = ontology_lineage.LAYERS
    if node:
        try:
            focused = lineage.subgraph(graph, node, depth=depth, direction=direction)
        except KNOWN as error:
            raise fail(404 if "unknown node" in str(error) else 422, "lineage_node", str(error)) from error
        focused.update(event_count=graph["event_count"], layers=graph["layers"])
        return focused
    return graph


@router.get("/ontologies/{ontology_id}/lineage/impact")
def ontology_lineage_impact(ontology_id: str, node: str, field: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """What depends on a dataset, or, with `field`, on one column of it."""
    scope(db, ontology_id, user)
    graph = lineage.graph_from_db(db, source=ontology_lineage.source_tag(ontology_id))
    try:
        out = lineage.impact(graph, node)
        if field:
            out["column"] = lineage.column_impact(graph, node, field)
        return out
    except KNOWN as error:
        raise fail(404, "lineage_node", str(error)) from error


# ---- all three on one dataset ----------------------------------------------------------------------------------------

@router.post("/pipeline/run")
def run_pipeline(body: PipelineRunInput, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    try:
        with Busy():
            outcome = pipeline.run(db, user.id, body.dataset_id)
    except KNOWN as error:
        db.rollback()
        raise translate(error) from error
    steps = {s["key"]: s["summary"] for s in outcome["steps"]}
    row = store(db, user, "pipeline", body.dataset_id, f"{outcome['title']} · 流水线", {}, steps, outcome)
    return run_payload(row)


# ---- runs ------------------------------------------------------------------------------------------------------------

@router.get("/runs")
def list_runs(kind: str | None = None, dataset_id: str | None = None, limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    query = db.query(AnalysisRun)
    if kind:
        query = query.filter(AnalysisRun.kind == kind)
    if dataset_id:
        query = query.filter(AnalysisRun.dataset_id == dataset_id)
    return {"runs": [run_payload(r, full=False) for r in query.order_by(AnalysisRun.created_at.desc()).limit(limit).all()]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)):
    row = db.get(AnalysisRun, run_id)
    if row is None:
        raise fail(404, "run_not_found", "no such run")
    return run_payload(row)
