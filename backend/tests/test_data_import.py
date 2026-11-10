"""The generic importer: tables in, an ontology with objects and relationships out, and the lineage that tells where everything came from.

The graph database is stood in by a recorder; the metadata, the candidate path, the object rows and the lineage are real.
"""
import io
import zipfile

import pandas as pd
import pytest
import yaml

from app.models.entity import Entity
from app.models.entity_instance import EntityInstance
from app.models.ontology import OntologyProject
from app.services.v2.analysis import lineage, ontology_lineage, ontology_metadata
from app.services.v2.data_import import catalog, duck, install, profile, runner, schema, uploads
from app.services.v2.data_import.model import Bundle, Derived, Link, Source, Table


class Recorder:
    """What the graph database would have been asked to store."""
    available = True
    nodes: list[dict] = []
    edges: list[dict] = []

    def __init__(self):
        type(self).nodes, type(self).edges = [], []

    def upsert_instances(self, ontology_id, items):
        type(self).nodes += items

    def upsert_relations(self, ontology_id, edges):
        type(self).edges += edges

    def delete_graph(self, graph_id):
        return None


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setattr(runner, "FalkorDBService", Recorder)
    monkeypatch.setattr("app.services.v2.graph.falkordb_service.FalkorDBService", Recorder)
    return Recorder


def shop(declare: bool = False) -> Bundle:
    customers = pd.DataFrame({"customer_id": ["c1", "c2", "c3", "c4"], "name": ["Ada Li", "Bo Chen", "Cy Wu", "Di Xu"], "city": ["Beijing", "Shanghai", "Beijing", "Xi'an"]})
    orders = pd.DataFrame({"order_id": [1, 2, 3, 4, 5, 6], "customer_id": ["c1", "c1", "c2", "c3", "c3", "c4"], "total": [10.0, 20.0, 5.5, 7.0, 99.0, 12.0]})
    totals = duck.connect(orders=orders).execute("SELECT customer_id, SUM(total) AS spent FROM orders GROUP BY customer_id").df()
    spend = Table(name="spend", frame=totals, type_name="CustomerSpend", label="客户消费", description="每个客户的消费合计", primary_key=["customer_id"],
                  derived=Derived(sql="SELECT customer_id, SUM(total) AS spent FROM orders GROUP BY customer_id", inputs=["orders"],
                                  columns={"customer_id": ["orders.customer_id"], "spent": ["orders.total"]}, description="按客户汇总订单"))
    tables = [Table(name="customers", frame=customers, type_name="Customer", label="客户", source="shop", primary_key=["customer_id"] if declare else []),
              Table(name="orders", frame=orders, type_name="Order", label="订单", source="shop", constraints={"total": {"minimum": 0}}), spend]
    return Bundle(id="test-shop", title="小店订单", summary="客户和订单", domain="零售", kind="relational", ontology_name="小店订单（测试）",
                  sources=[Source(id="shop", title="shop.csv", kind="file")], tables=tables)


# ---- keys and links ------------------------------------------------------------------------------------------------

def test_keys_and_links_are_inferred_the_way_a_person_would():
    bundle = shop()
    report = profile.finalize(bundle)
    assert bundle.table("customers").primary_key == ["customer_id"] and bundle.table("orders").primary_key == ["order_id"]
    link = next(l for l in bundle.links if l.source_table == "orders")
    assert (link.source_column, link.target_table, link.target_column, link.basis) == ("customer_id", "customers", "customer_id", "semantic_fk") and link.coverage == 1.0
    assert report["links"] == len(bundle.links) and any("推断" in note for note in report["notes"])


def test_a_declared_key_that_does_not_hold_is_replaced_and_said_so():
    bundle = shop(declare=True)
    bundle.table("customers").frame.loc[3, "customer_id"] = "c1"  # now a duplicate
    bundle.table("customers").primary_key = ["customer_id"]
    report = profile.finalize(bundle)
    assert bundle.table("customers").primary_key != ["customer_id"] and any("不成立" in note for note in report["notes"])


def test_a_link_declared_the_other_way_round_is_not_inferred_again():
    bundle = shop(declare=True)
    bundle.links = [Link("customers_to_orders", "customers", "customer_id", "orders", "customer_id", "declared_fk")]
    profile.finalize(bundle)
    names = [(l.source_table, l.target_table) for l in bundle.links]
    assert ("customers", "orders") in names and ("orders", "customers") not in names, "the same pair of columns is not linked twice, once in each direction"


