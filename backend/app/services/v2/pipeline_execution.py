"""Immutable input manifests for replayable Source-to-Ontology Pipeline runs."""
from __future__ import annotations

import copy
import hashlib
import json

from sqlalchemy.orm import Session


class PipelineExecutionError(ValueError):
    """A Pipeline cannot be safely pinned to immutable executable inputs."""


def content_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_pipeline_snapshot(pipeline, *, version: int | None = None) -> dict:
    """Copy the complete executable Pipeline contract without ORM references."""
    return {
        "pipeline_id": pipeline.id,
        "name": pipeline.name,
        "domain": pipeline.domain,
        "description": pipeline.description,
        "version": version if version is not None else pipeline.version,
        "source_dataset_id": pipeline.source_dataset_id,
        "route": pipeline.route,
        "spec": copy.deepcopy(pipeline.spec or {}),
        "definition": copy.deepcopy(pipeline.definition),
        "target_curated_ids": copy.deepcopy(pipeline.target_curated_ids or []),
        "updated_at": pipeline.updated_at.isoformat() if pipeline.updated_at else None,
    }


def _route_for_kind(kind: str | None, default_route: str | None = None) -> str:
    if default_route in ("A", "B", "C"):
        return default_route
    if kind == "semi":
        return "B"
    if kind == "unstructured":
        return "C"
    return "A"


def _find_dataset_for_file(db: Session, filename: str):
    from app.models.v2.dataset import Dataset, DatasetVersion

    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
    candidates = db.query(Dataset).filter(
        Dataset.name == stem
    ).order_by(Dataset.created_at.desc()).limit(20).all()
    for candidate in candidates:
        version = db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == candidate.id
        ).order_by(DatasetVersion.version_no.desc()).first()
        if version and ((version.rowcount or 0) > 0 or version.storage_uri):
            return candidate
    return candidates[0] if candidates else None


def collect_pipeline_sources(db: Session, snapshot: dict) -> list[dict]:
    """Resolve the Pipeline's source dataset identities from its snapshot."""
    from app.models.v2.dataset import Dataset

    sources: list[dict] = []
    definition = snapshot.get("definition") or {}
    for node in definition.get("nodes") or []:
        if node.get("type") != "connector":
            continue
        connector_node_id = node.get("id")
        config = node.get("config") or {}
        for file_info in config.get("files") or []:
            if not isinstance(file_info, dict):
                continue
            filename = file_info.get("name") or file_info.get("filename") or ""
            dataset_id = file_info.get("dataset_id")
            dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first() if dataset_id else None
            if not dataset and filename:
                dataset = _find_dataset_for_file(db, filename)
            if dataset:
                sources.append({
                    "dataset_id": dataset.id,
                    "filename": filename or dataset.name,
                    "kind": dataset.kind,
                    "route": _route_for_kind(dataset.kind),
                    "connector_node_ids": [connector_node_id] if connector_node_id else [],
                })

    if not sources and snapshot.get("source_dataset_id"):
        dataset = db.query(Dataset).filter(Dataset.id == snapshot["source_dataset_id"]).first()
        if dataset:
            sources.append({
                "dataset_id": dataset.id,
                "filename": dataset.name,
                "kind": dataset.kind,
                "route": _route_for_kind(dataset.kind, snapshot.get("route")),
                "connector_node_ids": [],
            })

    unique_sources: list[dict] = []
    seen: dict[str, dict] = {}
    for source in sources:
        existing = seen.get(source["dataset_id"])
        if existing:
            existing_ids = existing.setdefault("connector_node_ids", [])
            for node_id in source.get("connector_node_ids") or []:
                if node_id not in existing_ids:
                    existing_ids.append(node_id)
            continue
        seen[source["dataset_id"]] = source
        unique_sources.append(source)
    return unique_sources


