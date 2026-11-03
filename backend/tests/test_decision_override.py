"""Choosing a plan other than the recommended one needs a written reason that the reviewer sees and answers."""
import uuid

import pytest

from app.models.user import User
from app.models.v2.query_view import QueryDataView
from app.models.v2.scenario import ScenarioResource, ScenarioRun, ScenarioRunArtifact
from app.services.auth_service import hash_password
from app.services.v2 import decision_output, decision_packages


def candidate(candidate_id, label, metric_after, *, cost=0, applicable=True, recommended=None, harmed=()):
    row = {"id": candidate_id, "label": label, "applicable": applicable, "hard_constraints": [] if applicable else [{"code": "capacity_collision"}],
           "metric_after": metric_after, "cost": cost, "protected": ["WO-300"], "harmed": list(harmed), "actions": [{"action_type": "安排维护任务", "parameters": {}}]}
    if recommended is not None:
        row["recommended"] = recommended
    return row


def decision(candidates, direction="minimize"):
    subjects = [{"id": "WO-300", "label": "WO-300", "status": "未排产"}, *({"id": s, "label": s, "status": "按期"} for c in candidates for s in c["harmed"])]
    unique = {row["id"]: row for row in subjects}
    return {"schema": "decision/v1", "title": "测试决策", "subject_type": "WorkOrder",
            "metric": {"key": "exposure", "label": "高风险暴露", "unit": "分钟", "direction": direction, "baseline": 330},
            "subjects": list(unique.values()), "candidates": candidates}


def make_run(db, ontology_id, admin_user, output):
    view = QueryDataView(id=f"view-{uuid.uuid4().hex[:8]}", ontology_id=ontology_id, source_manifest_digest="s", metadata_digest="m",
                         graph_key="graph-x", status="ready", created_by=admin_user.id)
    scenario = ScenarioResource(ontology_id=ontology_id, owner_id=admin_user.id, name="工厂情景", base_view_id=view.id)
    db.add_all([view, scenario])
    db.commit()
    db.refresh(scenario)
    run = ScenarioRun(scenario_id=scenario.id, revision=0, case_key="基线", logic_asset_key="study:x", logic_version="scenario-study/v1", parameters={},
                      status="completed", progress=100, current_stage="done", input_digest="d" * 64, created_by=admin_user.id,
                      result_manifest={"nodes": {"decide": {"outputs": {"decision": output}}}})
    db.add(run)
    db.flush()
    db.add(ScenarioRunArtifact(run_id=run.id, artifact_type="node:decide", content_digest="sha256:" + "a" * 64))
    db.commit()
    return run


@pytest.fixture
def compiled(monkeypatch):
    """The real compile step needs the graph; the override rule sits around it, so stand in for just that call."""
    monkeypatch.setattr(decision_packages, "pinned_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(decision_output, "compile_decision_candidate", lambda db, oid, sid, snapshot, cand, actor: [
        {"action_type_id": "安排维护任务", "invocation": {"action_type": "安排维护任务", "parameters": {"candidate": cand["id"]}},
         "expected_edits": [{"op": "invoke_action", "sequence": 0}], "external_effect_class": "none"}])


@pytest.fixture
def reviewer_headers(client, db):
    db.add(User(id=str(uuid.uuid4()), username="reviewer", email="reviewer@test.com", password_hash=hash_password("review123"), role="admin"))
    db.commit()
    token = client.post("/api/v1/auth/login", json={"username": "reviewer", "password": "review123"}).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def create(client, headers, ontology_id, run, candidate_id, **extra):
    return client.post(f"/api/v2/ontologies/{ontology_id}/scenario-study-runs/{run.id}/decision-packages", headers=headers,
                       json={"candidate_id": candidate_id, "rationale": "先处理高风险设备", **extra})


PLANS = [candidate("combined", "组合方案", 0, cost=75, recommended=True), candidate("maintain", "只做检修", 0, cost=45),
         candidate("place", "加急排产", 330, cost=30, harmed=["WO-100"]), candidate("naive", "直接插入", 330, applicable=False)]


def test_recommended_plan_needs_no_override(client, auth_headers, ontology, db, admin_user, compiled):
    run = make_run(db, ontology["id"], admin_user, decision(PLANS))
    response = create(client, auth_headers, ontology["id"], run, "combined")
    assert response.status_code == 200, response.text
    assert response.json()["override"] is None
    assert [row["evidence_type"] for row in response.json()["evidence"]] == ["artifact", "metric"]
    # A reason sent for the recommended plan is not recorded as an override.
    other = create(client, auth_headers, ontology["id"], run, "combined", override_reason="其实不需要")
    assert other.status_code == 200 and other.json()["override"] is None


@pytest.mark.parametrize("reason", [None, "", "   ", "太短"])
def test_other_plans_need_a_real_reason(client, auth_headers, ontology, db, admin_user, compiled, reason):
    run = make_run(db, ontology["id"], admin_user, decision(PLANS))
    extra = {} if reason is None else {"override_reason": reason}
    response = create(client, auth_headers, ontology["id"], run, "maintain", **extra)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "override_reason_required"
    assert detail["details"]["recommended_candidate_id"] == "combined" and detail["details"]["chosen_candidate_id"] == "maintain"
    assert detail["details"]["recommended"]["cost"] == 75 and detail["details"]["chosen"]["cost"] == 45
    assert detail["details"]["metric"]["label"] == "高风险暴露"


