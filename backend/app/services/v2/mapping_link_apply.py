"""Preview and execute snapshot-pinned Link Mapping relationship Apply."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.relation import Relation
from app.models.v2.curated import CuratedDataset, CuratedReview
from app.models.v2.mapping import OntologyLinkMapping, OntologyMapping
from app.models.v2.mapping_apply_run import MappingApplyRun
from app.models.v2.mapping_definition_review import MappingDefinitionReview
from app.models.v2.mapping_link_apply_run import (
    MappingLinkApplyRun,
    MappingLinkRelationChange,
    MappingLinkRelationProjectionState,
)
from app.models.v2.mapping_link_definition_review import MappingLinkDefinitionReview
from app.models.v2.mapping_row_change import MappingRowChange


MAX_PROJECTION_BATCH = 200
MAX_PROJECTION_ATTEMPTS = 8
PROJECTION_LEASE_SECONDS = 900
_RELATION_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
LEGACY_RELATION_RECONCILIATION_SCAN_LIMIT = 5000
LEGACY_RELATION_RECONCILIATION_RETURN_LIMIT = 100


class LinkApplyError(Exception):
    def __init__(self, error_code: str, *, permanent: bool = True):
        self.error_code = error_code
        self.permanent = permanent
        super().__init__(error_code)


def _canonical_digest(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _review_is_independently_approved(review: CuratedReview) -> bool:
    if (review.status != "approved" or not review.dataset_version_id
            or not review.requested_by or not review.decided_by
            or str(review.requested_by) == str(review.decided_by)):
        return False
    return not review.reviewer_id or str(review.reviewer_id) == str(review.decided_by)


def _latest_approved_data_review(db: Session, dataset_id: str) -> CuratedReview:
    review = db.query(CuratedReview).filter(
        CuratedReview.curated_dataset_id == dataset_id,
        CuratedReview.status == "approved",
        CuratedReview.dataset_version_id.is_not(None),
    ).order_by(
        CuratedReview.decided_at.desc(), CuratedReview.created_at.desc(), CuratedReview.id.desc(),
    ).first()
    if review is None or not _review_is_independently_approved(review):
        raise LinkApplyError("approved_dataset_review_required")
    dataset = db.get(CuratedDataset, dataset_id)
    if dataset is None or dataset.status != "approved":
        raise LinkApplyError("dataset_not_approved")
    return review


def _entity_mapping_apply_pin(
    db: Session,
    *,
    ontology_id: str,
    dataset_id: str,
    data_review: CuratedReview,
    requested_run_id: str | None = None,
) -> tuple[OntologyMapping, MappingApplyRun, MappingDefinitionReview]:
    from app.services.v2.mapping_apply_runs import mapping_definition_snapshot

    query = db.query(MappingApplyRun).filter(
        MappingApplyRun.ontology_id == ontology_id,
        MappingApplyRun.curated_dataset_id == dataset_id,
        MappingApplyRun.review_id == data_review.id,
        MappingApplyRun.dataset_version_id == data_review.dataset_version_id,
        MappingApplyRun.status == "succeeded",
    )
    if requested_run_id:
        query = query.filter(MappingApplyRun.id == requested_run_id)
    runs = query.order_by(MappingApplyRun.completed_at.desc(), MappingApplyRun.id.desc()).all()
    mapping_ids = {str(run.mapping_id) for run in runs}
    if len(mapping_ids) != 1 or not runs:
        raise LinkApplyError("entity_mapping_apply_required")
    run = runs[0]
    mapping = db.query(OntologyMapping).filter(
        OntologyMapping.id == run.mapping_id,
        OntologyMapping.ontology_id == ontology_id,
        OntologyMapping.curated_dataset_id == dataset_id,
        OntologyMapping.status == "applied",
    ).first()
    if mapping is None:
        raise LinkApplyError("entity_mapping_unavailable")
    mapping_review_id = run.mapping_review_id
    mapping_review = db.query(MappingDefinitionReview).filter(
        MappingDefinitionReview.id == mapping_review_id,
        MappingDefinitionReview.mapping_id == mapping.id,
        MappingDefinitionReview.ontology_id == ontology_id,
        MappingDefinitionReview.status == "approved",
    ).first()
    if mapping_review is None:
        raise LinkApplyError("approved_entity_mapping_review_required")
    try:
        _snapshot, live_digest = mapping_definition_snapshot(mapping)
    except Exception:
        raise LinkApplyError("entity_mapping_unavailable") from None
    if live_digest != mapping_review.candidate_digest or live_digest != run.mapping_digest:
        raise LinkApplyError("entity_mapping_changed")
    if (mapping_review.submitted_by and mapping_review.decided_by
            and str(mapping_review.submitted_by) == str(mapping_review.decided_by)):
        raise LinkApplyError("entity_mapping_review_invalid")
    if mapping_review.reviewer_id and str(mapping_review.decided_by or "") != str(mapping_review.reviewer_id):
        raise LinkApplyError("entity_mapping_review_invalid")
    if not isinstance(run.result_json, dict):
        raise LinkApplyError("entity_mapping_identity_unavailable")
    schema_report = run.result_json.get("schema_report")
    if not isinstance(schema_report, dict) or not schema_report.get("primary_key_column"):
        raise LinkApplyError("entity_mapping_identity_unavailable")

    return mapping, run, mapping_review


def _assert_entity_rows_projected(db: Session, mapping: OntologyMapping, entity_ids: set[str]) -> None:
    if not entity_ids:
        raise LinkApplyError("entity_mapping_has_no_instances")
    changes = db.query(MappingRowChange).filter(
        MappingRowChange.mapping_id == mapping.id,
        MappingRowChange.object_id.in_(sorted(entity_ids)),
    ).order_by(MappingRowChange.created_at.desc(), MappingRowChange.id.desc()).all()
    latest: dict[str, MappingRowChange] = {}
    for change in changes:
        latest.setdefault(str(change.object_id), change)
    if set(latest) != entity_ids or any(row.projection_status != "projected" for row in latest.values()):
        raise LinkApplyError("entity_projection_pending")


def _effective_rows(
    db: Session,
    *,
    dataset_id: str,
    data_review: CuratedReview,
    mapping: OntologyMapping,
    mapping_run: MappingApplyRun,
) -> tuple[list[dict], dict, str, list[str], str]:
    from app.services.v2.curated.review_service import ReviewService
    from app.services.v2.dataset_service import DatasetService
    from app.services.v2.mapping.mapping_service import MappingService

    try:
        version, original, source_manifest = DatasetService(db).read_complete_version_snapshot(
            dataset_id, str(data_review.dataset_version_id),
        )
    except Exception:
        raise LinkApplyError("dataset_snapshot_unavailable") from None
    try:
        pk_column = str(mapping_run.result_json["schema_report"]["primary_key_column"])
        rows = ReviewService(db).apply_edits_to_snapshot(
            data_review.id,
            original,
            row_identity_column=pk_column,
            expected_dataset_version_id=str(version.id),
        )
    except Exception:
        raise LinkApplyError("dataset_review_edits_invalid") from None
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise LinkApplyError("dataset_snapshot_empty_or_invalid")
    try:
        rows_digest = _canonical_digest({"rows": rows})
        helper = MappingService(db)
        identities: list[str] = []
        for row in rows:
            identity = helper._stable_row_id(mapping, row, pk_column if pk_column in row else None)
            identities.append(str(identity))
    except Exception:
        raise LinkApplyError("entity_mapping_identity_invalid") from None
    if len(set(identities)) != len(rows):
        raise LinkApplyError("entity_mapping_identity_not_unique")

    instances = db.query(EntityInstance).filter(
        EntityInstance.ontology_id == mapping.ontology_id,
        EntityInstance.source_mapping_id == mapping.id,
        EntityInstance.source_version == str(version.id),
        EntityInstance.id.in_(identities),
        EntityInstance.is_deleted.is_(False),
    ).all()
    instances_by_id = {str(item.id): item for item in instances}
    if set(instances_by_id) != set(identities):
        raise LinkApplyError("entity_instances_do_not_match_pinned_version")
    for row, identity, instance in zip(rows, identities, (instances_by_id[value] for value in identities)):
        if str(instance.id) != identity:
            raise LinkApplyError("entity_instances_do_not_match_pinned_version")
    _assert_entity_rows_projected(db, mapping, set(identities))
    entity_records = db.query(Entity.id).filter(
        Entity.ontology_id == mapping.ontology_id,
        Entity.id.in_(identities),
        Entity.is_source_deleted.is_(False),
    ).all()
    if {str(item[0]) for item in entity_records} != set(identities):
        raise LinkApplyError("entity_instances_do_not_match_pinned_version")
    manifest = {
        **source_manifest,
        "curated_review_id": str(data_review.id),
        "effective_rows_digest": rows_digest,
        "primary_key_column": pk_column,
        "mapping_id": str(mapping.id),
        "mapping_review_id": str(mapping_run.mapping_review_id),
        "mapping_apply_run_id": str(mapping_run.id),
        "mapping_digest": str(mapping_run.mapping_digest),
    }
    return rows, manifest, rows_digest, identities, pk_column


def _join_value(row: dict, key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        raise LinkApplyError("link_key_must_be_scalar")
    normalized = str(value).strip()
    return normalized or None


def _link_review_and_definition(
    db: Session,
    *,
    link_mapping_id: str,
    ontology_id: str,
    link_review_id: str,
) -> tuple[OntologyLinkMapping, MappingLinkDefinitionReview, dict, str]:
    from app.services.v2.mapping_link_definition_reviews import link_mapping_snapshot

    link = db.query(OntologyLinkMapping).filter(
        OntologyLinkMapping.id == link_mapping_id,
        OntologyLinkMapping.ontology_id == ontology_id,
    ).first()
    review = db.query(MappingLinkDefinitionReview).filter(
        MappingLinkDefinitionReview.id == link_review_id,
        MappingLinkDefinitionReview.link_mapping_id == link_mapping_id,
        MappingLinkDefinitionReview.ontology_id == ontology_id,
        MappingLinkDefinitionReview.status == "approved",
        MappingLinkDefinitionReview.decision == "approved",
    ).first()
    if link is None or review is None:
        raise LinkApplyError("approved_link_mapping_review_required")
    if (not review.submitted_by or not review.decided_by
            or str(review.submitted_by) == str(review.decided_by)
            or (review.reviewer_id and str(review.reviewer_id) != str(review.decided_by))):
        raise LinkApplyError("link_mapping_review_invalid")
    try:
        snapshot, digest = link_mapping_snapshot(link)
    except Exception:
        raise LinkApplyError("link_mapping_definition_invalid") from None
    if digest != review.candidate_digest or snapshot != review.candidate_json:
        raise LinkApplyError("link_mapping_definition_changed")
    if not _RELATION_TYPE_RE.fullmatch(str(snapshot["relation_type"])):
        raise LinkApplyError("link_mapping_relation_type_invalid")
    if not link.src_dataset_id or not link.tgt_dataset_id:
        raise LinkApplyError("link_mapping_datasets_missing")
    return link, review, snapshot, digest


def _current_link_relations(db: Session, link: OntologyLinkMapping) -> list[Relation]:
    return db.query(Relation).filter(
        Relation.ontology_id == link.ontology_id,
        Relation.source_link_mapping_id == link.id,
    ).order_by(Relation.id.asc()).all()


def _fail_if_ambiguous_legacy_relations(
    db: Session,
    *,
    link: OntologyLinkMapping,
    definition: dict,
    source_mapping: OntologyMapping,
    target_mapping: OntologyMapping,
    current_instance_ids: set[str],
) -> None:
    legacy = db.query(Relation).filter(
        Relation.ontology_id == link.ontology_id,
        Relation.type == definition["relation_type"],
        Relation.source_link_mapping_id.is_(None),
    ).all()
    for relation in legacy:
        props = relation.properties if isinstance(relation.properties, dict) else {}
        if (props.get("mapping_type") != "link_mapping"
                or props.get("src_key") != definition["src_key"]
                or props.get("tgt_key") != definition["tgt_key"]):
            continue
        source = str(relation.source_entity)
        target = str(relation.target_entity)
        if source in current_instance_ids or target in current_instance_ids:
            raise LinkApplyError("legacy_link_relations_require_reconciliation")
        endpoint_entities = db.query(Entity.id, Entity.type).filter(
            Entity.id.in_([source, target]),
        ).all()
        types = {str(item[0]): str(item[1] or "") for item in endpoint_entities}
        if (types.get(source) == source_mapping.entity_class
                and types.get(target) == target_mapping.entity_class):
            raise LinkApplyError("legacy_link_relations_require_reconciliation")


def _legacy_link_relation_reconciliation_report(
    db: Session,
    *,
    link: OntologyLinkMapping,
    definition: dict,
    source_mapping: OntologyMapping,
    target_mapping: OntologyMapping,
    source_instance_ids: set[str],
    target_instance_ids: set[str],
    desired_relations: dict[str, tuple[str, str]],
) -> dict:
    """Build a bounded, read-only report; never claim, delete, or rewrite legacy facts."""
    rows = db.query(Relation).filter(
        Relation.ontology_id == link.ontology_id,
        Relation.type == definition["relation_type"],
        Relation.source_link_mapping_id.is_(None),
    ).order_by(Relation.id.asc()).limit(LEGACY_RELATION_RECONCILIATION_SCAN_LIMIT + 1).all()
    scan_truncated = len(rows) > LEGACY_RELATION_RECONCILIATION_SCAN_LIMIT
    rows = rows[:LEGACY_RELATION_RECONCILIATION_SCAN_LIMIT]
    endpoint_ids = {
        str(value)
        for relation in rows
        for value in (relation.source_entity, relation.target_entity)
    }
    endpoint_types = {
        str(item[0]): str(item[1] or "")
        for item in db.query(Entity.id, Entity.type).filter(
            Entity.id.in_(sorted(endpoint_ids) or ["__none__"]),
        ).all()
    }
    desired_pairs = set(desired_relations.values())
    source_class = str(source_mapping.entity_class or "")
    target_class = str(target_mapping.entity_class or "")
    candidates = []
    fingerprint_rows = []
    counts: dict[str, int] = defaultdict(int)

    for relation in rows:
        source_id = str(relation.source_entity)
        target_id = str(relation.target_entity)
        properties = relation.properties if isinstance(relation.properties, dict) else {}
        signature_match = (
            properties.get("mapping_type") == "link_mapping"
            and str(properties.get("src_key") or "") == str(definition["src_key"])
            and str(properties.get("tgt_key") or "") == str(definition["tgt_key"])
        )
        id_convention_match = str(relation.id) == str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{link.ontology_id}:{source_id}:{definition['relation_type']}:{target_id}:link_mapping",
        ))
        exact_desired_pair = (source_id, target_id) in desired_pairs
        source_current = source_id in source_instance_ids
        target_current = target_id in target_instance_ids
        source_type = endpoint_types.get(source_id, "")
        target_type = endpoint_types.get(target_id, "")
        endpoint_types_match = source_type == source_class and target_type == target_class

        fingerprint_rows.append({
            "id": str(relation.id),
            "type": str(relation.type),
            "source_entity": source_id,
            "target_entity": target_id,
            "properties": properties,
            "confidence": relation.confidence,
            "created_at": relation.created_at,
        })
        if not (signature_match or id_convention_match or exact_desired_pair
                or source_current or target_current or endpoint_types_match):
            continue

        if exact_desired_pair and signature_match and endpoint_types_match:
            classification = "exact_pair_legacy_mapping"
        elif signature_match and endpoint_types_match:
            classification = "mapping_signature_and_types"
        elif signature_match or id_convention_match:
            classification = "mapping_signature_without_endpoint_confirmation"
        elif source_current or target_current:
            classification = "current_dataset_endpoint_overlap"
        elif endpoint_types_match:
            classification = "same_entity_types_unverified"
        else:
            classification = "incomplete_endpoint_evidence"
        counts[classification] += 1
        candidates.append({
            "relation_id": str(relation.id),
            "relation_type": str(relation.type),
            "source_entity_id": source_id,
            "target_entity_id": target_id,
            "source_entity_type": source_type or None,
            "target_entity_type": target_type or None,
            "classification": classification,
            "evidence": {
                "legacy_id_convention_match": id_convention_match,
                "mapping_signature_match": signature_match,
                "exact_desired_pair_match": exact_desired_pair,
                "source_in_current_dataset_version": source_current,
                "target_in_current_dataset_version": target_current,
                "endpoint_types_match_mapping": endpoint_types_match,
            },
        })

    candidates.sort(key=lambda item: (item["classification"], item["relation_id"]))
    return {
        "mode": "read_only",
        "action_taken": False,
        "scan_limit": LEGACY_RELATION_RECONCILIATION_SCAN_LIMIT,
        "scanned_relation_count": len(rows),
        "scan_truncated": scan_truncated,
        "candidate_count": len(candidates),
        "candidate_list_limit": LEGACY_RELATION_RECONCILIATION_RETURN_LIMIT,
        "candidate_list_truncated": len(candidates) > LEGACY_RELATION_RECONCILIATION_RETURN_LIMIT,
        "candidate_counts_by_classification": dict(sorted(counts.items())),
        "candidate_relations": candidates[:LEGACY_RELATION_RECONCILIATION_RETURN_LIMIT],
        "evidence_digest": _canonical_digest({
            "schema_version": 1,
            "link_mapping_id": str(link.id),
            "source_mapping_id": str(source_mapping.id),
            "target_mapping_id": str(target_mapping.id),
            "source_instance_ids_digest": _canonical_digest({"ids": sorted(source_instance_ids)}),
            "target_instance_ids_digest": _canonical_digest({"ids": sorted(target_instance_ids)}),
            "desired_pairs_digest": _canonical_digest({
                "pairs": sorted([list(pair) for pair in desired_pairs]),
            }),
            "relation_type": str(definition["relation_type"]),
            "src_key": str(definition["src_key"]),
            "tgt_key": str(definition["tgt_key"]),
            "scan_limit": LEGACY_RELATION_RECONCILIATION_SCAN_LIMIT,
            "scan_truncated": scan_truncated,
            "relations": fingerprint_rows,
        }),
        "resolution_required": bool(candidates) or scan_truncated,
        "note": "旧关系仅被扫描和分类；本预览不会变更 PostgreSQL Relation 或图数据库。",
    }


def build_link_mapping_preview(
    db: Session,
    *,
    link_mapping_id: str,
    ontology_id: str,
    link_review_id: str,
    fixed_pins: dict | None = None,
    include_legacy_relation_report: bool = False,
) -> dict:
    """Build a non-writing preview and its opaque, input-bound digest."""
    link, link_review, definition, link_digest = _link_review_and_definition(
        db,
        link_mapping_id=link_mapping_id,
        ontology_id=ontology_id,
        link_review_id=link_review_id,
    )
    if fixed_pins is None:
        src_review = _latest_approved_data_review(db, str(link.src_dataset_id))
        tgt_review = _latest_approved_data_review(db, str(link.tgt_dataset_id))
        src_mapping, src_mapping_run, src_mapping_review = _entity_mapping_apply_pin(
            db, ontology_id=ontology_id, dataset_id=str(link.src_dataset_id), data_review=src_review,
        )
        tgt_mapping, tgt_mapping_run, tgt_mapping_review = _entity_mapping_apply_pin(
            db, ontology_id=ontology_id, dataset_id=str(link.tgt_dataset_id), data_review=tgt_review,
        )
    else:
        src_review = db.query(CuratedReview).filter(
            CuratedReview.id == fixed_pins.get("src_review_id"),
            CuratedReview.curated_dataset_id == link.src_dataset_id,
        ).first()
        tgt_review = db.query(CuratedReview).filter(
            CuratedReview.id == fixed_pins.get("tgt_review_id"),
            CuratedReview.curated_dataset_id == link.tgt_dataset_id,
        ).first()
        if (src_review is None or tgt_review is None
                or not _review_is_independently_approved(src_review)
                or not _review_is_independently_approved(tgt_review)):
            raise LinkApplyError("approved_dataset_review_required")
        src_mapping, src_mapping_run, src_mapping_review = _entity_mapping_apply_pin(
            db,
            ontology_id=ontology_id,
            dataset_id=str(link.src_dataset_id),
            data_review=src_review,
            requested_run_id=str(fixed_pins.get("src_mapping_apply_run_id") or ""),
        )
        tgt_mapping, tgt_mapping_run, tgt_mapping_review = _entity_mapping_apply_pin(
            db,
            ontology_id=ontology_id,
            dataset_id=str(link.tgt_dataset_id),
            data_review=tgt_review,
            requested_run_id=str(fixed_pins.get("tgt_mapping_apply_run_id") or ""),
        )
        if (str(src_mapping.id) != str(fixed_pins.get("src_mapping_id"))
                or str(src_mapping_review.id) != str(fixed_pins.get("src_mapping_review_id"))
                or str(tgt_mapping.id) != str(fixed_pins.get("tgt_mapping_id"))
                or str(tgt_mapping_review.id) != str(fixed_pins.get("tgt_mapping_review_id"))):
            raise LinkApplyError("entity_mapping_changed")

    if fixed_pins is not None:
        # A queued Apply may not silently fall back to a newer approved input.
        if (_latest_approved_data_review(db, str(link.src_dataset_id)).id != src_review.id
                or _latest_approved_data_review(db, str(link.tgt_dataset_id)).id != tgt_review.id):
            raise LinkApplyError("link_apply_snapshot_is_stale")

    source_rows, source_manifest, source_rows_digest, source_ids, source_pk = _effective_rows(
        db,
        dataset_id=str(link.src_dataset_id),
        data_review=src_review,
        mapping=src_mapping,
        mapping_run=src_mapping_run,
    )
    target_rows, target_manifest, target_rows_digest, target_ids, target_pk = _effective_rows(
        db,
        dataset_id=str(link.tgt_dataset_id),
        data_review=tgt_review,
        mapping=tgt_mapping,
        mapping_run=tgt_mapping_run,
    )

    target_by_key: dict[str, list[str]] = defaultdict(list)
    for row, entity_id in zip(target_rows, target_ids):
        key = _join_value(row, definition["tgt_key"])
        if key is not None:
            target_by_key[key].append(entity_id)
    matched_source_ids: set[str] = set()
    matched_target_ids: set[str] = set()
    desired: dict[str, tuple[str, str]] = {}
    for row, source_id in zip(source_rows, source_ids):
        key = _join_value(row, definition["src_key"])
        matches = target_by_key.get(key, []) if key is not None else []
        if not matches:
            continue
        matched_source_ids.add(source_id)
        for target_id in matches:
            matched_target_ids.add(target_id)
            relation_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"project4-link:{link.id}:{source_id}:{definition['relation_type']}:{target_id}",
            ))
            desired[relation_id] = (source_id, target_id)

    all_instance_ids = set(source_ids) | set(target_ids)
    legacy_reconciliation = None
    if include_legacy_relation_report:
        legacy_reconciliation = _legacy_link_relation_reconciliation_report(
            db,
            link=link,
            definition=definition,
            source_mapping=src_mapping,
            target_mapping=tgt_mapping,
            source_instance_ids=set(source_ids),
            target_instance_ids=set(target_ids),
            desired_relations=desired,
        )
    else:
        _fail_if_ambiguous_legacy_relations(
            db,
            link=link,
            definition=definition,
            source_mapping=src_mapping,
            target_mapping=tgt_mapping,
            current_instance_ids=all_instance_ids,
        )
    current_relations = _current_link_relations(db, link)
    current_ids = {str(item.id) for item in current_relations}
    desired_ids = set(desired)
    added_ids = desired_ids - current_ids
    removed_ids = current_ids - desired_ids
    unchanged_ids = desired_ids & current_ids
    source_degree: dict[str, int] = defaultdict(int)
    target_degree: dict[str, int] = defaultdict(int)
    for source_id, target_id in desired.values():
        source_degree[source_id] += 1
        target_degree[target_id] += 1
    source_unique = all(value <= 1 for value in source_degree.values())
    target_unique = all(value <= 1 for value in target_degree.values())
    cardinality = (
        "one_to_one" if source_unique and target_unique else
        "one_to_many" if source_unique else
        "many_to_one" if target_unique else
        "many_to_many"
    ) if desired else "unknown"

    source_contract = {
        "contract_version": 1,
        "link_mapping_id": str(link.id),
        "ontology_id": str(ontology_id),
        "link_review_id": str(link_review.id),
        "link_definition": definition,
        "link_definition_digest": link_digest,
        "source": source_manifest,
        "source_mapping_id": str(src_mapping.id),
        "source_mapping_review_id": str(src_mapping_review.id),
        "source_mapping_apply_run_id": str(src_mapping_run.id),
        "source_mapping_digest": str(src_mapping_run.mapping_digest),
        "source_primary_key_column": source_pk,
        "source_effective_rows_digest": source_rows_digest,
        "target": target_manifest,
        "target_mapping_id": str(tgt_mapping.id),
        "target_mapping_review_id": str(tgt_mapping_review.id),
        "target_mapping_apply_run_id": str(tgt_mapping_run.id),
        "target_mapping_digest": str(tgt_mapping_run.mapping_digest),
        "target_primary_key_column": target_pk,
        "target_effective_rows_digest": target_rows_digest,
        "desired_relation_ids_digest": _canonical_digest({"ids": sorted(desired_ids)}),
        "desired_relation_count": len(desired),
        **({
            "legacy_relation_evidence_digest": legacy_reconciliation["evidence_digest"],
            "legacy_relation_scan_truncated": legacy_reconciliation["scan_truncated"],
            "legacy_relation_candidate_count": legacy_reconciliation["candidate_count"],
        } if legacy_reconciliation is not None else {}),
        "matched_source_row_count": len(matched_source_ids),
        "matched_target_row_count": len(matched_target_ids),
        "unmatched_source_row_count": len(source_rows) - len(matched_source_ids),
        "unmatched_target_row_count": len(target_rows) - len(matched_target_ids),
        "cardinality": cardinality,
    }
    current_contract = {
        "current_relation_ids_digest": _canonical_digest({"ids": sorted(current_ids)}),
        "current_relation_count": len(current_ids),
        "added_relation_count": len(added_ids),
        "removed_relation_count": len(removed_ids),
        "unchanged_relation_count": len(unchanged_ids),
    }
    snapshot = {**source_contract, **current_contract}
    preview_digest = _canonical_digest(snapshot)
    summary = {
        "preview_digest": preview_digest,
        "link_mapping_id": str(link.id),
        "link_review_id": str(link_review.id),
        "relation_type": definition["relation_type"],
        "source_dataset_id": str(link.src_dataset_id),
        "source_review_id": str(src_review.id),
        "source_dataset_version_id": str(src_review.dataset_version_id),
        "target_dataset_version_id": str(tgt_review.dataset_version_id),
        "target_dataset_id": str(link.tgt_dataset_id),
        "target_review_id": str(tgt_review.id),
        "source_entity_mapping_id": str(src_mapping.id),
        "target_entity_mapping_id": str(tgt_mapping.id),
        "source_row_count": len(source_rows),
        "target_row_count": len(target_rows),
        "matched_relation_count": len(desired),
        "added_relation_count": len(added_ids),
        "removed_relation_count": len(removed_ids),
        "unchanged_relation_count": len(unchanged_ids),
        "unmatched_source_row_count": len(source_rows) - len(matched_source_ids),
        "unmatched_target_row_count": len(target_rows) - len(matched_target_ids),
        "cardinality": cardinality,
    }
    if legacy_reconciliation is not None:
        summary["legacy_reconciliation"] = legacy_reconciliation
    return {
        "preview_digest": preview_digest,
        "snapshot": snapshot,
        "summary": summary,
        "desired_relations": desired,
        "current_relations": current_relations,
        "definition": definition,
        "legacy_reconciliation": legacy_reconciliation,
    }


def _relation_snapshot(relation: Relation | None, *, version_src: str | None = None, version_tgt: str | None = None) -> dict | None:
    if relation is None:
        return None
    return {
        "relation_id": str(relation.id),
        "relation_type": str(relation.type),
        "source_entity_id": str(relation.source_entity),
        "target_entity_id": str(relation.target_entity),
        "source_dataset_version_id": str(version_src or relation.source_dataset_version_id or ""),
        "target_dataset_version_id": str(version_tgt or relation.target_dataset_version_id or ""),
    }


def _relation_change_digest(payload: dict) -> str:
    return _canonical_digest(payload)


def _append_relation_change(
    db: Session,
    *,
    run: MappingLinkApplyRun,
    sequence_no: int,
    previous_digest: str | None,
    relation_id: str,
    operation: str,
    relation_type: str,
    source_entity_id: str,
    target_entity_id: str,
    before_snapshot: dict | None,
    after_snapshot: dict | None,
) -> tuple[MappingLinkRelationChange, MappingLinkRelationProjectionState, str]:
    occurred_at = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "run_id": str(run.id),
        "sequence_no": int(sequence_no),
        "link_mapping_id": str(run.link_mapping_id),
        "ontology_id": str(run.ontology_id),
        "relation_id": str(relation_id),
        "operation": operation,
        "relation_type": relation_type,
        "source_entity_id": str(source_entity_id),
        "target_entity_id": str(target_entity_id),
        "source_dataset_version_id": str(run.src_dataset_version_id),
        "target_dataset_version_id": str(run.tgt_dataset_version_id),
        "before_snapshot": before_snapshot,
        "after_snapshot": after_snapshot,
        "previous_digest": previous_digest,
        "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(),
    }
    digest = _relation_change_digest(payload)
    change = MappingLinkRelationChange(
        run_id=str(run.id),
        link_mapping_id=str(run.link_mapping_id),
        ontology_id=str(run.ontology_id),
        sequence_no=int(sequence_no),
        relation_id=str(relation_id),
        operation=operation,
        relation_type=relation_type,
        source_entity_id=str(source_entity_id),
        target_entity_id=str(target_entity_id),
        source_dataset_version_id=str(run.src_dataset_version_id),
        target_dataset_version_id=str(run.tgt_dataset_version_id),
        before_snapshot_json=before_snapshot,
        after_snapshot_json=after_snapshot,
        previous_digest=previous_digest,
        event_digest=digest,
        occurred_at=occurred_at,
    )
    db.add(change)
    db.flush()
    state = MappingLinkRelationProjectionState(change_id=change.id, status="pending", attempts=0)
    db.add(state)
    db.flush()
    return change, state, digest


def _validate_relation_change_chain(changes: list[MappingLinkRelationChange], run_id: str) -> bool:
    previous = None
    for expected, change in enumerate(changes, 1):
        occurred = change.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        payload = {
            "schema_version": 1,
            "run_id": str(run_id),
            "sequence_no": int(change.sequence_no),
            "link_mapping_id": str(change.link_mapping_id),
            "ontology_id": str(change.ontology_id),
            "relation_id": str(change.relation_id),
            "operation": str(change.operation),
            "relation_type": str(change.relation_type),
            "source_entity_id": str(change.source_entity_id),
            "target_entity_id": str(change.target_entity_id),
            "source_dataset_version_id": str(change.source_dataset_version_id),
            "target_dataset_version_id": str(change.target_dataset_version_id),
            "before_snapshot": change.before_snapshot_json,
            "after_snapshot": change.after_snapshot_json,
            "previous_digest": change.previous_digest,
            "occurred_at": occurred.astimezone(timezone.utc).isoformat(),
        }
        if (change.sequence_no != expected or change.previous_digest != previous
                or _relation_change_digest(payload) != change.event_digest):
            return False
        previous = change.event_digest
    return True


def _persist_relation_facts(db: Session, run: MappingLinkApplyRun, plan: dict) -> list[MappingLinkRelationChange]:
    existing_changes = db.query(MappingLinkRelationChange).filter(
        MappingLinkRelationChange.run_id == run.id,
    ).order_by(MappingLinkRelationChange.sequence_no.asc()).all()
    desired_relations: dict[str, tuple[str, str]] = plan["desired_relations"]
    desired_ids = set(desired_relations)
    existing_current = _current_link_relations(
        db,
        db.query(OntologyLinkMapping).filter(OntologyLinkMapping.id == run.link_mapping_id).first(),
    )
    current_ids = {str(item.id) for item in existing_current}

    if existing_changes:
        if not _validate_relation_change_chain(existing_changes, run.id):
            raise LinkApplyError("relation_change_history_invalid")
        if current_ids != desired_ids:
            raise LinkApplyError("relation_facts_changed_during_apply")
        return existing_changes

    snapshot = run.input_snapshot_json
    if not isinstance(snapshot, dict):
        raise LinkApplyError("link_apply_snapshot_invalid")
    if (_canonical_digest(snapshot) != run.preview_digest
            or snapshot.get("current_relation_ids_digest") != _canonical_digest({"ids": sorted(current_ids)})):
        raise LinkApplyError("link_apply_preview_stale")

    definition = plan["definition"]
    current_by_id = {str(item.id): item for item in existing_current}
    source_version = str(run.src_dataset_version_id)
    target_version = str(run.tgt_dataset_version_id)
    cardinality = str(snapshot.get("cardinality") or "unknown")
    properties_base = {
        "mapping_type": "link_mapping",
        "link_mapping_id": str(run.link_mapping_id),
        "link_review_id": str(run.link_review_id),
        "link_apply_run_id": str(run.id),
        "src_key": definition["src_key"],
        "tgt_key": definition["tgt_key"],
        "src_dataset_id": str(run.src_dataset_id),
        "tgt_dataset_id": str(run.tgt_dataset_id),
        "src_mapping_id": str(run.src_mapping_id),
        "tgt_mapping_id": str(run.tgt_mapping_id),
        "src_mapping_review_id": str(run.src_mapping_review_id),
        "tgt_mapping_review_id": str(run.tgt_mapping_review_id),
        "src_mapping_apply_run_id": str(run.src_mapping_apply_run_id),
        "tgt_mapping_apply_run_id": str(run.tgt_mapping_apply_run_id),
        "source_dataset_version_id": source_version,
        "target_dataset_version_id": target_version,
        "cardinality": cardinality,
    }
    change_specs: list[tuple[str, str, str, str, str, dict | None, dict | None]] = []
    for relation_id, (source_id, target_id) in sorted(desired_relations.items()):
        relation = db.get(Relation, relation_id)
        if relation is not None and relation.source_link_mapping_id not in (None, run.link_mapping_id):
            raise LinkApplyError("relation_id_owned_by_other_mapping")
        before = _relation_snapshot(relation)
        relation_properties = {**properties_base}
        if relation is None:
            relation = Relation(
                id=relation_id,
                ontology_id=run.ontology_id,
                source_entity=source_id,
                target_entity=target_id,
                type=definition["relation_type"],
                properties=relation_properties,
                confidence=0.9,
                source_link_mapping_id=run.link_mapping_id,
                source_link_apply_run_id=run.id,
                source_dataset_version_id=source_version,
                target_dataset_version_id=target_version,
            )
            db.add(relation)
        else:
            if (str(relation.ontology_id) != str(run.ontology_id)
                    or str(relation.source_entity) != source_id
                    or str(relation.target_entity) != target_id
                    or str(relation.type) != str(definition["relation_type"])):
                raise LinkApplyError("relation_fact_identity_conflict")
            relation.properties = relation_properties
            relation.confidence = 0.9
            relation.source_link_mapping_id = run.link_mapping_id
            relation.source_link_apply_run_id = run.id
            relation.source_dataset_version_id = source_version
            relation.target_dataset_version_id = target_version
        after = _relation_snapshot(relation, version_src=source_version, version_tgt=target_version)
        change_specs.append((relation_id, "upsert", str(definition["relation_type"]), source_id, target_id, before, after))

    for relation_id, relation in sorted(current_by_id.items()):
        if relation_id in desired_ids:
            continue
        before = _relation_snapshot(relation)
        db.delete(relation)
        change_specs.append((
            relation_id,
            "delete",
            str(relation.type),
            str(relation.source_entity),
            str(relation.target_entity),
            before,
            None,
        ))

    sequence_no = 0
    previous_digest = None
    changes: list[MappingLinkRelationChange] = []
    for relation_id, operation, rel_type, source_id, target_id, before, after in sorted(
        change_specs, key=lambda item: (item[0], item[1]),
    ):
        sequence_no += 1
        change, _state, previous_digest = _append_relation_change(
            db,
            run=run,
            sequence_no=sequence_no,
            previous_digest=previous_digest,
            relation_id=relation_id,
            operation=operation,
            relation_type=rel_type,
            source_entity_id=source_id,
            target_entity_id=target_id,
            before_snapshot=before,
            after_snapshot=after,
        )
        changes.append(change)
    db.commit()
    return changes


def _project_relation_changes(
    db: Session,
    run: MappingLinkApplyRun,
    *,
    worker_actor_id: str | None = None,
) -> dict:
    from app.services.v2.mapping_link_projection import project_link_relation_changes

    return project_link_relation_changes(
        db, run=run, worker_actor_id=str(worker_actor_id or "unreported-worker"),
    )

def apply_approved_link_mapping(
    db: Session, run_id: str, *, worker_actor_id: str | None = None,
) -> dict:
    """Materialize SQL relation facts, persist projection intent, then write FalkorDB."""
    run = db.query(MappingLinkApplyRun).filter(MappingLinkApplyRun.id == run_id).first()
    if run is None:
        raise LinkApplyError("link_apply_run_missing")
    fixed_pins = {
        "src_review_id": run.src_review_id,
        "tgt_review_id": run.tgt_review_id,
        "src_mapping_id": run.src_mapping_id,
        "tgt_mapping_id": run.tgt_mapping_id,
        "src_mapping_review_id": run.src_mapping_review_id,
        "tgt_mapping_review_id": run.tgt_mapping_review_id,
        "src_mapping_apply_run_id": run.src_mapping_apply_run_id,
        "tgt_mapping_apply_run_id": run.tgt_mapping_apply_run_id,
    }
    plan = build_link_mapping_preview(
        db,
        link_mapping_id=run.link_mapping_id,
        ontology_id=run.ontology_id,
        link_review_id=run.link_review_id,
        fixed_pins=fixed_pins,
    )
    stored = run.input_snapshot_json if isinstance(run.input_snapshot_json, dict) else {}
    source_contract_fields = (
        "contract_version", "link_mapping_id", "ontology_id", "link_review_id", "link_definition",
        "link_definition_digest", "source", "source_mapping_id", "source_mapping_review_id",
        "source_mapping_apply_run_id", "source_mapping_digest", "source_primary_key_column",
        "source_effective_rows_digest", "target", "target_mapping_id", "target_mapping_review_id",
        "target_mapping_apply_run_id", "target_mapping_digest", "target_primary_key_column",
        "target_effective_rows_digest", "desired_relation_ids_digest", "desired_relation_count",
        "matched_source_row_count", "matched_target_row_count", "unmatched_source_row_count",
        "unmatched_target_row_count", "cardinality",
    )
    stable_current = {key: stored.get(key) for key in source_contract_fields}
    stable_recomputed = {key: plan["snapshot"].get(key) for key in source_contract_fields}
    if _canonical_digest(stable_current) != _canonical_digest(stable_recomputed):
        raise LinkApplyError("link_apply_inputs_changed")

    changes = db.query(MappingLinkRelationChange).filter(
        MappingLinkRelationChange.run_id == run.id,
    ).order_by(MappingLinkRelationChange.sequence_no.asc()).all()
    if not changes and plan["preview_digest"] != run.preview_digest:
        raise LinkApplyError("link_apply_preview_stale")
    changes = _persist_relation_facts(db, run, plan)
    projection = _project_relation_changes(db, run, worker_actor_id=worker_actor_id)
    desired_count = len(plan["desired_relations"])
    current_after = _current_link_relations(
        db,
        db.query(OntologyLinkMapping).filter(OntologyLinkMapping.id == run.link_mapping_id).first(),
    )
    if {str(item.id) for item in current_after} != set(plan["desired_relations"]):
        raise LinkApplyError("relation_fact_reconciliation_failed")
    return {
        "link_mapping_id": run.link_mapping_id,
        "link_review_id": run.link_review_id,
        "source_dataset_version_id": run.src_dataset_version_id,
        "target_dataset_version_id": run.tgt_dataset_version_id,
        "relation_count": desired_count,
        "relation_changes": len(changes),
        "projection": projection,
        "preview_digest": run.preview_digest,
    }
