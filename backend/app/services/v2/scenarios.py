"""Scenario catalog and immutable revision builder.

The service deliberately reuses QueryDataView and the existing FalkorDB adapter;
there is no second query or object storage model here.
"""
from __future__ import annotations
from collections.abc import Callable
from datetime import timedelta
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from app.models.ontology import OntologyProject
from app.models.v2.query_view import QueryDataView
from app.models.v2.scenario import ScenarioAudit, ScenarioChangeSet, ScenarioCheckpoint, ScenarioGrant, ScenarioResource, ScenarioRevision
from app.schemas.v2.scenario import ChangeSetRequest, Edit, LinkTarget, RevisionRequest, Target
from app.services.v2.graph.falkordb_service import FalkorDBService
from app.services.v2.object_query.core import FalkorReadAdapter
from app.services.v2.object_query.data_views import aware, now, require_ready
from app.services.v2.object_query.authorization import require_ontology_access
from app.services.v2.object_query.errors import ObjectQueryError
from app.services.v2.object_query.normalize import stable_hash
from app.schemas.v2.object_query import ExecutionContext


class ScenarioError(Exception):
    def __init__(self, code, message, status=422, path="scenario"):
        self.code, self.message, self.status, self.path = code, message, status, path
        super().__init__(message)


def error(exc: ScenarioError):
    return {"code": exc.code, "path": exc.path, "message": exc.message, "details": {}}


def _target_id(target):
    return str(target.object_id)


def _target_key(target):
    return (target.concrete_type, _target_id(target))


def scenario_expires_at(row):
    if row.expires_at:
        return aware(row.expires_at)
    if row.ttl_seconds:
        return aware(row.created_at) + timedelta(seconds=row.ttl_seconds)
    return None


def scenario_is_expired(row, at=None):
    expires_at = scenario_expires_at(row)
    return bool(row.expired_at or (expires_at and expires_at <= (aware(at) if at else now())))


def _validate_edit(edit: Edit, ontology_id: str):
    if edit.op in {"set_property", "unset_property", "delete_object"} and not edit.target:
        raise ScenarioError("invalid_edit", "Target is required for this edit", path=f"edits[{edit.sequence}].target")
    if edit.op in {"set_property", "unset_property"} and not edit.property:
        raise ScenarioError("invalid_edit", "Property is required", path=f"edits[{edit.sequence}].property")
    if edit.op in {"add_link", "remove_link"} and not edit.link:
        raise ScenarioError("invalid_edit", "Link is required", path=f"edits[{edit.sequence}].link")
    for candidate in (edit.target, edit.link.source if edit.link else None, edit.link.target if edit.link else None):
        if candidate and candidate.ontology_id != ontology_id:
            raise ScenarioError("ontology_context_mismatch", "Edit target ontology differs from scenario", path=f"edits[{edit.sequence}]")


def _summary(row):
    expires_at = scenario_expires_at(row)
    return {"id": row.id, "ontology_id": row.ontology_id, "owner_id": row.owner_id, "name": row.name,
            "description": row.description, "status": row.status, "base_view_id": row.base_view_id,
            "head_revision": row.head_revision, "etag": row.etag, "ttl_seconds": row.ttl_seconds,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "expired_at": row.expired_at.isoformat() if row.expired_at else None,
            "mode": row.mode, "last_rebased_at": row.last_rebased_at.isoformat() if row.last_rebased_at else None,
            "rebase_error": row.rebase_error,
            "protected_demo": row.protected_demo,
            "parent_scenario_id": row.parent_scenario_id,
            "parent_checkpoint_id": row.parent_checkpoint_id,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat()}


