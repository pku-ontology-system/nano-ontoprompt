from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func

from app.models.v2.mapping_apply_run import MappingApplyRun
from app.models.v2.curated import CuratedDataset, CuratedReview
from app.models.v2.dataset import DatasetVersion
from app.models.v2.mapping import OntologyMapping
from app.services.v2.mapping_apply_runs import MappingApplyRunService
from app.models.ontology import OntologyProject
from app.models.v2.pipeline import Pipeline


def _enqueue(db, *, review_id="review-1", mapping_id="mapping-1"):
    if db.get(CuratedDataset, "dataset-1") is None:
        db.add(CuratedDataset(id="dataset-1", name="reviewed source", status="approved"))
    if db.get(DatasetVersion, "version-1") is None:
        db.add(DatasetVersion(
            id="version-1", dataset_id="dataset-1", version_no=1,
            storage_uri="s3://datasets/dataset-1/v1.csv",
        ))
    if db.get(CuratedReview, review_id) is None:
        db.add(CuratedReview(
            id=review_id, curated_dataset_id="dataset-1", status="approved",
            dataset_version_id="version-1", decided_at=datetime.now(timezone.utc),
        ))
    if db.get(OntologyMapping, mapping_id) is None:
        db.add(OntologyMapping(
            id=mapping_id, ontology_id="ontology-1", curated_dataset_id="dataset-1",
            entity_class="Supplier", field_mapping={"id": "supplier_id"}, status="active",
        ))
    db.commit()
    return MappingApplyRunService(db).enqueue_review_mapping(
        mapping_id=mapping_id,
        ontology_id="ontology-1",
        curated_dataset_id="dataset-1",
        review_id=review_id,
        dataset_version_id="version-1",
    )


def _queued_run(db, *, celery_task_id="task-1", review_id="review-1"):
    run, _ = _enqueue(db, review_id=review_id)
    run.status = "queued"
    run.celery_task_id = celery_task_id
    run.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    db.commit()
    return run


class RecordingTask:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def apply_async(self, *, args, task_id):
        self.calls.append({"args": args, "task_id": task_id})
        if self.error:
            raise self.error


def test_enqueue_is_idempotent_for_mapping_review_pair(db):
    first, created_first = _enqueue(db)
    second, created_second = _enqueue(db)

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert db.query(func.count(MappingApplyRun.id)).scalar() == 1


def test_enqueue_rejects_reusing_idempotency_key_after_mapping_change(db):
    _enqueue(db)
    mapping = db.get(OntologyMapping, "mapping-1")
    mapping.field_mapping = {"id": "other_id"}
    db.commit()

    with pytest.raises(ValueError, match="different mapping request"):
        _enqueue(db)


def test_dispatch_persists_queued_run_before_publishing(db):
    run, _ = _enqueue(db)
    task = RecordingTask()

    result = MappingApplyRunService(db, dispatch_task=task).dispatch(run.id)

    assert result["status"] == "queued"
    assert result["celery_task_id"] == task.calls[0]["task_id"]
    assert task.calls[0]["args"] == [run.id]
    persisted = db.get(MappingApplyRun, run.id)
    assert persisted.status == "queued"
    assert persisted.next_attempt_at is not None


def test_dispatch_uses_the_registered_durable_celery_task_by_default(db):
    run, _ = _enqueue(db)
    task = RecordingTask()

    from unittest.mock import patch
    with patch("app.tasks.v2.mapping_apply.mapping_apply_run_task", task):
        result = MappingApplyRunService(db).dispatch(run.id)

    assert result["status"] == "queued"
    assert task.calls[0]["args"] == [run.id]


def test_broker_failure_keeps_a_retryable_durable_run(db):
    run, _ = _enqueue(db)
    task = RecordingTask(error=ConnectionError("broker offline"))

    result = MappingApplyRunService(db, dispatch_task=task).dispatch(run.id)

    assert result["status"] == "retry_wait"
    assert result["error_code"] == "dispatch_failed"
    assert result["next_attempt_at"] is not None
    assert db.get(MappingApplyRun, run.id).dispatch_attempts == 1


def test_execute_applies_mapping_once_and_persists_result(db):
    run = _queued_run(db)
    result = {"instances_written": 3, "dataset_version_id": "version-1"}

    from unittest.mock import patch
    with patch("app.tasks.v2.mapping_apply.apply_approved_mapping", return_value=result) as apply:
        completed = MappingApplyRunService(db).execute(run.id, celery_task_id="task-1")

    apply.assert_called_once_with(
        db, "mapping-1", "ontology-1", "version-1", "review-1",
        expected_mapping_digest=run.mapping_digest,
        mapping_apply_run_id=run.id,
    )
    assert completed["status"] == "succeeded"
    assert completed["result"] == result
    assert completed["execution_attempts"] == 1


