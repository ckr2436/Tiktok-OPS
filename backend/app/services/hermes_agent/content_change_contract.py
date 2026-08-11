"""Structured user-authorized change boundaries for source-based productions.

The Producer AI decides the values in this contract from the conversation.  The
runtime deliberately knows only generic media operations and fidelity levels;
it does not encode campaign copy, hooks, products, characters, or story rules.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AuthorizedSourceChange(BaseModel):
    """One bounded change explicitly authorized by the user."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=2000)
    dimensions: list[str] = Field(default_factory=list, max_length=32)
    start_seconds: float | None = Field(default=None, ge=0, le=3600)
    end_seconds: float | None = Field(default=None, gt=0, le=3600)
    evidence_quote: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _valid_window(self) -> "AuthorizedSourceChange":
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds <= self.start_seconds
        ):
            raise ValueError("authorized change end_seconds must exceed start_seconds")
        return self


class SourceTransformationContract(BaseModel):
    """AI-authored semantic diff contract between source and requested output."""

    model_config = ConfigDict(extra="forbid")

    source_role: str = Field(default="reference_video", min_length=1, max_length=128)
    fidelity: Literal[
        "inspiration",
        "adaptive",
        "exact_outside_authorized_changes",
        "exact",
    ] = "adaptive"
    execution_strategy: Literal[
        "director_decides",
        "local_edit",
        "selective_regeneration",
        "full_regeneration",
    ] = "director_decides"
    transfer_mode: Literal[
        "inspiration_only",
        "semantic_structure",
        "selective_elements",
        "source_media",
    ] = "semantic_structure"
    source_media_reuse: Literal[
        "director_decides",
        "forbidden",
        "allowed",
        "required",
    ] = "director_decides"
    protected_requirements: list[str] = Field(default_factory=list, max_length=64)
    authorized_changes: list[AuthorizedSourceChange] = Field(
        default_factory=list,
        max_length=32,
    )
    creative_freedom: list[str] = Field(default_factory=list, max_length=64)
    excluded_source_artifacts: list[str] = Field(
        default_factory=list,
        max_length=64,
    )
    success_checks: list[str] = Field(default_factory=list, max_length=64)
    rationale: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _coherent_strategy(self) -> "SourceTransformationContract":
        if self.fidelity == "exact_outside_authorized_changes" and not self.authorized_changes:
            raise ValueError(
                "exact_outside_authorized_changes requires at least one authorized change"
            )
        if self.fidelity in {"exact", "exact_outside_authorized_changes"} and (
            self.execution_strategy == "full_regeneration"
        ):
            raise ValueError(
                "full_regeneration cannot prove exact source preservation"
            )
        if self.execution_strategy == "local_edit" and not self.authorized_changes:
            raise ValueError("local_edit requires at least one authorized change")
        if (
            self.execution_strategy == "full_regeneration"
            and self.source_media_reuse == "required"
        ):
            raise ValueError(
                "full_regeneration cannot require reuse of source media"
            )
        if (
            self.transfer_mode == "source_media"
            and self.source_media_reuse == "forbidden"
        ):
            raise ValueError(
                "source_media transfer cannot forbid source media reuse"
            )
        return self


def transformation_contract_from_mapping(
    value: object,
) -> SourceTransformationContract | None:
    if not isinstance(value, dict) or not value:
        return None
    return SourceTransformationContract.model_validate(value)


def transformation_contract_constraint(
    contract: SourceTransformationContract,
) -> str:
    """Render a project-owned instruction without inventing creative content."""
    return (
        "Honor the project source_transformation_contract exactly. The user's "
        "protected_requirements are immutable. Make only authorized_changes "
        "inside their stated time windows; creative_freedom applies only where "
        "it does not conflict with those protections. Select an execution plan "
        f"compatible with fidelity={contract.fidelity} and "
        f"execution_strategy={contract.execution_strategy}, "
        f"transfer_mode={contract.transfer_mode}, and "
        f"source_media_reuse={contract.source_media_reuse}. Reproduce only the "
        "declared semantic or media elements and omit every "
        "excluded_source_artifact. Treat abstract attention architecture and "
        "source story identity as separate layers. A request to reference the "
        "hook, visual pacing, shot density, rhythm, tension curve, reveal timing, "
        "or conversion order authorizes only those abstract structural qualities; "
        "it does not authorize carrying over the source premise, signature "
        "metaphor, character role, setting, prop, action sequence, dialogue "
        "conceit, or product-transition device unless that exact narrative "
        "element is explicitly protected by the user. Even with "
        "transfer_mode=semantic_structure, reuse only the semantic elements "
        "listed in protected_requirements and redesign every other distinctive "
        "source signature. Preserving any authorized structure never authorizes "
        "copying source pixels, actors, audio, captions, platform chrome, or "
        "watermark-covering overlays unless the contract explicitly permits that "
        "media reuse. A visually polished result that changes an unauthorized "
        "element or retains an unauthorized source signature is a failed result."
    )


__all__ = [
    "AuthorizedSourceChange",
    "SourceTransformationContract",
    "transformation_contract_constraint",
    "transformation_contract_from_mapping",
]
