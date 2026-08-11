from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
load_dotenv(BACKEND_ROOT / ".env", override=False)

from sqlalchemy import inspect

from app.data.db import SessionLocal
from app.data.models.hermes_agent import (
    HermesContentDeliverable,
    HermesContentExecution,
    HermesContentFactoryAsset,
    HermesContentFactoryProject,
)
from app.data.models.kie_api import KieTask


def _asset_index(asset: HermesContentFactoryAsset) -> int:
    meta = dict(asset.meta_json or {})
    for key in (
        "content_factory_video_index",
        "content_factory_variant_index",
        "video_index",
        "variant_index",
    ):
        try:
            value = int(meta.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _available_file(asset: HermesContentFactoryAsset) -> bool:
    try:
        path = Path(str(asset.file_path or ""))
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _process_lines(pattern: str) -> list[str]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,comm=,args="],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return []
    expected = str(pattern or "").strip()
    matches: list[str] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        command_name = Path(parts[1]).name
        first_arg = (
            Path(parts[2].split(None, 1)[0]).name
            if len(parts) > 2 and parts[2].strip()
            else ""
        )
        if command_name == expected or first_arg == expected:
            matches.append(line)
    return matches


def audit_project(
    project_id: int,
    *,
    require_complete: bool,
    check_browser_idle: bool,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        tables = set(inspect(db.get_bind()).get_table_names())
        if "hermes_content_factory_projects" not in tables:
            raise SystemExit(
                "configured database has no content-factory tables; refusing "
                "to audit a default or wrong database"
            )
        project = db.get(HermesContentFactoryProject, int(project_id))
        if project is None:
            raise SystemExit(f"project {project_id} does not exist")
        config = dict(project.config_json or {})
        target = max(1, int(config.get("video_count") or 1))
        rows = db.query(HermesContentFactoryAsset).filter(
            HermesContentFactoryAsset.project_id == int(project.id),
            HermesContentFactoryAsset.kind.in_(("video", "edit_guidance")),
        ).all()
        videos: dict[int, list[HermesContentFactoryAsset]] = {}
        guides: dict[int, list[HermesContentFactoryAsset]] = {}
        for asset in rows:
            index = _asset_index(asset)
            if index <= 0 or not _available_file(asset):
                continue
            target_map = videos if asset.kind == "video" else guides
            target_map.setdefault(index, []).append(asset)

        failures: list[str] = []
        duplicate_video_indices = sorted(
            index for index, values in videos.items() if len(values) != 1
        )
        duplicate_guide_indices = sorted(
            index for index, values in guides.items() if len(values) != 1
        )
        if duplicate_video_indices:
            failures.append(f"duplicate video ordinals: {duplicate_video_indices}")
        if duplicate_guide_indices:
            failures.append(f"duplicate guide ordinals: {duplicate_guide_indices}")
        missing_guides = sorted(set(videos) - set(guides))
        orphan_guides = sorted(set(guides) - set(videos))
        if missing_guides:
            failures.append(f"completed videos without guides: {missing_guides}")
        if orphan_guides:
            failures.append(f"guides without completed videos: {orphan_guides}")
        completed_gap_indices = (
            sorted(set(range(1, max(videos) + 1)) - set(videos))
            if videos
            else []
        )
        if completed_gap_indices:
            failures.append(
                "user-facing deliverable ordinal gaps: "
                f"{completed_gap_indices}"
            )
        expected = set(range(1, target + 1))
        if require_complete and set(videos) != expected:
            failures.append(
                f"target mismatch: expected={target} videos actual={len(videos)} "
                f"missing={sorted(expected - set(videos))}"
            )
        if require_complete and (
            str(project.status or "").lower() != "complete"
            or str(project.current_stage or "").upper() != "COMPLETE"
        ):
            failures.append(
                "project is not durably complete: "
                f"status={project.status} stage={project.current_stage}"
            )

        for index, assets in sorted(videos.items()):
            for asset in assets:
                meta = dict(asset.meta_json or {})
                final_quality = dict(meta.get("final_quality") or {})
                if str(final_quality.get("status") or "").upper() != "PASS":
                    failures.append(
                        f"video {index} lacks passing final-quality evidence"
                    )
                segment_plan = [
                    dict(item)
                    for item in list(meta.get("segment_plan") or [])
                    if isinstance(item, dict)
                ]
                spoken = any(
                    bool(list(segment.get("dialogue_lines") or []))
                    for segment in segment_plan
                )
                if spoken and str(
                    dict(meta.get("voice_continuity") or {}).get("status")
                    or ""
                ).upper() != "PASS":
                    failures.append(
                        f"video {index} lacks passing voice-continuity evidence"
                    )

        for index, assets in sorted(guides.items()):
            for asset in assets:
                meta = dict(asset.meta_json or {})
                if not str(meta.get("publish_title") or "").strip():
                    failures.append(f"guide {index} has no publish title")
                hashtags = list(meta.get("hashtags") or [])
                if len(hashtags) > 5:
                    failures.append(
                        f"guide {index} exceeds five hashtags: {len(hashtags)}"
                    )

        if require_complete:
            pipeline = dict(
                dict(project.state_json or {}).get("video_variant_pipeline")
                or {}
            )
            stale_missing = sorted({
                int(value)
                for value in list(
                    pipeline.get("completion_blocked_missing_indices") or []
                )
                if str(value).strip().isdigit()
            })
            if stale_missing:
                failures.append(
                    "completed project retains stale missing-variant markers: "
                    f"{stale_missing}"
                )

        provider_tasks = []
        for task in db.query(KieTask).filter(
            KieTask.workspace_id == int(project.workspace_id)
        ).all():
            params = dict(task.input_json or {})
            if int(params.get("content_factory_project_id") or 0) == int(project.id):
                provider_tasks.append(task)
        logical_tasks: dict[tuple[str, int, int], list[KieTask]] = {}
        for task in provider_tasks:
            params = dict(task.input_json or {})
            manifest = str(
                params.get("content_factory_media_manifest_sha256") or ""
            )
            if not manifest:
                continue
            key = (
                manifest,
                int(params.get("content_factory_variant_index") or 0),
                int(params.get("content_factory_segment_index") or 0),
            )
            logical_tasks.setdefault(key, []).append(task)
        duplicate_provider_submissions = []
        for key, values in logical_tasks.items():
            provider_visible = [
                task
                for task in values
                if str(task.state or "").lower()
                not in {"queued_local", "waiting_dependency"}
                and str(task.fail_code or "").lower()
                not in {"cf_variant_superseded", "superseded"}
            ]
            if len(provider_visible) > 1:
                duplicate_provider_submissions.append({
                    "logical_key": key,
                    "task_ids": [int(task.id) for task in provider_visible],
                    "states": [str(task.state) for task in provider_visible],
                })
        if duplicate_provider_submissions:
            failures.append(
                "duplicate logical provider submissions: "
                + json.dumps(duplicate_provider_submissions, default=str)
            )

        execution_report: dict[str, Any] = {"available": False}
        if "hermes_content_executions" in tables:
            execution = db.query(HermesContentExecution).filter(
                HermesContentExecution.project_id == int(project.id)
            ).order_by(HermesContentExecution.id.desc()).first()
            if execution is not None:
                deliverables = db.query(HermesContentDeliverable).filter(
                    HermesContentDeliverable.execution_id == int(execution.id),
                    HermesContentDeliverable.status == "ready",
                ).all()
                ledger_videos = {
                    int(item.deliverable_ordinal)
                    for item in deliverables
                    if item.kind == "video" and Path(item.file_path).is_file()
                }
                ledger_guides = {
                    int(item.deliverable_ordinal)
                    for item in deliverables
                    if item.kind == "edit_guide" and Path(item.file_path).is_file()
                }
                execution_report = {
                    "available": True,
                    "execution_id": int(execution.id),
                    "status": execution.status,
                    "target_count": int(execution.target_count),
                    "video_ordinals": sorted(ledger_videos),
                    "guide_ordinals": sorted(ledger_guides),
                }
                new_video_indices = {
                    index
                    for index, assets in videos.items()
                    if any(
                        str(dict(asset.meta_json or {}).get(
                            "media_manifest_sha256"
                        ) or "")
                        for asset in assets
                    )
                }
                if not new_video_indices.issubset(ledger_videos):
                    failures.append(
                        "frozen-manifest videos missing from execution ledger: "
                        f"{sorted(new_video_indices - ledger_videos)}"
                    )
        browser_processes = _process_lines("agent-browser") if check_browser_idle else []
        if check_browser_idle and browser_processes:
            failures.append(
                f"agent-browser processes are active during API-only audit: {len(browser_processes)}"
            )

        return {
            "schema_version": "content-production-audit-v1",
            "status": "PASS" if not failures else "FAIL",
            "project": {
                "id": int(project.id),
                "project_key": project.project_key,
                "status": project.status,
                "current_stage": project.current_stage,
                "target_count": target,
            },
            "deliverables": {
                "video_count": len(videos),
                "guide_count": len(guides),
                "video_ordinals": sorted(videos),
                "guide_ordinals": sorted(guides),
            },
            "provider": {
                "task_count": len(provider_tasks),
                "frozen_logical_task_count": len(logical_tasks),
                "duplicate_logical_submissions": duplicate_provider_submissions,
            },
            "execution_ledger": execution_report,
            "agent_browser_process_count": len(browser_processes),
            "failures": failures,
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id", type=int)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--check-browser-idle", action="store_true")
    args = parser.parse_args()
    report = audit_project(
        args.project_id,
        require_complete=bool(args.require_complete),
        check_browser_idle=bool(args.check_browser_idle),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
