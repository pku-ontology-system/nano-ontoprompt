"""Shared lifecycle, authorization, and compilation boundary for ontology Actions.

Action definitions may be compiled by different entry points, but every
compiler receives the same policy-projected snapshot and every resulting edit
batch is checked against the raw execution snapshot before it can be applied.
Persisted ActionRun state transitions use the lifecycle helpers in this module.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

from app.models.v2.action import (
    OntologyActionRun,
    OntologyActionRunClaimEvent,
    OntologyActionRunEvent,
)
from app.models.v2.scenario import ScenarioChangeSet, ScenarioRevision
from app.services.v2.action_authorization import (
    authorize_action_snapshot,
    authorize_compiled_edits,
)
from app.services.v2.object_query.normalize import stable_hash
from app.services.v2.scenarios import ScenarioError


_ACTION_RUN_TRANSITIONS = {
    "queued": {"running", "failed", "cancelled"},
    "running": {"completed", "failed", "cancelled", "reconciliation_required"},
    # Durable write paths may need an explicit reconciliation pass after an
    # uncertain cross-store commit. Successful reconciliation closes the same
    # ActionRun instead of creating a second, disconnected success record.
    # A route may explicitly resume a pinned, idempotent projection after it
    # has recorded an uncertain outcome. The retry is an audited transition;
    # generic idempotency lookups still refuse to resume it implicitly.
    "reconciliation_required": {"running", "completed"},
    "completed": {"reverted"},
}

SCENARIO_ACTION_LEASE_SECONDS = 120


@dataclass(frozen=True)
class ActionInvocationRequest:
    """One typed Action definition invocation in a shared runtime request."""

    action_type_id: str
    parameters: dict[str, Any] = field(default_factory=dict)
    target_object_id: str | None = None

    def compiler_payload(self) -> dict[str, Any]:
        payload = {
            "action_type_id": self.action_type_id,
            "parameters": dict(self.parameters),
        }
        if self.target_object_id:
            payload["target_object_id"] = self.target_object_id
        return payload


@dataclass(frozen=True)
class ActionRequest:
    """Canonical typed input envelope shared by standard Action compiler callers.

    HTTP and persisted resources keep their existing external shapes. Adapters
    create this envelope before compilation so identity, invocation order, and
    execution context have one internal representation.
    """

    ontology_id: str
    compiler_scope: str
    invocations: tuple[ActionInvocationRequest, ...]
    source: str
    execution_context: dict[str, Any] = field(default_factory=dict)
    action_type_versions: dict[str, int] = field(default_factory=dict)
    definition_versions: dict[str, str | int] = field(default_factory=dict)
    expected_etag: str | None = None
    client_request_id: str | None = None
    preview_digest: str | None = None
    permission_digest: str | None = None

    @classmethod
    def from_invocations(
        cls,
        *,
        ontology_id: str,
        compiler_scope: str,
        source: str,
        invocations: Iterable[dict[str, Any]],
        execution_context: dict[str, Any] | None = None,
        action_type_versions: dict[str, int] | None = None,
        definition_versions: dict[str, str | int] | None = None,
        expected_etag: str | int | None = None,
        client_request_id: str | None = None,
        preview_digest: str | None = None,
        permission_digest: str | None = None,
    ) -> "ActionRequest":
        typed_invocations = []
        for index, invocation in enumerate(invocations):
            action_type_id = invocation.get("action_type_id")
            parameters = invocation.get("parameters", {})
            target_object_id = invocation.get("target_object_id")
            if not isinstance(action_type_id, str) or not action_type_id.strip():
                raise ScenarioError(
                    "invalid_action_request",
                    f"Action invocation {index} requires an action type ID",
                    422,
                )
            if not isinstance(parameters, dict):
                raise ScenarioError(
                    "invalid_action_request",
                    f"Action invocation {index} parameters must be an object",
                    422,
                )
            if target_object_id is not None and not isinstance(target_object_id, str):
                raise ScenarioError(
                    "invalid_action_request",
                    f"Action invocation {index} target ID must be a string",
                    422,
                )
            typed_invocations.append(ActionInvocationRequest(
                action_type_id=action_type_id,
                parameters=dict(parameters),
                target_object_id=target_object_id,
            ))
        normalized_definition_versions = {}
        for definition_id, version in (definition_versions or {}).items():
            if (
                not isinstance(definition_id, str)
                or not definition_id.strip()
                or isinstance(version, bool)
                or not isinstance(version, (str, int))
                or (isinstance(version, str) and not version.strip())
            ):
                raise ScenarioError(
                    "invalid_action_request",
                    "Definition versions require a stable ID and a string or integer version",
                    422,
                )
            normalized_definition_versions[definition_id] = version
        return cls(
            ontology_id=ontology_id,
            compiler_scope=compiler_scope,
            invocations=tuple(typed_invocations),
            source=source,
            execution_context=dict(execution_context or {}),
            action_type_versions={
                str(action_type_id): int(version)
                for action_type_id, version in (action_type_versions or {}).items()
            },
            definition_versions=normalized_definition_versions,
            expected_etag=str(expected_etag) if expected_etag is not None else None,
            client_request_id=client_request_id,
            preview_digest=preview_digest,
            permission_digest=permission_digest,
        )

    def compiler_payloads(self) -> list[dict[str, Any]]:
        return [invocation.compiler_payload() for invocation in self.invocations]

    def manifest(self) -> dict[str, Any]:
        manifest = {
            "ontology_id": self.ontology_id,
            "compiler_scope": self.compiler_scope,
            "source": self.source,
            "invocations": self.compiler_payloads(),
            "action_type_versions": dict(sorted(self.action_type_versions.items())),
            "execution_context": dict(self.execution_context),
            "expected_etag": self.expected_etag,
            "client_request_id": self.client_request_id,
        }
        if self.definition_versions:
            manifest["definition_versions"] = dict(sorted(self.definition_versions.items()))
        return manifest


def attribute_action_run(
    run: OntologyActionRun,
    *,
    requester_id: str | None,
    reviewer_id: str | None,
    executor_id: str | None,
) -> OntologyActionRun:
    """Record attributable human roles without inferring unknown history."""
    run.requested_by = str(requester_id) if requester_id is not None else None
    run.reviewed_by = str(reviewer_id) if reviewer_id is not None else None
    run.executed_by = str(executor_id) if executor_id is not None else None
    return run


def create_action_run_record(
    *,
    db,
    action_type_id: str | None,
    ontology_id: str,
    target_object_id: str | None = None,
    parameters: dict | None = None,
    execution_context: dict | None = None,
    initial_status: str = "queued",
    requester_id: str | None = None,
    reviewer_id: str | None = None,
    executor_id: str | None = None,
    decision_case_id: str | None = None,
    client_request_id: str | None = None,
    idempotency_scope: str | None = None,
    input_digest: str | None = None,
    preview_digest: str | None = None,
    permission_digest: str | None = None,
    before_digest: str | None = None,
    after_digest: str | None = None,
    expected_etag: str | None = None,
    action_version: int | None = None,
    trace_id: str | None = None,
    execution_attempt: int = 0,
    execution_lease_token: str | None = None,
    execution_lease_until: datetime | None = None,
    projection_status: str | None = None,
    event_actor_id: str | None = None,
    event_actor_kind: str | None = None,
) -> OntologyActionRun:
    """Build a new ActionRun through the shared identity and attribution boundary.

    The run and its first audit event join the caller's transaction. Callers
    retain commit ownership so a Scenario revision or DecisionCase update can
    commit atomically with the run where that entry point supports it.
    """
    if initial_status not in {"queued", "running"}:
        raise ScenarioError(
            "invalid_action_run_initial_status",
            f"ActionRun cannot be created directly in {initial_status!r}",
            409,
        )
    run = OntologyActionRun(
        id=str(uuid.uuid4()),
        action_type_id=action_type_id,
        ontology_id=ontology_id,
        target_object_id=target_object_id,
        parameters=dict(parameters or {}),
        execution_context=dict(execution_context or {}),
        status=initial_status,
        decision_case_id=decision_case_id,
        client_request_id=client_request_id,
        idempotency_scope=idempotency_scope,
        input_digest=input_digest,
        preview_digest=preview_digest,
        permission_digest=permission_digest,
        before_digest=before_digest,
        after_digest=after_digest,
        expected_etag=expected_etag,
        action_version=action_version,
        trace_id=trace_id,
        execution_attempt=execution_attempt,
        execution_lease_token=execution_lease_token,
        execution_lease_until=execution_lease_until,
        projection_status=projection_status,
    )
    attribute_action_run(
        run,
        requester_id=requester_id,
        reviewer_id=reviewer_id,
        executor_id=executor_id,
    )
    db.add(run)
    actor_id = event_actor_id or requester_id or executor_id
    _append_action_run_event(
        db,
        run,
        event_type="created",
        from_status=None,
        to_status=initial_status,
        actor_id=actor_id,
        actor_kind=event_actor_kind or ("user" if actor_id else "system"),
    )
    return run


def _event_time_value(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _action_run_event_manifest(
    *,
    action_run_id: str,
    sequence: int,
    event_type: str,
    from_status: str | None,
    to_status: str,
    actor_kind: str,
    actor_id: str | None,
    request_id: str | None,
    trace_id: str | None,
    input_digest: str | None,
    preview_digest: str | None,
    permission_digest: str | None,
    previous_digest: str | None,
    occurred_at: datetime,
) -> dict[str, Any]:
    manifest = {
        "action_run_id": action_run_id,
        "sequence": sequence,
        "event_type": event_type,
        "from_status": from_status,
        "to_status": to_status,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "request_id": request_id,
        "trace_id": trace_id,
        "input_digest": input_digest,
        "preview_digest": preview_digest,
        "permission_digest": permission_digest,
        "previous_digest": previous_digest,
        "occurred_at": _event_time_value(occurred_at),
    }


def _append_action_run_event(
    db,
    run: OntologyActionRun,
    *,
    event_type: str,
    from_status: str | None,
    to_status: str,
    actor_id: str | None,
    actor_kind: str,
) -> OntologyActionRunEvent:
    if actor_kind not in {"user", "agent", "worker", "system"}:
        raise ScenarioError("invalid_action_actor", "Action actor kind is not supported", 422)
    if not run.id:
        raise ScenarioError("action_run_id_missing", "ActionRun requires a stable ID before audit", 500)

    # Serialize sequence assignment on the parent ActionRun row. The unique
    # (run, sequence) constraint remains the final guard for all dialects.
    db.add(run)
    db.flush()
    db.query(OntologyActionRun).filter_by(id=run.id).with_for_update().first()
    prior = db.query(OntologyActionRunEvent).filter_by(
        action_run_id=run.id,
    ).order_by(OntologyActionRunEvent.sequence.desc()).first()
    sequence = (prior.sequence + 1) if prior else 1
    previous_digest = prior.event_digest if prior else None
    occurred_at = datetime.now(timezone.utc)
    actor_id = str(actor_id) if actor_id is not None else None
    manifest = _action_run_event_manifest(
        action_run_id=run.id,
        sequence=sequence,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor_kind=actor_kind,
        actor_id=actor_id,
        request_id=run.client_request_id,
        trace_id=run.trace_id,
        input_digest=run.input_digest,
        preview_digest=run.preview_digest,
        permission_digest=run.permission_digest,
        previous_digest=previous_digest,
        occurred_at=occurred_at,
    )
    row = OntologyActionRunEvent(
        action_run_id=run.id,
        sequence=sequence,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor_kind=actor_kind,
        actor_id=actor_id,
        request_id=run.client_request_id,
        trace_id=run.trace_id,
        input_digest=run.input_digest,
        preview_digest=run.preview_digest,
        permission_digest=run.permission_digest,
        previous_digest=previous_digest,
        event_digest=stable_hash(manifest),
        occurred_at=occurred_at,
    )
    db.add(row)
    db.flush()
    return row


def action_run_event_chain_valid(events: Iterable[OntologyActionRunEvent]) -> bool:
    saw_event = False
    previous_digest = None
    expected_sequence = 1
    for row in events:
        saw_event = True
        if row.sequence != expected_sequence or row.previous_digest != previous_digest:
            return False
        manifest = _action_run_event_manifest(
            action_run_id=row.action_run_id,
            sequence=row.sequence,
            event_type=row.event_type,
            from_status=row.from_status,
            to_status=row.to_status,
            actor_kind=row.actor_kind,
            actor_id=row.actor_id,
            request_id=row.request_id,
            trace_id=row.trace_id,
            input_digest=row.input_digest,
            preview_digest=row.preview_digest,
            permission_digest=row.permission_digest,
            previous_digest=row.previous_digest,
            occurred_at=row.occurred_at,
        )
        if row.event_digest != stable_hash(manifest):
            return False
        previous_digest = row.event_digest
        expected_sequence += 1
    return saw_event


def _normalized_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _action_run_claim_manifest(
    *,
    action_run_id: str,
    sequence: int,
    attempt_number: int,
    event_type: str,
    actor_kind: str,
    actor_id: str | None,
    request_id: str | None,
    input_digest: str | None,
    claim_token_digest: str,
    resource_digest: str | None,
    lease_expires_at: datetime | None,
    previous_digest: str | None,
    occurred_at: datetime,
) -> dict[str, Any]:
    manifest = {
        "action_run_id": action_run_id,
        "sequence": sequence,
        "attempt_number": attempt_number,
        "event_type": event_type,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "request_id": request_id,
        "input_digest": input_digest,
        "claim_token_digest": claim_token_digest,
        "lease_expires_at": (
            _event_time_value(lease_expires_at) if lease_expires_at else None
        ),
        "previous_digest": previous_digest,
        "occurred_at": _event_time_value(occurred_at),
    }
    # Events written before the resource digest column was added omitted this
    # key from their digest manifest. Preserve their verifiability after the
    # nullable column is migrated; new graph events bind it when present.
    if resource_digest is not None:
        manifest["resource_digest"] = resource_digest
    return manifest


def record_action_run_claim_event(
    db,
    run: OntologyActionRun,
    event_type: str,
    *,
    actor_id: str | None,
    actor_kind: str = "user",
    claim_token: str | None = None,
    attempt_number: int | None = None,
    lease_expires_at: datetime | None = None,
    resource_digest: str | None = None,
) -> OntologyActionRunClaimEvent:
    """Append one immutable claim/lease fact to the run's separate digest chain."""
    allowed = {
        "claim_acquired", "lease_renewed", "attempt_expired",
        "attempt_completed", "attempt_failed", "attempt_reconciliation_required",
        "graph_write_started", "graph_orphan_deleted", "graph_cleanup_failed",
        "graph_reference_preserved",
    }
    if event_type not in allowed or actor_kind not in {"user", "agent", "worker", "system"}:
        raise ScenarioError("invalid_action_run_claim_event", "ActionRun execution claim event is invalid", 422)
    if not run.id:
        raise ScenarioError("action_run_id_missing", "ActionRun requires a stable ID before audit", 500)
    resolved_token = claim_token or run.execution_lease_token
    resolved_attempt = attempt_number if attempt_number is not None else run.execution_attempt
    if resolved_attempt < 1:
        raise ScenarioError("action_run_claim_missing", "Execution claim evidence is incomplete", 409)
    if resource_digest is not None and (
        len(resource_digest) != 64
        or any(character not in "0123456789abcdef" for character in resource_digest)
    ):
        raise ScenarioError("invalid_action_run_resource_digest", "ActionRun resource digest is invalid", 422)

    # The parent row serializes sequence assignment and claim replacement.
    db.add(run)
    db.flush()
    db.query(OntologyActionRun).filter_by(id=run.id).with_for_update().first()
    prior = db.query(OntologyActionRunClaimEvent).filter_by(
        action_run_id=run.id,
    ).order_by(OntologyActionRunClaimEvent.sequence.desc()).first()
    if not resolved_token and (
        prior is None or prior.attempt_number != resolved_attempt
    ):
        raise ScenarioError("action_run_claim_missing", "Execution claim evidence is incomplete", 409)
    sequence = (prior.sequence + 1) if prior else 1
    previous_digest = prior.event_digest if prior else None
    occurred_at = datetime.now(timezone.utc)
    actor_id = str(actor_id) if actor_id is not None else None
    # Recovery events run after a failed attempt has cleared its raw lease
    # token. Continue the chain with the latest persisted token digest instead
    # of reintroducing the secret token into durable state.
    token_digest = (
        stable_hash({"execution_lease_token": resolved_token})
        if resolved_token else prior.claim_token_digest
    )
    expires_at = lease_expires_at if lease_expires_at is not None else run.execution_lease_until
    manifest = _action_run_claim_manifest(
        action_run_id=run.id,
        sequence=sequence,
        attempt_number=resolved_attempt,
        event_type=event_type,
        actor_kind=actor_kind,
        actor_id=actor_id,
        request_id=run.client_request_id,
        input_digest=run.input_digest,
        claim_token_digest=token_digest,
        resource_digest=resource_digest,
        lease_expires_at=expires_at,
        previous_digest=previous_digest,
        occurred_at=occurred_at,
    )
    row = OntologyActionRunClaimEvent(
        action_run_id=run.id,
        sequence=sequence,
        attempt_number=resolved_attempt,
        event_type=event_type,
        actor_kind=actor_kind,
        actor_id=actor_id,
        request_id=run.client_request_id,
        input_digest=run.input_digest,
        claim_token_digest=token_digest,
        resource_digest=resource_digest,
        lease_expires_at=expires_at,
        previous_digest=previous_digest,
        event_digest=stable_hash(manifest),
        occurred_at=occurred_at,
    )
    db.add(row)
    db.flush()
    return row


