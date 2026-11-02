"""Independent review lifecycle for source-to-target link mappings."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.v2.mapping import OntologyLinkMapping
from app.models.v2.mapping_link_definition_review import (
    MappingLinkDefinitionReview,
    MappingLinkDefinitionReviewEvent,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def link_mapping_snapshot(link: OntologyLinkMapping, candidate: dict | None = None) -> tuple[dict, str]:
    candidate = candidate or {}
    snapshot = {
        "link_mapping_id": str(link.id),
        "ontology_id": str(link.ontology_id),
        "src_dataset_id": str(link.src_dataset_id) if link.src_dataset_id else None,
        "tgt_dataset_id": str(link.tgt_dataset_id) if link.tgt_dataset_id else None,
        "relation_type": str(candidate.get("relation_type", link.relation_type)).strip(),
        "src_key": str(candidate.get("src_key", link.src_key)).strip(),
        "tgt_key": str(candidate.get("tgt_key", link.tgt_key)).strip(),
    }
    if (not snapshot["relation_type"] or len(snapshot["relation_type"]) > 100
            or not snapshot["src_key"] or len(snapshot["src_key"]) > 100
            or not snapshot["tgt_key"] or len(snapshot["tgt_key"]) > 100):
        raise ValueError("关系类型和两端匹配字段均须为 1–100 个字符")
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return snapshot, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _append_event(
    db: Session,
    review: MappingLinkDefinitionReview,
    *,
    actor_id: str,
    event_type: str,
) -> MappingLinkDefinitionReviewEvent:
    sequence = db.query(func.max(MappingLinkDefinitionReviewEvent.sequence_no)).filter(
        MappingLinkDefinitionReviewEvent.review_id == review.id,
    ).scalar()
    row = MappingLinkDefinitionReviewEvent(
        review_id=review.id,
        sequence_no=int(sequence or 0) + 1,
        actor_kind="user",
        actor_id=str(actor_id),
        event_type=event_type,
        status=review.status,
        detail_json={"revision_no": review.revision_no, "candidate_digest": review.candidate_digest},
    )
    db.add(row)
    db.flush()
    return row


def latest_link_mapping_review(db: Session, link_mapping_id: str) -> MappingLinkDefinitionReview | None:
    return db.query(MappingLinkDefinitionReview).filter(
        MappingLinkDefinitionReview.link_mapping_id == link_mapping_id,
    ).order_by(MappingLinkDefinitionReview.revision_no.desc()).first()


def serialize_link_mapping_review(
    review: MappingLinkDefinitionReview,
    *,
    actor_id: str | None = None,
    can_review: bool = False,
    include_events: bool = False,
    db: Session | None = None,
) -> dict:
    result = {
        "review_id": review.id,
        "link_mapping_id": review.link_mapping_id,
        "ontology_id": review.ontology_id,
        "revision_no": review.revision_no,
        "candidate": review.candidate_json,
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
            can_review and review.status == "pending_review" and actor_id
            and str(actor_id) != str(review.submitted_by or "")
            and (not review.reviewer_id or str(actor_id) == str(review.reviewer_id))
        ),
    }
    if include_events and db is not None:
        events = db.query(MappingLinkDefinitionReviewEvent).filter(
            MappingLinkDefinitionReviewEvent.review_id == review.id,
        ).order_by(MappingLinkDefinitionReviewEvent.sequence_no.desc()).limit(50).all()
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


class MappingLinkDefinitionReviewService:
    def __init__(self, db: Session):
        self._db = db

    def _mapping_query(self, link_mapping_id: str):
        query = self._db.query(OntologyLinkMapping).filter(OntologyLinkMapping.id == link_mapping_id)
        if self._db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update()
        return query

    def list_reviews(self, link_mapping_id: str) -> list[MappingLinkDefinitionReview]:
        return self._db.query(MappingLinkDefinitionReview).filter(
            MappingLinkDefinitionReview.link_mapping_id == link_mapping_id,
        ).order_by(MappingLinkDefinitionReview.revision_no.asc()).all()

    def submit(
        self,
        link_mapping_id: str,
        *,
        submitted_by: str,
        reviewer_id: str | None = None,
        candidate: dict | None = None,
    ) -> tuple[MappingLinkDefinitionReview, bool]:
        link = self._mapping_query(link_mapping_id).first()
        if link is None:
            raise LookupError("Link Mapping not found")
        if candidate is not None and link.status not in {"draft", "inferred"}:
            raise ValueError("已启用或已应用的关系映射不可原地修改")
        if reviewer_id and str(reviewer_id) == str(submitted_by):
            raise PermissionError("关系映射审核人必须与提交人不同")

        latest = latest_link_mapping_review(self._db, link.id)
        if candidate is not None:
            if latest and latest.status == "pending_review":
                raise ValueError("当前关系映射候选正在等待审核")
            if latest and latest.status not in {"rejected", "changes_requested"}:
                raise ValueError("仅草稿或被退回的关系映射可以修改")
            snapshot, digest = link_mapping_snapshot(link, candidate)
            link.relation_type = snapshot["relation_type"]
            link.src_key = snapshot["src_key"]
            link.tgt_key = snapshot["tgt_key"]
            self._db.flush()
        else:
            snapshot, digest = link_mapping_snapshot(link)

        latest = latest_link_mapping_review(self._db, link.id)
        if latest and latest.candidate_digest == digest and latest.status in {"pending_review", "approved"}:
            return latest, False
        if latest and latest.status == "pending_review":
            raise ValueError("已有关系映射候选正在等待审核")

        revision_no = int(self._db.query(func.max(MappingLinkDefinitionReview.revision_no)).filter(
            MappingLinkDefinitionReview.link_mapping_id == link.id,
        ).scalar() or 0) + 1
        review = MappingLinkDefinitionReview(
            link_mapping_id=link.id,
            ontology_id=link.ontology_id,
            revision_no=revision_no,
            candidate_json=snapshot,
            candidate_digest=digest,
            status="pending_review",
            submitted_by=str(submitted_by),
            reviewer_id=str(reviewer_id) if reviewer_id else None,
        )
        self._db.add(review)
        try:
            self._db.flush()
            _append_event(self._db, review, actor_id=submitted_by, event_type="submitted")
            self._db.commit()
            self._db.refresh(review)
            return review, True
        except IntegrityError:
            self._db.rollback()
            latest = latest_link_mapping_review(self._db, link_mapping_id)
            if latest and latest.candidate_digest == digest and latest.status in {"pending_review", "approved"}:
                return latest, False
            raise

    def decide(
        self,
        link_mapping_id: str,
        review_id: str,
        *,
        decided_by: str,
        decision: str,
        notes: str = "",
    ) -> MappingLinkDefinitionReview:
        link = self._mapping_query(link_mapping_id).first()
        if link is None:
            raise LookupError("Link Mapping not found")
        review = self._db.query(MappingLinkDefinitionReview).filter(
            MappingLinkDefinitionReview.id == review_id,
            MappingLinkDefinitionReview.link_mapping_id == link.id,
            MappingLinkDefinitionReview.ontology_id == link.ontology_id,
        ).with_for_update().first()
        if review is None:
            raise LookupError("Link Mapping review not found")
        if review.status != "pending_review":
            raise ValueError("Link Mapping review has already been decided")
        if str(review.submitted_by or "") == str(decided_by):
            raise PermissionError("关系映射提交者不能审核自己的候选")
        if review.reviewer_id and str(review.reviewer_id) != str(decided_by):
            raise PermissionError("当前用户不是指派的关系映射审核人")
        if decision not in {"approved", "rejected", "changes_requested"}:
            raise ValueError("Invalid Link Mapping review decision")

        if decision == "approved":
            snapshot, digest = link_mapping_snapshot(link)
            if digest != review.candidate_digest or snapshot != review.candidate_json:
                raise ValueError("Link Mapping changed after review submission")
        review.status = decision
        review.decision = decision
        review.decision_note = notes.strip() or None
        review.decided_by = str(decided_by)
        review.decided_at = _now()
        _append_event(self._db, review, actor_id=decided_by, event_type=decision)
        self._db.commit()
        self._db.refresh(review)
        return review