def test_duplicate_or_stale_task_cannot_claim_a_run(db):
    run = _queued_run(db)

    result = MappingApplyRunService(db).execute(run.id, celery_task_id="old-task")

    assert result["status"] == "queued"
    assert result["execution_attempts"] == 0


def test_deterministic_mapping_error_is_terminal(db):
    run = _queued_run(db)

    from unittest.mock import patch
    with patch("app.tasks.v2.mapping_apply.apply_approved_mapping",
               side_effect=ValueError("review is no longer valid")):
        result = MappingApplyRunService(db).execute(run.id, celery_task_id="task-1")

    assert result["status"] == "failed"
    assert result["error_code"] == "ValueError"
    assert result["completed_at"] is not None


def test_transient_mapping_error_schedules_retry(db):
    run = _queued_run(db)

    from unittest.mock import patch
    with patch("app.tasks.v2.mapping_apply.apply_approved_mapping",
               side_effect=ConnectionError("projection unavailable")):
        result = MappingApplyRunService(db).execute(run.id, celery_task_id="task-1")

    assert result["status"] == "retry_wait"
    assert result["error_code"] == "ConnectionError"
    assert result["next_attempt_at"] is not None


def test_stale_worker_cannot_overwrite_a_newer_attempt(db):
    run = _queued_run(db)

    def replace_attempt(session, *args, **kwargs):
        current = session.get(MappingApplyRun, run.id)
        current.celery_task_id = "new-task"
        session.commit()
        return {"instances_written": 1}

    from unittest.mock import patch
    with patch("app.tasks.v2.mapping_apply.apply_approved_mapping", side_effect=replace_attempt):
        result = MappingApplyRunService(db).execute(run.id, celery_task_id="task-1")

    assert result["status"] == "running"
    assert result["celery_task_id"] == "new-task"


def test_mapping_definition_change_after_enqueue_blocks_application(db):
    run = _queued_run(db)
    mapping = db.get(OntologyMapping, "mapping-1")
    mapping.field_mapping = {"id": "changed_after_review"}
    db.commit()

    result = MappingApplyRunService(db).execute(run.id, celery_task_id="task-1")

    assert result["status"] == "failed"
    assert result["error_code"] == "ValueError"
    assert "Mapping definition changed" in result["error_message"]


def test_reconcile_requeues_expired_leases_but_preserves_live_worker(db):
    now = datetime.now(timezone.utc)
    queued, _ = _enqueue(db, review_id="review-queued")
    queued.status = "queued"
    queued.celery_task_id = "lost-task"
    queued.next_attempt_at = now - timedelta(minutes=5)
    queued.updated_at = now - timedelta(minutes=5)

    expired, _ = _enqueue(db, review_id="review-expired")
    expired.status = "running"
    expired.celery_task_id = "expired-task"
    expired.run_lease_until = now - timedelta(minutes=1)
    expired.updated_at = now - timedelta(minutes=1)

    active, _ = _enqueue(db, review_id="review-active")
    active.status = "running"
    active.celery_task_id = "active-task"
    active.run_lease_until = now + timedelta(minutes=15)
    active.updated_at = now
    db.commit()

    task = RecordingTask()
    summary = MappingApplyRunService(db, dispatch_task=task).recover_and_dispatch()

    assert summary["recovered"] == 2
    # The expired running attempt has an unknown outcome, so it enters the
    # normal backoff window before another dispatch. The stale queued intent
    # is immediately redelivered.
    assert summary["dispatched"] == 1
    assert len(task.calls) == 1
    assert db.get(MappingApplyRun, active.id).status == "running"
    assert db.get(MappingApplyRun, queued.id).status == "queued"
    expired_after_recovery = db.get(MappingApplyRun, expired.id)
    assert expired_after_recovery.status == "retry_wait"
    assert expired_after_recovery.error_code == "execution_outcome_unknown"
    next_attempt_at = expired_after_recovery.next_attempt_at
    if next_attempt_at.tzinfo is None:  # SQLite drops timezone metadata.
        next_attempt_at = next_attempt_at.replace(tzinfo=timezone.utc)
    assert next_attempt_at > now


def test_review_status_endpoint_returns_only_runs_for_requested_review(db):
    first = _queued_run(db, review_id="review-first")
    _queued_run(db, review_id="review-second")

    from app.routers.v2.curated import list_review_mapping_runs
    response = list_review_mapping_runs(
        "review-first", limit=50, db=db,
        user=SimpleNamespace(id="admin-test", role="admin"),
    )

    assert response["review_id"] == "review-first"
    assert [run["run_id"] for run in response["runs"]] == [first.id]