def action_run_claim_chain_valid(events: Iterable[OntologyActionRunClaimEvent]) -> bool:
    saw_event = False
    previous_digest = None
    expected_sequence = 1
    for row in events:
        saw_event = True
        if row.sequence != expected_sequence or row.previous_digest != previous_digest:
            return False
        manifest = _action_run_claim_manifest(
            action_run_id=row.action_run_id,
            sequence=row.sequence,
            attempt_number=row.attempt_number,
            event_type=row.event_type,
            actor_kind=row.actor_kind,
            actor_id=row.actor_id,
            request_id=row.request_id,
            input_digest=row.input_digest,
            claim_token_digest=row.claim_token_digest,
            resource_digest=row.resource_digest,
            lease_expires_at=row.lease_expires_at,
            previous_digest=row.previous_digest,
            occurred_at=row.occurred_at,
        )
        if row.event_digest != stable_hash(manifest):
            return False
        previous_digest = row.event_digest
        expected_sequence += 1
    return saw_event


def scenario_build_resource_digest(graph_key: str, revision: int) -> str:
    """Hash an isolated Scenario graph identity for immutable claim evidence."""
    return stable_hash({"graph_key": graph_key, "revision": revision})


def transition_action_run(
    db,
    run: OntologyActionRun,
    status: str,
    *,
    actor_id: str | None = None,
    actor_kind: str | None = None,
) -> OntologyActionRun:
    """Apply the shared ActionRun lifecycle transition contract.

    Projection state remains separate from execution state: a completed Action
    may still have a pending or failed graph projection. Reverts are represented
    by a new completed run and this transition only closes the original run.
    """
    current = run.status or "queued"
    if status not in _ACTION_RUN_TRANSITIONS.get(current, set()):
        raise ScenarioError(
            "invalid_action_run_transition",
            f"ActionRun cannot transition from {current!r} to {status!r}",
            409,
        )
    run.status = status
    resolved_actor = actor_id if actor_id is not None else run.executed_by
    _append_action_run_event(
        db,
        run,
        event_type="status_changed",
        from_status=current,
        to_status=status,
        actor_id=resolved_actor,
        actor_kind=actor_kind or ("user" if resolved_actor else "system"),
    )
    return run


