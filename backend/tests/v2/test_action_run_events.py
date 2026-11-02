import pytest
import uuid

from app.models.v2.action import OntologyActionRun, OntologyActionRunEvent
from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.user import User
from app.models.v2.security import OntologySecurityPolicy
from app.services.auth_service import create_access_token
from app.services.v2.action_runtime import (
    action_run_event_chain_valid,
    complete_action_run,
    create_action_run_record,
    transition_action_run,
)


def test_action_run_lifecycle_events_are_actor_attributed_and_hash_chained(
    db, ontology, admin_user,
):
    run = create_action_run_record(
        db=db,
        ontology_id=ontology["id"],
        action_type_id=None,
        initial_status="queued",
        requester_id=admin_user.id,
        executor_id=admin_user.id,
        client_request_id="audit-run-1",
        input_digest="i" * 64,
        trace_id="trace-audit-1",
    )
    transition_action_run(db, run, "running", actor_id=admin_user.id, actor_kind="user")
    complete_action_run(
        db,
        run,
        before_snapshot={"private": "do not copy into event"},
        after_snapshot={"private": "do not copy into event"},
        side_effect_results=[{"op": "set_property", "value": "do not copy into event"}],
        actor_id=admin_user.id,
        actor_kind="user",
    )
    db.commit()

    events = db.query(OntologyActionRunEvent).filter_by(
        action_run_id=run.id,
    ).order_by(OntologyActionRunEvent.sequence.asc()).all()
    assert [event.sequence for event in events] == [1, 2, 3]
    assert [event.to_status for event in events] == ["queued", "running", "completed"]
    assert events[0].event_type == "created"
    assert [(event.from_status, event.to_status) for event in events[1:]] == [
        ("queued", "running"),
        ("running", "completed"),
    ]
    assert all(event.actor_kind == "user" and event.actor_id == admin_user.id for event in events)
    assert all(event.request_id == "audit-run-1" and event.trace_id == "trace-audit-1" for event in events)
    assert events[0].previous_digest is None
    assert events[1].previous_digest == events[0].event_digest
    assert events[2].previous_digest == events[1].event_digest
    assert action_run_event_chain_valid(events)
    assert "do not copy into event" not in repr(events)

    events[1].to_status = "failed"
    with pytest.raises(ValueError, match="append-only"):
        db.flush()
    db.rollback()


