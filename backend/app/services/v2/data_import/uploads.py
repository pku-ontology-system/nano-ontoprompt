"""Turn files a person uploads into a bundle: tables from table-like files, assets from every other kind of file.

Nothing here knows what the data is about. A spreadsheet sheet, a CSV, a JSON array of records, a Parquet file each become a table;
anything else (images, audio, video, PDF, documents, archives of those) becomes an asset object that records what the file is and where
its bytes are kept. A table column that holds file names is linked to the assets by the profiler like any other key.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any

import pandas as pd

from app.services.storage_service import get_storage_service
from app.services.v2.data_import.model import Bundle, Replay, Source, Table, pascal, snake

MAX_FILES = 400
MAX_BYTES = 120 * 1024 * 1024
MAX_ROWS = 200_000
TABLE_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet", ".xlsx", ".xls"}
MEDIA_KINDS = {"image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}, "audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
               "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"}, "pdf": {".pdf"}, "document": {".doc", ".docx", ".ppt", ".pptx", ".odt", ".rtf", ".xml", ".html"},
               "text": {".txt", ".md", ".log", ".yaml", ".yml"}, "pointcloud": {".ply", ".pcd", ".las", ".laz", ".obj", ".stl"}}
ASSET_TYPE = "MediaAsset"


class UploadError(ValueError):
    pass


def media_kind(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    return next((kind for kind, suffixes in MEDIA_KINDS.items() if suffix in suffixes), "other")


def singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ses") or word.endswith("xes"):
        return word[:-2]
    return word[:-1] if word.endswith("s") and not word.endswith("ss") and len(word) > 3 else word


def clean_columns(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    """snake_case, unique column names (the property ids), keeping the original header as the label."""
    seen: dict[str, int] = {}
    names, labels = [], {}
    for original in frame.columns:
        name = snake(original) or "column"
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}_{seen[name]}"
        names.append(name)
        if str(original) != name:
            labels[name] = str(original)
    frame = frame.copy()
    frame.columns = names
    return frame, labels


def _read_csv(content: bytes, sep: str | None) -> pd.DataFrame:
    """Text tables come in many encodings (a spreadsheet exported in China is usually GB18030) and a one-column file has no delimiter to find."""
    error: Exception | None = None
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            try:
                return pd.read_csv(io.BytesIO(content), sep=sep, engine="python", nrows=MAX_ROWS, encoding=encoding)
            except csv.Error:
                return pd.read_csv(io.BytesIO(content), sep=",", engine="python", nrows=MAX_ROWS, encoding=encoding)
        except UnicodeDecodeError as failure:
            error = failure
    raise error  # type: ignore[misc]


def read_table(name: str, content: bytes) -> list[tuple[str, pd.DataFrame]]:
    """(table name, frame) for each table a file holds: one, or one per sheet of a workbook."""
    suffix = PurePosixPath(name).suffix.lower()
    stem = PurePosixPath(name).stem
    buffer = io.BytesIO(content)
    try:
        if suffix in (".csv", ".tsv"):
            frames = [(stem, _read_csv(content, "\t" if suffix == ".tsv" else None))]
        elif suffix in (".jsonl", ".ndjson"):
            frames = [(stem, pd.read_json(buffer, lines=True, nrows=MAX_ROWS))]
        elif suffix == ".json":
            data = json.loads(content.decode("utf-8-sig"))
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [data])
            frames = [(stem, pd.json_normalize(data[:MAX_ROWS]))]
        elif suffix == ".parquet":
            frames = [(stem, pd.read_parquet(buffer))]
        elif suffix in (".xlsx", ".xls"):
            book = pd.read_excel(buffer, sheet_name=None)
            frames = [(f"{stem}_{sheet}" if len(book) > 1 else stem, frame) for sheet, frame in book.items()]
        else:
            raise UploadError(f"不是表格文件：{name}")
    except UploadError:
        raise
    except Exception as error:  # noqa: BLE001  a file that cannot be parsed is the user's to fix, whatever library said so
        raise UploadError(f"读不了 {name}：{str(error)[:160] or '文件是空的或格式不对'}") from error
    return [(table, frame.iloc[:MAX_ROWS]) for table, frame in frames if len(frame.columns)]


def expand(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Zip archives are opened (a folder of files is a common way to hand over images with their table); the rest pass through."""
    out: list[tuple[str, bytes]] = []
    for name, content in files:
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    for info in archive.infolist():
                        path = PurePosixPath(info.filename)
                        if info.is_dir() or path.is_absolute() or ".." in path.parts or path.name.startswith(".") or "__MACOSX" in path.parts:
                            continue
                        if info.file_size > MAX_BYTES:
                            raise UploadError(f"压缩包里的文件太大：{info.filename}")
                        out.append((str(path), archive.read(info)))
            except zipfile.BadZipFile as exc:
                raise UploadError(f"不是有效的压缩包：{name}") from exc
        else:
            out.append((name, content))
    if len(out) > MAX_FILES:
        raise UploadError(f"一次最多导入 {MAX_FILES} 个文件，这批有 {len(out)} 个")
    if sum(len(c) for _, c in out) > MAX_BYTES:
        raise UploadError(f"一次最多导入 {MAX_BYTES // 1024 // 1024} MB")
    return out