def complete_action_run(
    db,
    run: OntologyActionRun,
    *,
    before_snapshot: dict | None,
    after_snapshot: dict | None,
    side_effect_results: list | dict | None,
    actor_id: str | None = None,
    actor_kind: str | None = None,
    completed_at: datetime | None = None,
) -> OntologyActionRun:
    """Record a successful result through the shared ActionRun state path."""
    transition_action_run(db, run, "completed", actor_id=actor_id, actor_kind=actor_kind)
    run.before_snapshot = before_snapshot
    run.after_snapshot = after_snapshot
    run.side_effect_results = side_effect_results
    run.error = None
    run.completed_at = completed_at or datetime.now(timezone.utc)
    return run


def fail_action_run(
    db,
    run: OntologyActionRun,
    error: str,
    *,
    actor_id: str | None = None,
    actor_kind: str | None = None,
    completed_at: datetime | None = None,
) -> OntologyActionRun:
    """Record a failed Action without overwriting a completed or reverted run."""
    transition_action_run(db, run, "failed", actor_id=actor_id, actor_kind=actor_kind)
    run.error = error
    run.completed_at = completed_at or datetime.now(timezone.utc)
    return run


def find_idempotent_action_run(
    db,
    *,
    ontology_id: str,
    actor_id: str,
    scope: str,
    request_id: str,
    request_digest: str,
) -> OntologyActionRun | None:
    """Find a durable request claim, rejecting payload changes and uncertain replay."""
    run = db.query(OntologyActionRun).filter_by(
        ontology_id=ontology_id,
        executed_by=actor_id,
        idempotency_scope=scope,
        client_request_id=request_id,
    ).populate_existing().with_for_update().first()
    if run is None:
        return None
    if run.input_digest != request_digest:
        raise ScenarioError(
            "idempotency_conflict", "Request ID was used for a different Action request", 409)
    if run.status == "completed":
        return run
    if run.status in {"failed", "cancelled", "reconciliation_required"}:
        raise ScenarioError(
            "action_request_incomplete",
            "该 Action 请求已有终态记录；请使用新的请求编号，或先完成审计核对。", 409)
    raise ScenarioError(
        "action_request_pending",
        "该 Action 请求已有持久运行记录但尚未确认完成；请先核对运行记录，避免重复提交。", 409)


