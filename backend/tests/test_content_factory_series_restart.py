from __future__ import annotations

from app.data.models.hermes_agent import (
    HermesContentFactoryAsset,
    HermesContentFactoryProject,
    HermesContentFactoryStage,
    HermesContentSeriesSlate,
)
from app.services.hermes_agent.content_factory import restart_project


def test_series_restart_replans_only_missing_variants_and_preserves_completed(
    db_session,
    tmp_path,
):
    video = tmp_path / "v1.mp4"
    video.write_bytes(b"video" * 400)
    guide = tmp_path / "v1.md"
    guide.write_text("guide", encoding="utf-8")
    incomplete_image = tmp_path / "v2.png"
    incomplete_image.write_bytes(b"image" * 400)

    project = HermesContentFactoryProject(
        project_key="cf_series_restart",
        workspace_id=3,
        user_id=6,
        title="Series",
        product_name="Product",
        status="paused",
        current_stage="DIRECTOR",
        config_json={
            "video_count": 3,
            "manual_paused": False,
            "director_series_brief": {
                "series_id": "cf_series_restart.series",
                "series_version": 2,
                "objective": "Test continuation",
                "platform": "short-video",
                "locale": "en-US",
                "audience": "Adults",
                "target_count": 3,
                "minimum_duration_seconds": 10,
                "maximum_duration_seconds": 10,
                "default_duration_seconds": 10,
                "edit_headroom_seconds": 1,
                "speech_rate_wpm": 150,
                "aspect_ratio": "9:16",
                "capability_catalog": [{
                    "capability": "voiceover",
                    "input_contract": "script",
                    "output_contract": "spoken_audio",
                    "policy": {},
                }],
                "copy_review_criteria": [{
                    "criterion_id": "clarity",
                    "instruction": "Be clear.",
                    "minimum_score": 80,
                    "blocking": True,
                }],
                "diversity_requirements": [{
                    "dimension_id": "form",
                    "instruction": "Vary form.",
                    "minimum_unique_values": 1,
                }],
            },
            "director_briefs_by_variant": {
                "1": {"brief_id": "completed"},
                "2": {"brief_id": "rejected"},
                "3": {"brief_id": "unstarted"},
            },
            "approved_series_slate_sha256": "old-slate",
        },
        state_json={
            "approved_series_slate": {"slate_sha256": "old-slate"},
            "approved_director_artifacts_by_variant": {
                "1": {"artifact": "keep"},
                "2": {"artifact": "drop"},
            },
            "approved_production_plans_by_variant": {
                "1": {"plan": "keep"},
                "2": {"plan": "drop"},
            },
            "video_variant_pipeline": {
                "target_count": 3,
                "active_index": 2,
                "completed_indices": [1],
                "submitted_indices": [1, 2],
                "failed_indices": [2],
            },
            "ai_video_groups": [],
        },
    )
    db_session.add(project)
    db_session.flush()

    db_session.add(HermesContentSeriesSlate(
        project_id=project.id,
        workspace_id=3,
        user_id=6,
        series_id="cf_series_restart.series",
        series_version=2,
        status="approved",
        brief_sha256="a" * 64,
        slate_sha256="b" * 64,
        brief_json={"series_version": 2},
        slate_json={"series_version": 2},
        attempts_json=[],
        reviews_json=[],
        reason="approved",
    ))
    db_session.flush()

    completed_video = HermesContentFactoryAsset(
        project_id=project.id,
        workspace_id=3,
        user_id=6,
        stage="VIDEO_PROMPTS",
        kind="video",
        original_name="v1.mp4",
        file_path=str(video),
        mime_type="video/mp4",
        size_bytes=video.stat().st_size,
        meta_json={"content_factory_video_index": 1, "variant_index": 1},
    )
    completed_guide = HermesContentFactoryAsset(
        project_id=project.id,
        workspace_id=3,
        user_id=6,
        stage="EDIT_PACKAGE",
        kind="edit_guidance",
        original_name="v1.md",
        file_path=str(guide),
        mime_type="text/markdown",
        size_bytes=guide.stat().st_size,
        meta_json={"content_factory_video_index": 1, "variant_index": 1},
    )
    incomplete_asset = HermesContentFactoryAsset(
        project_id=project.id,
        workspace_id=3,
        user_id=6,
        stage="VISUAL_PREVIEW",
        kind="generated_image",
        original_name="v2.png",
        file_path=str(incomplete_image),
        mime_type="image/png",
        size_bytes=incomplete_image.stat().st_size,
        meta_json={"variant_index": 2},
    )
    completed_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=6,
        stage="DIRECTOR",
        attempt=1,
        status="success",
        input_json={"variant_index": 1},
    )
    rejected_stage = HermesContentFactoryStage(
        project_id=project.id,
        workspace_id=3,
        user_id=6,
        stage="DIRECTOR",
        attempt=2,
        status="failed",
        input_json={"variant_index": 2},
    )
    db_session.add_all([
        completed_video,
        completed_guide,
        incomplete_asset,
        completed_stage,
        rejected_stage,
    ])
    db_session.flush()
    keep_asset_ids = {completed_video.id, completed_guide.id}
    drop_asset_id = incomplete_asset.id

    restarted = restart_project(
        db_session,
        project,
        stage="SERIES_DIRECTOR",
        instruction="Replan only the missing videos.",
        allowed_audio_modes=["spoken"],
    )
    db_session.flush()

    assert restarted.status == "ready"
    assert restarted.current_stage == "SERIES_DIRECTOR"
    assert (
        restarted.config_json["director_series_brief"]["series_version"]
        == 3
    )
    assert restarted.config_json["director_series_brief"][
        "allowed_audio_modes"
    ] == ["spoken"]
    assert restarted.state_json["series_audio_policy_override"][
        "applies_to_variant_indices"
    ] == [2, 3]
    assert restarted.state_json["series_director_version_allocation"] == {
        "series_id": "cf_series_restart.series",
        "previous_configured_version": 2,
        "latest_persisted_version": 2,
        "allocated_version": 3,
        "reason": "continuation_replan",
        "at": restarted.state_json[
            "series_director_version_allocation"
        ]["at"],
    }
    assert set(restarted.config_json["director_briefs_by_variant"]) == {"1"}
    assert "approved_series_slate_sha256" not in restarted.config_json
    assert "approved_series_slate" not in restarted.state_json
    assert set(
        restarted.state_json["approved_director_artifacts_by_variant"]
    ) == {"1"}
    assert set(
        restarted.state_json["approved_production_plans_by_variant"]
    ) == {"1"}
    pipeline = restarted.state_json["video_variant_pipeline"]
    assert pipeline["active_index"] == 2
    assert pipeline["submitted_indices"] == [1]
    assert pipeline["failed_indices"] == []
    assert pipeline["completion_blocked_missing_indices"] == [2, 3]
    assert db_session.get(HermesContentFactoryStage, completed_stage.id).status == "success"
    assert db_session.get(HermesContentFactoryStage, rejected_stage.id).status == "superseded"
    assert {
        asset.id
        for asset in db_session.query(HermesContentFactoryAsset)
        .filter(HermesContentFactoryAsset.project_id == project.id)
        .all()
    } == keep_asset_ids
    assert db_session.get(HermesContentFactoryAsset, drop_asset_id) is None

    # A retry before version 3 is persisted must keep the same allocation;
    # otherwise transient failures would burn versions and invalidate any
    # durable page checkpoint on every operator restart.
    restarted_again = restart_project(
        db_session,
        restarted,
        stage="SERIES_DIRECTOR",
        instruction="Retry the same missing-video continuation.",
    )
    assert (
        restarted_again.config_json["director_series_brief"][
            "series_version"
        ]
        == 3
    )
