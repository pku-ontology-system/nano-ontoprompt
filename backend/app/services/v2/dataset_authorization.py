"""Resource-scoped access checks for versioned Dataset records."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.v2.dataset import Dataset


def _linked_ontology_ids(db: Session, dataset_id: str) -> set[str]:
    """Resolve every ontology whose provenance currently includes a dataset."""
    from app.models.v2.construction import ConstructionRun
    from app.models.v2.construction_draft import ConstructionDraft
    from app.models.v2.dataset import DatasetVersion
    from app.models.v2.mapping import OntologyLinkMapping, OntologyMapping
    from app.models.v2.temporal_replay import TemporalReplay

    ids: set[str] = {
        str(row[0]) for row in db.query(OntologyMapping.ontology_id).filter(
            OntologyMapping.curated_dataset_id == dataset_id,
        ).all()
    }
    for column in (OntologyLinkMapping.src_dataset_id, OntologyLinkMapping.tgt_dataset_id):
        ids.update(
            str(row[0]) for row in db.query(OntologyLinkMapping.ontology_id).filter(
                column == dataset_id,
            ).all()
        )
    ids.update(
        str(row[0]) for row in db.query(ConstructionRun.ontology_id).filter(
            ConstructionRun.dataset_id == dataset_id,
        ).all()
    )
    ids.update(
        str(row[0]) for row in db.query(ConstructionDraft.ontology_id).filter(
            ConstructionDraft.dataset_id == dataset_id,
            ConstructionDraft.ontology_id.is_not(None),
        ).all()
    )
    ids.update(
        str(row[0]) for row in db.query(TemporalReplay.ontology_id).filter(
            TemporalReplay.dataset_id == dataset_id,
        ).all()
    )
    ids.update(
        str(row[0]) for row in db.query(TemporalReplay.ontology_id).join(
            DatasetVersion, DatasetVersion.id == TemporalReplay.dataset_version_id,
        ).filter(DatasetVersion.dataset_id == dataset_id).all()
    )
    return ids


def _is_creator_or_source_owner(db: Session, dataset: Dataset, user: User) -> bool:
    principal_id = str(user.id)
    if str(dataset.created_by or "") == principal_id:
        return True

    if dataset.source_connection_id:
        from app.models.v2.connection import Connection

        connection = db.get(Connection, dataset.source_connection_id)
        if connection and str(connection.created_by or "") == principal_id:
            return True

    from app.models.v2.curated import CuratedDataset
    from app.models.v2.pipeline import Pipeline

    pipeline_ids = {
        str(row[0]) for row in db.query(Pipeline.id).filter(
            Pipeline.source_dataset_id == dataset.id,
            Pipeline.created_by == principal_id,
        ).all()
    }
    legacy_pipeline_id = db.query(CuratedDataset.pipeline_id).filter(
        CuratedDataset.id == dataset.id,
    ).scalar()
    if legacy_pipeline_id and db.query(Pipeline.id).filter(
        Pipeline.id == legacy_pipeline_id,
        Pipeline.created_by == principal_id,
    ).first():
        return True
    return bool(pipeline_ids)


def require_dataset_access(
    db: Session,
    dataset_or_id: Dataset | str,
    user: User,
    *,
    write: bool = False,
) -> Dataset:
    """Require current resource policy or creator scope; hide inaccessible IDs."""
    if write and user.role not in {"admin", "editor"}:
        raise HTTPException(403, "Editor role required")

    dataset = dataset_or_id if isinstance(dataset_or_id, Dataset) else db.get(Dataset, str(dataset_or_id))
    if dataset is None:
        raise HTTPException(404, "Dataset not found or inaccessible")
    if user.role == "admin":
        return dataset

    if (dataset.schema_json or {}).get("access_scope") == "shared_read":
        return dataset

    ontology_ids = _linked_ontology_ids(db, str(dataset.id))
    if ontology_ids:
        from app.services.v2.object_query.authorization import require_ontology_access
        from app.services.v2.object_query.errors import ObjectQueryError

        try:
            for ontology_id in sorted(ontology_ids):
                require_ontology_access(db, ontology_id, str(user.id), user.role)
        except ObjectQueryError:
            raise HTTPException(404, "Dataset not found or inaccessible") from None
        return dataset

    if _is_creator_or_source_owner(db, dataset, user):
        return dataset
    raise HTTPException(404, "Dataset not found or inaccessible")
