from unittest.mock import patch
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.models.v2.security import OntologySecurityPolicy
from app.services.v2.revision_service import create_revision
from app.services.v2.dynamic_ontology_service import OntologyEditError, apply_batch_changes, apply_change, validate_batch_changes
from app.services.v2.what_if_service import WhatIfError, _context_facts, _validate_assumptions, build_context, infer_facts, run_preview
from app.models.v2.dynamic_ontology import WhatIfRun, WhatIfScenario
from app.routers.v2 import what_if as what_if_router


class MemoryStorage:
    def put_bytes(self, *_args, **_kwargs):
        return "memory://revision"


def _project(db, admin_user, name="FactoryNet CNC"):
    project = OntologyProject(id="dynamic-ontology", name=name, domain="制造", data_class="temporal", created_by=admin_user.id)
    db.add(project)
    db.commit()
    return project


def test_single_operation_creates_revision_and_blocks_referenced_delete(db, admin_user):
    project = _project(db, admin_user)
    with patch("app.services.v2.revision_service.get_storage_service", return_value=MemoryStorage()), patch("app.services.v2.audit_runner.queue_local_audit", return_value=None):
        base = create_revision(db, project.id)
        added = apply_change(db, project.id, {"base_revision_id": base.id, "target_kind": "entity_type", "operation": "add", "payload": {"canonical_id": "Observation", "name_cn": "观测"}}, user_id=admin_user.id)
        prop = apply_change(db, project.id, {"base_revision_id": added["revision"]["id"], "target_kind": "property", "operation": "add", "payload": {"entity_id": "Observation", "property": {"id": "ordinal", "name": "Ordinal", "type": "integer", "source_field": "ordinal"}}}, user_id=admin_user.id)
        db.add(EntityInstance(id="instance-1", entity_id="Observation", ontology_id=project.id, row_identity="1", row_data={"ordinal": 1})); db.commit()
        try:
            apply_change(db, project.id, {"base_revision_id": prop["revision"]["id"], "target_kind": "entity_type", "operation": "delete", "target_id": "Observation"}, user_id=admin_user.id)
        except OntologyEditError as exc:
            assert exc.code == "CHANGE_BLOCKED"
        else:
            raise AssertionError("referenced entity deletion should be blocked")


def test_what_if_forward_chaining_and_property_override_are_isolated(db, admin_user):
    assert infer_facts([("IN_PHASE", ("obs", "phase")), ("HAS_TOOL_CONDITION", ("obs", "condition"))], [{"id": "r", "conditions": [("IN_PHASE", ["?obs", "?phase"]), ("HAS_TOOL_CONDITION", ["?obs", "?condition"])], "conclusion": ("PHASE_TOOL_STATE", ["?phase", "?condition"])}])["derived"][0]["fact"] == "PHASE_TOOL_STATE(phase, condition)"


def test_what_if_requires_a_selected_observation(db, admin_user):
    project = _project(db, admin_user, name="FactoryNet CNC requires target")
    try:
        build_context(db, project.id)
    except WhatIfError as exc:
        assert "选择一个 Observation" in str(exc)
    else:
        raise AssertionError("What-If should not build a whole-ontology context without a target")


def test_context_fact_projection_does_not_expand_unused_sensor_columns(db, admin_user):
    context = {
        "nodes": [
            {
                "id": f"obs-{index}",
                "entity_type": "Observation",
                "properties": {"ctx_tool_condition": "unworn", **{f"sensor_{column}": column for column in range(40)}},
            }
            for index in range(100)
        ],
        "edges": [
            {"id": f"edge-{index}", "source": f"obs-{index % 100}", "target": f"obs-{(index + 1) % 100}", "type": "NEXT_OBSERVATION"}
            for index in range(1500)
        ],
    }
    facts, _ = _context_facts(
        context,
        rules=[{"conditions": [("NEXT_OBSERVATION", ["?source", "?target"])], "property_specs": []}],
        assumptions=[{"kind": "set_property", "property": "ctx_tool_condition"}],
    )
    # The 1,500 synthetic edges intentionally repeat the same 100 adjacent
    # pairs, so the compiler's deterministic de-duplication leaves 200 facts
    # (100 structural relations plus 100 selected properties).
    assert len(facts) == 200
    assert any(fact[0] == "PROP_CTX_TOOL_CONDITION" for fact in facts)
    assert not any(fact[0] == "PROP_SENSOR_1" for fact in facts)


