"""ReviewService 单元测试"""
import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from fastapi import HTTPException
from app.services.v2.curated.review_service import ReviewService
from app.services.v2.dataset_service import DatasetService, DatasetStorageError
from app.models.v2.curated import CuratedDataset, CuratedReview, CuratedRowEdit
from app.models.v2.dataset import Dataset
import uuid


def make_db_with_dataset(status="pending_review"):
    db = MagicMock()
    ds = CuratedDataset(
        id="ds-1", name="test_dataset", status=status,
        pipeline_id=None, schema_json=None,
    )
    review = CuratedReview(
        id="rev-1", curated_dataset_id="ds-1", status="pending",
        dataset_version_id="version-1",
    )

    def query_side_effect(model):
        mock_q = MagicMock()
        mock_q.filter.return_value.with_for_update.return_value = mock_q.filter.return_value
        if model == CuratedDataset:
            mock_q.filter.return_value.first.return_value = ds
        elif model == CuratedReview:
            mock_q.filter.return_value.first.return_value = review
        elif model == CuratedRowEdit:
            mock_q.filter.return_value.all.return_value = []
        return mock_q

    db.query.side_effect = query_side_effect
    db.add = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    return db, ds, review


def test_start_review_creates_record():
    db, ds, _ = make_db_with_dataset()
    svc = ReviewService(db)

    # refresh 时设置 id
    def refresh_side_effect(obj):
        if isinstance(obj, CuratedReview) and not obj.id:
            obj.id = "new-rev-1"
    db.refresh.side_effect = refresh_side_effect

    with patch.object(DatasetService, "latest_version", return_value=SimpleNamespace(id="version-7")):
        review = svc.start_review("ds-1")
    db.add.assert_called_once()
    db.commit.assert_called()
    assert ds.status == "in_review"
    assert review.dataset_version_id == "version-7"


def test_approve_updates_status():
    db, ds, review = make_db_with_dataset("in_review")
    review.requested_by = "requester"
    svc = ReviewService(db)
    result = svc.approve("rev-1", notes="数据看起来不错", decided_by="reviewer")
    assert review.status == "approved"
    assert review.decided_by == "reviewer"
    assert ds.status == "approved"
    assert review.notes == "数据看起来不错"
    assert review.decided_at is not None
    assert review.dataset_version_id == "version-1"


def test_requester_cannot_approve_their_own_review():
    from fastapi import HTTPException
    db, ds, review = make_db_with_dataset("in_review")
    review.requested_by = "requester"
    with pytest.raises(HTTPException) as exc:
        ReviewService(db).approve("rev-1", decided_by="requester")
    assert exc.value.status_code == 409
    assert review.status == "pending" and ds.status == "in_review"


def test_start_review_allows_legacy_dataset_without_version_but_does_not_invent_one():
    db, ds, _ = make_db_with_dataset()
    svc = ReviewService(db)

    with patch.object(DatasetService, "latest_version", side_effect=DatasetStorageError("ds-1", None, "no version")):
        review = svc.start_review("ds-1")

    assert review.dataset_version_id is None


def test_start_review_rejects_preview_that_became_stale():
    db, _, _ = make_db_with_dataset()
    svc = ReviewService(db)

    with patch.object(DatasetService, "latest_version", return_value=SimpleNamespace(id="version-8")):
        with pytest.raises(HTTPException, match="Dataset changed after preview") as exc_info:
            svc.start_review("ds-1", expected_dataset_version_id="version-7")

    assert getattr(exc_info.value, "status_code", None) == 409
    db.add.assert_not_called()


def test_start_review_materializes_v2_curated_dataset_compatibility_row():
    db = MagicMock()
    source = Dataset(
        id="ds-v2", name="from pipeline", kind="curated",
        schema_json={"columns": ["id"]}, latest_version_id="version-9",
    )

    def query_side(model):
        q = MagicMock()
        if model == CuratedDataset:
            q.filter.return_value.first.return_value = None
        elif model == Dataset:
            q.filter.return_value.first.return_value = source
        return q

    db.query.side_effect = query_side
    db.add = MagicMock()
    db.flush = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    svc = ReviewService(db)

    with patch.object(DatasetService, "latest_version", return_value=SimpleNamespace(id="version-9")):
        review = svc.start_review("ds-v2")

    compatibility_row = next(
        call.args[0] for call in db.add.call_args_list
        if isinstance(call.args[0], CuratedDataset)
    )
    assert compatibility_row.latest_version_id == "version-9"
    assert review.dataset_version_id == "version-9"


