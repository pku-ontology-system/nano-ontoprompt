"""AI ontology candidates: parsing an Auto Ontology export, item review, publishing and one-click generation."""
from pathlib import Path

import pytest

from app.services.v2 import ontology_candidates as service
from app.services.v2.auto_ontology_runner import RunnerError, RunnerUnavailable

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "auto_ontology"
MINIMAX = (EXAMPLES / "factory_twin.minimax-m3.yaml").read_text(encoding="utf-8")


def base(ontology):
    return f"/api/v2/ontologies/{ontology['id']}/ai-candidates"


def import_example(client, auth_headers, ontology, example="factory_twin_minimax_m3"):
    response = client.post(f"{base(ontology)}:import", json={"example_id": example}, headers=auth_headers)
    assert response.status_code == 201, response.text
    return response.json()


def item(detail, key):
    return next(row for row in detail["items"] if row["key"] == key)


WRONG = "fk:factory_asset.source_unit->factory_resource.resource_id"


# ------------------------------------------------------------------------------------------------------- parsing
def test_parse_export_keeps_evidence_and_bridges():
    parsed = service.parse_interchange(MINIMAX)
    assert [row["name"] for row in parsed["entities"]] == ["FactoryAsset", "FactoryResource", "ScheduleItem", "WorkOrder"]
    asset = parsed["entities"][0]
    assert asset["evidence"]["source_table"] == "factory_asset" and asset["evidence"]["primary_key"] == ["asset_id"]
    assert {"id": "asset_id", "is_identifier": True, "source_field": "factory_asset.asset_id"}.items() <= asset["draft"]["properties"][0].items()
    by_key = {row["key"]: row for row in parsed["relationships"]}
    declared = by_key["fk:factory_asset.resource_id->factory_resource.resource_id"]
    assert declared["basis"] == "declared_fk" and declared["draft"]["cardinality"] == "many-to-one"
    wrong = by_key[WRONG]
    assert wrong["basis"] == "semantic_fk" and wrong["evidence"]["used_in_join_paths"] is False
    assert wrong["evidence"]["sample_values"]["source"]
    bridge = by_key["bridge:schedule_item_resource"]
    assert bridge["basis"] == "bridge_table" and bridge["draft"]["cardinality"] == "many-to-many"
    assert {bridge["draft"]["source"], bridge["draft"]["target"]} == {"FactoryResource", "ScheduleItem"}
    assert [(w["code"], w["table"]) for w in parsed["warnings"]] == [("bridge_unresolved", "schedule_dependency")]


@pytest.mark.parametrize("text", ["", "not: [valid", "just a string", "data_layer: 3", "data_layer: {databases: [{schemas: [{tables: [{name: x}]}]}]}"])
def test_parse_rejects_documents_that_are_not_exports(text):
    with pytest.raises(service.CandidateError) as error:
        service.parse_interchange(text)
    assert error.value.code in {"invalid_document"}


def test_parse_rejects_oversized_documents():
    with pytest.raises(service.CandidateError) as error:
        service.parse_interchange("a: " + "x" * service.MAX_DOCUMENT_BYTES)
    assert error.value.code == "document_too_large"


# ------------------------------------------------------------------------------------------- import, review, publish
def test_runner_overview_without_a_runner(client, auth_headers, ontology, monkeypatch):
    monkeypatch.setattr(service, "get_runner_client", lambda: None)
    body = client.get(f"{base(ontology)}/runner", headers=auth_headers).json()
    assert body["configured"] is False and body["reachable"] is False
    assert {row["id"] for row in body["examples"]} >= {"factory_twin_minimax_m3"}
    assert "wrong_meaning" in {row["id"] for row in body["reject_categories"]}


def test_import_creates_pending_items_with_evidence(client, auth_headers, ontology):
    detail = import_example(client, auth_headers, ontology)
    assert detail["status"] == "review" and detail["origin"] == "example"
    assert detail["generator"]["model_label"] == "MiniMax-M3" and detail["generator"]["locality"] == "cloud"
    assert len(detail["items"]) == 9 and detail["stats"]["pending"] == 9
    assert detail["warnings"][0]["table"] == "schedule_dependency"
    assert all(row["decision"] == "pending" for row in detail["items"])
    assert detail["stats"]["by_basis"]["semantic_fk"]["total"] == 3


