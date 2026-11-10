"""Run an import as a job whose steps can be watched: what was read, what was inferred, what was written, and how long each took."""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import func

from app.database import SessionLocal
from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.user import User
from app.services.v2.analysis import ontology_lineage, ontology_metadata
from app.services.v2.data_import import install, profile, schema as schema_mod
from app.services.v2.data_import.install import ImportConflict, ImportTooLarge
from app.services.v2.data_import.model import Bundle
from app.services.v2.graph.falkordb_service import FalkorDBService

STEPS = [("read", "读取数据"), ("profile", "识别类型、主键和关系"), ("ontology", "创建本体"), ("schema", "发布对象类型和关系"), ("objects", "写入对象"), ("links", "建立关系"), ("verify", "核对结果"), ("lineage", "生成数据血缘")]
_LOCK = threading.Lock()   # one import at a time: each writes thousands of rows and graph nodes
JOBS: dict[str, "Job"] = {}
MAX_JOBS = 40


@dataclass
class Job:
    id: str
    title: str
    status: str = "queued"           # queued | running | done | failed
    steps: list[dict[str, Any]] = field(default_factory=lambda: [{"key": k, "title": t, "status": "pending", "detail": None, "seconds": None} for k, t in STEPS])
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def step(self, key: str, status: str, detail: Any = None) -> None:
        row = next(s for s in self.steps if s["key"] == key)
        now = time.time()
        if status == "running":
            row["_t0"] = now
        if status in ("done", "failed") and row.get("_t0"):
            row["seconds"] = round(now - row["_t0"], 2)
        row["status"] = status
        if detail is not None:
            row["detail"] = detail

    def note(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {text}")
        del self.log[:-200]

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "steps": [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.steps],
                "log": self.log[-40:], "result": self.result, "error": self.error, "started_at": self.started_at, "finished_at": self.finished_at}


def get(job_id: str) -> Job | None:
    return JOBS.get(job_id)


def _remember(job: Job) -> None:
    JOBS[job.id] = job
    for old in list(JOBS)[:-MAX_JOBS]:
        JOBS.pop(old, None)


def start(title: str, user_id: str, load: Callable[[], Bundle], *, replace: bool = False) -> Job:
    if _LOCK.locked():
        raise RuntimeError("另一个导入还在运行，请等它结束")
    job = Job(id=str(uuid.uuid4()), title=title)
    _remember(job)
    threading.Thread(target=_work, args=(job, user_id, load, replace), daemon=True).start()
    return job


def _work(job: Job, user_id: str, load: Callable[[], Bundle], replace: bool) -> None:
    with _LOCK:
        job.status, job.started_at = "running", time.time()
        db = SessionLocal()
        try:
            job.result = run(db, db.get(User, user_id), load, replace=replace, job=job)
            job.status = "done"
        except ImportConflict as exc:
            job.status, job.error = "failed", f"{exc}。可以选择“替换”重新导入，或先到本体库查看。"
            job.result = {"conflict_ontology_id": exc.ontology_id}
        except (ImportTooLarge, ValueError) as exc:
            job.status, job.error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001  shown to the user with the step that failed; the traceback stays in the log
            db.rollback()
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
            job.note(traceback.format_exc(limit=3).replace("\n", " | "))
        finally:
            for row in job.steps:
                if row["status"] == "running":
                    row["status"] = "failed"
            job.finished_at = time.time()
            db.close()


def run(db, user: User, load: Callable[[], Bundle], *, replace: bool, job: Job | None = None) -> dict[str, Any]:
    job = job or Job(id="inline", title="")

    def stage(key: str, fn: Callable[[], Any]) -> Any:
        job.step(key, "running")
        try:
            out = fn()
        except Exception:
            job.step(key, "failed")
            raise
        return out

    bundle = stage("read", lambda: load())
    job.step("read", "done", f"{len(bundle.tables)} 张表，{sum(len(t.frame) for t in bundle.tables if t.kind not in ('bridge', 'stage')):,} 行，来自 {len(bundle.sources)} 个数据源")
    job.note(f"读取完成：{', '.join(t.name for t in bundle.tables)}")
    report = stage("profile", lambda: profile.finalize(bundle))
    job.step("profile", "done", f"{report['links']} 个关系；" + "；".join(report["notes"][:3]))
    for line in report["notes"]:
        job.note(line)
    install.check_size(bundle)
    ontology = stage("ontology", lambda: install.create_ontology(db, user, bundle, replace=replace))
    job.step("ontology", "done", f"{ontology.name}（{ontology.id[:8]}）")
    for table in bundle.tables:
        if table.kind == "asset" and "asset_id" in table.frame.columns:
            table.frame["preview_url"] = [f"/api/v2/ontology-imports/{ontology.id}/assets/{a}" for a in table.frame["asset_id"]]
    provenance = schema_mod.provenance(bundle, report)
    cset = stage("schema", lambda: install.publish_schema(db, user, ontology, bundle, provenance))
    constraints = install.apply_constraints(db, ontology.id, bundle)
    entities = ontology_metadata.type_entities(db, ontology.id)
    job.step("schema", "done", f"{len(entities)} 个对象类型、{len(bundle.links)} 个关系" + (f"，{constraints} 条业务规则" if constraints else ""))

    graph = FalkorDBService()
    if not graph.available:
        raise RuntimeError("图数据库（FalkorDB）不可用，没有地方存放对象之间的关系")
    job.step("objects", "running")
    total = 0
    slices: dict[str, Any] = {}
    for table in bundle.tables:
        if table.kind in ("bridge", "stage"):
            continue
        frame = table.frame
        if bundle.replay and bundle.replay.table == table.name:
            frame = table.frame.iloc[:bundle.replay.initial]
            slices[table.name] = frame
        written = install.install_table(db, graph, ontology.id, entities[table.type_name], table, frame, None)
        total += written
        job.note(f"{table.type_name}：写入 {written:,} 个对象")
        job.step("objects", "running", f"{total:,} 个对象（{table.type_name}）")
    job.step("objects", "done", f"{total:,} 个对象")
    edges = stage("links", lambda: install.install_links(db, graph, ontology.id, bundle, slices))
    job.step("links", "done", f"{edges:,} 条关系实例")
    job.note(f"关系实例：{edges:,}")

    def verify() -> dict[str, int]:
        rows = (db.query(Entity.name_en, func.count(EntityInstance.id)).join(EntityInstance, EntityInstance.entity_id == Entity.id)
                .filter(Entity.ontology_id == ontology.id).group_by(Entity.name_en).all())
        return {name: int(count) for name, count in rows}

    counts = stage("verify", verify)
    ontology.status = "created"
    db.commit()
    job.step("verify", "done", f"{sum(counts.values()):,} 个对象写入数据库，{len(counts)} 个类型都有对象")
    lineage_summary = _lineage(db, user, ontology.id, job)
    return {"ontology_id": ontology.id, "ontology_name": ontology.name, "candidate_set_id": cset.id, "objects": sum(counts.values()), "types": len(counts), "links": len(bundle.links),
            "link_instances": edges, "counts": counts, "replay": bool(bundle.replay), "bundle": bundle.id, "lineage": lineage_summary}


