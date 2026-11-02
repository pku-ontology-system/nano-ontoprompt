"""Curated 审批权限 — PRD Security Logic: only admin can approve curated rows"""
import uuid

import pytest

from app.models.user import User
from app.models.v2.curated import CuratedDataset, CuratedReview, CuratedRowEdit
from app.models.v2.pipeline import Pipeline
from app.services.auth_service import hash_password


@pytest.fixture
def curated_client(client, db):
    """v2 curated 路由用模块内 get_db, 需单独覆盖到测试库"""
    from app.main import app
    from app.routers.v2 import curated

    def _override():
        yield db

    app.dependency_overrides[curated.get_db] = _override
    yield client
    app.dependency_overrides.pop(curated.get_db, None)


def _make_curated(db) -> str:
    ds = CuratedDataset(name=f"authz-{uuid.uuid4().hex[:6]}", status="pending_review", quality_score=0.8)
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return ds.id


def _login(client, username, password):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def test_editor_cannot_approve_curated(curated_client, db, editor_user):
    ds_id = _make_curated(db)
    headers = _login(curated_client, "editor", "editor123")
    r = curated_client.post(f"/api/v2/curated/{ds_id}/review", params={"action": "approve"}, headers=headers)
    assert r.status_code == 403


def test_admin_can_approve_curated(curated_client, db, admin_user):
    ds_id = _make_curated(db)
    headers = _login(curated_client, "admin", "admin123")
    r = curated_client.post(f"/api/v2/curated/{ds_id}/review", params={"action": "approve"}, headers=headers)
    assert r.status_code == 200
    db.refresh(db.query(CuratedDataset).filter(CuratedDataset.id == ds_id).first())
    assert db.query(CuratedDataset).filter(CuratedDataset.id == ds_id).first().status == "approved"


def test_row_edit_audit_records_actor_and_requires_review_access(curated_client, db, editor_user):
    pipeline = Pipeline(
        id="row-edit-audit-pipeline", name="row edit audit", created_by=editor_user.id,
    )
    dataset = CuratedDataset(
        id="row-edit-audit-dataset", name="row edit audit", status="in_review",
        pipeline_id=pipeline.id,
    )
    review = CuratedReview(
        id="row-edit-audit-review", curated_dataset_id=dataset.id,
        requested_by=editor_user.id, status="pending",
    )
    outsider = User(
        id=str(uuid.uuid4()), username="audit-outsider", email="audit-outsider@test.com",
        password_hash=hash_password("outsider123"), role="editor",
    )
    db.add_all([pipeline, dataset, review, outsider])
    db.commit()

    editor_headers = _login(curated_client, "editor", "editor123")
    save = curated_client.post(
        f"/api/v2/curated/reviews/{review.id}/edits",
        json={"edits": [{
            "row_pk": "supplier-7", "field_name": "name",
            "old_value": "Old name", "new_value": "Reviewed name",
        }]},
        headers=editor_headers,
    )
    assert save.status_code == 200, save.text
    assert save.json()["edits"][0]["edited_by"] == editor_user.id

    history = curated_client.get(
        f"/api/v2/curated/reviews/{review.id}/edits", headers=editor_headers,
    )
    assert history.status_code == 200, history.text
    assert history.json()["total"] == 1
    assert history.json()["edits"][0] == save.json()["edits"][0]
    assert db.query(CuratedRowEdit).filter_by(review_id=review.id).one().edited_by == editor_user.id

    outsider_headers = _login(curated_client, "audit-outsider", "outsider123")
    denied = curated_client.get(
        f"/api/v2/curated/reviews/{review.id}/edits", headers=outsider_headers,
    )
    assert denied.status_code == 404
