from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

from app.tasks.hermes_agent import content_factory_tasks
from app.services.hermes_agent.content_source_diff import (
    audit_bounded_source_edit,
    audit_regenerated_source_originality,
)


FFMPEG = "/opt/apps/bin/ffmpeg"


def _source(path: Path) -> None:
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=180x320:r=12:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _bounded_overlay(source: Path, target: Path, *, end_seconds: float) -> None:
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            str(source),
            "-vf",
            (
                "drawbox=x=10:y=10:w=60:h=60:color=red:t=fill:"
                f"enable=between(t\\,0\\,{end_seconds})"
            ),
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-c:a",
            "copy",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _regenerated(target: Path) -> None:
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=180x320:r=12:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=16000:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def test_bounded_source_diff_passes_only_when_changes_stay_in_window(tmp_path):
    source = tmp_path / "source.mp4"
    valid = tmp_path / "valid.mp4"
    invalid = tmp_path / "invalid.mp4"
    _source(source)
    _bounded_overlay(source, valid, end_seconds=0.8)
    _bounded_overlay(source, invalid, end_seconds=2.5)
    contract = {
        "fidelity": "exact_outside_authorized_changes",
        "authorized_changes": [{"start_seconds": 0, "end_seconds": 1}],
    }

    valid_report = audit_bounded_source_edit(
        source=source,
        result=valid,
        contract=contract,
        source_duration_seconds=3.0,
        result_duration_seconds=3.0,
    )
    invalid_report = audit_bounded_source_edit(
        source=source,
        result=invalid,
        contract=contract,
        source_duration_seconds=3.0,
        result_duration_seconds=3.0,
    )

    assert valid_report["status"] == "PASS"
    assert valid_report["decoded_audio_identical"] is True
    assert invalid_report["status"] == "FAIL"
    assert "protected visual samples changed" in " ".join(
        invalid_report["failures"]
    )


def test_regenerated_source_originality_rejects_copied_media(tmp_path):
    source = tmp_path / "source.mp4"
    copied = tmp_path / "copied.mp4"
    regenerated = tmp_path / "regenerated.mp4"
    _source(source)
    _bounded_overlay(source, copied, end_seconds=0.5)
    _regenerated(regenerated)
    contract = {
        "transfer_mode": "semantic_structure",
        "source_media_reuse": "forbidden",
        "excluded_source_artifacts": ["embedded caption bands"],
    }

    copied_report = audit_regenerated_source_originality(
        source=source,
        result=copied,
        contract=contract,
        source_duration_seconds=3.0,
        result_duration_seconds=3.0,
    )
    regenerated_report = audit_regenerated_source_originality(
        source=source,
        result=regenerated,
        contract=contract,
        source_duration_seconds=3.0,
        result_duration_seconds=3.0,
    )

    assert copied_report["status"] == "FAIL"
    assert copied_report["decoded_audio_identical"] is True
    assert copied_report["near_duplicate_sample_count"] > 1
    assert regenerated_report["status"] == "PASS"
    assert regenerated_report["decoded_audio_identical"] is False
    assert regenerated_report["near_duplicate_sample_count"] == 0


class _EmptyAssetQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return []


class _EmptyAssetDb:
    def query(self, *_args, **_kwargs):
        return _EmptyAssetQuery()


def test_full_regeneration_without_local_source_passes_by_asset_isolation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        content_factory_tasks,
        "_project_transformation_contract",
        lambda _project: {
            "fidelity": "adaptive",
            "execution_strategy": "full_regeneration",
            "source_media_reuse": "forbidden",
            "excluded_source_artifacts": ["source story and source audio"],
        },
    )

    report = content_factory_tasks._source_transformation_quality_report(
        _EmptyAssetDb(),
        SimpleNamespace(id=1, workspace_id=2),
        tmp_path / "result.mp4",
    )

    assert report["status"] == "PASS"
    assert report["comparison_mode"] == "asset_isolation_no_local_source"
    assert report["source_media_available"] is False
    assert report["media_reuse_possible"] is False


def test_exact_transformation_without_local_source_still_fails_closed(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        content_factory_tasks,
        "_project_transformation_contract",
        lambda _project: {
            "fidelity": "exact_outside_authorized_changes",
            "execution_strategy": "local_edit",
            "source_media_reuse": "required",
        },
    )

    report = content_factory_tasks._source_transformation_quality_report(
        _EmptyAssetDb(),
        SimpleNamespace(id=1, workspace_id=2),
        tmp_path / "result.mp4",
    )

    assert report["status"] == "FAIL"
    assert "authoritative source video is unavailable" in report["failures"][0]