def test_import_validates_the_document(client, auth_headers, ontology):
    bad = client.post(f"{base(ontology)}:import", json={"document": "hello: world"}, headers=auth_headers)
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_document"
    empty = client.post(f"{base(ontology)}:import", json={}, headers=auth_headers)
    assert empty.status_code == 422 and empty.json()["detail"]["code"] == "document_required"
    unknown = client.post(f"{base(ontology)}:import", json={"example_id": "nope"}, headers=auth_headers)
    assert unknown.status_code == 404


def review(client, auth_headers, ontology, detail, key, **body):
    return client.post(f"{base(ontology)}/{detail['id']}/items/{item(detail, key)['id']}:review", json=body, headers=auth_headers)


def test_full_review_and_publish(client, auth_headers, ontology):
    detail = import_example(client, auth_headers, ontology)
    set_url = f"{base(ontology)}/{detail['id']}"

    blocked = client.post(f"{set_url}:publish", json={}, headers=auth_headers)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "pending_items"

    # A rejection needs a reason category and some text.
    no_reason = review(client, auth_headers, ontology, detail, WRONG, decision="rejected")
    assert no_reason.status_code == 422 and no_reason.json()["detail"]["code"] == "reject_category_required"
    no_text = review(client, auth_headers, ontology, detail, WRONG, decision="rejected", reject_category="wrong_meaning", reject_reason=" ")
    assert no_text.status_code == 422 and no_text.json()["detail"]["code"] == "reject_reason_required"
    rejected = review(client, auth_headers, ontology, detail, WRONG, decision="rejected", reject_category="wrong_meaning",
                      reject_reason="source_unit 是机组编号，不是资源 ID")
    assert rejected.status_code == 200 and rejected.json()["item"]["history"][0]["decision"] == "rejected"

    renamed = review(client, auth_headers, ontology, detail, "bridge:schedule_item_resource", decision="modified",
                     final={"type": "uses_resource", "description": "排程项占用的资源"})
    assert renamed.status_code == 200 and renamed.json()["item"]["effective"]["type"] == "uses_resource"
    described = review(client, auth_headers, ontology, detail, "entity:WorkOrder", decision="modified",
                       final={"name_cn": "工单", "description": "要完成的一项生产或检修工作"})
    assert described.json()["item"]["decision"] == "modified"

    for row in detail["items"]:
        if row["key"] not in {WRONG, "bridge:schedule_item_resource", "entity:WorkOrder"}:
            assert review(client, auth_headers, ontology, detail, row["key"], decision="accepted").status_code == 200

    after = client.get(set_url, headers=auth_headers).json()
    assert after["stats"]["accepted"] == 6 and after["stats"]["modified"] == 2 and after["stats"]["rejected"] == 1
    assert after["stats"]["reject_reasons"] == {"wrong_meaning": 1} and after["stats"]["accept_rate"] == round(8 / 9, 3)
    assert after["stats"]["by_basis"]["semantic_fk"]["rejected"] == 1

    dry_run = client.post(f"{set_url}:validate", json={}, headers=auth_headers)
    assert dry_run.status_code == 200 and dry_run.json()["can_apply"] is True and len(dry_run.json()["operations"]) == 8
    editor_before = client.get(f"/api/v2/ontologies/{ontology['id']}/editor", headers=auth_headers).json()
    assert not any(row["name_en"] == "FactoryAsset" for row in editor_before["entities"])

    published = client.post(f"{set_url}:publish", json={}, headers=auth_headers)
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["set"]["status"] == "published" and body["set"]["published_revision_id"] == body["revision"]["id"]
    assert all(row["publish_result"] and row["publish_result"]["revision_id"] == body["revision"]["id"]
               for row in body["set"]["items"] if row["decision"] != "rejected")
    assert item(body["set"], WRONG)["publish_result"] is None

    editor = client.get(f"/api/v2/ontologies/{ontology['id']}/editor", headers=auth_headers).json()
    names = {row["name_en"]: row for row in editor["entities"]}
    assert {"FactoryAsset", "FactoryResource", "ScheduleItem", "WorkOrder"} <= set(names)
    assert names["WorkOrder"]["name_cn"] == "工单"
    assert {p["id"] for p in names["FactoryAsset"]["properties"]["property_definitions"]} >= {"asset_id", "risk_level", "health_score"}
    relation_types = {row["type"] for row in editor["relationships"]}
    assert "uses_resource" in relation_types and "factory_asset_resource_id" in relation_types
    assert "factory_asset_source_unit" not in relation_types
    uses = next(row for row in editor["relationships"] if row["type"] == "uses_resource")
    assert uses["properties"]["cardinality"] == "many-to-many" and uses["properties"]["provenance"]["candidate_set"] == detail["id"]

    again = client.post(f"{set_url}:publish", json={}, headers=auth_headers)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_reviewable"
    late = review(client, auth_headers, ontology, detail, WRONG, decision="accepted")
    assert late.status_code == 409