def test_stage_and_bridge_tables_get_no_key_and_no_inferred_link():
    bundle = shop()
    bundle.tables.append(Table(name="raw", frame=pd.DataFrame({"customer_id": ["c1", "c2"]}), type_name="Raw", label="原始", kind="stage"))
    bundle.tables.append(Table(name="pairs", frame=pd.DataFrame({"a": ["c1"], "b": ["c2"]}), type_name="Pair", label="配对", kind="bridge"))
    profile.finalize(bundle)
    assert bundle.table("raw").primary_key == [] and bundle.table("pairs").primary_key == []
    assert all(l.source_table not in ("raw", "pairs") for l in bundle.links)


def test_the_interchange_document_lists_tables_terms_and_foreign_keys():
    bundle = shop()
    profile.finalize(bundle)
    bundle.tables.append(Table(name="raw", frame=pd.DataFrame({"x": [1]}), type_name="Raw", label="原始", kind="stage"))
    document = yaml.safe_load(schema.interchange(bundle))
    text = yaml.safe_dump(document, allow_unicode=True)
    assert "customers" in text and "orders" in text and "raw" not in text.replace("customer", ""), "a stage table is in the lineage, not in the ontology"
    assert "Customer" in text and "Order" in text


# ---- an import -----------------------------------------------------------------------------------------------------

def test_an_import_creates_an_ontology_with_objects_and_relationships(db, admin_user, graph):
    job = runner.Job(id="t", title="t")
    result = runner.run(db, admin_user, shop, replace=False, job=job)
    assert result["types"] == 3 and result["objects"] == 4 + 6 + 4 and result["link_instances"] >= 6
    assert [s["status"] for s in job.steps] == ["done"] * len(runner.STEPS), "every step is reported, the lineage step included"
    project = db.get(OntologyProject, result["ontology_id"])
    assert project.build_mode == "data_import" and project.status == "created"
    entities = ontology_metadata.type_entities(db, project.id)
    assert set(entities) == {"Customer", "Order", "CustomerSpend"}
    assert db.query(EntityInstance).filter(EntityInstance.ontology_id == project.id).count() == 14
    assert len(graph.nodes) == 14 and len(graph.edges) >= 6
    definition = next(d for d in entities["Order"].properties["property_definitions"] if d["id"] == "total")
    assert definition["minimum"] == 0, "a rule the steward states lands in the property definition"
    assert next(n for n in graph.nodes if n["entity_type"] == "Customer")["id"].startswith(project.id[:8] + ":Customer:")


def test_importing_the_same_name_again_needs_replace(db, admin_user, graph):
    first = runner.run(db, admin_user, shop, replace=False)
    with pytest.raises(install.ImportConflict):
        runner.run(db, admin_user, shop, replace=False)
    second = runner.run(db, admin_user, shop, replace=True)
    assert second["ontology_id"] != first["ontology_id"] and db.get(OntologyProject, first["ontology_id"]) is None
    assert db.query(OntologyProject).filter(OntologyProject.name == "小店订单（测试）").count() == 1


def test_the_lineage_of_an_import_runs_from_the_source_to_the_types(db, admin_user, graph):
    result = runner.run(db, admin_user, shop, replace=False)
    assert result["lineage"] and result["lineage"]["events"] > 0
    drawn = lineage.graph_from_db(db, source=ontology_lineage.source_tag(result["ontology_id"]))
    ids = [n["id"] for n in drawn["nodes"]]
    assert any(i.endswith(":Customer") or ":Customer" in i for i in ids) and any("spend" in i.lower() or "CustomerSpend" in i for i in ids)
    columns = {(e.get("source"), e.get("target")) for e in drawn.get("column_edges", [])}
    assert columns, "the derived table says which columns each of its columns is computed from"


