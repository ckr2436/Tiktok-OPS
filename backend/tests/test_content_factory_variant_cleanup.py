from types import SimpleNamespace

from app.tasks.hermes_agent.content_factory_tasks import (
    _clean_generated_variant_assets,
    _generated_asset_variant_index,
    _prune_project_asset_state,
)
from app.services.hermes_agent.content_factory import (
    _restore_visual_resume_instruction,
)


def _asset(*, stage: str, kind: str, meta: dict):
    return SimpleNamespace(stage=stage, kind=kind, meta_json=meta)


def test_legacy_composed_video_uses_video_index_for_variant_cleanup():
    asset = _asset(
        stage="VIDEO_PROMPTS",
        kind="video",
        meta={"content_factory_video_index": 2, "is_composed_final": True},
    )

    assert _generated_asset_variant_index(asset) == 2


def test_explicit_variant_index_remains_authoritative():
    asset = _asset(
        stage="FINAL_ASSETS",
        kind="generated_image",
        meta={"variant_index": 3, "content_factory_video_index": 8},
    )

    assert _generated_asset_variant_index(asset) == 3


def test_legacy_visual_asset_without_variant_is_first_variant():
    asset = _asset(stage="VISUAL_PREVIEW", kind="generated_image", meta={})

    assert _generated_asset_variant_index(asset) == 1


def test_deleted_asset_ids_are_pruned_from_project_state():
    project = SimpleNamespace(
        state_json={
            "ai_video_final_asset_ids": [10, "11", 12],
            "editor_guidance_asset_ids": [20, 21],
        }
    )

    _prune_project_asset_state(project, {11, 21})

    assert project.state_json["ai_video_final_asset_ids"] == [10, 12]
    assert project.state_json["editor_guidance_asset_ids"] == [20]


def test_partial_visual_cleanup_keeps_approved_reference_file(tmp_path):
    failed_path = tmp_path / "visual-preview-api-reference-01-failed.png"
    paid_outbox_path = tmp_path / "provider-paid-reference-01.png"
    passed_path = tmp_path / "visual-preview-api-reference-02-approved.png"
    failed_path.write_bytes(b"failed")
    paid_outbox_path.write_bytes(b"paid-provider-result")
    passed_path.write_bytes(b"approved")
    failed = SimpleNamespace(
        id=101,
        stage="VISUAL_PREVIEW",
        kind="generated_image",
        original_name=failed_path.name,
        file_path=str(failed_path),
        meta_json={
            "variant_index": 24,
            "reference_index": 1,
            "outbox_path": str(paid_outbox_path),
        },
    )
    passed = SimpleNamespace(
        id=102,
        stage="VISUAL_PREVIEW",
        kind="generated_image",
        original_name=passed_path.name,
        file_path=str(passed_path),
        meta_json={"variant_index": 24, "reference_index": 2},
    )

    class Query:
        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return [failed, passed]

    deleted = []
    db = SimpleNamespace(
        query=lambda *_args: Query(),
        delete=deleted.append,
        flush=lambda: None,
    )
    project = SimpleNamespace(
        id=168,
        workspace_id=3,
        project_key="cf_partial",
        state_json={},
    )

    removed = _clean_generated_variant_assets(
        db,
        project,
        ("VISUAL_PREVIEW", "FINAL_ASSETS"),
        variant_index=24,
        preserve_asset_ids={102},
    )

    assert removed == 1
    assert not failed_path.exists()
    assert paid_outbox_path.read_bytes() == b"paid-provider-result"
    assert passed_path.read_bytes() == b"approved"
    assert deleted == [failed]


def test_visual_resume_preserves_empty_source_instruction_and_audits_operator_note():
    stage = SimpleNamespace(instruction="Resume the paid image task.")

    stage_input = _restore_visual_resume_instruction(
        stage,
        {"source_instruction": None},
        {"visual_api": {"status": "partial_resumable"}},
    )

    assert stage.instruction is None
    assert stage_input["resume_operator_instruction"] == (
        "Resume the paid image task."
    )


def test_visual_resume_keeps_exact_nonempty_source_instruction():
    stage = SimpleNamespace(instruction="Operator-only note")

    stage_input = _restore_visual_resume_instruction(
        stage,
        {"source_instruction": "Original creative direction"},
        {},
    )

    assert stage.instruction == "Original creative direction"
    assert stage_input["resume_operator_instruction"] == "Operator-only note"
