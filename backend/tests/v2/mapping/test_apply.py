"""MappingService.apply_mapping 单元测试"""
import pytest
from unittest.mock import MagicMock, patch
from app.services.v2.mapping.mapping_service import MappingService
from app.models.v2.mapping import OntologyMapping


def make_mapping_obj(field_mapping=None):
    m = OntologyMapping(
        id="map-1",
        ontology_id="ont-1",
        curated_dataset_id="ds-1",
        entity_class="Order",
        field_mapping=field_mapping or {
            "order_id": "id",
            "customer_name": "customerName",
            "__primary_key__": "order_id",
        },
        status="draft",
        confidence=0.9,
    )
    return m


def make_db(mapping_obj):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = mapping_obj
    db.commit = MagicMock()
    return db


DATA = [
    {"order_id": "ORD-001", "customer_name": "Alice", "amount": "1200"},
    {"order_id": "ORD-002", "customer_name": "Bob",   "amount": "800"},
]


def test_apply_mapping_returns_summary():
    db = make_db(make_mapping_obj())
    svc = MappingService(db)
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_neo4j:
        mock_neo4j.driver.side_effect = Exception("offline")
        result = svc.apply_mapping("map-1", DATA)
    assert result["total_rows"] == 2
    assert result["entity_class"] == "Order"
    assert result["mapping_id"] == "map-1"


def test_apply_mapping_updates_status():
    m = make_mapping_obj()
    db = make_db(m)
    svc = MappingService(db)
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_neo4j:
        mock_neo4j.driver.side_effect = Exception("offline")
        svc.apply_mapping("map-1", DATA)
    assert m.status == "applied"
    db.commit.assert_called()


def test_apply_mapping_not_found_raises():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    svc = MappingService(db)
    with pytest.raises(ValueError, match="not found"):
        svc.apply_mapping("nonexistent", DATA)


def test_apply_mapping_empty_data():
    db = make_db(make_mapping_obj())
    svc = MappingService(db)
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_neo4j:
        mock_neo4j.driver.side_effect = Exception("offline")
        result = svc.apply_mapping("map-1", [])
    assert result["total_rows"] == 0


def test_rows_to_instances_upserts_stable_identity_and_advances_source_version():
    from app.models.entity_instance import EntityInstance

    db = MagicMock()
    svc = MappingService(db)
    mapping = make_mapping_obj()
    row_v1 = {"order_id": "ORD-001", "customer_name": "Alice"}
    row_v2 = {"order_id": "ORD-001", "customer_name": "Alice Chen"}

    svc._rows_to_instances(mapping, "concept-order", [row_v1], source_version_id="version-1")
    first = next(
        call.args[0] for call in db.merge.call_args_list
        if isinstance(call.args[0], EntityInstance)
    )
    db.merge.reset_mock()

    svc._rows_to_instances(mapping, "concept-order", [row_v2], source_version_id="version-2")
    second = next(
        call.args[0] for call in db.merge.call_args_list
        if isinstance(call.args[0], EntityInstance)
    )

    assert second.id == first.id
    assert second.row_data["customerName"] == "Alice Chen"
    assert second.source_version == "version-2"


def test_create_mapping_saves_to_db():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    svc = MappingService(db)
    svc.create_mapping(
        ontology_id="ont-1",
        curated_dataset_id="ds-1",
        entity_class="Order",
        field_mapping={"order_id": "id"},
    )
    db.add.assert_called_once()
    db.commit.assert_called_once()


def test_create_mapping_persists_primary_key_column():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    svc = MappingService(db)

    mapping = svc.create_mapping(
        ontology_id="ont-1",
        curated_dataset_id="ds-1",
        entity_class="Order",
        field_mapping={"order_id": "order_id"},
        primary_key_column="order_id",
    )

    assert mapping.field_mapping["__primary_key__"] == "order_id"


def test_approved_mapping_schema_gate_reports_new_columns_without_mutating_mapping():
    from app.models.entity_instance import EntityInstance

    mapping = make_mapping_obj({"order_id": "id", "customer_name": "customerName"})
    original = dict(mapping.field_mapping)
    db = make_db(mapping)
    db.query.return_value.filter.return_value.limit.return_value.all.return_value = []
    svc = MappingService(db)

    view, report = svc._approved_mapping_view(mapping, [
        {"order_id": "ORD-001", "customer_name": "Alice", "new_source_field": "ignored"},
        {"order_id": "ORD-002", "customer_name": "Bob", "new_source_field": "ignored"},
    ])

    assert view.field_mapping["__primary_key__"] == "order_id"
    assert report["unmapped_columns"] == ["new_source_field"]
    assert len(report["schema_fingerprint"]) == 64
    assert mapping.field_mapping == original
    db.query.assert_any_call(EntityInstance)


def test_approved_mapping_schema_gate_rejects_missing_mapped_source_column():
    mapping = make_mapping_obj()
    svc = MappingService(make_db(mapping))

    with pytest.raises(ValueError, match="SCHEMA_DRIFT_MISSING_COLUMNS.*customer_name"):
        svc._approved_mapping_view(mapping, [{"order_id": "ORD-001"}])


def test_approved_mapping_schema_gate_rejects_unstable_identity_key():
    mapping = make_mapping_obj({"customer_name": "customerName", "__primary_key__": "__row_hash__"})
    svc = MappingService(make_db(mapping))

    with pytest.raises(ValueError, match="SCHEMA_GATE_UNSTABLE_PRIMARY_KEY"):
        svc._approved_mapping_view(mapping, [{"customer_name": "Alice"}])


