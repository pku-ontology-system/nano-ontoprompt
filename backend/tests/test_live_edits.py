from types import SimpleNamespace

import pytest

from app.schemas.v2.scenario import Edit, Target
from app.services.v2 import live_edits
from app.services.v2.scenarios import ScenarioError

ACTOR = SimpleNamespace(id="user-1", role="editor")


@pytest.fixture(autouse=True)
def permission_checks(monkeypatch):
    """apply_live_edits rechecks the reviewed permission digest before any write.

    These tests cover graph and row mutation, so they record that check.
    """
    calls = []
    monkeypatch.setattr(live_edits, "reauthorize_edit_batch",
                        lambda db, **kwargs: calls.append(kwargs) or kwargs["expected_permission_digest"])
    return calls


class FakeGraph:
    def __init__(self):
        self.props = {"status": "pending"}

    def query(self, statement, params):
        if "SET n += $patch" in statement:
            self.props.update(params["patch"])


class FakeDb:
    def __init__(self):
        self.row = SimpleNamespace(row_data={"status": "pending"})

    def query(self, model):
        return self

    def filter_by(self, **kwargs):
        return self

    def with_for_update(self):
        return self

    def first(self):
        return self.row


def test_live_edit_updates_instance_and_graph_and_can_compensate(monkeypatch, permission_checks):
    graph = FakeGraph()
    service = SimpleNamespace(available=True, _graph=lambda ontology: graph)
    snapshot = SimpleNamespace(objects={("Order", "order-1"): {"status": "pending"}}, edges=[])
    monkeypatch.setattr(live_edits, "FalkorReadAdapter", lambda graph: SimpleNamespace(read=lambda ontology: snapshot))
    db = FakeDb()
    edit = Edit(sequence=0, op="set_property", target=Target(ontology_id="ont", concrete_type="Order", object_id="order-1"),
                property="status", value="approved", expected_old_value="pending")

    before, after, results, restore = live_edits.apply_live_edits(
        db, "ont", [edit], service, actor=ACTOR, expected_permission_digest="reviewed-digest")
    assert permission_checks[0]["actor"] is ACTOR
    assert permission_checks[0]["expected_permission_digest"] == "reviewed-digest"
    assert before["order-1"]["status"] == "pending"
    assert after["order-1"]["status"] == "approved"
    assert results[0]["op"] == "set_property"
    assert db.row.row_data["status"] == graph.props["status"] == "approved"
    restore()
    assert graph.props["status"] == "pending"


def test_live_edit_rejects_stale_value_before_mutating_graph(monkeypatch):
    graph = FakeGraph()
    service = SimpleNamespace(available=True, _graph=lambda ontology: graph)
    snapshot = SimpleNamespace(objects={("Order", "order-1"): {"status": "pending"}}, edges=[])
    monkeypatch.setattr(live_edits, "FalkorReadAdapter", lambda graph: SimpleNamespace(read=lambda ontology: snapshot))
    edit = Edit(sequence=0, op="set_property", target=Target(ontology_id="ont", concrete_type="Order", object_id="order-1"),
                property="status", value="approved", expected_old_value="reviewing")

    with pytest.raises(ScenarioError, match="changed"):
        live_edits.apply_live_edits(FakeDb(), "ont", [edit], service,
                                    actor=ACTOR, expected_permission_digest="reviewed-digest")
    assert graph.props["status"] == "pending"


def test_live_edit_batch_checks_each_step_against_projected_state(monkeypatch):
    graph = FakeGraph()
    service = SimpleNamespace(available=True, _graph=lambda ontology: graph)
    snapshot = SimpleNamespace(objects={("Order", "order-1"): {"status": "pending"}}, edges=[])
    monkeypatch.setattr(live_edits, "FalkorReadAdapter", lambda graph: SimpleNamespace(read=lambda ontology: snapshot))
    target = Target(ontology_id="ont", concrete_type="Order", object_id="order-1")
    edits = [Edit(sequence=0, op="set_property", target=target, property="status", value="reviewing", expected_old_value="pending"),
             Edit(sequence=1, op="set_property", target=target, property="status", value="approved", expected_old_value="reviewing")]
    db = FakeDb()
    before, after, results, _ = live_edits.apply_live_edits(
        db, "ont", edits, service, actor=ACTOR, expected_permission_digest="reviewed-digest")
    assert before["order-1"]["status"] == "pending"
    assert after["order-1"]["status"] == "approved"
    assert len(results) == 2
    assert db.row.row_data["status"] == graph.props["status"] == "approved"
