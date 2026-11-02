import logging
import os
from pathlib import Path
from threading import Lock
from time import monotonic, sleep

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from celery import Celery, Task
from celery.exceptions import Reject

from app.database import engine
from app.config import settings

logger = logging.getLogger(__name__)
_SCHEMA_CHECK_INTERVAL_SECONDS = 15.0
_SCHEMA_RETRY_BASE_SECONDS = 20.0
_SCHEMA_RETRY_MAX_SECONDS = 900.0
_SCHEMA_CHECK_LOCK = Lock()
_SCHEMA_CHECKED_UNTIL = 0.0
_SCHEMA_CHECK_FAILURE: str | None = None


class WorkerSchemaNotReady(RuntimeError):
    """Raised before a Celery task runs when its database contract is unsafe."""


def _verify_worker_schema() -> None:
    backend_dir = Path(__file__).resolve().parents[2]
    config = AlembicConfig(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    if os.environ.get("DATABASE_URL"):
        config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

    target_heads = sorted(ScriptDirectory.from_config(config).get_heads())
    if len(target_heads) != 1:
        raise RuntimeError("Expected exactly one Alembic head")
    with engine.connect() as connection:
        current_heads = sorted(MigrationContext.configure(connection).get_current_heads())
    if current_heads != target_heads:
        raise RuntimeError("Database revision does not match the checked-out Alembic head")
    command.check(config)


def ensure_worker_schema_ready() -> None:
    """Cache a read-only Alembic check and fail before task side effects on drift."""
    global _SCHEMA_CHECKED_UNTIL, _SCHEMA_CHECK_FAILURE
    now = monotonic()
    if now < _SCHEMA_CHECKED_UNTIL:
        if _SCHEMA_CHECK_FAILURE:
            raise WorkerSchemaNotReady("Worker schema verification failed; task was not run")
        return

    with _SCHEMA_CHECK_LOCK:
        now = monotonic()
        if now < _SCHEMA_CHECKED_UNTIL:
            if _SCHEMA_CHECK_FAILURE:
                raise WorkerSchemaNotReady("Worker schema verification failed; task was not run")
            return
        try:
            _verify_worker_schema()
        except Exception as exc:
            _SCHEMA_CHECK_FAILURE = type(exc).__name__
            _SCHEMA_CHECKED_UNTIL = monotonic() + _SCHEMA_CHECK_INTERVAL_SECONDS
            logger.error("Celery task blocked by schema verification failure (%s)", _SCHEMA_CHECK_FAILURE)
            raise WorkerSchemaNotReady("Worker schema verification failed; task was not run") from None
        _SCHEMA_CHECK_FAILURE = None
        _SCHEMA_CHECKED_UNTIL = monotonic() + _SCHEMA_CHECK_INTERVAL_SECONDS


def _wait_for_worker_schema_ready() -> None:
    retry_interval = _SCHEMA_CHECK_INTERVAL_SECONDS + 1.0
    while True:
        try:
            ensure_worker_schema_ready()
        except WorkerSchemaNotReady:
            sleep(retry_interval)
            continue
        return


def _wait_for_schema_and_broker_ready(task: Task) -> None:
    """Hold an early-acked task locally if retry publication reports failure."""
    retry_interval = _SCHEMA_CHECK_INTERVAL_SECONDS + 1.0
    while True:
        _wait_for_worker_schema_ready()

        try:
            with task.app.connection_for_write() as connection:
                connection.ensure_connection(max_retries=0)
        except Exception as exc:
            logger.error(
                "Celery task held because schema retry could not be published "
                "and broker is unavailable (%s)",
                type(exc).__name__,
            )
            sleep(retry_interval)
            continue
        return


class SchemaGuardedTask(Task):
    abstract = True
    # A deploy may leave a queued task waiting until the matching schema is
    # restored. Do not turn a pre-body schema rejection into a terminal task.
    max_retries = None

    def __call__(self, *args, **kwargs):
        try:
            ensure_worker_schema_ready()
        except WorkerSchemaNotReady as exc:
            if getattr(self.request, "called_directly", False):
                # Router fallbacks call task functions directly when Redis
                # publish fails. Do not invoke Task.retry outside a worker;
                # wait for schema readiness in this existing fallback thread.
                logger.warning("Direct Celery fallback held until schema verification succeeds")
                _wait_for_worker_schema_ready()
                return super().__call__(*args, **kwargs)

            retry_number = max(0, int(getattr(self.request, "retries", 0) or 0))
            countdown = min(
                _SCHEMA_RETRY_BASE_SECONDS * (2 ** min(retry_number, 6)),
                _SCHEMA_RETRY_MAX_SECONDS,
            )
            logger.warning(
                "Deferring Celery task until schema verification succeeds "
                "(retry=%s countdown_s=%s)",
                retry_number + 1,
                int(countdown),
            )
            # Schema drift is a deployment/readiness condition, not a business
            # failure. Schedule a capped-backoff retry with no retry limit; the
            # task body remains untouched until a later schema check passes.
            try:
                raise self.retry(exc=exc, countdown=countdown, max_retries=None)
            except Reject as publish_error:
                logger.error(
                    "Celery could not publish schema retry (%s)",
                    type(publish_error).__name__,
                )
                if self.acks_late:
                    # The original delivery remains unacked; return it to the
                    # broker instead of acknowledging a task that never ran.
                    raise Reject(publish_error, requeue=True) from None
                # Early-acked deliveries cannot be requeued. Keep this worker
                # slot until schema and broker recover, then resume this call.
                # A broker timeout may be ambiguous, so domain tasks still need
                # idempotency in case the retry message was accepted remotely.
                _wait_for_schema_and_broker_ready(self)
                return super().__call__(*args, **kwargs)
        return super().__call__(*args, **kwargs)


celery_app = Celery(
    "ontoprompt",
    broker=settings.redis_url,
    backend=settings.redis_url,
    task_cls=SchemaGuardedTask,
    include=[
        "app.tasks.extraction",
        "app.tasks.audit",
        "app.tasks.legacy_task_recovery",
        "app.tasks.v2.temporal_construction",
        "app.tasks.v2.connection_sync",
        "app.tasks.v2.workbench",
        "app.tasks.v2.object_query",
        "app.tasks.v2.scenario",
        "app.tasks.v2.scenario_tracking",
        "app.tasks.v2.action_projection",
        "app.tasks.v2.agent_sessions",
        "app.tasks.v2.mapping_apply",
        "app.tasks.v2.mapping_link_apply",
        "app.tasks.v2.pipeline_run",
        "app.tasks.v2.pipeline_schedule",
        "app.tasks.v2.temporal_replay",
        "app.tasks.v2.temporal_stream",
        "app.tasks.v2.temporal_vector_index",
        "app.tasks.v2.temporal_graph_projection",
        "app.tasks.v2.query_view_retention",
        "app.tasks.v2.scenario_graph_recovery",
    ],
)

# broker 不可用时快速失败 (默认会长时间重试, 导致 API 请求阻塞)
celery_app.conf.task_publish_retry = False
celery_app.conf.broker_connection_timeout = 3
celery_app.conf.broker_connection_retry_on_startup = True
celery_app.conf.beat_schedule = {
    'tracking-scenarios-every-ten-minutes': {'task': 'scenario_tracking.rebase_due', 'schedule': 600.0},
    'scenario-retention-expiry-every-ten-minutes': {
        'task': 'scenario_tracking.expire_due', 'schedule': 600.0,
    },
    'action-projection-reconcile-every-minute': {
        'task': 'v2.action_projection.reconcile', 'schedule': 60.0,
    },
    'mapping-apply-reconcile-every-minute': {
        'task': 'v2.mapping_apply.reconcile', 'schedule': 60.0,
    },
    'mapping-link-apply-reconcile-every-minute': {
        'task': 'v2.mapping_link_apply.reconcile', 'schedule': 60.0,
    },
    'mapping-link-projection-replay-reconcile-every-minute': {
        'task': 'v2.mapping_link_projection_replay.reconcile', 'schedule': 60.0,
    },
    'mapping-row-projection-reconcile-every-minute': {
        'task': 'v2.mapping_rows.reconcile', 'schedule': 60.0,
    },
    'agent-session-reconcile-every-minute': {
        'task': 'v2.agent_sessions.reconcile', 'schedule': 60.0,
    },
    'legacy-task-lease-reconcile-every-minute': {
        'task': 'legacy_tasks.reconcile', 'schedule': 60.0,
    },
    'pipeline-run-dispatch-reconcile-every-minute': {
        'task': 'v2.pipeline_run.reconcile', 'schedule': 60.0,
    },
    'pipeline-schedule-reconcile-every-minute': {
        'task': 'v2.pipeline_schedule.reconcile', 'schedule': 60.0,
    },
    'temporal-vector-index-reconcile-every-minute': {
        'task': 'v2.temporal_vector_index.reconcile', 'schedule': 60.0,
    },
    'temporal-graph-projection-reconcile-every-minute': {
        'task': 'v2.temporal_graph_projection.reconcile', 'schedule': 60.0,
    },
    'temporal-stream-worker-reconcile-every-minute': {
        'task': 'v2.temporal_stream.reconcile', 'schedule': 60.0,
    },
    'temporal-vector-index-retention-daily': {
        'task': 'v2.temporal_vector_index.retention', 'schedule': 86400.0,
    },
    'query-view-graph-retention-every-ten-minutes': {
        'task': 'v2.query_view_retention', 'schedule': 600.0,
    },
    'scenario-action-graph-recovery-every-minute': {
        'task': 'v2.scenario_graph_recovery.reconcile', 'schedule': 60.0,
    },
}
