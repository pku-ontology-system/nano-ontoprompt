"""Dataset CRUD + 版本管理服务 (含 DuckDB 预览)"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.models.v2.dataset import Dataset, DatasetVersion
from app.services.storage_service import StorageService, get_storage_service, user_facing_storage_error


class DatasetStorageError(RuntimeError):
    """A user-actionable error for a dataset version whose object is missing."""

    def __init__(self, dataset_id: str, storage_uri: str | None, reason: str):
        self.dataset_id = dataset_id
        self.storage_uri = storage_uri
        self.reason = reason
        # Keep the endpoint actionable even when MinIO returns a long English
        # S3 error.  The original exception remains in server logs.
        super().__init__(user_facing_storage_error(RuntimeError(reason), storage_uri))


class DatasetSnapshotIntegrityError(ValueError):
    """A pinned dataset cannot be proven to be a complete, stable snapshot."""


class DatasetService:
    def __init__(self, db: Session, storage: StorageService | None = None):
        self._db = db
        self._storage = storage or get_storage_service()

    def create_dataset(
        self,
        name: str,
        kind: str,
        connection_id: str | None = None,
        *,
        data_class: str = "regular",
        privacy_level: str = "standard",
        schema_json: dict | None = None,
        created_by: str | None = None,
    ) -> Dataset:
        if data_class not in {"regular", "temporal", "multimodal"}:
            raise ValueError("data_class must be regular, temporal or multimodal")
        if privacy_level not in {"standard", "private"}:
            raise ValueError("privacy_level must be standard or private")
        ds = Dataset(
            name=name, kind=kind, source_connection_id=connection_id,
            created_by=str(created_by) if created_by is not None else None,
            data_class=data_class, privacy_level=privacy_level,
            schema_json=schema_json or {},
        )
        self._db.add(ds)
        self._db.commit()
        self._db.refresh(ds)
        return ds

    def create_version(self, dataset_id: str, data: bytes, rowcount: int | None = None) -> DatasetVersion:
        """将数据存入 MinIO 并创建 DatasetVersion"""
        ds = self._db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not ds:
            raise ValueError(f"Dataset {dataset_id} not found")

        # 确定版本号
        last_ver = self._db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == dataset_id
        ).order_by(DatasetVersion.version_no.desc()).first()
        version_no = (last_ver.version_no + 1) if last_ver else 1

        # 存入 MinIO
        checksum = hashlib.sha256(data).hexdigest()
        key = f"datasets/{dataset_id}/v{version_no}/data.bin"
        uri = self._storage.put_bytes("raw-datasets", key, data)

        ver = DatasetVersion(
            dataset_id=dataset_id,
            version_no=version_no,
            rowcount=rowcount,
            storage_uri=uri,
            checksum=checksum,
        )
        self._db.add(ver)
        # SQLAlchemy evaluates the default UUID on flush.  Assigning
        # ``ds.latest_version_id`` before that point used to persist NULL and
        # made newly uploaded datasets appear to have no usable version.
        self._db.flush()
        ds.latest_version_id = ver.id
        # A version becomes selectable only after its object write has
        # completed.  Failed legacy references remain visible as repairable
        # records rather than silently producing empty previews.
        ds.readiness = "ready"
        self._db.commit()
        self._db.refresh(ver)
        return ver

    @staticmethod
    def count_snapshot_rows(raw: bytes) -> int:
        """Count records with the same header semantics used by preview()."""
        if raw[:2] == b"PK":
            import io
            import openpyxl

            workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            try:
                worksheet = workbook.active
                iterator = worksheet.iter_rows(values_only=True)
                next(iterator, None)  # first row is the header
                return sum(1 for _ in iterator)
            finally:
                workbook.close()

        text = raw.decode("utf-8", errors="strict").lstrip("\ufeff")
        stripped = text.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            payload = json.loads(text)
            if isinstance(payload, list):
                return len(payload)
            return 1 if isinstance(payload, dict) else 0

        import csv
        import io
        return sum(1 for _ in csv.DictReader(io.StringIO(text)))

    def get_dataset(self, dataset_id: str) -> Dataset | None:
        return self._db.query(Dataset).filter(Dataset.id == dataset_id).first()

    def list_datasets(self, kind: str | None = None) -> list[Dataset]:
        q = self._db.query(Dataset)
        if kind:
            q = q.filter(Dataset.kind == kind)
        return q.all()

    def list_versions(self, dataset_id: str) -> list[DatasetVersion]:
        return self._db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == dataset_id
        ).order_by(DatasetVersion.version_no).all()

    def latest_version(self, dataset_id: str) -> DatasetVersion:
        """Resolve the dataset's pinned latest version, with a legacy fallback."""
        dataset = self.get_dataset(dataset_id)
        if dataset is None:
            raise DatasetStorageError(dataset_id, None, "数据集不存在")
        if dataset.latest_version_id:
            version = self._db.get(DatasetVersion, dataset.latest_version_id)
            if version is None or version.dataset_id != dataset_id:
                raise DatasetStorageError(dataset_id, None, "最新数据版本指针无效")
            return version
        # Older dataset rows may predate latest_version_id being maintained.
        version = self._db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == dataset_id
        ).order_by(DatasetVersion.version_no.desc()).first()
        if version is None:
            raise DatasetStorageError(dataset_id, None, "数据集没有可读取的版本")
        return version

    def preview_version_id(self, dataset_id: str, version_id: str,
                           limit: int = 100) -> tuple[DatasetVersion, list[dict]]:
        version = self._db.get(DatasetVersion, version_id)
        if version is None or version.dataset_id != dataset_id:
            raise DatasetStorageError(dataset_id, None, "指定的数据版本不属于当前数据集")
        return version, self.preview(
            dataset_id, version.version_no, limit,
            version_id=version.id, verify_checksum=True,
        )

    def preview_latest(self, dataset_id: str, limit: int = 100) -> tuple[DatasetVersion, list[dict]]:
        version = self.latest_version(dataset_id)
        return self.preview_version_id(dataset_id, version.id, limit)

    def read_complete_version_snapshot(
        self, dataset_id: str, version_id: str,
    ) -> tuple[DatasetVersion, list[dict], dict]:
        """Read a pinned source to EOF before any snapshot-based writes."""
        version = self._db.get(DatasetVersion, version_id)
        if version is None or version.dataset_id != dataset_id:
            raise DatasetSnapshotIntegrityError(
                "DATASET_VERSION_MISMATCH: version is not in the selected dataset"
            )
        if not version.checksum:
            raise DatasetSnapshotIntegrityError(
                "DATASET_CHECKSUM_MISSING: a source checksum is required before applying a snapshot"
            )
        try:
            rows = self.preview(
                dataset_id, version.version_no, limit=None,
                version_id=version.id, verify_checksum=True, strict_text=True,
            )
        except DatasetStorageError as exc:
            raise DatasetSnapshotIntegrityError(
                f"DATASET_SNAPSHOT_UNVERIFIABLE: {exc}"
            ) from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DatasetSnapshotIntegrityError(
                "DATASET_SNAPSHOT_INVALID_ROWS: expected a complete list of row objects"
            )
        actual_rowcount = len(rows)
        if version.rowcount is not None and int(version.rowcount) != actual_rowcount:
            raise DatasetSnapshotIntegrityError(
                f"DATASET_ROWCOUNT_MISMATCH: manifest declares {version.rowcount} rows, "
                f"but the complete snapshot contains {actual_rowcount}"
            )
        manifest = {
            "dataset_id": dataset_id,
            "dataset_version_id": version.id,
            "version_no": int(version.version_no),
            "source_checksum": version.checksum,
            "row_count": actual_rowcount,
            "read_complete": True,
        }
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest["manifest_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return version, rows, manifest

    def read_latest_complete_snapshot(self, dataset_id: str) -> tuple[DatasetVersion, list[dict], dict]:
        version = self.latest_version(dataset_id)
        return self.read_complete_version_snapshot(dataset_id, version.id)

    def preview(
        self,
        dataset_id: str,
        version_no: int,
        limit: int | None = 100,
        *,
        version_id: str | None = None,
        verify_checksum: bool = False,
        strict_text: bool = False,
    ) -> list[dict]:
        """CSV/JSON 数据预览。无需 DuckDB, 纯 Python 处理。"""
        version_query = self._db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset_id)
        if version_id:
            version_query = version_query.filter(DatasetVersion.id == version_id)
        else:
            version_query = version_query.filter(DatasetVersion.version_no == version_no)
        ver = version_query.first()
        if not ver:
            return []
        if not ver.storage_uri:
            raise DatasetStorageError(dataset_id, None, "数据版本没有存储地址")

        try:
            raw = self._storage.get_object(ver.storage_uri)
            if verify_checksum and ver.checksum:
                actual_checksum = hashlib.sha256(raw).hexdigest()
                if actual_checksum != ver.checksum:
                    raise DatasetStorageError(dataset_id, ver.storage_uri, "数据版本校验和不匹配")
            text = "" if raw[:2] == b"PK" else raw.decode("utf-8", errors="strict" if strict_text else "replace").lstrip("﻿")

            # 检测是否 JSON
            stripped = text.lstrip()
            if stripped.startswith("[") or stripped.startswith("{"):
                import json
                data = json.loads(text)
                if isinstance(data, list):
                    return data if limit is None else data[:limit]
                elif isinstance(data, dict):
                    return [data]
                return []

            # 检测是否 Excel (xlsx) 二进制格式
            if raw[:2] == b"PK":
                try:
                    import openpyxl, io
                    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
                    try:
                        ws = wb.active
                        rows_list = []
                        headers = []
                        for ri, row in enumerate(ws.iter_rows(values_only=True)):
                            if ri == 0:
                                headers = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(row)]
                            else:
                                row_dict = {}
                                for i, val in enumerate(row):
                                    if i < len(headers):
                                        row_dict[headers[i]] = val if val is not None else ""
                                rows_list.append(row_dict)
                                if limit is not None and len(rows_list) >= limit:
                                    break
                        return rows_list
                    finally:
                        wb.close()
                except Exception:
                    if strict_text:
                        raise

            # 默认 CSV
            import csv, io
            reader = csv.DictReader(io.StringIO(text))
            rows = []
            for i, row in enumerate(reader):
                if limit is not None and i >= limit:
                    break
                rows.append(dict(row))
            return rows
        except DatasetStorageError:
            raise
        except Exception as exc:
            raise DatasetStorageError(dataset_id, ver.storage_uri, str(exc)) from exc
