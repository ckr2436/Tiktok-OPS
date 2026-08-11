from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.hermes_agent.content_change_contract import (
    SourceTransformationContract,
)


INTENT_SCHEMA_VERSION = "2.0"


class CreativeIntentRequirement(BaseModel):
    """One user-owned requirement with evidence and observable meaning.

    Requirements are deliberately semantic.  They tell an AI role what must be
    achieved and how the result can be observed without prescribing a fixed
    story template or reducing an instruction such as "use the reference's
    hook strength" to a boolean.
    """

    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(pattern=r"^R-[0-9]{3}$")
    kind: Literal[
        "objective",
        "preservation",
        "differentiation",
        "reference_transfer",
        "functional_artifact",
        "conversion",
        "visual",
        "audio",
        "acceptance",
    ]
    priority: Literal["critical", "high", "normal"] = "normal"
    scope: Literal[
        "project",
        "deliverable",
        "time_window",
        "final_output",
    ] = "project"
    deliverable_ordinals: list[int] = Field(default_factory=list, max_length=50)
    start_seconds: float | None = Field(default=None, ge=0, le=3600)
    end_seconds: float | None = Field(default=None, gt=0, le=3600)
    intent: str = Field(min_length=1, max_length=2000)
    evidence_quote: str = Field(min_length=1, max_length=2000)
    source_message_id: int | None = Field(default=None, ge=1)
    interpretation: str = Field(min_length=1, max_length=3000)
    observable_checks: list[str] = Field(min_length=1, max_length=16)
    creative_freedom: list[str] = Field(default_factory=list, max_length=16)
    must_not_reuse: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _coordinates(self) -> "CreativeIntentRequirement":
        if len(self.deliverable_ordinals) != len(set(self.deliverable_ordinals)):
            raise ValueError("deliverable_ordinals must be unique")
        if any(value < 1 for value in self.deliverable_ordinals):
            raise ValueError("deliverable ordinals must start at one")
        if self.scope == "deliverable" and not self.deliverable_ordinals:
            raise ValueError("deliverable-scoped requirements need ordinals")
        if self.scope == "time_window":
            if self.start_seconds is None or self.end_seconds is None:
                raise ValueError("time-window requirements need start and end")
            if self.end_seconds <= self.start_seconds:
                raise ValueError("requirement time window must be ordered")
        elif self.start_seconds is not None or self.end_seconds is not None:
            raise ValueError("timing is allowed only for time-window requirements")
        return self


class CreativeIntentManifest(BaseModel):
    """The single source of truth handed from the Producer to every AI role."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = INTENT_SCHEMA_VERSION
    objective: str = Field(min_length=1, max_length=3000)
    requirements: list[CreativeIntentRequirement] = Field(
        min_length=1,
        max_length=128,
    )
    transformation_contract: SourceTransformationContract | None = None
    manifest_sha256: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def _identity(self) -> "CreativeIntentManifest":
        ids = [item.requirement_id for item in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("intent requirement IDs must be unique")
        expected = creative_intent_manifest_sha256(self)
        if self.manifest_sha256 is not None and self.manifest_sha256 != expected:
            raise ValueError("manifest_sha256 does not match the intent manifest")
        return self


class RequirementExecutionMapping(BaseModel):
    """A Director-authored explanation of where one requirement is realized."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(pattern=r"^R-[0-9]{3}$")
    implementation: str = Field(min_length=1, max_length=3000)
    script_line_ids: list[str] = Field(default_factory=list, max_length=256)
    capability_node_ids: list[str] = Field(default_factory=list, max_length=64)
    segment_indices: list[int] = Field(default_factory=list, max_length=1000)
    evidence_plan: list[str] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _unique_coordinates(self) -> "RequirementExecutionMapping":
        for field_name in (
            "script_line_ids",
            "capability_node_ids",
            "segment_indices",
        ):
            values = list(getattr(self, field_name))
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        if any(value < 1 for value in self.segment_indices):
            raise ValueError("segment indices must start at one")
        return self