def claim_idempotent_scenario_action_run(
    db,
    *,
    ontology_id: str,
    actor_id: str | None,
    scope: str,
    request_id: str,
    request_digest: str,
    scenario_id: str,
    lease_seconds: int = SCENARIO_ACTION_LEASE_SECONDS,
) -> tuple[OntologyActionRun | None, str | None, bool]:
    """Replay a completed Scenario run or reclaim its expired fenced attempt.

    A legacy running row without a lease remains held for manual reconciliation.
    A committed ChangeSet beside a running ActionRun also fails closed because
    those records should have committed together.
    """
    run = db.query(OntologyActionRun).filter_by(
        ontology_id=ontology_id,
        executed_by=actor_id,
        idempotency_scope=scope,
        client_request_id=request_id,
    ).populate_existing().with_for_update().first()
    if run is None:
        return None, None, False
    if run.input_digest != request_digest:
        raise ScenarioError(
            "idempotency_conflict", "Request ID was used for a different Action request", 409)
    if run.status == "completed":
        return run, None, True
    if run.status != "running":
        raise ScenarioError(
            "action_request_incomplete",
            "该 Action 请求已有终态记录；需核对执行记录后再使用新请求编号。", 409)
    if not run.execution_lease_token or not run.execution_lease_until or run.execution_attempt < 1:
        raise ScenarioError(
            "action_request_legacy_pending",
            "该运行记录没有可验证的执行租约，需先人工核对，系统不会自动重放。", 409)

    claim_events = db.query(OntologyActionRunClaimEvent).filter_by(
        action_run_id=run.id,
    ).order_by(OntologyActionRunClaimEvent.sequence.asc())
    if not action_run_claim_chain_valid(claim_events.yield_per(500)):
        raise ScenarioError(
            "action_claim_history_invalid",
            "该运行记录的执行租约审计链缺失或无效，需先人工核对。", 409)

    now_utc = datetime.now(timezone.utc)
    lease_until = _normalized_utc(run.execution_lease_until)
    if lease_until and lease_until > now_utc:
        raise ScenarioError(
            "action_request_pending",
            "该 Scenario Action 仍由有效执行租约保护；请稍后使用同一请求编号重试。", 409)

    committed_change = db.query(ScenarioChangeSet).filter_by(
        scenario_id=scenario_id,
        client_request_id=request_id,
    ).first()
    if committed_change is not None:
        transition_action_run(
            db, run, "reconciliation_required", actor_id=actor_id, actor_kind="user",
        )
        run.error = "Scenario ChangeSet exists while its ActionRun remains running"
        run.completed_at = now_utc
        record_action_run_claim_event(
            db, run, "attempt_reconciliation_required", actor_id=actor_id,
            actor_kind="user",
        )
        run.execution_lease_token = None
        run.execution_lease_until = None
        db.commit()
        raise ScenarioError(
            "action_run_reconciliation_required",
            "已发现 Scenario 修订记录但 ActionRun 未完成；该请求已转入人工核对状态。", 409)

    old_attempt = run.execution_attempt
    old_token = run.execution_lease_token
    record_action_run_claim_event(
        db, run, "attempt_expired", actor_id=actor_id, actor_kind="user",
        claim_token=old_token, attempt_number=old_attempt,
    )
    new_token = str(uuid.uuid4())
    run.execution_attempt = old_attempt + 1
    run.execution_lease_token = new_token
    run.execution_lease_until = now_utc + timedelta(seconds=max(30, min(lease_seconds, 600)))
    record_action_run_claim_event(
        db, run, "claim_acquired", actor_id=actor_id, actor_kind="user",
    )
    db.commit()
    db.refresh(run)
    return run, new_token, False