def test_display_name_uses_order_line_identity():
    svc = MappingService(MagicMock())
    mapping = make_mapping_obj()
    mapping.entity_class = "PurchaseItem"
    row = {"order_id": "PO-2024-0001", "items.sku": "STL-001", "供应商名称": "天钢原材料有限公司"}

    assert svc._display_name(mapping, row, "__row_hash__", 0) == "PO-2024-0001 / STL-001"


def test_display_name_uses_order_id_for_order_entity():
    svc = MappingService(MagicMock())
    mapping = make_mapping_obj()
    mapping.entity_class = "SupplierOrders"
    row = {"order_id": "PO-2024-0001", "items.sku": "STL-001", "items.name": "钢材Q235"}

    assert svc._display_name(mapping, row, "__row_hash__", 0) == "PO-2024-0001"


def test_rows_to_entities_merges_duplicate_order_primary_key():
    mapping = make_mapping_obj({
        "order_id": "order_id",
        "items.sku": "items_sku",
        "__primary_key__": "order_id",
    })
    mapping.entity_class = "SupplierOrders"
    svc = MappingService(MagicMock())

    entities = svc._rows_to_entities(mapping, [
        {"order_id": "PO-2024-0001", "items.sku": "STL-001"},
        {"order_id": "PO-2024-0001", "items.sku": "STL-002"},
    ])

    assert len(entities) == 1
    assert entities[0]["name_cn"] == "PO-2024-0001"
    assert entities[0]["source_row_count"] == 2


def test_display_name_uses_inventory_transaction_identity():
    svc = MappingService(MagicMock())
    mapping = make_mapping_obj()
    row = {"日期": "2026-03-08", "物料编码": "MAT001", "操作类型": "出库", "所在仓库": "仓库C"}

    assert svc._display_name(mapping, row, "__row_hash__", 11) == "2026-03-08 / MAT001 / 出库 / 仓库C #12"


def test_display_name_uses_supplier_code_before_name_only():
    svc = MappingService(MagicMock())
    mapping = make_mapping_obj()
    row = {"供应商ID": "SUP001", "供应商名称": "天钢原材料有限公司"}

    assert svc._display_name(mapping, row, "供应商ID", 0) == "SUP001 / 天钢原材料有限公司"


def test_normalize_mapping_adds_property_metadata():
    mapping = make_mapping_obj({"order_id": "order_id"})
    svc = MappingService(make_db(mapping))

    svc._normalize_mapping(mapping, [
        {"order_id": "O-1", "amount": "12.5", "created_at": "2026-01-01", "markdown_text": "raw"},
    ])

    props = {p["column"]: p for p in mapping.field_mapping["__properties__"]}
    assert props["amount"]["type"] == "float"
    assert props["created_at"]["type"] == "timestamp"
    assert props["markdown_text"]["hidden"] is True


def test_rows_to_entities_skips_hidden_technical_properties():
    mapping = make_mapping_obj({
        "order_id": "order_id",
        "markdown_text": "markdown_text",
        "__primary_key__": "order_id",
        "__properties__": [
            {"column": "order_id", "property": "order_id", "hidden": False},
            {"column": "markdown_text", "property": "markdown_text", "hidden": True},
        ],
    })
    svc = MappingService(MagicMock())

    entities = svc._rows_to_entities(mapping, [{"order_id": "O-1", "markdown_text": "large raw text"}])

    assert entities[0]["order_id"] == "O-1"
    assert "markdown_text" not in entities[0]


def test_rows_to_entities_uses_row_instance_names():
    mapping = OntologyMapping(
        id="map-supplier",
        ontology_id="ont-1",
        curated_dataset_id="ds-1",
        entity_class="Supplier",
        field_mapping={
            "供应商ID": "supplier_id",
            "供应商名称": "supplier_name",
            "__primary_key__": "供应商ID",
        },
        status="draft",
        confidence=0.9,
    )
    svc = MappingService(MagicMock())

    entities = svc._rows_to_entities(mapping, [{"供应商ID": "SUP001", "供应商名称": "天钢原材料有限公司"}])

    assert entities[0]["display_name"] == "SUP001 / 天钢原材料有限公司"
    assert entities[0]["name_cn"] == "SUP001 / 天钢原材料有限公司"
    assert entities[0]["name_en"] == "SUP001 / 天钢原材料有限公司"
    assert entities[0]["name_en"] != mapping.entity_class


def test_display_name_uses_route_c_record_identity_before_filename():
    svc = MappingService(MagicMock())
    mapping = make_mapping_obj()
    row = {
        "filename": "supply_chain_strategy.md",
        "source_file": "supply_chain_strategy.md",
        "record_id": "supply_chain_strategy.md:section:2",
        "row_type": "section",
        "section_index": 2,
        "section_title": "供应商风险管理",
    }

    assert svc._display_name(mapping, row, "__row_hash__", 1) == (
        "supply_chain_strategy.md / 供应商风险管理 #2"
    )


def test_llm_detect_fk_accepts_links_object():
    svc = MappingService(MagicMock())
    payload = '{"links":[{"column":"供应商","relation_type":"HAS_SUP"}]}'

    with patch("app.services.model_config_selector.select_llm_model_config", return_value=object()), \
         patch("app.services.model_config_selector.llm_call_kwargs", return_value={
             "provider": "compatible",
             "api_key": "test-key",
             "api_base": "https://api.deepseek.com",
             "model": "deepseek-v4-flash",
         }), \
         patch("app.services.llm_service._call_llm", return_value=payload):
        result = svc._llm_detect_fk(["供应商", "数量"], "Supplier", "supplier_database")

    assert result == [("供应商", "HAS_SUP")]