def describe_asset(name: str, content: bytes) -> dict[str, Any]:
    kind = media_kind(name)
    digest = hashlib.sha256(content).hexdigest()
    row: dict[str, Any] = {"asset_id": digest[:16], "file_name": PurePosixPath(name).name, "path": name, "media_kind": kind, "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                           "size_bytes": len(content), "sha256": digest}
    if kind == "image":
        try:
            from PIL import Image

            with Image.open(io.BytesIO(content)) as image:
                row["width"], row["height"] = image.size
        except Exception:  # noqa: BLE001  an image PIL cannot open is still an asset
            pass
    elif kind == "text":
        row["text_preview"] = content[:4000].decode("utf-8", errors="ignore").strip()[:280]
    return row


def store(upload_id: str, files: list[tuple[str, bytes]]) -> dict[str, str]:
    """Keep the original bytes in the platform's object storage; returns file name -> storage uri."""
    storage = get_storage_service()
    uris = {}
    for name, content in files:
        bucket = "raw-datasets" if PurePosixPath(name).suffix.lower() in TABLE_SUFFIXES else "media"
        key = f"imports/{upload_id}/{hashlib.sha256(name.encode()).hexdigest()[:8]}-{PurePosixPath(name).name}"
        uris[name] = storage.put_bytes(bucket, key, content, content_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
    return uris


def build(upload_id: str, title: str, ontology_name: str, files: list[tuple[str, bytes]], uris: dict[str, str] | None = None, *, replay: dict[str, Any] | None = None) -> Bundle:
    """A bundle from uploaded files. `uris` (file name -> storage uri) is recorded as the source so the import can be repeated or continued."""
    files = expand(files)
    tables: list[Table] = []
    sources: list[Source] = []
    assets: list[dict[str, Any]] = []
    used: set[str] = set()
    for name, content in files:
        digest = hashlib.sha256(content).hexdigest()
        source = Source(id=f"file-{len(sources) + 1}", title=name, kind="upload", location=(uris or {}).get(name), bytes=len(content), sha256=digest)
        if PurePosixPath(name).suffix.lower() in TABLE_SUFFIXES:
            try:
                parsed = read_table(name, content)
            except UploadError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise UploadError(f"读不出表格：{name}（{type(exc).__name__}）") from exc
            for stem, frame in parsed:
                frame, labels = clean_columns(frame)
                base = snake(stem)
                table_name, n = base, 1
                while table_name in used:
                    n += 1
                    table_name = f"{base}_{n}"
                used.add(table_name)
                type_name = pascal(singular(table_name))
                tables.append(Table(name=table_name, frame=frame, type_name=type_name if type_name != ASSET_TYPE else f"{type_name}Table", label=stem, description=f"来自文件 {name}",
                                    source=source.id, column_labels=labels))
            sources.append(source)
        else:
            assets.append({**describe_asset(name, content), "storage_uri": (uris or {}).get(name)})
            sources.append(source)
    if assets:
        frame = pd.DataFrame(assets)
        tables.append(Table(name="media_assets", frame=frame, type_name=ASSET_TYPE, label="文件资产", description="上传的非表格文件：图片、音频、视频、文档等，记录文件本身的信息和存放位置",
                            source=None, primary_key=["asset_id"], kind="asset", column_labels={"asset_id": "资产编号", "file_name": "文件名", "path": "路径", "media_kind": "类别",
                                                                                           "mime_type": "媒体类型", "size_bytes": "字节数", "sha256": "SHA-256", "width": "宽", "height": "高",
                                                                                           "text_preview": "文本预览", "storage_uri": "存放位置", "preview_url": "预览地址"}))
    if not tables:
        raise UploadError("没有找到可以导入的内容")
    kinds = {"tabular": bool([t for t in tables if t.kind == "entity"]), "media": bool(assets)}
    kind = "multimodal" if kinds["media"] else "relational"
    bundle = Bundle(id=f"upload:{upload_id}", title=title, summary=f"上传的 {len(files)} 个文件：{len([t for t in tables if t.kind == 'entity'])} 张表，{len(assets)} 个其他文件", domain="其他", kind=kind,
                    ontology_name=ontology_name, sources=sources, tables=tables, tags=["上传"])
    if replay:
        target = next((t for t in tables if t.name == replay["table"]), None)
        if target is None or replay["order_by"] not in target.frame.columns:
            raise UploadError("逐步载入需要选一张表和一个排序列")
        target.kind = "event"
        target.order_by = replay["order_by"]
        target.frame = target.frame.sort_values(replay["order_by"], kind="stable").reset_index(drop=True)
        total = len(target.frame)
        bundle.replay = Replay(table=target.name, initial=max(1, min(int(replay.get("initial", 200)), total)), batch=max(1, int(replay.get("batch", 200))), total=total, order_by=replay["order_by"])
        bundle.kind = "events"
    return bundle


def preview(bundle: Bundle) -> dict[str, Any]:
    """What an upload contains, for the page to show before anything is written: tables with their columns, assets by kind."""
    from app.services.v2.data_import import profile

    report = profile.finalize(bundle)
    tables = [{"name": t.name, "type_name": t.type_name, "label": t.label, "kind": t.kind, "rows": int(len(t.frame)), "primary_key": t.primary_key,
               "columns": [{"name": str(c), "type": profile.platform_type(t.frame[c]), "label": t.column_labels.get(c, "")} for c in list(t.frame.columns)[:60]]} for t in bundle.tables]
    assets = {}
    for t in bundle.tables:
        if t.kind == "asset":
            assets = {k: int(v) for k, v in t.frame["media_kind"].value_counts().items()}
    return {"title": bundle.title, "kind": bundle.kind, "tables": tables, "assets": assets, "links": [{"name": l.name, "from": f"{l.source_table}.{l.source_column}", "to": f"{l.target_table}.{l.target_column}",
                                                                                                      "basis": l.basis, "coverage": l.coverage} for l in bundle.links], "notes": report["notes"]}