def test_rejecting_an_endpoint_blocks_publishing_its_relationships(client, auth_headers, ontology):
    detail = import_example(client, auth_headers, ontology)
    for row in detail["items"]:
        if row["key"] == "entity:FactoryResource":
            review(client, auth_headers, ontology, detail, row["key"], decision="rejected", reject_category="out_of_scope", reject_reason="暂不建模资源")
        else:
            review(client, auth_headers, ontology, detail, row["key"], decision="accepted")
    result = client.post(f"{base(ontology)}/{detail['id']}:publish", json={}, headers=auth_headers)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "endpoint_not_accepted"
    assert result.json()["detail"]["details"]["endpoint"] == "FactoryResource"
    editor = client.get(f"/api/v2/ontologies/{ontology['id']}/editor", headers=auth_headers).json()
    assert editor["entities"] == [] or not any(row["name_en"] == "FactoryAsset" for row in editor["entities"])
    assert client.get(f"{base(ontology)}/{detail['id']}", headers=auth_headers).json()["status"] == "review"


def test_modified_without_changes_counts_as_accepted(client, auth_headers, ontology):
    detail = import_example(client, auth_headers, ontology)
    response = review(client, auth_headers, ontology, detail, "entity:WorkOrder", decision="modified", final={})
    assert response.json()["item"]["decision"] == "accepted" and response.json()["item"]["final"] is None


@pytest.mark.parametrize("final", [
    {"properties": [{"id": "not_a_column", "name": "x"}]},
    {"properties": []},
    {"properties": [{"id": "order_id", "type": "money"}]},
    {"name_cn": "  "},
    {"description": 3},
])
def test_modification_cannot_invent_properties_or_invalid_values(client, auth_headers, ontology, final):
    detail = import_example(client, auth_headers, ontology)
    response = review(client, auth_headers, ontology, detail, "entity:WorkOrder", decision="modified", final=final)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_final"


def test_relationship_modification_validates_cardinality(client, auth_headers, ontology):
    detail = import_example(client, auth_headers, ontology)
    key = "fk:schedule_item.order_id->work_order.order_id"
    assert review(client, auth_headers, ontology, detail, key, decision="modified", final={"cardinality": "several"}).status_code == 422
    ok = review(client, auth_headers, ontology, detail, key, decision="modified", final={"cardinality": "1:N"})
    assert ok.json()["item"]["effective"]["cardinality"] == "one-to-many"