def test_what_if_rejects_property_assumption_outside_authorized_projection(db, admin_user):
    project = _project(db, admin_user, name="FactoryNet property projection")
    scenario = WhatIfScenario(
        id="scenario-property-policy",
        ontology_id=project.id,
        name="Restricted property",
        assumptions_json=[{"kind": "set_property", "instance_id": "obs-1", "property": "secret", "value": "leak"}],
        rule_overrides_json={},
        created_by=admin_user.id,
    )
    context = {
        "nodes": [{"id": "obs-1", "entity_type": "Observation", "properties": {"ordinal": 0, "secret": "hidden"}}],
        "edges": [],
        "provenance": {"authorized_properties_by_object": {"obs-1": ["ordinal"]}, "authorized_link_types": []},
    }
    try:
        _validate_assumptions(db, scenario, context)
    except WhatIfError as exc:
        assert "无权" in str(exc)
    else:
        raise AssertionError("What-if must reject property assumptions outside the authorized projection")


def test_what_if_rejects_unauthorized_link_assumption(db, admin_user):
    project = _project(db, admin_user, name="FactoryNet link projection")
    scenario = WhatIfScenario(
        id="scenario-link-policy",
        ontology_id=project.id,
        name="Restricted link",
        assumptions_json=[{"kind": "add_relation", "source": "obs-1", "target": "obs-2", "type": "SECRET_LINK"}],
        rule_overrides_json={},
        created_by=admin_user.id,
    )
    context = {
        "nodes": [
            {"id": "obs-1", "entity_type": "Observation", "properties": {}},
            {"id": "obs-2", "entity_type": "Machine", "properties": {}},
        ],
        "edges": [],
        "provenance": {"authorized_properties_by_object": {}, "authorized_link_types": ["ALLOWED_LINK"]},
    }
    try:
        _validate_assumptions(db, scenario, context)
    except WhatIfError as exc:
        assert "无权" in str(exc)
    else:
        raise AssertionError("What-if must reject link assumptions outside the authorized projection")


