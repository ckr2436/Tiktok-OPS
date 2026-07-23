from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.hermes_agent.content_director import (
    VideoProductionContract,
)


_MANIFEST_PATH = Path(__file__).with_name(
    "video_model_capabilities.v1.json"
)


class VideoModelCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str = Field(min_length=1, max_length=128)
    aliases: list[str] = Field(default_factory=list, max_length=64)
    label: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=128)
    provider_key: str = Field(min_length=1, max_length=128)
    task_model: str = Field(min_length=1, max_length=255)
    reference_image_minimum: int = Field(ge=0, le=64)
    reference_image_default: int = Field(ge=0, le=64)
    reference_image_maximum: int = Field(ge=0, le=64)
    reference_video_maximum: int = Field(ge=0, le=8)
    segment_duration_minimum_seconds: int = Field(ge=1, le=300)
    segment_duration_maximum_seconds: int = Field(ge=1, le=300)
    allowed_segment_durations_seconds: list[int] = Field(
        default_factory=list,
        max_length=300,
    )
    recommended_project_variant_parallelism: int = Field(ge=1, le=16)
    maximum_project_variant_parallelism: int = Field(ge=1, le=16)
    allows_human_face_references: bool
    hard_rules: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_ranges(self) -> "VideoModelCapability":
        if self.reference_image_minimum > self.reference_image_maximum:
            raise ValueError(
                "reference_image_minimum cannot exceed maximum"
            )
        if not (
            self.reference_image_minimum
            <= self.reference_image_default
            <= self.reference_image_maximum
        ):
            raise ValueError(
                "reference_image_default must be inside its range"
            )
        if (
            self.segment_duration_minimum_seconds
            > self.segment_duration_maximum_seconds
        ):
            raise ValueError(
                "segment duration minimum cannot exceed maximum"
            )
        invalid = [
            value
            for value in self.allowed_segment_durations_seconds
            if not (
                self.segment_duration_minimum_seconds
                <= value
                <= self.segment_duration_maximum_seconds
            )
        ]
        if invalid:
            raise ValueError(
                "allowed segment durations are outside the declared range: "
                f"{invalid}"
            )
        if (
            self.recommended_project_variant_parallelism
            > self.maximum_project_variant_parallelism
        ):
            raise ValueError(
                "recommended project parallelism cannot exceed maximum"
            )
        return self


class VideoModelCapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    models: list[VideoModelCapability] = Field(
        min_length=1,
        max_length=128,
    )


@lru_cache(maxsize=1)
def load_video_model_capability_manifest(
) -> VideoModelCapabilityManifest:
    manifest = VideoModelCapabilityManifest.model_validate(
        json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    identities = [
        value.casefold()
        for model in manifest.models
        for value in [model.model_id, *model.aliases]
    ]
    if len(identities) != len(set(identities)):
        raise ValueError(
            "video model capability manifest contains duplicate IDs or aliases"
        )
    return manifest


def get_video_model_capability(
    model_id: str,
) -> VideoModelCapability:
    requested = str(model_id or "").strip().casefold()
    for model in load_video_model_capability_manifest().models:
        if requested in {
            model.model_id.casefold(),
            *[alias.casefold() for alias in model.aliases],
        }:
            return model
    raise ValueError(f"unregistered video model capability: {model_id}")


def resolve_video_model_policy(
    *,
    model_id: str,
    reference_image_limit: int | None,
    allow_reference_video: bool,
    product_required: bool,
) -> dict[str, Any]:
    model = get_video_model_capability(model_id)
    requested_limit = int(
        reference_image_limit
        if reference_image_limit is not None
        else model.reference_image_default
    )
    reference_limit = max(
        model.reference_image_minimum,
        min(model.reference_image_maximum, requested_limit),
    )
    hard_rules = list(model.hard_rules)
    if product_required:
        hard_rules.append(
            "Product-visible segments must also receive the exact uploaded "
            "package image as product anchor."
        )
    return {
        "id": model.model_id,
        "label": model.label,
        "provider": model.provider,
        "provider_key": model.provider_key,
        "task_model": model.task_model,
        "reference_limit": reference_limit,
        "reference_video_limit": (
            model.reference_video_maximum
            if allow_reference_video
            else 0
        ),
        "segment_duration_min": (
            model.segment_duration_minimum_seconds
        ),
        "segment_duration_max": (
            model.segment_duration_maximum_seconds
        ),
        "allowed_segment_durations_seconds": list(
            model.allowed_segment_durations_seconds
        ),
        "allows_human_face_references": (
            model.allows_human_face_references
        ),
        "recommended_project_variant_parallelism": (
            model.recommended_project_variant_parallelism
        ),
        "maximum_project_variant_parallelism": (
            model.maximum_project_variant_parallelism
        ),
        "hard_rules": hard_rules,
    }


def resolve_project_variant_parallelism(
    *,
    model_id: str,
    requested: int | None,
    target_count: int,
) -> int:
    """Resolve one project's bounded provider window from model capability.

    The manifest owns operational defaults and provider ceilings. A project
    may request a smaller or larger window, but runtime clamps it to the
    registered provider/model limit and the project's actual target count.
    This keeps concurrency policy data-driven without changing any creative
    decisions or silently expanding an existing project.
    """
    model = get_video_model_capability(model_id)
    value = (
        model.recommended_project_variant_parallelism
        if requested is None
        else int(requested)
    )
    return max(
        1,
        min(
            int(target_count),
            model.maximum_project_variant_parallelism,
            value,
        ),
    )


def build_video_production_contract(
    *,
    model_id: str,
    reference_image_limit: int | None,
    allow_reference_video: bool,
) -> VideoProductionContract:
    policy = resolve_video_model_policy(
        model_id=model_id,
        reference_image_limit=reference_image_limit,
        allow_reference_video=allow_reference_video,
        product_required=False,
    )
    return VideoProductionContract(
        model_id=str(policy["id"]),
        segment_duration_minimum_seconds=float(
            policy["segment_duration_min"]
        ),
        segment_duration_maximum_seconds=float(
            policy["segment_duration_max"]
        ),
        allowed_segment_durations_seconds=[
            float(value)
            for value in list(
                policy.get("allowed_segment_durations_seconds") or []
            )
        ],
        reference_image_limit=int(policy["reference_limit"]),
        reference_video_limit=int(policy["reference_video_limit"]),
    )


__all__ = [
    "VideoModelCapability",
    "VideoModelCapabilityManifest",
    "build_video_production_contract",
    "get_video_model_capability",
    "load_video_model_capability_manifest",
    "resolve_project_variant_parallelism",
    "resolve_video_model_policy",
]
