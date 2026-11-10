"""Analysis after an ontology exists: data health, entity resolution, lineage, and the pipeline that chains them on one dataset."""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_editor
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError
from app.models.user import User
from app.models.v2.analysis import AnalysisRun, LineageEvent
from app.routers.v2.object_query import get_falkordb
from app.services.v2.analysis import demos, health, lineage, ontology_analysis, ontology_duplicates, ontology_lineage, ontology_metadata, ontology_resolution, ontology_source, pipeline, resolution

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
    if isinstance(error, ontology_source.SnapshotNotAllowed):
        return fail(403, "snapshot_not_allowed", str(error))
    if isinstance(error, ontology_source.OntologySourceError):
        return fail(404, "object_type_not_found", str(error))
    if isinstance(error, health.ExpectationError):
        return fail(422, "invalid_suite", str(error))
    if isinstance(error, resolution.NotApplicable):
        return fail(422, "resolution_not_applicable", str(error))
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


# ---- the whole platform at a glance --------------------------------------------------------------------------------------

@router.get("/overview")
def platform_overview(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """What is in the platform, for the home screen: the ontologies the caller may see with their sizes, the analysis runs, and the lineage."""
    from app.models.entity_instance import EntityInstance
    from app.models.relation import Relation
    from app.models.v2.ontology_candidates import OntologyCandidateSet
    from app.models.ontology import OntologyProject

    visible = []
    for project in db.query(OntologyProject).order_by(OntologyProject.created_at.desc()).all():
        try:
            require_ontology_access(db, project.id, str(user.id), user.role)
        except ObjectQueryError:
            continue
        visible.append(project)
    ids = [p.id for p in visible]
    objects = dict(db.query(EntityInstance.ontology_id, func.count(EntityInstance.id)).filter(EntityInstance.ontology_id.in_(ids), EntityInstance.is_deleted.is_(False)).group_by(EntityInstance.ontology_id).all()) if ids else {}
    links = dict(db.query(Relation.ontology_id, func.count(Relation.id)).filter(Relation.ontology_id.in_(ids)).group_by(Relation.ontology_id).all()) if ids else {}
    imported = {}
    for cset in db.query(OntologyCandidateSet).filter(OntologyCandidateSet.ontology_id.in_(ids), OntologyCandidateSet.status == "published").all() if ids else []:
        info = (cset.generator or {}).get("import")
        if info and cset.ontology_id not in imported:
            imported[cset.ontology_id] = {"tables": len(info.get("tables", [])), "sources": len(info.get("sources", [])), "kind": info.get("kind"), "replay": info.get("replay")}
    from app.models.entity import Entity
    types = dict(db.query(Entity.ontology_id, func.count(Entity.id)).filter(Entity.ontology_id.in_(ids), Entity.is_source_deleted.is_(False)).group_by(Entity.ontology_id).all()) if ids else {}
    ontologies = [{"id": p.id, "name": p.name, "domain": p.domain, "status": p.status, "build_mode": p.build_mode, "created_at": p.created_at.isoformat() if p.created_at else None,
                   "types": int(types.get(p.id, 0)), "objects": int(objects.get(p.id, 0)), "link_types": int(links.get(p.id, 0)), "imported": imported.get(p.id)} for p in visible]
    run_rows = db.query(AnalysisRun).order_by(AnalysisRun.created_at.desc()).limit(40).all()
    by_kind = dict(db.query(AnalysisRun.kind, func.count(AnalysisRun.id)).group_by(AnalysisRun.kind).all())
    health = [r.summary.get("success_percent") for r in run_rows if r.kind == "health" and isinstance(r.summary.get("success_percent"), (int, float))]
    lineage_jobs = db.query(LineageEvent.job_namespace, LineageEvent.job_name).filter(LineageEvent.source.startswith(ontology_lineage.SOURCE_PREFIX)).distinct().count()
    return {"ontologies": ontologies, "runs": [run_payload(r, full=False) for r in run_rows[:12]], "runs_by_kind": {k: int(v) for k, v in by_kind.items()},
            "health": {"average": round(sum(health) / len(health), 1) if health else None, "runs": len(health)}, "lineage_jobs": int(lineage_jobs),
            "totals": {"ontologies": len(ontologies), "types": sum(o["types"] for o in ontologies), "objects": sum(o["objects"] for o in ontologies), "link_types": sum(o["link_types"] for o in ontologies),
                       "imports": len(imported), "runs": int(sum(by_kind.values())), "lineage_jobs": int(lineage_jobs)}}


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


def execute_ontology_health(db: Session, user: User, graph: Any, ontology_id: str, body: OntologyHealthInput, loaded: tuple | None = None) -> tuple[AnalysisRun, dict]:
    suite, frame, info = ontology_analysis.suite_for(db, user, graph, ontology_id, body.object_type, baseline=body.baseline, loaded=loaded)
    suite = body.suite or suite
    report = ontology_analysis.run_health(frame, suite, dataset=ontology_analysis.dataset_id(ontology_id, body.object_type))
    report["source"] = {"kind": "ontology", "ontology_id": ontology_id, "object_type": body.object_type, "read": info}
    summary = {**report["statistics"], "object_count": report["meta"]["object_count"]}
    params = {"ontology_id": ontology_id, "object_type": body.object_type, "custom_suite": body.suite is not None, "baseline": body.baseline}
    row = store(db, user, "health", ontology_analysis.dataset_id(ontology_id, body.object_type), f"{body.object_type} · 数据健康（本体）", params, summary, report)
    return row, {"frame": frame, "info": info}


@router.post("/ontologies/{ontology_id}/health/run")
def run_ontology_health(ontology_id: str, body: OntologyHealthInput, db: Session = Depends(get_db), user: User = Depends(require_editor), graph=Depends(get_falkordb)):
    try:
        with Busy():
            row, _ = execute_ontology_health(db, user, graph, ontology_id, body)
    except KNOWN as error:
        raise translate(error) from error
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
        keys = [fk["property"] for fk in ontology_metadata.foreign_keys(db, ontology_id, object_type)]
        return {**ontology_resolution.suggest(frame, description, ground_truth=ground_truth, references=keys), "read": info}
    except KNOWN as error:
        raise translate(error) from error


def execute_ontology_resolution(db: Session, user: User, graph: Any, ontology_id: str, body: OntologyResolutionInput, loaded: tuple | None = None) -> AnalysisRun:
    frame, description, info = loaded or ontology_source.load_frame(db, user, graph, ontology_id, body.object_type)
    keys = [] if body.settings else [fk["property"] for fk in ontology_metadata.foreign_keys(db, ontology_id, body.object_type)]
    drafted = None if body.settings else ontology_resolution.suggest(frame, description, ground_truth=body.ground_truth, references=keys)
    result = ontology_resolution.run(frame, body.settings or drafted["settings"])
    notes = list(drafted["notes"]) if drafted else []
    biggest, records = result["summary"]["largest_cluster"], result["summary"]["records"]
    if records and biggest >= max(25, records // 20):  # one group holding a twentieth of everything: the comparison is too loose and has chained different things together
        notes.insert(0, f"注意：最大的一组有 {biggest:,} 个对象（占全部的 {biggest / records:.0%}），多半是比较条件太宽松，把不同的对象连成了一串；调高匹配概率阈值或收紧粗筛规则再看")
    result["source"] = {"kind": "ontology", "ontology_id": ontology_id, "object_type": body.object_type, "read": info, "notes": notes}
    summary = {**result["summary"], **{k: result["evaluation"][k] for k in ("precision", "recall", "f1")}} if result["evaluation"] else dict(result["summary"])
    params = {"ontology_id": ontology_id, "object_type": body.object_type, "custom_settings": body.settings is not None}
    return store(db, user, "resolution", ontology_analysis.dataset_id(ontology_id, body.object_type), f"{body.object_type} · 实体消歧（本体）", params, summary, result)


@router.post("/ontologies/{ontology_id}/resolution/run")
def run_ontology_resolution(ontology_id: str, body: OntologyResolutionInput, db: Session = Depends(get_db), user: User = Depends(require_editor), graph=Depends(get_falkordb)):
    try:
        with Busy():
            row = execute_ontology_resolution(db, user, graph, ontology_id, body)
    except KNOWN as error:
        raise translate(error) from error
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

class OntologyPipelineInput(BaseModel):
    object_type: str = Field(min_length=1, max_length=120)


@router.post("/ontologies/{ontology_id}/pipeline/run")
def run_ontology_pipeline(ontology_id: str, body: OntologyPipelineInput, db: Session = Depends(get_db), user: User = Depends(require_editor), graph=Depends(get_falkordb)):
    """One object type through all three: its objects are checked against what the ontology promises, grouped by entity resolution, and the lineage
    of the ontology is rewritten so the two runs hang off the type they were made on."""
    scope(db, ontology_id, user)
    try:
        with Busy():
            frame, description, info = ontology_source.load_frame(db, user, graph, ontology_id, body.object_type)
            health_row, _ = execute_ontology_health(db, user, graph, ontology_id, OntologyHealthInput(object_type=body.object_type), loaded=(frame, description, info))
            try:
                resolution_row = execute_ontology_resolution(db, user, graph, ontology_id, OntologyResolutionInput(object_type=body.object_type), loaded=(frame, description, info))
                not_applicable = None
            except resolution.NotApplicable as error:  # the type has nothing to tell individuals apart: the other two steps still run
                resolution_row, not_applicable = None, str(error)
            written = ontology_lineage.generate(db, user, ontology_id)
    except KNOWN as error:
        db.rollback()
        raise translate(error) from error
    ns = written["namespace"]
    report, result = health_row.result, resolution_row.result if resolution_row else None
    evaluation = {k: result["evaluation"][k] for k in ("precision", "recall", "f1")} if result and result["evaluation"] else {}
    resolve_summary = {**{k: result["summary"][k] for k in ("records", "candidate_pairs", "entities_found")}, **evaluation} if result else {"skipped": True, "reason": not_applicable}
    steps = [{"key": "ingest", "title": "读取对象", "node": f"dataset:{ns}:{body.object_type}", "summary": {"rows": int(len(frame)), "columns": len(description["properties"])}},
             {"key": "validate", "title": "数据健康检查", "node": f"job:{ns}:validate.{body.object_type}", "summary": {**report["statistics"], "success": report["success"], "suite": report["meta"].get("suite_name")}},
             {"key": "resolve", "title": "实体消歧", "node": f"job:{ns}:resolve.{body.object_type}", "summary": resolve_summary}]
    outcome = {"dataset_id": ontology_analysis.dataset_id(ontology_id, body.object_type), "title": f"{body.object_type}（本体）", "sha256": None,
               "source": {"kind": "ontology", "ontology_id": ontology_id, "object_type": body.object_type, "read": info}, "steps": steps, "health": report, "resolution": result,
               "runs": {"health": health_row.id, "resolution": resolution_row.id if resolution_row else None},
               "lineage": {"namespace": ns, "focus": f"dataset:{ns}:{body.object_type}", "events": written["events"], "ontology_id": ontology_id}}
    row = store(db, user, "pipeline", outcome["dataset_id"], f"{body.object_type} · 流水线（本体）", {"ontology_id": ontology_id, "object_type": body.object_type},
                {s["key"]: s["summary"] for s in steps}, outcome)
    return run_payload(row)


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
