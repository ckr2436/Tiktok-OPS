"""Deterministic source-versus-result evidence for bounded video edits."""

from __future__ import annotations

import hashlib
from pathlib import Path
import math
import subprocess
from typing import Any


FFMPEG_BIN = "/opt/apps/bin/ffmpeg"


def _decoded_audio_sha256(path: Path) -> str:
    result = subprocess.run(
        [
            FFMPEG_BIN,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0 or not result.stdout:
        return ""
    return hashlib.sha256(result.stdout).hexdigest()


def _gray_frame(path: Path, seconds: float) -> bytes:
    result = subprocess.run(
        [
            FFMPEG_BIN,
            "-v",
            "error",
            "-ss",
            f"{max(0.0, seconds):.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=64:64:force_original_aspect_ratio=decrease,"
            "pad=64:64:(ow-iw)/2:(oh-ih)/2,format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    return result.stdout if result.returncode == 0 else b""


def _frame_mae(left: bytes, right: bytes) -> float:
    if not left or len(left) != len(right):
        return 255.0
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def _inside_window(value: float, windows: list[tuple[float, float]]) -> bool:
    return any(start <= value <= end for start, end in windows)


def audit_bounded_source_edit(
    *,
    source: Path,
    result: Path,
    contract: dict[str, Any],
    source_duration_seconds: float,
    result_duration_seconds: float,
) -> dict[str, Any]:
    """Prove protected audio/duration and sample visuals outside edit windows."""
    fidelity = str(contract.get("fidelity") or "").strip().lower()
    if fidelity not in {"exact", "exact_outside_authorized_changes"}:
        return {
            "schema_version": "source-diff-v1",
            "status": "NOT_REQUIRED",
            "fidelity": fidelity,
        }
    windows: list[tuple[float, float]] = []
    for raw in list(contract.get("authorized_changes") or []):
        if not isinstance(raw, dict):
            continue
        start = raw.get("start_seconds")
        end = raw.get("end_seconds")
        if start is None or end is None:
            continue
        windows.append((max(0.0, float(start)), max(0.0, float(end))))

    failures: list[str] = []
    duration_delta = abs(source_duration_seconds - result_duration_seconds)
    if duration_delta > 0.12:
        failures.append(
            f"protected duration changed by {duration_delta:.3f} seconds"
        )
    source_audio = _decoded_audio_sha256(source)
    result_audio = _decoded_audio_sha256(result)
    if not source_audio or not result_audio or source_audio != result_audio:
        failures.append("protected decoded audio differs from the source")

    usable_duration = min(source_duration_seconds, result_duration_seconds)
    sample_times: list[float] = []
    cursor = 0.5
    step = max(1.0, usable_duration / 16.0)
    while cursor < max(0.5, usable_duration - 0.25) and len(sample_times) < 24:
        if not _inside_window(cursor, windows):
            sample_times.append(round(cursor, 3))
        cursor += step
    samples: list[dict[str, float]] = []
    for sample_time in sample_times:
        mae = _frame_mae(
            _gray_frame(source, sample_time),
            _gray_frame(result, sample_time),
        )
        samples.append({"seconds": sample_time, "mae": round(mae, 3)})
    # 64x64 luminance MAE stays below 0.3 for an ordinary H.264 re-encode in
    # our regression corpus.  1.0 still leaves ample codec headroom while
    # catching a changed inset, caption block, actor, or shot outside the
    # authorized window instead of averaging that local change away.
    violating = [item for item in samples if item["mae"] > 1.0]
    if violating:
        failures.append(
            "protected visual samples changed outside authorized windows: "
            + ", ".join(
                f"{item['seconds']:.3f}s(mae={item['mae']:.3f})"
                for item in violating[:8]
            )
        )
    return {
        "schema_version": "source-diff-v1",
        "status": "PASS" if not failures else "FAIL",
        "fidelity": fidelity,
        "authorized_windows_seconds": windows,
        "source_duration_seconds": source_duration_seconds,
        "result_duration_seconds": result_duration_seconds,
        "duration_delta_seconds": duration_delta,
        "decoded_audio_identical": bool(
            source_audio and source_audio == result_audio
        ),
        "visual_samples": samples,
        "failures": failures,
    }


def audit_regenerated_source_originality(
    *,
    source: Path,
    result: Path,
    contract: dict[str, Any],
    source_duration_seconds: float,
    result_duration_seconds: float,
) -> dict[str, Any]:
    """Reject source-media reuse when only semantic structure may transfer."""
    transfer_mode = str(contract.get("transfer_mode") or "").strip().lower()
    reuse = str(contract.get("source_media_reuse") or "").strip().lower()
    if reuse != "forbidden":
        return {
            "schema_version": "source-originality-v1",
            "status": "NOT_REQUIRED",
            "transfer_mode": transfer_mode,
            "source_media_reuse": reuse,
        }

    failures: list[str] = []
    source_audio = _decoded_audio_sha256(source)
    result_audio = _decoded_audio_sha256(result)
    decoded_audio_identical = bool(
        source_audio and source_audio == result_audio
    )
    if decoded_audio_identical:
        failures.append(
            "source audio was reused although source_media_reuse is forbidden"
        )

    fractions = [0.05 + index * 0.10 for index in range(10)]
    samples: list[dict[str, float]] = []
    for fraction in fractions:
        source_time = max(
            0.0,
            min(source_duration_seconds - 0.05, source_duration_seconds * fraction),
        )
        result_time = max(
            0.0,
            min(result_duration_seconds - 0.05, result_duration_seconds * fraction),
        )
        mae = _frame_mae(
            _gray_frame(source, source_time),
            _gray_frame(result, result_time),
        )
        samples.append(
            {
                "fraction": round(fraction, 3),
                "source_seconds": round(source_time, 3),
                "result_seconds": round(result_time, 3),
                "mae": round(mae, 3),
            }
        )
    near_duplicates = [item for item in samples if item["mae"] <= 1.0]
    allowed_near_duplicates = max(1, math.floor(len(samples) * 0.10))
    if len(near_duplicates) > allowed_near_duplicates:
        failures.append(
            "source video frames were reused although only semantic structure "
            "may transfer: "
            + ", ".join(
                f"{item['fraction']:.2f}(mae={item['mae']:.3f})"
                for item in near_duplicates[:8]
            )
        )
    return {
        "schema_version": "source-originality-v1",
        "status": "PASS" if not failures else "FAIL",
        "transfer_mode": transfer_mode,
        "source_media_reuse": reuse,
        "decoded_audio_identical": decoded_audio_identical,
        "near_duplicate_sample_count": len(near_duplicates),
        "allowed_near_duplicate_sample_count": allowed_near_duplicates,
        "visual_samples": samples,
        "excluded_source_artifacts": list(
            contract.get("excluded_source_artifacts") or []
        ),
        "failures": failures,
    }


__all__ = [
    "audit_bounded_source_edit",
    "audit_regenerated_source_originality",
]