def test_existing_object_types_cannot_be_added_twice(client, auth_headers, ontology):
    first = import_example(client, auth_headers, ontology)
    for row in first["items"]:
        if row["key"].startswith("entity:"):
            review(client, auth_headers, ontology, first, row["key"], decision="accepted")
        else:
            review(client, auth_headers, ontology, first, row["key"], decision="rejected", reject_category="other", reject_reason="只导入对象")
    assert client.post(f"{base(ontology)}/{first['id']}:publish", json={}, headers=auth_headers).status_code == 200
    second = import_example(client, auth_headers, ontology, "factory_twin_qwen2_5_7b")
    existing = item(second, "entity:FactoryResource")
    assert existing["exists_in_ontology"] is True
    blocked = review(client, auth_headers, ontology, second, "entity:FactoryResource", decision="accepted")
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "already_exists"
    # A relationship may point at an object type that already exists in the ontology.
    for row in second["items"]:
        if row["key"] == "fk:schedule_item.primary_resource_id->factory_resource.resource_id":
            continue
        if row["key"] in {"entity:ScheduleItem"}:
            review(client, auth_headers, ontology, second, row["key"], decision="accepted")
        elif not row["exists_in_ontology"] or row["kind"] == "relationship":
            review(client, auth_headers, ontology, second, row["key"], decision="rejected", reject_category="duplicate", reject_reason="已有")
    link = review(client, auth_headers, ontology, second, "fk:schedule_item.primary_resource_id->factory_resource.resource_id", decision="accepted")
    assert link.status_code == 200


# --------------------------------------------------------------------------------------------- one-click generation
class FakeRunner:
    def __init__(self, document=MINIMAX):
        self.document = document
        self.run_status = {"id": "r1", "status": "running", "stage": "terms", "tables": {"total": 6, "done": 2}, "llm_calls": 9, "detail": "x",
                           "stages": [{"key": "terms", "label": "术语与列描述", "status": "running", "started_at": 1.0, "finished_at": None}],
                           "log_tail": [], "error": None}
        self.started, self.cancelled, self.exports, self.down = [], [], 0, False
        self.model = {"id": "minimax-m3", "label": "MiniMax-M3", "locality": "cloud", "chat_model": "openai/MiniMax-M3", "embed_model": "nomic-embed-text",
                      "available": True, "outbound": ["库表名称和列名", "列的样本取值"]}
        self.local = {"id": "qwen", "label": "Qwen", "locality": "local", "chat_model": "openai/qwen2.5:7b", "embed_model": "nomic-embed-text",
                      "available": True, "outbound": []}
        self.sources_ = [{"id": "factory_twin", "label": "工厂排程关系库（演示）", "privacy_level": "demo", "tables": []},
                         {"id": "hr", "label": "人事库", "privacy_level": "private", "tables": []}]

    def _up(self):
        if self.down:
            raise RunnerUnavailable("无法连接 Auto Ontology 运行器")

    def health(self):
        self._up()
        return {"ok": True, "auto_ontology": {"version": "1.1.0", "commit": "dd78d06"}}

    def models(self):
        self._up()
        return [self.model, self.local]

    def sources(self):
        self._up()
        return self.sources_

    def start(self, source_id, model_id, requested_by):
        self._up()
        self.started.append((source_id, model_id, requested_by))
        return {**self.run_status, "status": "queued"}

    def status(self, run_id):
        self._up()
        return self.run_status

    def export(self, run_id):
        self.exports += 1
        return {"yaml": self.document, "run": {"total_seconds": 69.5, "llm_calls": 27}, "generator": {"commit": "dd78d06"}}

    def cancel(self, run_id):
        self.cancelled.append(run_id)
        return {}