class ScenarioService:
    def __init__(self, db, ontology_id, user, graph=None, *, execution_actor_kind="user"):
        if execution_actor_kind not in {"user", "system"}:
            raise ValueError("Unsupported Scenario execution actor kind")
        self.db, self.ontology_id, self.user = db, ontology_id, user
        self.graph = graph or FalkorDBService()
        self.execution_actor_kind = execution_actor_kind
        self.execution_actor_id = str(user.id) if execution_actor_kind == "user" else None

    def authorization_provenance(self, scenario_id, permission_digest=None):
        return {
            "actor_kind": self.execution_actor_kind,
            "policy_subject_id": str(self.user.id),
            "authorization_mode": (
                "delegated_owner_policy" if self.execution_actor_kind == "system"
                else "server_policy"
            ),
            "scope": {"kind": "scenario", "scenario_id": str(scenario_id),
                      "ontology_id": str(self.ontology_id)},
            **({"permission_digest": permission_digest}
               if permission_digest is not None else {}),
        }

    def authorize_ontology(self):
        row = self.db.get(OntologyProject, self.ontology_id)
        if not row:
            raise ScenarioError("not_found", "Ontology not found", 404)
        try:
            # Scenario reads follow the same Plan A projection as ontology and
            # object reads. Ownership is no longer required for a viewer;
            # mutations still use the write check in ``get`` and route guards.
            require_ontology_access(self.db, self.ontology_id, str(self.user.id), self.user.role)
        except ObjectQueryError as exc:
            raise ScenarioError("not_found", "Ontology not found", 404) from exc

    def get(self, scenario_id, write=False, allow_expired=False):
        row = self.db.get(ScenarioResource, scenario_id)
        if not row or row.ontology_id != self.ontology_id:
            raise ScenarioError("not_found", "Scenario not found", 404)
        grant = self.db.get(ScenarioGrant, (row.id, self.user.id))
        if self.user.role != "admin" and row.owner_id != self.user.id and not grant:
            if write:
                raise ScenarioError("forbidden", "Scenario editor access required", 403)
            try:
                require_ontology_access(self.db, self.ontology_id, str(self.user.id), self.user.role)
            except ObjectQueryError as exc:
                raise ScenarioError("forbidden", "Scenario access denied", 403) from exc
        if write and self.user.role != "admin" and row.owner_id != self.user.id and (not grant or grant.role != "editor"):
            raise ScenarioError("forbidden", "Scenario editor access required", 403)
        if not allow_expired and scenario_is_expired(row):
            raise ScenarioError('scenario_expired', 'Scenario retention period has expired', 410)
        return row

    def create(self, request):
        view = require_ready(self.db, request.base_view_id, self.ontology_id, principal_id=self.user.id, is_admin=self.user.role == "admin")
        row = ScenarioResource(ontology_id=self.ontology_id, owner_id=self.user.id, name=request.name,
                               description=request.description, base_view_id=view.id, ttl_seconds=request.ttl_seconds,
                               expires_at=(now() + timedelta(seconds=request.ttl_seconds)) if request.ttl_seconds else None,
                               protected_demo=request.protected_demo, mode=request.mode,
                               last_rebased_at=now() if request.mode == 'tracking' else None)
        self.db.add(row); self.db.flush()
        self.db.add(ScenarioAudit(scenario_id=row.id, actor_id=self.user.id, operation="create", summary={"base_view_id": view.id}))
        self.db.commit(); self.db.refresh(row)
        return row

    def validate(self, scenario, request: ChangeSetRequest):
        if request.base_revision != scenario.head_revision:
            raise ScenarioError("revision_conflict", "ChangeSet base revision is not the current head", 409, "base_revision")
        if len({edit.sequence for edit in request.edits}) != len(request.edits):
            raise ScenarioError("invalid_edit", "Edit sequence values must be unique", path="edits.sequence")
        for edit in sorted(request.edits, key=lambda item: item.sequence):
            _validate_edit(edit, self.ontology_id)
        canonical = request.model_dump(mode="json", exclude={"preview_digest"})
        return canonical, stable_hash(canonical)

    @staticmethod
    def _revision_preview_request(request):
        return request.model_dump(
            mode="json",
            exclude={"permission_digest", "preview_digest"},
        )

    def _revision_preview_digest(
        self,
        scenario,
        request,
        parent_view,
        permission_digest: str,
        *,
        replace_all: bool = False,
        operation_context: dict | None = None,
    ) -> str:
        return stable_hash({
            "contract": "scenario-revision-preview-v1",
            "scenario_id": scenario.id,
            "scenario_etag": scenario.etag,
            "head_revision": scenario.head_revision,
            "base_revision": request.base_revision,
            "expected_etag": request.expected_etag,
            "target_revision": scenario.head_revision + 1,
            "parent_view_id": parent_view.id,
            "parent_content_digest": (
                parent_view.content_digest or parent_view.source_manifest_digest
            ),
            "request_digest": stable_hash(self._revision_preview_request(request)),
            "replace_all": bool(replace_all),
            "operation_context": operation_context or {},
            "permission_digest": permission_digest,
        })

    @staticmethod
    def _revision_preview_changes(
        snapshot,
        edits: list[Edit],
        *,
        allow_existing_create_conflicts: bool = False,
    ) -> list[dict]:
        """Describe the visible before/after effect of each typed edit."""
        objects = {key: dict(value) for key, value in snapshot.objects.items()}
        edges = set(snapshot.edges)
        changes = []

        def property_state(properties: dict, name: str) -> dict:
            if name in properties:
                return {"present": True, "value": properties[name]}
            return {"present": False}

        for edit in sorted(edits, key=lambda item: item.sequence):
            if edit.op == "invoke_action":
                changes.append({
                    "sequence": edit.sequence,
                    "op": edit.op,
                    "preview_available": False,
                    "reason": "action_runtime_required",
                })
                continue

            if edit.op in {"set_property", "unset_property"}:
                key = _target_key(edit.target)
                properties = objects.get(key, {})
                before = property_state(properties, edit.property)
                if edit.op == "set_property":
                    after = {"present": True, "value": edit.value}
                    properties[edit.property] = edit.value
                else:
                    after = {"present": False}
                    properties.pop(edit.property, None)
                changes.append({
                    "sequence": edit.sequence,
                    "op": edit.op,
                    "target": {"object_type": key[0], "object_id": key[1]},
                    "property": edit.property,
                    "before": before,
                    "after": after,
                })
                continue

            if edit.op == "create_object":
                object_id = _target_id(edit.target) if edit.target else ""
                key = (edit.object_type or "Entity", object_id)
                existed = key in objects
                if existed and not allow_existing_create_conflicts:
                    raise ScenarioError(
                        "object_already_exists",
                        "Action cannot create an object over an existing object",
                        409,
                    )
                properties = dict(edit.properties)
                objects[key] = properties
                property_changes = {}
                for name in sorted(properties):
                    item = {"after": property_state(properties, name)}
                    if not existed:
                        item["before"] = {"present": False}
                    property_changes[name] = item
                changes.append({
                    "sequence": edit.sequence,
                    "op": edit.op,
                    "target": {"object_type": key[0], "object_id": key[1]},
                    "before": {"exists": existed},
                    "after": {"exists": True},
                    "properties": property_changes,
                })
                continue

            if edit.op == "delete_object":
                key = _target_key(edit.target)
                objects.pop(key, None)
                edges = {edge for edge in edges if edge[0] != key and edge[2] != key}
                changes.append({
                    "sequence": edit.sequence,
                    "op": edit.op,
                    "target": {"object_type": key[0], "object_id": key[1]},
                    "before": {"exists": True},
                    "after": {"exists": False},
                })
                continue

            if edit.op in {"add_link", "remove_link"}:
                link = edit.link
                edge = (
                    (link.source.concrete_type, link.source.object_id),
                    link.relation_type,
                    (link.target.concrete_type, link.target.object_id),
                )
                before_present = edge in edges
                if edit.op == "add_link":
                    edges.add(edge)
                    after_present = True
                else:
                    edges.discard(edge)
                    after_present = False
                changes.append({
                    "sequence": edit.sequence,
                    "op": edit.op,
                    "link": {
                        "relation_type": link.relation_type,
                        "source": {"object_type": edge[0][0], "object_id": edge[0][1]},
                        "target": {"object_type": edge[2][0], "object_id": edge[2][1]},
                    },
                    "before": {"exists": before_present},
                    "after": {"exists": after_present},
                })

        return changes

    def preview_revision(
        self,
        scenario,
        request: RevisionRequest,
        *,
        replace_all: bool = False,
        operation_context: dict | None = None,
        parent_view_id_override: str | None = None,
        allow_action_invocations: bool = False,
    ) -> dict:
        """Compile an authorized Scenario ChangeSet preview without writing."""
        scenario = self.get(scenario.id, write=True)
        if scenario.status != "active":
            raise ScenarioError("scenario_archived", "Archived scenarios cannot be edited", 409)
        canonical, payload_hash = self.validate(scenario, request)
        if not allow_action_invocations and any(edit.op == "invoke_action" for edit in request.edits):
            raise ScenarioError(
                "action_invocation_requires_runtime",
                "Action invocation must use the compiled Scenario Action endpoint",
                422,
                "edits",
            )
        if request.expected_etag != scenario.etag:
            raise ScenarioError("etag_conflict", "Scenario changed since it was read", 409, "expected_etag")
        parent_view_id = parent_view_id_override or (
            scenario.base_view_id
            if replace_all or scenario.head_revision == 0
            else self.db.query(ScenarioRevision).filter_by(
                scenario_id=scenario.id, revision=scenario.head_revision,
            ).one().result_view_id
        )
        parent_view = self.db.get(QueryDataView, parent_view_id)
        if parent_view is None:
            raise ScenarioError("scenario_parent_view_missing", "Scenario parent view is unavailable", 409)
        require_ready(
            self.db, parent_view.id, self.ontology_id,
            principal_id=parent_view.created_by, is_admin=False,
        )
        source = FalkorReadAdapter(
            self.graph._graph(parent_view.graph_key),
            graph_ontology_id=parent_view.graph_key,
        ).read(self.ontology_id)
        from app.services.v2.action_authorization import (
            authorize_action_snapshot,
            authorize_compiled_edits,
        )
        permission_digest = authorize_compiled_edits(
            self.db, self.ontology_id, self.user, source, request.edits,
        )
        review_snapshot, _ = authorize_action_snapshot(
            self.db, self.ontology_id, self.user, source,
        )
        if request.permission_digest and request.permission_digest != permission_digest:
            raise ScenarioError("permission_changed", "权限已变化；请重新预览", 409)
        preview_digest = self._revision_preview_digest(
            scenario, request, parent_view, permission_digest,
            replace_all=replace_all, operation_context=operation_context,
        )
        return {
            "scenario_id": scenario.id,
            "scenario_etag": scenario.etag,
            "base_revision": scenario.head_revision,
            "target_revision": scenario.head_revision + 1,
            "parent_view_id": parent_view.id,
            "parent_content_digest": parent_view.content_digest or parent_view.source_manifest_digest,
            "request_digest": stable_hash(self._revision_preview_request(request)),
            "payload_hash": payload_hash,
            "permission_digest": permission_digest,
            "preview_digest": preview_digest,
            "edits": canonical["edits"],
            "changes": self._revision_preview_changes(review_snapshot, request.edits),
            "edit_count": len(canonical["edits"]),
        }

    def publish_revision(
        self,
        scenario,
        request: RevisionRequest,
        *,
        replace_all: bool = False,
        operation_context: dict | None = None,
        source: str = "scenario_revision",
        require_preview: bool = False,
        parent_view_id_override: str | None = None,
        before_scenario_relock: Callable[[], None] | None = None,
        after_scenario_relock: Callable[[ScenarioResource], None] | None = None,
        before_sql_commit: Callable[[], None] | None = None,
    ):
        """Claim, fence, publish, and recover a Scenario revision write."""
        from app.services.v2.action_runtime import (
            claim_idempotent_scenario_action_run,
            create_scenario_revision_action_run_claim,
            fail_scenario_action_run_claim,
            scenario_action_run_claim_manifest,
        )

        scenario_id = scenario.id
        self.get(scenario_id, write=True)
        if (
            bool(self.db.new)
            or bool(self.db.deleted)
            or any(self.db.is_modified(item, include_collections=True) for item in self.db.dirty)
        ):
            raise ScenarioError(
                "revision_pending_state",
                "Scenario revision must claim its ActionRun before unrelated changes are staged",
                409,
            )
        request_payload = self._revision_preview_request(request)
        request_digest = stable_hash({
            "operation": "scenario_revision",
            "scenario_id": scenario_id,
            "request": request_payload,
            "replace_all": bool(replace_all),
            "operation_context": operation_context or {},
            "parent_view_id_override": parent_view_id_override,
        })
        scope = f"scenario:{scenario_id}"
        actor_id = self.execution_actor_id
        actor_kind = self.execution_actor_kind
        runtime_action_run = None
        claim_token = None
        try:
            existing_run, claim_token, replayed = claim_idempotent_scenario_action_run(
                self.db,
                ontology_id=self.ontology_id,
                actor_id=actor_id,
                scope=scope,
                request_id=request.client_request_id,
                request_digest=request_digest,
                scenario_id=scenario_id,
            )
            if existing_run is not None:
                runtime_action_run = existing_run
                if replayed:
                    if request.preview_digest and request.preview_digest != existing_run.preview_digest:
                        raise ScenarioError("idempotency_conflict", "Request ID was used with another Scenario preview", 409)
                    revision_number = (existing_run.after_snapshot or {}).get("scenario_revision")
                    revision = self.db.query(ScenarioRevision).filter_by(
                        scenario_id=scenario_id, revision=revision_number, status="ready",
                    ).first() if revision_number is not None else None
                    if revision is None:
                        raise ScenarioError("action_request_incomplete", "ActionRun 已完成但修订记录不可读取", 409)
                    return revision

            canonical = request.model_dump(mode="json", exclude={"preview_digest"})
            payload_envelope = {"request": canonical, "replace_all": replace_all}
            if operation_context is not None:
                payload_envelope["operation_context"] = operation_context
            legacy_change = self.db.query(ScenarioChangeSet).filter_by(
                scenario_id=scenario_id,
                client_request_id=request.client_request_id,
            ).first()
            if legacy_change is not None and existing_run is None:
                if legacy_change.payload_hash != stable_hash(payload_envelope):
                    raise ScenarioError("idempotency_conflict", "Request id was already used with another payload", 409, "client_request_id")
                report = legacy_change.validation_report or {}
                revision_id = report.get("revision_id")
                revision = self.db.get(ScenarioRevision, revision_id) if revision_id else None
                if revision is None:
                    raise ScenarioError("request_incomplete", "已记录的请求缺少可重放的情景修订", 409, "client_request_id")
                return revision

            if require_preview and not request.preview_digest:
                raise ScenarioError("preview_required", "请先预览该 Scenario 修订，再使用同一预览摘要提交", 409, "preview_digest")
            if require_preview and any(edit.op == "invoke_action" for edit in request.edits):
                raise ScenarioError(
                    "action_invocation_requires_runtime",
                    "Action invocation must use the compiled Scenario Action endpoint",
                    422,
                    "edits",
                )

            locked_scenario = self.db.query(ScenarioResource).filter_by(
                id=scenario_id, ontology_id=self.ontology_id,
            ).populate_existing().with_for_update().first()
            if locked_scenario is None:
                raise ScenarioError("not_found", "Scenario not found", 404)
            self.get(locked_scenario.id, write=True)
            preview = self.preview_revision(
                locked_scenario, request,
                replace_all=replace_all,
                operation_context=operation_context,
                parent_view_id_override=parent_view_id_override,
                allow_action_invocations=not require_preview,
            )
            if request.preview_digest and request.preview_digest != preview["preview_digest"]:
                raise ScenarioError("preview_stale", "情景、权限或修订内容已变化，请重新预览后提交", 409, "preview_digest")
            if runtime_action_run is not None and runtime_action_run.preview_digest != preview["preview_digest"]:
                raise ScenarioError("preview_stale", "原执行租约绑定的 Scenario 预览已失效，请重新预览", 409, "preview_digest")

            # Do not let a durable claim commit unrelated request mutations.
            pending_changes = (
                bool(self.db.new)
                or bool(self.db.deleted)
                or any(self.db.is_modified(item, include_collections=True) for item in self.db.dirty)
            )
            if pending_changes:
                raise ScenarioError(
                    "revision_pending_state",
                    "Scenario revision must claim its ActionRun before unrelated changes are staged",
                    409,
                )

            if runtime_action_run is None:
                # A second lookup under the Scenario lock closes the race with a
                # same-key request that claimed while this request prepared its preview.
                existing_run, claim_token, replayed = claim_idempotent_scenario_action_run(
                    self.db,
                    ontology_id=self.ontology_id,
                    actor_id=actor_id,
                    scope=scope,
                    request_id=request.client_request_id,
                    request_digest=request_digest,
                    scenario_id=scenario_id,
                )
                if existing_run is not None:
                    runtime_action_run = existing_run
                    if replayed:
                        if request.preview_digest and request.preview_digest != existing_run.preview_digest:
                            raise ScenarioError("idempotency_conflict", "Request ID was used with another Scenario preview", 409)
                        revision_number = (existing_run.after_snapshot or {}).get("scenario_revision")
                        revision = self.db.query(ScenarioRevision).filter_by(
                            scenario_id=scenario_id, revision=revision_number, status="ready",
                        ).first() if revision_number is not None else None
                        if revision is None:
                            raise ScenarioError("action_request_incomplete", "ActionRun 已完成但修订记录不可读取", 409)
                        return revision
                    # Reclaim commits the new token; re-lock and repeat the preview
                    # before allowing this attempt to approach the graph store.
                    locked_scenario = self.db.query(ScenarioResource).filter_by(
                        id=scenario_id, ontology_id=self.ontology_id,
                    ).populate_existing().with_for_update().first()
                    if locked_scenario is None:
                        raise ScenarioError("not_found", "Scenario not found", 404)
                    self.get(locked_scenario.id, write=True)
                    preview = self.preview_revision(
                        locked_scenario, request,
                        replace_all=replace_all,
                        operation_context=operation_context,
                        parent_view_id_override=parent_view_id_override,
                        allow_action_invocations=not require_preview,
                    )
                    if request.preview_digest and request.preview_digest != preview["preview_digest"]:
                        raise ScenarioError("preview_stale", "情景、权限或修订内容已变化，请重新预览后提交", 409, "preview_digest")
                    if runtime_action_run.preview_digest != preview["preview_digest"]:
                        raise ScenarioError("preview_stale", "原执行租约绑定的 Scenario 预览已失效，请重新预览", 409, "preview_digest")
                else:
                    runtime_action_run, claim_token = create_scenario_revision_action_run_claim(
                        self.db,
                        ontology_id=self.ontology_id,
                        scenario_id=scenario_id,
                        actor_id=actor_id,
                        actor_kind=actor_kind,
                        request_id=request.client_request_id,
                        request_digest=request_digest,
                        preview_digest=preview["preview_digest"],
                        permission_digest=preview["permission_digest"],
                        expected_etag=request.expected_etag,
                        source=source,
                        preview_mode=(
                            "typed_changeset_preview" if require_preview
                            else "server_derived_revision"
                        ),
                        confirmation_mode=(
                            "human_confirmed_preview" if require_preview and actor_kind == "user"
                            else "user_confirmed_operation" if actor_kind == "user"
                            else "system_generated"
                        ),
                        edit_count=preview["edit_count"],
                        operation_context=operation_context,
                    )

            if not claim_token:
                raise ScenarioError("action_claim_missing", "Scenario revision 缺少有效执行租约", 409)
            from app.services.v2.action_runtime import (
                renew_scenario_action_run_claim,
                scenario_action_run_claim_manifest,
            )
            runtime_action_run = renew_scenario_action_run_claim(
                self.db,
                run_id=runtime_action_run.id,
                claim_token=claim_token,
                actor_id=actor_id,
                actor_kind=actor_kind,
            )
            self.db.commit()
            self.db.refresh(runtime_action_run)
            final_request = request.model_copy(update={
                "permission_digest": preview["permission_digest"],
                "preview_digest": preview["preview_digest"],
            })
            runtime_manifest = {
                "action_input_hash": request_digest,
                "action_request_digest": request_digest,
                "expected_etag": request.expected_etag,
                "permission_digest": preview["permission_digest"],
                "preview_digest": preview["preview_digest"],
                "preview_mode": (
                    "typed_changeset_preview" if require_preview
                    else "server_derived_revision"
                ),
                "confirmation_mode": (
                    "human_confirmed_preview" if require_preview and actor_kind == "user"
                    else "user_confirmed_operation" if actor_kind == "user"
                    else "system_generated"
                ),
                "approval_mode": "not_required",
                "revision_preview_contract": "scenario-revision-preview-v1",
            }
            runtime_manifest.update(scenario_action_run_claim_manifest(
                runtime_action_run, claim_token, request_digest,
            ))
            return self.revision(
                locked_scenario,
                final_request,
                replace_all=replace_all,
                runtime_manifest=runtime_manifest,
                runtime_action_run=runtime_action_run,
                operation_context=operation_context,
                before_scenario_relock=before_scenario_relock,
                after_scenario_relock=after_scenario_relock,
                before_sql_commit=before_sql_commit,
                parent_view_id_override=parent_view_id_override,
            )
        except Exception as exc:
            self.db.rollback()
            if runtime_action_run is not None and claim_token:
                try:
                    fail_scenario_action_run_claim(
                        self.db,
                        run_id=runtime_action_run.id,
                        claim_token=claim_token,
                        error=exc.code if isinstance(exc, ScenarioError) else "scenario_revision_failed",
                        actor_id=actor_id,
                        actor_kind=actor_kind,
                    )
                except Exception:
                    self.db.rollback()
            raise

    def revision(self, scenario, request, *, replace_all=False,
                 runtime_manifest: dict | None = None, runtime_action_run=None,
                 operation_context: dict | None = None,
                 before_scenario_relock: Callable[[], None] | None = None,
                 after_scenario_relock: Callable[[ScenarioResource], None] | None = None,
                 before_sql_commit: Callable[[], None] | None = None,
                 parent_view_id_override: str | None = None):
        if runtime_action_run is None:
            return self.publish_revision(
                scenario, request,
                replace_all=replace_all,
                operation_context=operation_context,
                parent_view_id_override=parent_view_id_override,
                before_scenario_relock=before_scenario_relock,
                after_scenario_relock=after_scenario_relock,
                before_sql_commit=before_sql_commit,
            )
        # Serialize writers for a Scenario so a repeated Action request can
        # observe and replay the first committed revision before checking the
        # now-stale etag. ``populate_existing`` refreshes an instance that may
        # already be present in this request's identity map.
        locked_scenario = self.db.query(ScenarioResource).filter_by(
            id=scenario.id, ontology_id=self.ontology_id,
        ).populate_existing().with_for_update().first()
        if locked_scenario is None:
            raise ScenarioError("not_found", "Scenario not found", 404)
        scenario = locked_scenario
        initial_head_revision = scenario.head_revision
        initial_etag = scenario.etag
        self.get(scenario.id, write=True)
        if scenario.status != "active":
            raise ScenarioError("scenario_archived", "Archived scenarios cannot be edited", 409)
        canonical = request.model_dump(mode="json", exclude={"preview_digest"})
        payload_envelope = {"request": canonical, "replace_all": replace_all}
        if operation_context is not None:
            payload_envelope["operation_context"] = operation_context
        payload_hash = stable_hash(payload_envelope)
        # Registered Actions, Workbench submissions, Supplier Study
        # submissions, and generic revisions all publish under a fenced claim.
        leased_action_run = (
            runtime_action_run is not None
            and getattr(runtime_action_run, "execution_attempt", 0) > 0
        )
        if runtime_action_run is not None and not leased_action_run:
            raise ScenarioError(
                "action_claim_missing",
                "Scenario revision ActionRun has no fenced execution claim",
                409,
            )
        if not leased_action_run and any((
            before_scenario_relock, after_scenario_relock, before_sql_commit,
        )):
            raise ScenarioError(
                "revision_hook_requires_claim",
                "Scenario revision transaction hooks require a fenced ActionRun claim",
                409,
            )
        if leased_action_run:
            from app.services.v2.action_runtime import require_scenario_action_run_claim
            if not isinstance(runtime_manifest, dict):
                raise ScenarioError(
                    "action_runtime_manifest_missing",
                    "Scenario ActionRun finalization requires its execution claim manifest", 500)
            runtime_action_run = require_scenario_action_run_claim(
                self.db, runtime_manifest, scenario.id,
            )
        existing = self.db.query(ScenarioChangeSet).filter_by(scenario_id=scenario.id, client_request_id=request.client_request_id).first()
        if existing:
            if existing.payload_hash != payload_hash:
                raise ScenarioError("idempotency_conflict", "Request id was already used with another payload", 409, "client_request_id")
            existing_report = existing.validation_report or {}
            if operation_context is not None and existing_report.get("operation_context") != operation_context:
                raise ScenarioError("idempotency_conflict", "Request id was already used for another Scenario operation", 409, "client_request_id")
            existing_manifest = existing_report.get("runtime_manifest")
            if runtime_manifest is not None and existing_manifest != runtime_manifest:
                raise ScenarioError("idempotency_conflict", "Request id was already used with another Action manifest", 409, "client_request_id")
            revision_id = existing_report.get("revision_id")
            revision = self.db.get(ScenarioRevision, revision_id) if revision_id else None
            if revision is None:
                raise ScenarioError("request_incomplete", "已记录的请求缺少可重放的情景修订", 409, "client_request_id")
            return revision
        # Existing request keys must be replayable after the head advanced.
        # Validate revision/etag preconditions only for a new publication.
        canonical, _ = self.validate(scenario, request)
        if request.expected_etag != scenario.etag:
            raise ScenarioError("etag_conflict", "Scenario changed since it was read", 409, "expected_etag")
        parent_view_id = parent_view_id_override or (
            scenario.base_view_id
            if replace_all or scenario.head_revision == 0
            else self.db.query(ScenarioRevision).filter_by(
                scenario_id=scenario.id, revision=scenario.head_revision,
            ).one().result_view_id
        )
        parent_view = self.db.get(QueryDataView, parent_view_id)
        if parent_view is None:
            raise ScenarioError("scenario_parent_view_missing", "Scenario parent view is unavailable", 409)
        # Scenario ACL is checked above. Use the view's recorded creator for
        # its internal owner check so an authorized branch collaborator can
        # publish on a checkpoint created by another editor.
        require_ready(self.db, parent_view.id, self.ontology_id,
                      principal_id=parent_view.created_by, is_admin=False)
        revision_number = scenario.head_revision + 1
        source = FalkorReadAdapter(self.graph._graph(parent_view.graph_key), graph_ontology_id=parent_view.graph_key).read(self.ontology_id)
        # Enforce access at the shared revision boundary so direct typed
        # ChangeSet callers and scheduled workers cannot bypass object/property/link policy.
        from app.services.v2.action_authorization import authorize_compiled_edits
        permission_digest = authorize_compiled_edits(
            self.db, self.ontology_id, self.user, source, request.edits)
        if request.permission_digest and request.permission_digest != permission_digest:
            raise ScenarioError("permission_changed", "权限已变化；请重新预览", 409)
        authorization = self.authorization_provenance(scenario.id, permission_digest)
        if (runtime_manifest or {}).get("revision_preview_contract") == "scenario-revision-preview-v1":
            actual_preview_digest = self._revision_preview_digest(
                scenario, request, parent_view, permission_digest,
                replace_all=replace_all, operation_context=operation_context,
            )
            if (
                actual_preview_digest != runtime_manifest.get("preview_digest")
                or actual_preview_digest != request.preview_digest
            ):
                raise ScenarioError(
                    "preview_stale",
                    "情景、权限或修订内容已变化，请重新预览后提交",
                    409,
                    "preview_digest",
                )

        revision_number = scenario.head_revision + 1
        graph_key = f"scenario_{scenario.id}_{revision_number}_{payload_hash[:24]}"
        if leased_action_run:
            from app.services.v2.action_runtime import (
                SCENARIO_ACTION_LEASE_SECONDS,
                record_action_run_claim_event,
                require_scenario_action_run_claim,
                scenario_build_resource_digest,
            )

            # Commit only the lease renewal, immutable resource identity and
            # graph-write intent before crossing into FalkorDB. This lets a
            # later worker distinguish a safe orphan from an untracked graph.
            runtime_action_run = require_scenario_action_run_claim(
                self.db, runtime_manifest, scenario.id,
            )
            runtime_action_run.scenario_build_graph_key = graph_key
            runtime_action_run.scenario_build_revision = revision_number
            runtime_action_run.execution_lease_until = now() + timedelta(
                seconds=SCENARIO_ACTION_LEASE_SECONDS,
            )
            resource_digest = scenario_build_resource_digest(graph_key, revision_number)
            record_action_run_claim_event(
                self.db, runtime_action_run, "lease_renewed",
                actor_id=self.execution_actor_id, actor_kind=self.execution_actor_kind,
            )
            record_action_run_claim_event(
                self.db, runtime_action_run, "graph_write_started",
                actor_id=self.execution_actor_id, actor_kind=self.execution_actor_kind,
                resource_digest=resource_digest,
            )
            self.db.commit()

            # The first commit releases the Scenario lock. Reacquire it, then
            # recheck the head, ACL, parent readiness and edit authorization
            # before performing any external graph mutation.
            if before_scenario_relock is not None:
                before_scenario_relock()
            locked_scenario = self.db.query(ScenarioResource).filter_by(
                id=scenario.id, ontology_id=self.ontology_id,
            ).populate_existing().with_for_update().first()
            if locked_scenario is None:
                raise ScenarioError("not_found", "Scenario not found", 404)
            scenario = locked_scenario
            self.get(scenario.id, write=True)
            if scenario.status != "active":
                raise ScenarioError("scenario_archived", "Archived scenarios cannot be edited", 409)
            if (
                scenario.head_revision != initial_head_revision
                or scenario.etag != initial_etag
                or request.expected_etag != scenario.etag
            ):
                raise ScenarioError("etag_conflict", "Scenario changed while the Action was being prepared", 409, "expected_etag")
            parent_view = self.db.get(QueryDataView, parent_view_id)
            if parent_view is None:
                raise ScenarioError("scenario_parent_view_missing", "Scenario parent view is unavailable", 409)
            require_ready(self.db, parent_view.id, self.ontology_id,
                          principal_id=parent_view.created_by, is_admin=False)
            source = FalkorReadAdapter(
                self.graph._graph(parent_view.graph_key),
                graph_ontology_id=parent_view.graph_key,
            ).read(self.ontology_id)
            rechecked_permission_digest = authorize_compiled_edits(
                self.db, self.ontology_id, self.user, source, request.edits,
            )
            if (
                rechecked_permission_digest != permission_digest
                or (request.permission_digest and rechecked_permission_digest != request.permission_digest)
            ):
                raise ScenarioError("permission_changed", "权限已变化；请重新预览", 409)
            permission_digest = rechecked_permission_digest
            authorization = self.authorization_provenance(scenario.id, permission_digest)
            if (runtime_manifest or {}).get("revision_preview_contract") == "scenario-revision-preview-v1":
                actual_preview_digest = self._revision_preview_digest(
                    scenario, request, parent_view, permission_digest,
                    replace_all=replace_all, operation_context=operation_context,
                )
                if actual_preview_digest != runtime_manifest.get("preview_digest"):
                    raise ScenarioError(
                        "preview_stale",
                        "情景、权限或修订内容已变化，请重新预览后提交",
                        409,
                        "preview_digest",
                    )
            if after_scenario_relock is not None:
                after_scenario_relock(scenario)
            if leased_action_run:
                runtime_action_run = require_scenario_action_run_claim(
                    self.db, runtime_manifest, scenario.id,
                )
                if (
                    runtime_action_run.scenario_build_graph_key != graph_key
                    or runtime_action_run.scenario_build_revision != revision_number
                ):
                    raise ScenarioError(
                        "action_build_marker_changed",
                        "Scenario Action graph build marker changed before publication", 409,
                    )

        changeset = ScenarioChangeSet(scenario_id=scenario.id, base_revision=request.base_revision,
                                      ordered_edits=canonical["edits"], payload_hash=payload_hash,
                                      client_request_id=request.client_request_id,
                                      actor_kind=self.execution_actor_kind,
                                      actor_id=self.execution_actor_id,
                                      validation_report={"status": "validated",
                                                         "authorization": authorization,
                                                         **({"runtime_manifest": runtime_manifest}
                                                            if runtime_manifest is not None else {})})
        self.db.add(changeset); self.db.flush()
        objects = {key: dict(value) for key, value in source.objects.items()}
        edges = list(source.edges)
        for raw in sorted(request.edits, key=lambda item: item.sequence):
            edit = Edit.model_validate(raw)
            if edit.op == "set_property":
                key = _target_key(edit.target)
                if key not in objects: raise ScenarioError("object_not_found", "Target object does not exist", path=f"edits[{edit.sequence}]")
                if "expected_old_value" in edit.model_fields_set and objects[key].get(edit.property) != edit.expected_old_value:
                    raise ScenarioError("edit_conflict", "Target property changed since the Action was prepared", 409, f"edits[{edit.sequence}]")
                objects[key][edit.property] = edit.value
            elif edit.op == "unset_property":
                key = _target_key(edit.target)
                if key not in objects: raise ScenarioError("object_not_found", "Target object does not exist", path=f"edits[{edit.sequence}]")
                if "expected_old_value" in edit.model_fields_set and objects[key].get(edit.property) != edit.expected_old_value:
                    raise ScenarioError("edit_conflict", "Target property changed since the Action was prepared", 409, f"edits[{edit.sequence}]")
                objects[key].pop(edit.property, None)
            elif edit.op == "delete_object":
                key = _target_key(edit.target)
                if key not in objects: raise ScenarioError("object_not_found", "Target object does not exist", path=f"edits[{edit.sequence}]")
                objects.pop(key)
                edges = [edge for edge in edges if edge[0] != key and edge[2] != key]
            elif edit.op == "create_object":
                object_id = _target_id(edit.target) if edit.target else f"{scenario.id}:{request.client_request_id}:{edit.sequence}"
                objects[(edit.object_type or "Entity", object_id)] = dict(edit.properties)
            elif edit.op in {"add_link", "remove_link"}:
                link = edit.link
                edge = ((link.source.concrete_type, link.source.object_id), link.relation_type, (link.target.concrete_type, link.target.object_id))
                if edit.op == "add_link" and edge not in edges: edges.append(edge)
                if edit.op == "remove_link": edges = [item for item in edges if item != edge]
        projected_digest = stable_hash({"objects": sorted(objects.items()), "edges": sorted(edges)})
        expected_digest = (operation_context or {}).get("expected_content_digest")
        if expected_digest and projected_digest != expected_digest:
            raise ScenarioError("checkpoint_restore_mismatch", "Restored edits do not reproduce the checkpoint snapshot", 409)
        view = QueryDataView(ontology_id=self.ontology_id, source_manifest_digest=parent_view.content_digest or parent_view.source_manifest_digest,
                             base_view_id=parent_view.id, changeset_digest=payload_hash, changeset_version="scenario-changeset-v1",
                             metadata_digest=parent_view.metadata_digest, graph_key=graph_key, status="building", created_by=self.user.id,
                             retention_until=(now() + timedelta(seconds=scenario.ttl_seconds)) if scenario.ttl_seconds else None)
        self.db.add(view); self.db.flush()
        try:
            self.graph.upsert_instances(graph_key, [{"id": oid, "entity_type": typ, "properties": props} for (typ, oid), props in objects.items()])
            self.graph.upsert_relations(graph_key, [{"source": src[1], "target": dst[1], "type": rel} for src, rel, dst in edges])
            view.object_count, view.edge_count = len(objects), len(edges); view.content_digest = projected_digest; view.status = "ready"
            revision = ScenarioRevision(scenario_id=scenario.id, revision=revision_number, parent_revision=scenario.head_revision,
                                        changeset_id=changeset.id, result_view_id=view.id, content_digest=view.content_digest,
                                        created_by=self.user.id, status="ready")
            self.db.add(revision); self.db.flush()
            changeset.validation_report = {"status": "published", "revision_id": revision.id,
                                           "replace_all": replace_all,
                                           "permission_digest": permission_digest,
                                           "authorization": authorization,
                                           **({"operation_context": operation_context}
                                              if operation_context is not None else {}),
                                           **({"runtime_manifest": runtime_manifest}
                                              if runtime_manifest is not None else {})}
            scenario.head_revision, scenario.etag, scenario.updated_at = revision_number, scenario.etag + 1, now()
            self.db.add(ScenarioAudit(
                scenario_id=scenario.id,
                revision=revision_number,
                actor_kind=self.execution_actor_kind,
                actor_id=self.execution_actor_id,
                operation=(operation_context or {}).get("audit_operation", "publish_revision"),
                request_id=request.client_request_id,
                summary={"payload_hash": payload_hash,
                         "authorization": authorization,
                         **({"operation_context": operation_context}
                            if operation_context is not None else {})},
            ))
            if runtime_action_run is not None:
                from app.services.v2.action_runtime import complete_scenario_action_run
                if not runtime_manifest or not runtime_manifest.get("preview_digest"):
                    raise ScenarioError(
                        "action_runtime_manifest_missing",
                        "Scenario ActionRun finalization requires its preview manifest", 500)
                complete_scenario_action_run(
                    self.db,
                    runtime_action_run,
                    revision=revision,
                    parent_view_id=parent_view.id,
                    parent_digest=parent_view.content_digest or parent_view.source_manifest_digest,
                    changeset=changeset,
                    permission_digest=permission_digest or "",
                    preview_digest=runtime_manifest["preview_digest"],
                    actor_id=self.execution_actor_id,
                    actor_kind=self.execution_actor_kind,
                )
            if before_sql_commit is not None:
                before_sql_commit()
            self.db.commit(); self.db.refresh(revision)
            return revision
        except IntegrityError:
            self.db.rollback()
            # A concurrent same-key request may have committed while this
            # request was building the identical isolated graph revision.
            # Resolve the winner from SQL rather than returning a false 422.
            existing = self.db.query(ScenarioChangeSet).filter_by(
                scenario_id=scenario.id,
                client_request_id=request.client_request_id,
            ).first()
            if existing is not None:
                existing_report = existing.validation_report or {}
                if operation_context is not None and existing_report.get("operation_context") != operation_context:
                    raise ScenarioError("idempotency_conflict", "Request id was already used for another Scenario operation", 409, "client_request_id") from None
                if existing.payload_hash != payload_hash:
                    raise ScenarioError("idempotency_conflict", "Request id was already used with another payload", 409, "client_request_id") from None
                if runtime_manifest is not None and existing_report.get("runtime_manifest") != runtime_manifest:
                    raise ScenarioError("idempotency_conflict", "Request id was already used with another Action manifest", 409, "client_request_id") from None
                revision_id = existing_report.get("revision_id")
                revision = self.db.get(ScenarioRevision, revision_id) if revision_id else None
                if revision is not None:
                    return revision
                raise ScenarioError("request_incomplete", "并发请求已记录，但修订结果不可读取", 409, "client_request_id") from None
            raise ScenarioError("revision_conflict", "Scenario changed while this revision was being committed", 409) from None
        except Exception:
            self.db.rollback()
            raise

    def list_revisions(self, scenario):
        return self.db.query(ScenarioRevision).filter_by(scenario_id=scenario.id).order_by(ScenarioRevision.revision.desc()).all()

    @staticmethod
    def checkpoint_response(row):
        return {
            "id": row.id,
            "scenario_id": row.scenario_id,
            "revision": row.revision,
            "result_view_id": row.result_view_id,
            "content_digest": row.content_digest,
            "label": row.label,
            "source_etag": row.source_etag,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat(),
        }

    def create_checkpoint(self, scenario_id, request):
        scenario = self.db.query(ScenarioResource).filter_by(
            id=scenario_id, ontology_id=self.ontology_id,
        ).populate_existing().with_for_update().first()
        if scenario is None:
            raise ScenarioError("not_found", "Scenario not found", 404)
        self.get(scenario.id, write=True)
        if scenario.status != "active":
            raise ScenarioError("scenario_archived", "Archived scenarios cannot be checkpointed", 409)

        revision_number = scenario.head_revision if request.revision is None else request.revision
        request_digest = stable_hash({"label": request.label, "revision": revision_number})
        existing = self.db.query(ScenarioCheckpoint).filter_by(
            scenario_id=scenario.id, client_request_id=request.client_request_id,
        ).first()
        if existing is not None:
            if existing.request_digest != request_digest:
                raise ScenarioError("idempotency_conflict", "Checkpoint request id was used with different inputs", 409, "client_request_id")
            return existing

        if revision_number == 0:
            view = self.db.get(QueryDataView, scenario.base_view_id)
        else:
            revision = self.db.query(ScenarioRevision).filter_by(
                scenario_id=scenario.id, revision=revision_number, status="ready",
            ).first()
            view = self.db.get(QueryDataView, revision.result_view_id) if revision else None
        if view is None:
            raise ScenarioError("revision_not_found", "Scenario revision is unavailable", 404, "revision")
        try:
            view = require_ready(self.db, view.id, self.ontology_id,
                                 principal_id=view.created_by, is_admin=False)
        except ObjectQueryError as exc:
            raise ScenarioError("checkpoint_view_unavailable", "Scenario revision snapshot is unavailable", 409) from exc
        digest = view.content_digest
        if not digest:
            raise ScenarioError("checkpoint_digest_missing", "Scenario revision has no content digest", 409)

        row = ScenarioCheckpoint(
            scenario_id=scenario.id,
            revision=revision_number,
            result_view_id=view.id,
            content_digest=digest,
            label=request.label,
            client_request_id=request.client_request_id,
            request_digest=request_digest,
            source_etag=scenario.etag,
            created_by=self.user.id,
        )
        self.db.add(row)
        self.db.flush()
        self.db.add(ScenarioAudit(
            scenario_id=scenario.id,
            revision=revision_number,
            actor_id=self.user.id,
            operation="create_checkpoint",
            request_id=request.client_request_id,
            summary={"checkpoint_id": row.id, "view_id": view.id,
                     "content_digest": digest, "label": request.label},
        ))
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_checkpoints(self, scenario_id):
        scenario = self.get(scenario_id)
        return self.db.query(ScenarioCheckpoint).filter_by(
            scenario_id=scenario.id,
        ).order_by(ScenarioCheckpoint.created_at.desc(), ScenarioCheckpoint.id).all()

    def branch_from_checkpoint(self, scenario_id, checkpoint_id, request):
        source = self.db.query(ScenarioResource).filter_by(
            id=scenario_id, ontology_id=self.ontology_id,
        ).populate_existing().with_for_update().first()
        if source is None:
            raise ScenarioError("not_found", "Scenario not found", 404)
        self.get(source.id, write=True)
        if source.status != "active":
            raise ScenarioError("scenario_archived", "Archived scenarios cannot be branched", 409)
        checkpoint = self.db.get(ScenarioCheckpoint, checkpoint_id)
        if checkpoint is None or checkpoint.scenario_id != source.id:
            raise ScenarioError("checkpoint_not_found", "Scenario checkpoint not found", 404)

        request_digest = stable_hash({"checkpoint_id": checkpoint.id,
                                      "name": request.name,
                                      "description": request.description})
        prior = self.db.query(ScenarioAudit).filter_by(
            scenario_id=source.id, operation="branch",
            request_id=request.client_request_id,
        ).first()
        if prior is not None:
            if (prior.summary or {}).get("request_digest") != request_digest:
                raise ScenarioError("idempotency_conflict", "Branch request id was used with different inputs", 409, "client_request_id")
            child = self.db.get(ScenarioResource, (prior.summary or {}).get("child_scenario_id"))
            if child is None:
                raise ScenarioError("branch_result_missing", "The previously created branch is unavailable", 409)
            return child

        view = self.db.get(QueryDataView, checkpoint.result_view_id)
        if view is None or view.content_digest != checkpoint.content_digest:
            raise ScenarioError("checkpoint_snapshot_mismatch", "Checkpoint snapshot no longer matches its recorded digest", 409)
        try:
            require_ready(self.db, view.id, self.ontology_id,
                          principal_id=view.created_by, is_admin=False)
        except ObjectQueryError as exc:
            raise ScenarioError("checkpoint_view_unavailable", "Checkpoint snapshot is unavailable", 409) from exc

        expiry = scenario_expires_at(source)
        if expiry and expiry <= now():
            raise ScenarioError("scenario_expired", "Scenario retention period has expired", 410)
        child = ScenarioResource(
            ontology_id=source.ontology_id,
            owner_id=self.user.id,
            name=request.name,
            description=request.description,
            base_view_id=view.id,
            ttl_seconds=source.ttl_seconds if expiry else None,
            expires_at=expiry,
            protected_demo=source.protected_demo,
            mode="pinned",
            parent_scenario_id=source.id,
            parent_checkpoint_id=checkpoint.id,
        )
        self.db.add(child)
        self.db.flush()
        self.db.add(ScenarioAudit(
            scenario_id=source.id,
            actor_id=self.user.id,
            operation="branch",
            request_id=request.client_request_id,
            summary={"child_scenario_id": child.id,
                     "checkpoint_id": checkpoint.id,
                     "checkpoint_revision": checkpoint.revision,
                     "request_digest": request_digest},
        ))
        self.db.add(ScenarioAudit(
            scenario_id=child.id,
            actor_id=self.user.id,
            operation="branch_created",
            summary={"parent_scenario_id": source.id,
                     "checkpoint_id": checkpoint.id,
                     "checkpoint_revision": checkpoint.revision,
                     "content_digest": checkpoint.content_digest},
        ))
        self.db.commit()
        self.db.refresh(child)
        return child

    def _scenario_head_view(self, scenario):
        if scenario.head_revision == 0:
            view = self.db.get(QueryDataView, scenario.base_view_id)
        else:
            revision = self.db.query(ScenarioRevision).filter_by(
                scenario_id=scenario.id, revision=scenario.head_revision,
                status="ready",
            ).first()
            view = self.db.get(QueryDataView, revision.result_view_id) if revision else None
        if view is None:
            raise ScenarioError("revision_not_found", "Current Scenario revision snapshot is unavailable", 409)
        try:
            return require_ready(self.db, view.id, self.ontology_id,
                                 principal_id=view.created_by, is_admin=False)
        except ObjectQueryError as exc:
            raise ScenarioError("revision_view_unavailable", "Current Scenario revision snapshot is unavailable", 409) from exc

    def _read_checkpoint_state(self, view):
        return FalkorReadAdapter(
            self.graph._graph(view.graph_key), graph_ontology_id=view.graph_key,
        ).read(self.ontology_id)

    def _restore_edits(self, current, target):
        edits = []

        def add(op, *, target_ref=None, object_type=None, properties=None,
                property_name=None, value=None, old=None, link=None):
            edits.append(Edit(
                sequence=len(edits), op=op, target=target_ref,
                object_type=object_type, properties=properties or {},
                property=property_name, value=value,
                expected_old_value=old,
                link=link,
            ))

        current_objects, target_objects = current.objects, target.objects
        deleted = set(current_objects) - set(target_objects)
        created = set(target_objects) - set(current_objects)
        for kind, object_id in sorted(deleted):
            add("delete_object", target_ref=Target(
                ontology_id=self.ontology_id, concrete_type=kind, object_id=object_id,
            ))
        for kind, object_id in sorted(created):
            add("create_object", target_ref=Target(
                ontology_id=self.ontology_id, concrete_type=kind, object_id=object_id,
            ), object_type=kind, properties=target_objects[(kind, object_id)])
        for key in sorted(set(current_objects) & set(target_objects)):
            old_properties, new_properties = current_objects[key], target_objects[key]
            for name in sorted(set(old_properties) | set(new_properties)):
                if old_properties.get(name) == new_properties.get(name) and (name in old_properties) == (name in new_properties):
                    continue
                target_ref = Target(ontology_id=self.ontology_id,
                                    concrete_type=key[0], object_id=key[1])
                if name in new_properties:
                    add("set_property", target_ref=target_ref, property_name=name,
                        value=new_properties[name], old=old_properties.get(name))
                else:
                    add("unset_property", target_ref=target_ref,
                        property_name=name, old=old_properties.get(name))

        current_edges, target_edges = set(current.edges), set(target.edges)
        for edge in sorted(current_edges - target_edges, key=repr):
            source, relation, destination = edge
            if source in deleted or destination in deleted:
                continue
            add("remove_link", link=LinkTarget(
                ontology_id=self.ontology_id, relation_type=relation,
                source=Target(ontology_id=self.ontology_id,
                              concrete_type=source[0], object_id=source[1]),
                target=Target(ontology_id=self.ontology_id,
                              concrete_type=destination[0], object_id=destination[1]),
            ))
        for edge in sorted(target_edges - current_edges, key=repr):
            source, relation, destination = edge
            add("add_link", link=LinkTarget(
                ontology_id=self.ontology_id, relation_type=relation,
                source=Target(ontology_id=self.ontology_id,
                              concrete_type=source[0], object_id=source[1]),
                target=Target(ontology_id=self.ontology_id,
                              concrete_type=destination[0], object_id=destination[1]),
            ))
        if len(edits) > 500:
            raise ScenarioError("restore_limit_exceeded", "Checkpoint restore exceeds the 500-edit safety limit; branch from the checkpoint instead", 413, "checkpoint_id")
        return edits

    def restore_checkpoint(self, scenario_id, checkpoint_id, request):
        scenario = self.db.query(ScenarioResource).filter_by(
            id=scenario_id, ontology_id=self.ontology_id,
        ).populate_existing().with_for_update().first()
        if scenario is None:
            raise ScenarioError("not_found", "Scenario not found", 404)
        self.get(scenario.id, write=True)
        if scenario.status != "active":
            raise ScenarioError("scenario_archived", "Archived scenarios cannot be restored", 409)
        checkpoint = self.db.get(ScenarioCheckpoint, checkpoint_id)
        if checkpoint is None or checkpoint.scenario_id != scenario.id:
            raise ScenarioError("checkpoint_not_found", "Scenario checkpoint not found", 404)
        request_digest = stable_hash({"checkpoint_id": checkpoint.id})

        prior = self.db.query(ScenarioAudit).filter_by(
            scenario_id=scenario.id, operation="restore_checkpoint",
            request_id=request.client_request_id,
        ).first()
        if prior is not None:
            summary = prior.summary or {}
            prior_context = summary.get("operation_context") or {}
            if summary.get("request_digest", prior_context.get("request_digest")) != request_digest:
                raise ScenarioError("idempotency_conflict", "Restore request id was used for another checkpoint", 409, "client_request_id")
            revision = self.db.query(ScenarioRevision).filter_by(
                scenario_id=scenario.id, revision=prior.revision,
            ).first() if prior.revision else None
            if revision is not None:
                return revision
            return summary.get("response")
        prior_change = self.db.query(ScenarioChangeSet).filter_by(
            scenario_id=scenario.id,
            client_request_id=request.client_request_id,
        ).first()
        if prior_change is not None:
            raise ScenarioError("idempotency_conflict", "Restore request id was already used for a Scenario revision", 409, "client_request_id")
        if scenario.etag != request.expected_etag:
            raise ScenarioError("etag_conflict", "Scenario changed since it was read", 409, "expected_etag")

        target_view = self.db.get(QueryDataView, checkpoint.result_view_id)
        if target_view is None or target_view.content_digest != checkpoint.content_digest:
            raise ScenarioError("checkpoint_snapshot_mismatch", "Checkpoint snapshot no longer matches its recorded digest", 409)
        try:
            target_view = require_ready(self.db, target_view.id, self.ontology_id,
                                        principal_id=target_view.created_by, is_admin=False)
        except ObjectQueryError as exc:
            raise ScenarioError("checkpoint_view_unavailable", "Checkpoint snapshot is unavailable", 409) from exc
        current_view = self._scenario_head_view(scenario)
        current_digest = current_view.content_digest
        if current_digest == checkpoint.content_digest:
            response = {
                "scenario_id": scenario.id,
                "revision": scenario.head_revision,
                "parent_revision": scenario.head_revision - 1 if scenario.head_revision else None,
                "changeset_id": None,
                "result_view_id": current_view.id,
                "content_digest": current_digest,
                "status": "ready",
                "no_op": True,
            }
            self.db.add(ScenarioAudit(
                scenario_id=scenario.id,
                revision=scenario.head_revision,
                actor_id=self.user.id,
                operation="restore_checkpoint",
                request_id=request.client_request_id,
                summary={"checkpoint_id": checkpoint.id,
                         "request_digest": request_digest,
                         "revision": scenario.head_revision,
                         "response": response,
                         "no_op": True},
            ))
            self.db.commit()
            return response

        current_state = self._read_checkpoint_state(current_view)
        target_state = self._read_checkpoint_state(target_view)
        edits = self._restore_edits(current_state, target_state)
        if not edits:
            raise ScenarioError("checkpoint_digest_mismatch", "Different checkpoint digests produced no restorable edits", 409)
        operation_context = {
            "audit_operation": "restore_checkpoint",
            "checkpoint_id": checkpoint.id,
            "checkpoint_revision": checkpoint.revision,
            "checkpoint_digest": checkpoint.content_digest,
            "expected_content_digest": checkpoint.content_digest,
            "request_digest": request_digest,
        }
        restore_request = RevisionRequest(
            base_revision=scenario.head_revision,
            expected_etag=request.expected_etag,
            client_request_id=request.client_request_id,
            edits=edits,
        )
        return self.revision(scenario, restore_request,
                             operation_context=operation_context)


