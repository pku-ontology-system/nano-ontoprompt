"""Immutable, version-pinned input contract for scenario-based reasoning."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScenarioContext(BaseModel):
    """Canonical pins shared by scenario consumers.

    Keep this contract scalar-only so ``frozen=True`` also prevents nested
    mutable values from changing after validation.  It deliberately carries
    digests and version identifiers rather than permission rules or evidence
    bodies.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["1.0", "1.1"] = "1.1"
    ontology_id: str = Field(min_length=1, max_length=200)
    schema_revision_id: str = Field(min_length=1, max_length=200)
    schema_revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_model_snapshot_id: str = Field(min_length=1, max_length=200)
    data_model_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_version_id: str = Field(min_length=1, max_length=200)
    dataset_version_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    replay_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=120)
    episode_id: str = Field(min_length=1, max_length=200)
    time_kind: Literal["ordinal", "event_time"]
    time_value: str = Field(min_length=1, max_length=120)
    time_zone: str | None = Field(default=None, max_length=120)
    watermark_ordinal: str = Field(min_length=1, max_length=80)
    watermark_sequence: int = Field(ge=0)
    scenario_branch_id: str | None = Field(default=None, max_length=200)
    checkpoint_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_id: str = Field(min_length=1, max_length=200)
    permission_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    vector_index_revision: str | None = Field(default=None, max_length=200)
    source_adapter_id: str | None = Field(default=None, min_length=1, max_length=160)
    source_adapter_version: str | None = Field(default=None, min_length=1, max_length=40)
    source_mapping_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_event_timezone(self) -> "ScenarioContext":
        if self.time_kind == "event_time" and not self.time_zone:
            raise ValueError("event_time ScenarioContext requires a timezone")
        adapter_pins = (self.source_adapter_id, self.source_adapter_version, self.source_mapping_digest)
        if self.contract_version == "1.1" and not all(adapter_pins):
            raise ValueError("ScenarioContext 1.1 requires versioned temporal source adapter pins")
        if self.contract_version == "1.0" and any(adapter_pins):
            raise ValueError("ScenarioContext 1.0 cannot contain version 1.1 source adapter pins")
        return self


class WhatIfContextResponse(BaseModel):
    """Expose the typed context while retaining existing baseline fields."""

    model_config = ConfigDict(extra="allow", frozen=True)

    scenario_context: ScenarioContext


def _canonical_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _context_digest_payload(context: ScenarioContext) -> dict[str, object]:
    payload = context.model_dump(mode="json", exclude={"context_digest"})
    if context.contract_version == "1.0":
        # Preserve hashes for previously sealed contracts after adding the 1.1
        # adapter pins; nullable fields from the 1.0 contract remain included.
        payload.pop("source_adapter_id", None)
        payload.pop("source_adapter_version", None)
        payload.pop("source_mapping_digest", None)
    return payload


def scenario_inputs_digest(assumptions: list, rule_overrides: dict) -> str:
    """Digest mutable What-if inputs separately from the immutable baseline contract."""
    return _canonical_digest({
        "assumptions": assumptions,
        "rule_overrides": rule_overrides,
    })


def seal_scenario_context(values: dict[str, object]) -> ScenarioContext:
    """Validate and seal a context with a digest over all of its pins."""
    draft = ScenarioContext.model_validate({**values, "context_digest": "0" * 64})
    payload = _context_digest_payload(draft)
    return draft.model_copy(update={"context_digest": _canonical_digest(payload)})


def verify_scenario_context(value: object) -> bool:
    """Return false for missing, malformed, or modified scenario contexts."""
    if not isinstance(value, dict):
        return False
    try:
        context = ScenarioContext.model_validate(value)
    except Exception:
        return False
    payload = _context_digest_payload(context)
    expected = _canonical_digest(payload)
    return hmac.compare_digest(context.context_digest, expected)