def test_override_is_recorded_as_evidence_and_changes_the_digest(client, auth_headers, ontology, db, admin_user, compiled):
    run = make_run(db, ontology["id"], admin_user, decision(PLANS))
    first = create(client, auth_headers, ontology["id"], run, "maintain", override_reason="WO-300 本周不排，先只做检修").json()
    assert first["override"]["reason"] == "WO-300 本周不排，先只做检修"
    assert first["override"]["recommended_label"] == "组合方案" and first["override"]["chosen_label"] == "只做检修"
    assert first["override"]["metric"]["direction"] == "minimize"
    assert [row["evidence_type"] for row in first["evidence"]].count("override") == 1
    second = create(client, auth_headers, ontology["id"], run, "maintain", override_reason="另一个理由，同样足够长").json()
    assert first["content_digest"] != second["content_digest"], "the reason is part of what the package digest covers"
    # The candidate id of the chosen plan, not the recommended one, is what apply recompiles from.
    assert decision_packages.package_candidate_id(db, db.get(decision_packages_model(), first["id"])) == "maintain"


def decision_packages_model():
    from app.models.v2.decision import DecisionPackage
    return DecisionPackage


def test_inapplicable_plans_stay_blocked_before_any_override(client, auth_headers, ontology, db, admin_user, monkeypatch):
    monkeypatch.setattr(decision_packages, "pinned_snapshot", lambda *args, **kwargs: None)  # the real compile step refuses the plan
    run = make_run(db, ontology["id"], admin_user, decision(PLANS))
    response = create(client, auth_headers, ontology["id"], run, "naive", override_reason="我就是想要这个方案")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "hard_constraint_failed"


def test_override_needs_a_review_note_to_approve(client, auth_headers, reviewer_headers, ontology, db, admin_user, compiled):
    run = make_run(db, ontology["id"], admin_user, decision(PLANS))
    package = create(client, auth_headers, ontology["id"], run, "maintain", override_reason="WO-300 本周不排，先只做检修").json()
    packages = f"/api/v2/ontologies/{ontology['id']}/decision-packages"

    def move(item, event, headers, reason=""):
        return client.post(f"{packages}/{item['id']}/transition", headers=headers,
                           json={"event": event, "expected_etag": item["etag"], "request_id": uuid.uuid4().hex, "reason": reason})

    submitted = move(package, "submit", auth_headers).json()
    assert submitted["override"]["reason"] == "WO-300 本周不排，先只做检修"
    own = move(submitted, "approve", auth_headers, reason="我自己批")
    assert own.status_code == 403 and own.json()["detail"]["code"] == "self_review_forbidden"
    silent = move(submitted, "approve", reviewer_headers)
    assert silent.status_code == 422 and silent.json()["detail"]["code"] == "override_review_note_required"
    assert move(submitted, "approve", reviewer_headers, reason="  ").status_code == 422
    approved = move(submitted, "approve", reviewer_headers, reason="同意：WO-300 已与客户确认延后")
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert approved.json()["events"][-1]["reason"] == "同意：WO-300 已与客户确认延后"
    # Rejecting never needed an override note beyond the usual reason.
    again = create(client, auth_headers, ontology["id"], run, "maintain", override_reason="第二次：同样的理由").json()
    again = move(again, "submit", auth_headers).json()
    assert move(again, "reject", reviewer_headers, reason="理由不充分").json()["status"] == "rejected"


def test_a_package_without_override_approves_without_a_note(client, auth_headers, reviewer_headers, ontology, db, admin_user, compiled):
    run = make_run(db, ontology["id"], admin_user, decision(PLANS))
    package = create(client, auth_headers, ontology["id"], run, "combined").json()
    packages = f"/api/v2/ontologies/{ontology['id']}/decision-packages"
    submitted = client.post(f"{packages}/{package['id']}/transition", headers=auth_headers,
                            json={"event": "submit", "expected_etag": package["etag"], "request_id": "s1"}).json()
    approved = client.post(f"{packages}/{package['id']}/transition", headers=reviewer_headers,
                           json={"event": "approve", "expected_etag": submitted["etag"], "request_id": "a1"})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"


def test_direct_package_creation_cannot_forge_override_evidence(client, auth_headers, ontology, db, admin_user):
    run = make_run(db, ontology["id"], admin_user, decision(PLANS))
    body = {"scenario_id": run.scenario_id, "scenario_revision": 0, "run_id": run.id, "title": "手工", "rationale": "x",
            "actions": [{"action_type_id": "a", "invocation": {}, "expected_edits": []}],
            "evidence": [{"evidence_type": "override", "reference": {"reason": "伪造"}, "content_digest": "0" * 64}]}
    assert client.post(f"/api/v2/ontologies/{ontology['id']}/decision-packages", headers=auth_headers, json=body).status_code == 422


@pytest.mark.parametrize("candidates, direction, expected", [
    ([candidate("a", "A", 5), candidate("c", "C", 3, cost=10), candidate("b", "B", 3)], "minimize", "c"),          # best metric; a tie goes to the one listed first
    ([candidate("a", "A", 5), candidate("b", "B", 9)], "maximize", "b"),
    ([candidate("a", "A", 1), candidate("b", "B", 9, recommended=True)], "minimize", "b"),                        # the Function's own flag wins
    ([candidate("a", "A", 1, applicable=False), candidate("b", "B", 9)], "minimize", "b"),                        # inapplicable never recommended
    ([candidate("a", "A", 1, applicable=False)], "minimize", None),
])
def test_recommended_candidate_rule(candidates, direction, expected):
    result = decision_packages.recommended_candidate(decision(candidates, direction))
    assert (result or {}).get("id") == expected