def resolve_scenario_context(db, context: ExecutionContext, ontology_id: str, user) -> ExecutionContext:
    """Resolve a durable scenario pointer into the existing QueryDataView context."""
    if not context.scenario_id:
        return context
    scenario = db.get(ScenarioResource, context.scenario_id)
    if not scenario or scenario.ontology_id != ontology_id:
        raise ScenarioError("not_found", "Scenario context not found", 404, "context.scenario_id")
    if scenario_is_expired(scenario):
        raise ScenarioError('scenario_expired', 'Scenario retention period has expired', 410)
    grant = db.get(ScenarioGrant, (scenario.id, user.id))
    if user.role != "admin" and scenario.owner_id != user.id and not grant:
        raise ScenarioError("forbidden", "Scenario context access denied", 403, "context.scenario_id")
    revision = context.scenario_revision
    view_id = scenario.base_view_id
    if revision:
        row = db.query(ScenarioRevision).filter_by(scenario_id=scenario.id, revision=revision, status="ready").first()
        if not row:
            raise ScenarioError("not_found", "Scenario revision not found", 404, "context.scenario_revision")
        view_id = row.result_view_id
    else:
        initial = db.query(ScenarioRevision).filter_by(scenario_id=scenario.id, revision=0, status='ready').first()
        if initial:
            view_id = initial.result_view_id
    pinned_view = db.get(QueryDataView, view_id)
    if pinned_view is None:
        raise ScenarioError("view_unavailable", "Scenario data view is unavailable", 409)
    view = require_ready(db, view_id, ontology_id,
                         principal_id=pinned_view.created_by, is_admin=False)
    return context.model_copy(update={"data_view_id": view.id, "consistency": "snapshot", "revision_policy": "pinned",
                                      "revision_id": f"{scenario.id}:{revision or 0}", "metadata_digest": view.metadata_digest})
