"""Review event and worker-gate helpers for Dataset Construction."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.v2.construction import ConstructionRun
from app.models.v2.construction_review import ConstructionReview, ConstructionReviewEvent
from app.models.ontology import OntologyProject
from app.models.ontology_revision import OntologyRevision
from app.services.v2.construction_review_diff import (
    candidate_schema_diff,
    construction_impact_preview,
    live_schema_snapshot,
    schema_snapshot_digest,
)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def append_review_event(
    db: Session,
    review: ConstructionReview,
    *,
    actor_id: str | None,
    event_type: str,
    detail: dict[str, Any] | None = None,
) -> ConstructionReviewEvent:
    last_sequence = db.query(func.max(ConstructionReviewEvent.sequence_no)).filter(
        ConstructionReviewEvent.review_id == review.id,
    ).scalar()
    event = ConstructionReviewEvent(
        review_id=review.id,
        sequence_no=int(last_sequence or 0) + 1,
        actor_id=str(actor_id) if actor_id is not None else None,
        event_type=event_type,
        status=review.status,
        detail_json=detail or {},
    )
    db.add(event)
    db.flush()
    return event


def approved_review_for_run(db: Session, run: ConstructionRun) -> ConstructionReview | None:
    config = run.config if isinstance(run.config, dict) else {}
    review_id = config.get("construction_review_id")
    expected_digest = config.get("construction_review_digest")
    if not review_id or not expected_digest:
        return None
    review = db.query(ConstructionReview).filter(
        ConstructionReview.id == str(review_id),
        ConstructionReview.status == "approved",
        ConstructionReview.construction_run_id == run.id,
    ).first()
    if not review or review.candidate_digest != str(expected_digest):
        return None
    candidate = review.candidate_json if isinstance(review.candidate_json, dict) else {}
    manifest = candidate.get("manifest") if isinstance(candidate.get("manifest"), dict) else {}
    mapping = config.get("mapping") if isinstance(config.get("mapping"), dict) else {}
    ontology_mapping = mapping.get("ontology_mapping") if isinstance(mapping.get("ontology_mapping"), dict) else {}
    if (
        _stable_digest(manifest) != review.candidate_digest
        or run.dataset_id != manifest.get("dataset_id")
        or run.ontology_id != review.ontology_id
        or run.mode != manifest.get("data_class")
        or config.get("dataset_version_id") != manifest.get("dataset_version_id")
        or config.get("dataset_checksum") != manifest.get("dataset_checksum")
        or config.get("dataset_schema_digest") != manifest.get("dataset_schema_digest")
        or config.get("mapping_task_id") != manifest.get("mapping_task_id")
        or config.get("mapping_task_input_digest") != manifest.get("mapping_task_input_digest")
        or config.get("privacy_level") != manifest.get("privacy_level")
        or config.get("mode") != manifest.get("requested_build_mode")
        or config.get("model_id") != manifest.get("requested_model_id")
        or _stable_digest(config.get("selection") or {}) != manifest.get("selection_digest")
        or _stable_digest(config.get("processing") or {}) != manifest.get("processing_digest")
        or _stable_digest(ontology_mapping) != manifest.get("mapping_digest")
        or config.get("construction_submitted_by") != review.submitted_by
    ):
        return None
    submitted_by = str(review.submitted_by or "")
    reviewed_by = str(review.reviewed_by or "")
    configured_reviewer = str(config.get("construction_reviewed_by") or "")
    if not submitted_by or not reviewed_by or submitted_by == reviewed_by or reviewed_by != configured_reviewer:
        return None
    project = db.query(OntologyProject).filter(
        OntologyProject.id == run.ontology_id,
    ).with_for_update().first()
    if not project:
        return None
    current_revision_id = project.current_revision_id
    if current_revision_id:
        current = db.query(OntologyRevision).filter(
            OntologyRevision.id == current_revision_id,
            OntologyRevision.ontology_id == project.id,
            OntologyRevision.is_current.is_(True),
        ).first()
        if not current:
            return None
    else:
        current = db.query(OntologyRevision).filter(
            OntologyRevision.ontology_id == project.id,
            OntologyRevision.is_current.is_(True),
        ).order_by(OntologyRevision.revision_no.desc()).first()
        current_revision_id = current.id if current else None
    if current_revision_id != review.base_revision_id:
        return None
    if schema_snapshot_digest(live_schema_snapshot(db, project.id)) != manifest.get("base_schema_digest"):
        return None
    impact_preview = manifest.get("impact_preview") if isinstance(manifest.get("impact_preview"), dict) else {}
    if not impact_preview.get("digest") or not isinstance(ontology_mapping, dict):
        return None
    base_snapshot = live_schema_snapshot(db, project.id)
    projected_snapshot, current_diff = candidate_schema_diff(
        base_snapshot,
        ontology_mapping,
        ontology_id=project.id,
        data_class=run.mode,
    )
    current_impact = construction_impact_preview(
        db, project.id, base_snapshot, projected_snapshot, current_diff,
    )
    if current_impact.get("digest") != impact_preview.get("digest"):
        return None
    if review.base_revision_id:
        if not current or current.snapshot_hash != manifest.get("base_revision_snapshot_hash"):
            return None
    elif manifest.get("base_revision_snapshot_hash") is not None:
        return None
    return review
