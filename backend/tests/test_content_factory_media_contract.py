from __future__ import annotations

import pytest

from app.core.errors import APIError
from app.data.models.hermes_agent import HermesContentFactoryStage
from app.services.hermes_agent.content_factory import create_project, update_project
from app.tasks.hermes_agent.content_factory_tasks import (
    _generated_asset_manifest_paths,
    _generated_reference_index,
    _prune_stale_final_split_files,
)


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


def test_media_contract_can_change_before_media_generation(db_session):
    project = _project(db_session)

    update_project(
        db_session,
        project,
        values={"video_aspect_ratio": "16:9"},
    )

    assert project.config_json["video_aspect_ratio"] == "16:9"
    assert project.config_json["director_series_brief"]["aspect_ratio"] == "16:9"


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
