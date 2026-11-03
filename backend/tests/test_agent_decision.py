from app.models.ontology import OntologyProject
from app.models.ontology_revision import OntologyRevision
from app.services.v2.agent_decision import AgentToolbox


def test_deterministic_agent_requires_real_reasoning_basis(db, ontology):
    # Proposals read a published revision first; with one in place the refusal
    # must come from the missing reasoning basis.
    revision = OntologyRevision(ontology_id=ontology["id"], revision_no=1, status="current", is_current=True)
    db.add(revision)
    db.flush()
    db.get(OntologyProject, ontology["id"]).current_revision_id = revision.id
    db.commit()
    toolbox = AgentToolbox(ontology["id"], db)
    try:
        toolbox.propose("检查设备")
    except ValueError as exc:
        assert "推理结论" in str(exc) or "推理运行" in str(exc)
    else:
        raise AssertionError("an Agent proposal must be grounded in a reasoning conclusion")