def test_review_approval_and_all_mapping_runs_commit_before_any_dispatch(db, monkeypatch):
    from sqlalchemy.orm import Session as SQLAlchemySession
    from app.models.v2.curated import CuratedReview
    from app.services.v2.incremental.orchestrator import IncrementalOrchestrator
    from app.services.v2.mapping_apply_runs import serialize_mapping_apply_run

    db.add(CuratedDataset(id="dataset-atomic", name="atomic source", status="in_review"))
    db.add(DatasetVersion(
        id="version-atomic", dataset_id="dataset-atomic", version_no=1,
        storage_uri="s3://datasets/atomic/v1.csv",
    ))
    db.add(CuratedReview(
        id="review-atomic", curated_dataset_id="dataset-atomic", status="approved",
        dataset_version_id="version-atomic", decided_at=datetime.now(timezone.utc),
    ))
    for mapping_id in ("mapping-atomic-a", "mapping-atomic-b"):
        db.add(OntologyMapping(
            id=mapping_id,
            ontology_id="ontology-atomic",
            curated_dataset_id="dataset-atomic",
            entity_class=mapping_id,
            field_mapping={"id": "source_id", "__auto_apply_on_review__": True},
            status="active",
        ))
    db.flush()

    def assert_outbox_is_committed_before_dispatch(service, run_id):
        with SQLAlchemySession(bind=service._db.get_bind()) as observer:
            committed_review = observer.get(CuratedReview, "review-atomic")
            committed_runs = observer.query(MappingApplyRun).filter(
                MappingApplyRun.review_id == "review-atomic",
            ).all()
            assert committed_review.status == "approved"
            assert len(committed_runs) == 2
        return serialize_mapping_apply_run(service._db.get(MappingApplyRun, run_id))

    monkeypatch.setattr(MappingApplyRunService, "dispatch", assert_outbox_is_committed_before_dispatch)
    result = IncrementalOrchestrator(db).on_review_approved("review-atomic")

    assert len(result["triggered_mappings"]) == 2
    assert {run["status"] for run in result["triggered_mappings"]} == {"pending"}


def _login_headers(client, username, password):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_legacy_mapping_route_requires_ontology_access(client, editor_user, ontology):
    headers = _login_headers(client, "editor", "editor123")

    response = client.get(
        f"/api/v2/ontologies/{ontology['id']}/mappings",
        headers=headers,
    )

    assert response.status_code == 404


def test_legacy_mapping_create_requires_pipeline_ownership(client, db, editor_user, admin_user):
    db.add(OntologyProject(
        id="editor-owned-ontology", name="editor ontology", domain="test",
        created_by=editor_user.id,
    ))
    db.add_all([
        Pipeline(id="editor-pipeline", name="editor source", created_by=editor_user.id),
        Pipeline(id="admin-pipeline", name="admin source", created_by=admin_user.id),
    ])
    db.add_all([
        CuratedDataset(id="editor-dataset", name="editor dataset", pipeline_id="editor-pipeline"),
        CuratedDataset(id="admin-dataset", name="admin dataset", pipeline_id="admin-pipeline"),
    ])
    db.commit()
    headers = _login_headers(client, "editor", "editor123")
    body = {
        "entity_class": "Supplier",
        "field_mapping": {"supplier_id": "id"},
        "confidence": 0.9,
    }

    allowed = client.post(
        "/api/v2/ontologies/editor-owned-ontology/mappings",
        json={**body, "curated_dataset_id": "editor-dataset"},
        headers=headers,
    )
    denied = client.post(
        "/api/v2/ontologies/editor-owned-ontology/mappings",
        json={**body, "curated_dataset_id": "admin-dataset"},
        headers=headers,
    )

    assert allowed.status_code == 200, allowed.text
    assert denied.status_code == 404
    assert db.query(OntologyMapping).filter_by(curated_dataset_id="admin-dataset").count() == 0


def test_mapping_apply_rejects_mapping_from_another_ontology(
    client, db, admin_user, auth_headers, ontology, monkeypatch,
):
    db.add_all([
        OntologyProject(
            id="other-ontology", name="other ontology", domain="test",
            created_by=admin_user.id,
        ),
        CuratedDataset(id="source-for-mapping", name="mapping source"),
        OntologyMapping(
            id="mapping-owned-by-first-ontology", ontology_id=ontology["id"],
            curated_dataset_id="source-for-mapping", entity_class="Supplier",
            field_mapping={"supplier_id": "id"}, status="active",
        ),
    ])
    db.commit()
    from app.services.v2.mapping.mapping_service import MappingService
    called = False

    def unexpected_apply(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("cross-ontology mapping reached the write service")

    monkeypatch.setattr(MappingService, "apply_mapping", unexpected_apply)
    before = db.query(OntologyMapping).count()
    response = client.post(
        "/api/v2/ontologies/other-ontology/mappings/mapping-owned-by-first-ontology/apply",
        json=[{"supplier_id": "S-1"}],
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert called is False
    assert db.query(OntologyMapping).count() == before
