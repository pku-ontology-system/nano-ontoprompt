import pytest
from fastapi import HTTPException

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.models.v2.action import OntologyActionRun, OntologyActionType
from app.routers.v2 import logic_actions

def test_revert_legacy_run_in_registered_ontology_fails_closed(db, admin_user):
    ontology_id = "ont-revert"
    db.add(OntologyProject(id=ontology_id, name="Test", domain="Test", created_by=admin_user.id))
    db.add(Entity(id="order-type", ontology_id=ontology_id, name_cn="Order", name_en="Order", type="EntityType", properties={}))
    db.add(EntityInstance(id="order-1", ontology_id=ontology_id, entity_id="order-type", row_identity="order-1", row_data={"status": "approved"}))
    db.add(OntologyActionType(id="approve", ontology_id=ontology_id, name="Approve", action_category="crud", effects=[], status="published", enabled=True))
    original = OntologyActionRun(id="run-1", ontology_id=ontology_id, action_type_id="approve",
                                 target_object_id="order-1", parameters={}, execution_context={"ontology_id": ontology_id, "consistency": "live"},
                                 status="completed", before_snapshot={"order-1": {"status": "pending"}},
                                 after_snapshot={"order-1": {"status": "approved"}},
                                 side_effect_results=[{"op": "set_property", "target_id": "order-1"}], executed_by=admin_user.id)
    db.add(original)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        logic_actions.revert_action_run(ontology_id, original.id, logic_actions.ActionRevertRequest(
            client_request_id="revert-legacy-1", preview_digest="0" * 64, reason="undo a legacy run",
        ), db, admin_user)

    db.refresh(original)
    row = db.get(EntityInstance, "order-1")
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "legacy_run_requires_manual_compensation"
    assert original.status == "completed"
    assert row.row_data["status"] == "approved"