def _pin_sources(db: Session, sources: list[dict]) -> list[dict]:
    from app.models.v2.dataset import Dataset, DatasetVersion

    pinned: list[dict] = []
    for source in sources:
        dataset = db.query(Dataset).filter(
            Dataset.id == source["dataset_id"]
        ).with_for_update().first()
        if dataset is None:
            raise PipelineExecutionError(f"Source dataset {source['dataset_id']} no longer exists")

        if dataset.latest_version_id:
            version = db.query(DatasetVersion).filter(
                DatasetVersion.id == dataset.latest_version_id,
                DatasetVersion.dataset_id == dataset.id,
            ).first()
            if version is None:
                raise PipelineExecutionError(f"Source dataset {dataset.id} has an invalid latest version pointer")
        else:
            version = db.query(DatasetVersion).filter(
                DatasetVersion.dataset_id == dataset.id
            ).order_by(DatasetVersion.version_no.desc()).first()

        if version is None or not version.storage_uri:
            raise PipelineExecutionError(f"Source dataset {dataset.id} has no readable DatasetVersion")

        pinned.append({
            **source,
            "dataset_version_id": version.id,
            "version_no": version.version_no,
            "rowcount": version.rowcount,
            "checksum": version.checksum,
            "storage_uri": version.storage_uri,
            "source_path": version.source_path,
        })
    return pinned


def _pipeline_llm_request(snapshot: dict) -> dict:
    """Resolve the effective structured-extraction request from the pinned spec."""
    base = snapshot.get("spec") if isinstance(snapshot.get("spec"), dict) else {}
    extraction = dict(base.get("md_to_structured") or {})
    requested = bool(
        extraction.get("target_schema")
        or extraction.get("auto_extract")
        or extraction.get("llm_enrich")
    )
    for node in (snapshot.get("definition") or {}).get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") != "transform":
            continue
        config = node.get("config") or {}
        direct = config.get("md_to_structured")
        if isinstance(direct, dict):
            extraction.update(direct)
            requested = requested or bool(
                direct.get("target_schema") or direct.get("auto_extract") or direct.get("llm_enrich")
            )
        for step in config.get("steps") or []:
            if not isinstance(step, dict):
                continue
            op_name = step.get("op")
            params = step.get("params") if isinstance(step.get("params"), dict) else {}
            if op_name == "llm_structurize":
                extraction.update(params)
                extraction["auto_extract"] = True
                requested = True
            elif op_name == "llm_enrich":
                if params.get("fields"):
                    extraction["enrich_fields"] = params["fields"]
                if "fallback_policy" in params:
                    extraction["fallback_policy"] = params["fallback_policy"]
                extraction["llm_enrich"] = True
                requested = True
    return {"requested": requested, **extraction}


def _pipeline_model_pin(db: Session, snapshot: dict) -> dict | None:
    extraction = _pipeline_llm_request(snapshot)
    if not extraction.get("requested"):
        return None

    from app.models.model_config import ModelConfig
    from app.services.model_config_selector import (
        model_config_provenance_snapshot,
        select_llm_model_config,
    )
    from app.services.v2.pipeline.steps.md_to_structured import prompt_provenance

    model_id = extraction.get("model_id")
    if model_id:
        config = db.query(ModelConfig).filter(
            ModelConfig.id == model_id,
            ModelConfig.config_type == "llm",
        ).first()
    else:
        config = select_llm_model_config(
            db=db,
            purpose_tags=("结构化提取", "结构化抽取", "LLM结构化", "llm_structurize"),
            allow_vlm=False,
        )

    prompt_map = prompt_provenance()
    methods = []
    if extraction.get("target_schema"):
        methods.append("llm_schema")
    elif extraction.get("auto_extract"):
        methods.append("llm_auto")
    if extraction.get("llm_enrich"):
        methods.append("llm_enrich")
    model_names = getattr(config, "models", None) if config else None
    if isinstance(model_names, (list, tuple)):
        model_name = str(model_names[0]) if model_names and model_names[0] else None
    else:
        model_name = str(model_names) if model_names else None
    return {
        "requested": True,
        "model_config_id": getattr(config, "id", None),
        "model_config_digest": (
            model_config_provenance_snapshot(config) or {}
        ).get("config_digest"),
        "provider": getattr(config, "provider", None),
        "model_name": model_name,
        "prompt_provenance": {name: prompt_map[name] for name in methods},
        "provider_model_artifact_reproducibility": "unverified",
    }


