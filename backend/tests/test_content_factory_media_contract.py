from __future__ import annotations

import pytest
from PIL import Image

from app.core.errors import APIError
from app.data.models.hermes_agent import HermesContentFactoryStage
from app.services.hermes_agent.content_factory import create_project, update_project
from app.services.hermes_agent.content_intent import (
    CreativeIntentManifest,
    sign_creative_intent_manifest,
)
from app.services.hermes_agent.content_producer import (
    compile_confirmed_creative_copy_contract,
)
from app.tasks.hermes_agent.content_factory_tasks import (
    _generated_asset_manifest_paths,
    _generated_reference_index,
    _normalize_reference_panel_to_aspect,
    _prune_stale_final_split_files,
)


def test_reference_normalization_uses_generated_blur_fill_not_black_bands(tmp_path):
    target = tmp_path / "wide-reference.png"
    Image.new("RGB", (800, 450), (30, 120, 210)).save(target)

    meta = _normalize_reference_panel_to_aspect(target, aspect_ratio="9:16")

    with Image.open(target) as image:
        assert image.size == (1080, 1920)
        assert image.getpixel((10, 10)) != (18, 18, 18)
        assert image.getpixel((540, 960)) == (30, 120, 210)
    assert meta["mode"] == "contain_over_blurred_fill"
    assert meta["padded"] is True


def test_reference_normalization_recovers_an_existing_legacy_matte(tmp_path):
    target = tmp_path / "legacy-reference.png"
    legacy = Image.new("RGB", (1080, 1920), (18, 18, 18))
    foreground = Image.new("RGB", (1080, 1709), (70, 150, 90))
    legacy.paste(foreground, (0, 105))
    legacy.save(target)

    meta = _normalize_reference_panel_to_aspect(
        target,
        aspect_ratio="9:16",
        existing_normalization={
            "padded": True,
            "source_width": 393,
            "source_height": 622,
        },
    )

    with Image.open(target) as image:
        assert image.getpixel((10, 10)) != (18, 18, 18)
        assert image.getpixel((540, 960)) == (70, 150, 90)
    assert meta["mode"] == "center_crop_full_bleed"
    assert meta["padded"] is False


def test_reference_normalization_crops_near_aspect_storyboard_gutters_full_bleed(tmp_path):
    target = tmp_path / "storyboard-cell.png"
    cell = Image.new("RGB", (393, 622), (255, 255, 255))
    scene = Image.new("RGB", (383, 592), (82, 41, 133))
    cell.paste(scene, (5, 15))
    cell.save(target)

    meta = _normalize_reference_panel_to_aspect(target, aspect_ratio="9:16")

    with Image.open(target) as image:
        assert image.size == (1080, 1920)
        assert image.getpixel((10, 10)) == (82, 41, 133)
        assert image.getpixel((1070, 1910)) == (82, 41, 133)
    assert meta["mode"] == "center_crop_full_bleed"
    assert meta["padded"] is False


def _project(db_session):
    project = create_project(
        db_session,
        workspace_id=301,
        user_id=401,
        title="Universal short-form series",
        content_objective="Explain a supplied topic accurately.",
        target_audience="Adults interested in the supplied topic.",
        content_mode="education",
        product_required=False,
        product_name="",
        market="US",
        product_brief=None,
        video_count=3,
        video_duration_min_seconds=20,
        video_duration_max_seconds=40,
        video_model="omni_flash",
        video_resolution="720p",
        video_aspect_ratio="9:16",
        video_language="en-US",
        auto_run=False,
    )
    db_session.flush()
    return project