def _lineage(db, user: User, ontology_id: str, job: Job) -> dict[str, Any] | None:
    """Draw the lineage of what was just built. It is a convenience: the objects are already there, so a failure is reported but does not fail the import."""
    job.step("lineage", "running")
    try:
        summary = ontology_lineage.generate(db, user, ontology_id)
    except Exception as error:  # noqa: BLE001
        db.rollback()
        job.step("lineage", "failed", f"没有生成：{error}；可以到数据血缘页再点“生成血缘”")
        job.note(f"数据血缘没有生成：{error}")
        return None
    jobs = sum(summary["jobs"].values()) if isinstance(summary.get("jobs"), dict) else 0
    job.step("lineage", "done", f"{summary['events']} 条运行事件，{jobs} 个作业")
    job.note(f"数据血缘：{summary['events']} 条运行事件")
    return {"events": summary["events"], "jobs": jobs}



def load_again(provenance: dict[str, Any]) -> Bundle:
    """The bundle an ontology was imported from, read again: a prepared bundle from the catalog, an upload from the storage it was kept in."""
    from app.services.storage_service import get_storage_service
    from app.services.v2.data_import import catalog, uploads

    bundle_id = str(provenance["bundle"])
    if bundle_id.startswith("upload:"):
        storage = get_storage_service()
        files = [(s["title"], storage.get_object(s["location"])) for s in provenance["sources"] if s.get("location")]
        replay = provenance.get("replay")
        return uploads.build(bundle_id.split(":", 1)[1], provenance["title"], provenance["title"], files, None,
                             replay={"table": replay["table"], "order_by": replay["order_by"], "initial": replay["initial"], "batch": replay["batch"]} if replay else None)
    entry = catalog.get(bundle_id)
    if entry is None:
        raise ValueError(f"找不到导入这个本体的数据：{bundle_id}")
    return entry.load()


def advance(db, user: User, ontology_id: str, count: int | None = None) -> dict[str, Any]:
    """Load the next events of a replayed import: objects for the next rows of its event table, and their links."""
    from app.models.v2.ontology_candidates import OntologyCandidateSet

    cset = next((c for c in db.query(OntologyCandidateSet).filter(OntologyCandidateSet.ontology_id == ontology_id, OntologyCandidateSet.status == "published")
                 .order_by(OntologyCandidateSet.created_at.desc()).all() if ((c.generator or {}).get("import") or {}).get("replay")), None)
    if cset is None:
        raise ValueError("这个本体不是逐步载入的")
    provenance = dict(cset.generator["import"])
    replay = dict(provenance["replay"])
    bundle = load_again(provenance)
    profile.finalize(bundle)
    table = bundle.table(replay["table"])
    cursor, step = int(replay["cursor"]), int(count or replay["batch"])
    chunk = table.frame.iloc[cursor:cursor + step]
    if chunk.empty:
        return {"cursor": cursor, "total": replay["total"], "installed": 0, "done": True}
    graph = FalkorDBService()
    if not graph.available:
        raise RuntimeError("图数据库（FalkorDB）不可用")
    entities = ontology_metadata.type_entities(db, ontology_id)
    installed = install.install_table(db, graph, ontology_id, entities[table.type_name], table, chunk)
    edges = install.install_links(db, graph, ontology_id, bundle, {table.name: chunk}, only_slices=True)
    replay["cursor"] = cursor + len(chunk)
    provenance["replay"] = replay
    cset.generator = {**cset.generator, "import": provenance}
    db.commit()
    return {"cursor": replay["cursor"], "total": replay["total"], "installed": installed, "link_instances": edges, "done": replay["cursor"] >= replay["total"]}