def acquire_initial_scenario_action_run_claim(
    db,
    run: OntologyActionRun,
    *,
    actor_id: str,
    actor_kind: str = "user",
    lease_seconds: int = SCENARIO_ACTION_LEASE_SECONDS,
) -> str:
    """Persist a new Scenario ActionRun with its first fenced execution claim."""
    scenario_id = (run.execution_context or {}).get("scenario_id")
    if (
        run.status != "running"
        or run.execution_attempt != 0
        or run.execution_lease_token is not None
        or run.execution_lease_until is not None
        or not scenario_id
        or not run.client_request_id
        or not run.input_digest
        or not run.idempotency_scope
    ):
        raise ScenarioError(
            "action_claim_input_invalid",
            "A new Scenario ActionRun requires a running record and pinned request context",
            409,
        )

    token = str(uuid.uuid4())
    now_utc = datetime.now(timezone.utc)
    run.execution_attempt = 1
    run.execution_lease_token = token
    run.execution_lease_until = now_utc + timedelta(
        seconds=max(30, min(lease_seconds, 600)),
    )
    record_action_run_claim_event(
        db, run, "claim_acquired", actor_id=actor_id, actor_kind=actor_kind,
    )
    db.commit()
    db.refresh(run)
    return token


def create_scenario_revision_action_run_claim(
    db,
    *,
    ontology_id: str,
    scenario_id: str,
    actor_id: str | None,
    actor_kind: str,
    request_id: str,
    request_digest: str,
    preview_digest: str,
    permission_digest: str,
    expected_etag: int | str | None,
    source: str,
    preview_mode: str,
    confirmation_mode: str,
    edit_count: int,
    operation_context: dict | None = None,
) -> tuple[OntologyActionRun, str]:
    """Persist a leased ActionRun for a non-registered Scenario revision."""
    if actor_kind not in {"user", "system"}:
        raise ScenarioError(
            "scenario_revision_actor_invalid",
            "Scenario revision ActionRun actor is invalid",
            422,
        )
    run = create_action_run_record(
        db=db,
        action_type_id=None,
        ontology_id=ontology_id,
        parameters={
            "request_digest": request_digest,
            "edit_count": edit_count,
        },
        execution_context={
            "scenario_id": scenario_id,
            "source": source,
            "operation_context": dict(operation_context or {}),
            "preview_mode": preview_mode,
            "confirmation_mode": confirmation_mode,
            "approval_mode": "not_required",
        },
        initial_status="running",
        requester_id=actor_id if actor_kind == "user" else None,
        executor_id=actor_id if actor_kind == "user" else None,
        client_request_id=request_id,
        idempotency_scope=f"scenario:{scenario_id}",
        input_digest=request_digest,
        preview_digest=preview_digest,
        permission_digest=permission_digest,
        expected_etag=str(expected_etag) if expected_etag is not None else None,
        event_actor_id=actor_id,
        event_actor_kind=actor_kind,
    )
    db.add(run)
    db.flush()
    token = acquire_initial_scenario_action_run_claim(
        db,
        run,
        actor_id=actor_id,
        actor_kind=actor_kind,
    )
    return run, token


