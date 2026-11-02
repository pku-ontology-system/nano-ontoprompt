from types import SimpleNamespace

import pytest

from app.models.entity import Entity
from app.models.ontology import OntologyProject
from app.models.relation import Relation
from app.models.v2.security import OntologySecurityPolicy
from app.models.v2.query_view import QueryDataView
from app.models.v2.scenario import ScenarioChangeSet, ScenarioResource
from app.schemas.v2.scenario import Edit, LinkTarget, RevisionRequest, Target
from app.services.v2.action_authorization import (
    authorize_action_snapshot,
    authorize_compiled_edits,
)
from app.services.v2 import action_runtime
from app.services.v2.scenario_actions import compile_actions
from app.services.v2 import scenarios as scenarios_service
from app.services.v2.scenarios import ScenarioError, ScenarioService
from app.services.v2.object_query.core import GraphData


def _fixture(db, ontology, admin_user, editor_user, *, allow_link=False):
    project = db.get(OntologyProject, ontology["id"])
    project.created_by = admin_user.id
    db.add_all([
        Entity(id="machine", ontology_id=project.id, name_cn="设备", name_en="Machine", type="entity"),
        Entity(id="supplier", ontology_id=project.id, name_cn="供应商", name_en="Supplier", type="entity"),
        Relation(id="machine-supplier", ontology_id=project.id, source_entity="machine",
                 target_entity="supplier", type="SUPPLIED_BY"),
        OntologySecurityPolicy(
            ontology_id=project.id, name="editor may update status", subject_kind="user",
            subject_id=editor_user.id, effect="allow", scope_kind="object_type",
            scope_id="machine", conditions_json=[], field_allowlist_json=["status"],
            created_by=admin_user.id,
        ),
        OntologySecurityPolicy(
            ontology_id=project.id, name="editor may read supplier", subject_kind="user",
            subject_id=editor_user.id, effect="allow", scope_kind="object_type",
            scope_id="supplier", conditions_json=[], field_allowlist_json=[],
            created_by=admin_user.id,
        ),
    ])
    if allow_link:
        db.add(OntologySecurityPolicy(
            ontology_id=project.id, name="editor may traverse supplier links", subject_kind="user",
            subject_id=editor_user.id, effect="allow", scope_kind="link",
            scope_id="SUPPLIED_BY", conditions_json=[], field_allowlist_json=[],
            created_by=admin_user.id,
        ))
    db.commit()
    snapshot = GraphData(
        objects={
            ("Machine", "machine-1"): {"_type": "Machine", "_instance_id": "machine-1",
                                        "status": "ready", "secret": "internal"},
            ("Supplier", "supplier-1"): {"_type": "Supplier", "_instance_id": "supplier-1",
                                          "name": "Supplier A"},
        },
        edges=[(("Machine", "machine-1"), "SUPPLIED_BY", ("Supplier", "supplier-1"))],
    )
    return project, snapshot


def test_action_snapshot_redacts_fields_and_denies_unlisted_links(db, ontology, admin_user, editor_user):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user)

    authorized, permission_digest = authorize_action_snapshot(db, project.id, editor_user, snapshot)

    assert authorized.objects[("Machine", "machine-1")]["status"] == "ready"
    assert authorized.objects[("Machine", "machine-1")]["secret"] is None
    assert authorized.objects[("Supplier", "supplier-1")]["name"] == "Supplier A"
    assert authorized.edges == []
    assert len(permission_digest) == 64


