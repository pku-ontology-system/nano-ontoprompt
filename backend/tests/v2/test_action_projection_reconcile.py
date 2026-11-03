from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import sessionmaker

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.v2.action import OntologyActionRun, OntologyActionType
from app.models.v2.decision_case import DecisionCase
from app.services.v2 import decision_cases
from app.services.v2.decision_cases import DecisionCaseService, digest


def _seed_run(db, ontology_id, *, actor_id, attempts=0):
    entity = Entity(
        id="projection-machine-type", ontology_id=ontology_id,
        name_cn="设备", name_en="Machine", type="entity",
    )
    instance = EntityInstance(
        id="projection-machine-1", entity_id=entity.id, ontology_id=ontology_id,
        row_identity="projection-machine-1", row_data={"status": "approved"},
    )
    action_type = OntologyActionType(
        id="projection-action-type", ontology_id=ontology_id,
        name="Update machine", action_category="write", parameters=[], effects=[],
    )
    case = DecisionCase(
        id="projection-case-1", ontology_id=ontology_id,
        action_type_id=action_type.id, kind="general", title="Projection retry",
        target_object_id=instance.id, target_object_type="Machine", parameters={},
        evidence_refs=[], reason="projection test", context_json={},
        context_digest=digest({}), expected_etag=digest({}),
        client_request_id="projection-case-request", request_digest=digest({}),
        status="completed", created_by=actor_id,
    )
    run = OntologyActionRun(
        ontology_id=ontology_id, action_type_id=action_type.id,
        target_object_id=instance.id, parameters={}, execution_context={"consistency": "live"},
        status="completed", decision_case_id="projection-case-1",
        before_snapshot={instance.id: {"status": "pending"}},
        after_digest=digest(instance.row_data), projection_status="pending",
        projection_attempts=attempts,
    )
    db.add_all([entity, instance, action_type, case, run])
    db.flush()
    case.action_run_id = run.id
    db.commit()
    return run


def test_projection_failure_persists_retry_schedule_and_attempt(db, ontology, admin_user, monkeypatch):
    run = _seed_run(db, ontology["id"], actor_id=admin_user.id)

    class UnavailableGraph:
        available = False

    monkeypatch.setattr(decision_cases, "FalkorDBService", UnavailableGraph)
    updated = DecisionCaseService(db, ontology["id"], admin_user).sync_projection(run)

    assert updated.projection_status == "failed"
    assert updated.projection_attempts == 1
    assert updated.projection_last_attempt_at is not None
    assert updated.projection_next_attempt_at is not None


def test_projection_success_clears_retry_schedule(db, ontology, admin_user, monkeypatch):
    run = _seed_run(db, ontology["id"], actor_id=admin_user.id)
    graph_writes = []

    class AvailableGraph:
        available = True

        def upsert_instances(self, ontology_id, rows):
            graph_writes.extend(rows)
            return 1

    monkeypatch.setattr(decision_cases, "FalkorDBService", AvailableGraph)
    updated = DecisionCaseService(db, ontology["id"], admin_user).sync_projection(run)

    assert updated.projection_status == "synced"
    assert updated.projection_attempts == 1
    assert updated.projection_next_attempt_at is None
    assert graph_writes == [{"id": run.target_object_id, "entity_type": "Machine",
                             "properties": {"status": "approved"}}]


def test_manual_projection_retry_uses_separate_budget_and_reason(db, ontology, admin_user, monkeypatch):
    run = _seed_run(db, ontology["id"], actor_id=admin_user.id, attempts=8)

    class UnavailableGraph:
        available = False

    monkeypatch.setattr(decision_cases, "FalkorDBService", UnavailableGraph)
    updated = DecisionCaseService(db, ontology["id"], admin_user).sync_projection(
        run, manual_retry=True, manual_reason="operator verified graph availability",
        client_request_id="manual-reconcile-1",
    )

    assert updated.projection_status == "failed"
    assert updated.projection_attempts == 8
    assert updated.projection_manual_attempts == 1
    assert updated.projection_next_attempt_at is None
    replayed = DecisionCaseService(db, ontology["id"], admin_user).sync_projection(
        updated, manual_retry=True, manual_reason="operator verified graph availability",
        client_request_id="manual-reconcile-1",
    )
    assert replayed.projection_attempts == 8
    assert replayed.projection_manual_attempts == 1
    events = db.query(decision_cases.DecisionCaseEvent).filter_by(
        case_id="projection-case-1",
    ).all()
    assert len([event for event in events if event.event_type == "projection_attempt_started"]) == 1
    started = next(event for event in events if event.event_type == "projection_attempt_started")
    finished = next(event for event in events if event.event_type == "projection_attempt_finished")
    assert started.payload["reason"] == "operator verified graph availability"
    assert started.payload["attempt_kind"] == "manual_retry"
    assert finished.payload["status"] == "failed"


def test_projection_retry_delay_is_exponential_and_bounded():
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert decision_cases._projection_retry_at(1, now=instant) == instant + timedelta(seconds=30)
    assert decision_cases._projection_retry_at(2, now=instant) == instant + timedelta(seconds=60)
    assert decision_cases._projection_retry_at(8, now=instant) is None


def test_celery_reconcile_scans_and_delivers_due_projection(db, ontology, admin_user, monkeypatch):
    run = _seed_run(db, ontology["id"], actor_id=admin_user.id)
    session_factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    monkeypatch.setattr("app.database.SessionLocal", session_factory)
    graph_writes = []

    class AvailableGraph:
        available = True

        def upsert_instances(self, ontology_id, rows):
            graph_writes.extend(rows)
            return 1

    monkeypatch.setattr(decision_cases, "FalkorDBService", AvailableGraph)
    from app.tasks.v2.action_projection import reconcile_action_projections

    result = reconcile_action_projections()

    db.refresh(run)
    assert result == {"scanned": 1, "synced": 1, "failed": 0, "conflict": 0, "skipped": 0}
    assert run.projection_status == "synced"
    assert run.projection_attempts == 1
    assert len(graph_writes) == 1
