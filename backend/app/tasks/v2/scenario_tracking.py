"""Periodic Tracking Scenario refresh; Pinned resources are never selected."""
from datetime import timedelta
from app.tasks.celery_app import celery_app


@celery_app.task(name='scenario_tracking.rebase_due')
def rebase_due():
    from app.database import SessionLocal
    from app.models.user import User
    from app.models.v2.scenario import ScenarioResource
    from app.services.v2.object_query.data_views import now, aware
    from app.services.v2.scenarios import ScenarioService, scenario_is_expired
    from app.services.v2.scenario_tracking import rebase
    processed = 0
    with SessionLocal() as db:
        rows = db.query(ScenarioResource).filter_by(mode='tracking', status='active', protected_demo=False).all()
        candidates = [(row.id, row.ontology_id, row.owner_id) for row in rows
                      if (not row.last_rebased_at or aware(row.last_rebased_at) <= now() - timedelta(minutes=10))
                      and (row.rebase_error or {}).get('code') != 'rebase_conflict'
                      and not scenario_is_expired(row)]
        for scenario_id, ontology_id, owner_id in candidates:
            owner = db.get(User, owner_id)
            if not owner or not owner.is_active:
                continue
            try:
                # The owner is the policy subject and Scenario ACL boundary;
                # the durable actor recorded by the service is the worker.
                service = ScenarioService(
                    db, ontology_id, owner, execution_actor_kind="system")
                service.authorize_ontology()
                row = service.get(scenario_id, write=True)
                rebase(service, scenario_id, row.etag)
                processed += 1
            except Exception as exc:
                db.rollback()
                row = db.get(ScenarioResource, scenario_id)
                if row:
                    row.rebase_error = {'code': getattr(exc, 'code', 'rebase_failed'), 'message': 'Automatic rebase failed; retry or review the Scenario'}
                    db.commit()
    return {'processed': processed}


@celery_app.task(name='scenario_tracking.expire_due')
def expire_due(batch_size: int = 200):
    """Archive TTL-expired scenarios and record one system retention event."""
    from app.database import SessionLocal
    from app.models.v2.scenario import ScenarioResource, ScenarioRetentionEvent
    from app.services.v2.object_query.data_views import now
    from app.services.v2.scenarios import scenario_expires_at

    run_at = now()
    processed = 0
    expired = 0
    with SessionLocal() as db:
        due = (db.query(ScenarioResource)
               .filter(ScenarioResource.ttl_seconds.isnot(None),
                       ScenarioResource.expired_at.is_(None),
                       ScenarioResource.expires_at <= run_at)
               .order_by(ScenarioResource.expires_at.asc())
               .limit(batch_size)
               .with_for_update(skip_locked=True).all())
        remaining = max(batch_size - len(due), 0)
        legacy = []
        if remaining:
            legacy = (db.query(ScenarioResource)
                      .filter(ScenarioResource.ttl_seconds.isnot(None),
                              ScenarioResource.expired_at.is_(None),
                              ScenarioResource.expires_at.is_(None))
                      .order_by(ScenarioResource.created_at.asc())
                      .limit(remaining)
                      .with_for_update(skip_locked=True).all())
        rows = due + legacy
        for scenario in rows:
            processed += 1
            expiry = scenario_expires_at(scenario)
            if expiry is None:
                continue
            if scenario.expires_at is None:
                scenario.expires_at = expiry
            if expiry > run_at:
                continue
            existing = db.query(ScenarioRetentionEvent).filter_by(
                scenario_id=scenario.id, event_type='expired').first()
            if existing:
                previous_status = scenario.status
                scenario.expired_at = existing.created_at
                scenario.status = 'archived'
                scenario.updated_at = existing.created_at
                if previous_status == 'active':
                    scenario.etag += 1
                continue
            previous_status = scenario.status
            previous_etag = scenario.etag
            scenario.expired_at = run_at
            scenario.status = 'archived'
            scenario.updated_at = run_at
            if previous_status == 'active':
                scenario.etag += 1
            db.add(ScenarioRetentionEvent(
                scenario_id=scenario.id,
                event_type='expired',
                expires_at=expiry,
                details={'source': 'ttl', 'ttl_seconds': scenario.ttl_seconds,
                         'previous_status': previous_status, 'previous_etag': previous_etag},
                created_at=run_at,
            ))
            expired += 1
        db.commit()
    return {'processed': processed, 'expired': expired}