def test_action_run_event_history_http_is_scoped_to_authorized_run(
    client, db, ontology, admin_user, auth_headers,
):
    run = create_action_run_record(
        db=db,
        ontology_id=ontology["id"],
        action_type_id=None,
        initial_status="queued",
        requester_id=admin_user.id,
        executor_id=admin_user.id,
        client_request_id="audit-http-1",
    )
    transition_action_run(db, run, "running", actor_id=admin_user.id, actor_kind="user")
    complete_action_run(
        db,
        run,
        before_snapshot={},
        after_snapshot={},
        side_effect_results=[],
        actor_id=admin_user.id,
        actor_kind="user",
    )
    db.commit()

    response = client.get(
        f"/api/v2/ontologies/{ontology['id']}/action-runs/{run.id}/events",
        headers=auth_headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run.id
    assert payload["chain_valid"] is True
    assert payload["history_truncated"] is False
    assert len(payload["events"]) == 3
    assert payload["events"][0]["event_type"] == "created"
    assert payload["events"][0]["actor_id"] == admin_user.id

    truncated = client.get(
        f"/api/v2/ontologies/{ontology['id']}/action-runs/{run.id}/events",
        params={"limit": 1},
        headers=auth_headers,
    )
    assert truncated.status_code == 200
    assert truncated.json()["chain_valid"] is True
    assert truncated.json()["history_truncated"] is True
    assert [row["sequence"] for row in truncated.json()["events"]] == [1]

    hidden = client.get(
        f"/api/v2/ontologies/{ontology['id']}/action-runs/not-a-run/events",
        headers=auth_headers,
    )
    assert hidden.status_code == 404


def test_legacy_action_run_without_events_is_reported_as_unavailable(
    client, db, ontology, admin_user, auth_headers,
):
    legacy_run = OntologyActionRun(
        ontology_id=ontology["id"],
        parameters={},
        execution_context={},
        status="completed",
        executed_by=admin_user.id,
    )
    db.add(legacy_run)
    db.commit()

    response = client.get(
        f"/api/v2/ontologies/{ontology['id']}/action-runs/{legacy_run.id}/events",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["history_available"] is False
    assert response.json()["chain_valid"] is None
    assert response.json()["events"] == []
    assert response.json()["history_truncated"] is False


def test_action_run_audit_is_visible_to_attributed_participants_only(
    db, admin_user, editor_user,
):
    from fastapi.testclient import TestClient

    from app.deps import get_db
    from app.main import app
    from app.models.ontology import OntologyProject

    project = OntologyProject(
        id=str(uuid.uuid4()), name="Action audit role matrix", domain="test",
        created_by=admin_user.id,
    )
    db.add(project)
    reviewer = User(
        id=str(uuid.uuid4()), username="audit-reviewer", email="audit-reviewer@test.com",
        password_hash="unused", role="editor",
    )
    executor = User(
        id=str(uuid.uuid4()), username="audit-executor", email="audit-executor@test.com",
        password_hash="unused", role="editor",
    )
    observer = User(
        id=str(uuid.uuid4()), username="audit-observer", email="audit-observer@test.com",
        password_hash="unused", role="editor",
    )
    db.add_all([reviewer, executor, observer])
    db.flush()
    # Give every non-admin a real ontology grant so the observer test reaches
    # the ActionRun participant boundary instead of failing the outer ACL.
    for principal in (editor_user, reviewer, executor, observer):
        db.add(OntologySecurityPolicy(
            ontology_id=project.id, name=f"audit access for {principal.username}",
            subject_kind="user", subject_id=principal.id, effect="allow",
            scope_kind="ontology", conditions_json=[], field_allowlist_json=[],
            created_by=admin_user.id,
        ))
    machine_type = Entity(
        id=str(uuid.uuid4()), ontology_id=project.id,
        name_cn="设备", name_en="Machine", type="entity",
    )
    supplier_type = Entity(
        id=str(uuid.uuid4()), ontology_id=project.id,
        name_cn="供应商", name_en="Supplier", type="entity",
    )
    db.add_all([machine_type, supplier_type])
    db.flush()
    hidden_for_requester = EntityInstance(
        id=str(uuid.uuid4()), entity_id=machine_type.id, ontology_id=project.id,
        row_identity="machine-audit-1", row_data={"name": "restricted"},
    )
    visible_to_requester = EntityInstance(
        id=str(uuid.uuid4()), entity_id=supplier_type.id, ontology_id=project.id,
        row_identity="supplier-audit-1", row_data={"name": "visible"},
    )
    db.add_all([hidden_for_requester, visible_to_requester])
    db.add(OntologySecurityPolicy(
        ontology_id=project.id, name="requester cannot read machine objects",
        subject_kind="user", subject_id=editor_user.id, effect="deny",
        scope_kind="object_type", scope_id="Machine", conditions_json=[],
        field_allowlist_json=[], created_by=admin_user.id,
    ))
    db.flush()

    def make_run(target_id, request_id):
        run = create_action_run_record(
            db=db,
            ontology_id=project.id,
            action_type_id=None,
            target_object_id=target_id,
            initial_status="running",
            requester_id=editor_user.id,
            reviewer_id=reviewer.id,
            executor_id=executor.id,
            client_request_id=request_id,
        )
        complete_action_run(
            db, run, before_snapshot={}, after_snapshot={}, side_effect_results=[],
            actor_id=executor.id, actor_kind="user",
        )
        return run

    visible_run = make_run(visible_to_requester.id, "audit-visible-target")
    hidden_run = make_run(hidden_for_requester.id, "audit-hidden-target")
    db.commit()

    def override_get_db():
        yield db

    def headers_for(principal):
        token = create_access_token({"sub": principal.id, "role": principal.role})
        return {"Authorization": f"Bearer {token}"}

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        run_list_url = f"/api/v2/ontologies/{project.id}/action-runs"
        public_event_url = f"/api/v2/ontologies/{project.id}/action-runs/{visible_run.id}/events"
        hidden_event_url = f"/api/v2/ontologies/{project.id}/action-runs/{hidden_run.id}/events"
        for participant in (editor_user, reviewer, executor, admin_user):
            headers = headers_for(participant)
            visible_list = client.get(
                run_list_url, params={"target_object_id": visible_to_requester.id}, headers=headers,
            )
            assert visible_list.status_code == 200
            assert [item["id"] for item in visible_list.json()] == [visible_run.id]
            history = client.get(public_event_url, headers=headers)
            assert history.status_code == 200
            assert history.json()["history_available"] is True

            hidden_list = client.get(
                run_list_url, params={"target_object_id": hidden_for_requester.id}, headers=headers,
            )
            hidden_history = client.get(hidden_event_url, headers=headers)
            if participant.id == editor_user.id:
                assert hidden_list.status_code == 200
                assert hidden_list.json() == []
                assert hidden_history.status_code == 404
            else:
                assert hidden_list.status_code == 200
                assert [item["id"] for item in hidden_list.json()] == [hidden_run.id]
                assert hidden_history.status_code == 200

        observer_headers = headers_for(observer)
        unlinked_list = client.get(
            run_list_url, params={"target_object_id": visible_to_requester.id}, headers=observer_headers,
        )
        assert unlinked_list.status_code == 200
        assert unlinked_list.json() == []
        unlinked_history = client.get(public_event_url, headers=observer_headers)
        assert unlinked_history.status_code == 404
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
