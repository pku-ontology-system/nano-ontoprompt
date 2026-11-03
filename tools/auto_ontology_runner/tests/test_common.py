"""Unit tests for the runner's pure helpers; run with: python -m pytest tools/auto_ontology_runner/tests"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common  # noqa: E402

# Real log lines from an Auto Ontology run (logger, message).
LINES = [
    ("auto_ontology.catalog.write", "Started parsing db factory_twin."),
    ("auto_ontology.utils.embedding", "Embed: 37 row(s) in 1 chunk(s) of 1024 via http://127.0.0.1:11434/v1 (model nomic-embed-text)"),
    ("auto_ontology.semantic.pipeline", "[1/6] Processing table: factory_asset"),
    ("auto_ontology.semantic.visit_enter", "[factory_asset] Detecting foreign keys…"),
    ("auto_ontology.semantic.visit_enter", "[factory_asset] Generating terms and descriptions (5 columns) done in 16.2s"),
    ("auto_ontology.utils.embedding", "Embed: 6 row(s) in 1 chunk(s) of 1024"),  # a later embedding must not move the stage back
    ("auto_ontology.semantic.compile", "Resolving semantic FK edges…"),
    ("auto_ontology.semantic.compile", "Suggesting SqlAttributes from query history…"),
    ("auto_ontology.semantic.compile", "Building bridge tables sql attributes"),
]


def replay(lines):
    status = common.new_status("0" * 16, "factory_twin", "m")
    seen = []
    for logger, message in lines:
        stage, event = common.classify(logger, message)
        if stage:
            common.advance(status, stage)
        seen.append((status["stage"], event))
    return status, seen


def test_stages_only_move_forward():
    status, seen = replay(LINES)
    assert [stage for stage, _ in seen] == ["catalog", "embed", "terms", "terms", "terms", "terms", "semantic_fk", "metrics", "bridge"]
    keys = [row["key"] for row in status["stages"] if row["status"] == "done"]
    assert keys == ["prepare", "catalog", "embed", "terms", "semantic_fk", "metrics"]
    assert [row["status"] for row in status["stages"] if row["key"] == "bridge"] == ["running"]


def test_table_events():
    _, seen = replay(LINES)
    assert seen[2][1] == {"total": 6}
    assert seen[4][1] == {"done_table": "factory_asset"}


def test_finish_closes_everything():
    status, _ = replay(LINES)
    common.finish_stages(status, ok=True)
    assert {row["status"] for row in status["stages"]} == {"done"}
    failed, _ = replay(LINES[:3])
    common.finish_stages(failed, ok=False)
    assert [row["status"] for row in failed["stages"] if row["key"] == "terms"] == ["failed"]


def test_redact_removes_credentials():
    text = "dsn postgresql://postgres:s3cret-pass@127.0.0.1:25433/db Authorization: Bearer abcdefgh12345678 key sk-" + "a" * 40
    cleaned = common.redact(text)
    assert "s3cret-pass" not in cleaned and "abcdefgh12345678" not in cleaned and "a" * 40 not in cleaned
    assert "127.0.0.1:25433/db" in cleaned
