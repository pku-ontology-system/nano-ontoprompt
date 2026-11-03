from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models.v2.curated import CuratedDataset, CuratedReview, CuratedRowEdit
from app.models.v2.dataset import DatasetVersion
from app.models.v2.mapping import OntologyMapping
from app.tasks.v2.mapping_apply import apply_approved_mapping
from tests.v2.review_approvals import approve_mapping_definition, ensure_actors, review_actors


def _seed_reviewed_mapping(db, *, review_version="version-1"):
    ensure_actors(db, "ontology-1")
    db.add_all([
        CuratedDataset(id="curated-1", name="approved source", status="approved"),
        DatasetVersion(
            id=review_version, dataset_id="curated-1", version_no=1,
            storage_uri="s3://raw-datasets/curated-1/v1/data.csv",
        ),
        CuratedReview(
            id="review-1", curated_dataset_id="curated-1", status="approved",
            dataset_version_id=review_version, decided_at=datetime.now(timezone.utc),
            **review_actors(),
        ),
        OntologyMapping(
            id="mapping-1", ontology_id="ontology-1", curated_dataset_id="curated-1",
            entity_class="Supplier", field_mapping={"supplier_id": "id"}, status="applied",
        ),
    ])
    db.flush()
    approve_mapping_definition(db, db.get(OntologyMapping, "mapping-1"))
    db.commit()
    return db.query(DatasetVersion).filter(DatasetVersion.id == review_version).one()


def test_apply_approved_mapping_uses_reviewed_version_and_records_provenance(db):
    version = _seed_reviewed_mapping(db)
    rows = [{"supplier_id": "S-1", "name": "Supplier One"}]
    manifest = {
        "dataset_id": "curated-1", "dataset_version_id": "version-1",
        "version_no": 1, "source_checksum": "sha256:fixture", "row_count": 1,
        "read_complete": True,
    }

    with patch("app.services.v2.dataset_service.DatasetService.read_complete_version_snapshot",
               return_value=(version, rows, manifest)) as snapshot, \
         patch("app.services.v2.mapping.mapping_service.MappingService.apply_mapping",
               return_value={"instances_written": 1}) as apply:
        result = apply_approved_mapping(
            db, "mapping-1", "ontology-1", "version-1", "review-1",
        )

    snapshot.assert_called_once_with("curated-1", "version-1")
    apply.assert_called_once_with("mapping-1", rows, source_version_id="version-1", strict_source_schema=True)
    assert result == {
        "instances_written": 1,
        "review_id": "review-1",
        "source_manifest": manifest,
    }


def test_apply_approved_mapping_rejects_a_version_not_reviewed(db):
    _seed_reviewed_mapping(db)

    with patch("app.services.v2.dataset_service.DatasetService.preview_version_id") as preview, \
         patch("app.services.v2.mapping.mapping_service.MappingService.apply_mapping") as apply:
        with pytest.raises(ValueError, match="No approved review authorizes"):
            apply_approved_mapping(db, "mapping-1", "ontology-1", "version-2", "review-1")

    preview.assert_not_called()
    apply.assert_not_called()


def test_apply_approved_mapping_applies_review_edits_before_upsert(db):
    version = _seed_reviewed_mapping(db)
    db.add(CuratedRowEdit(
        review_id="review-1", row_pk="S-1", field_name="name",
        old_value="Old", new_value="Reviewed Name",
    ))
    db.commit()
    rows = [{"supplier_id": "S-1", "name": "Old"}]
    manifest = {
        "dataset_id": "curated-1", "dataset_version_id": "version-1",
        "version_no": 1, "source_checksum": "sha256:fixture", "row_count": 1,
        "read_complete": True,
    }

    with patch("app.services.v2.dataset_service.DatasetService.read_complete_version_snapshot",
               return_value=(version, rows, manifest)), \
         patch("app.services.v2.mapping.mapping_service.MappingService.apply_mapping",
               return_value={"instances_written": 1}) as apply:
        result = apply_approved_mapping(
            db, "mapping-1", "ontology-1", "version-1", "review-1",
        )

    edited_rows = [{"supplier_id": "S-1", "name": "Reviewed Name"}]
    apply.assert_called_once_with("mapping-1", edited_rows, source_version_id="version-1", strict_source_schema=True)
    assert result["review_id"] == "review-1"


def test_mapping_apply_task_is_registered_with_celery_worker_imports():
    from app.tasks.celery_app import celery_app
    from app.tasks.v2.mapping_apply import mapping_apply_run_task, reconcile_mapping_apply_runs

    assert "app.tasks.v2.mapping_apply" in celery_app.conf.include
    assert mapping_apply_run_task.name == "v2.mapping_apply"
    assert reconcile_mapping_apply_runs.name == "v2.mapping_apply.reconcile"
    assert "mapping-apply-reconcile-every-minute" in celery_app.conf.beat_schedule