@pytest.fixture
def runner(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(service, "get_runner_client", lambda: fake)
    return fake


def generate(client, auth_headers, ontology, **body):
    return client.post(f"{base(ontology)}:generate", json={"source_id": "factory_twin", "model_id": "minimax-m3", **body}, headers=auth_headers)


def test_cloud_models_need_an_explicit_acknowledgement(client, auth_headers, ontology, runner):
    refused = generate(client, auth_headers, ontology)
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["code"] == "cloud_acknowledgement_required" and "列的样本取值" in detail["details"]["outbound"]
    assert runner.started == []
    started = generate(client, auth_headers, ontology, acknowledge_cloud=True)
    assert started.status_code == 201 and started.json()["status"] == "generating"
    assert started.json()["generator"]["cloud_acknowledged"] is True and started.json()["generator"]["version"] == "1.1.0"
    assert runner.started == [("factory_twin", "minimax-m3", "admin")]


def test_private_sources_never_go_to_cloud_models(client, auth_headers, ontology, runner):
    blocked = generate(client, auth_headers, ontology, source_id="hr", acknowledge_cloud=True)
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "privacy_blocked"
    allowed = generate(client, auth_headers, ontology, source_id="hr", model_id="qwen")
    assert allowed.status_code == 201 and allowed.json()["generator"]["cloud_acknowledged"] is False


def test_generation_rejects_unknown_or_unavailable_choices(client, auth_headers, ontology, runner):
    assert generate(client, auth_headers, ontology, source_id="nope", acknowledge_cloud=True).json()["detail"]["code"] == "unknown_selection"
    runner.model["available"] = False
    unavailable = generate(client, auth_headers, ontology, acknowledge_cloud=True)
    assert unavailable.status_code == 409 and unavailable.json()["detail"]["code"] == "model_unavailable"
    runner.down = True
    down = generate(client, auth_headers, ontology, acknowledge_cloud=True)
    assert down.status_code == 503 and down.json()["detail"]["code"] == "runner_unavailable"


def test_a_busy_runner_is_reported(client, auth_headers, ontology, runner, monkeypatch):
    def busy(*args):
        raise RunnerError(409, "run_active", "Run abc is still in progress")
    monkeypatch.setattr(runner, "start", busy)
    response = generate(client, auth_headers, ontology, acknowledge_cloud=True)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "run_active"


def test_progress_is_synced_and_the_export_is_imported_once(client, auth_headers, ontology, runner):
    started = generate(client, auth_headers, ontology, acknowledge_cloud=True).json()
    url = f"{base(ontology)}/{started['id']}"
    running = client.get(url, headers=auth_headers).json()
    assert running["status"] == "generating" and running["progress"]["tables"] == {"total": 6, "done": 2} and running["items"] == []

    runner.run_status = {**runner.run_status, "status": "succeeded", "stage": "export", "tables": {"total": 6, "done": 6}}
    done = client.get(url, headers=auth_headers).json()
    assert done["status"] == "review" and len(done["items"]) == 9
    assert done["generator"]["seconds"] == 69.5 and done["generator"]["llm_calls"] == 27 and done["generator"]["table_count"] == 6
    again = client.get(url, headers=auth_headers).json()
    assert len(again["items"]) == 9 and runner.exports == 1


def test_failed_and_cancelled_runs(client, auth_headers, ontology, runner):
    first = generate(client, auth_headers, ontology, acknowledge_cloud=True).json()
    runner.run_status = {**runner.run_status, "status": "failed", "error": "model quota exceeded"}
    failed = client.get(f"{base(ontology)}/{first['id']}", headers=auth_headers).json()
    assert failed["status"] == "failed" and failed["error"] == "model quota exceeded"

    runner.run_status = {**runner.run_status, "status": "running", "error": None}
    second = generate(client, auth_headers, ontology, acknowledge_cloud=True).json()
    cancelled = client.post(f"{base(ontology)}/{second['id']}:cancel", headers=auth_headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled" and runner.cancelled == ["r1"]
    assert client.post(f"{base(ontology)}/{second['id']}:cancel", headers=auth_headers).status_code == 409


def test_an_unreachable_runner_does_not_fail_a_running_generation(client, auth_headers, ontology, runner):
    started = generate(client, auth_headers, ontology, acknowledge_cloud=True).json()
    runner.down = True
    detail = client.get(f"{base(ontology)}/{started['id']}", headers=auth_headers).json()
    assert detail["status"] == "generating" and "runner_unreachable" in detail["progress"]


def test_a_broken_export_fails_the_set_instead_of_importing_half(client, auth_headers, ontology, runner):
    runner.document = "hello: world"
    started = generate(client, auth_headers, ontology, acknowledge_cloud=True).json()
    runner.run_status = {**runner.run_status, "status": "succeeded"}
    detail = client.get(f"{base(ontology)}/{started['id']}", headers=auth_headers).json()
    assert detail["status"] == "failed" and detail["items"] == [] and "data_layer" in detail["error"]


def test_listing_shows_stats_for_each_set(client, auth_headers, ontology):
    import_example(client, auth_headers, ontology)
    rows = client.get(base(ontology), headers=auth_headers).json()["sets"]
    assert len(rows) == 1 and rows[0]["stats"]["total"] == 9 and "items" not in rows[0]
