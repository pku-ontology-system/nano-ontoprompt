"""Serve test rows the way Plan A v4's Mapping reads them.

Mapping no longer reads a preview: it reads the latest complete, checksummed
dataset version (DatasetService.read_latest_complete_snapshot). Tests that
supply rows in memory serve them as that pinned version.
"""
import hashlib
import json
from contextlib import contextmanager
from unittest.mock import patch

from app.models.v2.dataset import DatasetVersion


@contextmanager
def complete_snapshots(db, rows_by_dataset):
    """Serve each dataset's rows as its latest complete version."""
    versions = {}
    for dataset_id in rows_by_dataset:
        version = db.query(DatasetVersion).filter_by(dataset_id=dataset_id, version_no=1).first()
        if version is None:
            version = DatasetVersion(dataset_id=dataset_id, version_no=1,
                                     storage_uri=f"memory://{dataset_id}/v1.json")
            db.add(version)
        versions[dataset_id] = version
    db.commit()

    def read(_service, dataset_id):
        rows = [dict(row) for row in rows_by_dataset[dataset_id]]
        version = versions[dataset_id]
        manifest = {"dataset_id": dataset_id, "dataset_version_id": version.id, "row_count": len(rows)}
        canonical = json.dumps({**manifest, "rows": rows}, ensure_ascii=False, sort_keys=True)
        manifest["manifest_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return version, rows, manifest

    with patch("app.services.v2.dataset_service.DatasetService.read_latest_complete_snapshot",
               autospec=True, side_effect=read):
        yield versions