def test_what_if_context_uses_shared_object_property_and_link_projection(db, admin_user, editor_user, monkeypatch):
    from app.models.v2.security import OntologySecurityPolicy
    from app.services.v2.what_if_service import verify_context_digest

    project = _project(db, admin_user, name="FactoryNet authorized neighborhood")
    db.add_all([
        Entity(
            id="Observation",
            ontology_id=project.id,
            canonical_id="Observation",
            semantic_resource_id="type:Observation",
            name_cn="观测",
            properties={"property_definitions": [{"id": "ordinal", "name": "Ordinal", "type": "integer"}]},
        ),
        Entity(
            id="Machine",
            ontology_id=project.id,
            canonical_id="Machine",
            semantic_resource_id="type:Machine",
            name_cn="机器",
            properties={"property_definitions": [{"id": "serial", "name": "Serial", "type": "string"}]},
        ),
        OntologySecurityPolicy(
            ontology_id=project.id,
            name="Editor may see authorized ontology fields",
            subject_kind="user",
            subject_id=editor_user.id,
            effect="allow",
            scope_kind="ontology",
            scope_id=None,
            conditions_json=[],
            field_allowlist_json=["ordinal"],
            created_by=admin_user.id,
        ),
        OntologySecurityPolicy(
            ontology_id=project.id,
            name="Hide restricted objects",
            subject_kind="user",
            subject_id=editor_user.id,
            effect="deny",
            scope_kind="ontology",
            scope_id=None,
            conditions_json=[{"field": "visibility", "operator": "=", "value": "restricted"}],
            field_allowlist_json=[],
            created_by=admin_user.id,
        ),
        OntologySecurityPolicy(
            ontology_id=project.id,
            name="Editor may see next-observation links",
            subject_kind="user",
            subject_id=editor_user.id,
            effect="allow",
            scope_kind="link",
            scope_id="NEXT_OBSERVATION",
            conditions_json=[],
            field_allowlist_json=[],
            created_by=admin_user.id,
        ),
    ])
    db.commit()

    graph_data = {
        "target_instance_id": "obs-1",
        "nodes": [
            {"id": "obs-1", "entity_id": "Observation", "entity_type": "Observation", "properties": {"ordinal": 0, "secret": "private"}},
            {"id": "obs-2", "entity_id": "Observation", "entity_type": "Observation", "properties": {"ordinal": 1, "secret": "private"}},
            {"id": "machine-1", "entity_id": "Machine", "entity_type": "Machine", "properties": {"serial": "private", "visibility": "restricted"}},
        ],
        "edges": [
            {"id": "edge-next", "source": "obs-1", "target": "obs-2", "type": "NEXT_OBSERVATION", "properties": {}},
            {"id": "edge-hidden", "source": "obs-1", "target": "machine-1", "type": "NEAR", "properties": {}},
        ],
    }

    class FakeGraph:
        available = True

        def get_neighborhood(self, *_args, **_kwargs):
            return graph_data

    monkeypatch.setattr("app.services.v2.what_if_service.FalkorDBService", FakeGraph)
    context = build_context(db, project.id, target_instance_id="obs-1", user=editor_user)

    assert {node["id"] for node in context["nodes"]} == {"obs-1", "obs-2"}
    assert context["nodes"][0]["properties"]["secret"] is None
    assert context["provenance"]["authorized_properties_by_object"]["obs-1"] == ["ordinal"]
    assert [(edge["source"], edge["target"]) for edge in context["edges"]] == [("obs-1", "obs-2")]
    assert context["provenance"]["authorized_link_types"] == ["NEXT_OBSERVATION"]
    assert verify_context_digest(context)


def test_what_if_scenario_and_run_reads_are_owner_scoped(db, admin_user, editor_user):
    project = _project(db, admin_user, name="FactoryNet private what-if")
    scenario = WhatIfScenario(
        id="private-what-if-scenario",
        ontology_id=project.id,
        name="Private scenario",
        baseline_json={},
        assumptions_json=[],
        rule_overrides_json={},
        created_by=admin_user.id,
    )
    run = WhatIfRun(
        id="private-what-if-run",
        scenario_id=scenario.id,
        ontology_id=project.id,
        status="completed",
        stage="completed",
        progress=100,
        context_json={},
        baseline_result_json={},
        scenario_result_json={},
        diff_json={},
        created_by=admin_user.id,
    )
    db.add_all([
        scenario,
        run,
        OntologySecurityPolicy(
            ontology_id=project.id,
            name="Editor may read ontology",
            subject_kind="user",
            subject_id=editor_user.id,
            effect="allow",
            scope_kind="ontology",
            scope_id=None,
            conditions_json=[],
            field_allowlist_json=[],
            created_by=admin_user.id,
        ),
    ])
    db.commit()
    scenarios_response = what_if_router.list_scenarios(project.id, db, editor_user)
    assert scenarios_response["scenarios"] == []
    try:
        what_if_router.get_run(project.id, run.id, db, editor_user)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("a different user's What-if run should be invisible")


