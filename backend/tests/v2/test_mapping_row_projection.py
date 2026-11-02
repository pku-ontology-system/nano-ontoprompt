import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.ontology import OntologyProject
from app.models.v2.curated import CuratedDataset, CuratedReview
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.mapping import OntologyMapping
from app.models.v2.mapping_apply_run import MappingApplyRun
from app.models.v2.mapping_row_change import MappingRowChange, MappingRowProjectionEvent
from app.services.v2.mapping_row_projection import project_mapping_row_changes


def _projection_event():
    return MappingRowProjectionEvent(
        id="projection-event-1",
        attempt_id="projection-attempt-1",
        event_type="started",
        row_change_id="row-change-1",
        mapping_apply_run_id="apply-run-1",
        mapping_id="mapping-1",
        ontology_id="ontology-1",
        object_id="object-1",
        attempt_number=1,
        attempt_kind="automatic",
        actor_kind="system",
        created_at=datetime.now(timezone.utc),
    )


def _projection_migration():
    path = Path(__file__).resolve().parents[2] / (
        "alembic/versions/2026_10_24_0065_mapping_projection_event_immutability.py"
    )
    spec = importlib.util.spec_from_file_location("mapping_projection_immutability", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _projection_run(db, ontology, *, rows):
    project = db.get(OntologyProject, ontology["id"])
    dataset = Dataset(id="projection-dataset", name="Projection source", kind="structured")
    version = DatasetVersion(
        id="projection-version", dataset_id=dataset.id, version_no=1,
        rowcount=len(rows), storage_uri="s3://test/projection.csv", checksum="a" * 64,
    )
    curated = CuratedDataset(
        id="projection-curated", name="Projection source", status="approved",
    )
    review = CuratedReview(
        id="projection-review", curated_dataset_id=curated.id, dataset_version_id=version.id,
        status="approved",
    )
    mapping = OntologyMapping(
        id="projection-mapping", ontology_id=project.id, curated_dataset_id=curated.id,
        entity_class="Supplier", field_mapping={"name": "supplier_name"}, status="active",
    )
    run = MappingApplyRun(
        id="projection-run", mapping_id=mapping.id, ontology_id=project.id,
        curated_dataset_id=curated.id, review_id=review.id, dataset_version_id=version.id,
        mapping_snapshot_json={"entity_class": "Supplier"}, mapping_digest="b" * 64,
        idempotency_key="projection-run-key", status="completed",
    )
    db.add_all([dataset, version, curated, review, mapping, run])
    for object_id, properties, attempts in rows:
        db.add(MappingRowChange(
            mapping_apply_run_id=run.id,
            mapping_id=mapping.id,
            ontology_id=project.id,
            object_id=object_id,
            row_identity=f"identity:{object_id}",
            dataset_version_id=version.id,
            operation="upsert",
            after_row_data=properties,
            projection_status="pending",
            projection_attempts=attempts,
        ))
    db.commit()
    return run


def test_mapping_row_projection_events_are_append_only_in_orm(db):
    row = _projection_event()
    event_id = row.id
    db.add(row)
    db.commit()

    row.error_message = "tampered"
    with pytest.raises(ValueError, match="append-only"):
        db.flush()
    db.rollback()

    db.delete(db.get(MappingRowProjectionEvent, event_id))
    with pytest.raises(ValueError, match="append-only"):
        db.flush()
    db.rollback()


def test_mapping_projection_failure_isolated_per_object(db, ontology, monkeypatch):
    run = _projection_run(db, ontology, rows=[
        ("good-object", {"supplier_name": "Good"}, 0),
        ("bad-object", {"supplier_name": "Bad"}, 0),
    ])

    class Graph:
        available = True

        def upsert_instances(self, _ontology_id, nodes):
            if any(node["id"] == "bad-object" for node in nodes):
                raise ConnectionError("graph unavailable")
            return len(nodes)

        def upsert_relations(self, _ontology_id, relations):
            return len(relations)

    monkeypatch.setattr(
        "app.services.v2.graph.falkordb_service.FalkorDBService", Graph,
    )

    result = project_mapping_row_changes(db, mapping_apply_run_id=run.id)

    assert result == {
        "selected": 2, "projected": 1, "failed": 1, "pending": 1,
        "dead_letter": 0, "replayed": 0,
    }
    by_object = {
        row.object_id: row.projection_status
        for row in db.query(MappingRowChange).filter_by(mapping_apply_run_id=run.id).all()
    }
    assert by_object == {"good-object": "projected", "bad-object": "pending"}
    finished = db.query(MappingRowProjectionEvent).filter_by(
        mapping_apply_run_id=run.id, event_type="finished",
    ).all()
    assert {event.object_id: event.outcome for event in finished} == {
        "good-object": "projected", "bad-object": "retry_wait",
    }


def test_mapping_projection_dead_letter_and_manual_retry_are_separate_and_idempotent(
    db, ontology, admin_user, monkeypatch,
):
    run = _projection_run(db, ontology, rows=[("manual-object", {"supplier_name": "Manual"}, 7)])

    class Graph:
        available = True
        fail = True

        def upsert_instances(self, _ontology_id, nodes):
            if self.fail:
                raise ConnectionError("graph unavailable")
            return len(nodes)

        def upsert_relations(self, _ontology_id, relations):
            return len(relations)

    graph = Graph()
    monkeypatch.setattr(
        "app.services.v2.graph.falkordb_service.FalkorDBService", lambda: graph,
    )

    automatic = project_mapping_row_changes(db, mapping_apply_run_id=run.id)
    row = db.query(MappingRowChange).filter_by(mapping_apply_run_id=run.id).one()
    assert automatic["dead_letter"] == 1
    assert row.projection_status == "dead_letter"
    assert row.projection_attempts == 8
    assert project_mapping_row_changes(db, mapping_apply_run_id=run.id)["selected"] == 0

    graph.fail = False
    manual = project_mapping_row_changes(
        db,
        mapping_apply_run_id=run.id,
        manual_actor_id=admin_user.id,
        manual_reason="图服务恢复后由管理员核验并重放",
        manual_request_id="manual-projection-1",
    )
    row = db.query(MappingRowChange).filter_by(mapping_apply_run_id=run.id).one()
    assert manual["projected"] == 1
    assert row.projection_status == "projected"
    assert row.projection_attempts == 8
    assert row.projection_manual_attempts == 1

    replay = project_mapping_row_changes(
        db,
        mapping_apply_run_id=run.id,
        manual_actor_id=admin_user.id,
        manual_reason="图服务恢复后由管理员核验并重放",
        manual_request_id="manual-projection-1",
    )
    assert replay["replayed"] is True
    assert replay["projected"] == 1
    assert db.query(MappingRowProjectionEvent).filter_by(
        mapping_apply_run_id=run.id, actor_id=admin_user.id,
        client_request_id="manual-projection-1", event_type="started",
    ).count() == 1


def test_sqlite_migration_blocks_direct_event_update_delete_and_history_loss():
    migration = _projection_migration()
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "CREATE TABLE v2_mapping_row_projection_events "
                "(id TEXT PRIMARY KEY, error_message TEXT)"
            ))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            connection.execute(sa.text(
                "INSERT INTO v2_mapping_row_projection_events (id, error_message) "
                "VALUES ('event-1', 'original')"
            ))
            for statement in (
                "UPDATE v2_mapping_row_projection_events SET error_message='tampered' WHERE id='event-1'",
                "DELETE FROM v2_mapping_row_projection_events WHERE id='event-1'",
            ):
                with pytest.raises(sa.exc.DatabaseError):
                    connection.execute(sa.text(statement))
            assert connection.execute(sa.text(
                "SELECT error_message FROM v2_mapping_row_projection_events WHERE id='event-1'"
            )).scalar_one() == "original"
            with pytest.raises(RuntimeError, match="would erase audit evidence"):
                migration.downgrade()
    finally:
        engine.dispose()


def test_sqlite_migration_can_downgrade_when_projection_history_is_empty():
    migration = _projection_migration()
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "CREATE TABLE v2_mapping_row_projection_events "
                "(id TEXT PRIMARY KEY, error_message TEXT)"
            ))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            migration.downgrade()
            trigger_names = {
                row[0] for row in connection.execute(sa.text(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )).all()
            }
            assert "trg_mapping_row_projection_event_no_update" not in trigger_names
            assert "trg_mapping_row_projection_event_no_delete" not in trigger_names
    finally:
        engine.dispose()