def test_function_backed_action_only_receives_redacted_snapshot(db, ontology, admin_user, editor_user):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user)
    authorized, _ = authorize_action_snapshot(db, project.id, editor_user, snapshot)
    observed = {}
    action = SimpleNamespace(
        id="status-action", version=1, enabled=True, status="published", side_effects=None,
        backed_by_function=True, effects=[], parameters=[], submission_criteria=[],
        target_entity_type="Machine", permission_rules=[],
    )

    def resolve_function(_action, _invocation, context):
        observed["secret"] = context.objects[("Machine", "machine-1")]["secret"]
        return (
            [{"name": "target", "type": "object", "required": True}],
            {"target": {"object_type": "Machine", "object_id": "machine-1"}},
            [{"op": "set_property", "target_parameter": "target", "property": "status",
              "static_value": "maintenance"}],
            {},
            {"function_id": "test-function", "version": 1},
        )

    edits = compile_actions(
        project.id, "scenario-test", [{"action_type_id": action.id, "parameters": {}}],
        {action.id: action}, authorized, actor=editor_user, function_resolver=resolve_function,
    )

    assert observed["secret"] is None
    status_edit = next(edit for edit in edits if edit.op == "set_property")
    assert status_edit.value == "maintenance"


def test_compiled_property_write_rejects_hidden_field(db, ontology, admin_user, editor_user):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user)
    edit = Edit(
        sequence=0, op="set_property",
        target=Target(ontology_id=project.id, concrete_type="Machine", object_id="machine-1"),
        property="secret", value="exfiltrated",
    )

    with pytest.raises(ScenarioError) as exc_info:
        authorize_compiled_edits(db, project.id, editor_user, snapshot, [edit])

    assert exc_info.value.code == "forbidden_field"


def test_compiled_link_write_requires_explicit_link_access(db, ontology, admin_user, editor_user):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user)
    edit = Edit(
        sequence=0, op="add_link",
        link=LinkTarget(
            ontology_id=project.id,
            relation_type="SUPPLIED_BY",
            source=Target(ontology_id=project.id, concrete_type="Machine", object_id="machine-1"),
            target=Target(ontology_id=project.id, concrete_type="Supplier", object_id="supplier-1"),
        ),
    )

    with pytest.raises(ScenarioError) as exc_info:
        authorize_compiled_edits(db, project.id, editor_user, snapshot, [edit])

    assert exc_info.value.code == "forbidden_link"


def test_explicit_link_policy_allows_compiled_link_write(db, ontology, admin_user, editor_user):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user, allow_link=True)
    edit = Edit(
        sequence=0, op="add_link",
        link=LinkTarget(
            ontology_id=project.id,
            relation_type="SUPPLIED_BY",
            source=Target(ontology_id=project.id, concrete_type="Machine", object_id="machine-1"),
            target=Target(ontology_id=project.id, concrete_type="Supplier", object_id="supplier-1"),
        ),
    )

    assert len(authorize_compiled_edits(db, project.id, editor_user, snapshot, [edit])) == 64


def test_action_snapshot_resolves_duplicate_relation_labels_by_endpoint_types(
    db, ontology, admin_user, editor_user,
):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user, allow_link=True)
    alternate = Entity(
        id="alternate-machine", ontology_id=project.id, name_cn="备用设备",
        name_en="AlternateMachine", type="entity",
    )
    db.add_all([
        alternate,
        Relation(
            id="alternate-machine-supplier", ontology_id=project.id,
            source_entity=alternate.id, target_entity="supplier", type="SUPPLIED_BY",
        ),
        OntologySecurityPolicy(
            ontology_id=project.id, name="editor may read alternate machine",
            subject_kind="user", subject_id=editor_user.id, effect="allow",
            scope_kind="object_type", scope_id=alternate.id,
            conditions_json=[], field_allowlist_json=[], created_by=admin_user.id,
        ),
    ])
    snapshot.objects[("AlternateMachine", "machine-2")] = {
        "_type": "AlternateMachine", "_instance_id": "machine-2", "status": "ready",
    }
    snapshot.edges.append((
        ("AlternateMachine", "machine-2"), "SUPPLIED_BY", ("Supplier", "supplier-1"),
    ))
    db.commit()

    authorized, _ = authorize_action_snapshot(db, project.id, editor_user, snapshot)

    assert authorized.edges == snapshot.edges


