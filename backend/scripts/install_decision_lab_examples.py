"""Install three bounded benchmark slices as separate, source-linked ontologies."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import app.main  # noqa: F401  register the models used by migration/startup
from app.database import SessionLocal
from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.models.relation import Relation
from app.models.user import User
from app.models.v2.construction import ConstructionRun, EvidenceRef
from app.models.v2.dataset import Dataset, DatasetVersion, MediaItem, MultimodalSample
from app.models.v2.decision_lab import DecisionLabRun
from app.services.storage_service import get_storage_service
from app.services.v2.decision_lab import (
    DATASET_IDS, DATASET_VERSION_IDS, DESCRIPTIONS, ONTOLOGY_IDS, PRESETS, ROOT,
    RULE_VERSION, calculate, canonical_sha, load_examples,
)
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.revision_service import create_revision


LABELS = {
    "ai4i": {"Record": "维护观测记录"},
    "factorynet": {"Episode": "加工批次", "Observation": "速度观测"},
    "ibadas": {"Scene": "采集场景", "InspectionSample": "质量复核样例"},
}


def _source_version(db, domain: str, source: dict) -> DatasetVersion:
    if domain != "ibadas":
        dataset_id, version_id = DATASET_IDS[domain], DATASET_VERSION_IDS[domain]
        raw = (ROOT / f"{domain}_examples.json").read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        version = db.get(DatasetVersion, version_id)
        if version is not None and version.checksum and version.checksum != checksum:
            raise ValueError(f"Installed {domain} slice changed; create a new version before replacing it")
        storage = get_storage_service()
        uri = storage.put_bytes("raw-datasets", f"datasets/{dataset_id}/v1/examples.json", raw, "application/json")
        dataset = db.get(Dataset, dataset_id)
        if dataset is None:
            dataset = Dataset(id=dataset_id, name={"ai4i": "AI4I 2020 · 维护决策 29 条样例", "factorynet": "FactoryNet CNC · 4 个批次 256 点"}[domain],
                              kind="structured", data_class=DESCRIPTIONS[domain]["data_class"],
                              readiness="ready", latest_version_id=version_id,
                              schema_json={"source_id": f"decision_lab_{domain}", "source_url": source["url"],
                                           "license": source.get("license"), "source_sha256": source["source_sha256"],
                                           "selection": source["selection"], "slice_sha256": checksum})
            db.add(dataset)
        else:
            dataset.latest_version_id = version_id
            dataset.readiness = "ready"
            dataset.schema_json = {**(dataset.schema_json or {}), "source_url": source["url"],
                                   "license": source.get("license"), "source_sha256": source["source_sha256"],
                                   "selection": source["selection"], "slice_sha256": checksum}
        if version is None:
            version = DatasetVersion(id=version_id, dataset_id=dataset_id, version_no=1,
                                     rowcount=len(load_examples(domain)[0]), storage_uri=uri, checksum=checksum,
                                     source_path=f"backend/data/decision_lab/{domain}_examples.json")
            db.add(version)
        else:
            version.storage_uri = uri
            version.checksum = checksum
        db.flush()
        return version
    dataset = next((item for item in db.query(Dataset).all()
                    if (item.schema_json or {}).get("source_id") == source["source_id"]), None)
    version = db.get(DatasetVersion, dataset.latest_version_id) if dataset and dataset.latest_version_id else None
    if not version or version.checksum != source["source_sha256"]:
        raise ValueError("I-BADAS source version is absent or changed")
    if not get_storage_service().object_exists(version.storage_uri):
        raise ValueError("I-BADAS source manifest is missing from object storage")
    return version


def _objects(domain: str, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    objects: list[dict] = []
    edges: list[dict] = []
    if domain == "ai4i":
        for row in rows:
            objects.append({"id": f"decision-lab:ai4i:record:{row['source_row_id']}", "entity_type": "Record",
                            "properties": row, "source_row_id": row["source_row_id"]})
    elif domain == "factorynet":
        episodes = sorted({row["episode_id"] for row in rows})
        for episode in episodes:
            items = [row for row in rows if row["episode_id"] == episode]
            episode_id = f"decision-lab:factorynet:episode:{episode}"
            objects.append({"id": episode_id, "entity_type": "Episode", "source_row_id": items[0]["source_row_id"],
                            "properties": {"episode_id": episode, "tool_condition": items[0]["tool_condition"],
                                           "visual_inspection": items[0]["visual_inspection"], "selected_points": len(items)}})
            for row in items:
                observation_id = f"decision-lab:factorynet:observation:{row['source_row_id']}"
                objects.append({"id": observation_id, "entity_type": "Observation", "properties": row,
                                "source_row_id": row["source_row_id"]})
                edges.append({"source": episode_id, "target": observation_id, "type": "HAS_OBSERVATION",
                              "properties": {"source_index": row["source_row_id"]}})
    else:
        scenes = sorted({row["scene_id"] for row in rows})
        for scene in scenes:
            scene_id = f"decision-lab:ibadas:scene:{scene}"
            objects.append({"id": scene_id, "entity_type": "Scene", "source_row_id": scene,
                            "properties": {"scene_id": scene}})
        for row in rows:
            sample_id = f"decision-lab:ibadas:sample:{row['sample_key']}"
            roles = sorted({asset["role"] for asset in row["assets"]})
            objects.append({"id": sample_id, "entity_type": "InspectionSample", "source_row_id": row["sample_key"],
                            "properties": {"sample_key": row["sample_key"], "scene_id": row["scene_id"],
                                           "official_label": row["label"], "asset_count": len(row["assets"]),
                                           "available_roles": roles}})
            edges.append({"source": f"decision-lab:ibadas:scene:{row['scene_id']}", "target": sample_id,
                          "type": "HAS_SAMPLE", "properties": {"sample_key": row["sample_key"]}})
    return objects, edges


def _ensure_schema(db, ontology_id: str, domain: str, objects: list[dict], edges: list[dict]) -> None:
    for kind, label in LABELS[domain].items():
        type_id = f"{ontology_id}:type:{kind}"
        if db.get(Entity, type_id) is None:
            names = sorted({key for obj in objects if obj["entity_type"] == kind for key in obj["properties"]})
            db.add(Entity(id=type_id, ontology_id=ontology_id, name_cn=label, name_en=kind, type="EntityType",
                          properties={"property_definitions": [{"id": key, "name": key, "type": "string"} for key in names]}))
    db.flush()
    if edges:
        source_kind, target_kind = ("Episode", "Observation") if domain == "factorynet" else ("Scene", "InspectionSample")
        relation_type = edges[0]["type"]
        relation_id = f"{ontology_id}:rel:{relation_type}"
        if db.get(Relation, relation_id) is None:
            db.add(Relation(id=relation_id, ontology_id=ontology_id,
                            source_entity=f"{ontology_id}:type:{source_kind}",
                            target_entity=f"{ontology_id}:type:{target_kind}", type=relation_type))
    db.flush()


def _seed_runs(db, admin: User, domain: str, version: DatasetVersion) -> None:
    for preset in PRESETS[domain]:
        request_id = f"prepared:{domain}:{preset['key']}:{RULE_VERSION[domain]}"
        existing = db.query(DecisionLabRun).filter_by(domain=domain, created_by=admin.id, client_request_id=request_id).first()
        if existing:
            source = load_examples(domain)[1]
            if (existing.result_json or {}).get("source") != source:
                corrected = {**existing.result_json, "source": source}
                existing.result_json = corrected
                existing.output_digest = canonical_sha(corrected)
            continue
        result = calculate(domain, preset["parameters"])
        db.add(DecisionLabRun(domain=domain, ontology_id=ONTOLOGY_IDS[domain], dataset_version_id=version.id,
                              case_key=preset["key"], case_name=preset["name"], rule_version=RULE_VERSION[domain],
                              parameters=result["parameters"], result_json=result, source_digest=result["source_digest"],
                              input_digest=canonical_sha({"domain": domain, "rule": RULE_VERSION[domain],
                                                          "source": result["source_digest"], "parameters": result["parameters"]}),
                              output_digest=canonical_sha(result), client_request_id=request_id, created_by=admin.id))


def install() -> list[dict]:
    graph = FalkorDBService()
    if not graph.available:
        raise RuntimeError("FalkorDB unavailable; decision-lab import not started")
    reports = []
    with SessionLocal() as db:
        admin = db.query(User).filter(User.role == "admin", User.is_active == True).order_by(User.created_at).first()
        if admin is None:
            raise RuntimeError("Initialize an active local admin before importing examples")
        for domain in ("ai4i", "factorynet", "ibadas"):
            rows, source, slice_digest = load_examples(domain)
            version = _source_version(db, domain, source)
            ontology_id = ONTOLOGY_IDS[domain]
            ontology = db.get(OntologyProject, ontology_id)
            if ontology is None:
                ontology = OntologyProject(id=ontology_id, name=DESCRIPTIONS[domain]["title"] + "本体",
                                           domain={"ai4i": "设备维护", "factorynet": "工业时序", "ibadas": "工业质检"}[domain],
                                           description=DESCRIPTIONS[domain]["summary"] + " 固定公开样例，规则计算仅供演示。",
                                           data_class=DESCRIPTIONS[domain]["data_class"], build_mode="rule", status="published", created_by=admin.id)
                db.add(ontology)
                db.flush()
            run_id = f"{ontology_id}:import"
            run = db.get(ConstructionRun, run_id)
            if run is None:
                run = ConstructionRun(id=run_id, ontology_id=ontology_id, dataset_id=version.dataset_id,
                                      mode=DESCRIPTIONS[domain]["data_class"], status="running",
                                      config={"adapter": "decision_lab_examples_v1", "source": source, "slice_sha256": slice_digest}, metrics={})
                db.add(run)
            elif (run.config or {}).get("slice_sha256") != slice_digest:
                raise ValueError(f"Previously installed {domain} ontology uses another slice")
            else:
                run.config = {**(run.config or {}), "source": source}
            objects, edges = _objects(domain, rows)
            _ensure_schema(db, ontology_id, domain, objects, edges)
            sample_ids = {}
            if domain == "ibadas":
                sample_ids = {item.sample_key: item.id for item in db.query(MultimodalSample).filter_by(dataset_version_id=version.id)}
                if any(row["sample_key"] not in sample_ids for row in rows):
                    raise ValueError("I-BADAS sample identity no longer matches the pinned source")
            for obj in objects:
                if db.get(EntityInstance, obj["id"]) is None:
                    db.add(EntityInstance(id=obj["id"], ontology_id=ontology_id,
                                          entity_id=f"{ontology_id}:type:{obj['entity_type']}",
                                          row_identity=obj["id"], row_data=obj["properties"], source_version=slice_digest))
                evidence_id = f"{ontology_id}:evidence:{obj['id']}"
                if db.get(EvidenceRef, evidence_id) is None:
                    evidence_text = json.dumps(obj["properties"], ensure_ascii=False, sort_keys=True)
                    media_id = None
                    if domain == "ibadas" and obj["entity_type"] == "InspectionSample":
                        media = db.query(MediaItem).filter_by(sample_id=sample_ids[obj["source_row_id"]], asset_role="rgb").first()
                        media_id = media.id if media else None
                    db.add(EvidenceRef(id=evidence_id, construction_run_id=run_id, ontology_id=ontology_id,
                                       assertion_id=obj["id"], assertion_kind="node", source_dataset_id=version.dataset_id,
                                       source_version=source.get("source_sha256"), source_file=source["url"],
                                       source_row_id=obj["source_row_id"], source_media_id=media_id,
                                       source_sample_id=sample_ids.get(obj["source_row_id"]), extractor="rule",
                                       evidence_text=evidence_text, content_hash=hashlib.sha256(evidence_text.encode()).hexdigest()))
            db.flush()
            if not ontology.current_revision_id:
                create_revision(db, ontology_id, source_run_id=run_id, summary={"source": source["url"], "slice_sha256": slice_digest})
            db.commit()
            try:
                graph.upsert_instances(ontology_id, [{"id": obj["id"], "entity_type": obj["entity_type"],
                    "properties": {key: value for key, value in obj["properties"].items()
                                   if value is not None and not isinstance(value, (dict, list))}} for obj in objects])
                if edges:
                    graph.upsert_relations(ontology_id, edges)
                count = graph._graph(ontology_id).query("MATCH (n:Instance) RETURN count(n)").result_set[0][0]
                relation_count = graph._graph(ontology_id).query("MATCH (:Instance)-[r]->(:Instance) RETURN count(r)").result_set[0][0]
                if count != len(objects) or relation_count != len(edges):
                    raise RuntimeError(f"Projection count mismatch: {count}/{relation_count}")
                run.status, run.error = "completed", None
                run.metrics = {"objects": count, "links": relation_count, "selected_examples": len(rows)}
                _seed_runs(db, admin, domain, version)
                db.commit()
            except Exception as exc:
                run.status, run.error = "failed", str(exc)
                db.commit()
                raise
            reports.append({"domain": domain, "ontology_id": ontology_id, "dataset_id": version.dataset_id,
                            "version_id": version.id, "examples": len(rows), "objects": count, "links": relation_count,
                            "prepared_runs": len(PRESETS[domain])})
    return reports


if __name__ == "__main__":
    print(json.dumps(install(), ensure_ascii=False, indent=2))