def test_adaptive_reference_copy_compiles_as_independent_editable_videos(
    db_session,
):
    manifest = sign_creative_intent_manifest(
        CreativeIntentManifest.model_validate({
            "objective": "Create three differentiated videos from reference copy.",
            "requirements": [{
                "requirement_id": "R-001",
                "kind": "objective",
                "priority": "critical",
                "scope": "project",
                "intent": "Preserve the conversion logic while rewriting the copy.",
                "evidence_quote": "可以适当修改，但转化逻辑不能变",
                "interpretation": "The wording is editable; the conversion logic remains.",
                "observable_checks": [
                    "Each output uses new wording and retains the conversion arc."
                ],
            }],
            "transformation_contract": {
                "source_role": "reference_copy",
                "fidelity": "adaptive",
                "execution_strategy": "full_regeneration",
                "transfer_mode": "semantic_structure",
                "source_media_reuse": "forbidden",
                "protected_requirements": ["Preserve the conversion logic."],
                "authorized_changes": [{
                    "instruction": "Rewrite the wording and create differentiated videos.",
                    "dimensions": ["spoken wording", "story premise", "setting"],
                    "evidence_quote": "可以适当修改，但转化逻辑不能变",
                }],
                "creative_freedom": ["Invent original hooks and settings."],
                "excluded_source_artifacts": ["source-specific wording"],
                "success_checks": ["Every video has an original script."],
                "rationale": "The user authorized adaptive rewriting.",
            },
        })
    )
    intent = {
        "delivery_mode": "independent_videos",
        "source_material_mode": "reference_copy",
        "user_goal": "Create three differentiated videos.",
        "intent_manifest": manifest.model_dump(mode="json"),
        "deliverables": [
            {
                "ordinal": ordinal,
                "label": f"Video {ordinal}",
                "objective": f"Create differentiated video {ordinal}",
                "relationship": "independent",
                "target_duration_seconds": 20,
                "differentiation": [f"Distinct hook {ordinal}"],
            }
            for ordinal in range(1, 4)
        ],
    }
    copy_contract = compile_confirmed_creative_copy_contract(
        intent_spec=intent,
        authoritative_script=(181, "Reference copy with a proven conversion arc."),
    )

    project = create_project(
        db_session,
        workspace_id=301,
        user_id=401,
        title="Adaptive reference-copy series",
        content_objective="Create differentiated short videos.",
        target_audience="US TikTok viewers",
        content_mode="general",
        product_required=False,
        product_name="",
        market="US",
        product_brief=None,
        video_count=3,
        video_duration_min_seconds=20,
        video_duration_max_seconds=20,
        video_model="omni_flash",
        video_resolution="720p",
        video_aspect_ratio="9:16",
        video_language="en-US",
        creative_copy_contract=copy_contract,
        producer_intent_spec=intent,
        auto_run=False,
    )

    assert project.config_json["creative_copy_contract"][
        "copy_authority"
    ] == "producer_draft_editable"
    assert "required_verbatim_voiceover" not in project.config_json[
        "creative_copy_contract"
    ]
    assert project.config_json["director_series_brief"]["target_count"] == 3


def test_media_contract_can_change_before_media_generation(db_session):
    project = _project(db_session)

    update_project(
        db_session,
        project,
        values={"video_aspect_ratio": "16:9"},
    )

    assert project.config_json["video_aspect_ratio"] == "16:9"
    assert project.config_json["director_series_brief"]["aspect_ratio"] == "16:9"


def test_context_only_product_project_does_not_create_product_conversion_gate(
    db_session,
):
    project = create_project(
        db_session,
        workspace_id=301,
        user_id=401,
        title="Product-category hooks without product appearance",
        content_objective="Create ten product-related opening hooks.",
        target_audience="US nighttime wellness viewers.",
        content_mode="product",
        product_use_mode="context_only",
        product_required=False,
        product_name="",
        market="US",
        product_brief=None,
        video_count=10,
        video_duration_min_seconds=4,
        video_duration_max_seconds=4,
        video_model="omni_flash",
        auto_run=False,
    )

    assert project.config_json["product_use_mode"] == "context_only"
    assert project.config_json["product_required"] is False
    assert project.config_json["director_series_brief"]["conversion"][
        "product_required"
    ] is False


def test_product_use_mode_cannot_disagree_with_product_required(db_session):
    with pytest.raises(APIError) as error:
        create_project(
            db_session,
            workspace_id=301,
            user_id=401,
            title="Impossible product contract",
            content_mode="product",
            product_use_mode="context_only",
            product_required=True,
            product_name="Product",
            market="US",
            product_brief=None,
            video_count=1,
            video_duration_min_seconds=4,
            video_duration_max_seconds=4,
            video_model="omni_flash",
            auto_run=False,
        )

    assert error.value.code == "CONTENT_PRODUCT_USE_CONTRACT_CONFLICT"


