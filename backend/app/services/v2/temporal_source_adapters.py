"""Versioned source adapters for replay-pinned What-if targets."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.models.entity import Entity
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.ontology_revision import OntologyRevision
from app.models.v2.temporal_replay import TemporalReplay, TemporalStreamEvent
from app.models.v2.semantic_core import OntologySemanticResourceVersion

FACTORYNET_SOURCE_ID = "factorynet_cnc"
FACTORYNET_OBSERVATION_PREFIX = "FactoryNet:Observation:"


class TemporalSourceAdapterError(ValueError):
    """A replay does not satisfy a registered temporal source contract."""

    code = "WHAT_IF_SOURCE_ADAPTER_INVALID"


@dataclass(frozen=True)
class TemporalSourceAdapter:
    adapter_id: str
    version: str
    source_id: str
    target_kind: Literal["factorynet_observation", "mapped_entity"]

    @property
    def is_factorynet(self) -> bool:
        return self.target_kind == "factorynet_observation"

    def mapping_digest(self, replay: TemporalReplay) -> str:
        mapping = (replay.config or {}).get("mapping") if isinstance(replay.config, dict) else None
        value = {
            "adapter_id": self.adapter_id,
            "adapter_version": self.version,
            "source_id": replay.source_id,
            "mapping": mapping if isinstance(mapping, dict) else None,
        }
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def parse_target(self, target_instance_id: str) -> tuple[str | None, str | None]:
        if not self.is_factorynet or not target_instance_id.startswith(FACTORYNET_OBSERVATION_PREFIX):
            return None, None
        tail = target_instance_id[len(FACTORYNET_OBSERVATION_PREFIX):]
        if ":" not in tail:
            return None, None
        return tuple(tail.rsplit(":", 1))  # episode_id, source_row_id

    def resolve_event(
        self,
        db,
        replay: TemporalReplay,
        target_instance_id: str,
        episode_id: str | None,
        source_row_id: str | None,
        as_of_ordinal: Decimal | None,
    ) -> TemporalStreamEvent | None:
        if replay.source_id != self.source_id:
            return None
        query = db.query(TemporalStreamEvent).filter(
            TemporalStreamEvent.replay_id == replay.id,
            TemporalStreamEvent.status == "committed",
        )
        if episode_id:
            query = query.filter(TemporalStreamEvent.episode_id == episode_id)
        if self.is_factorynet:
            if source_row_id is None:
                return None
            row_filter = TemporalStreamEvent.source_row_id == source_row_id
            if source_row_id.isdigit():
                row_filter = row_filter | (TemporalStreamEvent.source_sequence == int(source_row_id))
            return query.filter(row_filter).order_by(
                TemporalStreamEvent.ordinal.asc(),
                TemporalStreamEvent.source_sequence.asc(),
                TemporalStreamEvent.id.asc(),
            ).first()

        query = query.filter(
            (TemporalStreamEvent.entity_key == target_instance_id)
            | (TemporalStreamEvent.source_row_id == target_instance_id)
            | (TemporalStreamEvent.id == target_instance_id)
            | (TemporalStreamEvent.event_key == target_instance_id)
        )
        if as_of_ordinal is not None:
            query = query.filter(TemporalStreamEvent.ordinal <= as_of_ordinal)
        return query.order_by(
            TemporalStreamEvent.ordinal.desc(),
            TemporalStreamEvent.source_sequence.desc(),
            TemporalStreamEvent.id.desc(),
        ).first()

    def validate_pins(self, db, replay: TemporalReplay, version: DatasetVersion) -> None:
        if (
            not self.source_id
            or replay.schema_revision_id is None
            or replay.source_mode not in ({"file_replay", "push", "simulated_live"} if self.is_factorynet else {"file_replay", "push"})
            or not replay.dataset_id
            or version.id != replay.dataset_version_id
            or version.dataset_id != replay.dataset_id
        ):
            raise TemporalSourceAdapterError("回放缺少适配器要求的固定来源、schema 或 DatasetVersion")
        dataset = db.get(Dataset, version.dataset_id)
        if dataset is None:
            raise TemporalSourceAdapterError("固定 DatasetVersion 的来源数据集不存在")
        manifest = dataset.schema_json if isinstance(dataset.schema_json, dict) else {}
        manifest_source = manifest.get("source_id")
        if self.is_factorynet:
            if manifest_source not in {None, self.source_id} and dataset.name != "FactoryNet CNC 铣削时序数据":
                raise TemporalSourceAdapterError("固定数据集不属于 FactoryNet TemporalSourceAdapter")
            return

        mapping = (replay.config or {}).get("mapping") if isinstance(replay.config, dict) else None
        if manifest_source != self.source_id:
            raise TemporalSourceAdapterError("固定数据集的 source_id 与回放不一致")
        if (
            not isinstance(mapping, dict)
            or not isinstance(mapping.get("entity_type"), str)
            or not mapping["entity_type"].strip()
            or not isinstance(mapping.get("properties"), dict)
            or not mapping["properties"]
        ):
            raise TemporalSourceAdapterError("通用时序来源缺少回放时固定的对象/属性映射")
        if any(
            not isinstance(source_field, str)
            or not source_field.strip()
            or not isinstance(target_field, str)
            or not target_field.strip()
            or target_field.startswith("_")
            for source_field, target_field in mapping["properties"].items()
        ):
            raise TemporalSourceAdapterError("通用来源属性映射格式无效")
        if replay.source_mode == "file_replay" and not isinstance(mapping.get("entity_key_field"), str):
            raise TemporalSourceAdapterError("文件回放缺少固定的 entity_key_field")
        revision = db.query(OntologyRevision).filter(
            OntologyRevision.id == replay.schema_revision_id,
            OntologyRevision.ontology_id == replay.ontology_id,
        ).first()
        snapshot = revision.snapshot_json if revision and isinstance(revision.snapshot_json, dict) else {}
        mapped_entity = next((entity for entity in snapshot.get("entities", []) if isinstance(entity, dict) and str(mapping["entity_type"]) in {
            str(entity.get("id") or ""),
            str(entity.get("name_en") or ""),
            str(entity.get("name_cn") or ""),
            str(entity.get("canonical_id") or ""),
        }), None)
        if mapped_entity is None:
            raise TemporalSourceAdapterError("通用来源对象映射不属于固定 schema revision")
        mapped_entity_id = str(mapped_entity.get("id") or "")
        if not mapped_entity_id:
            raise TemporalSourceAdapterError("通用来源对象缺少固定 schema 对象 ID")
        link_specs = mapping.get("links", [])
        if not isinstance(link_specs, list) or len(link_specs) > 64:
            raise TemporalSourceAdapterError("通用来源关系映射数量或格式无效")
        seen_link_fields: set[str] = set()
        for spec in link_specs:
            if not isinstance(spec, dict):
                raise TemporalSourceAdapterError("通用来源关系映射格式无效")
            source_field = str(spec.get("source_field") or "").strip()
            link_type_id = str(spec.get("link_type_id") or "").strip()
            if (
                not source_field
                or not link_type_id
                or source_field in seen_link_fields
                or source_field in mapping["properties"]
            ):
                raise TemporalSourceAdapterError("通用来源关系字段重复或缺少固定 Link Type ID")
            seen_link_fields.add(source_field)
        if link_specs:
            mapped_entity_row = db.query(Entity).filter(
                Entity.ontology_id == replay.ontology_id,
                Entity.id == mapped_entity_id,
                Entity.is_source_deleted.is_(False),
            ).first()
            semantic_resource_id = str(mapped_entity_row.semantic_resource_id or "") if mapped_entity_row else ""
            if not semantic_resource_id:
                raise TemporalSourceAdapterError("通用来源关系映射对象缺少固定语义资源 ID")
        else:
            semantic_resource_id = ""
        for spec in link_specs:
            if not isinstance(spec, dict):
                raise TemporalSourceAdapterError("通用来源关系映射格式无效")
            link_version = db.query(OntologySemanticResourceVersion).filter(
                OntologySemanticResourceVersion.ontology_id == replay.ontology_id,
                OntologySemanticResourceVersion.revision_id == replay.schema_revision_id,
                OntologySemanticResourceVersion.resource_id == str(spec.get("link_type_id") or spec.get("semantic_resource_id") or ""),
                OntologySemanticResourceVersion.kind == "link_type",
            ).first()
            if (
                link_version is None
                or link_version.source_resource_id != semantic_resource_id
                or link_version.target_resource_id != semantic_resource_id
                or str(spec.get("semantic_resource_id") or link_version.resource_id) != str(link_version.resource_id)
                or str(spec.get("relation_type") or link_version.api_name) != str(link_version.api_name)
            ):
                raise TemporalSourceAdapterError("通用来源关系映射不属于固定 schema revision 的自关系")


FACTORYNET_TEMPORAL_ADAPTER = TemporalSourceAdapter(
    adapter_id="project4.factorynet-temporal",
    version="1.0.0",
    source_id=FACTORYNET_SOURCE_ID,
    target_kind="factorynet_observation",
)
MAPPED_TEMPORAL_ADAPTER = TemporalSourceAdapter(
    adapter_id="project4.mapped-temporal-source",
    version="1.0.0",
    source_id="*",
    target_kind="mapped_entity",
)


def adapter_for_replay(replay: TemporalReplay) -> TemporalSourceAdapter | None:
    """Resolve only the built-in FactoryNet and explicitly mapped source contracts."""
    source_id = str(replay.source_id or "").strip()
    if source_id == FACTORYNET_SOURCE_ID:
        return FACTORYNET_TEMPORAL_ADAPTER
    if source_id and replay.source_mode in {"file_replay", "push"}:
        return TemporalSourceAdapter(
            adapter_id=MAPPED_TEMPORAL_ADAPTER.adapter_id,
            version=MAPPED_TEMPORAL_ADAPTER.version,
            source_id=source_id,
            target_kind=MAPPED_TEMPORAL_ADAPTER.target_kind,
        )
    return None