def test_action_snapshot_rejects_duplicate_relation_label_with_same_endpoints(
    db, ontology, admin_user, editor_user,
):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user, allow_link=True)
    db.add(Relation(
        id="duplicate-machine-supplier", ontology_id=project.id,
        source_entity="machine", target_entity="supplier", type="SUPPLIED_BY",
    ))
    db.commit()

    with pytest.raises(ScenarioError) as exc_info:
        authorize_action_snapshot(db, project.id, editor_user, snapshot)

    assert exc_info.value.code == "ambiguous_link_type"


def test_shared_action_runtime_projects_before_compile_and_rechecks_edits(
    db, ontology, admin_user, editor_user,
):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user)
    observed = {}

    def compiler(projected):
        observed["secret"] = projected.objects[("Machine", "machine-1")]["secret"]
        observed["edges"] = projected.edges
        return [Edit(
            sequence=0, op="set_property",
            target=Target(ontology_id=project.id, concrete_type="Machine", object_id="machine-1"),
            property="status", value="maintenance",
        )]

    edits, permission_digest = action_runtime.compile_with_authorization(
        db, project.id, editor_user, snapshot, compiler,
    )

    assert observed == {"secret": None, "edges": []}
    assert edits[0].value == "maintenance"
    assert len(permission_digest) == 64


def test_shared_action_runtime_rejects_compiled_hidden_field_write(
    db, ontology, admin_user, editor_user,
):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user)
    hidden_write = Edit(
        sequence=0, op="set_property",
        target=Target(ontology_id=project.id, concrete_type="Machine", object_id="machine-1"),
        property="secret", value="exfiltrated",
    )

    with pytest.raises(ScenarioError) as exc_info:
        action_runtime.compile_with_authorization(
            db, project.id, editor_user, snapshot, lambda _projected: [hidden_write],
        )

    assert exc_info.value.code == "forbidden_field"


def test_shared_action_runtime_rejects_permission_change_during_compile(
    db, ontology, admin_user, editor_user, monkeypatch,
):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user)
    monkeypatch.setattr(
        action_runtime, "authorize_compiled_edits", lambda *_args, **_kwargs: "f" * 64,
    )

    with pytest.raises(ScenarioError) as exc_info:
        action_runtime.compile_with_authorization(
            db, project.id, editor_user, snapshot, lambda _projected: [],
        )

    assert exc_info.value.code == "permission_changed"


def test_direct_scenario_revision_rejects_hidden_field_write(db, ontology, admin_user, editor_user, monkeypatch):
    project, snapshot = _fixture(db, ontology, admin_user, editor_user)
    view = QueryDataView(
        id="action-auth-view", ontology_id=project.id, source_manifest_digest="s" * 64,
        metadata_digest="m" * 64, graph_key="action-auth-graph", status="ready",
        created_by=admin_user.id,
    )
    scenario = ScenarioResource(
        id="action-auth-scenario", ontology_id=project.id, owner_id=editor_user.id,
        name="Authorization boundary", base_view_id=view.id,
    )
    db.add_all([view, scenario])
    db.commit()

    monkeypatch.setattr(scenarios_service, "require_ready", lambda *_args, **_kwargs: view)

    class SnapshotReader:
        def __init__(self, *_args, **_kwargs):
            pass

        def read(self, _ontology_id):
            return snapshot

    monkeypatch.setattr(scenarios_service, "FalkorReadAdapter", SnapshotReader)
    service = ScenarioService(
        db, project.id, editor_user, graph=SimpleNamespace(_graph=lambda _key: object())
    )
    request = RevisionRequest(
        base_revision=0, expected_etag=scenario.etag, client_request_id="direct-hidden-write",
        edits=[Edit(
            sequence=0, op="set_property",
            target=Target(ontology_id=project.id, concrete_type="Machine", object_id="machine-1"),
            property="secret", value="exfiltrated",
        )],
    )

    with pytest.raises(ScenarioError) as exc_info:
        service.revision(scenario, request)

    assert exc_info.value.code == "forbidden_field"
    assert scenario.head_revision == 0
    assert db.query(ScenarioChangeSet).filter_by(scenario_id=scenario.id).count() == 0
