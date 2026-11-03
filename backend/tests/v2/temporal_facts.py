"""Compare what temporal runs publish, independent of how their events arrived."""
import json

from app.models.v2.temporal_replay import TemporalFact


def published_facts(db, run):
    """The facts a run published, as comparable tuples."""
    rows = db.query(TemporalFact).filter(TemporalFact.replay_id == run.id).all()
    return sorted(
        (fact.subject_id, fact.predicate, fact.object_id,
         json.dumps(fact.object_value, sort_keys=True, default=str), fact.state_key,
         str(fact.valid_from_ordinal), str(fact.valid_to_ordinal), fact.status)
        for fact in rows
    )
