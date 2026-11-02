"""Current-access digest for persisted reasoning results."""
from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.v2.construction import EvidenceRef
from app.services.v2.dataset_authorization import require_dataset_access


def reasoning_permission_digest(db: Session, ontology_id: str, user, project_row, access) -> str:
    dataset_ids = {
        str(row[0]) for row in db.query(EvidenceRef.source_dataset_id).filter(
            EvidenceRef.ontology_id == ontology_id,
            EvidenceRef.source_dataset_id.is_not(None),
        ).distinct().all()
    }
    visible_dataset_ids = []
    for dataset_id in sorted(dataset_ids):
        try:
            require_dataset_access(db, dataset_id, user)
        except HTTPException:
            continue
        visible_dataset_ids.append(dataset_id)
    contract = {
        "ontology_id": str(ontology_id),
        "principal_id": str(user.id),
        "ontology_revision_id": str(project_row.current_revision_id or ""),
        "ontology_permission_digest": access.permission_digest,
        "visible_evidence_dataset_ids": visible_dataset_ids,
    }
    serialized = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()