def test_board_and_image_model_chain_are_project_owned_media_contract(db_session):
    project = _project(db_session)

    update_project(
        db_session,
        project,
        values={
            "visual_reference_generation_mode": "board",
            "visual_image_model_chain": [
                "gpt-image-2.0",
                "nano_banana_pro",
            ],
        },
    )

    assert project.config_json["visual_reference_generation_mode"] == "board"
    assert project.config_json["visual_image_model_chain"] == [
        "gpt-image-2",
        "nano_banana_pro",
    ]


def test_new_image_to_video_project_defaults_to_splittable_board(db_session):
    project = _project(db_session)

    assert project.config_json["visual_reference_generation_mode"] == "board"


def test_text_to_video_is_a_persisted_zero_reference_media_contract(db_session):
    project = create_project(
        db_session,
        workspace_id=301,
        user_id=401,
        title="Prompt-only park story",
        content_objective="Pair a locked voiceover with empty park scenery.",
        target_audience="US adults",
        content_mode="general",
        product_required=False,
        product_name="",
        market="US",
        product_brief=None,
        video_count=1,
        video_duration_min_seconds=120,
        video_duration_max_seconds=120,
        video_model="omni_flash",
        video_resolution="720p",
        video_aspect_ratio="9:16",
        video_language="en-US",
        allow_reference_video=True,
        video_generation_mode="text_to_video",
        auto_run=False,
    )

    assert project.config_json["video_generation_mode"] == "text_to_video"
    assert project.config_json["allow_reference_video"] is False
    assert any(
        "must not generate, extract, upload, or attach reference images"
        in constraint
        for constraint in project.config_json["director_creative_constraints"]
    )


def test_source_transformation_contract_reaches_director_before_project_starts(
    db_session,
):
    intent = {
        "delivery_mode": "single",
        "source_material_mode": "reference_copy",
        "user_goal": "Improve only the first two seconds of the supplied video.",
        "shared_requirements": [],
        "variation_axes": ["opening product information only"],
        "non_negotiables": ["Preserve the original story, audio, pacing and ending."],
        "acceptance_criteria": ["Frames after two seconds retain the source sequence."],
        "deliverables": [],
        "transformation_contract": {
            "source_role": "authoritative edit source",
            "fidelity": "exact_outside_authorized_changes",
            "execution_strategy": "local_edit",
            "transfer_mode": "source_media",
            "source_media_reuse": "required",
            "protected_requirements": [
                "Preserve the original story, audio, pacing and ending."
            ],
            "authorized_changes": [{
                "instruction": "Add product information during 0-2 seconds.",
                "dimensions": ["visual overlay"],
                "start_seconds": 0,
                "end_seconds": 2,
                "evidence_quote": "only change the first two seconds",
            }],
            "creative_freedom": ["Choose a readable overlay placement."],
            "excluded_source_artifacts": [],
            "success_checks": ["Original audio remains unchanged."],
            "rationale": "The user authorized one bounded change.",
        },
    }
    project = create_project(
        db_session,
        workspace_id=301,
        user_id=401,
        title="Bounded source edit",
        content_objective="Improve only the first two seconds.",
        target_audience="US TikTok viewers",
        content_mode="general",
        product_required=False,
        product_name="",
        market="US",
        product_brief=None,
        video_count=1,
        video_duration_min_seconds=20,
        video_duration_max_seconds=20,
        video_model="omni_flash",
        video_resolution="720p",
        video_aspect_ratio="9:16",
        video_language="en-US",
        producer_intent_spec=intent,
        auto_run=False,
    )

    assert project.config_json["producer_intent_spec"] == intent
    brief = project.config_json["director_series_brief"]
    contract = brief["truth_payload"]["source_transformation_contract"]
    assert contract["fidelity"] == "exact_outside_authorized_changes"
    assert contract["transfer_mode"] == "source_media"
    assert contract["source_media_reuse"] == "required"
    assert any(
        row["criterion_id"] == "source_change_boundary_fidelity"
        and row["minimum_score"] == 100
        for row in brief["copy_review_criteria"]
    )
    copy_boundary = next(
        row
        for row in brief["copy_review_criteria"]
        if row["criterion_id"] == "source_change_boundary_fidelity"
    )
    assert "do not require" in copy_boundary["instruction"].lower()
    assert "media provenance" in copy_boundary["instruction"].lower()
    assert "final media originality audit" in copy_boundary["instruction"].lower()
    assert any(
        row["criterion_id"] == "source_change_boundary_fidelity"
        for row in brief["series_global_review_criteria"]
    )
    series_boundary = next(
        row
        for row in brief["series_global_review_criteria"]
        if row["criterion_id"] == "source_change_boundary_fidelity"
    )
    assert "distinctive source premise" in series_boundary["instruction"]
    assert "abstract structures" in series_boundary["instruction"]


