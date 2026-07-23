from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.services.hermes_agent.content_director import (
    DirectorCapabilitySpec,
    DirectorProjectBrief,
    DirectorSeriesBrief,
)


_MANIFEST_PATH = Path(__file__).with_name("content_capabilities.v1.json")


class ContentCapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    capabilities: list[DirectorCapabilitySpec] = Field(
        min_length=1,
        max_length=128,
    )


@lru_cache(maxsize=1)
def load_content_capability_manifest() -> ContentCapabilityManifest:
    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = ContentCapabilityManifest.model_validate(payload)
    names = [item.capability for item in manifest.capabilities]
    if len(names) != len(set(names)):
        raise ValueError("content capability manifest contains duplicate names")
    return manifest


def validate_brief_capabilities_against_registry(
    brief: DirectorProjectBrief | DirectorSeriesBrief,
) -> None:
    """Require every brief capability to be an exact registered spec."""
    registered = {
        item.capability: item
        for item in load_content_capability_manifest().capabilities
    }
    for requested in brief.capability_catalog:
        authoritative = registered.get(requested.capability)
        if authoritative is None:
            raise ValueError(
                f"director brief requests unregistered capability: "
                f"{requested.capability}"
            )
        if (
            requested.model_dump(mode="json")
            != authoritative.model_dump(mode="json")
        ):
            raise ValueError(
                f"director brief changed registered capability contract or "
                f"policy: {requested.capability}"
            )


__all__ = [
    "ContentCapabilityManifest",
    "load_content_capability_manifest",
    "validate_brief_capabilities_against_registry",
]