def test_a_failing_lineage_does_not_fail_the_import(db, admin_user, graph, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(ontology_lineage, "generate", broken)
    job = runner.Job(id="t", title="t")
    result = runner.run(db, admin_user, shop, replace=False, job=job)
    assert result["lineage"] is None and result["objects"] == 14
    step = next(s for s in job.steps if s["key"] == "lineage")
    assert step["status"] == "failed" and "boom" in step["detail"]


def test_an_import_that_is_too_big_is_refused_before_anything_is_written(db, admin_user, graph):
    bundle = shop()
    bundle.tables += [Table(name=f"t{i}", frame=pd.DataFrame({f"t{i}_id": [1, 2]}), type_name=f"T{i}", label=f"表{i}") for i in range(60)]
    with pytest.raises(install.ImportTooLarge):
        runner.run(db, admin_user, lambda: bundle, replace=False)
    assert db.query(OntologyProject).filter(OntologyProject.name == bundle.ontology_name).count() == 0


# ---- the prepared demos --------------------------------------------------------------------------------------------

def test_the_catalog_has_five_demos_of_different_kinds_and_none_of_them_is_the_old_person_master():
    entries = catalog.ENTRIES
    assert len(entries) == 5 and len({e.kind for e in entries}) >= 4
    assert not any("Splink" in e.title or "人员主数据" in e.title for e in entries)
    assert {e.id for e in entries} == {"demo-tpch", "demo-nyc-taxi", "demo-brick-site-b", "demo-abt-buy", "demo-beans"}
    assert catalog.get("demo-abt-buy") is not None and catalog.get("nope") is None


def test_the_event_demo_loads_a_first_slice_and_steps_forward(db, admin_user, graph):
    result = runner.run(db, admin_user, catalog.get("demo-nyc-taxi").load, replace=False)
    assert result["replay"] is True
    trips = db.query(EntityInstance).join(Entity, Entity.id == EntityInstance.entity_id).filter(Entity.ontology_id == result["ontology_id"], Entity.name_en == "Trip").count()
    assert trips == 400
    stepped = runner.advance(db, admin_user, result["ontology_id"], 250)
    assert stepped["installed"] == 250 and stepped["cursor"] == 650 and stepped["link_instances"] > 0
    trips = db.query(EntityInstance).join(Entity, Entity.id == EntityInstance.entity_id).filter(Entity.ontology_id == result["ontology_id"], Entity.name_en == "Trip").count()
    assert trips == 650


def test_a_demo_with_a_gold_answer_carries_the_columns_entity_resolution_needs(db, admin_user, graph):
    bundle = catalog.get("demo-abt-buy").load()
    profile.finalize(bundle)
    products = bundle.table("products").frame
    assert {"name", "model_no", "catalog_id", "true_entity", "price"} <= set(products.columns)
    assert products["catalog_id"].nunique() == 2 and products["model_no"].notna().mean() > 0.7
    sizes = products["true_entity"].value_counts()
    assert (sizes >= 2).sum() > 900, "most products have a counterpart in the other shop"


# ---- uploads: any kind of data -------------------------------------------------------------------------------------

def test_a_zip_is_expanded_into_its_files():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a/people.csv", "id,name\n1,Ada\n2,Bo\n")
        archive.writestr("a/photo.png", b"\x89PNG\r\n\x1a\n")
    names = [name for name, _ in uploads.expand([("bundle.zip", buffer.getvalue())])]
    assert sorted(names) == ["a/people.csv", "a/photo.png"] or sorted(n.split("/")[-1] for n in names) == ["people.csv", "photo.png"]


def test_any_table_format_is_read_and_column_names_are_cleaned():
    csv = uploads.read_table("People Data.csv", "Full Name,Birth Year\nAda,1815\nBo,1990\n".encode())
    assert csv and csv[0][0] == "People Data"
    cleaned, labels = uploads.clean_columns(csv[0][1])
    assert list(cleaned.columns) == ["full_name", "birth_year"] and labels["full_name"] == "Full Name", "the property id is snake_case, the original header stays as the label"
    lines = uploads.read_table("events.jsonl", b'{"id": 1, "kind": "start"}\n{"id": 2, "kind": "stop"}\n')
    assert lines and len(lines[0][1]) == 2
    with pytest.raises(uploads.UploadError):
        uploads.read_table("broken.csv", b"")


def test_media_files_of_any_kind_become_asset_objects():
    files = [("scans/leaf_001.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32), ("scans/note.txt", b"hello"), ("audio/call.wav", b"RIFF" + b"0" * 40), ("people.csv", b"id,name\n1,Ada\n2,Bo\n")]
    bundle = uploads.build("u1", "混合上传", "混合上传（测试）", files, None)
    assets = [t for t in bundle.tables if t.kind == "asset"]
    assert assets and uploads.ASSET_TYPE == assets[0].type_name
    kinds = set(assets[0].frame["media_kind"])
    assert {"image", "audio"} <= kinds, "nothing is limited to one industry's kinds of data"
    assert any(t.kind == "entity" and t.name == "people" for t in bundle.tables)
    preview = uploads.preview(bundle)
    assert preview["tables"] and preview["assets"].get("image") == 1 and preview["assets"].get("audio") == 1
