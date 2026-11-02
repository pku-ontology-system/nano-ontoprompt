"""Pipeline 执行 Celery 任务 — 支持 DAG 编译 + 节点状态追踪"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

logger = logging.getLogger(__name__)

DISPATCH_MAX_ATTEMPTS = 8
DISPATCH_LEASE_SECONDS = 90
QUEUED_RETRY_AFTER = timedelta(minutes=5)


def get_celery_app():
    try:
        from app.tasks.extraction import celery_app
        return celery_app
    except Exception:
        return None


def _as_utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def dispatch_pipeline_run(run_id: str) -> dict:
    """Claim and publish one persisted PipelineRun; reconciliation retries lost dispatches."""
    from app.database import SessionLocal
    from app.models.v2.pipeline import PipelineRun

    now = datetime.now(timezone.utc)
    claim_token = str(uuid4())
    with SessionLocal() as db:
        run = db.query(PipelineRun).filter(PipelineRun.id == run_id).with_for_update().first()
        if run is None:
            return {"run_id": run_id, "state": "missing"}
        if run.status != "pending":
            return {"run_id": run_id, "state": run.dispatch_status or run.status}

        state = run.dispatch_status or "pending"
        if state == "pending":
            next_at = _as_utc(run.dispatch_next_at)
            if next_at and next_at > now:
                return {"run_id": run_id, "state": "deferred"}
        elif state == "dispatching":
            lease_until = _as_utc(run.dispatch_lease_until)
            if lease_until and lease_until > now:
                return {"run_id": run_id, "state": "claimed"}
        elif state == "queued":
            sent_at = _as_utc(run.dispatched_at)
            if sent_at and sent_at + QUEUED_RETRY_AFTER > now:
                return {"run_id": run_id, "state": "queued"}
        else:
            return {"run_id": run_id, "state": state}

        if not run.execution_manifest or not run.execution_digest:
            run.status = "failed"
            run.dispatch_status = "failed"
            run.dispatch_error = "无法安全重投：PipelineRun 缺少不可变执行快照"
            run.error_log = run.dispatch_error
            run.finished_at = now
            db.commit()
            return {"run_id": run_id, "state": "failed", "reason": "missing_manifest"}

        if run.dispatch_attempts >= DISPATCH_MAX_ATTEMPTS:
            run.status = "failed"
            run.dispatch_status = "failed"
            run.dispatch_error = "PipelineRun 派发达到最大重试次数"
            run.error_log = run.dispatch_error
            run.finished_at = now
            run.dispatch_lease_until = None
            run.dispatch_claim_token = None
            db.commit()
            return {"run_id": run_id, "state": "failed", "reason": "dispatch_exhausted"}

        run.dispatch_attempts += 1
        attempt = run.dispatch_attempts
        task_id = f"pipeline-run-{run.id}-{attempt}"
        pipeline_id = run.pipeline_id
        run.dispatch_status = "dispatching"
        run.dispatch_claim_token = claim_token
        run.dispatch_task_id = task_id
        run.dispatch_lease_until = now + timedelta(seconds=DISPATCH_LEASE_SECONDS)
        run.dispatch_error = None
        db.commit()

    try:
        if not celery_app:
            raise RuntimeError("Celery app is unavailable")
        pipeline_run_task.apply_async(
            args=[pipeline_id, run_id],
            task_id=task_id,
        )
    except Exception as exc:
        failure_time = datetime.now(timezone.utc)
        with SessionLocal() as db:
            run = db.query(PipelineRun).filter(PipelineRun.id == run_id).with_for_update().first()
            if run and run.status == "pending" and run.dispatch_claim_token == claim_token:
                run.dispatch_error = str(exc)[:4000]
                run.dispatch_claim_token = None
                run.dispatch_lease_until = None
                if run.dispatch_attempts >= DISPATCH_MAX_ATTEMPTS:
                    run.dispatch_status = "failed"
                    run.status = "failed"
                    run.error_log = f"PipelineRun 派发达到最大重试次数: {exc}"
                    run.finished_at = failure_time
                    state = "failed"
                else:
                    run.dispatch_status = "pending"
                    delay_seconds = min(30 * (2 ** min(run.dispatch_attempts - 1, 5)), 900)
                    run.dispatch_next_at = failure_time + timedelta(seconds=delay_seconds)
                    state = "pending_retry"
                db.commit()
                return {"run_id": run_id, "state": state, "error": str(exc)[:500]}
        return {"run_id": run_id, "state": "already_claimed"}

    with SessionLocal() as db:
        run = db.query(PipelineRun).filter(PipelineRun.id == run_id).with_for_update().first()
        if run and run.status == "pending" and run.dispatch_claim_token == claim_token:
            run.dispatch_status = "queued"
            run.dispatched_at = datetime.now(timezone.utc)
            run.dispatch_next_at = run.dispatched_at + QUEUED_RETRY_AFTER
            run.dispatch_claim_token = None
            run.dispatch_lease_until = None
            run.dispatch_error = None
            db.commit()
            return {"run_id": run_id, "state": "queued"}
    return {"run_id": run_id, "state": "started"}


def reconcile_pipeline_run_dispatch(batch_size: int = 50) -> dict:
    """Re-publish persisted pending Runs whose enqueue or worker claim was lost."""
    from sqlalchemy import and_, or_
    from app.database import SessionLocal
    from app.models.v2.pipeline import PipelineRun

    batch_size = min(max(int(batch_size), 1), 200)
    now = datetime.now(timezone.utc)
    queued_cutoff = now - QUEUED_RETRY_AFTER
    due = or_(
        and_(
            PipelineRun.dispatch_status == "pending",
            or_(PipelineRun.dispatch_next_at.is_(None), PipelineRun.dispatch_next_at <= now),
        ),
        and_(
            PipelineRun.dispatch_status == "dispatching",
            or_(PipelineRun.dispatch_lease_until.is_(None), PipelineRun.dispatch_lease_until <= now),
        ),
        and_(
            PipelineRun.dispatch_status == "queued",
            or_(PipelineRun.dispatched_at.is_(None), PipelineRun.dispatched_at <= queued_cutoff),
        ),
    )
    with SessionLocal() as db:
        run_ids = [row.id for row in db.query(PipelineRun.id).filter(
            PipelineRun.status == "pending", due,
        ).order_by(PipelineRun.created_at.asc(), PipelineRun.id.asc()).limit(batch_size).all()]

    states: dict[str, int] = {}
    for run_id in run_ids:
        result = dispatch_pipeline_run(run_id)
        state = str(result.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
    return {"scanned": len(run_ids), "states": states}


celery_app = get_celery_app()


def _init_node_status(definition: dict | None) -> dict[str, str]:
    """从 definition 中提取所有节点 ID，初始化为 'idle'"""
    if not definition:
        return {}
    nodes = definition.get("nodes", [])
    return {n["id"]: "idle" for n in nodes}


def _node_ids_for_type(definition: dict | None, node_type: str) -> list[str]:
    if not definition:
        return []
    return [
        node["id"] for node in definition.get("nodes") or []
        if node.get("type") == node_type and node.get("id")
    ]


def _compute_quality_score(rows: list[dict], route: str, meta: dict) -> float:
    if not rows:
        return 0.0
    if route == "C":
        meta_fields = {"markdown_text", "filename", "source_file", "source_dataset_id",
                       "extraction_strategy", "extraction_method", "structured_extraction_ok",
                       "structured_extraction_error"}
        meaningful_fields = [k for k in rows[0].keys() if k not in meta_fields]
        total_fields = len(meaningful_fields) or 1
        filled = sum(1 for row in rows for k in meaningful_fields if row.get(k))
        completeness = filled / (len(rows) * total_fields) if total_fields > 0 else 0
        rule_bonus = min(0.2, int(rows[0].get("rule_count", 0)) * 0.02)
        return min(1.0, completeness + rule_bonus)
    rows_before = meta.get("rows_before", len(rows)) or len(rows)
    rows_after = meta.get("rows_after", len(rows)) or len(rows)
    retention = rows_after / rows_before if rows_before > 0 else 1.0
    total_cells = sum(len(r) for r in rows) or 1
    filled_cells = sum(1 for r in rows for v in r.values() if v is not None and str(v).strip() != "")
    fill_rate = filled_cells / total_cells
    return round(retention * 0.4 + fill_rate * 0.6, 3)


def _route_for_kind(kind: str | None, default_route: str | None = None) -> str:
    if default_route in ("A", "B", "C"):
        return default_route
    if kind == "semi":
        return "B"
    if kind == "unstructured":
        return "C"
    return "A"


def _transform_nodes(definition: dict | None) -> list[dict]:
    if not definition:
        return []
    return [n for n in definition.get("nodes") or [] if n.get("type") == "transform"]


def _route_from_transform_config(config: dict | None) -> str | None:
    path = (config or {}).get("path")
    if path == "structured":
        return "A"
    if path == "semi_structured":
        return "B"
    if path == "unstructured":
        return "C"
    if path == "wide_table":
        return "A"
    return None


def _merge_dict(base: dict, overlay: dict) -> dict:
    result = dict(base or {})
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def _spec_from_transform_config(config: dict | None) -> dict:
    config = config or {}
    spec: dict = {}
    steps = config.get("steps") or []

    if config.get("engine"):
        spec["engine"] = config.get("engine")
    if config.get("path"):
        spec["path"] = config.get("path")

    for step in steps:
        op = step.get("op") if isinstance(step, dict) else None
        params = step.get("params") if isinstance(step, dict) else {}
        params = params or {}

        if op in ("parse_json", "flatten_json", "explode_array"):
            spec["format"] = "json"
            flatten = dict(spec.get("json_flatten") or {})
            if op == "explode_array":
                flatten["array_explode"] = True
            flatten.update(params)
            spec["json_flatten"] = flatten
        elif op == "parse_xml":
            spec["format"] = "xml"
        elif op in ("detect_wide_table", "suggest_split", "apply_split"):
            wide = dict(spec.get("wide_table_split") or {})
            wide["enabled"] = True
            if op in ("detect_wide_table", "suggest_split"):
                wide["suggest_only"] = True
            if op == "apply_split":
                wide["suggest_only"] = False
            wide.update(params)
            spec["wide_table_split"] = wide
        elif op in ("drop_duplicates", "drop_nulls", "fill_nulls", "normalize_dates"):
            cleansing = dict(spec.get("cleansing") or {})
            if op == "drop_duplicates":
                cleansing["deduplicate"] = True
            elif op == "drop_nulls":
                cleansing["null_strategy"] = "drop"
            elif op == "fill_nulls":
                cleansing["null_strategy"] = params.get("strategy", "fill_empty")
            elif op == "normalize_dates":
                cleansing["normalize_dates"] = True
            cleansing.update({k: v for k, v in params.items() if k != "strategy"})
            spec["cleansing"] = cleansing
        elif op == "document_to_markdown":
            doc = dict(spec.get("document_to_md") or {})
            doc["strategy"] = params.get("strategy", doc.get("strategy", "markitdown"))
            doc.update(params)
            spec["document_to_md"] = doc
        elif op == "ocr_extract":
            doc = dict(spec.get("document_to_md") or {})
            doc["strategy"] = "ocr"
            doc.update(params)
            spec["document_to_md"] = doc
        elif op == "vlm_extract":
            doc = dict(spec.get("document_to_md") or {})
            doc["strategy"] = "vlm"
            doc.update(params)
            spec["document_to_md"] = doc
        elif op == "llm_structurize":
            extract = dict(spec.get("md_to_structured") or {})
            extract.update(params)
            # The typed LLM step is an explicit request; params may select
            # fallback behavior but cannot turn the request into rule mode.
            extract["auto_extract"] = True
            spec["md_to_structured"] = extract
        elif op == "llm_enrich":
            # 方案三：规则提取后 LLM 语义增强
            extract = dict(spec.get("md_to_structured") or {})
            extract["llm_enrich"] = True
            if params.get("fields"):
                extract["enrich_fields"] = params["fields"]
            if "fallback_policy" in params:
                extract["fallback_policy"] = params["fallback_policy"]
            spec["md_to_structured"] = extract

    if config.get("path") == "wide_table":
        wide = dict(spec.get("wide_table_split") or {})
        wide.setdefault("enabled", True)
        wide.setdefault("suggest_only", False)
        spec["wide_table_split"] = wide
    return spec


def _pipeline_runtime_config(pl) -> tuple[str | None, dict]:
    transforms = _transform_nodes(pl.definition)
    route = None
    spec = dict(pl.spec or {})
    for node in transforms:
        cfg = node.get("config") or {}
        route = route or _route_from_transform_config(cfg)
        spec = _merge_dict(spec, _spec_from_transform_config(cfg))
        if isinstance(cfg.get("md_to_structured"), dict):
            spec = _merge_dict(spec, {"md_to_structured": cfg["md_to_structured"]})
    return route, spec


def _source_runtime_route(source: dict, transform_route: str | None, default_route: str | None) -> str:
    return transform_route or source.get("route") or _route_for_kind(source.get("kind"), default_route)


def _find_dataset_for_file(db, filename: str):
    from app.models.v2.dataset import Dataset, DatasetVersion

    stem = Path(filename).stem
    candidates = db.query(Dataset).filter(
        Dataset.name == stem
    ).order_by(Dataset.created_at.desc()).limit(20).all()
    for candidate in candidates:
        ver = db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == candidate.id
        ).order_by(DatasetVersion.version_no.desc()).first()
        if ver and ((ver.rowcount or 0) > 0 or ver.storage_uri):
            return candidate
    return candidates[0] if candidates else None


def _collect_sources(db, pl) -> list[dict]:
    from app.models.v2.dataset import Dataset

    sources: list[dict] = []
    definition = pl.definition or {}
    for node in definition.get("nodes") or []:
        if node.get("type") != "connector":
            continue
        for file_info in (node.get("config") or {}).get("files", []) or []:
            filename = file_info.get("name") or file_info.get("filename") or ""
            dataset_id = file_info.get("dataset_id")
            ds = db.query(Dataset).filter(Dataset.id == dataset_id).first() if dataset_id else None
            if not ds and filename:
                ds = _find_dataset_for_file(db, filename)
            if ds:
                sources.append({
                    "dataset_id": ds.id,
                    "filename": filename or ds.name,
                    "route": _route_for_kind(ds.kind, None),
                    "kind": ds.kind,
                })

    if not sources and pl.source_dataset_id:
        ds = db.query(Dataset).filter(Dataset.id == pl.source_dataset_id).first()
        if ds:
            sources.append({
                "dataset_id": ds.id,
                "filename": ds.name,
                "route": _route_for_kind(ds.kind, pl.route),
                "kind": ds.kind,
            })

    # Preserve order while removing duplicate datasets.
    seen: set[str] = set()
    unique_sources = []
    for source in sources:
        if source["dataset_id"] in seen:
            continue
        seen.add(source["dataset_id"])
        unique_sources.append(source)
    return unique_sources


def _load_source_rows(db, svc, source: dict, limit: int = 10000) -> list[dict]:
    from app.models.v2.dataset import DatasetVersion

    version = db.get(DatasetVersion, source.get("dataset_version_id"))
    if version is None or version.dataset_id != source["dataset_id"]:
        raise ValueError(f"Pinned DatasetVersion is missing for source {source['dataset_id']}")

    if source["route"] == "C":
        if not version.storage_uri:
            return []
        raw = svc._storage.get_object(version.storage_uri)
        if version.checksum:
            import hashlib
            if hashlib.sha256(raw).hexdigest() != version.checksum:
                raise ValueError(f"Pinned DatasetVersion checksum mismatch for source {source['dataset_id']}")
        return [{
            "filename": source["filename"],
            "content": raw,
            "storage_uri": version.storage_uri,
            "source_dataset_id": source["dataset_id"],
        }]
    _, rows = svc.preview_version_id(
        source["dataset_id"], version.id, limit=limit,
    )
    return rows


def _execute_route(route: str, ctx, data: list[dict]) -> tuple[list[dict], object]:
    from app.services.v2.pipeline.engine import execute_route_a, execute_route_b, execute_route_c

    if route == "B":
        return execute_route_b(ctx, data)
    if route == "C":
        return execute_route_c(ctx, data)
    return execute_route_a(ctx, data)


def _summarize_llm_executions(executions: list[dict]) -> dict | None:
    if not executions:
        return None
    statuses = {str(item.get("status") or "unknown") for item in executions}
    if "failed" in statuses:
        status = "failed"
    elif "degraded" in statuses:
        status = "degraded"
    elif statuses == {"skipped"}:
        status = "skipped"
    elif "skipped" in statuses:
        status = "partial"
    else:
        status = "success"

    def common_value(key: str) -> str | None:
        values = list(dict.fromkeys(str(item.get(key) or "none") for item in executions))
        return values[0] if len(values) == 1 else "mixed"

    reasons = list(dict.fromkeys(
        str(item["fallback_reason"])
        for item in executions
        if item.get("fallback_reason")
    ))
    return {
        "requested_method": common_value("requested_method"),
        "effective_method": common_value("effective_method"),
        "fallback_policy": common_value("fallback_policy"),
        "fallback_reason": reasons or None,
        "status": status,
        "source_count": len(executions),
        "processed": sum(int(item.get("processed") or 0) for item in executions),
        "success": sum(int(item.get("success") or 0) for item in executions),
        "sources": executions,
    }


def _safe_csv_bytes(data: list[dict]) -> bytes:
    import csv
    import io
    import json as _json

    def _safe_str(v) -> str:
        if v is None:
            return ""
        if isinstance(v, bytes):
            return f"<{len(v)} bytes>"
        if isinstance(v, (dict, list)):
            return _json.dumps(v, ensure_ascii=False)
        return str(v)

    if not data:
        return b""

    all_keys: list[str] = []
    seen_keys: set[str] = set()
    for row in data:
        for key in row.keys():
            if key == "content":
                continue
            if key not in seen_keys:
                all_keys.append(key)
                seen_keys.add(key)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=all_keys, extrasaction="ignore", restval="")
    writer.writeheader()
    for row in data:
        writer.writerow({k: _safe_str(row.get(k)) for k in all_keys})
    return buf.getvalue().encode("utf-8")


def _save_curated_dataset(db, svc, pl, source: dict, data: list[dict], ctx, multi_source: bool, table_name: str | None = None) -> dict:
    from app.models.v2.curated import CuratedDataset

    stem = Path(source["filename"]).stem
    name_parts = [pl.name]
    if multi_source:
        name_parts.append(stem)
    if table_name:
        name_parts.append(table_name)
    name_parts.append("curated")
    ds_name = " ".join(name_parts)
    curated_ds = svc.create_dataset(name=ds_name, kind="curated", created_by=pl.created_by)
    db.add(CuratedDataset(
        id=curated_ds.id,
        pipeline_id=pl.id,
        name=ds_name,
        status="pending_review",
    ))
    db.commit()
    svc.create_version(curated_ds.id, _safe_csv_bytes(data), rowcount=len(data))

    if data:
        try:
            # 赋新 dict, 原地修改 JSON 列不会被 SQLAlchemy 跟踪
            schema = dict(curated_ds.schema_json or {})
            schema["quality_score"] = _compute_quality_score(data, source["route"], ctx.meta)
            extraction = ctx.meta.get("md_to_structured") or {}
            if extraction.get("status") == "degraded":
                schema["quality_status"] = "not_evaluated_degraded"
            else:
                schema["quality_status"] = "evaluated"
            schema["columns"] = [k for k in data[0].keys() if k != "content"]
            schema["route"] = source["route"]
            schema["source_dataset_id"] = source["dataset_id"]
            schema["source_dataset_version_id"] = ctx.meta.get("source_dataset_version_id")
            schema["source_dataset_version_no"] = ctx.meta.get("source_dataset_version_no")
            schema["source_dataset_checksum"] = ctx.meta.get("source_dataset_checksum")
            schema["pipeline_run_id"] = ctx.meta.get("pipeline_run_id")
            schema["pipeline_execution_digest"] = ctx.meta.get("pipeline_execution_digest")
            requested_method = extraction.get("requested_method")
            if isinstance(requested_method, str) and requested_method.startswith("llm_"):
                schema["extraction_requested_method"] = requested_method
                schema["extraction_effective_method"] = extraction.get("effective_method")
                schema["extraction_fallback_policy"] = extraction.get("fallback_policy")
                schema["extraction_fallback_reason"] = extraction.get("fallback_reason")
                schema["pipeline_execution_outcome"] = extraction.get("status")
            if table_name:
                schema["transform_output_table"] = table_name
            curated_ds.schema_json = schema
            db.commit()
        except Exception:
            pass

    return {
        "source_dataset_id": source["dataset_id"],
        "source_dataset_version_id": ctx.meta.get("source_dataset_version_id"),
        "pipeline_run_id": ctx.meta.get("pipeline_run_id"),
        "source_file": source["filename"],
        "route": source["route"],
        "table_name": table_name,
        "curated_dataset_id": curated_ds.id,
        "rows_in": ctx.rows_in,
        "rows_out": len(data),
        "meta": ctx.meta,
    }


def _save_curated_outputs(db, svc, pl, source: dict, data: list[dict], ctx, multi_source: bool) -> list[dict]:
    split_tables = ctx.meta.get("split_tables")
    if isinstance(split_tables, dict) and split_tables:
        outputs = []
        for table_name, rows in split_tables.items():
            outputs.append(_save_curated_dataset(
                db, svc, pl, source, rows or [], ctx, multi_source=True, table_name=str(table_name)
            ))
        return outputs
    return [_save_curated_dataset(db, svc, pl, source, data, ctx, multi_source)]


def pipeline_run_task(pipeline_id: str, run_id: str):
    """Pipeline 执行任务 — 支持 DAG 编译 + 节点状态追踪"""
    from app.database import SessionLocal
    from app.models.v2.pipeline import Pipeline, PipelineRun, PipelineSchedule, PipelineScheduleRun
    from app.models.v2.service_principal import ServicePrincipal, ServicePrincipalGrant
    from app.services.v2.pipeline.base import PipelineContext
    from app.services.v2.pipeline.dag_compiler import compile_definition
    from app.services.v2.dataset_service import DatasetService
    from app.services.v2.pipeline_execution import verify_pipeline_run_manifest

    db = SessionLocal()
    run = None
    node_status: dict[str, str] = {}
    node_status_reason: dict[str, str] = {}
    llm_executions: list[dict] = []
    run_degraded = False
    try:
        run = db.query(PipelineRun).filter(
            PipelineRun.id == run_id,
            PipelineRun.pipeline_id == pipeline_id,
        ).with_for_update().first()
        if not run:
            return
        # Reconciliation may publish the same Run more than once after a broker
        # acknowledgment is lost. Only the first worker may claim pending work.
        if run.status != "pending":
            return

        if run.service_principal_id:
            run_manifest = run.execution_manifest if isinstance(run.execution_manifest, dict) else {}
            manifest_context = run_manifest.get("trigger_context")
            service_actor = manifest_context.get("service_actor") if isinstance(manifest_context, dict) else None
            context_matches = (
                isinstance(service_actor, dict)
                and service_actor.get("kind") == "service_principal"
                and str(service_actor.get("id") or "") == str(run.service_principal_id)
            )
            principal = db.query(ServicePrincipal).filter(
                ServicePrincipal.id == run.service_principal_id,
                ServicePrincipal.status == "active",
            ).with_for_update().first()
            grant = db.query(ServicePrincipalGrant).filter(
                ServicePrincipalGrant.service_principal_id == run.service_principal_id,
                ServicePrincipalGrant.resource_type == "pipeline",
                ServicePrincipalGrant.resource_id == pipeline_id,
                ServicePrincipalGrant.permission == "run",
                ServicePrincipalGrant.active.is_(True),
            ).with_for_update().first()
            failure_code = None
            if not context_matches:
                failure_code = "SERVICE_PRINCIPAL_CONTEXT_MISMATCH"
            elif not principal:
                failure_code = "SERVICE_PRINCIPAL_INACTIVE"
            elif not grant:
                failure_code = "SERVICE_PRINCIPAL_SCOPE_REVOKED"
            if failure_code:
                now = datetime.now(timezone.utc)
                safe_message = {
                    "SERVICE_PRINCIPAL_CONTEXT_MISMATCH": "PipelineRun 的服务主体与固定执行上下文不一致",
                    "SERVICE_PRINCIPAL_INACTIVE": "PipelineRun 服务主体不存在或已暂停",
                    "SERVICE_PRINCIPAL_SCOPE_REVOKED": "PipelineRun 服务主体已失去此 Pipeline 的 Run 授权",
                }[failure_code]
                run.status = "failed"
                run.dispatch_status = "failed"
                run.dispatch_error = safe_message
                run.error_log = safe_message
                run.finished_at = now
                db.commit()

                service_principal_id = run.service_principal_id
                schedule_id = manifest_context.get("schedule_id") if isinstance(manifest_context, dict) else None
                schedule_run_id = manifest_context.get("schedule_run_id") if isinstance(manifest_context, dict) else None
                with SessionLocal() as recovery_db:
                    schedule = None
                    if isinstance(schedule_id, str) and schedule_id:
                        schedule = recovery_db.query(PipelineSchedule).filter(
                            PipelineSchedule.id == schedule_id
                        ).with_for_update().first()
                    if schedule:
                        from app.tasks.v2.pipeline_schedule import _disable_schedule_for_principal
                        _disable_schedule_for_principal(
                            recovery_db, schedule, service_principal_id, now
                        )
                    if isinstance(schedule_run_id, str) and schedule_run_id:
                        scheduled_run = recovery_db.query(PipelineScheduleRun).filter(
                            PipelineScheduleRun.id == schedule_run_id
                        ).with_for_update().first()
                        if scheduled_run and scheduled_run.status in {"pending", "dispatching", "queued", "running"}:
                            scheduled_run.status = "failed"
                            scheduled_run.error_code = failure_code
                            scheduled_run.error_message = safe_message
                            scheduled_run.finished_at = now
                            scheduled_run.claim_token = None
                            scheduled_run.lease_until = None
                    recovery_db.commit()
                return

        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        run.dispatch_status = "dispatched"
        run.dispatch_claim_token = None
        run.dispatch_lease_until = None
        run.dispatch_next_at = None
        db.commit()

        current_pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
        if not current_pipeline:
            run.status = "failed"
            run.error_log = "Pipeline not found"
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
            return

        manifest = run.execution_manifest
        verify_pipeline_run_manifest(manifest, run.execution_digest)
        if run.execution_mode != manifest.get("execution_mode"):
            raise ValueError("PipelineRun execution mode does not match its manifest")
        if run.pipeline_version_id != manifest.get("pipeline_version_id"):
            raise ValueError("PipelineRun version does not match its manifest")
        pinned_sources = manifest.get("sources")
        if not isinstance(pinned_sources, list) or not pinned_sources:
            raise ValueError("PipelineRun has no pinned source versions")
        if len(pinned_sources) == 1:
            if run.dataset_version_id != pinned_sources[0].get("dataset_version_id"):
                raise ValueError("PipelineRun dataset version does not match its manifest")
        elif run.dataset_version_id is not None:
            raise ValueError("Multi-source PipelineRun must not claim a single dataset version")
        pipeline_snapshot = manifest["pipeline_snapshot"]
        if pipeline_snapshot.get("pipeline_id") != pipeline_id:
            raise ValueError("PipelineRun snapshot belongs to another Pipeline")
        pl = SimpleNamespace(**pipeline_snapshot)

        # ── DAG 编译 ──────────────────────────────────────────────
        definition = pl.definition
        plan = compile_definition(definition)
        node_status = _init_node_status(definition)

        def persist_node_status():
            run.stats = {
                **(run.stats or {}),
                "node_status": dict(node_status),
                "node_status_reason": dict(node_status_reason),
            }
            db.commit()

        def set_node_status(nid: str, status: str, reason: str | None = None):
            if nid in node_status:
                node_status[nid] = status
                if reason:
                    node_status_reason[nid] = reason[:1000]
                else:
                    node_status_reason.pop(nid, None)
                # 每步更新持久化到 Run；必须赋新 dict，原地修改 JSON 列不会被 SQLAlchemy 跟踪
                persist_node_status()

        def set_nodes_by_type(node_type: str, status: str, reason: str | None = None):
            for node_id in _node_ids_for_type(definition, node_type):
                set_node_status(node_id, status, reason)

        def record_llm_execution(source: dict, extraction: dict | None):
            if not isinstance(extraction, dict):
                return
            requested_method = extraction.get("requested_method")
            if not isinstance(requested_method, str) or not requested_method.startswith("llm_"):
                return
            entry = {
                "source_dataset_id": source.get("dataset_id"),
                "source_dataset_version_id": source.get("dataset_version_id"),
                "requested_method": requested_method,
                "effective_method": extraction.get("effective_method"),
                "fallback_policy": extraction.get("fallback_policy"),
                "fallback_reason": extraction.get("fallback_reason"),
                "status": extraction.get("status"),
                "processed": int(extraction.get("processed") or 0),
                "success": int(extraction.get("success") or 0),
            }
            for index, previous in enumerate(llm_executions):
                if previous.get("source_dataset_version_id") == entry["source_dataset_version_id"]:
                    llm_executions[index] = entry
                    break
            else:
                llm_executions.append(entry)
            run.stats = {**(run.stats or {}), "llm_executions": list(llm_executions)}
            db.commit()

        svc = DatasetService(db)
        sources = manifest.get("sources") or []
        if not sources:
            raise ValueError("PipelineRun manifest has no pinned source versions")

        connector_node_ids = _node_ids_for_type(definition, "connector")
        source_connector_ids = [
            [node_id for node_id in source.get("connector_node_ids", []) if node_id in connector_node_ids]
            for source in sources
        ]
        referenced_connector_ids = {
            node_id
            for ids in source_connector_ids
            for node_id in ids
        }
        legacy_connector_fallback = bool(connector_node_ids) and not referenced_connector_ids
        connector_remaining_sources = {
            node_id: sum(node_id in ids for ids in source_connector_ids)
            for node_id in referenced_connector_ids
        }
        if legacy_connector_fallback:
            connector_remaining_sources = {node_id: len(sources) for node_id in connector_node_ids}
        for node_id in connector_node_ids:
            if referenced_connector_ids and node_id not in referenced_connector_ids:
                set_node_status(
                    node_id,
                    "skipped",
                    "此连接器没有解析到本次 Run 使用的来源 DatasetVersion。",
                )
            else:
                set_node_status(node_id, "running")
        set_nodes_by_type("storage", "skipped", "当前执行器直接读取已固定的 DatasetVersion，没有单独执行存储节点。")

        transform_route, runtime_spec = _pipeline_runtime_config(pl)

        extraction_spec = runtime_spec.get("md_to_structured") or {}
        llm_requested = bool(
            extraction_spec.get("target_schema")
            or extraction_spec.get("auto_extract")
            or extraction_spec.get("llm_enrich")
        )
        pinned_model_config = None
        model_unavailable_reason = None
        if "llm_model_pin" in manifest:
            model_pin = manifest.get("llm_model_pin") or {}
            if model_pin.get("requested"):
                from app.services.model_config_selector import llm_call_kwargs, resolve_pinned_llm_model_config
                from app.services.v2.pipeline.steps.md_to_structured import prompt_provenance

                model_config, model_unavailable_reason = resolve_pinned_llm_model_config(db, model_pin)
                expected_prompts = model_pin.get("prompt_provenance") or {}
                current_prompts = prompt_provenance()
                if not expected_prompts:
                    model_config = None
                    model_unavailable_reason = "prompt_pin_missing"
                elif any(
                    method not in current_prompts
                    or current_prompts[method] != expected
                    for method, expected in expected_prompts.items()
                ):
                    model_config = None
                    model_unavailable_reason = "prompt_changed"
                if model_config is not None:
                    try:
                        call_kwargs = llm_call_kwargs(model_config)
                    except Exception:
                        call_kwargs = None
                    if call_kwargs:
                        models = list(model_config.models or [])
                        pinned_model_config = SimpleNamespace(
                            id=model_config.id,
                            provider=model_config.provider,
                            models=models,
                            _model_config_digest=model_pin.get("model_config_digest"),
                            _pinned_call_kwargs=call_kwargs,
                        )
                    else:
                        model_unavailable_reason = "model_unavailable"
            elif llm_requested:
                model_unavailable_reason = "model_unavailable"
        elif llm_requested and int(manifest.get("schema_version") or 1) >= 2:
            model_unavailable_reason = "model_pin_missing"
        elif llm_requested:
            # Compatibility for PipelineRuns queued before ModelConfig pinning.
            from app.services.model_config_selector import llm_call_kwargs, select_llm_model_config

            model_config = select_llm_model_config(
                db=db,
                model_id=extraction_spec.get("model_id"),
                purpose_tags=("结构化提取", "结构化抽取", "LLM结构化", "llm_structurize"),
                allow_vlm=False,
            )
            if model_config is not None:
                try:
                    call_kwargs = llm_call_kwargs(model_config)
                except Exception:
                    call_kwargs = None
                if call_kwargs:
                    from app.services.model_config_selector import model_config_provenance_snapshot

                    models = list(model_config.models or [])
                    config_snapshot = model_config_provenance_snapshot(model_config) or {}
                    pinned_model_config = SimpleNamespace(
                        id=model_config.id,
                        provider=model_config.provider,
                        models=models,
                        _model_config_digest=config_snapshot.get("config_digest"),
                        _pinned_call_kwargs=call_kwargs,
                    )
            if pinned_model_config is None:
                model_unavailable_reason = "model_unavailable"

        llm_attempt_recorder = None
        if llm_requested:
            from app.services.v2.pipeline.llm_invocation_recorder import PipelineLLMInvocationRecorder

            llm_attempt_recorder = PipelineLLMInvocationRecorder(
                db,
                pipeline_run_id=run.id,
                unavailable_reason=model_unavailable_reason,
                model_config_id=(manifest.get("llm_model_pin") or {}).get("model_config_id"),
                model_config_digest=(manifest.get("llm_model_pin") or {}).get("model_config_digest"),
                provider=(manifest.get("llm_model_pin") or {}).get("provider"),
                model_name=(manifest.get("llm_model_pin") or {}).get("model_name"),
            )

        outputs = []
        multi_source = len(sources) > 1
        transforms_remaining = len(sources)
        for source_index, pinned_source in enumerate(sources):
            source = dict(pinned_source)
            source["route"] = _source_runtime_route(source, transform_route, pl.route)
            data = _load_source_rows(db, svc, source)
            current_connector_ids = source_connector_ids[source_index]
            if not current_connector_ids and legacy_connector_fallback:
                current_connector_ids = connector_node_ids
            for node_id in current_connector_ids:
                connector_remaining_sources[node_id] = max(0, connector_remaining_sources[node_id] - 1)
                if connector_remaining_sources[node_id] == 0:
                    set_node_status(node_id, "success")
            ctx = PipelineContext(
                dataset_id=source["dataset_id"],
                version_no=source["version_no"],
                route=source["route"],
                spec=runtime_spec,
                runtime={
                    "llm_model_config": pinned_model_config,
                    "llm_attempt_recorder": llm_attempt_recorder,
                } if llm_requested else {},
            )
            if llm_attempt_recorder is not None:
                llm_attempt_recorder.set_source(source.get("dataset_version_id"))
            if source["route"] == "C":
                ctx.spec = dict(ctx.spec or {})
                existing_md_spec = ctx.spec.get("md_to_structured") or {}
                if existing_md_spec.get("auto_extract") or existing_md_spec.get("target_schema"):
                    existing_md_spec = dict(existing_md_spec)
                    existing_md_spec.setdefault("fallback_policy", "fail_closed")
                    ctx.spec["md_to_structured"] = existing_md_spec
                else:
                    ctx.spec["md_to_structured"] = {"rule_based": True, **existing_md_spec}
            ctx.rows_in = len(data)
            set_nodes_by_type("transform", "running")
            try:
                data, ctx = _execute_route(source["route"], ctx, data)
            except Exception:
                record_llm_execution(source, ctx.meta.get("md_to_structured"))
                raise
            extraction = ctx.meta.get("md_to_structured")
            record_llm_execution(source, extraction)
            if isinstance(extraction, dict) and extraction.get("status") == "degraded":
                run_degraded = True
            ctx.rows_out = len(data)
            transforms_remaining -= 1
            if transforms_remaining == 0:
                set_nodes_by_type("transform", "success")
            ctx.meta = {
                **(ctx.meta or {}),
                "source_dataset_version_id": source["dataset_version_id"],
                "source_dataset_version_no": source["version_no"],
                "source_dataset_checksum": source.get("checksum"),
                "pipeline_run_id": run.id,
                "pipeline_execution_digest": run.execution_digest,
            }
            set_nodes_by_type("output", "running")
            outputs.extend(_save_curated_outputs(db, svc, pl, source, data, ctx, multi_source))

        if legacy_connector_fallback:
            set_nodes_by_type("connector", "success")
        set_nodes_by_type("output", "success")

        if run_degraded:
            set_nodes_by_type(
                "transform",
                "degraded",
                "LLM 请求按 Pipeline 配置回退到规则提取；此输出需人工审核。",
            )

        curated_ids = [o["curated_dataset_id"] for o in outputs]
        if current_pipeline.updated_at and pipeline_snapshot.get("updated_at") == current_pipeline.updated_at.isoformat():
            current_pipeline.target_curated_ids = curated_ids
            resolved_routes = {
                _source_runtime_route(dict(source), transform_route, pl.route)
                for source in sources
            }
            if len(resolved_routes) == 1:
                current_pipeline.route = next(iter(resolved_routes))
            db.commit()

        llm_summary = _summarize_llm_executions(llm_executions)
        run.status = "degraded" if run_degraded else "success"
        run.finished_at = datetime.now(timezone.utc)
        run.stats = {
            "outcome": run.status,
            "rows_in": sum(o["rows_in"] for o in outputs),
            "rows_out": sum(o["rows_out"] for o in outputs),
            "meta": {
                "outputs": outputs,
                **({"md_to_structured": llm_summary} if llm_summary else {}),
            },
            "llm_executions": list(llm_executions),
            "node_status": node_status,
            "node_status_reason": node_status_reason,
            "curated_dataset_id": curated_ids[0] if curated_ids else None,
            "curated_dataset_ids": curated_ids,
            "execution_digest": run.execution_digest,
            "execution_mode": run.execution_mode,
            "source_versions": [
                {
                    "dataset_id": source["dataset_id"],
                    "dataset_version_id": source["dataset_version_id"],
                    "version_no": source["version_no"],
                    "checksum": source.get("checksum"),
                }
                for source in sources
            ],
        }
        db.commit()

    except Exception as e:
        logger.error(f"Pipeline run failed: {e}")
        if run:
            db.rollback()
            run.status = "failed"
            run.error_log = str(e)
            run.finished_at = datetime.now(timezone.utc)
            for node_id, status in list(node_status.items()):
                if status == "running":
                    node_status[node_id] = "failed"
                    node_status_reason[node_id] = str(e)[:1000]
            stats = dict(run.stats or {})
            llm_summary = _summarize_llm_executions(llm_executions)
            stats["outcome"] = "failed"
            stats.setdefault("rows_in", 0)
            stats.setdefault("rows_out", 0)
            if llm_summary:
                stats["meta"] = {
                    **(stats.get("meta") or {}),
                    "md_to_structured": llm_summary,
                }
                stats["llm_executions"] = list(llm_executions)
            stats["node_status"] = dict(node_status)
            stats["node_status_reason"] = dict(node_status_reason)
            run.stats = stats
            db.commit()
    finally:
        db.close()


def _get_node_type(definition: dict | None, node_id: str) -> str:
    """从 definition 中获取节点类型"""
    if not definition:
        return ""
    for n in definition.get("nodes", []):
        if n.get("id") == node_id:
            return n.get("type", "")
    return ""


if celery_app:
    pipeline_run_task = celery_app.task(pipeline_run_task)
    reconcile_pipeline_run_dispatch = celery_app.task(
        reconcile_pipeline_run_dispatch,
        name="v2.pipeline_run.reconcile",
        acks_late=True,
        reject_on_worker_lost=True,
    )