def build_pipeline_run_manifest(
    db: Session,
    pipeline,
    *,
    trigger_mode: str = "manual",
    pipeline_version_id: str | None = None,
    trigger_context: dict | None = None,
) -> dict:
    """Pin the published snapshot (or explicit preview snapshot) and every input version."""
    from app.models.v2.pipeline import PipelineVersion

    pinned_version_id = None
    if pipeline_version_id:
        published = db.query(PipelineVersion).filter(
            PipelineVersion.id == pipeline_version_id,
            PipelineVersion.pipeline_id == pipeline.id,
            PipelineVersion.status == "published",
        ).first()
        if not published or not published.execution_snapshot:
            raise PipelineExecutionError("Pinned Pipeline version is missing or is not a published snapshot")
        snapshot = copy.deepcopy(published.execution_snapshot)
        actual_digest = content_digest(snapshot)
        if not published.snapshot_digest or actual_digest != published.snapshot_digest:
            raise PipelineExecutionError("Pinned Pipeline snapshot failed its digest check")
        if content_digest(snapshot.get("definition") or {}) != published.definition_digest:
            raise PipelineExecutionError("Pinned Pipeline definition failed its digest check")
        pinned_version_id = published.id
        execution_mode = "published"
    elif pipeline.status == "published":
        published = db.query(PipelineVersion).filter(
            PipelineVersion.pipeline_id == pipeline.id,
            PipelineVersion.version == pipeline.version,
            PipelineVersion.status == "published",
        ).first()
        if published and published.execution_snapshot:
            snapshot = copy.deepcopy(published.execution_snapshot)
            actual_digest = content_digest(snapshot)
            if not published.snapshot_digest or actual_digest != published.snapshot_digest:
                raise PipelineExecutionError("Published Pipeline snapshot failed its digest check")
            if content_digest(snapshot.get("definition") or {}) != published.definition_digest:
                raise PipelineExecutionError("Published Pipeline definition failed its digest check")
            pinned_version_id = published.id
            execution_mode = "published"
        else:
            # Older releases did not persist spec/route/source data. Pin the
            # current state as an explicitly identified compatibility snapshot.
            snapshot = make_pipeline_snapshot(pipeline)
            execution_mode = "legacy_published_snapshot"
    else:
        snapshot = make_pipeline_snapshot(pipeline)
        execution_mode = "preview"

    sources = _pin_sources(db, collect_pipeline_sources(db, snapshot))
    if not sources:
        raise PipelineExecutionError("Pipeline has no readable source datasets")

    snapshot_digest = content_digest(snapshot)
    manifest = {
        "schema_version": 2,
        "execution_mode": execution_mode,
        "trigger_mode": trigger_mode,
        "pipeline_version_id": pinned_version_id,
        "pipeline_snapshot": snapshot,
        "pipeline_snapshot_digest": snapshot_digest,
        "definition_digest": content_digest(snapshot.get("definition") or {}),
        "sources": sources,
    }
    if trigger_context:
        manifest["trigger_context"] = copy.deepcopy(trigger_context)
    model_pin = _pipeline_model_pin(db, snapshot)
    if model_pin is not None:
        manifest["llm_model_pin"] = model_pin
    manifest["execution_digest"] = content_digest(manifest)
    return manifest


def verify_pipeline_run_manifest(manifest: dict, expected_digest: str | None) -> None:
    if not isinstance(manifest, dict) or not expected_digest:
        raise PipelineExecutionError("PipelineRun has no immutable execution manifest; create a new Run")
    payload = dict(manifest)
    recorded_digest = payload.pop("execution_digest", None)
    actual_digest = content_digest(payload)
    if recorded_digest != expected_digest or actual_digest != expected_digest:
        raise PipelineExecutionError("PipelineRun execution manifest failed its digest check")
    snapshot = manifest.get("pipeline_snapshot")
    if not isinstance(snapshot, dict):
        raise PipelineExecutionError("PipelineRun is missing its Pipeline snapshot")
    if content_digest(snapshot) != manifest.get("pipeline_snapshot_digest"):
        raise PipelineExecutionError("PipelineRun Pipeline snapshot failed its digest check")
