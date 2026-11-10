"""The analysis API on an ontology that was imported from tables: the objects are the imported rows, the lineage is the import."""
from types import SimpleNamespace

import pytest

from app.main import app
from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.routers.v2 import analysis as router
from app.services.v2.analysis import ontology_source
from app.services.v2.data_import import runner
from tests.test_data_import import graph, shop  # noqa: F401  (the graph recorder fixture and the sample shop)

BASE = "/api/v2/analysis"


@pytest.fixture
def imported(db, admin_user, graph, monkeypatch):  # noqa: F811
    """A shop imported for real; the object query is answered from the stored rows instead of the graph database."""
    result = runner.run(db, admin_user, shop, replace=False)
    oid = result["ontology_id"]

    def rows(otype):
        entity = db.query(Entity).filter(Entity.ontology_id == oid, Entity.name_en == otype).first()
        return [] if entity is None else db.query(EntityInstance).filter(EntityInstance.entity_id == entity.id).all()

    monkeypatch.setattr(ontology_source, "object_counts", lambda db_, user, graph_, o, names: {n: {"count": len(rows(n)), "capped": False} for n in names})
    monkeypatch.setattr(ontology_source, "load_objects", lambda db_, user, graph_, o, otype, limit=10_000: ([SimpleNamespace(object_id=r.id, properties=dict(r.row_data)) for r in rows(otype)],
                                                                                                          {"object_type": otype, "snapshot": False, "truncated": False}))
    app.dependency_overrides[router.get_falkordb] = lambda: object()
    yield oid
    app.dependency_overrides.pop(router.get_falkordb, None)


def test_the_overview_lists_the_imported_ontology_with_its_sizes(client, auth_headers, imported):
    body = client.get(f"{BASE}/overview", headers=auth_headers).json()
    mine = next(o for o in body["ontologies"] if o["id"] == imported)
    assert (mine["types"], mine["objects"], mine["link_types"]) == (3, 14, 1)
    assert mine["imported"]["tables"] == 3 and mine["build_mode"] == "data_import"
    assert body["totals"]["imports"] >= 1 and body["lineage_jobs"] > 0


def test_every_imported_type_can_be_read_and_checked_against_the_ontology(client, auth_headers, imported):
    types = client.get(f"{BASE}/ontologies/{imported}/object-types", headers=auth_headers).json()["types"]
    assert {t["api_name"]: t["object_count"] for t in types} == {"Customer": 4, "Order": 6, "CustomerSpend": 4}
    for name in ("Customer", "Order", "CustomerSpend"):
        run = client.post(f"{BASE}/ontologies/{imported}/health/run", json={"object_type": name}, headers=auth_headers)
        assert run.status_code == 200, run.text
        report = run.json()["result"]
        assert report["success"] is True and report["statistics"]["evaluated_expectations"] >= 4, name
    suite = client.get(f"{BASE}/ontologies/{imported}/health/suites/Order", headers=auth_headers).json()["suite"]["expectations"]
    assert any(e["expectation_type"] == "expect_column_values_to_be_between" and e["kwargs"]["column"] == "total" for e in suite), "the steward's range is an expectation"


def test_a_type_with_names_can_be_resolved_and_one_without_is_not_applicable(client, auth_headers, imported):
    done = client.post(f"{BASE}/ontologies/{imported}/resolution/run", json={"object_type": "Customer"}, headers=auth_headers)
    assert done.status_code == 200 and done.json()["result"]["summary"]["records"] == 4
    refused = client.post(f"{BASE}/ontologies/{imported}/resolution/run", json={"object_type": "CustomerSpend"}, headers=auth_headers)
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "resolution_not_applicable"


def test_the_pipeline_skips_resolution_for_a_type_that_cannot_be_resolved_and_still_draws_the_lineage(client, auth_headers, imported):
    body = client.post(f"{BASE}/ontologies/{imported}/pipeline/run", json={"object_type": "CustomerSpend"}, headers=auth_headers)
    assert body.status_code == 200, body.text
    result = body.json()["result"]
    assert result["resolution"] is None and result["runs"]["resolution"] is None and result["runs"]["health"]
    resolve = next(s for s in result["steps"] if s["key"] == "resolve")
    assert resolve["summary"]["skipped"] is True and "不适用" in resolve["summary"]["reason"]
    assert result["lineage"]["events"] > 0 and result["health"]["statistics"]["evaluated_expectations"] >= 4
    full = client.post(f"{BASE}/ontologies/{imported}/pipeline/run", json={"object_type": "Customer"}, headers=auth_headers).json()["result"]
    assert full["resolution"] is not None and full["runs"]["resolution"]


def test_the_lineage_of_the_import_is_there_without_asking_for_it(client, auth_headers, imported):
    graph = client.get(f"{BASE}/ontologies/{imported}/lineage/graph", headers=auth_headers).json()
    assert len(graph["nodes"]) >= 6 and graph["edges"] and graph["column_edges"], "tables, the derived table with its columns, and the types"
    spend = next(n for n in graph["nodes"] if n["id"].endswith("CustomerSpend") or "spend" in n["id"].lower())
    impact = client.get(f"{BASE}/ontologies/{imported}/lineage/impact", params={"node": spend["id"]}, headers=auth_headers)
    assert impact.status_code == 200


def test_a_big_type_a_read_only_account_cannot_pin_is_refused_with_403(client, auth_headers, imported, monkeypatch):
    def refuse(*args, **kwargs):
        raise ontology_source.SnapshotNotAllowed("这个类型的对象多于 200 个，要先固定一份快照才能读完")
    monkeypatch.setattr(ontology_source, "load_frame", refuse)
    response = client.post(f"{BASE}/ontologies/{imported}/health/run", json={"object_type": "Order"}, headers=auth_headers)
    assert response.status_code == 403 and response.json()["detail"]["code"] == "snapshot_not_allowed"


def test_the_import_catalog_reports_what_is_already_imported(client, auth_headers, imported):
    bundles = client.get("/api/v2/ontology-imports/bundles", headers=auth_headers).json()["bundles"]
    assert {b["id"] for b in bundles} >= {"demo-tpch", "demo-nyc-taxi", "demo-brick-site-b", "demo-abt-buy", "demo-beans"}
    assert all(b["kind"] and b["title"] for b in bundles)
