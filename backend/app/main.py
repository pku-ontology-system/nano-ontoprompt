"""
OntoPrompt API v2

架构：FastAPI + PostgreSQL + Neo4j + ChromaDB + MinIO + Celery/Redis
v2 新增：Pipelines 全链路（Connection→Dataset→Transform→Curated→Mapping）
v1 兼容：/api/v1/* 路由全部保留

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from fastapi import FastAPI, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
import socket
from threading import Lock
from time import monotonic
from urllib.parse import urlparse

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
import logging
from app.database import engine, SessionLocal
from app.config import settings

logger = logging.getLogger(__name__)

_SCHEMA_READINESS = {
    "status": "not_started",
    "current_heads": [],
    "target_heads": [],
    "failure": "startup_not_completed",
}
from app.routers import auth, users, overview, ontologies, files, prompts, models, entities, logic, actions, extraction, graph, settings as settings_router, export, audit
from app.routers.v2 import connections as connections_v2
from app.routers.v2 import datasets as datasets_v2
from app.routers.v2 import pipelines as pipelines_v2
from app.routers.v2 import service_principals as service_principals_v2
from app.routers.v2 import graph as graph_v2
from app.routers.v2 import search as search_v2
from app.routers.v2 import curated as curated_v2
from app.routers.v2 import mappings as mappings_v2
from app.routers.v2 import incremental as incremental_v2
from app.routers.v2 import logic_actions as logic_actions_v2
from app.routers.v2 import construction_runs as construction_runs_v2
from app.routers.v2 import benchmarks as benchmarks_v2
from app.routers.v2 import multimodal as multimodal_v2
from app.routers.v2 import dashboard as dashboard_v2
from app.routers.v2 import revisions as revisions_v2
from app.routers.v2 import audits as audits_v2
from app.routers.v2 import construction_drafts as construction_drafts_v2
from app.routers.v2 import temporal as temporal_v2
from app.routers.v2 import model_routes as model_routes_v2
from app.routers.v2 import reasoning_workbench as reasoning_v2
from app.routers.v2 import decisions as decisions_v2
from app.routers.v2 import agent as agent_v2
from app.routers.v2 import agent_sessions as agent_sessions_v2
from app.routers.v2 import manufacturing_data as manufacturing_data_v2
from app.routers.v2 import logic_assets as logic_assets_v2
from app.routers.v2 import object_query as object_query_v2
from app.routers.v2 import object_views as object_views_v2
from app.routers.v2 import object_sets as object_sets_v2
from app.routers.v2 import scenarios as scenarios_v2
from app.routers.v2 import scenario_workbench as scenario_workbench_v2
from app.routers.v2 import what_if_demo as what_if_demo_v2
from app.routers.v2 import decision_lab as decision_lab_v2
from app.routers.v2 import decision_cases as decision_cases_v2
from app.routers.v2 import supplier_studies as supplier_studies_v2
from app.routers.v2 import scenario_studies as scenario_studies_v2
from app.routers.v2 import rule_runtime as rule_runtime_v2
from app.routers.v2 import ontology_candidates as ontology_candidates_v2
from app.routers.v2 import sensor_playback as sensor_playback_v2
from app.routers.v2 import graph_views as graph_views_v2
from app.routers.v2 import workshop_apps as workshop_apps_v2
from app.routers.v2 import ontology_queries as ontology_queries_v2
from app.services.v2 import mcp_server
from app.routers.v2 import dynamic_ontology as dynamic_ontology_v2
from app.routers.v2 import temporal_replays as temporal_replays_v2
from app.routers.v2 import temporal_streams as temporal_streams_v2
from app.routers.v2 import dynamic_data as dynamic_data_v2
from app.routers.v2 import what_if as what_if_v2
from app.routers.v2 import semantic_core as semantic_core_v2
from app.routers.v2 import security as security_v2
from app.routers.v2 import data_plane as data_plane_v2
from app.routers.v2 import schema_migrations as schema_migrations_v2
from app.routers.v2 import derived_projections as derived_projections_v2
from app.routers.v2 import process_models as process_models_v2

def _run_schema_migration():
    """Upgrade and verify the database schema before startup seeding."""
    import os
    from pathlib import Path
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    backend_dir = Path(__file__).resolve().parents[1]
    alembic_ini = str(backend_dir / "alembic.ini")
    cfg = AlembicConfig(alembic_ini)
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    if os.environ.get("DATABASE_URL"):
        cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

    try:
        # A pre-existing, unversioned database needs an explicit developer
        # opt-in before its schema can be treated as the baseline.
        insp = inspect(engine)
        existing_tables = set(insp.get_table_names())
        has_alembic_version = "alembic_version" in existing_tables
        if existing_tables and not has_alembic_version:
            allow_legacy_stamp = os.environ.get(
                "ALLOW_LEGACY_DB_BASELINE_STAMP", ""
            ).strip().lower() == "true"
            if settings.environment == "production" or not allow_legacy_stamp:
                raise RuntimeError(
                    "Refusing to stamp an unversioned database without explicit developer opt-in"
                )
            command.stamp(cfg, "0001_full_baseline")
        if settings.environment != "production":
            command.upgrade(cfg, "head")
        else:
            # Production migrations are applied by the release/deployment step;
            # API workers only verify that the database already matches code.
            logger.info("Production startup will verify schema without applying migrations")

        script = ScriptDirectory.from_config(cfg)
        target_heads = sorted(script.get_heads())
        if len(target_heads) != 1:
            raise RuntimeError(f"Expected one Alembic head, found {len(target_heads)}")
        with engine.connect() as connection:
            current_heads = sorted(MigrationContext.configure(connection).get_current_heads())
        if current_heads != target_heads:
            raise RuntimeError("Database revision does not match the checked-out Alembic head")

        _SCHEMA_READINESS.update(
            status="verifying",
            current_heads=current_heads,
            target_heads=target_heads,
            failure=None,
        )
        # Revision equality alone misses manual DDL drift. Check ORM metadata
        # once at startup; this is deliberately not part of each readiness poll.
        command.check(cfg)
        _SCHEMA_READINESS.update(
            status="ready",
            current_heads=current_heads,
            target_heads=target_heads,
            failure=None,
        )
        return True
    except Exception as exc:
        _SCHEMA_READINESS.update(
            status="not_ready",
            failure="migration_or_schema_verification_failed",
        )
        engine.dispose()
        if settings.environment == "production":
            logger.error("Database schema migration/readiness verification failed", exc_info=True)
            raise RuntimeError("Database schema migration/readiness verification failed") from exc

        # Keep local development inspectable, but do not mutate an unknown
        # schema after a failed migration or verification.
        logger.warning("Database schema is not ready; startup seeding and writes remain disabled", exc_info=True)
        return False


def _seed_db():
    from app.services.auth_service import seed_admin
    from app.models.rules_config import RulesConfig
    import uuid

    db = SessionLocal()
    try:
        # Import all models to ensure tables are created
        from app.models import user, ontology, file, prompt, model_config, entity, logic as logic_model, action, relation, extraction_task, rules_config, audit_task
        from app.models.v2 import dataset as v2_dataset, pipeline as v2_pipeline, connection as v2_connection  # noqa: F401
        from app.models.ontology_revision import OntologyRevision  # noqa: F401
        from app.models.v2.logic import OntologyLogicRule, OntologyStateMachine  # noqa: F401
        from app.models.v2.action import OntologyActionType, OntologyActionRun  # noqa: F401
        from app.models.v2.decision_case import DecisionCase, DecisionCaseEvent  # noqa: F401
        from app.models.v2.curated import CuratedDataset, CuratedReview, CuratedRowEdit  # noqa: F401
        from app.models.v2.mapping import OntologyMapping, OntologyLinkMapping  # noqa: F401
        from app.models.v2.mapping_apply_run import MappingApplyRun  # noqa: F401
        from app.models.v2.mapping_link_apply_run import MappingLinkApplyRun  # noqa: F401
        from app.models.v2.construction import ConstructionRun, EvidenceRef  # noqa: F401
        from app.models.v2.temporal_profile import TemporalDatasetProfile  # noqa: F401
        from app.models.v2.multimodal import ExtractedFragment  # noqa: F401
        from app.models.v2.multimodal_install import MultimodalInstallTask  # noqa: F401
        from app.models.v2.construction_draft import ConstructionDraft  # noqa: F401
        from app.models.v2.construction_review import ConstructionReview, ConstructionReviewEvent  # noqa: F401
        from app.models.v2.logic_asset import LogicAsset, LogicAssetRun  # noqa: F401
        from app.models.v2.query_view import QueryDataView, QueryViewRetentionEvent  # noqa: F401
        from app.models.v2.query_job import QueryJob  # noqa: F401
        from app.models.v2.scenario import ScenarioResource, ScenarioRevision, ScenarioChangeSet, ScenarioRun, ScenarioRunStage, ScenarioMetricSnapshot, ScenarioGrant, ScenarioAudit, ScenarioStudy, ScenarioStudyCase  # noqa: F401
        from app.models.v2.scenario_study import StudyDefinition, StudyVersion, CaseDefinition, RunPlan  # noqa: F401
        from app.models.v2.rule_runtime import RuleResource, RuleVersion, RuleProposal, RuleEvaluation, RuleAlert, RuleAlertEvent  # noqa: F401
        from app.models.v2.graph_view import GraphView, GraphViewVersion  # noqa: F401
        from app.models.v2.workbench_task import MappingTask, DataImportTask, ModelInvocation  # noqa: F401
        from app.models.v2.dynamic_ontology import OntologyChange, WhatIfScenario, WhatIfRun  # noqa: F401
        from app.models.v2.temporal_replay import (  # noqa: F401
            DataModelSnapshot,
            TemporalFact,
            TemporalReplay,
            TemporalReplayBatch,
            TemporalStreamEvent,
        )
        from app.models.v2.semantic_core import (  # noqa: F401
            OntologySemanticResource,
            OntologySemanticResourceVersion,
            OntologySourceMapping,
        )
        from app.models.v2.security import OntologySecurityPolicy  # noqa: F401
        from app.models.v2.schema_migration import (  # noqa: F401
            SchemaDependency,
            SchemaMigrationInstruction,
            SchemaMigrationPlan,
            SchemaMigrationRun,
        )
        from app.models.v2.time_series import TimeSeriesSync  # noqa: F401
        from app.models.v2.derived_projection import DerivedProjection, DerivedProjectionRun  # noqa: F401
        if not _run_schema_migration():
            logger.error("Skipping startup seed/backfill because database schema is not verified")
            return

        seed_admin(db)

        # Opt-in MiniMax M3 bootstrap.  The key is read from the process
        # environment, verified against the provider model list, then stored
        # encrypted in the model registry; no secret is returned or logged.
        try:
            from app.services.minimax_bootstrap import bootstrap_minimax_model
            bootstrap_minimax_model(db)
        except Exception:
            logger.warning("MiniMax bootstrap skipped; configure it from Models when needed", exc_info=True)
        try:
            from app.services.local_model_bootstrap import bootstrap_local_model_slot
            bootstrap_local_model_slot(db)
        except Exception:
            logger.warning("Local Ollama model slot bootstrap skipped", exc_info=True)
        try:
            from app.services.v2.datasets.supply_chain_installer import ensure_cmapss_fd001_demo
            ensure_cmapss_fd001_demo(db)
        except Exception:
            logger.warning("Builtin supply-chain source bootstrap skipped", exc_info=True)
        # Older ontologies predate immutable revisions.  Create a revision-1
        # snapshot once per project after migrations; this never overwrites a
        # saved graph or removes historical metadata.
        try:
            from app.models.ontology import OntologyProject
            from app.models.ontology_revision import OntologyRevision
            from app.models.entity import Entity
            from app.services.v2.revision_service import create_revision
            from app.services.v2.semantic_core_service import ensure_semantic_metadata
            published_status_changed = False
            for project in db.query(OntologyProject).all():
                if not db.query(OntologyRevision.id).filter(OntologyRevision.ontology_id == project.id).first():
                    create_revision(db, project.id)
                current_revision = db.query(OntologyRevision).filter(
                    OntologyRevision.ontology_id == project.id,
                    OntologyRevision.is_current.is_(True),
                ).order_by(OntologyRevision.revision_no.desc()).first()
                if current_revision:
                    ensure_semantic_metadata(db, project.id, current_revision.id, commit=False)
                # Historical successful builds used the default ``draft``
                # label forever.  Preserve empty/failed drafts, but show an
                # actually materialised ontology as created without changing
                # its graph, revision, evidence, or ownership.
                if project.status == "draft" and db.query(Entity.id).filter(Entity.ontology_id == project.id).first():
                    project.status = "created"
                    published_status_changed = True
            # The semantic backfill is itself a durable compatibility
            # migration.  Commit it even when no legacy project status needed
            # changing, otherwise the session close would roll back the
            # canonical rows for an otherwise unchanged ontology.
            db.commit()
        except Exception:
            logger.warning("Ontology revision backfill skipped", exc_info=True)

        # Only clean up local-thread work when Celery is disabled. A Celery
        # worker is a separate process and can keep running while this API
        # process starts; blanket cleanup would incorrectly fail live jobs.
        from app.models.extraction_task import ExtractionTask
        from app.models.audit_task import AuditTask
        from app.tasks.task_claims import celery_dispatch_enabled
        celery_enabled = celery_dispatch_enabled()
        if not celery_enabled:
            try:
                stale_extractions = db.query(ExtractionTask).filter(ExtractionTask.status == "running").all()
                stale_audits = db.query(AuditTask).filter(AuditTask.status.in_(("running", "cancel_requested"))).all()
            except Exception:
                # A deployment with an external/unknown Alembic revision can leave
                # the first pooled connection aborted. Startup cleanup is best effort.
                db.rollback()
                logger.warning("Stale local-task cleanup skipped", exc_info=True)
                stale_extractions, stale_audits = [], []
            for task in stale_extractions:
                task.status = "failed"
                task.error = "服务重启，提取任务中断。请重新触发提取。"
            for task in stale_audits:
                was_cancelled = task.status == "cancel_requested" or task.cancel_requested
                task.status = "cancelled" if was_cancelled else "failed"
                task.error = "服务重启，本地审查任务中断。请重新发起审查。"
            if stale_extractions or stale_audits:
                db.commit()
        else:
            logger.info("Skipping local-task startup cleanup; Celery workers run independently of the API")

        # Long-running workbench tasks are resumable.  When the API process is
        # replaced, a task that was marked ``running`` no longer has a worker
        # behind it.  Put it back in the durable queue and publish one Celery
        # message after the transaction commits; the installers/importers are
        # idempotent, so a retried message cannot create duplicate samples.
        if celery_enabled:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
            def is_stale(item):
                timestamp = item.updated_at
                if timestamp is None:
                    return True
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                return timestamp < cutoff

            resumable_install = [item for item in db.query(MultimodalInstallTask).filter(MultimodalInstallTask.status == "running").all() if is_stale(item)]
            resumable_mapping = [item for item in db.query(MappingTask).filter(MappingTask.status == "running").all() if is_stale(item)]
            resumable_import = [item for item in db.query(DataImportTask).filter(DataImportTask.status == "running").all() if is_stale(item)]
            resumable_what_if = [item for item in db.query(WhatIfRun).filter(WhatIfRun.status == "running").all() if is_stale(item)]
            for item in resumable_install:
                item.status = "queued"
                item.updated_at = datetime.now(timezone.utc)
            for item in resumable_mapping:
                item.status = "queued"
                item.started_at = None
                item.completed_at = None
                item.updated_at = datetime.now(timezone.utc)
            for item in resumable_import:
                item.status = "queued"
                item.updated_at = datetime.now(timezone.utc)
            for item in resumable_what_if:
                item.status = "queued"
                item.stage = "prepare_baseline"
                item.progress = 0
                item.started_at = None
                item.completed_at = None
                item.error = None
                item.updated_at = datetime.now(timezone.utc)
            if resumable_install or resumable_mapping or resumable_import or resumable_what_if:
                db.commit()
                try:
                    from app.tasks.v2.workbench import run_ibadas_install_task, run_mapping_task
                    from app.tasks.v2.connection_sync import run_data_import_task
                    from app.tasks.v2.workbench import run_what_if_task
                    for item in resumable_install:
                        run_ibadas_install_task.delay(item.id)
                    for item in resumable_mapping:
                        run_mapping_task.delay(item.id)
                    for item in resumable_import:
                        run_data_import_task.delay(item.id)
                    for item in resumable_what_if:
                        run_what_if_task.delay(item.id)
                except Exception:
                    logger.warning("Resumable workbench tasks were queued but could not be published", exc_info=True)

        # A process restart can leave an event-at-a-time stream marked as
        # running even though its worker thread/Celery task has disappeared.
        # Requeue only stale active streams; the committed event watermark and
        # idempotent event keys let the next worker continue exactly where it
        # stopped.  This is deliberately best-effort so startup never fails
        # because a broker is unavailable.
        stream_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        def stream_is_stale(item):
            timestamp = item.updated_at
            if timestamp is None:
                return True
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return timestamp < stream_cutoff

        resumable_streams = [
            item for item in db.query(TemporalReplay).filter(
                TemporalReplay.status.in_(["running", "pausing"]),
            ).all() if stream_is_stale(item)
        ]
        for item in resumable_streams:
            item.status = "queued"
            item.pause_requested = False
            item.step_requested = False
            item.cancel_requested = False
            item.error = None
            item.updated_at = datetime.now(timezone.utc)
        if resumable_streams:
            db.commit()
            try:
                from app.services.v2.temporal_stream_service import dispatch_stream
                for item in resumable_streams:
                    dispatch_stream(item.id)
            except Exception:
                logger.warning("Resumable temporal streams were requeued but could not be dispatched", exc_info=True)

        # Seed confidence rules
        if db.query(RulesConfig).count() == 0:
            rules = [
                ("confidence_entity_min", "0.5", "实体最低置信度", "Entity min confidence"),
                ("confidence_logic_min", "0.6", "逻辑规则最低置信度", "Logic rule min confidence"),
                ("confidence_action_min", "0.6", "动作最低置信度", "Action min confidence"),
                ("confidence_relation_min", "0.5", "关系最低置信度", "Relation min confidence"),
                ("confidence_high_threshold", "0.9", "高置信度阈值", "High confidence threshold"),
                ("confidence_medium_threshold", "0.7", "中置信度阈值", "Medium confidence threshold"),
                ("confidence_low_threshold", "0.5", "低置信度阈值", "Low confidence threshold"),
                ("confidence_display_dashed_below", "0.7", "低于此值显示虚线边", "Show dashed edge below threshold"),
            ]
            for key, val, label_cn, label_en in rules:
                db.add(RulesConfig(id=str(uuid.uuid4()), rule_key=key, rule_value=val,
                                   rule_label_cn=label_cn, rule_label_en=label_en))
            db.commit()

        # Seed / update builtin prompts (upsert by name)
        from app.models.prompt import Prompt
        from app.models.user import User
        from app.routers.prompts import BUILTIN_PROMPTS
        admin = db.query(User).filter(User.role == "admin").first()
        if admin:
            for p in BUILTIN_PROMPTS:
                existing = db.query(Prompt).filter(Prompt.name == p["name"]).first()
                if existing:
                    existing.content = p["content"]
                    existing.domain = p["domain"]
                else:
                    db.add(Prompt(id=str(uuid.uuid4()), name=p["name"], domain=p["domain"],
                                  content=p["content"], version="v1.0", created_by=admin.id))
            db.commit()
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # MinIO is an external service from the API's point of view.  A fresh
    # volume therefore has no buckets yet, even though the service itself is
    # healthy.  Create the declared buckets before startup seeding so a new
    # deployment cannot publish dataset URIs that immediately fail with
    # ``NoSuchBucket``.
    try:
        from app.services.storage_service import get_storage_service
        get_storage_service().ensure_default_buckets()
    except Exception:
        logger.warning("Object-storage bucket setup skipped; startup continues", exc_info=True)
    _seed_db()
    # 初始化 Neo4j 索引（后台执行，不阻塞启动）
    try:
        from app.services.v2.graph.index_setup import setup_indexes
        setup_indexes()
    except Exception:
        logger.warning("Neo4j index setup skipped (unavailable); startup continues", exc_info=True)
    # The Ontology MCP endpoint (streamable HTTP) needs its session manager running for the app's lifetime.
    async with mcp_server.start().run():
        yield

app = FastAPI(title="OntoPrompt API", version="0.1.0", lifespan=lifespan)

# 注册限流器 - 保护 Auth 等敏感端点
from app.limiter import limiter
from slowapi.middleware import SlowAPIMiddleware
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def schema_write_readiness_gate(request: Request, call_next):
    """Fail closed for every HTTP write until the database schema is verified."""
    safe_methods = {"GET", "HEAD", "OPTIONS"}
    if request.method not in safe_methods and _SCHEMA_READINESS["status"] != "ready":
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "schema_not_ready",
                    "message": "Database schema is not verified; write requests are disabled.",
                    "schema_status": _SCHEMA_READINESS["status"],
                }
            },
            headers={"Retry-After": "3"},
        )
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(overview.router, prefix="/api/v1/overview", tags=["overview"])
app.include_router(ontologies.router, prefix="/api/v1/ontologies", tags=["ontologies"])
app.include_router(files.router, prefix="/api/v1/ontologies/{ontology_id}/files", tags=["files"])
app.include_router(entities.router, prefix="/api/v1/ontologies/{ontology_id}/entities", tags=["entities"])
app.include_router(logic.router, prefix="/api/v1/ontologies/{ontology_id}/logic", tags=["logic"])
app.include_router(actions.router, prefix="/api/v1/ontologies/{ontology_id}/actions", tags=["actions"])
app.include_router(extraction.router, prefix="/api/v1/ontologies/{ontology_id}/execute", tags=["extraction"])
app.include_router(graph.router, prefix="/api/v1/ontologies/{ontology_id}/graph", tags=["graph"])
app.include_router(export.router, prefix="/api/v1/ontologies/{ontology_id}/export", tags=["export"])
app.include_router(audit.router, prefix="/api/v1/ontologies/{ontology_id}/audit", tags=["audit"])
app.include_router(prompts.router, prefix="/api/v1/prompts", tags=["prompts"])
app.include_router(models.router, prefix="/api/v1/models", tags=["models"])
app.include_router(settings_router.router, prefix="/api/v1/settings", tags=["settings"])
app.include_router(connections_v2.router, prefix="/api/v2/connections", tags=["v2-connections"])
app.include_router(connections_v2.data_imports_router, prefix="/api/v2", tags=["v2-data-imports"])
app.include_router(datasets_v2.router, prefix="/api/v2/datasets", tags=["v2-datasets"])
app.include_router(datasets_v2.data_sources_router, prefix="/api/v2", tags=["v2-data-sources"])
app.include_router(pipelines_v2.router, prefix="/api/v2/pipelines", tags=["v2-pipelines"])
app.include_router(service_principals_v2.router, prefix="/api/v2/service-principals", tags=["v2-service-principals"])
app.include_router(graph_v2.router, prefix="/api/v2/ontologies", tags=["v2-graph"])
app.include_router(search_v2.router, prefix="/api/v2/ontologies", tags=["v2-search"])
app.include_router(curated_v2.router, prefix="/api/v2/curated", tags=["v2-curated"])
app.include_router(mappings_v2.router, prefix="/api/v2/ontologies", tags=["v2-mappings"])
app.include_router(incremental_v2.router, prefix="/api/v2/incremental", tags=["v2-incremental"])
app.include_router(logic_actions_v2.router, prefix="/api/v2/ontologies", tags=["v2-logic-actions"])
app.include_router(construction_runs_v2.router, prefix="/api/v2/ontologies", tags=["v2-construction-runs"])
app.include_router(construction_runs_v2.construction_root_router, prefix="/api/v2", tags=["v2-construction-runs"])
app.include_router(construction_runs_v2.assertions_router, prefix="/api/v2", tags=["v2-provenance"])
app.include_router(benchmarks_v2.router, prefix="/api/v2", tags=["v2-benchmarks"])
app.include_router(multimodal_v2.router, prefix="/api/v2", tags=["v2-multimodal"])
app.include_router(dashboard_v2.router, prefix="/api/v2/dashboard", tags=["v2-dashboard"])
app.include_router(revisions_v2.router, prefix="/api/v2/ontologies", tags=["v2-revisions"])
app.include_router(audits_v2.router, prefix="/api/v2/audits", tags=["v2-audits"])
app.include_router(construction_drafts_v2.router, prefix="/api/v2", tags=["v2-construction-drafts"])
app.include_router(construction_drafts_v2.review_router, prefix="/api/v2", tags=["v2-construction-reviews"])
app.include_router(construction_drafts_v2.mapping_tasks_router, prefix="/api/v2", tags=["v2-mapping-tasks"])
app.include_router(temporal_v2.router, prefix="/api/v2", tags=["v2-temporal"])
app.include_router(temporal_v2.ontology_router, prefix="/api/v2/ontologies", tags=["v2-temporal"])
app.include_router(temporal_replays_v2.router, prefix="/api/v2", tags=["v2-temporal-replays"])
app.include_router(temporal_streams_v2.router, prefix="/api/v2", tags=["v2-temporal-streams"])
app.include_router(dynamic_data_v2.router, prefix="/api/v2", tags=["v2-dynamic-data"])
app.include_router(model_routes_v2.router, prefix="/api/v2/model-routes", tags=["v2-model-routes"])
app.include_router(model_routes_v2.invocations_router, prefix="/api/v2", tags=["v2-model-invocations"])
app.include_router(reasoning_v2.router, prefix="/api/v2/ontologies", tags=["v2-reasoning"])
app.include_router(decisions_v2.router, prefix="/api/v2/ontologies", tags=["v2-decisions"])
app.include_router(agent_v2.router, prefix="/api/v2/ontologies", tags=["v2-agent"])
app.include_router(agent_sessions_v2.router, prefix="/api/v2/ontologies", tags=["v2-agent-sessions"])
app.include_router(manufacturing_data_v2.router, prefix="/api/v2/ontologies", tags=["v2-manufacturing-data"])
app.include_router(logic_assets_v2.router, prefix="/api/v2/ontologies", tags=["v2-logic-assets"])
app.include_router(object_query_v2.router, prefix="/api/v2/ontologies", tags=["v2-object-query"])
app.include_router(object_views_v2.router, prefix="/api/v2/ontologies", tags=["v2-object-views"])
app.include_router(object_sets_v2.router, prefix="/api/v2/ontologies", tags=["v2-object-sets"])
app.include_router(scenarios_v2.router, prefix="/api/v2/ontologies", tags=["v2-scenarios"])
app.include_router(scenario_workbench_v2.router, prefix="/api/v2/ontologies", tags=["v2-scenario-workbench"])
app.include_router(what_if_demo_v2.router, prefix="/api/v2/what-if", tags=["v2-what-if-demo"])
app.include_router(decision_lab_v2.router, prefix="/api/v2/decision-lab", tags=["v2-decision-lab"])
app.include_router(decision_cases_v2.router, prefix="/api/v2/ontologies", tags=["v2-decision-cases"])
app.include_router(supplier_studies_v2.router, prefix="/api/v2/ontologies", tags=["v2-supplier-studies"])
app.include_router(scenario_studies_v2.router, prefix="/api/v2/ontologies", tags=["v2-scenario-study-definitions"])
app.include_router(rule_runtime_v2.router, prefix="/api/v2/ontologies", tags=["v2-rule-runtime"])
app.include_router(ontology_candidates_v2.router, prefix="/api/v2/ontologies", tags=["v2-ontology-candidates"])
app.include_router(sensor_playback_v2.router, prefix="/api/v2/ontologies", tags=["v2-sensor-playback"])
app.include_router(graph_views_v2.router, prefix="/api/v2/ontologies", tags=["v2-graph-views"])
app.include_router(workshop_apps_v2.router, prefix="/api/v2/ontologies", tags=["v2-workshop-apps"])
app.include_router(ontology_queries_v2.router, prefix="/api/v2/ontologies", tags=["v2-ontology-queries"])
app.mount("/api/v2/mcp", mcp_server.http_app)
app.include_router(dynamic_ontology_v2.router, prefix="/api/v2/ontologies", tags=["v2-ontology-editor"])
app.include_router(what_if_v2.router, prefix="/api/v2/ontologies", tags=["v2-what-if"])
app.include_router(semantic_core_v2.router, prefix="/api/v2/ontologies", tags=["v2-semantic-core"])
app.include_router(security_v2.router, prefix="/api/v2/ontologies", tags=["v2-security"])
app.include_router(data_plane_v2.router, prefix="/api/v2", tags=["v2-data-plane"])
app.include_router(schema_migrations_v2.router, prefix="/api/v2", tags=["v2-schema-migrations"])
app.include_router(derived_projections_v2.router, prefix="/api/v2/ontologies", tags=["v2-derived-projections"])
app.include_router(process_models_v2.router, prefix="/api/v2/ontologies", tags=["v2-process-models"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _host_port(endpoint: str, default_port: int) -> tuple[str, int]:
    """Parse host[:port] and URL-shaped service endpoints consistently."""
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}")
    return parsed.hostname or "localhost", parsed.port or default_port


_TCP_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="health-probe")
_TCP_PROBE_CACHE: dict[tuple[str, int], tuple[float, bool]] = {}
_TCP_PROBE_LOCK = Lock()


def _blocking_tcp_probe(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _cache_tcp_probe(key: tuple[str, int], value: bool, ttl: float = 15.0) -> None:
    with _TCP_PROBE_LOCK:
        _TCP_PROBE_CACHE[key] = (monotonic() + ttl, value)


def _tcp_reachable(host: str, port: int, timeout: float = 0.2) -> bool:
    """Bound TCP and Docker-DNS waits, caching the optional-service result."""
    key = (host, int(port))
    now = monotonic()
    with _TCP_PROBE_LOCK:
        cached = _TCP_PROBE_CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]

    future = _TCP_PROBE_EXECUTOR.submit(_blocking_tcp_probe, host, int(port), timeout)
    try:
        available = future.result(timeout=timeout)
    except FutureTimeoutError:
        # Docker's embedded DNS can ignore the socket timeout for service
        # names that are not present in the selected Compose profile. Return
        # promptly; the callback refreshes the cache when resolution finishes.
        _cache_tcp_probe(key, False, ttl=2.0)

        def remember_result(completed):
            try:
                _cache_tcp_probe(key, bool(completed.result()))
            except Exception:
                _cache_tcp_probe(key, False)

        future.add_done_callback(remember_result)
        return False
    except Exception:
        available = False

    _cache_tcp_probe(key, available)
    return available


@app.get("/health")
def health(db: Session = Depends(get_db)):
    checks = {
        "status": "ok",
        "auth_mode": settings.auth_mode,
        "db": "unknown",
        "neo4j": "unknown",
        "falkordb": "unknown",
        "minio": "unknown",
        "chroma": "unknown",
    }

    # PostgreSQL check
    try:
        db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "error"

    # Optional dependencies must never hold the liveness endpoint hostage.
    # Probe the TCP listener before constructing clients, then give protocol
    # handshakes explicit timeouts. This keeps /health useful when the local
    # Compose profile intentionally omits Neo4j, MinIO, or Chroma.
    neo4j_endpoint = urlparse(settings.neo4j_uri)
    neo4j_host = neo4j_endpoint.hostname or "localhost"
    neo4j_port = neo4j_endpoint.port or 7687
    if _tcp_reachable(neo4j_host, neo4j_port):
        driver = None
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
                connection_timeout=1.0,
                connection_acquisition_timeout=1.0,
            )
            driver.verify_connectivity()
            checks["neo4j"] = "ok"
        except Exception:
            checks["neo4j"] = "unavailable"
        finally:
            if driver is not None:
                driver.close()
    else:
        checks["neo4j"] = "unavailable"

    # FalkorDB is the authoritative backend for new instance/temporal builds.
    if _tcp_reachable(settings.falkordb_host, settings.falkordb_port):
        try:
            from app.services.v2.graph.falkordb_service import FalkorDBService
            checks["falkordb"] = "ok" if FalkorDBService().available else "unavailable"
        except Exception:
            checks["falkordb"] = "unavailable"
    else:
        checks["falkordb"] = "unavailable"

    # MinIO check
    minio_host, minio_port = _host_port(
        settings.minio_endpoint,
        443 if settings.minio_use_ssl else 9000,
    )
    if _tcp_reachable(minio_host, minio_port):
        try:
            import urllib3
            from minio import Minio
            http_client = urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=0.5, read=1.0),
                retries=False,
            )
            client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_use_ssl,
                http_client=http_client,
            )
            client.list_buckets()
            checks["minio"] = "ok"
        except Exception:
            checks["minio"] = "unavailable"
    else:
        checks["minio"] = "unavailable"

    # ChromaDB check
    if _tcp_reachable(settings.chroma_host, settings.chroma_port):
        try:
            import urllib3
            http = urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=0.5, read=1.0),
                retries=False,
            )
            response = http.request(
                "GET",
                f"http://{settings.chroma_host}:{settings.chroma_port}/api/v2/heartbeat",
            )
            checks["chroma"] = "ok" if response.status < 500 else "unavailable"
        except Exception:
            checks["chroma"] = "unavailable"
    else:
        checks["chroma"] = "unavailable"

    return checks


@app.get("/health/live")
def liveness():
    """Process liveness probe; does not depend on database or optional services."""
    return {"status": "alive"}


@app.get("/health/ready")
def readiness(db: Session = Depends(get_db)):
    """Readiness probe requiring a reachable database and verified Alembic schema."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable", "schema": _SCHEMA_READINESS},
        )

    status_code = 200 if _SCHEMA_READINESS["status"] == "ready" else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if status_code == 200 else "not_ready",
            "database": "ok",
            "schema": _SCHEMA_READINESS,
        },
    )


@app.get("/api/v1/runtime")
def runtime_info():
    """Small public capability probe used by the desktop shell.

    It intentionally contains no credentials or model secrets. The frontend
    uses it to keep the local single-user switch explicit rather than
    inferring authentication from a failed request.
    """
    return {
        "data": {
            "auth_mode": settings.auth_mode,
            "local_single_user": settings.auth_mode == "local_single_user",
            "desktop_only": True,
        },
        "message": "ok",
    }
