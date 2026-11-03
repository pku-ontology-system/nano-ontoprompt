"""Approvals that Plan A v4 requires before Mapping data reaches the ontology.

Mapping Apply runs only for a Mapping definition approved by someone other
than its submitter, over a data review decided by someone other than its
requester. Tests that exercise apply paths record those approvals here.
"""
from datetime import datetime, timezone

from sqlalchemy import func

from app.models.ontology import OntologyProject
from app.models.user import User
from app.models.v2.mapping_definition_review import MappingDefinitionReview
from app.services.v2.mapping_apply_runs import mapping_definition_snapshot

REQUESTER, REVIEWER = "apply-requester", "apply-reviewer"


def ensure_actors(db, ontology_id=None):
    """Create the requester and reviewer, and the ontology their reviews belong to."""
    for user_id in (REQUESTER, REVIEWER):
        if db.get(User, user_id) is None:
            db.add(User(id=user_id, username=user_id, email=f"{user_id}@example.test",
                        password_hash="unused", role="editor"))
    if ontology_id and db.get(OntologyProject, ontology_id) is None:
        db.add(OntologyProject(id=ontology_id, name=ontology_id, domain="test", created_by=REQUESTER))
    db.flush()


def review_actors():
    """Keyword arguments for a CuratedReview decided independently of its requester."""
    return {"requested_by": REQUESTER, "reviewer_id": REVIEWER, "decided_by": REVIEWER}


def approve_mapping_definition(db, mapping):
    """Record an independent approval of the Mapping definition as it stands now."""
    ensure_actors(db, mapping.ontology_id)
    candidate, digest = mapping_definition_snapshot(mapping)
    revision = (db.query(func.max(MappingDefinitionReview.revision_no))
                .filter(MappingDefinitionReview.mapping_id == mapping.id).scalar() or 0) + 1
    review = MappingDefinitionReview(
        mapping_id=mapping.id, ontology_id=mapping.ontology_id, revision_no=revision,
        candidate_json=candidate, candidate_digest=digest, status="approved",
        submitted_by=REQUESTER, reviewer_id=REVIEWER, decided_by=REVIEWER, decision="approved",
        decided_at=datetime.now(timezone.utc),
    )
    db.add(review)
    db.flush()
    return review
