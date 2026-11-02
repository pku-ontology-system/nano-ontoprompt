import json
from types import SimpleNamespace

import pytest

from app.services.v2 import agent_sessions


class StubQuery:
    def __init__(self, result):
        self.result = result

    def filter_by(self, **_kwargs):
        return self

    def first(self):
        return self.result


class StubDB:
    def __init__(self, config):
        self.config = config
        self.commits = 0

    def query(self, _model):
        return StubQuery(self.config)

    def commit(self):
        self.commits += 1


def make_service(api_base="http://host.docker.internal:11434/v1"):
    service = agent_sessions.AgentSessionService.__new__(agent_sessions.AgentSessionService)
    service.db = StubDB(SimpleNamespace(
        name=agent_sessions.LOCAL_CONFIG_NAME,
        provider="ollama",
        models=[agent_sessions.LOCAL_MODEL_NAME],
        api_base=api_base,
    ))
    return service


def test_local_model_config_accepts_only_the_local_ollama_endpoint():
    service = make_service()

    config, api_base = service._local_model_config()

    assert config.provider == "ollama"
    assert api_base == "http://host.docker.internal:11434/v1"


@pytest.mark.parametrize("api_base", [
    "https://api.example.com/v1",
    "http://10.0.0.10:11434/v1",
    "http://host.docker.internal:11434/v1?route=paid",
    "http://host.docker.internal:11434/v1/chat/completions",
])
def test_local_model_config_rejects_non_local_or_unexpected_routes(api_base):
    service = make_service(api_base)

    with pytest.raises(agent_sessions.SessionError) as exc_info:
        service._local_model_config()

    assert exc_info.value.code == "unsafe_model_endpoint"


def test_local_model_answer_validates_model_citations(monkeypatch):
    service = make_service()
    ontology_revision_id = "revision-7"
    refs = [{"kind": "object", "ref_id": "machine-1", "source_version": "v3"}]
    objects = [{"id": "machine-1", "object_type": "Machine", "properties": {"status": "warning"}}]
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "answer": "设备状态为 warning，建议复核。",
                "used_refs": ["object:machine-1"],
                "uncertainty": "缺少近期维护记录。",
            }, ensure_ascii=False)}}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["request"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setattr(agent_sessions.httpx, "post", fake_post)

    answer, citations, model_name, uncertainty = service._local_model_answer(
        "设备需要检查吗？", ontology_revision_id, refs, objects,
    )

    assert captured["url"] == "http://host.docker.internal:11434/v1/chat/completions"
    assert captured["request"]["model"] == agent_sessions.LOCAL_MODEL_NAME
    prompt_context = json.loads(captured["request"]["messages"][1]["content"])
    assert prompt_context["authorized_objects"] == objects
    assert prompt_context["ontology_revision_id"] == ontology_revision_id
    assert model_name == agent_sessions.LOCAL_MODEL_NAME
    assert answer == "设备状态为 warning，建议复核。"
    assert citations[0]["key"] == "object:machine-1"
    assert uncertainty == "缺少近期维护记录。"
    assert service.db.commits == 1


def test_local_model_answer_rejects_fabricated_citations(monkeypatch):
    service = make_service()
    session = SimpleNamespace(metadata_revision_id="revision-7")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "answer": "已找到证据。",
                "used_refs": ["object:unauthorized-machine"],
                "uncertainty": "",
            })}}]}

    monkeypatch.setattr(agent_sessions.httpx, "post", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(agent_sessions.SessionError) as exc_info:
        service._local_model_answer("问题", "revision-7", [], [])

    assert exc_info.value.code == "invalid_local_model_citations"