def test_reject_updates_status():
    db, ds, review = make_db_with_dataset("in_review")
    svc = ReviewService(db)
    result = svc.reject("rev-1", notes="存在数据质量问题")
    assert review.status == "rejected"
    assert ds.status == "rejected"


def test_edit_row_creates_edit_record():
    db, _, review = make_db_with_dataset()
    svc = ReviewService(db)
    edit = svc.edit_row("rev-1", "row-1", "name", "旧名称", "新名称")
    db.add.assert_called()
    db.commit.assert_called()


def test_batch_edit_rows():
    db, _, review = make_db_with_dataset()
    svc = ReviewService(db)
    edits = [
        {"row_pk": "1", "field_name": "name", "old_value": "A", "new_value": "B"},
        {"row_pk": "2", "field_name": "age", "old_value": "25", "new_value": "26"},
    ]
    results = svc.batch_edit_rows("rev-1", edits)
    assert len(results) == 2
    assert db.add.call_count == 2


def test_batch_edit_rows_persists_editor_identity():
    db, _, _ = make_db_with_dataset()
    svc = ReviewService(db)

    results = svc.batch_edit_rows(
        "rev-1",
        [{"row_pk": "1", "field_name": "name", "old_value": "A", "new_value": "B"}],
        edited_by="editor-1",
    )

    assert results[0].edited_by == "editor-1"


def test_apply_edits_to_snapshot():
    db = MagicMock()
    review = CuratedReview(id="rev-1", curated_dataset_id="ds-1", status="pending")

    edits = [
        CuratedRowEdit(review_id="rev-1", row_pk="1", field_name="name", old_value="Alice", new_value="Alice Smith"),
        CuratedRowEdit(review_id="rev-1", row_pk="2", field_name="age", old_value="25", new_value=None),
    ]
    db.query.return_value.filter.return_value.all.return_value = edits

    svc = ReviewService(db)
    original = [
        {"id": "1", "name": "Alice", "age": "30"},
        {"id": "2", "name": "Bob",   "age": "25"},
    ]
    result = svc.apply_edits_to_snapshot("rev-1", original)

    # row 1: name 修改
    assert result[0]["name"] == "Alice Smith"
    # row 2: age 被删除（new_value=None）
    assert "age" not in result[1]


def test_get_edits_empty():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    svc = ReviewService(db)
    result = svc.get_edits("rev-1")
    assert result == []


def test_apply_edits_no_edits_returns_original():
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    svc = ReviewService(db)
    original = [{"id": "1", "name": "Alice"}]
    result = svc.apply_edits_to_snapshot("rev-1", original)
    assert result == original


@pytest.mark.parametrize("row_pk", ["SUP-1", "0"])
def test_apply_edits_matches_business_key_or_review_table_index(row_pk):
    db = MagicMock()
    edit = CuratedRowEdit(
        review_id="rev-1", row_pk=row_pk,
        field_name="supplier_name", old_value="Old", new_value="New",
    )
    db.query.return_value.filter.return_value.all.return_value = [edit]
    svc = ReviewService(db)

    result = svc.apply_edits_to_snapshot(
        "rev-1", [{"supplier_id": "SUP-1", "supplier_name": "Old"}],
        row_identity_column="supplier_id",
    )

    assert result == [{"supplier_id": "SUP-1", "supplier_name": "New"}]


def test_apply_edits_rejects_rows_not_in_reviewed_snapshot():
    db = MagicMock()
    edit = CuratedRowEdit(
        review_id="rev-1", row_pk="STALE", field_name="name",
        old_value="Old", new_value="New",
    )
    db.query.return_value.filter.return_value.all.return_value = [edit]
    svc = ReviewService(db)

    with pytest.raises(ValueError, match="absent from its pinned dataset version"):
        svc.apply_edits_to_snapshot("rev-1", [{"id": "CURRENT", "name": "Old"}])
