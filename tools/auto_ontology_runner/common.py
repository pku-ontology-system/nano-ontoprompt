"""Shared helpers of the Auto Ontology runner (standard library only).

Imported by ``runner.py`` (plain interpreter) and ``pipeline.py`` (the Auto Ontology
checkout's own interpreter), so it must not depend on any third-party package.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

# (key, label) in the order a run moves through them.
STAGES: list[tuple[str, str]] = [
    ("prepare", "准备存储"),
    ("catalog", "导入目录"),
    ("embed", "向量化"),
    ("terms", "术语与列描述"),
    ("semantic_fk", "语义外键"),
    ("metrics", "指标 SQL"),
    ("bridge", "桥表识别"),
    ("export", "导出"),
]
STAGE_INDEX = {key: index for index, (key, _label) in enumerate(STAGES)}
TERMINAL = {"succeeded", "failed", "cancelled"}

_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", re.I)
_BEARER = re.compile(r"(?i)\b(bearer|authorization:?)\s+[A-Za-z0-9._\-+/=]{8,}")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def redact(text: str) -> str:
    """Remove credentials from a log line before it is stored or served."""
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}***:***@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} ***", text)
    return _LONG_TOKEN.sub("***", text)


def now() -> float:
    return time.time()


def write_json(path: Path, payload: dict) -> None:
    """Atomic write so a reader never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def new_status(run_id: str, source_id: str, model_id: str) -> dict:
    return {
        "id": run_id, "source_id": source_id, "model_id": model_id, "status": "queued", "stage": None, "error": None,
        "created_at": now(), "started_at": None, "finished_at": None, "llm_calls": 0,
        "tables": {"total": 0, "done": 0}, "detail": "",
        "stages": [{"key": key, "label": label, "status": "pending", "started_at": None, "finished_at": None}
                   for key, label in STAGES],
    }


def classify(logger: str, message: str) -> tuple[str | None, dict]:
    """Map one Auto Ontology log record to (stage key, table event). Pure; unit-tested on a real log."""
    if logger.startswith("auto_ontology.catalog"):
        return "catalog", {}
    if logger == "auto_ontology.utils.embedding" and message.startswith("Embed:"):
        return "embed", {}
    if logger == "auto_ontology.semantic.pipeline":
        match = re.match(r"\[(\d+)/(\d+)\] Processing table: (.+)", message)
        if match:
            return "terms", {"total": int(match.group(2))}
        return "terms", {}
    if logger == "auto_ontology.semantic.visit_enter":
        match = re.match(r"\[(.+?)\] Generating terms and descriptions \(\d+ columns\) done in", message)
        return "terms", ({"done_table": match.group(1)} if match else {})
    if logger == "auto_ontology.semantic.compile":
        if "Resolving semantic FK" in message:
            return "semantic_fk", {}
        if "Suggesting SqlAttributes" in message:
            return "metrics", {}
        if "Building bridge tables" in message:
            return "bridge", {}
    return None, {}


def advance(status: dict, stage: str, at: float | None = None) -> None:
    """Move the run forward to ``stage``; earlier stages are closed, the order never goes back."""
    at = at or now()
    target = STAGE_INDEX[stage]
    current = STAGE_INDEX.get(status.get("stage") or "", -1)
    if target <= current:
        return
    for index, row in enumerate(status["stages"]):
        if index < target and row["status"] != "done":
            row["status"] = "done"
            row["started_at"] = row["started_at"] or at
            row["finished_at"] = at
        elif index == target:
            row["status"] = "running"
            row["started_at"] = row["started_at"] or at
    status["stage"] = stage


def finish_stages(status: dict, ok: bool, at: float | None = None) -> None:
    at = at or now()
    for row in status["stages"]:
        if row["status"] == "running":
            row["status"] = "done" if ok else "failed"
            row["finished_at"] = at
        elif row["status"] == "pending" and ok:
            row["status"] = "done"
            row["started_at"] = row["finished_at"] = at