def creative_intent_manifest_sha256(
    manifest: CreativeIntentManifest | dict[str, Any],
) -> str:
    payload = (
        manifest.model_dump(mode="json")
        if isinstance(manifest, CreativeIntentManifest)
        else dict(manifest)
    )
    payload.pop("manifest_sha256", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sign_creative_intent_manifest(
    manifest: CreativeIntentManifest,
) -> CreativeIntentManifest:
    return manifest.model_copy(
        update={"manifest_sha256": creative_intent_manifest_sha256(manifest)}
    )


def intent_manifest_from_spec(
    intent_spec: dict[str, Any] | None,
) -> CreativeIntentManifest:
    payload = dict(intent_spec or {}).get("intent_manifest")
    if not isinstance(payload, dict):
        raise ValueError("producer intent_spec requires intent_manifest v2")
    return CreativeIntentManifest.model_validate(payload)


def applicable_requirements(
    manifest: CreativeIntentManifest,
    *,
    deliverable_ordinal: int | None = None,
) -> list[CreativeIntentRequirement]:
    return [
        item
        for item in manifest.requirements
        if not item.deliverable_ordinals
        or deliverable_ordinal is None
        or deliverable_ordinal in item.deliverable_ordinals
    ]


def validate_requirement_execution_coverage(
    manifest: CreativeIntentManifest,
    mappings: list[RequirementExecutionMapping],
    *,
    deliverable_ordinal: int | None = None,
    valid_script_line_ids: set[str] | None = None,
    valid_capability_node_ids: set[str] | None = None,
    valid_segment_indices: set[int] | None = None,
) -> None:
    applicable = applicable_requirements(
        manifest,
        deliverable_ordinal=deliverable_ordinal,
    )
    applicable_by_id = {item.requirement_id: item for item in applicable}
    mapping_by_id = {item.requirement_id: item for item in mappings}
    if len(mapping_by_id) != len(mappings):
        raise ValueError("requirement execution mappings must be unique")
    unknown = sorted(set(mapping_by_id) - set(applicable_by_id))
    if unknown:
        raise ValueError(f"requirement mappings cite unknown requirements: {unknown}")
    required = {
        item.requirement_id
        for item in applicable
        if item.priority in {"critical", "high"}
    }
    missing = sorted(required - set(mapping_by_id))
    if missing:
        raise ValueError(
            "critical/high intent requirements lack execution mappings: "
            f"{missing}"
        )
    for mapping in mappings:
        if valid_script_line_ids is not None:
            invalid = sorted(set(mapping.script_line_ids) - valid_script_line_ids)
            if invalid:
                raise ValueError(
                    f"requirement {mapping.requirement_id} cites unknown script lines: {invalid}"
                )
        if valid_capability_node_ids is not None:
            invalid = sorted(
                set(mapping.capability_node_ids) - valid_capability_node_ids
            )
            if invalid:
                raise ValueError(
                    f"requirement {mapping.requirement_id} cites unknown capabilities: {invalid}"
                )
        if valid_segment_indices is not None:
            invalid = sorted(set(mapping.segment_indices) - valid_segment_indices)
            if invalid:
                raise ValueError(
                    f"requirement {mapping.requirement_id} cites unknown segments: {invalid}"
                )


def requirement_review_packet(
    manifest: CreativeIntentManifest,
    requirement_ids: list[str],
) -> list[dict[str, Any]]:
    selected = set(requirement_ids)
    return [
        {
            "requirement_id": item.requirement_id,
            "kind": item.kind,
            "priority": item.priority,
            "scope": item.scope,
            "start_seconds": item.start_seconds,
            "end_seconds": item.end_seconds,
            "intent": item.intent,
            "interpretation": item.interpretation,
            "observable_checks": list(item.observable_checks),
            "must_not_reuse": list(item.must_not_reuse),
        }
        for item in manifest.requirements
        if item.requirement_id in selected
    ]


__all__ = [
    "CreativeIntentManifest",
    "CreativeIntentRequirement",
    "INTENT_SCHEMA_VERSION",
    "RequirementExecutionMapping",
    "applicable_requirements",
    "creative_intent_manifest_sha256",
    "intent_manifest_from_spec",
    "requirement_review_packet",
    "sign_creative_intent_manifest",
    "validate_requirement_execution_coverage",
]
