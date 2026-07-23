from __future__ import annotations

from app.tasks.hermes_agent.content_factory_tasks import (
    KIE_TASK_FAIL_MSG_MAX_LENGTH,
    PRODUCT_VISUAL_QA_FAIL_CODE,
    VOICE_CONTINUITY_FAIL_CODE,
    _apply_voice_continuity_retry_prompt,
    _completed_asset_matches_segment_results,
    _voice_continuity_outliers,
)
from app.data.models.kie_api import KieTask


def _record(task_id: int, pitch: float, speaker_id: str = "narrator"):
    return {
        "task_id": task_id,
        "segment_index": task_id,
        "speaker_id": speaker_id,
        "median_pitch_hz": pitch,
        "expected_gender": "",
    }


def test_voice_continuity_rejects_only_minority_cross_gender_outliers():
    report = _voice_continuity_outliers([
        _record(2724, 108.1),
        _record(2725, 202.5),
        _record(2726, 183.9),
        _record(2727, 103.2),
        _record(2728, 197.5),
    ])

    assert report["status"] == "FAIL"
    assert report["blocking_task_ids"] == [2724, 2727]
    assert report["repair_by_task_id"]["2724"]["gender"] == "female"
    assert report["repair_by_task_id"]["2727"]["speaker_id"] == "narrator"


def test_voice_continuity_accepts_normal_intonation_change():
    report = _voice_continuity_outliers([
        _record(1, 175.0),
        _record(2, 188.0),
        _record(3, 205.0),
        _record(4, 181.0),
    ])

    assert report["status"] == "PASS"
    assert report["blocking_task_ids"] == []


def test_voice_continuity_never_compares_different_signed_speakers():
    report = _voice_continuity_outliers([
        _record(1, 105.0, "male_narrator"),
        _record(2, 205.0, "female_character"),
    ])

    assert report["status"] == "PASS"
    assert report["blocking_task_ids"] == []


def test_voice_continuity_failure_code_fits_transport_column():
    column = KieTask.__table__.columns["fail_code"]
    assert len(VOICE_CONTINUITY_FAIL_CODE) <= int(column.type.length)
    assert len(PRODUCT_VISUAL_QA_FAIL_CODE) <= int(column.type.length)
    message_column = KieTask.__table__.columns["fail_msg"]
    assert KIE_TASK_FAIL_MSG_MAX_LENGTH <= int(message_column.type.length)


def test_voice_retry_repairs_the_authoritative_base_prompt_idempotently():
    payload = {
        "prompt": "transport prompt",
        "content_factory_base_prompt": "signed segment prompt",
    }
    repair = {
        "speaker_id": "narrator",
        "gender": "female",
        "target_pitch_hz": 184.0,
    }

    first = _apply_voice_continuity_retry_prompt(payload, repair)
    second = _apply_voice_continuity_retry_prompt(first, repair)

    assert first["content_factory_base_prompt"].startswith(
        "ACOUSTIC VOICE REPAIR (authoritative): narrator must use one adult "
        "female US English voice"
    )
    assert first["prompt"] == first["content_factory_base_prompt"]
    assert first["content_factory_base_prompt"].endswith(
        "signed segment prompt"
    )
    assert second["content_factory_base_prompt"].count(
        "ACOUSTIC VOICE REPAIR"
    ) == 1


def test_completed_asset_identity_changes_when_same_task_is_retried():
    old_meta = {
        "source_task_ids": [2724, 2725],
        "source_file_ids": [9001, 9002],
    }

    assert _completed_asset_matches_segment_results(
        old_meta,
        source_task_ids=[2724, 2725],
        source_file_ids=[9001, 9002],
    )
    assert not _completed_asset_matches_segment_results(
        old_meta,
        source_task_ids=[2724, 2725],
        source_file_ids=[9010, 9002],
    )
