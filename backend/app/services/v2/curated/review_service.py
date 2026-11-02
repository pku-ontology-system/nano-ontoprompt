"""人工审核服务 — 行级编辑、版本合并、审核状态管理"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import math
from sqlalchemy.orm import Session
from app.models.v2.curated import CuratedDataset, CuratedReview, CuratedRowEdit


ROW_EDIT_TOKEN_PREFIX = "review-row-v1:"


class ReviewService:

    def __init__(self, db: Session):
        self._db = db

    def start_review(
        self,
        curated_dataset_id: str,
        reviewer_id: str | None = None,
        *,
        requested_by: str | None = None,
        expected_dataset_version_id: str | None = None,
    ) -> CuratedReview:
        """新建审核记录，并固定审核人实际看到的数据版本。"""
        ds = self._get_dataset_or_raise(curated_dataset_id)
        dataset_version_id = self._latest_dataset_version_id(curated_dataset_id)
        if expected_dataset_version_id and expected_dataset_version_id != dataset_version_id:
            from fastapi import HTTPException
            raise HTTPException(
                409,
                "Dataset changed after preview; refresh the data before starting review",
            )

        review = CuratedReview(
            curated_dataset_id=curated_dataset_id,
            requested_by=requested_by,
            reviewer_id=reviewer_id,
            dataset_version_id=dataset_version_id,
            status="pending",
        )
        self._db.add(review)
        ds.status = "in_review"
        self._db.commit()
        self._db.refresh(review)
        return review

    def edit_row(
        self,
        review_id: str,
        row_pk: str,
        field_name: str,
        old_value: str | None,
        new_value: str | None,
        *,
        edited_by: str | None = None,
    ) -> CuratedRowEdit:
        """记录单行单字段的修改"""
        review = self._get_review_or_raise(review_id)
        if review.status != "pending":
            from fastapi import HTTPException
            raise HTTPException(409, "Only pending reviews can be edited")

        edit = CuratedRowEdit(
            review_id=review_id,
            edited_by=edited_by,
            row_pk=row_pk,
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
        )
        self._db.add(edit)
        self._db.commit()
        self._db.refresh(edit)
        return edit

    def batch_edit_rows(
        self,
        review_id: str,
        edits: list[dict],
        *,
        edited_by: str | None = None,
    ) -> list[CuratedRowEdit]:
        """批量提交行编辑
        edits 格式：[{"row_pk": "...", "field_name": "...", "old_value": "...", "new_value": "..."}]
        """
        review = self._get_review_or_raise(review_id, for_update=True)
        if review.status != "pending":
            from fastapi import HTTPException
            raise HTTPException(409, "Only pending reviews can be edited")
        seen_cells: set[tuple[str, str]] = set()
        normalized_edits = []
        for edit in edits:
            row_pk = str(edit.get("row_pk") or "")
            field_name = str(edit.get("field_name") or "")
            old_value = edit.get("old_value")
            new_value = edit.get("new_value")
            if not row_pk or len(row_pk) > 200 or not field_name or len(field_name) > 200:
                from fastapi import HTTPException
                raise HTTPException(422, "Each edit requires a valid row token and field name")
            if old_value is None or new_value is None:
                from fastapi import HTTPException
                raise HTTPException(422, "Row edits must include old and new values")
            cell = (row_pk, field_name)
            if cell in seen_cells:
                from fastapi import HTTPException
                raise HTTPException(422, "A batch may edit each row field only once")
            seen_cells.add(cell)
            normalized_edits.append((row_pk, field_name, str(old_value), str(new_value)))
        results = []
        for row_pk, field_name, old_value, new_value in normalized_edits:
            edit = CuratedRowEdit(
                review_id=review_id,
                edited_by=edited_by,
                row_pk=row_pk,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
            )
            self._db.add(edit)
            results.append(edit)
        if results:
            self._db.commit()
        return results

    def approve(self, review_id: str, notes: str = "", *, decided_by: str | None = None,
                commit: bool = True) -> CuratedReview:
        """审核通过 — 将数据集状态改为 approved"""
        review = self._get_review_or_raise(review_id, for_update=True)
        if review.status != "pending":
            from fastapi import HTTPException
            raise HTTPException(409, "Only pending reviews can be approved")
        if (not review.requested_by or not decided_by
                or str(review.requested_by) == str(decided_by)):
            from fastapi import HTTPException
            raise HTTPException(409, "An independent reviewer must approve this review")
        if review.reviewer_id and str(review.reviewer_id) != str(decided_by):
            from fastapi import HTTPException
            raise HTTPException(403, "Only the assigned reviewer can approve this review")
        review.status = "approved"
        review.notes = notes
        review.decided_at = datetime.now(timezone.utc)
        review.decided_by = decided_by

        ds = self._db.query(CuratedDataset).filter(
            CuratedDataset.id == review.curated_dataset_id
        ).first()
        if ds:
            ds.status = "approved"

        if commit:
            self._db.commit()
            self._db.refresh(review)
        return review

    def reject(self, review_id: str, notes: str = "", *, decided_by: str | None = None) -> CuratedReview:
        """审核拒绝"""
        review = self._get_review_or_raise(review_id, for_update=True)
        if review.status != "pending":
            from fastapi import HTTPException
            raise HTTPException(409, "Only pending reviews can be rejected")
        review.status = "rejected"
        review.notes = notes
        review.decided_at = datetime.now(timezone.utc)
        review.decided_by = decided_by

        ds = self._db.query(CuratedDataset).filter(
            CuratedDataset.id == review.curated_dataset_id
        ).first()
        if ds:
            ds.status = "rejected"

        self._db.commit()
        self._db.refresh(review)
        return review

    def get_edits(self, review_id: str) -> list[CuratedRowEdit]:
        """获取审核下全部行编辑记录。"""
        return self._db.query(CuratedRowEdit).filter(
            CuratedRowEdit.review_id == review_id
        ).all()

    @staticmethod
    def row_edit_token(dataset_version_id: str, row_index: int, row: dict) -> str:
        """Create a stable row identity tied to one immutable data version."""
        canonical = json.dumps(
            {"dataset_version_id": str(dataset_version_id), "row_index": int(row_index), "row": row},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{ROW_EDIT_TOKEN_PREFIX}{dataset_version_id}:{row_index}:{digest}"

    def apply_edits_to_snapshot(
        self,
        review_id: str,
        original_data: list[dict],
        *,
        row_identity_column: str | None = None,
        expected_dataset_version_id: str | None = None,
    ) -> list[dict]:
        """Apply reviewed edits to a copy of the source snapshot.

        Production apply passes the pinned version and gets strict token,
        unique-row, field, and old-value checks. The unpinned branch is retained
        for legacy callers only; it must not be used for ontology writes.
        """
        if expected_dataset_version_id:
            review = self._get_review_or_raise(review_id)
            if review.status != "approved":
                raise ValueError("REVIEW_NOT_APPROVED")
            if str(review.dataset_version_id or "") != str(expected_dataset_version_id):
                raise ValueError("REVIEW_DATASET_VERSION_MISMATCH")

        edits = self.get_edits(review_id)
        if not edits:
            return [dict(row) for row in original_data]

        if expected_dataset_version_id:
            return self._apply_strict_edits(
                edits,
                original_data,
                dataset_version_id=expected_dataset_version_id,
                row_identity_column=row_identity_column,
            )

        # 按 row_pk 分组编辑
        edit_map: dict[str, dict[str, str | None]] = {}
        for edit in edits:
            if edit.row_pk not in edit_map:
                edit_map[edit.row_pk] = {}
            edit_map[edit.row_pk][edit.field_name] = edit.new_value

        matched: set[str] = set()
        result = []
        for index, row in enumerate(original_data):
            candidate_keys = {str(index)}
            for key in ("id", "__pk__", row_identity_column):
                value = row.get(key) if key else None
                if value is not None:
                    candidate_keys.add(str(value))
            first_value = next(iter(row.values()), None)
            if first_value is not None:
                candidate_keys.add(str(first_value))

            row_edits = [key for key in edit_map if key in candidate_keys]
            if row_edits:
                row = dict(row)
                for row_pk in row_edits:
                    matched.add(row_pk)
                    for field, new_val in edit_map[row_pk].items():
                        if new_val is None:
                            row.pop(field, None)  # 删除字段
                        else:
                            row[field] = new_val
            result.append(row)

        unmatched = set(edit_map) - matched
        if unmatched:
            raise ValueError(
                f"Review {review_id} contains edits for rows absent from its pinned dataset version: "
                f"{sorted(unmatched)[:5]}"
            )
        return result

    def apply_edits_to_preview(
        self,
        review_id: str,
        dataset_version_id: str,
        preview_rows: list[dict],
        *,
        protected_fields: set[str] | None = None,
        additional_edits: list[CuratedRowEdit] | None = None,
    ) -> tuple[list[dict], dict[str, int]]:
        """Project saved edits onto a page of the exact reviewed version.

        Edits for rows beyond the requested preview window are counted and
        skipped; the complete worker snapshot validates every edit at apply.
        """
        review = self._get_review_or_raise(review_id)
        if str(review.dataset_version_id or "") != str(dataset_version_id):
            raise ValueError("REVIEW_DATASET_VERSION_MISMATCH")
        edits = [*self.get_edits(review_id), *(additional_edits or [])]
        result = [dict(row) for row in preview_rows]
        if not edits:
            return result, {"applied": 0, "outside_preview": 0}

        token_indexes: dict[str, int] = {}
        alias_indexes: dict[str, set[int]] = {}
        for index, row in enumerate(preview_rows):
            token_indexes[self.row_edit_token(dataset_version_id, index, row)] = index
            aliases = self._legacy_row_aliases(index, row)
            for field_name in protected_fields or set():
                value = row.get(field_name)
                if isinstance(value, (str, int, float, bool)):
                    aliases.add(str(value))
            for alias in aliases:
                alias_indexes.setdefault(alias, set()).add(index)

        stats = {"applied": 0, "outside_preview": 0}
        resolved_edits: list[tuple[int, CuratedRowEdit]] = []
        for edit in edits:
            row_pk = str(edit.row_pk)
            row_index: int | None = None
            if row_pk.startswith(ROW_EDIT_TOKEN_PREFIX):
                token_version, token_index = self._token_coordinates(row_pk)
                if token_version != str(dataset_version_id):
                    raise ValueError("REVIEW_EDIT_TOKEN_VERSION_MISMATCH")
                if token_index >= len(preview_rows):
                    stats["outside_preview"] += 1
                    continue
                row_index = token_indexes.get(row_pk)
                if row_index is None:
                    raise ValueError("REVIEW_EDIT_ROW_TOKEN_INVALID")
            else:
                matches = alias_indexes.get(row_pk, set())
                if not matches:
                    stats["outside_preview"] += 1
                    continue
                if len(matches) != 1:
                    raise ValueError("REVIEW_EDIT_ROW_IDENTITY_AMBIGUOUS")
                row_index = next(iter(matches))

            resolved_edits.append((row_index, edit))
        edit_protected_fields = {"id", "__pk__"}
        edit_protected_fields.update(protected_fields or set())
        self._apply_strict_edit_chains(
            result,
            resolved_edits,
            protected_fields=edit_protected_fields,
        )
        stats["applied"] = len(resolved_edits)
        return result, stats

    def _apply_strict_edits(
        self,
        edits: list[CuratedRowEdit],
        original_data: list[dict],
        *,
        dataset_version_id: str,
        row_identity_column: str | None,
    ) -> list[dict]:
        result = [dict(row) for row in original_data]
        alias_indexes: dict[str, set[int]] = {}
        has_legacy_edits = any(
            not str(edit.row_pk).startswith(ROW_EDIT_TOKEN_PREFIX) for edit in edits
        )
        if has_legacy_edits:
            for index, row in enumerate(original_data):
                for alias in self._legacy_row_aliases(index, row, row_identity_column):
                    alias_indexes.setdefault(alias, set()).add(index)

        resolved_edits: list[tuple[int, CuratedRowEdit]] = []
        for edit in edits:
            row_pk = str(edit.row_pk)
            if row_pk.startswith(ROW_EDIT_TOKEN_PREFIX):
                token_version, token_index = self._token_coordinates(row_pk)
                if token_version != str(dataset_version_id):
                    raise ValueError("REVIEW_EDIT_TOKEN_VERSION_MISMATCH")
                if token_index >= len(original_data):
                    raise ValueError("REVIEW_EDIT_ROW_TOKEN_INVALID")
                if self.row_edit_token(
                    dataset_version_id, token_index, original_data[token_index],
                ) != row_pk:
                    raise ValueError("REVIEW_EDIT_ROW_TOKEN_INVALID")
                row_index = token_index
            else:
                matches = alias_indexes.get(row_pk, set())
                if not matches:
                    raise ValueError("REVIEW_EDIT_ROW_NOT_FOUND")
                if len(matches) != 1:
                    raise ValueError("REVIEW_EDIT_ROW_IDENTITY_AMBIGUOUS")
                row_index = next(iter(matches))
            resolved_edits.append((row_index, edit))
        edit_protected_fields = {"id", "__pk__"}
        if row_identity_column:
            edit_protected_fields.add(str(row_identity_column))
        self._apply_strict_edit_chains(
            result,
            resolved_edits,
            protected_fields=edit_protected_fields,
        )
        return result

    @classmethod
    def _apply_strict_edit_chains(
        cls,
        rows: list[dict],
        resolved_edits: list[tuple[int, CuratedRowEdit]],
        *,
        protected_fields: set[str] | None = None,
    ) -> None:
        """Replay each cell's unique old→new chain without trusting DB row order."""
        grouped: dict[tuple[int, str], list[CuratedRowEdit]] = {}
        for row_index, edit in resolved_edits:
            grouped.setdefault((row_index, str(edit.field_name)), []).append(edit)

        for (row_index, field_name), chain in grouped.items():
            if field_name in (protected_fields or set()):
                raise ValueError("REVIEW_EDIT_IDENTITY_FIELD_IMMUTABLE")
            if field_name not in rows[row_index]:
                raise ValueError("REVIEW_EDIT_FIELD_NOT_FOUND")
            remaining = list(chain)
            while remaining:
                current_text = cls._edit_value_text(rows[row_index][field_name])
                matching = [edit for edit in remaining if str(edit.old_value or "") == current_text]
                if not matching:
                    raise ValueError("REVIEW_EDIT_OLD_VALUE_MISMATCH")
                if len(matching) != 1:
                    raise ValueError("REVIEW_EDIT_HISTORY_BRANCH_AMBIGUOUS")
                edit = matching[0]
                cls._apply_one_strict_edit(rows[row_index], edit)
                remaining.remove(edit)

    @staticmethod
    def _token_coordinates(token: str) -> tuple[str, int]:
        try:
            prefix, version_id, row_index, _digest = token.split(":", 3)
            if f"{prefix}:" != ROW_EDIT_TOKEN_PREFIX or not version_id:
                raise ValueError
            index = int(row_index)
            if index < 0:
                raise ValueError
            return version_id, index
        except (TypeError, ValueError):
            raise ValueError("REVIEW_EDIT_ROW_TOKEN_INVALID") from None

    @staticmethod
    def _legacy_row_aliases(index: int, row: dict, row_identity_column: str | None = None) -> set[str]:
        aliases = {str(index)}
        for key in ("id", "__pk__", row_identity_column):
            value = row.get(key) if key else None
            if isinstance(value, (str, int, float, bool)):
                aliases.add(str(value))
        first_value = next(iter(row.values()), None)
        if isinstance(first_value, (str, int, float, bool)):
            aliases.add(str(first_value))
        return aliases

    @staticmethod
    def _apply_one_strict_edit(row: dict, edit: CuratedRowEdit) -> None:
        field_name = str(edit.field_name or "")
        if not field_name or field_name not in row:
            raise ValueError("REVIEW_EDIT_FIELD_NOT_FOUND")
        current_value = row[field_name]
        if edit.old_value is None or ReviewService._edit_value_text(current_value) != str(edit.old_value):
            raise ValueError("REVIEW_EDIT_OLD_VALUE_MISMATCH")
        if edit.new_value is None:
            raise ValueError("REVIEW_EDIT_FIELD_REMOVAL_NOT_ALLOWED")
        row[field_name] = ReviewService._coerce_edit_value(current_value, str(edit.new_value))

    @staticmethod
    def _edit_value_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        if isinstance(value, (str, int, float)):
            return str(value)
        raise ValueError("REVIEW_EDIT_FIELD_TYPE_UNSUPPORTED")

    @staticmethod
    def _coerce_edit_value(current_value, new_value: str):
        if current_value is None or isinstance(current_value, str):
            return new_value
        if isinstance(current_value, bool):
            normalized = new_value.strip().lower()
            if normalized not in {"true", "false"}:
                raise ValueError("REVIEW_EDIT_VALUE_TYPE_INVALID")
            return normalized == "true"
        if isinstance(current_value, int):
            try:
                return int(new_value.strip())
            except (TypeError, ValueError):
                raise ValueError("REVIEW_EDIT_VALUE_TYPE_INVALID") from None
        if isinstance(current_value, float):
            try:
                parsed = float(new_value.strip())
            except (TypeError, ValueError):
                raise ValueError("REVIEW_EDIT_VALUE_TYPE_INVALID") from None
            if not math.isfinite(parsed):
                raise ValueError("REVIEW_EDIT_VALUE_TYPE_INVALID")
            return parsed
        raise ValueError("REVIEW_EDIT_FIELD_TYPE_UNSUPPORTED")

    # ── 私有辅助 ──────────────────────────────────────────────────

    def _get_dataset_or_raise(self, dataset_id: str) -> CuratedDataset:
        ds = self._db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
        if not ds:
            # Pipeline-created curated data is authoritative in v2_datasets;
            # materialize the review compatibility row on first review.
            from app.models.v2.dataset import Dataset
            source = self._db.query(Dataset).filter(
                Dataset.id == dataset_id,
                Dataset.kind == "curated",
            ).first()
            if source:
                ds = CuratedDataset(
                    id=source.id,
                    name=source.name,
                    schema_json=source.schema_json,
                    latest_version_id=source.latest_version_id,
                    status="pending_review",
                )
                self._db.add(ds)
                self._db.flush()
            else:
                from fastapi import HTTPException
                raise HTTPException(404, f"Curated dataset {dataset_id} not found")
        return ds

    def _latest_dataset_version_id(self, dataset_id: str) -> str | None:
        """Return the immutable source snapshot visible when review starts."""
        from app.services.v2.dataset_service import DatasetService, DatasetStorageError
        try:
            return DatasetService(self._db).latest_version(dataset_id).id
        except DatasetStorageError:
            # Keep review available for legacy datasets, but they cannot
            # trigger ontology writes without a versioned source snapshot.
            return None

    def _get_review_or_raise(self, review_id: str, *, for_update: bool = False) -> CuratedReview:
        query = self._db.query(CuratedReview).filter(CuratedReview.id == review_id)
        if for_update:
            query = query.with_for_update()
        review = query.first()
        if not review:
            from fastapi import HTTPException
            raise HTTPException(404, f"Review {review_id} not found")
        return review
