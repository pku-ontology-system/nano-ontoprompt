"""One Auto Ontology compilation, run inside the Auto Ontology checkout's own interpreter.

    <checkout>/.venv/bin/python -I pipeline.py <run_dir>

``runner.py`` writes ``<run_dir>/request.json`` and starts this script with the model and
database settings in its environment.  The script keeps ``status.json`` current while it works
and leaves ``export.yaml`` and ``run.json`` behind.  Upstream files are not changed; the only
runtime shim sets the vector size to match the embedding model (upstream hard-codes 2048).

Each run uses a fresh store database, so results of earlier runs can never leak into this one.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

RUN_DIR = Path(sys.argv[1]).resolve()
REQUEST = json.loads((RUN_DIR / "request.json").read_text(encoding="utf-8"))
STATUS = common.read_json(RUN_DIR / "status.json", {})
LAST_WRITE = 0.0


def save(force: bool = False) -> None:
    global LAST_WRITE
    if force or time.monotonic() - LAST_WRITE > 0.4:
        common.write_json(RUN_DIR / "status.json", STATUS)
        LAST_WRITE = time.monotonic()


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return common.redact(super().format(record))


class Progress(logging.Handler):
    """Turns Auto Ontology's own log lines into stage and table progress."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.done_tables: set[str] = set()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.name == "httpx" and "/chat/completions" in message:
                STATUS["llm_calls"] = STATUS.get("llm_calls", 0) + 1
            stage, event = common.classify(record.name, message)
            if stage:
                before = STATUS.get("stage")
                common.advance(STATUS, stage)
                if event.get("total"):
                    STATUS["tables"]["total"] = event["total"]
                if event.get("done_table"):
                    self.done_tables.add(event["done_table"])
                    STATUS["tables"]["done"] = len(self.done_tables)
                if stage != before:
                    save(force=True)
            if record.name.startswith("auto_ontology") and not message.startswith("====="):
                STATUS["detail"] = common.redact(message)[:200]
            save()
        except Exception:  # progress reporting must never break the run
            pass


def set_stage(stage: str) -> None:
    common.advance(STATUS, stage)
    save(force=True)


def admin_connection(psycopg, database: str = "postgres"):
    return psycopg.connect(host=os.environ["POSTGRES_HOST"], port=os.environ["POSTGRES_PORT"], user=os.environ["POSTGRES_USER"],
                           password=os.environ["POSTGRES_PASSWORD"], dbname=database, autocommit=True)


def prepare_store(psycopg, name: str) -> None:
    with admin_connection(psycopg) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    with admin_connection(psycopg, name) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=os.getcwd(), env=os.environ.copy(),
                            capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError("alembic upgrade failed: " + common.redact(result.stderr[-400:]))


def drop_store(psycopg, name: str) -> None:
    try:
        with admin_connection(psycopg) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    except Exception as exc:  # the run result is already saved; only report
        logging.getLogger("runner").warning("store cleanup failed: %s", common.redact(str(exc)))


def main() -> int:
    sys.path.insert(0, os.getcwd())  # the Auto Ontology checkout; -I keeps everything else off sys.path
    embed_dim = int(os.environ.get("EMBED_DIM", "768"))
    store = os.environ["POSTGRES_DATABASE"]

    handler = logging.FileHandler(RUN_DIR / "run.log", encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    progress = Progress()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(progress)

    import psycopg

    def flush_forever() -> None:  # events can stop for a while during one long model call
        while True:
            time.sleep(1.0)
            save(force=True)

    threading.Thread(target=flush_forever, daemon=True).start()
    STATUS.update(status="running", started_at=common.now())
    set_stage("prepare")
    started = time.monotonic()
    timing: dict[str, float] = {}
    tables = 0
    try:
        prepare_store(psycopg, store)
        timing["prepare_seconds"] = round(time.monotonic() - started, 1)

        import auto_ontology.vdb.postgres as pg

        original_init = pg.PostgresVDB.__init__

        def init_with_local_dimension(self, *args, **kwargs):
            kwargs.setdefault("vector_size", embed_dim)
            original_init(self, *args, **kwargs)

        pg.PostgresVDB.__init__ = init_with_local_dimension

        if REQUEST["model"].get("structured_output") == "function_calling":
            # Some hosted models ignore response_format on long prompts and answer in prose or add fields.
            # Tool calling follows the schema reliably; upstream only selects it by model name (Claude).
            import auto_ontology.utils.llm_invoke as llm_invoke

            llm_invoke._structured_output_kwargs = lambda _llm: {"method": "function_calling", "tool_choice": None}

        from auto_ontology.connectors.registry import create_connector
        from auto_ontology.semantic.compile import run_semantic_compilation
        from auto_ontology.server.model_interchange.schemas import ExportRequest
        from auto_ontology.server.model_interchange.service import export_model
        from dev_tools.local_ingest import run_ingest

        connection = os.environ["CONNECTION_STRINGS"].split(",")[0].strip()
        database = create_connector(connection).database_name
        step = time.monotonic()
        run_ingest(connection)
        timing["ingest_seconds"] = round(time.monotonic() - step, 1)
        step = time.monotonic()
        tables = run_semantic_compilation(database)
        timing["compile_seconds"] = round(time.monotonic() - step, 1)
        set_stage("export")
        document = export_model(ExportRequest())
        (RUN_DIR / "export.yaml").write_text(document, encoding="utf-8")
        summary = {"database": database, "tables_compiled": tables, "chat_model": REQUEST["model"]["chat_model"],
                   "embed_model": REQUEST["model"]["embed_model"], "embed_dim": embed_dim, "locality": REQUEST["model"]["locality"],
                   "llm_calls": STATUS.get("llm_calls", 0), **timing,
                   "total_seconds": round(time.monotonic() - started, 1)}
        (RUN_DIR / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        common.finish_stages(STATUS, ok=True)
        STATUS.update(status="succeeded", finished_at=common.now(), detail="")
        save(force=True)
        return 0
    except BaseException as exc:  # noqa: BLE001 - everything must end up in status.json
        logging.getLogger("runner").exception("pipeline failed")
        common.finish_stages(STATUS, ok=False)
        STATUS.update(status="failed", finished_at=common.now(), error=common.redact(f"{type(exc).__name__}: {exc}")[:500])
        save(force=True)
        return 1
    finally:
        drop_store(psycopg, store)


if __name__ == "__main__":
    raise SystemExit(main())