def scenario_action_run_claim_manifest(
    run: OntologyActionRun,
    claim_token: str,
    request_digest: str,
) -> dict[str, Any]:
    """Build the non-secret manifest used to fence Scenario revision writes."""
    if (
        run.status != "running"
        or not run.execution_lease_token
        or run.execution_lease_token != claim_token
        or run.execution_attempt < 1
        or not run.client_request_id
        or run.input_digest != request_digest
    ):
        raise ScenarioError(
            "action_claim_missing",
            "Scenario ActionRun has no current claim for this request",
            409,
        )
    return {
        "action_run_id": run.id,
        "client_request_id": run.client_request_id,
        "request_digest": request_digest,
        "attempt_number": run.execution_attempt,
        "claim_token_digest": stable_hash({"execution_lease_token": claim_token}),
    }


def renew_scenario_action_run_claim(
    db,
    *,
    run_id: str,
    claim_token: str,
    actor_id: str | None,
    actor_kind: str = "user",
    lease_seconds: int = SCENARIO_ACTION_LEASE_SECONDS,
) -> OntologyActionRun:
    """Renew a claim only while its fencing token is still the current owner."""
    run = db.query(OntologyActionRun).filter_by(id=run_id).populate_existing().with_for_update().first()
    lease_until = _normalized_utc(run.execution_lease_until) if run is not None else None
    if (
        run is None
        or run.status != "running"
        or run.execution_lease_token != claim_token
        or lease_until is None
        or lease_until <= datetime.now(timezone.utc)
    ):
        raise ScenarioError(
            "action_attempt_superseded",
            "Scenario Action 执行租约已过期或被其他尝试接管；此尝试不得续期或提交。", 409)
    run.execution_lease_until = datetime.now(timezone.utc) + timedelta(
        seconds=max(30, min(lease_seconds, 600)))
    record_action_run_claim_event(
        db, run, "lease_renewed", actor_id=actor_id, actor_kind=actor_kind,
    )
    return run


def require_scenario_action_run_claim(db, runtime_manifest: dict, scenario_id: str) -> OntologyActionRun:
    """Fence graph publication with the current persisted Scenario claim."""
    run_id = runtime_manifest.get("action_run_id")
    run = db.query(OntologyActionRun).filter_by(id=run_id).populate_existing().with_for_update().first()
    claim_token_digest = runtime_manifest.get("claim_token_digest")
    lease_until = _normalized_utc(run.execution_lease_until) if run is not None else None
    if (
        run is None
        or run.status != "running"
        or not run.execution_lease_token
        or stable_hash({"execution_lease_token": run.execution_lease_token}) != claim_token_digest
        or run.execution_attempt != runtime_manifest.get("attempt_number")
        or run.client_request_id != runtime_manifest.get("client_request_id")
        or run.input_digest != runtime_manifest.get("request_digest")
        or (run.execution_context or {}).get("scenario_id") != scenario_id
        or lease_until is None
        or lease_until <= datetime.now(timezone.utc)
    ):
        raise ScenarioError(
            "action_attempt_superseded",
            "Scenario Action 执行租约已过期或已被接管；此尝试不得提交。", 409)
    return run


def fail_scenario_action_run_claim(
    db,
    *,
    run_id: str,
    claim_token: str,
    error: str,
    actor_id: str | None,
    actor_kind: str = "user",
) -> str:
    """Close a failed attempt without allowing an obsolete owner to fail its successor."""
    run = db.query(OntologyActionRun).filter_by(id=run_id).populate_existing().with_for_update().first()
    if run is None:
        return "missing"
    if run.status == "completed":
        return "completed"
    if run.status != "running" or run.execution_lease_token != claim_token:
        return "superseded"
    fail_action_run(db, run, error, actor_id=actor_id, actor_kind=actor_kind)
    record_action_run_claim_event(
        db, run, "attempt_failed", actor_id=actor_id, actor_kind=actor_kind,
    )
    run.execution_lease_token = None
    run.execution_lease_until = None
    db.commit()
    return "failed"