def test_media_contract_is_frozen_after_media_generation_starts(db_session):
    project = _project(db_session)
    db_session.add(
        HermesContentFactoryStage(
            project_id=project.id,
            workspace_id=project.workspace_id,
            user_id=project.user_id,
            stage="VISUAL_PREVIEW",
            attempt=1,
            status="failed",
        )
    )
    db_session.flush()

    with pytest.raises(APIError) as exc_info:
        update_project(
            db_session,
            project,
            values={"video_aspect_ratio": "16:9"},
        )

    assert exc_info.value.code == "CONTENT_MEDIA_CONTRACT_FROZEN"
    assert "video_aspect_ratio" in exc_info.value.message
    assert project.config_json["video_aspect_ratio"] == "9:16"

    with pytest.raises(APIError) as content_exc:
        update_project(
            db_session,
            project,
            values={"content_objective": "A changed objective"},
        )
    assert content_exc.value.code == "CONTENT_MEDIA_CONTRACT_FROZEN"


def test_runtime_parallelism_remains_editable_after_media_generation_starts(
    db_session,
):
    project = _project(db_session)
    db_session.add(
        HermesContentFactoryStage(
            project_id=project.id,
            workspace_id=project.workspace_id,
            user_id=project.user_id,
            stage="VIDEO_PROMPTS",
            attempt=1,
            status="success",
        )
    )
    db_session.flush()

    update_project(
        db_session,
        project,
        values={"max_api_video_variants_in_flight": 3},
    )

    assert project.config_json["max_api_video_variants_in_flight"] == 3


def test_final_asset_manifest_excludes_clean_plate_and_evidence_paths():
    final_paths = [f"/outbox/final-{index}.png" for index in range(1, 6)]
    envelope = {
        "result": {
            "reference_images": [
                {
                    "index": index,
                    "path": path,
                    "clean_plate_path": (
                        "/outbox/product-scene-clean-plate.png"
                        if index == 5
                        else None
                    ),
                }
                for index, path in enumerate(final_paths, 1)
            ],
        },
        "evidence": {
            "files": final_paths,
            "diagnostic_path": "/outbox/visual-review-debug.png",
        },
    }

    assert _generated_asset_manifest_paths("FINAL_ASSETS", envelope) == final_paths


def test_native_reference_filename_recovers_signed_row_index():
    assert _generated_reference_index(
        "/outbox/visual-preview-api-reference-05-deadbeef.png"
    ) == 5
    assert _generated_reference_index("/outbox/final_assets-3.png") == 3
    assert _generated_reference_index("/outbox/clean-plate.png") == 0


def test_final_split_prunes_retired_clean_plates_and_numbered_leftovers(tmp_path):
    saved = [tmp_path / f"final_assets-{index}.png" for index in range(1, 6)]
    for path in saved:
        path.write_bytes(b"final")
    clean_plate = tmp_path / "final_assets-5-clean-plate.png"
    clean_plate.write_bytes(b"clean")
    stale = tmp_path / "final_assets-6.png"
    stale.write_bytes(b"stale")
    diagnostic = tmp_path / "final_assets-debug.png"
    diagnostic.write_bytes(b"keep")

    removed = _prune_stale_final_split_files(
        tmp_path,
        saved_paths=[str(path) for path in saved],
        split_manifest=[{"clean_plate_path": str(clean_plate)}],
    )

    assert removed == [str(clean_plate), str(stale)]
    assert not stale.exists()
    assert not clean_plate.exists()
    assert diagnostic.exists()
