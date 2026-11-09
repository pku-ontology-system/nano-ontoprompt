"""Analysis after an ontology exists: data health, entity resolution, lineage, and the pipeline that chains them on one dataset."""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_editor
from app.models.user import User
from app.models.v2.analysis import AnalysisRun
from app.services.v2.analysis import demos, health, lineage, pipeline, resolution

router = APIRouter(dependencies=[Depends(get_current_user)])

# The engines run in the request on demo-sized data; one heavy run at a time keeps a second click from piling on.
_BUSY = threading.Lock()
SEED_SOURCE = "demo:marquez"


def fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def translate(error: Exception) -> HTTPException:
    if isinstance(error, demos.DemoError):
        return fail(404 if str(error).startswith("unknown") else 409, "demo_unavailable", str(error))
    if isinstance(error, health.ExpectationError):
        return fail(422, "invalid_suite", str(error))
    if isinstance(error, resolution.ResolutionError):
        return fail(422, "invalid_resolution_settings", str(error))
    return fail(422, "invalid_lineage_event", str(error))


KNOWN = (demos.DemoError, health.ExpectationError, resolution.ResolutionError, lineage.LineageError)


class HealthRunInput(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=80)
    suite: dict[str, Any] | None = None


class ResolutionRunInput(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=80)
    settings: dict[str, Any] | None = None


class PipelineRunInput(BaseModel):
    dataset_id: str = Field(default="splink-fake-1000", min_length=1, max_length=80)


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
    graph = lineage.graph_from_db(db, namespace=namespace)
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
        return lineage.impact(lineage.graph_from_db(db, namespace=namespace), node)
    except KNOWN as error:
        raise fail(404, "lineage_node", str(error)) from error


@router.delete("/lineage")
def lineage_reset(namespace: str | None = None, source: str | None = None, db: Session = Depends(get_db), user: User = Depends(require_editor)):
    if not namespace and not source:
        raise fail(400, "filter_required", "give a namespace or a source; deleting every event is not offered")
    return {"deleted": lineage.reset(db, namespace=namespace, source=source)}


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
