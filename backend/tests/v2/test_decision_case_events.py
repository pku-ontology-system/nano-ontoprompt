"""Case events keep distinct sequences even when the session does not autoflush (the application's setting)."""
from sqlalchemy.orm import sessionmaker

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.v2.action import OntologyActionType
from app.models.v2.decision_case import DecisionCase, DecisionCaseEvent
from app.services.v2 import decision_cases
from app.services.v2.decision_cases import digest


def test_events_added_in_one_unit_of_work_get_distinct_sequences(db, ontology, admin_user):
    entity = Entity(id="event-machine-type", ontology_id=ontology["id"], name_cn="设备", name_en="Machine", type="entity")
    instance = EntityInstance(id="event-machine-1", entity_id=entity.id, ontology_id=ontology["id"], row_identity="event-machine-1", row_data={})
    action = OntologyActionType(id="event-action", ontology_id=ontology["id"], name="Update machine", action_category="write", parameters=[], effects=[])
    case = DecisionCase(id="event-case", ontology_id=ontology["id"], action_type_id=action.id, kind="general", title="事件序号", target_object_id=instance.id,
                        target_object_type="Machine", parameters={}, evidence_refs=[], reason="r", context_json={}, context_digest=digest({}),
                        expected_etag=digest({}), client_request_id="event-case-request", request_digest=digest({}), status="draft", created_by=admin_user.id)
    db.add_all([entity, instance, action, case])
    db.commit()

    session = sessionmaker(bind=db.get_bind(), autoflush=False)()
    try:
        row = session.get(DecisionCase, "event-case")
        names = ["projection_synced", "projection_attempt_finished", "projection_attempt_started"]
        events = [decision_cases._event(session, row, name, admin_user.id, {}, "trace") for name in names]
        session.commit()
        assert [event.sequence for event in events] == [1, 2, 3]
        stored = session.query(DecisionCaseEvent).filter_by(case_id="event-case").order_by(DecisionCaseEvent.sequence).all()
        assert [event.event_type for event in stored] == names
    finally:
        session.close()
