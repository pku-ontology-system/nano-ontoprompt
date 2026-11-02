"""Independent approval and immutable versioning for Ontology Mapping definitions."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.v2.mapping import OntologyMapping
from app.models.v2.mapping_definition_review import (
    MappingDefinitionReview,
    MappingDefinitionReviewEvent,
)
from app.models.v2.mapping_apply_run import MappingApplyRun
from app.services.v2.mapping_apply_runs import mapping_definition_snapshot


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _append_event(
    db: Session,
    review: MappingDefinitionReview,
    *,
    actor_kind: str,
    actor_id: str | None,
    event_type: str,
    detail: dict | None = None,
) -> MappingDefinitionReviewEvent:
    sequence = db.query(func.max(MappingDefinitionReviewEvent.sequence_no)).filter(
        MappingDefinitionReviewEvent.review_id == review.id,
    ).scalar()
    event = MappingDefinitionReviewEvent(
        review_id=review.id,
        sequence_no=int(sequence or 0) + 1,
        actor_kind=actor_kind,
        actor_id=str(actor_id) if actor_id is not None else None,
        event_type=event_type,
        status=review.status,
        detail_json=detail or {},
    )
    db.add(event)
    db.flush()
    return event


def latest_mapping_review(db: Session, mapping_id: str) -> MappingDefinitionReview | None:
    return db.query(MappingDefinitionReview).filter(
        MappingDefinitionReview.mapping_id == mapping_id,
    ).order_by(MappingDefinitionReview.revision_no.desc()).first()


def approved_mapping_review_for_run(
    db: Session,
    mapping: OntologyMapping,
    review_id: str | None,
    *,
    expected_digest: str | None = None,
) -> MappingDefinitionReview:
    if not review_id:
        latest = latest_mapping_review(db, mapping.id)
        review_id = latest.id if latest is not None else None
    if not review_id:
        raise ValueError("Mapping Apply requires an approved Mapping definition review")
    review = db.query(MappingDefinitionReview).filter(
        MappingDefinitionReview.id == review_id,
        MappingDefinitionReview.mapping_id == mapping.id,
        MappingDefinitionReview.ontology_id == mapping.ontology_id,
        MappingDefinitionReview.status == "approved",
    ).first()
    if review is None:
        raise ValueError("Mapping Apply requires an approved review for this Mapping definition")
    latest = latest_mapping_review(db, mapping.id)
    if latest is None or latest.id != review.id:
        raise ValueError("Mapping definition review is no longer the current published revision")
    if (not review.submitted_by or not review.decided_by
            or str(review.submitted_by) == str(review.decided_by)):
        raise ValueError("Mapping definition approval must be independent of its requester")
    if review.reviewer_id and str(review.reviewer_id) != str(review.decided_by):
        raise ValueError("Mapping definition was not approved by its assigned reviewer")
    current_candidate, current_digest = mapping_definition_snapshot(mapping)
    if (current_digest != review.candidate_digest
            or current_candidate != review.candidate_json
            or (expected_digest and expected_digest != review.candidate_digest)):
        raise ValueError("Mapping definition changed after approval")
    if mapping.status not in {"active", "applied"}:
        raise ValueError("Mapping definition is not published")
    return review


def serialize_mapping_review(
    review: MappingDefinitionReview,
    *,
    actor_id: str | None = None,
    can_review: bool = False,
    include_events: bool = False,
    db: Session | None = None,
) -> dict:
    result = {
        "review_id": review.id,
        "mapping_id": review.mapping_id,
        "revision_no": review.revision_no,
        "candidate_digest": review.candidate_digest,
        "status": review.status,
        "submitted_by": review.submitted_by,
        "reviewer_id": review.reviewer_id,
        "decided_by": review.decided_by,
        "decision": review.decision,
        "decision_note": review.decision_note,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "decided_at": review.decided_at.isoformat() if review.decided_at else None,
        "can_review": bool(
            can_review
            and review.status == "pending_review"
            and actor_id
            and str(actor_id) != str(review.submitted_by or "")
            and (not review.reviewer_id or str(actor_id) == str(review.reviewer_id))
        ),
    }
    if include_events and db is not None:
        events = db.query(MappingDefinitionReviewEvent).filter(
            MappingDefinitionReviewEvent.review_id == review.id,
        ).order_by(MappingDefinitionReviewEvent.sequence_no.desc()).limit(50).all()
        result["events"] = [
            {
                "sequence_no": item.sequence_no,
                "actor_kind": item.actor_kind,
                "actor_id": item.actor_id,
                "event_type": item.event_type,
                "status": item.status,
                "detail": item.detail_json or {},
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in reversed(events)
        ]
    return result


class MappingDefinitionReviewService:
    def __init__(self, db: Session):
        self._db = db

    def submit(
        self,
        mapping_id: str,
        *,
        submitted_by: str,
        reviewer_id: str | None = None,
        candidate: dict | None = None,
    ) -> tuple[MappingDefinitionReview, bool]:
        query = self._db.query(OntologyMapping).filter(OntologyMapping.id == mapping_id)
        if self._db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update()
        mapping = query.first()
        if mapping is None or mapping.status == "disabled":
            raise ValueError("Mapping is missing or disabled")
        if reviewer_id and str(reviewer_id) == str(submitted_by):
            raise PermissionError("Mapping review must be assigned to a different reviewer")

        latest = latest_mapping_review(self._db, mapping.id)
        if candidate is not None:
            if mapping.status != "draft":
                raise ValueError("Only an unpublished draft Mapping can be revised")
            if latest and latest.status == "pending_review":
                raise ValueError("A Mapping candidate is already awaiting review")
            if latest and latest.status not in {"rejected", "changes_requested"}:
                raise ValueError("Only a draft or a Mapping candidate returned for changes can be revised")
            existing_run = self._db.query(MappingApplyRun.id).filter(
                MappingApplyRun.mapping_id == mapping.id,
            ).first()
            if existing_run:
                raise ValueError("A Mapping with an Apply run cannot be revised; create a new Mapping")

            entity_class = str(candidate.get("entity_class") or "").strip()
            field_mapping = candidate.get("field_mapping")
            if not entity_class or len(entity_class) > 200:
                raise ValueError("Mapping entity class must contain 1 to 200 characters")
            if not isinstance(field_mapping, dict):
                raise ValueError("Mapping field mapping must be an object")
            field_mapping = dict(field_mapping)
            field_mapping.pop("__auto_apply_on_review__", None)
            confidence = mapping.confidence
            if "confidence" in candidate:
                confidence = candidate["confidence"]
                if confidence is not None:
                    try:
                        confidence = float(confidence)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Mapping confidence must be a number between 0 and 1") from exc
                    if not 0 <= confidence <= 1:
                        raise ValueError("Mapping confidence must be between 0 and 1")
            mapping.entity_class = entity_class
            mapping.field_mapping = field_mapping
            mapping.confidence = confidence
            self._db.flush()

        candidate, digest = mapping_definition_snapshot(mapping)
        latest = latest_mapping_review(self._db, mapping.id)
        if latest and latest.candidate_digest == digest and latest.status in {"pending_review", "approved"}:
            return latest, False
        if latest and latest.status == "pending_review":
            raise ValueError("An earlier Mapping candidate is still awaiting review")

        revision_no = int(self._db.query(func.max(MappingDefinitionReview.revision_no)).filter(
            MappingDefinitionReview.mapping_id == mapping.id,
        ).scalar() or 0) + 1
        review = MappingDefinitionReview(
            mapping_id=mapping.id,
            ontology_id=mapping.ontology_id,
            revision_no=revision_no,
            candidate_json=candidate,
            candidate_digest=digest,
            status="pending_review",
            submitted_by=str(submitted_by),
            reviewer_id=str(reviewer_id) if reviewer_id else None,
        )
        self._db.add(review)
        try:
            self._db.flush()
            _append_event(
                self._db,
                review,
                actor_kind="user",
                actor_id=submitted_by,
                event_type="submitted",
                detail={
                    "revision_no": revision_no,
                    "candidate_digest": digest,
                    "supersedes_review_id": latest.id if latest is not None else None,
                },
            )
            self._db.commit()
            self._db.refresh(review)
            return review, True
        except IntegrityError:
            self._db.rollback()
            latest = latest_mapping_review(self._db, mapping_id)
            if latest and latest.candidate_digest == digest and latest.status in {"pending_review", "approved"}:
                return latest, False
            raise

    def decide(
        self,
        mapping_id: str,
        review_id: str,
        *,
        decided_by: str,
        decision: str,
        notes: str = "",
    ) -> MappingDefinitionReview:
        if decision not in {"approved", "rejected", "changes_requested"}:
            raise ValueError("Decision must be approved, rejected, or changes_requested")
        review_query = self._db.query(MappingDefinitionReview).filter(
            MappingDefinitionReview.id == review_id,
            MappingDefinitionReview.mapping_id == mapping_id,
        )
        if self._db.get_bind().dialect.name == "postgresql":
            review_query = review_query.with_for_update()
        review = review_query.first()
        if review is None:
            raise LookupError("Mapping review not found")
        if review.status != "pending_review":
            raise ValueError("Only pending Mapping candidates can be decided")
        if not review.submitted_by or str(review.submitted_by) == str(decided_by):
            raise PermissionError("Mapping reviewer must be different from the requester")
        if review.reviewer_id and str(review.reviewer_id) != str(decided_by):
            raise PermissionError("Only the assigned reviewer can decide this Mapping candidate")

        mapping_query = self._db.query(OntologyMapping).filter(OntologyMapping.id == mapping_id)
        if self._db.get_bind().dialect.name == "postgresql":
            mapping_query = mapping_query.with_for_update()
        mapping = mapping_query.first()
        if mapping is None or mapping.status == "disabled":
            raise ValueError("Mapping is missing or disabled")
        _, current_digest = mapping_definition_snapshot(mapping)
        if current_digest != review.candidate_digest:
            raise ValueError("Mapping changed after review submission; submit a fresh candidate")
        latest = latest_mapping_review(self._db, mapping.id)
        if latest is None or latest.id != review.id:
            raise ValueError("Only the latest Mapping candidate can be decided")

        review.status = decision
        review.decision = decision
        review.decided_by = str(decided_by)
        review.decision_note = notes.strip()[:4000] or None
        review.decided_at = _now()
        if decision == "approved":
            mapping.status = "active"
        _append_event(
            self._db,
            review,
            actor_kind="user",
            actor_id=decided_by,
            event_type=decision,
            detail={"candidate_digest": review.candidate_digest},
        )
        self._db.commit()
        self._db.refresh(review)
        return review

    def list_reviews(self, mapping_id: str) -> list[MappingDefinitionReview]:
        return self._db.query(MappingDefinitionReview).filter(
            MappingDefinitionReview.mapping_id == mapping_id,
        ).order_by(MappingDefinitionReview.revision_no.desc()).all()
