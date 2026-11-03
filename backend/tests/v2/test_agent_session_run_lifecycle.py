from datetime import datetime, timedelta, timezone
import uuid
from sqlalchemy.orm import sessionmaker
import pytest

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.models.v2.agent import AgentEvent, AgentRun, AgentSession
from app.models.v2.action import OntologyActionRun, OntologyActionRunEvent, OntologyActionType
from app.models.v2.decision_case import DecisionCase, DecisionCaseEvent
from app.models.v2.query_view import QueryDataView
from app.models.v2.scenario import ScenarioGrant, ScenarioResource
from app.models.v2.security import OntologySecurityPolicy
from app.services.v2.agent_sessions import AgentSessionService, SessionError
from app.services.v2.decision_cases import DecisionCaseService


def _service(db, ontology, admin_user):
    service = AgentSessionService(db, ontology["id"], admin_user)
    service._projection_digest = lambda: "permission-digest"
    return service


def test_ontology_authorized_editor_can_create_private_agent_session(db, ontology, editor_user):
    from app.models.ontology import OntologyProject

    project = db.get(OntologyProject, ontology["id"])
    db.add(OntologySecurityPolicy(
        ontology_id=project.id, name="Editor may use ontology tools",
        subject_kind="user", subject_id=editor_user.id, effect="allow",
        scope_kind="ontology", scope_id=None, conditions_json=[],
        field_allowlist_json=[], created_by=project.created_by,
    ))
    db.commit()

    service = AgentSessionService(db, ontology["id"], editor_user)
    session = service.create("编辑者私有会话")

    assert session.owner_id == editor_user.id
    assert service.get(session.id)["owner_id"] == editor_user.id


def test_agent_session_requires_shared_ontology_policy(db, ontology, editor_user):
    with pytest.raises(SessionError) as exc_info:
        AgentSessionService(db, ontology["id"], editor_user)

    assert exc_info.value.code == "not_found"


