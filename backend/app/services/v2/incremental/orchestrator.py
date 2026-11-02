"""增量更新编排器 — 触发链路：Connection→Pipeline→Curated→Ontology"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class IncrementalOrchestrator:
    """
    管理全链路增量更新触发逻辑：

    1. on_connection_sync  → 检测关联 Pipeline，触发增量运行
    2. on_pipeline_success → 更新 Curated Dataset，发出审核通知
    3. on_review_approved  → 检测关联 Mapping，自动触发增量 Mapping
    """

    def __init__(self, db: Session):
        self._db = db

    # ── 触发点 1：Connection 同步完成 ────────────────────────────────

    def on_connection_sync(self, connection_id: str, dataset_id: str) -> dict:
        """
        数据连接同步完成后：
        - 找到以该 dataset 为输入的所有 Pipeline
        - 若 Pipeline.spec.trigger.on_dataset_version = true，触发增量运行
        """
        from app.models.v2.pipeline import Pipeline

        triggered = []
        pipelines = self._db.query(Pipeline).filter(
            Pipeline.source_dataset_id == dataset_id,
        ).filter(
            Pipeline.status != "disabled",
        ).all()

        for pl in pipelines:
            spec = pl.spec or {}
            trigger_config = spec.get("trigger", {})
            if trigger_config.get("on_dataset_version", False):
                run_id = self._trigger_pipeline(pl.id, mode="incremental")
                if run_id:
                    triggered.append({"pipeline_id": pl.id, "run_id": run_id})
                    logger.info(f"增量触发 Pipeline {pl.id}，run_id={run_id}")

        return {"triggered_pipelines": triggered, "dataset_id": dataset_id}

    # ── 触发点 2：Pipeline 运行成功 ───────────────────────────────────

    def on_pipeline_success(self, pipeline_run_id: str) -> dict:
        """
        Pipeline 运行成功后：
        - 找到关联的 Curated Dataset
        - 将其状态重置为 pending_review（供人工审核）
        - 记录增量标记
        """
        from app.models.v2.pipeline import PipelineRun, Pipeline
        from app.models.v2.curated import CuratedDataset

        run = self._db.query(PipelineRun).filter(PipelineRun.id == pipeline_run_id).first()
        if not run or run.status != "success":
            return {"status": "skipped", "reason": "run not found or not success"}

        pipeline = self._db.query(Pipeline).filter(Pipeline.id == run.pipeline_id).first()
        if not pipeline:
            return {"status": "skipped", "reason": "pipeline not found"}

        updated_datasets = []
        target_ids = pipeline.target_curated_ids or []

        for ds_id in target_ids:
            ds = self._db.query(CuratedDataset).filter(CuratedDataset.id == ds_id).first()
            if ds and ds.status == "approved":
                # 已审核的数据集有新增量，重置为待审核
                ds.status = "pending_review"
                ds.updated_at = datetime.now(timezone.utc)
                updated_datasets.append(ds_id)
                logger.info(f"Curated Dataset {ds_id} 有新增量，重置为 pending_review")

        self._db.commit()
        return {"updated_datasets": updated_datasets, "pipeline_run_id": pipeline_run_id}

    # ── 触发点 3：审核通过 ─────────────────────────────────────────────

    def on_review_approved(self, review_id: str) -> dict:
        """
        Curated Dataset 审核通过后：
        - 找到关联该 Dataset 的所有 OntologyMapping
        - 若 spec.auto_apply_on_review = true，自动触发 Mapping Apply
        """
        from app.models.v2.curated import CuratedReview
        from app.models.v2.mapping import OntologyMapping
        from app.services.v2.mapping_definition_reviews import approved_mapping_review_for_run

        review = self._db.query(CuratedReview).filter(CuratedReview.id == review_id).first()
        if not review or review.status != "approved":
            return {"status": "skipped", "reason": "review not found or not approved"}

        dataset_id = review.curated_dataset_id
        mappings = self._db.query(OntologyMapping).filter(
            OntologyMapping.curated_dataset_id == dataset_id,
        ).filter(
            OntologyMapping.status != "disabled",
        ).all()

        auto_mappings = []
        for mapping in mappings:
            if not (mapping.field_mapping or {}).get("__auto_apply_on_review__", False):
                continue
            try:
                mapping_review = approved_mapping_review_for_run(self._db, mapping, None)
            except ValueError:
                # A data review cannot authorize an unreviewed Mapping definition.
                continue
            auto_mappings.append((mapping, mapping_review))
        # The review pins the snapshot it inspected. Resolving "latest" here
        # could apply a newer, unreviewed version if ingestion races approval.
        dataset_version_id = getattr(review, "dataset_version_id", None) if auto_mappings else None
        if auto_mappings and not dataset_version_id:
            # The decision may still be recorded; mappings are explicitly not
            # dispatched without the exact immutable version that was reviewed.
            self._db.commit()
            return {
                "status": "skipped",
                "reason": "approved review has no pinned immutable dataset version",
                "dataset_id": dataset_id,
                "review_id": review_id,
                "triggered_mappings": [],
            }

        prepared = []
        for mapping, mapping_review in auto_mappings:
            service, run, created = self._enqueue_mapping_apply(
                mapping.id, mapping.ontology_id, mapping.curated_dataset_id,
                dataset_version_id, review.id, mapping_review.id,
            )
            prepared.append((mapping, mapping_review, service, run, created))

        # Commit the review decision and every durable Mapping Apply row in one
        # transaction. If the process stops after this point, the reconciler
        # can dispatch every run without losing an approval-to-outbox gap.
        self._db.commit()

        triggered = []
        for mapping, mapping_review, service, run, created in prepared:
            try:
                run_result = service.dispatch(run.id)
            except Exception as exc:
                # The review and outbox rows are already durable. Leave the
                # run recoverable and report its current state to the caller.
                self._db.rollback()
                self._db.expire_all()
                current = self._db.get(type(run), run.id)
                from app.services.v2.mapping_apply_runs import serialize_mapping_apply_run
                run_result = serialize_mapping_apply_run(current) if current else {
                    "run_id": run.id, "status": "pending",
                }
                run_result["dispatch_error"] = type(exc).__name__
                logger.exception("Mapping Apply dispatch deferred for durable run %s", run.id)
            run_result["deduplicated"] = not created
            triggered.append({
                "mapping_id": mapping.id,
                "mapping_review_id": mapping_review.id,
                "run_id": run_result.get("run_id"),
                "task_id": run_result.get("celery_task_id"),
                "status": run_result.get("status"),
                "deduplicated": run_result.get("deduplicated", False),
                "dataset_version_id": dataset_version_id,
            })
            logger.info("持久化 Mapping Apply Run %s（mapping=%s, version=%s, status=%s）",
                        run_result.get("run_id"), mapping.id, dataset_version_id,
                        run_result.get("status"))

        return {
            "triggered_mappings": triggered,
            "dataset_id": dataset_id,
            "review_id": review_id,
        }

    # ── 幂等性保障 ────────────────────────────────────────────────────

    def _trigger_pipeline(self, pipeline_id: str, mode: str = "incremental") -> str | None:
        """触发 Pipeline 运行，返回 run_id"""
        from app.models.v2.pipeline import Pipeline, PipelineRun
        from app.services.v2.pipeline_execution import (
            PipelineExecutionError,
            build_pipeline_run_manifest,
        )

        pipeline = self._db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
        if pipeline is None:
            return None
        try:
            manifest = build_pipeline_run_manifest(self._db, pipeline, trigger_mode=mode)
            pinned_sources = manifest["sources"]
            run = PipelineRun(
                pipeline_id=pipeline_id,
                status="pending",
                dataset_version_id=pinned_sources[0]["dataset_version_id"] if len(pinned_sources) == 1 else None,
                pipeline_version_id=manifest.get("pipeline_version_id"),
                execution_mode=manifest["execution_mode"],
                execution_manifest=manifest,
                execution_digest=manifest["execution_digest"],
            )
        except PipelineExecutionError as exc:
            run = PipelineRun(
                pipeline_id=pipeline_id,
                status="failed",
                error_log=f"Pipeline execution snapshot rejected: {exc}",
                finished_at=datetime.now(timezone.utc),
                execution_mode="rejected",
            )
        self._db.add(run)
        self._db.commit()
        self._db.refresh(run)

        if run.status == "failed":
            return run.id

        try:
            from app.tasks.v2.pipeline_run import pipeline_run_task
            pipeline_run_task.delay(pipeline_id, run.id)
        except Exception as exc:
            run.status = "failed"
            run.error_log = f"Pipeline dispatch failed: {type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(timezone.utc)
            self._db.commit()

        return run.id

    def _enqueue_mapping_apply(self, mapping_id: str, ontology_id: str,
                               curated_dataset_id: str, dataset_version_id: str,
                               review_id: str, mapping_review_id: str):
        """Stage a durable run in the approval transaction; dispatch after commit."""
        from app.services.v2.mapping_apply_runs import MappingApplyRunService

        service = MappingApplyRunService(self._db)
        run, created = service.enqueue_review_mapping(
            mapping_id=mapping_id,
            ontology_id=ontology_id,
            curated_dataset_id=curated_dataset_id,
            review_id=review_id,
            mapping_review_id=mapping_review_id,
            dataset_version_id=dataset_version_id,
            requested_by=None,
            request_actor_kind="system",
            request_actor_id="curated_review_approval",
            commit=False,
        )
        return service, run, created