def complete_scenario_action_run(
    db,
    run: OntologyActionRun,
    *,
    revision,
    parent_view_id: str,
    parent_digest: str | None,
    changeset: ScenarioChangeSet,
    permission_digest: str,
    preview_digest: str,
    actor_id: str | None = None,
    actor_kind: str | None = None,
) -> None:
    """Finalize a Scenario ActionRun inside the Scenario revision SQL transaction."""
    complete_action_run(
        db,
        run,
        before_snapshot={
            "scenario_revision": revision.parent_revision,
            "view_id": parent_view_id,
        },
        after_snapshot={
            "scenario_revision": revision.revision,
            "result_view_id": revision.result_view_id,
            "changeset_id": changeset.id,
        },
        side_effect_results=[
            {"op": edit.get("op"), "sequence": edit.get("sequence")}
            for edit in (changeset.ordered_edits or [])
            if edit.get("op") != "invoke_action"
        ],
        actor_id=actor_id,
        actor_kind=actor_kind,
    )
    if run.execution_attempt > 0 and run.execution_lease_token:
        resource_digest = (
            scenario_build_resource_digest(
                run.scenario_build_graph_key,
                run.scenario_build_revision,
            )
            if run.scenario_build_graph_key and run.scenario_build_revision is not None
            else None
        )
        record_action_run_claim_event(
            db, run, "attempt_completed", actor_id=actor_id, actor_kind=actor_kind or "user",
            resource_digest=resource_digest,
        )
        run.execution_lease_token = None
        run.execution_lease_until = None
        run.scenario_build_graph_key = None
        run.scenario_build_revision = None
    run.permission_digest = permission_digest
    run.preview_digest = preview_digest
    run.before_digest = (
        parent_digest if parent_digest and len(parent_digest) == 64
        else stable_hash({"parent_digest": parent_digest}) if parent_digest
        else None
    )
    result_digest = revision.content_digest
    run.after_digest = (
        result_digest if result_digest and len(result_digest) == 64
        else stable_hash({"result_digest": result_digest}) if result_digest
        else None
    )


def scenario_action_run_response(db, run: OntologyActionRun, *, replayed: bool) -> dict:
    """Return the stored Scenario revision result for a completed Action request."""
    snapshot = run.after_snapshot or {}
    revision = snapshot.get("scenario_revision")
    if revision is None:
        raise ScenarioError(
            "action_request_incomplete",
            "ActionRun 已完成但关联的 Scenario 修订记录不可读取。", 409)
    edits = []
    if replayed:
        scenario_id = (run.execution_context or {}).get("scenario_id")
        revision_row = db.query(ScenarioRevision).filter_by(
            scenario_id=scenario_id, revision=revision, status="ready",
        ).first() if scenario_id else None
        if revision_row is None:
            raise ScenarioError(
                "action_request_incomplete",
                "ActionRun 已完成但关联的 Scenario 修订记录不可读取。", 409)
    else:
        changeset = db.get(ScenarioChangeSet, snapshot.get("changeset_id"))
        if changeset is None:
            raise ScenarioError(
                "action_request_incomplete",
                "ActionRun 已完成但关联的 Scenario 修订记录不可读取。", 409)
        edits = changeset.ordered_edits or []
    return {
        "run_id": run.id,
        "status": "completed",
        "target": "scenario",
        "revision": revision,
        "execution_context": run.execution_context,
        "permission_digest": run.permission_digest,
        "preview_digest": run.preview_digest,
        "edits": edits,
        "side_effect_results": run.side_effect_results or [],
        "replayed": replayed,
    }