def test_agent_session_api_accepts_editor_with_ontology_policy(
    client, db, ontology, admin_user, editor_user,
):
    from app.models.ontology import OntologyProject

    project = db.get(OntologyProject, ontology["id"])
    db.add(OntologySecurityPolicy(
        ontology_id=project.id, name="Editor may use ontology tools",
        subject_kind="user", subject_id=editor_user.id, effect="allow",
        scope_kind="ontology", scope_id=None, conditions_json=[],
        field_allowlist_json=[], created_by=admin_user.id,
    ))
    db.commit()
    token_response = client.post("/api/v1/auth/login", json={
        "username": editor_user.username, "password": "editor123",
    })
    assert token_response.status_code == 200, token_response.text

    response = client.post(
        f"/api/v2/ontologies/{project.id}/agent/sessions",
        headers={"Authorization": f"Bearer {token_response.json()['data']['access_token']}"},
        json={"title": "HTTP 授权会话"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["owner_id"] == editor_user.id


def test_agent_turn_http_persists_and_returns_accepted_before_worker_completion(
    client, auth_headers, ontology, monkeypatch,
):
    monkeypatch.setattr(AgentSessionService, "_dispatch_turn", lambda _service, _run_id: True)
    created = client.post(f"/api/v2/ontologies/{ontology['id']}/agent/sessions",
        headers=auth_headers, json={"title": "后台轮次 HTTP 验收"})
    assert created.status_code == 201, created.text
    session = created.json()

    response = client.post(
        f"/api/v2/ontologies/{ontology['id']}/agent/sessions/{session['id']}:turn",
        headers=auth_headers,
        json={"expected_version": session["version"], "task": "汇总当前上下文",
              "client_request_id": "http-queued-turn", "mode": "deterministic"},
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    assert response.json()["dispatch_pending"] is False


def test_celery_task_executes_persisted_turn_against_worker_session_factory(
    db, ontology, admin_user, monkeypatch,
):
    service = AgentSessionService(db, ontology["id"], admin_user)
    session = service.create("Celery worker 运行")
    monkeypatch.setattr(service, "_dispatch_turn", lambda _run_id: True)
    queued = service.turn(session.id, session.version, "汇总当前上下文", "celery-run-1",
                          "deterministic", enqueue=True)
    worker_factory = sessionmaker(bind=db.get_bind())
    import app.database
    monkeypatch.setattr(app.database, "SessionLocal", worker_factory)

    from app.tasks.v2.agent_sessions import execute_agent_session_turn
    result = execute_agent_session_turn.apply(
        args=[queued["run_id"]], task_id="isolated-agent-worker-task").get()

    db.expire_all()
    assert result["status"] == "completed"
    assert db.query(AgentRun).filter_by(id=queued["run_id"]).one().status == "completed"
    assert db.query(AgentSession).filter_by(id=session.id).one().status == "idle"


def test_agent_context_uses_shared_scenario_access_rules(db, ontology, admin_user, editor_user):
    from app.models.ontology import OntologyProject

    project = db.get(OntologyProject, ontology["id"])
    view = QueryDataView(
        id="agent-scenario-view", ontology_id=project.id, source_manifest_digest="s" * 64,
        metadata_digest="m" * 64, graph_key="agent-scenario-graph", status="ready",
        created_by=admin_user.id,
    )
    scenario = ScenarioResource(
        id="agent-shared-scenario", ontology_id=project.id, owner_id=admin_user.id,
        name="Shared Scenario", base_view_id=view.id,
    )
    db.add_all([view, scenario,
        OntologySecurityPolicy(
            ontology_id=project.id, name="Editor may read ontology",
            subject_kind="user", subject_id=editor_user.id, effect="allow",
            scope_kind="ontology", scope_id=None, conditions_json=[],
            field_allowlist_json=[], created_by=admin_user.id,
        ),
        ScenarioGrant(scenario_id=scenario.id, principal_id=editor_user.id, role="viewer"),
    ])
    db.commit()

    service = AgentSessionService(db, project.id, editor_user)
    resolved = service._resolve_ref("scenario", scenario.id)

    assert resolved["source_version"] == "0"
    assert resolved["status"] == "active"


def test_turn_persists_running_state_before_inference_and_finalizes_after_revalidation(
    db, ontology, admin_user,
):
    service = _service(db, ontology, admin_user)
    session = service.create("生命周期测试")
    refs = [{"kind": "object", "ref_id": "machine-1", "source_version": "v3"}]
    objects = [{"id": "machine-1", "object_type": "Machine", "properties": {"status": "warning"}}]
    service._grounded_context = lambda _session: (refs, objects)
    observed = {}

    def run_local_model(_task, _revision, _refs, _objects, *_pinned_model, **_options):
        observer_factory = sessionmaker(bind=db.get_bind())
        observer = observer_factory()
        try:
            persisted_session = observer.query(AgentSession).filter_by(id=session.id).one()
            persisted_run = observer.query(AgentRun).filter_by(session_id=session.id).one()
            observed["session_status"] = persisted_session.status
            observed["run_status"] = persisted_run.status
        finally:
            observer.close()
        return "设备需要人工复核。", refs, "qwen3.5:0.8b", "缺少维护记录。"

    service._local_model_answer = run_local_model
    result = service.turn(session.id, session.version, "需要检查设备吗？", "request-1", "local")

    assert observed == {"session_status": "running", "run_status": "running"}
    assert result["status"] == "completed"
    persisted_session = db.query(AgentSession).filter_by(id=session.id).one()
    persisted_run = db.query(AgentRun).filter_by(session_id=session.id).one()
    assert persisted_session.status == "idle"
    assert persisted_run.status == "completed"
    assert persisted_run.payload["permission_digest"] == "permission-digest"
    assert result["response"]["answer"] == "设备需要人工复核。"
    assert [event.event_type for event in db.query(AgentEvent).filter_by(
        session_id=session.id).order_by(AgentEvent.sequence).all()] == [
            "session_created", "user_input", "turn_started", "tool_call", "tool_result", "answer",
        ]


def test_queued_turn_is_durable_and_worker_finalizes_it_once(db, ontology, admin_user):
    service = _service(db, ontology, admin_user)
    session = service.create("后台运行")
    refs = [{"kind": "object", "ref_id": "machine-1", "source_version": "v3"}]
    objects = [{"id": "machine-1", "object_type": "Machine", "properties": {"status": "warning"}}]
    service._grounded_context = lambda _session: (refs, objects)
    dispatched = []
    service._dispatch_turn = lambda run_id: dispatched.append(run_id) or True

    result = service.turn(session.id, session.version, "分析设备", "queued-1", enqueue=True)

    run = db.query(AgentRun).filter_by(session_id=session.id).one()
    assert result["status"] == "queued"
    assert result["dispatch_pending"] is False
    assert dispatched == [run.id]
    assert db.query(AgentSession).filter_by(id=session.id).one().status == "queued"
    assert run.status == "queued"

    completed = service.execute_queued_turn(run.id, "worker-task-1")

    assert completed["status"] == "completed"
    assert db.query(AgentSession).filter_by(id=session.id).one().status == "idle"
    assert db.query(AgentRun).filter_by(id=run.id).one().status == "completed"
    assert [event.event_type for event in db.query(AgentEvent).filter_by(
        session_id=session.id).order_by(AgentEvent.sequence).all()] == [
            "session_created", "user_input", "turn_queued", "turn_started",
            "tool_call", "tool_result", "answer",
        ]
    assert service.execute_queued_turn(run.id, "duplicate-task") is None


def test_resume_leaves_recent_run_active(db, ontology, admin_user):
    service = _service(db, ontology, admin_user)
    session = service.create("近期运行")
    session.status = "running"
    session.version += 1
    run = AgentRun(ontology_id=ontology["id"], created_by=admin_user.id,
        session_id=session.id, client_request_id="turn:recent", input_digest="input",
        trace_id="trace-recent", status="running", payload={"mode": "local"},
        created_at=datetime.now(timezone.utc))
    db.add(run)
    db.commit()

    result = service.resume(session.id, session.version)

    assert result["waiting"] is True
    assert result["running"] is True
    assert db.query(AgentSession).filter_by(id=session.id).one().status == "running"
    assert db.query(AgentRun).filter_by(id=run.id).one().status == "running"


def test_resume_marks_expired_run_interrupted_without_replaying_it(db, ontology, admin_user):
    service = _service(db, ontology, admin_user)
    session = service.create("过期运行")
    session.status = "running"
    session.version += 1
    run = AgentRun(ontology_id=ontology["id"], created_by=admin_user.id,
        session_id=session.id, client_request_id="turn:expired", input_digest="input",
        trace_id="trace-expired", status="running", payload={"mode": "local"},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    db.add(run)
    db.commit()

    result = service.resume(session.id, session.version)

    assert result["interrupted"] is True
    assert db.query(AgentSession).filter_by(id=session.id).one().status == "idle"
    assert db.query(AgentRun).filter_by(id=run.id).one().status == "interrupted"
    interruption = db.query(AgentEvent).filter_by(session_id=session.id,
        event_type="turn_interrupted").one()
    assert run.id in interruption.payload["run_ids"]


def test_model_failure_is_persisted_and_releases_session(db, ontology, admin_user):
    service = _service(db, ontology, admin_user)
    session = service.create("失败恢复")
    service._grounded_context = lambda _session: ([], [])

    def fail_local_model(*_args):
        raise SessionError("local_model_unavailable", "本机模型不可用", 503)

    service._local_model_answer = fail_local_model
    with pytest.raises(SessionError, match="本机模型不可用"):
        service.turn(session.id, session.version, "分析一下", "request-failure", "local")

    assert db.query(AgentSession).filter_by(id=session.id).one().status == "idle"
    run = db.query(AgentRun).filter_by(session_id=session.id).one()
    assert run.status == "failed"
    assert run.payload["error"]["code"] == "local_model_unavailable"
    assert db.query(AgentEvent).filter_by(session_id=session.id, event_type="error").count() == 1
    assert db.query(AgentEvent).filter_by(session_id=session.id, event_type="answer").count() == 0


def test_turn_discards_model_answer_when_authorized_context_changes(db, ontology, admin_user):
    service = _service(db, ontology, admin_user)
    session = service.create("上下文变化")
    refs = [{"kind": "object", "ref_id": "machine-1", "source_version": "v3"}]
    initial_objects = [{"id": "machine-1", "object_type": "Machine", "properties": {"status": "warning"}}]
    changed_objects = [{"id": "machine-1", "object_type": "Machine", "properties": {"status": "offline"}}]
    contexts = iter([(refs, initial_objects), (refs, changed_objects)])
    service._grounded_context = lambda _session: next(contexts)
    service._local_model_answer = lambda *_args: ("设备正常。", refs, "qwen3.5:0.8b", "")

    with pytest.raises(SessionError) as exc_info:
        service.turn(session.id, session.version, "设备状态如何？", "request-stale", "local")

    assert exc_info.value.code == "context_changed_during_run"
    assert db.query(AgentSession).filter_by(id=session.id).one().status == "idle"
    run = db.query(AgentRun).filter_by(session_id=session.id).one()
    assert run.status == "failed"
    assert db.query(AgentEvent).filter_by(session_id=session.id, event_type="answer").count() == 0
    error_event = db.query(AgentEvent).filter_by(session_id=session.id, event_type="error").one()
    assert error_event.payload["code"] == "context_changed_during_run"


def test_agent_session_action_proposal_executes_through_reviewed_action_runtime(
    db, admin_user, editor_user, monkeypatch,
):
    project = OntologyProject(
        id=str(uuid.uuid4()), name="Agent Action Runtime integration", domain="test",
        created_by=admin_user.id,
    )
    machine_type = Entity(
        id=str(uuid.uuid4()), ontology_id=project.id, name_cn="设备", name_en="Machine",
        type="entity", properties={"status": "warning"},
    )
    machine = EntityInstance(
        id=str(uuid.uuid4()), ontology_id=project.id, entity_id=machine_type.id,
        row_identity="agent-action-machine-1", row_data={"status": "warning"},
        source_version="machine-source-v1",
    )
    action = OntologyActionType(
        id=str(uuid.uuid4()), ontology_id=project.id, name="批准设备状态",
        action_category="state_transition", target_entity_type="Machine",
        parameters=[{"name": "status", "type": "string", "required": True}],
        effects=[{"action": "set_property", "property": "status"}],
        status="published", enabled=True, created_by=admin_user.id,
    )
    db.add_all([project, machine_type, machine, action,
        OntologySecurityPolicy(
            ontology_id=project.id, name="editor can use scoped Agent and status",
            subject_kind="user", subject_id=editor_user.id, effect="allow",
            scope_kind="ontology", scope_id=None, conditions_json=[],
            field_allowlist_json=["status"], created_by=admin_user.id,
        ),
    ])
    db.commit()

    service = AgentSessionService(db, project.id, editor_user)
    session = service.create("状态审批会话")
    with_context = service.add_context(session.id, session.version, "object", machine.id)
    proposal = service.propose_action(
        session.id, with_context["version"], action_type_id=action.id,
        target_object_id=machine.id, parameters={"status": "approved"},
        evidence_refs=[], title="批准设备恢复", reason="现场复核完成",
        client_request_id="agent-runtime-integration-1",
    )
    case_id = proposal["case_id"]
    assert proposal["case_status"] == "draft"
    assert proposal["session"]["status"] == "waiting_for_review"

    requester_cases = DecisionCaseService(db, project.id, editor_user)
    preview = requester_cases.preview(case_id)
    assert preview["preview"]["diff"] == [
        {"op": "set_property", "property": "status", "before": "warning", "after": "approved"},
    ]
    requester_cases.submit(case_id, preview["preview_digest"])
    reviewer_cases = DecisionCaseService(db, project.id, admin_user)
    reviewer_cases.review(case_id, "approved", "复核通过")
    monkeypatch.setattr(DecisionCaseService, "sync_projection", lambda *_args, **_kwargs: None)
    completed_case, action_run = reviewer_cases.execute(case_id)

    db.refresh(machine)
    assert completed_case.status == "completed"
    assert completed_case.created_by == editor_user.id
    assert completed_case.reviewed_by == admin_user.id
    assert completed_case.action_run_id == action_run.id
    assert action_run.status == "completed"
    assert action_run.requested_by == editor_user.id
    assert action_run.reviewed_by == admin_user.id
    assert action_run.executed_by == admin_user.id
    assert action_run.decision_case_id == case_id
    assert machine.row_data["status"] == "approved"

    case_events = db.query(DecisionCaseEvent).filter_by(case_id=case_id).order_by(
        DecisionCaseEvent.sequence).all()
    assert [event.event_type for event in case_events] == [
        "created", "submitted", "reviewed", "executed",
    ]
    action_events = db.query(OntologyActionRunEvent).filter_by(
        action_run_id=action_run.id).order_by(OntologyActionRunEvent.sequence).all()
    assert [event.to_status for event in action_events] == ["running", "completed"]
    assert [event.actor_id for event in action_events] == [editor_user.id, admin_user.id]
    assert db.query(OntologyActionRun).filter_by(id=action_run.id).one().status == "completed"

    resumed = service.resume(session.id, proposal["session"]["version"])
    db.refresh(session)
    persisted_agent_run = db.query(AgentRun).filter_by(session_id=session.id).one()
    assert resumed["waiting"] is False
    assert resumed["case_status"] == "completed"
    assert session.status == "idle"
    assert session.pending_case_id is None
    assert persisted_agent_run.status == "completed"
    assert [event.event_type for event in db.query(AgentEvent).filter_by(
        session_id=session.id).order_by(AgentEvent.sequence).all()] == [
            "session_created", "context_added", "tool_call", "approval_wait", "resumed",
        ]
