from __future__ import annotations

import pytest

from app.services.hermes_agent.content_capabilities import (
    load_content_capability_manifest,
    validate_brief_capabilities_against_registry,
)
from app.services.hermes_agent.content_director import (
    ConversionIntent,
    CopyReviewCriterion,
    DirectorCapabilitySpec,
    DirectorProjectBrief,
)


def _brief(capability: DirectorCapabilitySpec) -> DirectorProjectBrief:
    return DirectorProjectBrief(
        brief_id="registry-test",
        objective="Create a video.",
        platform="TikTok",
        locale="en-US",
        audience="US adults.",
        target_duration_seconds=20,
        edit_headroom_seconds=2,
        speech_rate_wpm=150,
        aspect_ratio="9:16",
        conversion=ConversionIntent(product_required=False),
        capability_catalog=[capability],
        copy_review_criteria=[
            CopyReviewCriterion(
                criterion_id="clarity",
                instruction="Understandable on first listen.",
                minimum_score=80,
            )
        ],
    )


def test_capability_manifest_is_unique_and_exact():
    manifest = load_content_capability_manifest()
    names = [item.capability for item in manifest.capabilities]
    assert len(names) == len(set(names))
    validate_brief_capabilities_against_registry(
        _brief(manifest.capabilities[0])
    )

    changed = manifest.capabilities[0].model_copy(
        update={"policy": {"media_spend": True}}
    )
    with pytest.raises(ValueError, match="changed registered capability"):
        validate_brief_capabilities_against_registry(_brief(changed))

    invented = DirectorCapabilitySpec(
        capability="invented.magic",
        input_contract="Anything",
        output_contract="Everything",
    )
    with pytest.raises(ValueError, match="unregistered capability"):
        validate_brief_capabilities_against_registry(_brief(invented))
