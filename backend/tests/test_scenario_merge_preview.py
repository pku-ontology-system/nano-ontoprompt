from types import SimpleNamespace

from app.models.v2.action import OntologyActionRun
from app.models.v2.scenario import ScenarioAudit, ScenarioChangeSet, ScenarioResource, ScenarioRevision
from app.models.entity_instance import EntityInstance
from app.routers.v2 import scenario_workbench as workbench


class Query:
    def __init__(self, row):
        self.row = row

    def filter_by(self, **_kwargs):
        return self

    def order_by(self, *_args):
        return self

    def first(self):
        return self.row

    def all(self):
        return [] if self.row is None else [self.row]


class Database:
    def __init__(self, revision, changeset):
        self.revision = revision
        self.changeset = changeset

    def query(self, model):
        return Query(self.revision if model is ScenarioRevision else
                     SimpleNamespace(id="T1") if model is EntityInstance else None)

    def get(self, model, _id):
        return self.changeset if model is ScenarioChangeSet else None


def test_merge_preview_checks_live_values_and_sequential_action_edits(monkeypatch):
    edits = [
        {"sequence": 0, "op": "set_property", "target": {"ontology_id": "ontology", "concrete_type": "Ticket", "object_id": "T1"},
         "property": "priority", "expected_old_value": "P2", "value": "P1"},
        {"sequence": 1, "op": "set_property", "target": {"ontology_id": "ontology", "concrete_type": "Ticket", "object_id": "T1"},
         "property": "priority", "expected_old_value": "P1", "value": "P0"},
    ]
    revision = SimpleNamespace(revision=1, changeset_id="change", content_digest="revision-digest")
    changeset = SimpleNamespace(ordered_edits=edits, validation_report={
        "replace_all": True, "permission_digest": "p" * 64, "preview_digest": "a" * 64})
    db = Database(revision, changeset)
    scenario = SimpleNamespace(id="scenario", ontology_id="ontology", base_view_id="base",
                               head_revision=1, etag=2)
    service = SimpleNamespace(db=db, ontology_id="ontology",
                              graph=SimpleNamespace(_graph=lambda _id: object()),
                              user=SimpleNamespace(id="owner", role="admin"),
                              _revision_preview_changes=lambda *_args, **_kwargs: [])
    monkeypatch.setattr(workbench, "authorize_compiled_edits", lambda *_args, **_kwargs: "p" * 64)
    # The merge preview shows changes over the live data the user may read.
    monkeypatch.setattr(workbench, "authorize_action_snapshot", lambda _db, _ontology, _actor, snapshot: (snapshot, {}))
    monkeypatch.setattr(workbench, "_snapshot", lambda *_args: SimpleNamespace(
        objects={("Ticket", "T1"): {"priority": "P2"}}, edges=[]))

    live = SimpleNamespace(objects={("Ticket", "T1"): {"priority": "P2"}}, edges=[])
    monkeypatch.setattr(workbench, "FalkorReadAdapter", lambda _graph: SimpleNamespace(read=lambda _id: live))
    result, selected, preview_digest = workbench._merge_state(service, scenario)
    assert result["can_merge"] is True
    assert result["applied_edits"] == 2
    assert selected == edits
    assert len(preview_digest) == 64

    live.objects[("Ticket", "T1")]["priority"] = "P3"
    result, _, _ = workbench._merge_state(service, scenario)
    assert result["can_merge"] is False
    assert result["conflicts"][0]["reason"] == "Main Ontology value changed since this Scenario was created"


def test_merge_commit_uses_live_action_edit_adapter(monkeypatch):
    from app.schemas.v2.scenario import Edit
    from app.services.v2 import live_edits
    calls = []

    class Query:
        def __init__(self, row):
            self.row = row
        def filter_by(self, **_kwargs):
            return self
        def with_for_update(self):
            return self
        def populate_existing(self):
            return self
        def first(self):
            return self.row

    class Db:
        def __init__(self):
            self.audit = None
            self.scenario = SimpleNamespace(id="scenario", head_revision=1, protected_demo=False,
                                            base_view_id="base", etag=2)
            self.revision = SimpleNamespace(id="revision-1", revision=1, changeset_id="change",
                                            content_digest="content-digest")
            self.changeset = SimpleNamespace(id="change-1",
                                             validation_report={"permission_digest": "p" * 64})
        def query(self, model):
            if model is ScenarioResource:
                return Query(self.scenario)
            if model is ScenarioAudit:
                return Query(self.audit)
            if model is ScenarioRevision:
                return Query(self.revision)
            raise AssertionError(f"unexpected query model: {model}")
        def get(self, model, _id):
            if model is ScenarioChangeSet:
                return self.changeset
            if model is ScenarioAudit:
                return self.audit
            if model is OntologyActionRun:
                return self.action_run
            return None
        def add(self, value):
            if isinstance(value, ScenarioAudit):
                value.id = "merge-audit-1"
                self.audit = value
                calls.append(("audit", value.operation))
            else:  # the merge also records an ActionRun
                self.action_run = value
                calls.append(("action_run", value.id))
        def flush(self):
            pass
        def commit(self):
            calls.append(("commit",))
    db = Db()
    user = SimpleNamespace(id="owner")
    scenario = db.scenario
    service = SimpleNamespace(graph=object(), get=lambda *args, **kwargs: scenario)
    monkeypatch.setattr(workbench, "_service", lambda *args: service)
    raw = {"sequence": 0, "op": "set_property", "target": {"ontology_id": "ont", "concrete_type": "Ticket", "object_id": "T1"},
           "property": "priority", "value": "P1", "expected_old_value": "P2"}
    monkeypatch.setattr(workbench, "_merge_state",
                        lambda *args, **_kwargs: ({"can_merge": True}, [raw], "a" * 64))
    monkeypatch.setattr(workbench, "create_action_run_record", lambda **_kwargs: SimpleNamespace(id="action-run-1"))
    monkeypatch.setattr(workbench, "complete_action_run", lambda *_args, **_kwargs: calls.append(("complete_run",)))
    def apply(db_arg, ontology, edits, graph, *, actor, expected_permission_digest):
        assert db_arg is db and ontology == "ont" and graph is service.graph
        # The executor rechecks the permission digest the revision was reviewed under.
        assert actor is user and expected_permission_digest == "p" * 64
        assert len(edits) == 1 and isinstance(edits[0], Edit)
        calls.append(("apply", edits[0].value))
        return {}, {}, [], lambda: calls.append(("restore",))
    monkeypatch.setattr(live_edits, "apply_live_edits", apply)
    result = workbench.merge_scenario("ont", "scenario", workbench.MergeCommand(
        expected_revision=1, preview_digest="a" * 64,
        client_request_id="merge-test-001"), db, user)
    assert result["status"] == "merged"
    assert result["audit_id"] == "merge-audit-1"
    assert result["replayed"] is False
    assert db.audit.request_id == "merge-test-001"
    assert db.audit.summary["status"] == "completed"
    assert result["action_run_id"] == "action-run-1"
    assert calls == [("audit", "merge"), ("action_run", "action-run-1"), ("commit",),
                     ("apply", "P1"), ("complete_run",), ("commit",)]