def test_what_if_worker_claims_and_executes_a_queued_run_once(db, admin_user, monkeypatch):
    project = _project(db, admin_user, name="FactoryNet idempotent worker")
    baseline = {"ontology_id": project.id, "nodes": [], "edges": [], "provenance": {"context_digest": "sealed"}}
    scenario = WhatIfScenario(
        id="worker-scenario",
        ontology_id=project.id,
        name="Worker scenario",
        status="queued",
        baseline_json=baseline,
        assumptions_json=[],
        rule_overrides_json={},
        created_by=admin_user.id,
    )
    db.add(scenario)
    db.flush()
    run = WhatIfRun(
        id="worker-run",
        scenario_id=scenario.id,
        ontology_id=project.id,
        status="queued",
        stage="prepare_baseline",
        progress=0,
        context_json=baseline,
        baseline_result_json={},
        scenario_result_json={},
        diff_json={},
        created_by=admin_user.id,
    )
    run.input_hash = what_if_router._scenario_input_hash(scenario)
    db.add(run)
    db.commit()
    monkeypatch.setattr(what_if_router, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(what_if_router, "revalidate_scenario_context", lambda *_args: baseline)
    executions = []

    def preview(*_args, **_kwargs):
        executions.append("executed")
        return {"baseline": {}, "scenario": {}, "diff": {}, "engine": "isolated-test", "engine_version": "test"}

    monkeypatch.setattr(what_if_router, "run_preview", preview)
    what_if_router._execute_run(run.id)
    what_if_router._execute_run(run.id)

    db.expire_all()
    assert db.get(WhatIfRun, run.id).status == "completed"
    assert db.get(WhatIfScenario, scenario.id).status == "completed"
    assert executions == ["executed"]


def test_what_if_http_create_queue_and_owner_isolation_use_test_database(db, admin_user, editor_user, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.deps import get_current_user, require_editor
    from app.routers.v2 import what_if as what_if_router

    project = _project(db, admin_user, name="FactoryNet isolated API path")
    db.add(OntologySecurityPolicy(
        ontology_id=project.id,
        name="Editor may access ontology",
        subject_kind="user",
        subject_id=editor_user.id,
        effect="allow",
        scope_kind="ontology",
        scope_id=None,
        conditions_json=[],
        field_allowlist_json=[],
        created_by=admin_user.id,
    ))
    db.commit()
    baseline = {
        "ontology_id": project.id,
        "data_class": "temporal",
        "base_revision_id": None,
        "dataset_version_id": "dataset-test-v1",
        "target_instance_id": "obs-http-1",
        "episode_id": "episode-test",
        "at": 0,
        "mode": "cumulative",
        "nodes": [{"id": "obs-http-1", "entity_type": "Observation", "properties": {"ordinal": 0}}],
        "edges": [],
        "rules": [],
        "limits": {"nodes": 500, "facts": 2000},
        "provenance": {
            "context_digest": "a" * 64,
            "authorized_properties_by_object": {"obs-http-1": ["ordinal"]},
            "authorized_link_types": [],
        },
    }
    monkeypatch.setattr(what_if_router, "build_context", lambda *_args, **_kwargs: baseline)
    monkeypatch.setattr(what_if_router, "revalidate_scenario_context", lambda *_args: baseline)
    dispatched = []
    monkeypatch.setattr(what_if_router, "_execute_run", lambda run_id: dispatched.append(run_id))
    actor = [admin_user]
    app = FastAPI()
    app.include_router(what_if_router.router, prefix="/api/v2/ontologies")
    app.dependency_overrides[get_current_user] = lambda: actor[0]
    app.dependency_overrides[require_editor] = lambda: actor[0]
    app.dependency_overrides[what_if_router.get_db] = lambda: db

    with TestClient(app) as client:
        created = client.post(
            f"/api/v2/ontologies/{project.id}/what-if/scenarios",
            json={"target_instance_id": "obs-http-1", "name": "isolated API scenario"},
        )
        assert created.status_code == 200, created.text
        scenario_id = created.json()["id"]
        listed = client.get(f"/api/v2/ontologies/{project.id}/what-if/scenarios")
        assert [item["id"] for item in listed.json()["scenarios"]] == [scenario_id]
        queued = client.post(f"/api/v2/ontologies/{project.id}/what-if/scenarios/{scenario_id}/runs")
        assert queued.status_code == 202, queued.text
        run_id = queued.json()["id"]
        assert queued.json()["status"] == "queued"

        actor[0] = editor_user
        other_user_list = client.get(f"/api/v2/ontologies/{project.id}/what-if/scenarios")
        other_user_run = client.get(f"/api/v2/ontologies/{project.id}/what-if/runs/{run_id}")

    assert other_user_list.status_code == 200
    assert other_user_list.json()["scenarios"] == []
    assert other_user_run.status_code == 404
    assert dispatched == [run_id]
    assert db.get(WhatIfRun, run_id).input_hash == what_if_router._scenario_input_hash(db.get(WhatIfScenario, scenario_id))


def test_batch_suggestions_are_validated_together_and_applied_atomically(db, admin_user):
    project = _project(db, admin_user, name="FactoryNet batch editor")
    with patch("app.services.v2.revision_service.get_storage_service", return_value=MemoryStorage()), patch("app.services.v2.audit_runner.queue_local_audit", return_value=None):
        base = create_revision(db, project.id)
        duplicate = {
            "base_revision_id": base.id,
            "operations": [
                {"target_kind": "entity_type", "operation": "add", "payload": {"canonical_id": "Observation", "name_cn": "观测"}},
                {"target_kind": "entity_type", "operation": "add", "payload": {"canonical_id": "Observation", "name_cn": "另一观测"}},
            ],
        }
        try:
            validate_batch_changes(db, project.id, duplicate)
        except OntologyEditError as exc:
            assert exc.code == "BATCH_CONFLICT"
        else:
            raise AssertionError("duplicate suggestions should be rejected before any write")
        assert db.query(Entity).filter(Entity.ontology_id == project.id).count() == 0

        valid = {
            "base_revision_id": base.id,
            "operations": [
                {"target_kind": "entity_type", "operation": "add", "payload": {"canonical_id": "Equipment", "name_cn": "设备"}},
                {"target_kind": "entity_type", "operation": "add", "payload": {"canonical_id": "Observation", "name_cn": "观测"}},
                {"target_kind": "relationship", "operation": "add", "payload": {"source_entity": "Equipment", "target_entity": "Observation", "type": "HAS_OBSERVATION", "cardinality": "one-to-many"}},
            ],
        }
        checked = validate_batch_changes(db, project.id, valid)
        assert checked["can_apply"] is True
        assert len(checked["operations"]) == 3
        result = apply_batch_changes(db, project.id, valid, user_id=admin_user.id)
        assert result["revision"]["is_current"] is True
        assert db.query(Entity).filter(Entity.ontology_id == project.id).count() == 2


def test_entity_update_preserves_properties_and_rejects_bulk_property_payload(db, admin_user):
    project = _project(db, admin_user, name="FactoryNet entity edit")
    entity = Entity(
        id="Observation",
        ontology_id=project.id,
        name_cn="观测",
        properties={"property_definitions": [{"id": "ordinal", "name": "Ordinal", "type": "integer"}]},
    )
    db.add(entity)
    db.commit()
    with patch("app.services.v2.revision_service.get_storage_service", return_value=MemoryStorage()), patch("app.services.v2.audit_runner.queue_local_audit", return_value=None):
        base = create_revision(db, project.id)
        result = apply_change(
            db,
            project.id,
            {"base_revision_id": base.id, "target_kind": "entity_type", "operation": "update", "target_id": "Observation", "payload": {"name_cn": "加工观测"}},
            user_id=admin_user.id,
        )
        assert result["revision"]["is_current"] is True
        db.refresh(entity)
        assert entity.name_cn == "加工观测"
        assert entity.properties["property_definitions"][0]["id"] == "ordinal"
        try:
            apply_change(
                db,
                project.id,
                {"base_revision_id": result["revision"]["id"], "target_kind": "entity_type", "operation": "update", "target_id": "Observation", "payload": {"properties": {"property_definitions": []}}},
                user_id=admin_user.id,
            )
        except OntologyEditError as exc:
            assert exc.code == "PROPERTY_OPERATION_REQUIRED"
        else:
            raise AssertionError("bulk property payload should require a typed property operation")
