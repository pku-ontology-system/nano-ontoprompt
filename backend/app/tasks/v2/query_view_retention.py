"""Bounded, reference-aware cleanup for expired immutable query graphs."""
from __future__ import annotations

from app.tasks.celery_app import celery_app


@celery_app.task(name="v2.query_view_retention")
def cleanup_expired_query_views(batch_size: int = 25) -> dict:
    """Delete expired graph snapshots while retaining their SQL manifests and audit."""
    from app.database import SessionLocal
    from app.models.v2.query_view import QueryDataView, QueryViewRetentionEvent
    from app.models.v2.scenario import ScenarioResource, ScenarioRevision
    from app.services.v2.graph.falkordb_service import FalkorDBService
    from app.services.v2.object_query.data_views import aware, now, retention_pin_reason

    batch_size = min(max(int(batch_size), 1), 100)
    run_at = now()
    graph = FalkorDBService()
    if not graph.available:
        return {"state": "unavailable", "scanned": 0, "deleted": 0,
                "pinned": 0, "failed": 0}

    counts = {"scanned": 0, "deleted": 0, "pinned": 0,
              "not_due": 0, "failed": 0}
    with SessionLocal() as db:
        candidate_ids = [row.id for row in db.query(QueryDataView.id).filter(
            QueryDataView.retention_until.isnot(None),
            QueryDataView.retention_until <= run_at,
            QueryDataView.status.in_(["ready", "expired"]),
        ).order_by(QueryDataView.retention_until.asc(), QueryDataView.id.asc())
            .limit(batch_size).all()]

        for view_id in candidate_ids:
            # Match Scenario mutation paths' lock order (Scenario, then view).
            # This prevents a TTL extension or revision publish from racing the
            # external graph delete after the pin check.
            scenario_ids = {
                row.id for row in db.query(ScenarioResource.id).filter(
                    ScenarioResource.base_view_id == view_id,
                ).all()
            }
            scenario_ids.update(
                row.scenario_id for row in db.query(ScenarioRevision.scenario_id).filter(
                    ScenarioRevision.result_view_id == view_id,
                ).all()
            )
            if scenario_ids:
                (db.query(ScenarioResource)
                 .filter(ScenarioResource.id.in_(scenario_ids))
                 .order_by(ScenarioResource.id.asc())
                 .populate_existing().with_for_update().all())
            view = (db.query(QueryDataView).filter_by(id=view_id)
                    .with_for_update(skip_locked=True).first())
            if view is None:
                continue
            counts["scanned"] += 1
            if (view.status not in {"ready", "expired"}
                    or view.retention_until is None or aware(view.retention_until) > run_at):
                counts["not_due"] += 1
                db.commit()
                continue

            existing = db.query(QueryViewRetentionEvent).filter_by(
                view_id=view.id, event_type="graph_deleted",
            ).first()
            if existing:
                view.status = "expired"
                db.commit()
                continue

            pin_reason = retention_pin_reason(db, view, at=run_at)
            if pin_reason:
                counts["pinned"] += 1
                db.commit()
                continue

            # The graph key is generated internally from the immutable view ID.
            # A failed delete leaves both the status and audit untouched so a
            # later pass can retry; a process crash after delete is safe because
            # FalkorDB graph deletion is idempotent.
            if not graph.delete_graph(view.graph_key):
                counts["failed"] += 1
                db.rollback()
                continue

            view.status = "expired"
            db.add(QueryViewRetentionEvent(
                ontology_id=view.ontology_id,
                view_id=view.id,
                event_type="graph_deleted",
                reason="retention_expired",
                content_digest=view.content_digest,
                object_count=view.object_count,
                edge_count=view.edge_count,
                retention_until=view.retention_until,
                details={
                    "result": "deleted_or_already_absent",
                    "pinned_references_checked": [
                        "active_scenarios", "scenario_studies", "scenario_runs",
                        "retained_child_views",
                    ],
                },
                created_at=run_at,
            ))
            db.commit()
            counts["deleted"] += 1

    return {"state": "completed", **counts}
