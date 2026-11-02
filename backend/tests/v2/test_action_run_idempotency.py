import pytest
from sqlalchemy.exc import IntegrityError

from app.models.v2.action import OntologyActionRun


def _run(ontology_id, *, decision_case_id=None):
    return OntologyActionRun(
        ontology_id=ontology_id,
        action_type_id=None,
        target_object_id="machine-1",
        parameters={},
        execution_context={"consistency": "live"},
        status="completed",
        decision_case_id=decision_case_id,
    )


def test_decision_case_has_at_most_one_action_run_and_legacy_runs_remain_valid(db, ontology):
    ontology_id = ontology["id"]
    db.add_all([
        _run(ontology_id),
        _run(ontology_id),
        _run(ontology_id, decision_case_id="case-1"),
    ])
    db.commit()

    db.add(_run(ontology_id, decision_case_id="case-1"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    assert db.query(OntologyActionRun).filter_by(
        ontology_id=ontology_id, decision_case_id="case-1",
    ).count() == 1
    assert db.query(OntologyActionRun).filter_by(
        ontology_id=ontology_id, decision_case_id=None,
    ).count() == 2