def reconcile_expired_scenario_action_graph_builds(
    db,
    *,
    batch_size: int = 25,
    graph=None,
    at: datetime | None = None,
) -> dict[str, int | str]:
    """Remove only expired, unreferenced graphs left by leased Scenario builds.

    The durable marker is written before FalkorDB work. Recovery takes locks in
    the same Scenario-then-ActionRun order as publication, then checks both the
    claim digest chain and SQL references before touching the graph store.
    Ambiguous terminal states and orphaned ChangeSets are left for review.
    """
    from app.models.v2.query_view import QueryDataView
    from app.models.v2.scenario import ScenarioChangeSet, ScenarioResource
    from app.services.v2.graph.falkordb_service import FalkorDBService

    batch_size = min(max(int(batch_size), 1), 100)
    run_at = _normalized_utc(at or datetime.now(timezone.utc))
    graph = graph or FalkorDBService()
    if not graph.available:
        return {"state": "unavailable", "scanned": 0, "deleted": 0,
                "preserved": 0, "not_due": 0, "manual": 0, "failed": 0}

    candidates = db.query(
        OntologyActionRun.id,
        OntologyActionRun.ontology_id,
        OntologyActionRun.execution_context,
    ).filter(
        OntologyActionRun.scenario_build_graph_key.is_not(None),
    ).order_by(
        OntologyActionRun.started_at.asc(), OntologyActionRun.id.asc(),
    ).limit(batch_size).all()

    counts = {"scanned": 0, "deleted": 0, "preserved": 0,
              "not_due": 0, "manual": 0, "failed": 0}
    for run_id, _ontology_id, execution_context in candidates:
        try:
            # Match Scenario publication's lock order so a recovery delete
            # cannot race a publisher inserting the corresponding view row.
            scenario_id = (execution_context or {}).get("scenario_id")
            if scenario_id:
                scenario_exists = db.query(ScenarioResource.id).filter_by(
                    id=scenario_id,
                ).first()
                if scenario_exists and db.query(ScenarioResource).filter_by(
                    id=scenario_id,
                ).populate_existing().with_for_update(skip_locked=True).first() is None:
                    db.rollback()
                    counts["not_due"] += 1
                    continue

            run = db.query(OntologyActionRun).filter_by(
                id=run_id,
            ).populate_existing().with_for_update(skip_locked=True).first()
            if run is None or not run.scenario_build_graph_key:
                db.rollback()
                continue
            counts["scanned"] += 1

            graph_key = run.scenario_build_graph_key
            graph_revision = run.scenario_build_revision
            if graph_revision is None or run.execution_attempt < 1:
                counts["manual"] += 1
                db.rollback()
                continue
            resource_digest = scenario_build_resource_digest(graph_key, graph_revision)

            events = db.query(OntologyActionRunClaimEvent).filter_by(
                action_run_id=run.id,
            ).order_by(OntologyActionRunClaimEvent.sequence.asc()).all()
            if (
                not action_run_claim_chain_valid(events)
                or not any(
                    row.event_type == "graph_write_started"
                    and row.resource_digest == resource_digest
                    for row in events
                )
            ):
                counts["manual"] += 1
                db.rollback()
                continue

            lease_until = _normalized_utc(run.execution_lease_until)
            if lease_until is not None and lease_until > run_at:
                counts["not_due"] += 1
                db.rollback()
                continue
            if run.status == "running" and lease_until is None:
                # An unleased running row cannot prove that no publisher is
                # still writing, so leave its graph untouched.
                counts["manual"] += 1
                db.rollback()
                continue

            # Any QueryDataView row reserves the graph key, regardless of its
            # readiness or retention status. Its owner manages later cleanup.
            view = db.query(QueryDataView.id).filter_by(graph_key=graph_key).first()
            if view is not None:
                record_action_run_claim_event(
                    db, run, "graph_reference_preserved", actor_id=None,
                    actor_kind="system", resource_digest=resource_digest,
                )
                run.scenario_build_graph_key = None
                run.scenario_build_revision = None
                db.commit()
                counts["preserved"] += 1
                continue

            if run.status in {"completed", "reconciliation_required"}:
                counts["manual"] += 1
                db.rollback()
                continue
            if run.status not in {"running", "failed", "cancelled"}:
                counts["manual"] += 1
                db.rollback()
                continue

            request_id = run.client_request_id
            if scenario_id and request_id:
                changeset = db.query(ScenarioChangeSet.id).filter_by(
                    scenario_id=scenario_id,
                    client_request_id=request_id,
                ).first()
                if changeset is not None:
                    counts["manual"] += 1
                    db.rollback()
                    continue

            try:
                deleted = graph.delete_graph(run.ontology_id, namespace=graph_key)
            except Exception:
                deleted = False
            if not deleted:
                record_action_run_claim_event(
                    db, run, "graph_cleanup_failed", actor_id=None,
                    actor_kind="system", resource_digest=resource_digest,
                )
                db.commit()
                counts["failed"] += 1
                continue

            record_action_run_claim_event(
                db, run, "graph_orphan_deleted", actor_id=None,
                actor_kind="system", resource_digest=resource_digest,
            )
            run.scenario_build_graph_key = None
            run.scenario_build_revision = None
            db.commit()
            counts["deleted"] += 1
        except Exception:
            db.rollback()
            counts["failed"] += 1

    return {"state": "completed", **counts}


def compile_with_authorization(
    db,
    ontology_id: str,
    actor,
    snapshot,
    compiler: Callable[[Any], Any],
    *,
    edits_from: Callable[[Any], Iterable] | None = None,
):
    """Compile against visible data and recheck the produced edits.

    The permission digest is calculated before and after compilation. A policy
    update during compilation invalidates the result instead of allowing a
    stale preview to proceed.
    """
    projected, input_permission_digest = authorize_action_snapshot(
        db, ontology_id, actor, snapshot)
    result = compiler(projected)
    edits = edits_from(result) if edits_from else result
    permission_digest = authorize_compiled_edits(
        db, ontology_id, actor, snapshot, edits)
    if permission_digest != input_permission_digest:
        raise ScenarioError(
            "permission_changed", "执行期间权限发生变化；请重新预览", 409)
    return result, permission_digest


def reauthorize_edit_batch(
    db,
    *,
    ontology_id: str,
    actor,
    snapshot,
    edits,
    expected_permission_digest: str | None,
    allow_create_collisions: bool = False,
) -> str:
    """Recheck a compiled batch at its live execution or recovery boundary.

    Preview-time authorization is not sufficient for a durable write. Callers
    must carry the digest they reviewed, and the executor compares it with the
    current policy projection over the execution snapshot before applying any
    side effects.
    """
    if not isinstance(expected_permission_digest, str) or not expected_permission_digest:
        raise ScenarioError(
            "permission_manifest_missing",
            "Action execution requires the reviewed permission digest",
            409,
        )
    current_permission_digest = authorize_compiled_edits(
        db,
        ontology_id,
        actor,
        snapshot,
        edits,
        allow_create_collisions=allow_create_collisions,
    )
    if current_permission_digest != expected_permission_digest:
        raise ScenarioError(
            "permission_changed",
            "当前权限与 Action 预览不一致；请重新预览后再执行",
            409,
        )
    return current_permission_digest


def compile_action_definitions(
    db,
    request: ActionRequest,
    actor,
    action_types: dict,
    snapshot,
    *,
    id_factory=None,
    function_resolver=None,
):
    """Compile standard Action definitions through the shared boundary."""
    from app.services.v2.scenario_actions import compile_actions

    if function_resolver is None:
        from app.services.v2.function_binding import function_resolver as bind_function
        function_resolver = bind_function(db, request.ontology_id)

    return compile_with_authorization(
        db, request.ontology_id, actor, snapshot,
        lambda projected: compile_actions(
            request.ontology_id, request.compiler_scope, request.compiler_payloads(),
            action_types, projected,
            actor=actor, id_factory=id_factory, function_resolver=function_resolver),
    )
