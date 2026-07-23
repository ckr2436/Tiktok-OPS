from __future__ import annotations

import os
import re
import json
import base64
import fcntl
import hashlib
import hmac
import shutil
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from urllib.parse import urlparse

import httpx
from fastapi import UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.core.config import settings
from app.data.models.hermes_agent import (
    HermesBrowserBridge,
    HermesContentDirectorArtifact,
    HermesContentFactoryAsset,
    HermesContentFactoryProject,
    HermesContentFactoryStage,
    HermesContentProductionPlanAudit,
    HermesContentSeriesSlate,
    HermesContentProduct,
    HermesContentProductAsset,
    HermesContentSegmentRun,
)
from app.data.models.kie_api import KieTask
from app.services.kie_api.accounts import (
    BANDIANWA_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
    has_active_key,
    normalize_video_model_id,
)
from app.services.hermes_agent.stage_routing import stage_execution_backend
from app.services.hermes_agent.content_rollout_gate import (
    parse_variant_rollout_gate,
)
from app.services.hermes_agent.video_model_capabilities import (
    resolve_project_variant_parallelism,
)


STAGE_ORDER = [
    "FACTS",
    "SERIES_DIRECTOR",
    "DIRECTOR",
    "PRODUCTION_PLAN",
    "VISUAL_PREVIEW",
    "CREATIVE_REVIEW",
    "FINAL_ASSETS",
    "VIDEO_PROMPTS",
    "EDIT_PACKAGE",
    "COMPLETE",
]
WAITING_STAGES = {"WAITING_VIDEO_INPUT"}
RESTARTABLE_STAGES = {
    "SERIES_DIRECTOR",
    "DIRECTOR",
    "PRODUCTION_PLAN",
    "VISUAL_PREVIEW",
    "CREATIVE_REVIEW",
    "FINAL_ASSETS",
    "VIDEO_PROMPTS",
    "EDIT_PACKAGE",
}
VARIANT_RESTART_STAGES = {
    "DIRECTOR",
    "PRODUCTION_PLAN",
    "VISUAL_PREVIEW",
    "CREATIVE_REVIEW",
    "FINAL_ASSETS",
    "VIDEO_PROMPTS",
    "EDIT_PACKAGE",
}
STORAGE_ROOT = Path(
    os.getenv("CONTENT_FACTORY_STORAGE_ROOT", "/data/gmv_ops/hermes_content_factory")
).expanduser()
BROWSER_INBOX = STORAGE_ROOT / "browser_inbox"
MAX_PRODUCT_ASSET_BYTES = 100 * 1024 * 1024
MAX_PROJECT_SOURCE_BYTES = 100 * 1024 * 1024
MAX_REFERENCE_VIDEO_BYTES = 200 * 1024 * 1024
MAX_CHARACTER_REFERENCE_BYTES = 15 * 1024 * 1024
CONTENT_PROVIDER_TERMINAL_STATES = frozenset({
    "success",
    "succeeded",
    "failed",
    "fail",
    "error",
    "timeout",
    "cancelled",
    "canceled",
    "superseded",
    "downloaded",
})
WINDOWS_INBOX = os.getenv("HERMES_DEFAULT_WINDOWS_INBOX", r"%LOCALAPPDATA%\MYUPONA\HermesInbox")
HERMES_QUEUE_BASE = str(settings.HERMES_AGENT_TASK_QUEUE)
SELF_HEAL_POLICY_VERSION = 69
BRIDGE_DEFAULT_TTL_SECONDS = 90
BRIDGE_LEASE_HOURS = 6
BRIDGE_PORT_START = int(os.getenv("HERMES_BRIDGE_PORT_START", "9322"))
BRIDGE_PORT_END = int(os.getenv("HERMES_BRIDGE_PORT_END", "9422"))
BRIDGE_LOCAL_CDP_PORT = int(os.getenv("HERMES_BRIDGE_LOCAL_CDP_PORT", "9222"))
BRIDGE_SSH_TARGET = os.getenv("HERMES_BRIDGE_SSH_TARGET", "root@192.168.1.2")
BRIDGE_AGENT_BINARY = Path(os.getenv("HERMES_BRIDGE_AGENT_BINARY", "/opt/gmv/GMV-OPS/backend/assets/MYUPONA-HermesBridge.exe"))
BRIDGE_AGENT_VERSION = os.getenv("HERMES_BRIDGE_AGENT_VERSION", "2026.07.18.2")
BRIDGE_AGENT_SSH_HOST = os.getenv("HERMES_BRIDGE_AGENT_SSH_HOST", "192.168.1.2")
BRIDGE_AGENT_SSH_USER = os.getenv("HERMES_BRIDGE_AGENT_SSH_USER", "gmv")
BRIDGE_AGENT_SSH_PORT = int(os.getenv("HERMES_BRIDGE_AGENT_SSH_PORT", "22"))
BRIDGE_AGENT_AUTHORIZED_KEYS = Path(os.getenv("HERMES_BRIDGE_AGENT_AUTHORIZED_KEYS", "/opt/gmv/.ssh/authorized_keys"))
BRIDGE_AGENT_SESSION_GUARD = Path(
    os.getenv("HERMES_BRIDGE_AGENT_SESSION_GUARD", "/opt/gmv/bin/hermes-bridge-session-guard")
)
BRIDGE_AGENT_CONFIG_MARKER = b"\nMYUPONA_BRIDGE_AGENT_CONFIG_V1\n"
BRIDGE_AGENT_TOKEN_TTL_DAYS = int(os.getenv("HERMES_BRIDGE_AGENT_TOKEN_TTL_DAYS", "365"))


def _ensure_storage_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    # ``mkdir(parents=True)`` applies our chmod only to the leaf. When an
    # operator creates the first project as root under a setgid tree, an
    # intermediate project directory can inherit a restrictive 02700 umask.
    # The leaf assets directory then looks correct while the API user cannot
    # traverse its parent. Normalize every repository-owned component, never
    # anything above STORAGE_ROOT.
    try:
        relative = path.relative_to(STORAGE_ROOT)
    except ValueError:
        candidates = [path]
    else:
        candidates = [STORAGE_ROOT]
        current = STORAGE_ROOT
        for component in relative.parts:
            current = current / component
            candidates.append(current)
    for candidate in candidates:
        try:
            candidate.chmod(0o2775)
        except OSError:
            pass


def _mark_group_writable(path: Path) -> None:
    try:
        path.chmod(0o664)
    except OSError:
        pass


def _stored_file_available(value: Any) -> bool:
    """A broken or unreadable asset must not take down a project listing."""

    try:
        return Path(str(value or "")).is_file()
    except OSError:
        return False


def _now() -> datetime:
    return datetime.now()


def _project_active_variant_index(project: HermesContentFactoryProject) -> int:
    state = dict(project.state_json or {})
    pipeline = dict(state.get("video_variant_pipeline") or {})
    return max(
        1,
        int(
            pipeline.get("active_index")
            or state.get("active_variant_index")
            or 1
        ),
    )


def _stage_variant_index(
    stage: HermesContentFactoryStage,
    *,
    fallback: int = 1,
) -> int:
    stage_input = dict(stage.input_json or {})
    stage_output = dict(stage.output_json or {})
    evidence = dict(stage_output.get("evidence") or {})
    result = dict(stage_output.get("result") or {})
    for value in (
        stage_input.get("variant_index"),
        stage_output.get("content_factory_variant_index"),
        evidence.get("content_factory_variant_index"),
        evidence.get("variant_index"),
        result.get("content_factory_variant_index"),
        result.get("variant_index"),
    ):
        try:
            index = int(value or 0)
        except (TypeError, ValueError):
            index = 0
        if index > 0:
            return index
    return max(1, int(fallback or 1))


def _asset_variant_index(
    asset: HermesContentFactoryAsset,
    *,
    fallback: int = 1,
) -> int:
    meta = dict(asset.meta_json or {})
    for value in (
        meta.get("content_factory_variant_index"),
        meta.get("content_factory_video_index"),
        meta.get("variant_index"),
        meta.get("video_index"),
    ):
        try:
            index = int(value or 0)
        except (TypeError, ValueError):
            index = 0
        if index > 0:
            return index
    return max(1, int(fallback or 1))


def _is_resumable_visual_board(raw_board: Any) -> bool:
    if not isinstance(raw_board, dict):
        return False
    board = dict(raw_board)
    status = str(board.get("status") or "").strip().lower()
    output_path = str(board.get("output_path") or "").strip()
    if status == "completed":
        return bool(output_path and Path(output_path).is_file())
    generated_scene_source = str(
        board.get("generated_scene_source_path") or ""
    ).strip()
    if (
        status == "failed"
        and str(board.get("failure_class") or "").strip().lower()
        == "product_scene_unplaceable"
    ):
        return bool(
            generated_scene_source
            and Path(generated_scene_source).is_file()
        )
    return bool(
        status in {"queued", "submitted", "running", "processing"}
        and str(board.get("task_id") or "").strip()
        and str(board.get("prompt_digest") or "").strip()
    )


def _resumable_visual_api_checkpoint(
    stage: HermesContentFactoryStage | None,
) -> dict[str, Any]:
    """Return durable completed or unambiguous in-flight image boards.

    A manual pause may land after one provider result has been downloaded but
    before the remaining references are submitted.  Those completed files are
    already paid-for project assets even though the stage itself is not yet
    successful.  A submitted provider task is also safe to resume when both
    its durable task id and exact prompt digest are present: the successor
    delivery revalidates that digest before polling it, and clears the task id
    if the prompt changed. A failed product-placement analysis is also
    resumable when its paid product-free scene is still local; a successor
    placement policy can re-analyze those exact pixels before another image
    purchase. Other failed, ambiguous, or missing-file rows are rebuilt.
    """
    if stage is None or str(stage.stage or "").upper() != "VISUAL_PREVIEW":
        return {}
    stage_input = dict(stage.input_json or {})
    visual_api = dict(stage_input.get("visual_api") or {})
    completed_boards: dict[str, dict[str, Any]] = {}
    for raw_index, raw_board in dict(visual_api.get("boards") or {}).items():
        if not isinstance(raw_board, dict):
            continue
        board = dict(raw_board)
        if not _is_resumable_visual_board(board):
            continue
        try:
            board_index = max(1, int(raw_index))
        except (TypeError, ValueError):
            continue
        completed_boards[str(board_index)] = board
    if not completed_boards:
        return {}

    checkpoint = {
        key: value
        for key, value in visual_api.items()
        if key not in {
            "boards",
            "status",
            "last_error",
            "task_id",
            "retry_after",
            "provider_retry_generation",
            "account_quota_exhausted",
            "account_quota_exhausted_at",
            "bandianwa_account_quota_exhausted",
            "provider_failures",
        }
    }
    checkpoint["boards"] = completed_boards
    checkpoint["status"] = "partial_resumable"
    # Provider-account exhaustion belongs to the failed delivery attempt, not
    # to the paid image files. Carrying it into the successor would skip the
    # durable boards or force a browser fallback before local post-processing
    # can be retried.
    checkpoint["provider_retry_generation"] = 0
    checkpoint["account_quota_exhausted"] = False
    checkpoint["completed_board_count"] = len(completed_boards)
    return {
        "source_stage_id": int(stage.id),
        "source_instruction": str(stage.instruction or "").strip() or None,
        "variant_index": int(stage_input.get("variant_index") or 1),
        "api_route": str(stage_input.get("api_route") or "").strip() or None,
        "visual_api": checkpoint,
    }


def _latest_resumable_visual_api_checkpoint(
    db: Session,
    project: HermesContentFactoryProject,
    paused_current_stage: HermesContentFactoryStage | None,
) -> dict[str, Any]:
    """Find the latest same-variant paid image checkpoint across fallbacks.

    A visual API delivery may finish every image and then fail during a
    non-generative placement/verification call. The fallback successor has no
    ``visual_api`` payload of its own, so inspecting only the paused current
    row loses the already-paid downloads and regenerates them. Search backward
    within the active variant and reuse only boards whose local files or exact
    in-flight identities remain durable.
    """
    direct = _resumable_visual_api_checkpoint(paused_current_stage)
    if direct:
        return direct
    if str(project.current_stage or "").upper() not in {
        "VISUAL_PREVIEW",
        "PRODUCTION_PLAN",
    }:
        return {}

    active_variant = _project_active_variant_index(project)
    upper_stage_id = int(paused_current_stage.id) if paused_current_stage else None
    query = db.query(HermesContentFactoryStage).filter(
        HermesContentFactoryStage.project_id == int(project.id),
        HermesContentFactoryStage.stage == "VISUAL_PREVIEW",
    )
    if upper_stage_id is not None:
        query = query.filter(HermesContentFactoryStage.id < upper_stage_id)
    for candidate in query.order_by(HermesContentFactoryStage.id.desc()).limit(20):
        if _stage_variant_index(candidate, fallback=active_variant) != active_variant:
            continue
        checkpoint = _resumable_visual_api_checkpoint(candidate)
        if checkpoint:
            checkpoint["recovered_across_fallback"] = True
            checkpoint["fallback_stage_id"] = upper_stage_id
            return checkpoint
    return {}


def _restore_visual_resume_instruction(
    stage: HermesContentFactoryStage,
    pending_visual_resume: dict[str, Any],
    stage_input: dict[str, Any],
) -> dict[str, Any]:
    """Keep an in-flight image request's exact prompt across manual resume.

    An empty source instruction is still authoritative. Leaving an operator's
    resume note on the successor stage changes the image prompt digest, clears
    the durable provider task id, and submits the same paid image twice.
    Operator text is retained only as audit metadata.
    """
    normalized = dict(stage_input or {})
    if "source_instruction" not in pending_visual_resume:
        return normalized
    operator_instruction = str(stage.instruction or "").strip()
    source_instruction = str(
        pending_visual_resume.get("source_instruction") or ""
    ).strip()
    stage.instruction = source_instruction or None
    if operator_instruction != source_instruction:
        normalized["resume_operator_instruction"] = (
            operator_instruction[:4000] or None
        )
    return normalized


def resume_stage_force_browser(
    paused_stage_input: dict[str, Any] | None,
    resumed_project_state: dict[str, Any] | None,
) -> bool:
    """Honor a browser fallback unless a durable API checkpoint supersedes it."""
    pending = dict(
        dict(resumed_project_state or {}).get("pending_visual_api_resume") or {}
    )
    visual_api = dict(pending.get("visual_api") or {})
    if any(
        _is_resumable_visual_board(board)
        for board in dict(visual_api.get("boards") or {}).values()
    ):
        return False
    paused = dict(paused_stage_input or {})
    return bool(
        paused.get("api_fallback_to_browser")
        or paused.get("visual_api_force_browser_fallback")
        or str(paused.get("execution_backend") or "").strip().lower() == "browser"
    )


def _safe_revoke_task(task_id: str | None, *, terminate: bool = False) -> bool:
    value = str(task_id or "").strip()
    if not value:
        return False
    try:
        from app.celery_app import celery_app

        celery_app.control.revoke(value, terminate=terminate)
        return True
    except Exception:
        return False


def _bridge_ttl_seconds() -> int:
    try:
        return max(30, min(600, int(os.getenv("HERMES_BRIDGE_TTL_SECONDS", str(BRIDGE_DEFAULT_TTL_SECONDS)))))
    except ValueError:
        return BRIDGE_DEFAULT_TTL_SECONDS


def _bridge_alive_cutoff() -> datetime:
    return _now() - timedelta(seconds=_bridge_ttl_seconds())


def _parse_bridge_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _bridge_agent_last_heartbeat_at(bridge: HermesBrowserBridge) -> datetime | None:
    meta = dict(bridge.meta_json or {})
    return _parse_bridge_timestamp(meta.get("agent_last_heartbeat_at"))


def _bridge_agent_recent(bridge: HermesBrowserBridge) -> bool:
    seen_at = _bridge_agent_last_heartbeat_at(bridge)
    return bool(seen_at and seen_at >= _bridge_alive_cutoff())


def _server_load_snapshot() -> dict[str, Any]:
    cpu_count = max(1, os.cpu_count() or 1)
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    try:
        configured_capacity = int(os.getenv("HERMES_DYNAMIC_BROWSER_MAX_ACTIVE", "0"))
    except ValueError:
        configured_capacity = 0
    if configured_capacity > 0:
        capacity = configured_capacity
    else:
        pressure = load1 / max(1, cpu_count)
        if pressure >= 1.5:
            capacity = 1
        elif pressure >= 1.0:
            capacity = max(1, min(4, cpu_count // 2 or 1))
        else:
            capacity = max(1, min(8, cpu_count))
    memory_available_mb = 0
    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            meminfo[key] = int(raw.strip().split()[0])
        memory_available_mb = int(meminfo.get("MemAvailable", 0)) // 1024
    except (OSError, ValueError, IndexError):
        memory_available_mb = 0
    if memory_available_mb:
        # Keep 2 GiB for the API/database and budget 512 MiB for each
        # browser-driving worker. CPU and memory must both admit a new slot.
        memory_capacity = max(1, (memory_available_mb - 2048) // 512)
        capacity = min(int(capacity), int(memory_capacity))
    return {
        "cpu_count": cpu_count,
        "load1": round(float(load1), 2),
        "load5": round(float(load5), 2),
        "load15": round(float(load15), 2),
        "memory_available_mb": int(memory_available_mb),
        "capacity": max(1, min(32, int(capacity))),
    }


def _bridge_recently_degraded(bridge: HermesBrowserBridge) -> bool:
    meta = dict(bridge.meta_json or {})
    raw = str(meta.get("last_degraded_at") or "").strip()
    if not raw:
        return False
    try:
        degraded_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    cooldown = int(os.getenv("HERMES_BRIDGE_DEGRADED_COOLDOWN_SECONDS", "600"))
    return degraded_at >= _now() - timedelta(seconds=max(30, cooldown))


def _bridge_degraded_min_seconds() -> int:
    try:
        return max(15, min(300, int(os.getenv("HERMES_BRIDGE_DEGRADED_MIN_SECONDS", "60"))))
    except ValueError:
        return 60


def _bridge_degraded_old_enough_to_probe(bridge: HermesBrowserBridge) -> bool:
    meta = dict(bridge.meta_json or {})
    raw = str(meta.get("last_degraded_at") or "").strip()
    if not raw:
        return True
    try:
        degraded_at = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return degraded_at <= _now() - timedelta(seconds=_bridge_degraded_min_seconds())


def _clear_bridge_degraded_marker(bridge: HermesBrowserBridge) -> None:
    meta = dict(bridge.meta_json or {})
    changed = False
    for key in ("last_degraded_at", "last_degraded_reason", "last_degraded_project_id"):
        if key in meta:
            meta.pop(key, None)
            changed = True
    if changed:
        meta["last_recovered_at"] = _now().isoformat()
        bridge.meta_json = meta


def _bridge_connected(bridge: HermesBrowserBridge) -> bool:
    return (
        str(bridge.status or "").lower() == "active"
        and bridge.last_seen_at is not None
        and bridge.last_seen_at >= _bridge_alive_cutoff()
        and not _bridge_recently_degraded(bridge)
    )


def _agent_slot_mode(bridge: HermesBrowserBridge) -> str:
    """Return the server-requested lifecycle mode for an Agent-managed slot."""
    mode = str(dict(bridge.meta_json or {}).get("agent_slot_mode") or "active").strip().lower()
    return "dormant" if mode == "dormant" else "active"


def _same_slot_restart_grace_seconds() -> int:
    try:
        return max(
            60,
            min(600, int(os.getenv("HERMES_SAME_SLOT_RESTART_GRACE_SECONDS", "180"))),
        )
    except ValueError:
        return 180


def _same_slot_restart_project_id(bridge: HermesBrowserBridge) -> int | None:
    raw = dict(bridge.meta_json or {}).get("same_slot_restart_project_id")
    try:
        project_id = int(raw)
    except (TypeError, ValueError):
        return None
    return project_id if project_id > 0 else None


def _bridge_same_slot_restart_in_grace(
    bridge: HermesBrowserBridge,
    *,
    now: datetime | None = None,
) -> bool:
    """Keep one sticky Agent slot alive while Chrome and SSH restart.

    The Agent intentionally removes the slot from one heartbeat while it stops
    the old Chrome/tunnel pair. That missing report acknowledges the restart;
    it is not evidence that the profile should be retired.
    """
    meta = dict(bridge.meta_json or {})
    requested_at = _parse_bridge_timestamp(meta.get("same_slot_restart_requested_at"))
    current = now or _now()
    return bool(
        requested_at is not None
        and current - timedelta(seconds=_same_slot_restart_grace_seconds())
        <= requested_at
        <= current + timedelta(seconds=30)
        and _same_slot_restart_project_id(bridge) is not None
        and bool(meta.get("agent_managed"))
        and _agent_slot_mode(bridge) == "active"
    )


def _clear_same_slot_restart_marker(bridge: HermesBrowserBridge) -> None:
    meta = dict(bridge.meta_json or {})
    changed = False
    for key in (
        "same_slot_restart_requested_at",
        "same_slot_restart_project_id",
        "same_slot_restart_reason",
    ):
        if key in meta:
            meta.pop(key, None)
            changed = True
    if changed:
        meta["same_slot_restart_completed_at"] = _now().isoformat()
        bridge.meta_json = meta


def _bridge_is_api_video_dormant(
    bridge: HermesBrowserBridge,
    project: HermesContentFactoryProject | None,
    *,
    active_stage: HermesContentFactoryStage | None = None,
) -> bool:
    """A dormant slot is intentional, not a lost CDP connection.

    The project retains the exact local profile and ChatGPT identity while API
    video generation runs.  It is never eligible for another project, yet the
    Windows Agent may stop Chrome and the SSH tunnel until browser fallback is
    explicitly needed again.
    """
    if project is None or int(bridge.active_project_id or 0) != int(project.id or 0):
        return False
    config = dict(project.config_json or {})
    if _agent_slot_mode(bridge) != "dormant" or bool(config.get("manual_paused", False)):
        return False
    # The bounded parallel pipeline can advance the project from
    # WAITING_VIDEO_INPUT into the next variant's Director/visual stages while
    # prior video tasks are still running. Those stages are API-first and do
    # not own Chrome. Looking only at project.current_stage made lease
    # reconciliation call the intentionally sleeping slot "disconnected",
    # overwrite the project with waiting_bridge, and make the Windows Agent
    # repeatedly reopen/close Chrome. The durable authority is the active
    # stage's resolved execution backend.
    if active_stage is not None:
        stage_input = dict(active_stage.input_json or {})
        backend = stage_execution_backend(
            str(active_stage.stage or ""),
            api_route=str(stage_input.get("api_route") or "").strip() or None,
            stage_input=stage_input,
        )
        if backend != "browser":
            return True
    return bool(
        str(project.current_stage or "") == "WAITING_VIDEO_INPUT"
        and str(project.status or "").lower() in {"generating_video", "paused"}
    )


def hibernate_project_browser_slot_for_api_video(
    db: Session,
    *,
    project: HermesContentFactoryProject,
    active_stage: HermesContentFactoryStage | None = None,
) -> bool:
    """Request one controlled Chrome shutdown for API-only project work.

    This changes Agent intent once; it does not release the project lease and
    therefore cannot cause a different Chrome profile to be selected.  The
    next browser stage wakes this exact slot through the normal sticky-slot
    recovery path.
    """
    if active_stage is not None:
        stage_input = dict(active_stage.input_json or {})
        if stage_execution_backend(
            str(active_stage.stage or ""),
            api_route=str(stage_input.get("api_route") or "").strip() or None,
            stage_input=stage_input,
        ) == "browser":
            return False
    elif str(project.current_stage or "") != "WAITING_VIDEO_INPUT":
        return False
    if bool(dict(project.config_json or {}).get("manual_paused", False)):
        return False
    changed = False
    now = _now()
    rows = db.query(HermesBrowserBridge).filter(
        HermesBrowserBridge.active_project_id == int(project.id),
    ).all()
    for bridge in rows:
        meta = dict(bridge.meta_json or {})
        if str(meta.get("agent_slot_mode") or "active").lower() == "dormant":
            continue
        meta["agent_slot_mode"] = "dormant"
        meta["agent_slot_mode_requested_at"] = now.isoformat()
        meta["agent_slot_mode_project_id"] = int(project.id)
        bridge.meta_json = meta
        db.add(bridge)
        changed = True
    if changed:
        state = dict(project.state_json or {})
        state["browser_slot_mode"] = "dormant"
        state["browser_slot_dormant_requested_at"] = now.isoformat()
        project.state_json = state
        db.add(project)
    return changed


def _bridge_is_displayable(bridge: HermesBrowserBridge) -> bool:
    """Hide unused placeholder slots from the UI.

    The Windows bridge agent may pre-create a warm slot before Chrome/SSH has
    connected. That row is useful for the agent handshake, but showing it as a
    red disconnected slot makes users think the system is broken. Active or
    recently-seen slots are still returned, as are occupied slots that need
    operator attention.
    """
    if bridge.active_project_id is not None:
        return True
    if str(bridge.status or "").lower() == "standby":
        return False
    if str(bridge.status or "").lower() in {"retired", "stopping"}:
        return False
    if str(bridge.status or "").lower() in {"active", "busy"} and _bridge_connected(bridge):
        return True
    if bridge.last_seen_at is not None and bridge.last_seen_at >= _bridge_alive_cutoff():
        return True
    if _bridge_agent_recent(bridge):
        return True
    created_at = getattr(bridge, "created_at", None)
    if created_at is not None and created_at >= _now() - timedelta(minutes=10):
        return True
    return False


def _probe_bridge(bridge: HermesBrowserBridge) -> tuple[bool, str | None, str | None]:
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(f"{bridge.cdp_url.rstrip('/')}/json/version")
            response.raise_for_status()
            data = response.json()
        return True, str(data.get("Browser") or bridge.browser or ""), None
    except Exception as exc:
        return False, None, str(exc)[:300]


def _recover_degraded_bridge_if_reachable(
    bridge: HermesBrowserBridge,
    *,
    now: datetime | None = None,
) -> bool:
    """Recover a sticky slot after its heartbeat and CDP tunnel return.

    A project must stay on its original slot, but a stale degraded marker used
    to make that same healthy slot look offline for the full cooldown. Require
    a fresh agent heartbeat, then probe CDP before clearing the marker.
    """
    recovered_at = now or _now()
    if str(bridge.status or "").lower() != "active":
        return False
    if bridge.last_seen_at is None or bridge.last_seen_at < _bridge_alive_cutoff():
        return False
    if not _bridge_recently_degraded(bridge):
        return _bridge_connected(bridge)
    if not _bridge_degraded_old_enough_to_probe(bridge):
        return False
    reachable, browser, _probe_error = _probe_bridge(bridge)
    if not reachable:
        return False
    bridge.status = "active"
    bridge.browser = browser or bridge.browser
    bridge.last_seen_at = recovered_at
    _clear_bridge_degraded_marker(bridge)
    return True


def _release_bridge_for_project(db: Session, *, project_id: int) -> None:
    rows = db.query(HermesBrowserBridge).filter(HermesBrowserBridge.active_project_id == int(project_id)).all()
    for bridge in rows:
        bridge.active_project_id = None
        bridge.active_stage_id = None
        bridge.lease_expires_at = None
        meta = dict(bridge.meta_json or {})
        for key in (
            "agent_slot_mode",
            "agent_slot_mode_requested_at",
            "agent_slot_mode_project_id",
        ):
            meta.pop(key, None)
        bridge.meta_json = meta
        db.add(bridge)


def _project_bridge_lock_terminal(project: HermesContentFactoryProject | None, *, active_stage: HermesContentFactoryStage | None = None) -> bool:
    if project is None:
        return True
    config = dict(project.config_json or {})
    status = str(project.status or "").lower()
    if (
        status in {"paused", "complete"}
        or str(getattr(project, "current_stage", "") or "").upper() == "COMPLETE"
        or bool(config.get("manual_paused", False))
    ):
        return True
    if status == "waiting_bridge":
        # A waiting label alone is not browser demand. If no stage is queued,
        # running, or retrying there is nothing that can consume the slot.
        # Release Chrome now; a later real browser retry records a fresh slot
        # request and wakes the same project/device through normal acquisition.
        if active_stage is None:
            return True
        session_state = dict(dict(project.state_json or {}).get("chatgpt_session") or {})
        if str(session_state.get("status") or "").lower() in {
            "login_required",
            "temporarily_rate_limited",
            "quota_limited",
        }:
            # Login/quota waits can last minutes or hours. Keep the project's
            # sticky bridge id in state_json, but release the live lease so a
            # different project on the same user device can use the slot.
            return True
    if status == "failed":
        state = dict(project.state_json or {})
        return (
            bool(state.get("ai_video_terminal_failure"))
            or not bool(config.get("auto_run", True))
            or active_stage is None
        )
    return False


def _project_active_stage_for_bridge_lock(db: Session, project_id: int) -> HermesContentFactoryStage | None:
    return (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == int(project_id),
            HermesContentFactoryStage.status.in_(("queued", "running", "retrying")),
        )
        .order_by(HermesContentFactoryStage.id.desc())
        .first()
    )


def _reserve_project_state_bridge_locks(
    db: Session,
    *,
    workspace_id: int | None = None,
    user_id: int | None = None,
) -> int:
    """Backfill bridge table ownership from each active project's sticky lock."""
    query = db.query(HermesContentFactoryProject).filter(
        HermesContentFactoryProject.status.in_((
            "queued",
            "running",
            "retrying",
            "waiting_bridge",
            "generating_video",
            "ready",
            "failed",
        ))
    )
    if workspace_id is not None:
        query = query.filter(HermesContentFactoryProject.workspace_id == int(workspace_id))
    if user_id is not None:
        query = query.filter(HermesContentFactoryProject.user_id == int(user_id))
    repaired = 0
    now = _now()
    for project in query.order_by(HermesContentFactoryProject.updated_at.desc()).limit(500).all():
        state = dict(project.state_json or {})
        bridge_id = str(state.get("browser_bridge_id") or "").strip()
        if not bridge_id:
            continue
        active_stage = _project_active_stage_for_bridge_lock(db, int(project.id))
        if _project_bridge_lock_terminal(project, active_stage=active_stage):
            continue
        bridge = (
            db.query(HermesBrowserBridge)
            .filter(
                HermesBrowserBridge.bridge_id == bridge_id,
                HermesBrowserBridge.workspace_id == int(project.workspace_id),
                HermesBrowserBridge.user_id == int(project.user_id or 0),
            )
            .one_or_none()
        )
        if bridge is None:
            continue
        # Keep the sticky bridge id in project state, but do not reserve
        # capacity for a device that is offline.  The same row is reclaimed
        # when that device heartbeats again; reserving it while offline made
        # projects appear permanently queued and extended dead leases forever.
        if not _bridge_connected(bridge):
            continue
        if bridge.active_project_id not in (None, int(project.id)):
            continue
        changed = False
        if bridge.active_project_id != int(project.id):
            bridge.active_project_id = int(project.id)
            changed = True
        expected_stage_id = int(active_stage.id) if active_stage is not None else None
        if bridge.active_stage_id != expected_stage_id:
            bridge.active_stage_id = expected_stage_id
            changed = True
        if bridge.lease_expires_at is None or bridge.lease_expires_at <= now:
            bridge.lease_expires_at = now + timedelta(hours=BRIDGE_LEASE_HOURS)
            changed = True
        if changed:
            db.add(bridge)
            repaired += 1
    if repaired:
        db.flush()
    return repaired


def reconcile_bridge_project_leases(
    db: Session,
    *,
    workspace_id: int | None = None,
    user_id: int | None = None,
) -> dict[str, int]:
    """Repair stale bridge ownership without moving a project across users or devices."""
    _release_expired_bridge_leases(db)
    reserved_from_state = _reserve_project_state_bridge_locks(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
    )
    query = db.query(HermesBrowserBridge).filter(HermesBrowserBridge.active_project_id.isnot(None))
    if workspace_id is not None:
        query = query.filter(HermesBrowserBridge.workspace_id == int(workspace_id))
    if user_id is not None:
        query = query.filter(HermesBrowserBridge.user_id == int(user_id))
    rows = query.order_by(HermesBrowserBridge.active_project_id.asc(), HermesBrowserBridge.id.desc()).all()
    grouped: dict[int, list[HermesBrowserBridge]] = {}
    for bridge in rows:
        grouped.setdefault(int(bridge.active_project_id), []).append(bridge)

    stats = {"checked": len(rows), "released": 0, "duplicates": 0, "repaired": reserved_from_state}
    for project_id, project_bridges in grouped.items():
        project = db.get(HermesContentFactoryProject, int(project_id))
        active_stage = None
        if project is not None:
            active_stage = (
                db.query(HermesContentFactoryStage)
                .filter(
                    HermesContentFactoryStage.project_id == int(project.id),
                    HermesContentFactoryStage.status.in_(("queued", "running", "retrying")),
                )
                .order_by(HermesContentFactoryStage.id.desc())
                .first()
            )
        # API provider work owns no browser process.  A dormant slot is an
        # intentional, project-pinned profile reservation, not an unhealthy
        # bridge.  Do this before generic disconnected-CDP recovery so a
        # 20-second video waiter can never turn controlled sleep into a
        # restart/release loop.
        dormant_rows = [
            bridge for bridge in project_bridges
            if _bridge_is_api_video_dormant(
                bridge,
                project,
                active_stage=active_stage,
            )
        ]
        if dormant_rows:
            for bridge in dormant_rows:
                if bridge.active_stage_id is not None:
                    bridge.active_stage_id = None
                    stats["repaired"] += 1
                bridge.lease_expires_at = _now() + timedelta(hours=BRIDGE_LEASE_HOURS)
                db.add(bridge)
            project_bridges = [bridge for bridge in project_bridges if bridge not in dormant_rows]
            if not project_bridges:
                continue
        # Manual pause is useful during diagnosis, but it must not cancel an
        # already-requested restart of this project's exact Chrome profile.
        # Preserve that lease until CDP returns or the bounded grace expires.
        restarting_rows = [
            bridge for bridge in project_bridges
            if (
                project is not None
                and bridge.active_project_id == int(project.id)
                and _bridge_same_slot_restart_in_grace(bridge)
            )
        ]
        if restarting_rows:
            for bridge in restarting_rows:
                bridge.active_stage_id = int(active_stage.id) if active_stage is not None else None
                bridge.lease_expires_at = _now() + timedelta(hours=BRIDGE_LEASE_HOURS)
                db.add(bridge)
            project_bridges = [bridge for bridge in project_bridges if bridge not in restarting_rows]
            if not project_bridges:
                continue
        disconnected_rows = [bridge for bridge in project_bridges if not _bridge_connected(bridge)]
        if disconnected_rows:
            owner_valid = project is not None and all(
                int(bridge.workspace_id) == int(project.workspace_id)
                and int(bridge.user_id or 0) == int(project.user_id or 0)
                for bridge in project_bridges
            )
            if owner_valid and not _project_bridge_lock_terminal(project, active_stage=active_stage):
                rebuilding_rows = [
                    bridge for bridge in disconnected_rows
                    if (
                        str(bridge.status or "").lower() == "pending"
                        and bool(dict(bridge.meta_json or {}).get("agent_managed"))
                        and bridge.active_project_id == int(project.id)
                        and _bridge_base_device_online(db, bridge)
                    )
                ]
                release_rows = [bridge for bridge in disconnected_rows if bridge not in rebuilding_rows]
                for bridge in release_rows:
                    # Preserve project.state_json.browser_bridge_id so the
                    # project remains pinned to this exact user/device slot,
                    # while releasing server capacity until the agent returns.
                    bridge.active_project_id = None
                    bridge.active_stage_id = None
                    bridge.lease_expires_at = None
                    if not _bridge_agent_recent(bridge) and str(bridge.status or "").lower() == "active":
                        bridge.status = "offline"
                    db.add(bridge)
                    stats["released"] += 1
                for bridge in rebuilding_rows:
                    bridge.active_project_id = int(project.id)
                    bridge.active_stage_id = int(active_stage.id) if active_stage is not None else None
                    bridge.lease_expires_at = _now() + timedelta(hours=BRIDGE_LEASE_HOURS)
                    db.add(bridge)
                config = dict(project.config_json or {})
                if str(project.status or "").lower() not in {"paused", "complete"} and not bool(config.get("manual_paused", False)):
                    state = dict(project.state_json or {})
                    agent_online = bool(rebuilding_rows) or any(
                        _bridge_agent_recent(bridge) or _bridge_base_device_online(db, bridge)
                        for bridge in disconnected_rows
                    )
                    offline_key = "browser_cdp_recovering" if agent_online else "browser_device_offline"
                    if not bool(state.get(offline_key)) or str(project.status or "").lower() != "waiting_bridge":
                        state["browser_bridge_stale_at"] = _now().isoformat()
                    state["browser_device_offline"] = not agent_online
                    state["browser_cdp_recovering"] = agent_online
                    state["browser_bridge_lock_policy"] = "project_sticky_slot"
                    project.state_json = state
                    project.status = "waiting_bridge"
                    project.last_error = (
                        "The local bridge agent is online and is rebuilding this project's Chrome/CDP slot."
                        if agent_online
                        else "The project's browser device is offline. Hermes will resume after that device starts and reconnects."
                    )
                    db.add(project)
                    stats["repaired"] += 1
                continue
            for bridge in disconnected_rows:
                bridge.active_project_id = None
                bridge.active_stage_id = None
                bridge.lease_expires_at = None
                if str(bridge.status or "").lower() == "active":
                    bridge.status = "offline"
                db.add(bridge)
                stats["released"] += 1
            project_bridges = [bridge for bridge in project_bridges if _bridge_connected(bridge)]
            if project is not None and not project_bridges:
                config = dict(project.config_json or {})
                if str(project.status or "").lower() not in {"paused", "complete"} and not bool(config.get("manual_paused", False)):
                    state = dict(project.state_json or {})
                    project.status = "waiting_bridge"
                    project.last_error = (
                        "Browser bridge heartbeat expired; Hermes released the stale slot "
                        "and will resume when this user's local bridge reconnects."
                    )
                    if not bool(state.get("browser_device_offline")):
                        state["browser_bridge_stale_at"] = _now().isoformat()
                    state["browser_device_offline"] = True
                    state["browser_cdp_recovering"] = False
                    project.state_json = state
                    db.add(project)
                    stats["repaired"] += 1
                continue
        invalid_owner = project is None or any(
            int(bridge.workspace_id) != int(project.workspace_id)
            or int(bridge.user_id) != int(project.user_id or 0)
            for bridge in project_bridges
        )
        terminal = _project_bridge_lock_terminal(project, active_stage=active_stage)
        if invalid_owner or terminal:
            for bridge in project_bridges:
                bridge.active_project_id = None
                bridge.active_stage_id = None
                bridge.lease_expires_at = None
                db.add(bridge)
                stats["released"] += 1
            continue

        state = dict(project.state_json or {})
        offline_state_cleared = any(
            key in state for key in ("browser_device_offline", "browser_cdp_recovering")
        )
        state.pop("browser_device_offline", None)
        state.pop("browser_cdp_recovering", None)
        if offline_state_cleared:
            project.state_json = state
            db.add(project)
        preferred_device_id = str(state.get("preferred_browser_device_id") or "").strip()
        if preferred_device_id:
            wrong_device_rows = [
                bridge for bridge in project_bridges
                if not _bridge_matches_preferred_device(bridge, preferred_device_id)
            ]
            for bridge in wrong_device_rows:
                bridge.active_project_id = None
                bridge.active_stage_id = None
                bridge.lease_expires_at = None
                db.add(bridge)
                stats["released"] += 1
            project_bridges = [
                bridge for bridge in project_bridges
                if _bridge_matches_preferred_device(bridge, preferred_device_id)
            ]
            if not project_bridges:
                for key in (
                    "browser_bridge_id",
                    "browser_device_id",
                    "browser_device_name",
                    "browser_cdp_url",
                    "browser_inbox_root",
                    "browser_lease_expires_at",
                ):
                    state.pop(key, None)
                project.state_json = state
                project.status = "waiting_bridge"
                project.last_error = (
                    "The project's original browser device is offline. "
                    "Hermes will resume only on that same user device."
                )
                db.add(project)
                stats["repaired"] += 1
                continue
        preferred_bridge_id = str(state.get("browser_bridge_id") or "").strip()
        matching = [bridge for bridge in project_bridges if str(bridge.bridge_id) == preferred_bridge_id]
        keep = matching[0] if matching else max(
            project_bridges,
            key=lambda bridge: bridge.last_seen_at or bridge.updated_at or bridge.created_at,
        )
        for bridge in project_bridges:
            if int(bridge.id) == int(keep.id):
                continue
            bridge.active_project_id = None
            bridge.active_stage_id = None
            bridge.lease_expires_at = None
            db.add(bridge)
            stats["released"] += 1
            stats["duplicates"] += 1

        expected_stage_id = int(active_stage.id) if active_stage is not None else None
        if keep.active_stage_id != expected_stage_id:
            keep.active_stage_id = expected_stage_id
            stats["repaired"] += 1
        keep.lease_expires_at = _now() + timedelta(hours=BRIDGE_LEASE_HOURS)
        db.add(keep)

        if (
            str(state.get("browser_bridge_id") or "") != str(keep.bridge_id)
            or str(state.get("browser_cdp_url") or "") != str(keep.cdp_url or "")
        ):
            state["browser_bridge_id"] = keep.bridge_id
            state["browser_device_id"] = keep.device_id
            state["browser_device_name"] = keep.device_name
            state["browser_cdp_url"] = keep.cdp_url
            state["browser_inbox_root"] = keep.inbox_root
            state["browser_lease_expires_at"] = keep.lease_expires_at.isoformat()
            project.state_json = state
            db.add(project)
            stats["repaired"] += 1
    db.flush()
    return stats


def _allocated_bridge_ports(db: Session) -> set[int]:
    ports: set[int] = set()
    for (port,) in db.query(HermesBrowserBridge.server_port).filter(HermesBrowserBridge.server_port.isnot(None)).all():
        try:
            ports.add(int(port))
        except (TypeError, ValueError):
            pass
    return ports


def _allocate_bridge_port(db: Session) -> int:
    used = _allocated_bridge_ports(db)
    for port in range(BRIDGE_PORT_START, BRIDGE_PORT_END + 1):
        if port not in used:
            return port
    raise APIError("CONTENT_BROWSER_PORTS_EXHAUSTED", "No browser bridge tunnel port is available. Please wait and retry.", 429)


def _port_from_url(value: str) -> int | None:
    try:
        parsed = urlparse(value)
        return parsed.port
    except Exception:
        return None


def _ensure_bridge_endpoint_not_reused(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    device_id: str,
    cdp_url: str,
    row_id: int | None = None,
) -> None:
    """A CDP reverse-tunnel endpoint belongs to exactly one user device.

    Without this guard a different account can register a bridge row that
    points at an already-existing 127.0.0.1:<server_port> tunnel, which makes
    that user's projects drive another user's local Chrome session.
    """
    url = str(cdp_url or "").strip().rstrip("/")
    port = _port_from_url(url)
    if not url and port is None:
        return
    query = db.query(HermesBrowserBridge)
    if row_id is not None:
        query = query.filter(HermesBrowserBridge.id != int(row_id))
    if port is not None:
        query = query.filter(
            (HermesBrowserBridge.server_port == int(port))
            | (HermesBrowserBridge.cdp_url == url)
        )
    else:
        query = query.filter(HermesBrowserBridge.cdp_url == url)
    conflict = query.first()
    if conflict is None:
        return
    same_owner = (
        int(conflict.workspace_id) == int(workspace_id)
        and int(conflict.user_id or 0) == int(user_id)
        and str(conflict.device_id or "") == str(device_id or "")
    )
    if not same_owner:
        raise APIError(
            "CONTENT_BROWSER_ENDPOINT_IN_USE",
            "This browser bridge endpoint is already registered to another user or device. "
            "Please start this user's own bridge executable instead of reusing an existing tunnel.",
            409,
        )


def _bridge_command(port: int) -> str:
    return (
        "自动模式：下载并运行一次自启动安装脚本，之后本机将自动启动 Chrome 和 SSH 隧道。\n"
        "临时手动命令：ssh -N -R 127.0.0.1:{server_port}:127.0.0.1:{local_port} {target}"
    ).format(server_port=int(port), local_port=BRIDGE_LOCAL_CDP_PORT, target=BRIDGE_SSH_TARGET)


def _ps_single_quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _agent_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _agent_b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_bridge_agent_token(*, workspace_id: int, user_id: int, device_id: str) -> str:
    payload = {
        "workspace_id": int(workspace_id),
        "user_id": int(user_id),
        "device_id": str(device_id or "").strip()[:128],
        "expires_at": int((_now() + timedelta(days=max(1, BRIDGE_AGENT_TOKEN_TTL_DAYS))).timestamp()),
        "version": 1,
    }
    encoded = _agent_b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = hmac.new(settings.SECRET_KEY.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_agent_b64(signature)}"


def verify_bridge_agent_token(token: str) -> dict[str, Any]:
    try:
        encoded, supplied_signature = str(token or "").split(".", 1)
        expected = hmac.new(settings.SECRET_KEY.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _agent_b64d(supplied_signature)):
            raise ValueError("signature mismatch")
        payload = json.loads(_agent_b64d(encoded).decode("utf-8"))
        if int(payload.get("expires_at") or 0) < int(_now().timestamp()):
            raise ValueError("token expired")
        if not payload.get("workspace_id") or not payload.get("user_id") or not payload.get("device_id"):
            raise ValueError("token payload incomplete")
        return payload
    except Exception as exc:
        raise APIError("CONTENT_BROWSER_AGENT_AUTH_INVALID", "Browser agent authentication is invalid or expired.", 401) from exc


def build_bridge_agent_executable(
    *, workspace_id: int, user_id: int, device_id: str, device_name: str | None, api_base_url: str,
) -> tuple[str, bytes]:
    if not BRIDGE_AGENT_BINARY.is_file():
        raise APIError("CONTENT_BROWSER_AGENT_BINARY_MISSING", "Windows browser agent is not installed on the server.", 503)
    token = create_bridge_agent_token(workspace_id=workspace_id, user_id=user_id, device_id=device_id)
    config = {
        "api_url": f"{api_base_url.rstrip('/')}/api/v1/tenants/{int(workspace_id)}/hermes-agent/content-factory/bridge/agent",
        "token": token,
        "workspace_id": int(workspace_id),
        "user_id": int(user_id),
        "device_id": str(device_id or "").strip()[:128],
        "device_name": str(device_name or "Windows device").strip()[:255],
        "local_capacity": 4,
    }
    payload = json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    filename = "MYUPONA-HermesBridge.exe"
    return filename, BRIDGE_AGENT_BINARY.read_bytes() + BRIDGE_AGENT_CONFIG_MARKER + payload


def _agent_rows(db: Session, *, workspace_id: int, user_id: int, device_id: str) -> list[HermesBrowserBridge]:
    rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.status != "retired",
        )
        .order_by(HermesBrowserBridge.id.asc())
        .all()
    )
    return [row for row in rows if str(dict(row.meta_json or {}).get("agent_device_id") or "") == str(device_id)]


def _bridge_base_device_id(bridge: HermesBrowserBridge) -> str:
    meta = dict(bridge.meta_json or {})
    return str(meta.get("agent_device_id") or str(bridge.device_id or "").split("::slot:", 1)[0]).strip()


def _bridge_device_bound(bridge: HermesBrowserBridge) -> bool:
    return dict(bridge.meta_json or {}).get("account_device_bound") is not False


def _bridge_auth_status(bridge: HermesBrowserBridge) -> str:
    return str(dict(bridge.meta_json or {}).get("chatgpt_auth_status") or "checking").strip().lower()


def _bridge_login_ready(bridge: HermesBrowserBridge) -> bool:
    return _bridge_auth_status(bridge) == "ready"


def _account_device_rows(
    db: Session, *, workspace_id: int, user_id: int, device_id: str | None = None,
    include_retired: bool = False,
) -> list[HermesBrowserBridge]:
    query = db.query(HermesBrowserBridge).filter(
        HermesBrowserBridge.workspace_id == int(workspace_id),
        HermesBrowserBridge.user_id == int(user_id),
    )
    if not include_retired:
        query = query.filter(HermesBrowserBridge.status != "retired")
    rows = query.order_by(HermesBrowserBridge.id.asc()).all()
    wanted = str(device_id or "").strip()
    return [row for row in rows if not wanted or _bridge_base_device_id(row) == wanted]


def browser_devices(db: Session, *, workspace_id: int, user_id: int) -> list[dict[str, Any]]:
    groups: dict[str, list[HermesBrowserBridge]] = {}
    for row in _account_device_rows(db, workspace_id=workspace_id, user_id=user_id):
        device_id = _bridge_base_device_id(row)
        if device_id:
            groups.setdefault(device_id, []).append(row)
    devices: list[dict[str, Any]] = []
    for device_id, rows in groups.items():
        bound = any(_bridge_device_bound(row) for row in rows)
        agent_online = any(_bridge_agent_recent(row) for row in rows)
        connected = any(bound and _bridge_connected(row) for row in rows)
        login_ready_slots = sum(1 for row in rows if bound and _bridge_connected(row) and _bridge_login_ready(row))
        login_required_slots = sum(1 for row in rows if bound and _bridge_auth_status(row) == "login_required")
        selected = bound and any(bool(dict(row.meta_json or {}).get("account_device_selected")) for row in rows)
        active_projects = sorted({int(row.active_project_id) for row in rows if row.active_project_id})
        heartbeat_values = [
            str(dict(row.meta_json or {}).get("agent_last_heartbeat_at") or "")
            for row in rows
            if str(dict(row.meta_json or {}).get("agent_last_heartbeat_at") or "")
        ]
        devices.append({
            "device_id": device_id,
            "device_name": next((str(row.device_name) for row in reversed(rows) if row.device_name), "Windows device"),
            "bound": bound,
            "selected": selected,
            "online": agent_online,
            "connected": connected,
            "slot_count": len(rows),
            "login_ready_slot_count": login_ready_slots,
            "login_required_slot_count": login_required_slots,
            "active_project_ids": active_projects,
            "last_heartbeat_at": max(heartbeat_values) if heartbeat_values else None,
        })
    devices.sort(key=lambda item: (not item["online"], not item["bound"], str(item["device_name"]), item["device_id"]))
    return devices


def _effective_browser_device_id(devices: list[dict[str, Any]]) -> tuple[str | None, bool]:
    online = [item for item in devices if item.get("bound") and item.get("online")]
    if len(online) == 1:
        return str(online[0]["device_id"]), False
    if len(online) > 1:
        selected = [item for item in online if item.get("selected")]
        if len(selected) == 1:
            return str(selected[0]["device_id"]), False
        return None, True
    return None, False


def bind_browser_device(db: Session, *, workspace_id: int, user_id: int, device_id: str) -> None:
    rows = _account_device_rows(db, workspace_id=workspace_id, user_id=user_id, device_id=device_id)
    if not rows:
        raise APIError("CONTENT_BROWSER_DEVICE_NOT_FOUND", "Browser device is not registered for this account.", 404)
    for row in rows:
        meta = dict(row.meta_json or {})
        meta["account_device_bound"] = True
        meta.pop("account_device_unbound_at", None)
        row.meta_json = meta
        if row.status == "unbound":
            row.status = "pending"
        db.add(row)


def select_browser_device(db: Session, *, workspace_id: int, user_id: int, device_id: str) -> None:
    all_rows = _account_device_rows(db, workspace_id=workspace_id, user_id=user_id)
    target_rows = [row for row in all_rows if _bridge_base_device_id(row) == str(device_id)]
    if not target_rows or not any(_bridge_device_bound(row) for row in target_rows):
        raise APIError("CONTENT_BROWSER_DEVICE_NOT_BOUND", "Bind this browser device before selecting it.", 409)
    if not any(_bridge_agent_recent(row) for row in target_rows):
        raise APIError("CONTENT_BROWSER_DEVICE_OFFLINE", "Only an online browser device can be selected.", 409)
    for row in all_rows:
        meta = dict(row.meta_json or {})
        meta["account_device_selected"] = _bridge_base_device_id(row) == str(device_id)
        row.meta_json = meta
        db.add(row)


def unbind_browser_device(db: Session, *, workspace_id: int, user_id: int, device_id: str) -> None:
    rows = _account_device_rows(db, workspace_id=workspace_id, user_id=user_id, device_id=device_id)
    if not rows:
        raise APIError("CONTENT_BROWSER_DEVICE_NOT_FOUND", "Browser device is not registered for this account.", 404)
    if any(row.active_project_id is not None for row in rows):
        raise APIError("CONTENT_BROWSER_DEVICE_IN_USE", "Pause or delete projects using this device before unbinding it.", 409)
    now = _now()
    bridge_ids = {str(row.bridge_id) for row in rows}
    for row in rows:
        meta = dict(row.meta_json or {})
        meta["account_device_bound"] = False
        meta["account_device_selected"] = False
        meta["account_device_unbound_at"] = now.isoformat()
        row.meta_json = meta
        row.status = "unbound"
        row.active_stage_id = None
        row.lease_expires_at = None
        db.add(row)
    projects = (
        db.query(HermesContentFactoryProject)
        .filter(
            HermesContentFactoryProject.workspace_id == int(workspace_id),
            HermesContentFactoryProject.user_id == int(user_id),
            HermesContentFactoryProject.status.in_(("paused", "failed", "waiting_bridge", "queued")),
        )
        .all()
    )
    for project in projects:
        state = dict(project.state_json or {})
        if (
            str(state.get("browser_bridge_id") or "") in bridge_ids
            or str(state.get("preferred_browser_device_id") or "") == str(device_id)
        ):
            for key in (
                "browser_bridge_id", "browser_device_id", "browser_device_name", "browser_cdp_url",
                "browser_inbox_root", "browser_lease_expires_at", "preferred_browser_device_id",
            ):
                state.pop(key, None)
            state["browser_device_released_at"] = now.isoformat()
            project.state_json = state
            db.add(project)


def _agent_used_slot_indices(db: Session, *, workspace_id: int, user_id: int, device_id: str) -> set[int]:
    rows = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
        )
        .order_by(HermesBrowserBridge.id.asc())
        .all()
    )
    indices: set[int] = set()
    for row in rows:
        meta = dict(row.meta_json or {})
        if str(meta.get("agent_device_id") or "") != str(device_id):
            continue
        # Retired slots have already been removed from the agent's desired set
        # and their Chrome/SSH processes are stopped. Reuse their local index;
        # otherwise a long-running device eventually exhausts the 0..31 pool.
        if str(row.status or "").lower() == "retired":
            continue
        try:
            indices.add(int(meta.get("slot_index") or 0))
        except (TypeError, ValueError):
            continue
    return indices


def _agent_target_slot_count(
    *, capacity: int, active_project_ids: set[int], requested_project_ids: set[int],
) -> int:
    demand = set(active_project_ids) | set(requested_project_ids)
    # Browser slots are cold by default. A browser stage first writes a recent,
    # explicit project request; the following Agent heartbeat then creates the
    # exact slot that stage can acquire. Keeping one speculative bootstrap
    # Chrome open made API-only work repeatedly launch a browser even though no
    # browser fallback had been requested.
    return min(max(1, int(capacity)), len(demand))


def _recent_project_slot_request(
    project: HermesContentFactoryProject, *, device_id: str, now: datetime,
) -> bool:
    if project.status in {"paused", "failed", "complete", "deleted"} or project.current_stage == "COMPLETE":
        return False
    if bool(dict(project.config_json or {}).get("manual_paused", False)):
        return False
    state = dict(project.state_json or {})
    preferred = str(state.get("preferred_browser_device_id") or "").strip()
    if not preferred or preferred != str(device_id):
        return False
    requested_at = _parse_bridge_timestamp(state.get("browser_slot_requested_at"))
    return bool(requested_at and requested_at >= now - timedelta(minutes=15))


def _retire_agent_slot(row: HermesBrowserBridge, *, reason: str, now: datetime) -> None:
    row.status = "retired"
    row.active_project_id = None
    row.active_stage_id = None
    row.lease_expires_at = None
    meta = dict(row.meta_json or {})
    meta["retired_reason"] = str(reason)
    meta["retired_at"] = now.isoformat()
    row.meta_json = meta


def _retire_dead_agent_rows(
    db: Session,
    rows: list[HermesBrowserBridge],
    *,
    reports: dict[str, dict[str, Any]],
    now: datetime,
) -> list[HermesBrowserBridge]:
    """Drop stale warm slots so a restarted bridge agent receives fresh ports.

    A downloaded Windows bridge can outlive failed SSH/CDP tunnels. If we keep
    returning those old bridge IDs forever, the user's local agent keeps
    retrying dead reverse ports and the UI stays "not connected". Occupied
    project slots are never retired here.
    """
    try:
        stale_minutes = max(5, min(120, int(os.getenv("HERMES_AGENT_STALE_SLOT_MINUTES", "20"))))
    except ValueError:
        stale_minutes = 20
    stale_cutoff = now - timedelta(minutes=stale_minutes)
    kept: list[HermesBrowserBridge] = []
    for row in rows:
        if row.active_project_id is not None:
            kept.append(row)
            continue
        if _bridge_same_slot_restart_in_grace(row, now=now):
            restart_project_id = _same_slot_restart_project_id(row)
            project = db.get(HermesContentFactoryProject, int(restart_project_id or 0))
            state = dict(project.state_json or {}) if project is not None else {}
            owner_matches = bool(
                project is not None
                and int(project.workspace_id) == int(row.workspace_id)
                and int(project.user_id or 0) == int(row.user_id or 0)
                and str(state.get("browser_bridge_id") or "") == str(row.bridge_id)
                and str(project.status or "").lower() not in {"complete", "deleted"}
            )
            if owner_matches:
                row.status = "pending"
                row.active_project_id = int(project.id)
                row.active_stage_id = None
                row.lease_expires_at = now + timedelta(hours=BRIDGE_LEASE_HOURS)
                load = dict(row.load_json or {})
                row.load_json = {
                    **load,
                    "agent_error": "awaiting requested same-slot restart",
                    "restart_required": True,
                }
                db.add(row)
                kept.append(row)
                continue
        report = reports.get(str(row.bridge_id)) or {}
        status = str(row.status or "").lower()
        load = dict(row.load_json or {})
        error_text = str(report.get("error") or load.get("agent_error") or "").strip()
        # A stopping slot disappears from the agent's next heartbeat after its
        # Chrome and SSH processes have been closed. Retire it immediately;
        # retaining it for the generic stale timeout makes the device summary
        # claim several slots are still open even though they no longer exist.
        if status == "stopping" and str(row.bridge_id) not in reports:
            _retire_agent_slot(row, reason="agent_confirmed_slot_stopped", now=now)
            db.add(row)
            continue
        if bool(report.get("connected")) and _cdp_tunnel_unusable_error(error_text):
            row.status = "pending"
            row.active_project_id = None
            row.active_stage_id = None
            row.lease_expires_at = None
            row.load_json = {
                **load,
                "agent_error": error_text[:500],
                "restart_required": True,
            }
            db.add(row)
            kept.append(row)
            continue
        if bool(report.get("connected")):
            kept.append(row)
            continue
        created_at = getattr(row, "created_at", None)
        # A pending slot may need up to 45 seconds for Chrome plus the SSH
        # reverse tunnel to become ready.  Its CDP ``last_seen_at`` is still
        # empty during that window, so use the local Agent heartbeat as real
        # activity too.  Looking only at an old ``created_at`` retired the row
        # on every heartbeat and changed its server port, which made the Agent
        # continuously close and reopen the same Chrome profile.
        activity_values = [
            value
            for value in (row.last_seen_at, _bridge_agent_last_heartbeat_at(row), created_at)
            if value is not None
        ]
        last_activity = max(activity_values) if activity_values else None
        if last_activity is not None and last_activity >= stale_cutoff:
            kept.append(row)
            continue
        if status in {"pending", "offline", "stopping"} or error_text:
            _retire_agent_slot(row, reason="stale_agent_slot", now=now)
            db.add(row)
            continue
        kept.append(row)
    return kept


def _cdp_tunnel_unusable_error(value: str | None) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "server disconnected without sending a response",
            "empty reply from server",
            "connection reset",
            "connection refused",
            "failed to connect to cdp",
            "cdp unavailable",
            "websocket connect failed",
            "no connection could be made",
            "actively refused",
        )
    )


def _authorize_bridge_agent_key(*, public_key: str, device_id: str, user_id: int) -> None:
    key = " ".join(str(public_key or "").strip().split())
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]{40,}[^\r\n]*", key):
        raise APIError("CONTENT_BROWSER_AGENT_KEY_INVALID", "Browser agent SSH public key is invalid.", 400)
    key_parts = key.split()
    normalized = f"{key_parts[0]} {key_parts[1]}"
    comment = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"myupona-{user_id}-{device_id}")[:120]
    guard = str(BRIDGE_AGENT_SESSION_GUARD)
    if not guard.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9_./-]+", guard):
        raise RuntimeError("HERMES_BRIDGE_AGENT_SESSION_GUARD must be a safe absolute path")
    line = (
        f'command="exec {guard}",restrict,port-forwarding,'
        f'permitlisten="127.0.0.1:*" {normalized} {comment}'
    )
    BRIDGE_AGENT_AUTHORIZED_KEYS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = BRIDGE_AGENT_AUTHORIZED_KEYS.with_name(f".{BRIDGE_AGENT_AUTHORIZED_KEYS.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            os.fchmod(lock_file.fileno(), 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            existing = (
                BRIDGE_AGENT_AUTHORIZED_KEYS.read_text("utf-8")
                if BRIDGE_AGENT_AUTHORIZED_KEYS.exists()
                else ""
            )
            current_lines = existing.splitlines()
            updated_lines: list[str] = []
            matched = False
            for existing_line in current_lines:
                if normalized not in existing_line:
                    updated_lines.append(existing_line)
                    continue
                if not matched:
                    updated_lines.append(line)
                    matched = True
            if not matched:
                updated_lines.append(line)
            updated = "\n".join(updated_lines) + "\n"
            if updated != existing:
                temp_path = BRIDGE_AGENT_AUTHORIZED_KEYS.with_name(
                    f".{BRIDGE_AGENT_AUTHORIZED_KEYS.name}.{uuid4().hex}.tmp"
                )
                try:
                    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        handle.write(updated)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp_path, BRIDGE_AGENT_AUTHORIZED_KEYS)
                    directory_fd = os.open(BRIDGE_AGENT_AUTHORIZED_KEYS.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    try:
        BRIDGE_AGENT_AUTHORIZED_KEYS.parent.chmod(0o700)
        BRIDGE_AGENT_AUTHORIZED_KEYS.chmod(0o600)
    except OSError:
        pass


def _new_agent_slot(
    db: Session, *, workspace_id: int, user_id: int, device_id: str, device_name: str, inbox_root: str, slot_index: int,
) -> HermesBrowserBridge:
    slot_device_id = f"{device_id[:96]}::slot:{int(slot_index)}"
    identity_filter = (
        HermesBrowserBridge.workspace_id == int(workspace_id),
        HermesBrowserBridge.user_id == int(user_id),
        HermesBrowserBridge.device_id == slot_device_id,
    )
    existing = db.query(HermesBrowserBridge).filter(*identity_filter).one_or_none()
    if existing is not None and str(existing.status or "").lower() != "retired":
        # Older bridge versions could create a ``device::slot:N`` identity
        # through the generic registration endpoint. The unique key then
        # exists, but without agent metadata `_agent_rows()` cannot associate
        # the next heartbeat with it. Adopt that row in place so the Agent does
        # not repeatedly stop and recreate the same Chrome profile.
        meta = dict(existing.meta_json or {})
        expected_meta = {
            "agent_managed": True,
            "agent_device_id": device_id,
            "account_device_bound": True,
            "slot_index": int(slot_index),
            "local_port": int(BRIDGE_LOCAL_CDP_PORT + slot_index),
        }
        if any(meta.get(key) != value for key, value in expected_meta.items()):
            now = _now()
            meta.update(expected_meta)
            meta["agent_last_heartbeat_at"] = now.isoformat()
            existing.meta_json = meta
            existing.device_name = str(device_name or existing.device_name or "Windows device")[:255]
            existing.inbox_root = str(inbox_root or existing.inbox_root or WINDOWS_INBOX)[:1024]
            existing.last_seen_at = now
            if str(existing.status or "").lower() in {"offline", "unbound", "stopping"}:
                existing.status = "pending"
            db.add(existing)
            db.flush()
        return existing

    port = _allocate_bridge_port(db)
    now = _now()
    values = {
        "bridge_id": f"br_{uuid4().hex}",
        "workspace_id": int(workspace_id),
        "user_id": int(user_id),
        "device_id": slot_device_id,
        "device_name": str(device_name or "Windows device")[:255],
        "cdp_url": f"http://127.0.0.1:{port}",
        "server_port": port,
        "inbox_root": str(inbox_root or WINDOWS_INBOX)[:1024],
        "browser": "Chrome",
        "status": "pending",
        # This is an Agent-bootstrap timestamp, not a successful CDP probe.
        # It gives Chrome/SSH one stale-slot window to report readiness.
        "last_seen_at": now,
        "load_json": {},
        "meta_json": {
            "agent_managed": True,
            "agent_device_id": device_id,
            "account_device_bound": True,
            "slot_index": int(slot_index),
            "local_port": int(BRIDGE_LOCAL_CDP_PORT + slot_index),
            "agent_last_heartbeat_at": now.isoformat(),
        },
    }

    if existing is not None:
        # A retired row still owns this device/slot identity and its stable
        # Chrome profile. Revive it in place rather than violating the unique
        # key or forcing the user to sign in to a replacement profile.
        for key, value in values.items():
            if key not in {"bridge_id", "workspace_id", "user_id", "device_id"}:
                setattr(existing, key, value)
        existing.active_project_id = None
        existing.active_stage_id = None
        existing.lease_expires_at = None
        db.add(existing)
        db.flush()
        return existing

    dialect_name = str(db.get_bind().dialect.name or "").lower()
    if dialect_name in {"mysql", "mariadb"}:
        # Heartbeats can arrive at different API workers simultaneously. The
        # unique device key is the arbiter: one insert wins and every loser
        # performs a harmless no-op update, then reads the same canonical row.
        # This keeps the transaction usable and prevents an IntegrityError 500.
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        statement = mysql_insert(HermesBrowserBridge).values(**values)
        statement = statement.on_duplicate_key_update(device_id=statement.inserted.device_id)
        db.execute(statement)
        db.flush()
        row = db.query(HermesBrowserBridge).filter(*identity_filter).one()
        return row

    row = HermesBrowserBridge(**values)
    db.add(row)
    db.flush()
    return row


def prepare_browser_slot(
    db: Session, *, workspace_id: int, user_id: int, device_id: str,
) -> HermesBrowserBridge:
    """Create one persistent, device-local slot for manual ChatGPT login."""
    rows = _agent_rows(db, workspace_id=workspace_id, user_id=user_id, device_id=device_id)
    if not rows or not any(_bridge_agent_recent(row) for row in rows):
        raise APIError("CONTENT_BROWSER_DEVICE_OFFLINE", "The selected browser device is offline.", 409)
    if not any(_bridge_device_bound(row) for row in rows):
        raise APIError("CONTENT_BROWSER_DEVICE_NOT_BOUND", "Bind this browser device before adding a slot.", 409)

    device_rows = [
        row
        for row in _account_device_rows(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            device_id=device_id,
            include_retired=True,
        )
        if _bridge_base_device_id(row) == str(device_id)
    ]

    # The initial bootstrap slot already has a stable local Chrome profile.
    # Adopt it on the first click instead of opening an unnecessary second
    # browser. Subsequent clicks intentionally add another login slot.
    reusable = next((
        row for row in rows
        if row.active_project_id is None
        and not bool(dict(row.meta_json or {}).get("manual_pool_slot"))
    ), None)
    if reusable is None:
        # A retired slot still owns its stable Chrome profile. Revive the most
        # recent one before allocating a new browser so the user's login and
        # the project's existing ChatGPT conversation remain available.
        reusable = next((
            row for row in sorted(device_rows, key=lambda item: int(item.id or 0), reverse=True)
            if str(row.status or "").lower() == "retired"
            and row.active_project_id is None
        ), None)
    now = _now()
    if reusable is not None:
        meta = dict(reusable.meta_json or {})
        meta["manual_pool_slot"] = True
        meta["manual_pool_added_at"] = now.isoformat()
        if str(reusable.status or "").lower() in {"retired", "stopping", "offline"}:
            reusable.status = "pending"
            # Give the local Agent one heartbeat window to recreate Chrome/SSH.
            # Leaving the historical timestamp (or None with an old created_at)
            # lets stale-slot cleanup retire the row before it reaches the Agent.
            reusable.last_seen_at = now
            reusable.lease_expires_at = None
            for key in (
                "retired_at",
                "retired_reason",
                "agent_last_heartbeat_at",
                "chatgpt_auth_status",
                "chatgpt_account",
                "chatgpt_page_url",
            ):
                meta.pop(key, None)
        reusable.meta_json = meta
        db.add(reusable)
        db.flush()
        return reusable

    load = _server_load_snapshot()
    capacity = max(1, min(8, int(load.get("capacity") or 1)))
    active_slot_count = sum(1 for row in device_rows if str(row.status or "").lower() != "retired")
    if active_slot_count >= capacity:
        raise APIError(
            "CONTENT_BROWSER_CAPACITY_FULL",
            "The server is at its current browser slot capacity. Remove an idle slot or wait for capacity.",
            429,
        )
    sample = rows[-1]
    used_indices = _agent_used_slot_indices(
        db, workspace_id=workspace_id, user_id=user_id, device_id=device_id,
    )
    slot_index = next(index for index in range(32) if index not in used_indices)
    row = _new_agent_slot(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        device_id=device_id,
        device_name=str(sample.device_name or "Windows device"),
        inbox_root=str(sample.inbox_root or WINDOWS_INBOX),
        slot_index=slot_index,
    )
    meta = dict(row.meta_json or {})
    meta["manual_pool_slot"] = True
    meta["manual_pool_added_at"] = now.isoformat()
    row.meta_json = meta
    db.add(row)
    db.flush()
    return row


def remove_browser_slot(
    db: Session, *, workspace_id: int, user_id: int, bridge_id: str,
) -> None:
    row = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.bridge_id == str(bridge_id),
            HermesBrowserBridge.status != "retired",
        )
        .one_or_none()
    )
    if row is None:
        raise APIError("CONTENT_BROWSER_SLOT_NOT_FOUND", "Browser slot was not found.", 404)
    if row.active_project_id is not None:
        raise APIError("CONTENT_BROWSER_SLOT_IN_USE", "Pause or delete the project before removing this slot.", 409)
    meta = dict(row.meta_json or {})
    meta["retired_reason"] = "manual_slot_pool_remove"
    meta["retired_at"] = _now().isoformat()
    meta["manual_pool_slot"] = False
    row.meta_json = meta
    row.status = "retired"
    row.active_stage_id = None
    row.lease_expires_at = None
    db.add(row)
    db.flush()


def reconcile_bridge_agent(
    db: Session, *, workspace_id: int, user_id: int, device_id: str, device_name: str,
    agent_version: str, public_key: str, inbox_root: str, local_capacity: int,
    reported_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    _authorize_bridge_agent_key(public_key=public_key, device_id=device_id, user_id=user_id)
    reconcile_bridge_project_leases(
        db,
        workspace_id=int(workspace_id),
        user_id=int(user_id),
    )
    rows = _agent_rows(db, workspace_id=workspace_id, user_id=user_id, device_id=device_id)
    reports = {str(item.get("bridge_id") or ""): item for item in (reported_slots or [])}
    now = _now()
    rows = _retire_dead_agent_rows(db, rows, reports=reports, now=now)
    if not rows:
        # Keep one server-side device registration without returning a desired
        # browser slot. Retiring every row made the next valid heartbeat fail
        # authentication, which caused the Windows Agent to reconnect/restart
        # in a loop after the last Chrome was intentionally stopped.
        registration = _new_agent_slot(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            device_id=device_id,
            device_name=device_name,
            inbox_root=inbox_root,
            slot_index=0,
        )
        registration.status = "standby"
        registration.active_project_id = None
        registration.active_stage_id = None
        registration.lease_expires_at = None
        db.add(registration)
        rows = [registration]
    try:
        connected_dead_minutes = max(2, min(30, int(os.getenv("HERMES_AGENT_CONNECTED_DEAD_MINUTES", "5"))))
    except ValueError:
        connected_dead_minutes = 5
    connected_dead_cutoff = now - timedelta(minutes=connected_dead_minutes)
    device_bound = not rows or any(_bridge_device_bound(row) for row in rows)
    for row in rows:
        meta = dict(row.meta_json or {})
        meta["agent_last_heartbeat_at"] = now.isoformat()
        meta["agent_device_name"] = str(device_name or row.device_name or "Windows device")[:255]
        meta["agent_version"] = str(agent_version or "legacy")[:64]
        meta.setdefault("account_device_bound", True)
        row.meta_json = meta
        row.device_name = str(device_name or row.device_name or "Windows device")[:255]
        if not device_bound:
            row.status = "unbound"
            row.active_project_id = None
            row.active_stage_id = None
            row.lease_expires_at = None
            db.add(row)
            continue
        report = reports.get(str(row.bridge_id))
        if not report:
            db.add(row)
            continue
        report_mode = str(report.get("mode") or "active").strip().lower()
        if report_mode == "dormant":
            # Only accept a dormant acknowledgement while the server still
            # requests dormancy.  A one-heartbeat stale report after a wake
            # request must not put a slot back to sleep.
            if _agent_slot_mode(row) == "dormant":
                row.status = "dormant"
                row.last_seen_at = now
                row.load_json = {
                    "agent_error": "",
                    "synced_files": report.get("synced_files") or [],
                    "last_sync_at": report.get("last_sync_at"),
                    "sync_error": str(report.get("sync_error") or "")[:1000],
                    "chatgpt_auth_status": "dormant",
                }
                db.add(row)
                continue
            row.status = "pending"
            row.last_seen_at = now
            row.load_json = {
                "agent_error": "awaiting requested browser-slot wake",
                "synced_files": report.get("synced_files") or [],
                "last_sync_at": report.get("last_sync_at"),
                "sync_error": str(report.get("sync_error") or "")[:1000],
                "restart_required": True,
            }
            db.add(row)
            continue
        auth_status = str(report.get("auth_status") or "checking").strip().lower()
        if auth_status not in {"ready", "login_required", "checking"}:
            auth_status = "checking"
        meta["chatgpt_auth_status"] = auth_status
        meta["chatgpt_auth_checked_at"] = now.isoformat()
        meta["chatgpt_account_name"] = str(report.get("account_name") or "").strip()[:120]
        meta["chatgpt_page_url"] = str(report.get("page_url") or "").strip()[:1000]
        row.meta_json = meta
        connected = bool(report.get("connected"))
        reachable = False
        probe_browser = None
        probe_error = None
        if connected:
            reachable, probe_browser, probe_error = _probe_bridge(row)
        if not reachable and _bridge_same_slot_restart_in_grace(row, now=now):
            row.status = "pending"
            row.load_json = {
                "agent_error": "awaiting requested same-slot restart",
                "synced_files": report.get("synced_files") or [],
                "last_sync_at": report.get("last_sync_at"),
                "sync_error": str(report.get("sync_error") or "")[:1000],
                "chatgpt_auth_status": auth_status,
                "restart_required": True,
            }
            db.add(row)
            continue
        if connected and not reachable and row.active_project_id is None:
            last_activity = row.last_seen_at or getattr(row, "created_at", None)
            if str(probe_error or "").strip() and (
                _cdp_tunnel_unusable_error(probe_error)
                or last_activity is None
                or last_activity < connected_dead_cutoff
            ):
                row.status = "pending"
                row.active_project_id = None
                row.active_stage_id = None
                row.lease_expires_at = None
                row.load_json = {
                    "agent_error": str(probe_error or report.get("error") or "")[:500],
                    "synced_files": report.get("synced_files") or [],
                    "last_sync_at": report.get("last_sync_at"),
                    "sync_error": str(report.get("sync_error") or "")[:1000],
                    "restart_required": True,
                }
                db.add(row)
                continue
        row.status = "active" if reachable else ("pending" if connected else "offline")
        row.last_seen_at = now if reachable else row.last_seen_at
        if reachable:
            _clear_bridge_degraded_marker(row)
            _clear_same_slot_restart_marker(row)
        row.browser = str(probe_browser or report.get("browser") or row.browser or "Chrome")[:255]
        row.load_json = {
            "agent_error": str(probe_error or report.get("error") or "")[:500],
            "synced_files": report.get("synced_files") or [],
            "last_sync_at": report.get("last_sync_at"),
            "sync_error": str(report.get("sync_error") or "")[:1000],
            "chatgpt_auth_status": auth_status,
        }
        db.add(row)
    rows = [row for row in rows if str(row.status or "").lower() != "retired"]

    if rows and not device_bound:
        db.flush()
        return {
            "poll_seconds": 5,
            "agent_version": BRIDGE_AGENT_VERSION,
            "update_required": str(agent_version or "legacy") != BRIDGE_AGENT_VERSION,
            "device_bound": False,
            "server_load": _server_load_snapshot(),
            "slots": [],
            "project_keys": [],
        }

    load = _server_load_snapshot()
    requested_capacity = max(1, min(8, int(local_capacity or 1)))
    capacity = max(1, min(requested_capacity, int(load["capacity"])))
    active_rows = [
        row for row in rows
        if row.active_project_id is not None
        and (row.lease_expires_at is None or row.lease_expires_at > now)
    ]
    active_project_ids = {int(row.active_project_id) for row in active_rows if row.active_project_id}
    active_stage_projects = (
        db.query(HermesContentFactoryStage, HermesContentFactoryProject)
        .join(HermesContentFactoryProject, HermesContentFactoryProject.id == HermesContentFactoryStage.project_id)
        .filter(
            HermesContentFactoryProject.workspace_id == int(workspace_id),
            HermesContentFactoryProject.user_id == int(user_id),
            HermesContentFactoryProject.status != "paused",
            HermesContentFactoryStage.status.in_(("queued", "running", "retrying")),
        )
        .all()
    )
    latest_active_stage_by_project_id: dict[int, HermesContentFactoryStage] = {}
    for stage, project in active_stage_projects:
        project_id = int(project.id)
        current = latest_active_stage_by_project_id.get(project_id)
        if current is None or int(stage.id or 0) > int(current.id or 0):
            latest_active_stage_by_project_id[project_id] = stage
    requested_project_ids: set[int] = set()
    for stage, project in active_stage_projects:
        state = dict(project.state_json or {})
        stage_input = dict(stage.input_json or {})
        backend = stage_execution_backend(
            str(stage.stage or ""),
            api_route=str(stage_input.get("api_route") or "").strip() or None,
            stage_input=stage_input,
        )
        if backend != "browser":
            continue
        preferred = str(state.get("preferred_browser_device_id") or "").strip()
        if preferred and preferred != str(device_id):
            continue
        bridge_id = str(stage_input.get("browser_bridge_id") or state.get("browser_bridge_id") or "").strip()
        bridge = next((row for row in rows if str(row.bridge_id) == bridge_id), None)
        if bridge is None or not _bridge_connected(bridge):
            requested_project_ids.add(int(project.id))
    request_candidates = (
        db.query(HermesContentFactoryProject)
        .filter(
            HermesContentFactoryProject.workspace_id == int(workspace_id),
            HermesContentFactoryProject.user_id == int(user_id),
            HermesContentFactoryProject.status.in_(("draft", "ready", "queued", "running", "retrying", "waiting_bridge")),
        )
        .all()
    )
    requested_project_ids.update(
        int(project.id)
        for project in request_candidates
        if _recent_project_slot_request(project, device_id=device_id, now=now)
    )

    # Do not keep an extra Chrome profile merely as a warm spare. A bridge slot
    # maps to a real local Chrome profile and ChatGPT login session, so creating
    # one speculatively makes users log into the wrong browser. Keep one idle
    # slot only when there is no active project; create additional slots only
    # when another project is actually waiting for this user's device.
    target_count = _agent_target_slot_count(
        capacity=capacity,
        active_project_ids=active_project_ids,
        requested_project_ids=requested_project_ids,
    )
    manual_rows = [row for row in rows if bool(dict(row.meta_json or {}).get("manual_pool_slot"))]
    persistent_row_ids = {int(row.id) for row in active_rows} | {int(row.id) for row in manual_rows}
    target_count = min(capacity, max(target_count, len(persistent_row_ids)))
    used_indices = _agent_used_slot_indices(db, workspace_id=workspace_id, user_id=user_id, device_id=device_id)
    while len(rows) < target_count:
        slot_index = next(index for index in range(32) if index not in used_indices)
        rows.append(_new_agent_slot(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            device_id=device_id,
            device_name=device_name,
            inbox_root=inbox_root,
            slot_index=slot_index,
        ))
        used_indices.add(slot_index)

    desired_rows = list(active_rows)
    desired_ids = {int(row.id) for row in desired_rows}
    for row in manual_rows:
        if len(desired_rows) >= capacity:
            break
        if int(row.id) not in desired_ids:
            desired_rows.append(row)
            desired_ids.add(int(row.id))
    for row in sorted(rows, key=lambda item: int(dict(item.meta_json or {}).get("slot_index") or 0)):
        if len(desired_rows) >= target_count:
            break
        if int(row.id) not in desired_ids:
            desired_rows.append(row)
            desired_ids.add(int(row.id))
    for row in rows:
        row.inbox_root = str(inbox_root or row.inbox_root or WINDOWS_INBOX)[:1024]
        if int(row.id) not in desired_ids and row.active_project_id is None:
            # Omitting this row from ``slots`` is the stop command. Preserve
            # the row as a device registration so the next signed heartbeat
            # remains authorized without keeping Chrome or SSH alive.
            row.status = "standby"
            db.add(row)

    project_keys: set[str] = set()
    for row in desired_rows:
        project = db.get(HermesContentFactoryProject, int(row.active_project_id)) if row.active_project_id else None
        if row.active_project_id:
            if project is not None and int(project.user_id or 0) == int(user_id):
                project_keys.add(str(project.project_key))

    db.flush()
    return {
        "poll_seconds": 3,
        "agent_version": BRIDGE_AGENT_VERSION,
        "update_required": str(agent_version or "legacy") != BRIDGE_AGENT_VERSION,
        "server_load": load,
        "slots": [
            {
                "bridge_id": row.bridge_id,
                "desired": True,
                "local_port": int(dict(row.meta_json or {}).get("local_port") or BRIDGE_LOCAL_CDP_PORT),
                "server_port": int(row.server_port),
                "ssh_host": BRIDGE_AGENT_SSH_HOST,
                "ssh_user": BRIDGE_AGENT_SSH_USER,
                "ssh_port": int(BRIDGE_AGENT_SSH_PORT),
                "inbox_root": str(row.inbox_root or WINDOWS_INBOX),
                "agent_last_heartbeat_at": str(dict(row.meta_json or {}).get("agent_last_heartbeat_at") or ""),
                "active_project_id": int(row.active_project_id) if row.active_project_id else None,
                "mode": "dormant" if _bridge_is_api_video_dormant(
                    row,
                    db.get(HermesContentFactoryProject, int(row.active_project_id)) if row.active_project_id else None,
                    active_stage=latest_active_stage_by_project_id.get(int(row.active_project_id))
                    if row.active_project_id
                    else None,
                ) else "active",
                "restart_required": bool(
                    str(row.status or "").lower() == "pending"
                    and str(dict(row.load_json or {}).get("agent_error") or "").strip()
                ),
                "server_probe_error": str(dict(row.load_json or {}).get("agent_error") or "")[:500],
            }
            for row in desired_rows
        ],
        "project_keys": sorted(project_keys),
    }


def bridge_agent_inbox_file(*, workspace_id: int, user_id: int, relative_path: str) -> Path:
    relative = Path(str(relative_path or "").replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise APIError("CONTENT_BROWSER_AGENT_FILE_INVALID", "Invalid browser inbox path.", 400)
    project_key = relative.parts[0] if relative.parts else ""
    if not project_key:
        raise APIError("CONTENT_BROWSER_AGENT_FILE_INVALID", "Invalid browser inbox path.", 400)
    # Project ownership is checked by the caller before serving the file.
    root = (BROWSER_INBOX / f"workspace_{int(workspace_id)}").resolve()
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        raise APIError("CONTENT_BROWSER_AGENT_FILE_NOT_FOUND", "Browser inbox file not found.", 404)
    return target


def bridge_agent_inbox_manifest(db: Session, *, workspace_id: int, user_id: int) -> list[dict[str, Any]]:
    active_projects = (
        db.query(HermesContentFactoryProject)
        .filter(
            HermesContentFactoryProject.workspace_id == int(workspace_id),
            HermesContentFactoryProject.user_id == int(user_id),
            HermesContentFactoryProject.status.in_(("ready", "queued", "running", "retrying", "waiting_bridge")),
        )
        .all()
    )
    # A transient stage failure does not release its sticky browser slot. Keep
    # that project's files in the device manifest while self-heal is deciding
    # whether to retry; otherwise the agent can prune the files between the
    # failed attempt and the recovery attempt.
    available_bridges = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.status.in_(("active", "pending", "offline", "degraded")),
        )
        .all()
    )
    available_bridge_ids = {str(row.bridge_id) for row in available_bridges}
    assigned_project_ids = {
        int(project_id)
        for project_id in (row.active_project_id for row in available_bridges)
        if project_id is not None
    }
    known_ids = {int(project.id) for project in active_projects}
    if assigned_project_ids - known_ids:
        active_projects.extend(
            db.query(HermesContentFactoryProject)
            .filter(
                HermesContentFactoryProject.workspace_id == int(workspace_id),
                HermesContentFactoryProject.user_id == int(user_id),
                HermesContentFactoryProject.id.in_(assigned_project_ids - known_ids),
            )
            .all()
        )
    # A browser lease is intentionally released between automatic retries, so
    # active_project_id alone cannot identify every project whose files must
    # remain on the user's device. Preserve failed projects that still point at
    # a live sticky bridge. Pausing or deleting the project clears/excludes that
    # association and lets the agent prune the cache normally.
    failed_projects = (
        db.query(HermesContentFactoryProject)
        .filter(
            HermesContentFactoryProject.workspace_id == int(workspace_id),
            HermesContentFactoryProject.user_id == int(user_id),
            HermesContentFactoryProject.status == "failed",
        )
        .all()
    )
    for project in failed_projects:
        state = dict(project.state_json or {})
        bridge_id = str(state.get("browser_bridge_id") or "").strip()
        if bridge_id in available_bridge_ids and int(project.id) not in known_ids:
            active_projects.append(project)
            known_ids.add(int(project.id))
    root = BROWSER_INBOX / f"workspace_{int(workspace_id)}"
    files: list[dict[str, Any]] = []
    for project in active_projects:
        project_root = root / str(project.project_key)
        if not project_root.is_dir():
            continue
        for path in project_root.rglob("*"):
            if not path.is_file():
                continue
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
            files.append({
                "path": relative,
                "size": int(stat.st_size),
                "mtime": int(stat.st_mtime),
            })
            if len(files) >= 1000:
                return files
    return files


def ensure_project_assets_in_browser_inbox(
    db: Session, *, project: HermesContentFactoryProject, assets: list[HermesContentFactoryAsset] | None = None,
) -> list[HermesContentFactoryAsset]:
    """Mirror project assets into the server-side inbox consumed by browser agents.

    This is intentionally idempotent and runs before every browser stage, so
    old projects and product-library assets created before bridge support still
    become uploadable on the user's own computer.
    """
    selected = assets
    if selected is None:
        selected = (
            db.query(HermesContentFactoryAsset)
            .filter(HermesContentFactoryAsset.project_id == int(project.id))
            .order_by(HermesContentFactoryAsset.id.asc())
            .all()
        )
    bridge_dir = BROWSER_INBOX / f"workspace_{int(project.workspace_id)}" / str(project.project_key)
    _ensure_storage_dir(bridge_dir)
    updated: list[HermesContentFactoryAsset] = []
    for asset in selected:
        source = Path(str(asset.file_path or ""))
        if not source.is_file():
            continue
        meta = dict(asset.meta_json or {})
        disk_name = _safe_name(Path(str(asset.file_path or asset.original_name or "file")).name)
        try:
            variant_index = int(
                meta.get("content_factory_variant_index")
                or meta.get("variant_index")
                or 0
            )
        except (TypeError, ValueError):
            variant_index = 0
        if (
            variant_index > 0
            and str(asset.stage or "").upper()
            in {"VISUAL_PREVIEW", "CREATIVE_REVIEW", "FINAL_ASSETS", "VIDEO_PROMPTS"}
            and not disk_name.lower().startswith(f"v{variant_index:02d}-")
        ):
            disk_name = f"v{variant_index:02d}-{disk_name}"
        target = bridge_dir / disk_name
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
            _mark_group_writable(target)
        if meta.get("bridge_path") != str(target):
            meta["bridge_path"] = str(target)
            meta["browser_inbox_relative"] = (
                f"workspace_{int(project.workspace_id)}/{project.project_key}/{disk_name}"
            )
            asset.meta_json = meta
            db.add(asset)
        updated.append(asset)
    db.flush()
    return updated


def ensure_bridge_agent_file_access(db: Session, *, workspace_id: int, user_id: int, relative_path: str) -> Path:
    normalized = str(relative_path or "").replace("\\", "/").lstrip("/")
    project_key = normalized.split("/", 1)[0]
    project = (
        db.query(HermesContentFactoryProject.id)
        .filter(
            HermesContentFactoryProject.workspace_id == int(workspace_id),
            HermesContentFactoryProject.user_id == int(user_id),
            HermesContentFactoryProject.project_key == project_key,
        )
        .one_or_none()
    )
    if project is None:
        raise APIError("CONTENT_BROWSER_AGENT_FILE_FORBIDDEN", "Browser inbox file is not owned by this user.", 403)
    return bridge_agent_inbox_file(workspace_id=workspace_id, user_id=user_id, relative_path=normalized)


def _bridge_installer_filename(bridge: HermesBrowserBridge) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(bridge.bridge_id or "bridge")).strip("-")[:48] or "bridge"
    return f"Install-MYUPONA-HermesBridge-{suffix}.ps1"


def _bridge_install_command(bridge: HermesBrowserBridge) -> str:
    return f"powershell -NoProfile -ExecutionPolicy Bypass -File .\\{_bridge_installer_filename(bridge)}"


def _bridge_installer_script(bridge: HermesBrowserBridge) -> str:
    server_port = int(bridge.server_port or _port_from_url(bridge.cdp_url or "") or BRIDGE_PORT_START)
    local_port = int(dict(bridge.meta_json or {}).get("local_port") or BRIDGE_LOCAL_CDP_PORT)
    bridge_id = str(bridge.bridge_id or "bridge")
    config_json = json.dumps(
        {
            "BridgeId": bridge_id,
            "Server": BRIDGE_SSH_TARGET,
            "ServerPort": server_port,
            "LocalPort": local_port,
            "InboxRoot": str(bridge.inbox_root or WINDOWS_INBOX),
            "ServerInboxRoot": BROWSER_INBOX.as_posix(),
            "WorkspaceId": int(bridge.workspace_id),
            "DeviceId": str(bridge.device_id or ""),
        },
        ensure_ascii=False,
        indent=2,
    )
    return f'''$ErrorActionPreference = "Stop"

$BridgeId = {_ps_single_quote(bridge_id)}
$InstallRoot = Join-Path $env:LOCALAPPDATA ("MYUPONA\\HermesBridge\\" + $BridgeId)
$ConfigFile = Join-Path $InstallRoot "bridge.config.json"
$RunnerFile = Join-Path $InstallRoot "Start-HermesBridge.ps1"
$TaskName = "MYUPONA Hermes Browser Bridge " + $BridgeId.Substring(0, [Math]::Min(10, $BridgeId.Length))

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
@'
{config_json}
'@ | Set-Content -LiteralPath $ConfigFile -Encoding UTF8

@'
$ErrorActionPreference = "Continue"
$ConfigFile = Join-Path $PSScriptRoot "bridge.config.json"
$Config = Get-Content -Raw -LiteralPath $ConfigFile | ConvertFrom-Json
$Root = $PSScriptRoot
$Profile = Join-Path $Root "ChromeProfile"
$Inbox = [string]$Config.InboxRoot
$StatusFile = Join-Path $Root "HermesBridge.status"
$LogFile = Join-Path $Root "HermesBridge.log"
$TunnelErrorFile = Join-Path $Root "HermesBridge.ssh-error.log"

New-Item -ItemType Directory -Force -Path $Profile | Out-Null
New-Item -ItemType Directory -Force -Path $Inbox | Out-Null

function Set-BridgeStatus([string]$Status) {{
    $line = "$(Get-Date -Format o) $Status"
    $line | Set-Content -LiteralPath $StatusFile -Encoding UTF8
    $line | Add-Content -LiteralPath $LogFile -Encoding UTF8
}}

function Get-ChromePath {{
    $candidates = @(
        "$env:ProgramFiles\\Google\\Chrome\\Application\\chrome.exe",
        "${{env:ProgramFiles(x86)}}\\Google\\Chrome\\Application\\chrome.exe",
        "$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe"
    )
    foreach ($item in $candidates) {{
        if ($item -and (Test-Path -LiteralPath $item)) {{ return $item }}
    }}
    throw "Google Chrome was not found."
}}

function Test-HermesChrome {{
    try {{
        $null = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $Config.LocalPort + "/json/version") -TimeoutSec 2
        return $true
    }} catch {{
        return $false
    }}
}}

function Start-HermesChrome {{
    if (Test-HermesChrome) {{ return $true }}
    $chrome = Get-ChromePath
    Set-BridgeStatus "starting-browser"
    Start-Process -FilePath $chrome -ArgumentList @(
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=$($Config.LocalPort)",
        "--remote-allow-origins=*",
        "--user-data-dir=$Profile",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "https://chatgpt.com/"
    ) | Out-Null
    $deadline = (Get-Date).AddSeconds(45)
    do {{
        Start-Sleep -Seconds 1
        if (Test-HermesChrome) {{ return $true }}
    }} until ((Get-Date) -gt $deadline)
    return $false
}}

function Clear-StaleRemoteBridge {{
    Set-BridgeStatus "clearing-stale-remote-listener"
    & ssh.exe -o BatchMode=yes -o ConnectTimeout=10 $Config.Server "fuser -k $($Config.ServerPort)/tcp >/dev/null 2>&1 || true"
    return ($LASTEXITCODE -eq 0)
}}

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, ("Local\\MYUPONA_HermesBridge_" + $Config.BridgeId), [ref]$createdNew)
if (-not $createdNew) {{ exit 0 }}

try {{
    while ($true) {{
        try {{
            if (-not (Start-HermesChrome)) {{
                Set-BridgeStatus "browser-start-failed"
                Start-Sleep -Seconds 10
                continue
            }}
            [void](Clear-StaleRemoteBridge)
            Remove-Item -LiteralPath $TunnelErrorFile -Force -ErrorAction SilentlyContinue
            Set-BridgeStatus "starting-tunnel"
            $tunnel = Start-Process -FilePath "ssh.exe" -WindowStyle Hidden -PassThru -ArgumentList @(
                "-N",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-R", ("127.0.0.1:" + $Config.ServerPort + ":127.0.0.1:" + $Config.LocalPort),
                $Config.Server
            ) -RedirectStandardError $TunnelErrorFile
            Start-Sleep -Seconds 2
            if ($tunnel.HasExited) {{
                $detail = if (Test-Path -LiteralPath $TunnelErrorFile) {{ [string](Get-Content -Raw -LiteralPath $TunnelErrorFile) }} else {{ "unknown ssh error" }}
                Set-BridgeStatus ("tunnel-start-failed:" + $detail.Trim())
                Start-Sleep -Seconds 8
                continue
            }}
            Set-BridgeStatus "connected"
            $lastSync = [datetime]::MinValue
            while (-not $tunnel.HasExited) {{
                Start-Sleep -Seconds 3
                if (-not (Test-HermesChrome)) {{
                    Set-BridgeStatus "browser-disconnected"
                    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
                    break
                }}
                if (((Get-Date) - $lastSync).TotalSeconds -ge 10) {{
                    & scp.exe -q -r ($Config.Server + ":" + $Config.ServerInboxRoot + "/.") $Inbox
                    if ($LASTEXITCODE -eq 0) {{ $lastSync = Get-Date }}
                }}
            }}
        }} catch {{
            Set-BridgeStatus ("loop-error:" + $_.Exception.Message)
            Start-Sleep -Seconds 10
        }}
        Set-BridgeStatus "reconnecting"
        Start-Sleep -Seconds 3
    }}
}} finally {{
    Set-BridgeStatus "stopped"
    if ($tunnel -and -not $tunnel.HasExited) {{ Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue }}
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}}
'@ | Set-Content -LiteralPath $RunnerFile -Encoding UTF8

$Action = New-ScheduledTaskAction `
    -Execute "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerFile`""
$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$WatchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($LogonTrigger, $WatchdogTrigger) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Starts Chrome CDP and maintains the reverse SSH tunnel for GMV Ops Hermes." `
    -Force | Out-Null

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process |
    Where-Object {{ $_.CommandLine -like "*$RunnerFile*" -and $_.Name -eq "powershell.exe" }} |
    ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}

Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName $TaskName
Write-Output ("Installed and started: " + $TaskName)
Write-Output ("Status file: " + (Join-Path $InstallRoot "HermesBridge.status"))
'''


def register_browser_bridge(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    device_id: str,
    device_name: str | None = None,
    cdp_url: str | None = None,
    inbox_root: str | None = None,
    outbox_root: str | None = None,
    browser: str | None = None,
    load_json: dict[str, Any] | None = None,
) -> HermesBrowserBridge:
    device = str(device_id or "").strip()[:128]
    if not device:
        raise APIError("CONTENT_BROWSER_DEVICE_REQUIRED", "Browser bridge device_id is required.", 400)
    normalized_cdp = (cdp_url or "").strip().rstrip("/")
    row = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.user_id == int(user_id),
            HermesBrowserBridge.device_id == device,
        )
        .one_or_none()
    )
    if row is None:
        if normalized_cdp:
            _ensure_bridge_endpoint_not_reused(
                db,
                workspace_id=workspace_id,
                user_id=user_id,
                device_id=device,
                cdp_url=normalized_cdp,
            )
        port = _port_from_url(normalized_cdp) or _allocate_bridge_port(db)
        row = HermesBrowserBridge(
            bridge_id=f"br_{uuid4().hex}",
            workspace_id=int(workspace_id),
            user_id=int(user_id),
            device_id=device,
            device_name=(device_name or "").strip()[:255] or None,
            cdp_url=(normalized_cdp or f"http://127.0.0.1:{port}").strip().rstrip("/"),
            server_port=port,
            inbox_root=(inbox_root or WINDOWS_INBOX).strip() or WINDOWS_INBOX,
            outbox_root=(outbox_root or "").strip() or None,
            browser=(browser or "").strip()[:255] or None,
            status="active",
            last_seen_at=_now(),
            load_json=load_json or {},
            meta_json={"connect_command": _bridge_command(port)},
        )
        db.add(row)
    else:
        if normalized_cdp:
            _ensure_bridge_endpoint_not_reused(
                db,
                workspace_id=workspace_id,
                user_id=user_id,
                device_id=device,
                cdp_url=normalized_cdp,
                row_id=int(row.id),
            )
            row.cdp_url = normalized_cdp
            row.server_port = _port_from_url(row.cdp_url) or row.server_port
        if device_name is not None:
            row.device_name = device_name.strip()[:255] or row.device_name
        if inbox_root is not None:
            row.inbox_root = inbox_root.strip() or row.inbox_root
        if outbox_root is not None:
            row.outbox_root = outbox_root.strip() or None
        if browser is not None:
            row.browser = browser.strip()[:255] or row.browser
        row.status = "active"
        row.last_seen_at = _now()
        row.load_json = load_json or row.load_json or {}
        meta = dict(row.meta_json or {})
        meta["account_device_bound"] = True
        meta.pop("account_device_unbound_at", None)
        if row.server_port:
            meta["connect_command"] = _bridge_command(int(row.server_port))
        row.meta_json = meta
        db.add(row)
    db.flush()
    meta = dict(row.meta_json or {})
    meta["account_device_bound"] = True
    meta.pop("account_device_unbound_at", None)
    if row.server_port:
        meta["connect_command"] = _bridge_command(int(row.server_port))
    meta["installer_filename"] = _bridge_installer_filename(row)
    meta["install_command"] = _bridge_install_command(row)
    row.meta_json = meta
    db.add(row)
    db.flush()
    return row


def _bridge_out(bridge: HermesBrowserBridge, *, connected: bool, browser: str | None = None, detail: str | None = None, queue: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    meta = dict(bridge.meta_json or {})
    active_project = (queue or [None])[0] if queue else None
    occupied = bool(connected and bridge.active_project_id)
    return {
        "bridge_id": bridge.bridge_id,
        "slot": bridge.bridge_id,
        "workspace_id": int(bridge.workspace_id),
        "user_id": int(bridge.user_id) if bridge.user_id is not None else None,
        "device_id": bridge.device_id,
        "agent_device_id": meta.get("agent_device_id") or _bridge_base_device_id(bridge),
        "device_name": bridge.device_name,
        "slot_index": meta.get("slot_index"),
        "url": bridge.cdp_url,
        "connected": bool(connected),
        "browser": browser or bridge.browser,
        "detail": detail,
        "usage_status": "occupied" if occupied else "free",
        "active_project": active_project,
        "active_project_id": int(bridge.active_project_id) if occupied else None,
        "active_stage_id": int(bridge.active_stage_id) if occupied and bridge.active_stage_id else None,
        "queue": queue or [],
        "queue_depth": len(queue or []),
        "inbox_root": bridge.inbox_root,
        "server_port": bridge.server_port,
        "lease_expires_at": bridge.lease_expires_at,
        "last_seen_at": bridge.last_seen_at,
        "agent_last_heartbeat_at": meta.get("agent_last_heartbeat_at"),
        "agent_error": dict(bridge.load_json or {}).get("agent_error"),
        "auth_status": _bridge_auth_status(bridge),
        "account_name": meta.get("chatgpt_account_name") or None,
        "chatgpt_page_url": meta.get("chatgpt_page_url") or None,
        "manual_pool_slot": bool(meta.get("manual_pool_slot")),
        "connect_command": meta.get("connect_command"),
        "installer_filename": meta.get("installer_filename") or _bridge_installer_filename(bridge),
        "installer_script": _bridge_installer_script(bridge),
        "install_command": meta.get("install_command") or _bridge_install_command(bridge),
    }


def _active_bridge_usage(db: Session, *, workspace_id: int, user_id: int | None = None) -> dict[str, list[dict[str, Any]]]:
    query = (
        db.query(HermesContentFactoryStage, HermesContentFactoryProject)
        .join(HermesContentFactoryProject, HermesContentFactoryProject.id == HermesContentFactoryStage.project_id)
        .filter(HermesContentFactoryStage.status.in_(("queued", "running", "retrying")))
        .filter(HermesContentFactoryProject.workspace_id == int(workspace_id))
        .filter(HermesContentFactoryProject.status != "paused")
    )
    if user_id is not None:
        query = query.filter(HermesContentFactoryProject.user_id == int(user_id))
    usage: dict[str, list[dict[str, Any]]] = {}
    for stage, project in query.order_by(HermesContentFactoryStage.id.asc()).all():
        if bool(dict(project.config_json or {}).get("manual_paused", False)):
            continue
        stage_input = dict(stage.input_json or {})
        bridge_id = str(stage_input.get("browser_bridge_id") or dict(project.state_json or {}).get("browser_bridge_id") or "").strip()
        if not bridge_id:
            continue
        usage.setdefault(bridge_id, []).append({
            "project_id": int(project.id),
            "project_key": project.project_key,
            "title": project.title,
            "product_name": project.product_name,
            "project_status": project.status,
            "current_stage": project.current_stage,
            "stage_id": int(stage.id),
            "stage": stage.stage,
            "stage_status": stage.status,
            "attempt": int(stage.attempt or 0),
            "queue": str(stage_input.get("queue") or project_hermes_queue(project)),
        })
    return usage


def _bridge_matches_preferred_device(bridge: HermesBrowserBridge, preferred_device_id: str | None) -> bool:
    preferred = str(preferred_device_id or "").strip()
    if not preferred:
        return True
    device_id = str(bridge.device_id or "")
    meta_device = str(dict(bridge.meta_json or {}).get("agent_device_id") or "")
    return (
        device_id == preferred
        or device_id.startswith(preferred[:96] + "::slot:")
        or meta_device == preferred
    )


def _bridge_base_device_online(db: Session, bridge: HermesBrowserBridge) -> bool:
    """Return whether this slot's bound local Agent is still heartbeating."""
    base_device_id = _bridge_base_device_id(bridge)
    if not base_device_id:
        return False
    return any(
        _bridge_device_bound(row) and _bridge_agent_recent(row)
        for row in _account_device_rows(
            db,
            workspace_id=int(bridge.workspace_id),
            user_id=int(bridge.user_id or 0),
            device_id=base_device_id,
            include_retired=True,
        )
    )


def _retired_locked_slot_can_rebind(
    db: Session,
    *,
    project: HermesContentFactoryProject,
    bridge: HermesBrowserBridge,
) -> bool:
    """Permit one same-device replacement only after the Agent retires a slot.

    A running or temporarily offline slot remains sticky. Rebinding is allowed
    only when the local Agent explicitly confirmed that the old managed Chrome
    process stopped, the slot is unleased, and the same physical device is still
    online. This prevents both permanent deadlocks and cross-device migration.
    """
    meta = dict(bridge.meta_json or {})
    return bool(
        str(bridge.status or "").lower() == "retired"
        and str(meta.get("retired_reason") or "").lower() == "agent_confirmed_slot_stopped"
        and bool(meta.get("agent_managed"))
        and _bridge_device_bound(bridge)
        and bridge.active_project_id in (None, int(project.id))
        and _bridge_base_device_online(db, bridge)
    )


def _request_locked_agent_slot_restart(
    db: Session,
    *,
    project: HermesContentFactoryProject,
    bridge: HermesBrowserBridge,
    now: datetime,
    reason: str,
) -> bool:
    """Revive the exact Agent slot/profile owned by an interrupted project."""
    meta = dict(bridge.meta_json or {})
    if (
        not bool(meta.get("agent_managed"))
        or not _bridge_device_bound(bridge)
        or bridge.active_project_id not in (None, int(project.id))
        or not _bridge_base_device_online(db, bridge)
    ):
        return False

    bridge.status = "pending"
    bridge.active_project_id = int(project.id)
    bridge.active_stage_id = None
    bridge.lease_expires_at = now + timedelta(hours=BRIDGE_LEASE_HOURS)
    # Keep stale-slot cleanup from retiring the row before the next Agent poll.
    bridge.last_seen_at = now
    for key in (
        "retired_at",
        "retired_reason",
        "agent_slot_mode_requested_at",
        "agent_slot_mode_project_id",
    ):
        meta.pop(key, None)
    # A dormant API-only slot is deliberately disconnected.  Browser fallback
    # must wake this exact profile, never allocate a different idle slot.
    meta["agent_slot_mode"] = "active"
    meta["same_slot_restart_requested_at"] = now.isoformat()
    meta["same_slot_restart_project_id"] = int(project.id)
    meta["same_slot_restart_reason"] = str(reason)[:800]
    bridge.meta_json = meta
    load = dict(bridge.load_json or {})
    load.update({
        "agent_error": f"server requested same-slot restart: {str(reason)[:420]}",
        "restart_required": True,
    })
    bridge.load_json = load
    db.add(bridge)

    state = dict(project.state_json or {})
    base_device_id = _bridge_base_device_id(bridge)
    state.update({
        "browser_bridge_id": str(bridge.bridge_id),
        "browser_device_id": str(bridge.device_id or ""),
        "browser_device_name": str(bridge.device_name or ""),
        "browser_cdp_url": str(bridge.cdp_url or ""),
        "browser_inbox_root": str(bridge.inbox_root or ""),
        "browser_lease_expires_at": bridge.lease_expires_at.isoformat(),
        "browser_bridge_lock_policy": "project_sticky_slot",
        "browser_slot_requested_at": now.isoformat(),
        "browser_slot_restart_requested_at": now.isoformat(),
        "browser_cdp_recovering": True,
        "browser_device_offline": False,
        "browser_slot_mode": "active",
        "browser_slot_wake_requested_at": now.isoformat(),
    })
    if base_device_id:
        state["preferred_browser_device_id"] = base_device_id
    project.state_json = state
    project.status = "waiting_bridge"
    project.last_error = "Hermes is restarting this project's original Chrome/CDP slot and will resume automatically."
    db.add(project)
    db.flush()
    return True


def _acquire_project_bridge(db: Session, *, project: HermesContentFactoryProject, user_id: int) -> HermesBrowserBridge:
    now = _now()
    reconcile_bridge_project_leases(
        db,
        workspace_id=int(project.workspace_id),
        user_id=int(user_id),
    )
    state = dict(project.state_json or {})
    bridge_id = str(state.get("browser_bridge_id") or "").strip()
    preferred_device_id = str(state.get("preferred_browser_device_id") or "").strip()
    bridge = None
    locked_bridge_unavailable_reason = ""
    retired_slot_rebind_allowed = False
    if bridge_id:
        bridge = (
            db.query(HermesBrowserBridge)
            .filter(
                HermesBrowserBridge.bridge_id == bridge_id,
                HermesBrowserBridge.workspace_id == int(project.workspace_id),
                HermesBrowserBridge.user_id == int(user_id),
            )
            .one_or_none()
        )
        if bridge is None:
            locked_bridge_unavailable_reason = "locked browser slot row is missing"
        elif _retired_locked_slot_can_rebind(db, project=project, bridge=bridge):
            retired_slot_rebind_allowed = True
            base_device_id = _bridge_base_device_id(bridge)
            old_bridge_id = str(bridge.bridge_id or "")
            bridge = None
            locked_bridge_unavailable_reason = ""
            for key in (
                "browser_bridge_id", "browser_device_id", "browser_device_name",
                "browser_cdp_url", "browser_inbox_root", "browser_lease_expires_at",
                "browser_bridge_unavailable_at", "browser_bridge_unavailable_reason",
                "browser_slot_restart_requested_at", "browser_cdp_recovering",
            ):
                state.pop(key, None)
            if base_device_id:
                state["preferred_browser_device_id"] = base_device_id[:128]
            state["browser_retired_slot_replaced_at"] = now.isoformat()
            state["browser_retired_slot_bridge_id"] = old_bridge_id
            project.state_json = state
            db.add(project)
        elif not _bridge_device_bound(bridge):
            locked_bridge_unavailable_reason = "locked browser device is no longer bound to this account"
        elif not _bridge_connected(bridge):
            if _recover_degraded_bridge_if_reachable(bridge, now=now):
                db.add(bridge)
            else:
                locked_bridge_unavailable_reason = "locked browser slot is offline"
        if bridge is not None and preferred_device_id and not _bridge_matches_preferred_device(bridge, preferred_device_id):
            if bridge.active_project_id == int(project.id):
                bridge.active_project_id = None
                bridge.active_stage_id = None
                bridge.lease_expires_at = None
                db.add(bridge)
            locked_bridge_unavailable_reason = "locked browser slot is not on the original device"
            bridge = None
        if bridge is not None and locked_bridge_unavailable_reason and _request_locked_agent_slot_restart(
            db,
            project=project,
            bridge=bridge,
            now=now,
            reason=locked_bridge_unavailable_reason,
        ):
            raise APIError(
                "CONTENT_BROWSER_BRIDGE_OFFLINE",
                "The original browser slot is restarting on this user's device. Hermes will resume automatically.",
                409,
            )
        if (bridge is None or locked_bridge_unavailable_reason) and not retired_slot_rebind_allowed:
            state["browser_bridge_lock_policy"] = "project_sticky_slot"
            state["browser_bridge_unavailable_at"] = now.isoformat()
            state["browser_bridge_unavailable_reason"] = locked_bridge_unavailable_reason or "locked browser slot unavailable"
            project.state_json = state
            db.add(project)
            raise APIError(
                "CONTENT_BROWSER_LOCKED_SLOT_UNAVAILABLE",
                (
                    "This project is locked to its original browser slot, but that slot is unavailable. "
                    "Reconnect the same local bridge slot, or pause/delete the project to release it."
                ),
                409,
            )
        if bridge is not None:
            if not _bridge_login_ready(bridge):
                state["browser_slot_wait_error_code"] = "CONTENT_BROWSER_LOGIN_REQUIRED"
                state["browser_slot_wait_error_at"] = now.isoformat()
                project.state_json = state
                db.add(project)
                raise APIError(
                    "CONTENT_BROWSER_LOGIN_REQUIRED",
                    "This project's browser slot is connected, but ChatGPT is not logged in yet. Log in in that slot and Hermes will resume automatically.",
                    409,
                )
            recovered_state = False
            for key in ("browser_bridge_unavailable_at", "browser_bridge_unavailable_reason"):
                if key in state:
                    state.pop(key, None)
                    recovered_state = True
            if recovered_state:
                state["browser_bridge_recovered_at"] = now.isoformat()
                project.state_json = state
                db.add(project)
    if not preferred_device_id and bridge is not None:
        preferred_device_id = str(
            dict(bridge.meta_json or {}).get("agent_device_id")
            or bridge.device_id
            or ""
        ).strip()
        if preferred_device_id:
            state["preferred_browser_device_id"] = preferred_device_id[:128]
            project.state_json = state
            db.add(project)
    if bridge is None:
        if not preferred_device_id:
            preferred_device_id, selection_required = _effective_browser_device_id(
                browser_devices(db, workspace_id=int(project.workspace_id), user_id=int(user_id))
            )
            if selection_required:
                raise APIError(
                    "CONTENT_BROWSER_DEVICE_SELECTION_REQUIRED",
                    "多个已绑定设备在线，请先在内容工厂选择本项目使用的设备。",
                    409,
                )
            if preferred_device_id:
                state["preferred_browser_device_id"] = preferred_device_id[:128]
                project.state_json = state
                db.add(project)
        candidates = (
            db.query(HermesBrowserBridge)
            .filter(
                HermesBrowserBridge.workspace_id == int(project.workspace_id),
                HermesBrowserBridge.user_id == int(user_id),
                HermesBrowserBridge.status == "active",
                HermesBrowserBridge.last_seen_at >= _bridge_alive_cutoff(),
            )
            .filter((HermesBrowserBridge.active_project_id.is_(None)) | (HermesBrowserBridge.active_project_id == int(project.id)))
            .order_by(HermesBrowserBridge.active_project_id.desc(), HermesBrowserBridge.last_seen_at.desc())
            .all()
        )
        candidates = [item for item in candidates if _bridge_device_bound(item)]
        if preferred_device_id:
            candidates = [item for item in candidates if _bridge_matches_preferred_device(item, preferred_device_id)]
        connected_candidates = [item for item in candidates if _bridge_connected(item)]
        if not connected_candidates:
            recovered_candidates: list[HermesBrowserBridge] = []
            for item in candidates:
                if not _bridge_recently_degraded(item) or not _bridge_degraded_old_enough_to_probe(item):
                    continue
                reachable, browser, _probe_error = _probe_bridge(item)
                if not reachable:
                    continue
                item.status = "active"
                item.browser = browser or item.browser
                item.last_seen_at = now
                _clear_bridge_degraded_marker(item)
                db.add(item)
                recovered_candidates.append(item)
            connected_candidates = recovered_candidates
        login_ready_candidates = [item for item in connected_candidates if _bridge_login_ready(item)]
        if connected_candidates and not login_ready_candidates:
            raise APIError(
                "CONTENT_BROWSER_LOGIN_REQUIRED",
                "The selected device has browser slots, but none is logged in to ChatGPT. Log in to an idle slot first.",
                409,
            )
        candidates = login_ready_candidates
        bridge = candidates[0] if candidates else None
        if bridge is not None and not preferred_device_id:
            preferred_device_id = str(
                dict(bridge.meta_json or {}).get("agent_device_id")
                or bridge.device_id
                or ""
            ).strip()
            if preferred_device_id:
                state["preferred_browser_device_id"] = preferred_device_id[:128]
                project.state_json = state
                db.add(project)
    if bridge is None:
        raise APIError("CONTENT_BROWSER_BRIDGE_REQUIRED", "请先在当前电脑创建并连接浏览器桥，再运行内容工厂项目。", 409)
    reachable, browser, probe_error = _probe_bridge(bridge)
    if not reachable:
        bridge.status = "offline"
        bridge.last_seen_at = None
        db.add(bridge)
        raise APIError("CONTENT_BROWSER_BRIDGE_OFFLINE", f"当前电脑的浏览器桥不可访问：{probe_error or 'CDP unavailable'}", 409)
    bridge.status = "active"
    bridge.browser = browser or bridge.browser
    bridge.last_seen_at = now

    load = _server_load_snapshot()
    active_count = (
        db.query(HermesBrowserBridge)
        .filter(
            HermesBrowserBridge.status == "active",
            HermesBrowserBridge.last_seen_at >= _bridge_alive_cutoff(),
            HermesBrowserBridge.active_project_id.isnot(None),
            HermesBrowserBridge.lease_expires_at.isnot(None),
            HermesBrowserBridge.lease_expires_at > now,
        )
        .count()
    )
    if bridge.active_project_id not in (None, int(project.id)) and bridge.lease_expires_at and bridge.lease_expires_at > now:
        raise APIError("CONTENT_BROWSER_BRIDGE_OCCUPIED", "当前电脑的浏览器桥正在运行另一个项目，请等待或暂停后再运行。", 409)
    if bridge.active_project_id in (None, int(project.id)) and int(active_count) >= int(load.get("capacity") or 1) and bridge.active_project_id != int(project.id):
        raise APIError("CONTENT_BROWSER_CAPACITY_FULL", "服务器当前负载较高，暂时无法创建新的浏览器 slot。项目请稍后恢复或重新排队。", 429)

    bridge.active_project_id = int(project.id)
    bridge.lease_expires_at = now + timedelta(hours=BRIDGE_LEASE_HOURS)
    db.add(bridge)
    state.pop("browser_slot_requested_at", None)
    state.pop("browser_slot_request_stage", None)
    state["browser_bridge_id"] = bridge.bridge_id
    state["browser_device_id"] = bridge.device_id
    state["browser_device_name"] = bridge.device_name
    state["browser_cdp_url"] = bridge.cdp_url
    state["browser_inbox_root"] = bridge.inbox_root
    state["browser_lease_expires_at"] = bridge.lease_expires_at.isoformat()
    state["browser_bridge_lock_policy"] = "project_sticky_slot"
    state["server_load"] = load
    project.state_json = state
    db.add(project)
    return bridge


def _release_expired_bridge_leases(db: Session, *, now: datetime | None = None) -> int:
    current = now or _now()
    rows = (
        db.query(HermesBrowserBridge)
        .filter(HermesBrowserBridge.lease_expires_at.isnot(None), HermesBrowserBridge.lease_expires_at <= current)
        .all()
    )
    for bridge in rows:
        project = db.get(HermesContentFactoryProject, int(bridge.active_project_id)) if bridge.active_project_id else None
        if project is not None and not _project_bridge_lock_terminal(project):
            bridge.lease_expires_at = current + timedelta(hours=BRIDGE_LEASE_HOURS)
            db.add(bridge)
            continue
        bridge.active_project_id = None
        bridge.active_stage_id = None
        bridge.lease_expires_at = None
        db.add(bridge)
    return len(rows)


def visible_project_query(db: Session, workspace_id: int, user_id: int):
    return db.query(HermesContentFactoryProject).filter(
        HermesContentFactoryProject.workspace_id == int(workspace_id),
        HermesContentFactoryProject.user_id == int(user_id),
        HermesContentFactoryProject.status != "deleted",
    )


def get_project(db: Session, workspace_id: int, user_id: int, project_key: str) -> HermesContentFactoryProject:
    project = visible_project_query(db, workspace_id, user_id).filter(HermesContentFactoryProject.project_key == project_key).one_or_none()
    if project is None:
        raise APIError("CONTENT_PROJECT_NOT_FOUND", "Content factory project not found.", 404)
    return project


def _normalize_library_key(*parts: str | None) -> str:
    normalized = []
    for part in parts:
        value = unicodedata.normalize("NFKC", str(part or "")).strip().lower()
        value = re.sub(r"\s+", " ", value)
        value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value)
        normalized.append(value.strip("-"))
    return "::".join(item for item in normalized if item)[:191]


def _brand_name_from_config(config: dict[str, Any] | None) -> str:
    return str((config or {}).get("brand_name") or "").strip()


def list_products(db: Session, *, workspace_id: int) -> list[HermesContentProduct]:
    return (
        db.query(HermesContentProduct)
        .filter(HermesContentProduct.workspace_id == int(workspace_id), HermesContentProduct.status == "active")
        .order_by(HermesContentProduct.updated_at.desc(), HermesContentProduct.id.desc())
        .all()
    )


def get_product(db: Session, *, workspace_id: int, product_id: int) -> HermesContentProduct:
    product = (
        db.query(HermesContentProduct)
        .filter(HermesContentProduct.workspace_id == int(workspace_id), HermesContentProduct.id == int(product_id))
        .one_or_none()
    )
    if product is None:
        raise APIError("CONTENT_PRODUCT_NOT_FOUND", "Product not found in this company library.", 404)
    return product


def product_out(db: Session, product: HermesContentProduct) -> dict[str, Any]:
    assets = (
        db.query(HermesContentProductAsset)
        .filter(HermesContentProductAsset.product_id == product.id)
        .order_by(HermesContentProductAsset.id.asc())
        .all()
    )
    return {
        "id": product.id,
        "product_key": product.product_key,
        "workspace_id": product.workspace_id,
        "user_id": product.user_id,
        "brand_name": product.brand_name,
        "product_name": product.product_name,
        "market": product.market,
        "product_brief": product.product_brief,
        "facts_json": product.facts_json,
        "status": product.status,
        "meta_json": product.meta_json,
        "created_at": product.created_at,
        "updated_at": product.updated_at,
        "assets": assets,
    }


_PRODUCT_LIBRARY_VOLATILE_TEXT_PATTERNS = (
    re.compile(r"[$€£¥]\s*\d", re.I),
    re.compile(r"\b(?:usd|cny|rmb)\s*\d", re.I),
    re.compile(r"\d+(?:\.\d+)?\s*(?:美元|元)(?:\b|。|，|,|$)", re.I),
    re.compile(
        r"(?:价格|售价|新客|立减|促销|折扣|优惠券|包邮|满减|限时|特价|买一送一|"
        r"\bprice\b|\bnew\s+customer\b|\bdiscount\b|\bcoupon\b|\bpromot\w*\b|\bpricing\b|"
        r"\bfree\s+shipping\b|\blimited[-\s]?time\b|\bsale\s+price\b)",
        re.I,
    ),
    re.compile(
        r"(?:\d+\s*秒|\d+\s*:\s*\d+|竖屏|横屏|节奏|钩子|对白|口型|快切|镜头|"
        r"\btiktok\b|\binstagram\b|\byoutube\b|\bvoiceover\b|\baspect\s+ratio\b)",
        re.I,
    ),
)
_PRODUCT_LIBRARY_VOLATILE_FACT_KEYS = {
    "price",
    "pricing",
    "promotion",
    "promotions",
    "discount",
    "discounts",
    "coupon",
    "coupons",
    "offer",
    "offers",
    "current promotion",
    "current price",
    "价格",
    "促销",
    "折扣",
    "优惠",
    "优惠券",
}
_PRODUCT_LIBRARY_COMMERCIAL_TEXT_PATTERN = re.compile(
    r"[$€£¥]\s*\d|\b(?:usd|cny|rmb)\s*\d|"
    r"(?:价格|售价|新客|立减|促销|折扣|优惠券|包邮|满减|限时|特价|"
    r"\bprice\b|\bnew\s+customer\b|\bdiscount\b|\bcoupon\b|\bpromot\w*\b|\bpricing\b|"
    r"\bfree\s+shipping\b|\blimited[-\s]?time\b|\bsale\s+price\b)",
    re.I,
)
_PRODUCT_LIBRARY_DROP = object()


def normalize_product_attribute_brief(value: str | None) -> str | None:
    """Keep the shared product library limited to durable product attributes."""

    normalized = str(value or "").strip()
    if not normalized:
        return None
    if any(pattern.search(normalized) for pattern in _PRODUCT_LIBRARY_VOLATILE_TEXT_PATTERNS):
        raise APIError(
            "CONTENT_PRODUCT_ATTRIBUTES_ONLY",
            "商品库只保存稳定商品属性；价格、促销、包邮、视频时长和画面创意请在具体项目中说明。",
            400,
        )
    return normalized


def _sanitize_product_facts_value(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[_\-]+", " ", str(key)).strip().lower()
            if normalized_key in _PRODUCT_LIBRARY_VOLATILE_FACT_KEYS:
                continue
            child = _sanitize_product_facts_value(item)
            if child is not _PRODUCT_LIBRARY_DROP:
                cleaned[str(key)] = child
        return cleaned
    if isinstance(value, list):
        return [
            child
            for item in value
            if (child := _sanitize_product_facts_value(item))
            is not _PRODUCT_LIBRARY_DROP
        ]
    if isinstance(value, str) and _PRODUCT_LIBRARY_COMMERCIAL_TEXT_PATTERN.search(value):
        return _PRODUCT_LIBRARY_DROP
    return value


def sanitize_product_facts_json(value: Any) -> Any:
    """Drop volatile commercial fields from imported product-fact envelopes."""

    cleaned = _sanitize_product_facts_value(value)
    return None if cleaned is _PRODUCT_LIBRARY_DROP else cleaned


def is_product_facts_project(project: HermesContentFactoryProject) -> bool:
    return dict(project.config_json or {}).get("purpose") == "product_facts"


def create_product(
    db: Session, *, workspace_id: int, user_id: int, brand_name: str, product_name: str,
    market: str = "US", product_brief: str | None = None, facts_json: dict[str, Any] | None = None,
) -> HermesContentProduct:
    brand = brand_name.strip()
    if not brand:
        raise APIError("CONTENT_PRODUCT_BRAND_REQUIRED", "Brand name is required.", 400)
    product = product_name.strip()
    selected_market = market.strip() or "US"
    stable_brief = normalize_product_attribute_brief(product_brief)
    stable_facts = sanitize_product_facts_json(facts_json) if facts_json else facts_json
    product_key = _normalize_library_key(brand, product, selected_market)
    row = (
        db.query(HermesContentProduct)
        .filter(HermesContentProduct.workspace_id == int(workspace_id), HermesContentProduct.product_key == product_key)
        .one_or_none()
    )
    if row is None:
        row = HermesContentProduct(
            workspace_id=int(workspace_id), user_id=int(user_id), product_key=product_key,
            brand_name=brand, product_name=product, market=selected_market,
            product_brief=stable_brief,
            facts_json=stable_facts,
            meta_json={},
        )
        db.add(row)
    else:
        row.brand_name = brand
        row.product_name = product
        row.market = selected_market
        if product_brief is not None:
            row.product_brief = stable_brief
        if stable_facts:
            row.facts_json = stable_facts
        row.status = "active"
    db.flush()
    _ensure_storage_dir(STORAGE_ROOT / "product_library" / f"workspace_{workspace_id}" / row.product_key)
    return row


def update_product(
    db: Session, *, product: HermesContentProduct, brand_name: str | None = None,
    product_name: str | None = None, market: str | None = None, product_brief: str | None = None,
) -> HermesContentProduct:
    old_key = product.product_key
    brand = (brand_name if brand_name is not None else product.brand_name).strip()
    if not brand:
        raise APIError("CONTENT_PRODUCT_BRAND_REQUIRED", "Brand name is required.", 400)
    name = (product_name if product_name is not None else product.product_name).strip()
    selected_market = (market if market is not None else product.market).strip() or "US"
    if not name:
        raise APIError("CONTENT_PRODUCT_NAME_REQUIRED", "Product name is required.", 400)
    new_key = _normalize_library_key(brand, name, selected_market)
    if new_key != old_key:
        exists = (
            db.query(HermesContentProduct)
            .filter(
                HermesContentProduct.workspace_id == product.workspace_id,
                HermesContentProduct.product_key == new_key,
                HermesContentProduct.id != product.id,
            )
            .one_or_none()
        )
        if exists is not None:
            raise APIError("CONTENT_PRODUCT_DUPLICATE", "A product with the same brand, name, and market already exists.", 409)
        old_dir = STORAGE_ROOT / "product_library" / f"workspace_{product.workspace_id}" / old_key
        new_dir = STORAGE_ROOT / "product_library" / f"workspace_{product.workspace_id}" / new_key
        _ensure_storage_dir(new_dir.parent)
        if old_dir.is_dir() and not new_dir.exists():
            shutil.move(str(old_dir), str(new_dir))
        else:
            _ensure_storage_dir(new_dir)
        for asset in db.query(HermesContentProductAsset).filter(HermesContentProductAsset.product_id == product.id).all():
            path = Path(asset.file_path)
            if path.is_file() and old_dir in path.parents:
                asset.file_path = str(new_dir / path.name)
        product.product_key = new_key
    product.brand_name = brand
    product.product_name = name
    product.market = selected_market
    product.product_brief = normalize_product_attribute_brief(product_brief)
    product.meta_json = {
        **dict(product.meta_json or {}),
        "facts_status": "needs_update",
        "facts_error": None,
    }
    db.flush()
    _ensure_storage_dir(STORAGE_ROOT / "product_library" / f"workspace_{product.workspace_id}" / product.product_key)
    return product


def delete_product_asset(db: Session, *, product: HermesContentProduct, asset_id: int) -> None:
    asset = (
        db.query(HermesContentProductAsset)
        .filter(
            HermesContentProductAsset.workspace_id == product.workspace_id,
            HermesContentProductAsset.product_id == product.id,
            HermesContentProductAsset.id == int(asset_id),
        )
        .one_or_none()
    )
    if asset is None:
        raise APIError("CONTENT_PRODUCT_ASSET_NOT_FOUND", "Product asset not found.", 404)
    path = Path(asset.file_path)
    db.delete(asset)
    product.meta_json = {
        **dict(product.meta_json or {}),
        "facts_status": "needs_update",
        "facts_error": None,
    }
    db.flush()
    root = (STORAGE_ROOT / "product_library" / f"workspace_{product.workspace_id}" / product.product_key).resolve()
    try:
        resolved = path.resolve()
        if resolved.is_file() and (resolved == root or root in resolved.parents):
            resolved.unlink()
    except OSError:
        pass


def delete_product(db: Session, *, product: HermesContentProduct) -> None:
    """Delete a company-library product without destroying existing projects.

    Project assets are copied into each project at creation time. Existing
    projects therefore keep their immutable inputs and deliverables while the
    library relation is cleared before the source library row and files are
    removed.
    """
    workspace_root = (STORAGE_ROOT / "product_library" / f"workspace_{product.workspace_id}").resolve()
    product_root = (workspace_root / product.product_key).resolve()
    try:
        if product_root.is_dir() and product_root != workspace_root and workspace_root in product_root.parents:
            shutil.rmtree(product_root)
    except OSError as exc:
        raise APIError(
            "CONTENT_PRODUCT_STORAGE_DELETE_FAILED",
            f"The product source directory could not be deleted: {exc}",
            500,
        ) from exc
    db.query(HermesContentFactoryProject).filter(
        HermesContentFactoryProject.workspace_id == product.workspace_id,
        HermesContentFactoryProject.product_id == product.id,
    ).update({HermesContentFactoryProject.product_id: None}, synchronize_session=False)
    db.delete(product)
    db.flush()


def find_product_knowledge(
    db: Session, *, workspace_id: int, brand_name: str, product_name: str, market: str,
    exclude_project_id: int | None = None,
) -> dict[str, Any] | None:
    product = product_name.strip()
    selected_market = market.strip()
    product_key = _normalize_library_key(brand_name, product, selected_market)
    library_product = (
        db.query(HermesContentProduct)
        .filter(
            HermesContentProduct.workspace_id == int(workspace_id),
            HermesContentProduct.product_key == product_key,
            HermesContentProduct.status == "active",
        )
        .one_or_none()
    )
    if library_product and library_product.facts_json:
        return {
            "product_library_key": product_key,
            "product_id": int(library_product.id),
            "brand_name": library_product.brand_name,
            "product_name": library_product.product_name,
            "market": library_product.market,
            "source": "product_library",
            "facts_envelope": dict(library_product.facts_json or {}),
            "updated_at": library_product.updated_at.isoformat() if library_product.updated_at else None,
        }
    query = (
        db.query(HermesContentFactoryProject, HermesContentFactoryStage)
        .join(HermesContentFactoryStage, HermesContentFactoryStage.project_id == HermesContentFactoryProject.id)
        .filter(
            HermesContentFactoryProject.workspace_id == int(workspace_id),
            HermesContentFactoryProject.product_name == product,
            HermesContentFactoryProject.market == selected_market,
            HermesContentFactoryProject.status != "deleted",
            HermesContentFactoryStage.stage == "FACTS",
            HermesContentFactoryStage.status == "success",
            HermesContentFactoryStage.output_json.isnot(None),
        )
        .order_by(HermesContentFactoryStage.completed_at.desc(), HermesContentFactoryStage.id.desc())
        .limit(50)
    )
    if exclude_project_id:
        query = query.filter(HermesContentFactoryProject.id != int(exclude_project_id))
    for project, stage in query.all():
        config = dict(project.config_json or {})
        candidate_key = str(config.get("product_library_key") or "")
        if not candidate_key:
            candidate_key = _normalize_library_key(_brand_name_from_config(config), project.product_name, project.market)
        if candidate_key != product_key:
            continue
        return {
            "product_library_key": product_key,
            "brand_name": brand_name.strip(),
            "product_name": product,
            "market": selected_market,
            "source_project_key": project.project_key,
            "source_stage_id": int(stage.id),
            "facts_envelope": dict(stage.output_json or {}),
            "updated_at": stage.completed_at.isoformat() if stage.completed_at else None,
        }
    return None


def create_project(
    db: Session, *, workspace_id: int, user_id: int, title: str, product_name: str,
    market: str, product_brief: str | None, brand_name: str = "",
    content_objective: str | None = None,
    target_audience: str | None = None,
    content_mode: str = "product",
    product_required: bool | None = None,
    product_id: int | None = None,
    video_count: int = 10,
    max_api_video_variants_in_flight: int | None = None,
    video_duration_seconds: int | None = None,
    video_duration_min_seconds: int = 10, video_duration_max_seconds: int = 10,
    video_model: str = "omni_flash", video_reference_limit: int = 7,
    video_resolution: str = "720p", video_aspect_ratio: str = "9:16",
    video_language: str = "en-US",
    video_frame_mode: str = "reference", allow_reference_video: bool = False,
    visual_reference_generation_mode: str = "individual",
    visual_image_model_chain: list[str] | None = None,
    confirmed_claims: str | None = None, confirmed_selling_points: str | None = None,
    confirmed_promotions: str | None = None, promotion_cta: str | None = None,
    allow_promotional_cta: bool = True,
    publishing_profile: dict[str, Any] | None = None,
    creative_copy_contract: dict[str, Any] | None = None,
    creative_cast_policy: dict[str, Any] | None = None,
    product_presentation_policy: dict[str, Any] | None = None,
    content_director_mode: str = "enforce",
    director_series_brief: dict[str, Any] | None = None,
    director_briefs_by_variant: dict[str, dict[str, Any]] | None = None,
    director_loop_policy: dict[str, Any] | None = None,
    director_creative_constraints: list[str] | None = None,
    director_copy_review_criteria: list[dict[str, Any]] | None = None,
    director_series_page_review_criteria: list[dict[str, Any]] | None = None,
    director_series_global_review_criteria: list[dict[str, Any]] | None = None,
    director_diversity_requirements: list[dict[str, Any]] | None = None,
    director_structured_intent_contract_required: bool | None = None,
    preferred_browser_device_id: str | None = None,
    auto_run: bool = True,
):
    normalized_content_mode = re.sub(
        r"[^a-z0-9_.-]+",
        "-",
        str(content_mode or "general").strip().lower(),
    ).strip("-._") or "general"
    product_required = (
        normalized_content_mode == "product"
        if product_required is None
        else bool(product_required)
    )
    library_product = (
        get_product(db, workspace_id=workspace_id, product_id=product_id)
        if product_required and product_id
        else None
    )
    if library_product:
        brand_name = library_product.brand_name
        product_name = library_product.product_name
        market = library_product.market
        product_brief = product_brief or library_product.product_brief
    model = normalize_video_model_id(video_model)
    if model not in {"omni_flash", "seedance_2_0_mini"}:
        model = "omni_flash"
    target_count = max(1, min(50, int(video_count)))
    variant_parallelism = resolve_project_variant_parallelism(
        model_id=model,
        requested=max_api_video_variants_in_flight,
        target_count=target_count,
    )
    model_limit = 9 if model == "seedance_2_0_mini" else 7
    reference_generation_mode = str(
        visual_reference_generation_mode or "individual"
    ).strip().lower()
    if reference_generation_mode not in {"individual", "board"}:
        raise APIError(
            "CONTENT_VISUAL_REFERENCE_MODE_INVALID",
            "Visual reference generation mode must be individual or board.",
            400,
        )
    image_model_aliases = {
        "gpt-image-2.0": "gpt-image-2",
        "gpt-image-2": "gpt-image-2",
        "nano_banana_pro": "nano_banana_pro",
        "nano_banana_2": "nano_banana_2",
    }
    image_model_chain: list[str] = []
    for raw_model in list(
        visual_image_model_chain
        or ["gpt-image-2", "nano_banana_pro"]
    ):
        normalized_image_model = image_model_aliases.get(
            str(raw_model or "").strip().lower()
        )
        if normalized_image_model and normalized_image_model not in image_model_chain:
            image_model_chain.append(normalized_image_model)
    if not image_model_chain:
        raise APIError(
            "CONTENT_VISUAL_IMAGE_MODEL_CHAIN_INVALID",
            "At least one supported visual image model is required.",
            400,
        )
    resolution = str(video_resolution or "720p").strip().lower()
    resolution = resolution if resolution in {"480p", "720p"} else "720p"
    aspect_ratio = str(video_aspect_ratio or "9:16").strip()
    if aspect_ratio not in {"9:16", "16:9", "1:1"}:
        raise APIError(
            "CONTENT_VIDEO_ASPECT_RATIO_INVALID",
            "Video aspect ratio must be 9:16, 16:9, or 1:1.",
            400,
        )
    language_raw = str(video_language or "en-US").strip().lower().replace("_", "-")
    language = {"en": "en-US", "en-us": "en-US", "zh": "zh-CN", "zh-cn": "zh-CN", "cn": "zh-CN"}.get(language_raw, "en-US")
    legacy_duration = int(video_duration_seconds) if video_duration_seconds is not None else None
    duration_min = max(1, min(120, int(legacy_duration or video_duration_min_seconds)))
    duration_max = max(duration_min, min(120, int(legacy_duration or video_duration_max_seconds)))
    if model == "omni_flash" and not any(duration_min <= value <= duration_max for value in range(10, 121, 10)):
        raise APIError(
            "CONTENT_DURATION_RANGE_INVALID",
            "Omni Flash duration range must include a multiple of 10 seconds.",
            400,
        )
    brand = brand_name.strip() if product_required else ""
    if product_required and not brand:
        raise APIError("CONTENT_PRODUCT_BRAND_REQUIRED", "Brand name is required.", 400)
    product = product_name.strip() if product_required else ""
    selected_market = market.strip()
    product_key = _normalize_library_key(brand, product, selected_market) if product_required else ""
    if product_required and library_product and library_product.facts_json:
        product_knowledge = {
            "product_library_key": product_key,
            "product_id": int(library_product.id),
            "brand_name": library_product.brand_name,
            "product_name": library_product.product_name,
            "market": library_product.market,
            "source": "product_library",
            "facts_envelope": dict(library_product.facts_json or {}),
            "updated_at": library_product.updated_at.isoformat() if library_product.updated_at else None,
        }
    elif product_required:
        product_knowledge = find_product_knowledge(
            db, workspace_id=workspace_id, brand_name=brand, product_name=product, market=selected_market
        )
    else:
        product_knowledge = None
    normalized_director_mode = str(
        content_director_mode or "enforce"
    ).strip().lower()
    if normalized_director_mode != "enforce":
        raise APIError(
            "CONTENT_DIRECTOR_REQUIRED",
            "New content-factory projects require the universal Director.",
            400,
        )
    normalized_director_briefs = {
        str(key): dict(value)
        for key, value in dict(director_briefs_by_variant or {}).items()
        if isinstance(value, dict)
    }
    normalized_series_brief = (
        dict(director_series_brief)
        if isinstance(director_series_brief, dict)
        else None
    )
    normalized_director_policy = dict(director_loop_policy or {})
    project_key = f"cf_{uuid4().hex[:20]}"
    director_series_brief_source = (
        "explicit" if normalized_series_brief is not None else None
    )
    if (
        normalized_director_mode == "enforce"
        and normalized_series_brief is None
        and not normalized_director_briefs
    ):
        from app.services.hermes_agent.content_director_profile import (
            compile_universal_director_series_brief,
            default_director_loop_policy,
        )

        publishing = dict(publishing_profile or {})
        compiled = compile_universal_director_series_brief(
            series_id=f"{project_key}.series",
            objective=(
                str(content_objective or "").strip()
                or title.strip()
            ),
            platform=(
                str(publishing.get("platform") or "").strip()
                or "short-video"
            ),
            locale=language,
            audience=(
                str(target_audience or "").strip()
                or (
                    "Use only audience details explicitly supplied in the "
                    f"project truth for market {selected_market}."
                )
            ),
            target_count=target_count,
            minimum_duration_seconds=duration_min,
            maximum_duration_seconds=duration_max,
            video_model=model,
            video_reference_limit=video_reference_limit,
            allow_reference_video=allow_reference_video,
            aspect_ratio=aspect_ratio,
            product_required=product_required,
            brand_name=brand,
            product_name=product,
            market=selected_market,
            project_brief=product_brief,
            confirmed_claims=confirmed_claims,
            confirmed_selling_points=confirmed_selling_points,
            confirmed_promotions=confirmed_promotions,
            promotion_cta=promotion_cta,
            allow_promotional_cta=allow_promotional_cta,
            creative_copy_contract=creative_copy_contract,
            creative_cast_policy=creative_cast_policy,
            product_presentation_policy=product_presentation_policy,
            product_truth=product_knowledge,
            additional_creative_constraints=director_creative_constraints,
            additional_copy_review_criteria=(
                director_copy_review_criteria
            ),
            additional_series_page_review_criteria=(
                director_series_page_review_criteria
            ),
            additional_series_global_review_criteria=(
                director_series_global_review_criteria
            ),
            diversity_requirements_override=(
                director_diversity_requirements
            ),
            structured_intent_contract_required=(
                director_structured_intent_contract_required
            ),
        )
        normalized_series_brief = compiled.model_dump(mode="json")
        normalized_director_policy = (
            normalized_director_policy
            or default_director_loop_policy()
        )
        director_series_brief_source = "universal_profile"
    if normalized_director_mode == "enforce":
        from app.services.hermes_agent.content_capabilities import (
            validate_brief_capabilities_against_registry,
        )
        from app.services.hermes_agent.content_director import (
            DirectorProjectBrief,
            DirectorSeriesBrief,
        )
        from app.services.hermes_agent.content_director_runtime import (
            DirectorLoopPolicy,
        )

        if normalized_series_brief is None and "1" not in normalized_director_briefs:
            raise APIError(
                "CONTENT_DIRECTOR_BRIEF_REQUIRED",
                "Enforced director mode requires either a project series brief "
                "or explicit per-variant briefs.",
                400,
            )
        try:
            if normalized_series_brief is not None:
                series_brief = DirectorSeriesBrief.model_validate(
                    normalized_series_brief
                )
                validate_brief_capabilities_against_registry(series_brief)
                if series_brief.target_count != target_count:
                    raise ValueError(
                        "series brief target_count must match video_count"
                    )
                if series_brief.locale != language:
                    raise ValueError(
                        "series brief locale must match video_language"
                    )
                if series_brief.aspect_ratio != aspect_ratio:
                    raise ValueError(
                        "series brief aspect_ratio must match video_aspect_ratio"
                    )
                if (
                    series_brief.minimum_duration_seconds < duration_min
                    or series_brief.maximum_duration_seconds > duration_max
                ):
                    raise ValueError(
                        "series brief duration range must stay inside the "
                        "project duration range"
                    )
                if (
                    series_brief.conversion.product_required
                    != product_required
                ):
                    raise ValueError(
                        "series brief product mode must match the project"
                    )
                if product_required and (
                    str(series_brief.conversion.product_name or "")
                    .strip()
                    .casefold()
                    .find(product.casefold())
                    < 0
                ):
                    raise ValueError(
                        "series brief product identity must match the project"
                    )
            for raw in normalized_director_briefs.values():
                brief = DirectorProjectBrief.model_validate(raw)
                validate_brief_capabilities_against_registry(brief)
                if brief.aspect_ratio != aspect_ratio:
                    raise ValueError(
                        "director brief aspect_ratio must match video_aspect_ratio"
                    )
            DirectorLoopPolicy.model_validate(normalized_director_policy)
        except ValueError as exc:
            raise APIError(
                "CONTENT_DIRECTOR_CONFIG_INVALID",
                str(exc),
                400,
            ) from exc
    auto_director_needs_facts = bool(
        normalized_director_mode == "enforce"
        and director_series_brief_source == "universal_profile"
        and product_required
        and product_knowledge is None
    )
    start_stage = (
        "FACTS"
        if auto_director_needs_facts
        else (
            "SERIES_DIRECTOR"
            if normalized_series_brief is not None
            else "DIRECTOR"
        )
    )
    preferred_device = str(preferred_browser_device_id or "").strip()[:128] or None
    project_state: dict[str, Any] = {
        "approvals": {},
        "repair_counts": {},
        "active_variant_index": 1,
        "video_variant_pipeline": {
            "target_count": target_count,
            "active_index": 1,
            "submitted_indices": [],
            "completed_indices": [],
            "failed_indices": [],
            "mode": (
                "bounded_api_parallel_v1"
                if target_count > 1 and variant_parallelism > 1
                else (
                    "serial_one_complete_video_at_a_time"
                    if target_count > 1
                    else "single_batch"
                )
            ),
            "max_api_video_variants_in_flight": variant_parallelism,
            "started_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        },
        "product_knowledge": product_knowledge,
        "product_facts_source": (
            "library" if product_knowledge else "project_assets"
        ) if product_required else "not_applicable",
        "state_machine": {
            "revision": 1,
            "stage": start_stage,
            "status": "draft",
            "updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "history": [{"from": None, "to": start_stage, "reason": "project_created"}],
        },
    }
    if preferred_device:
        project_state["preferred_browser_device_id"] = preferred_device
    project = HermesContentFactoryProject(
        project_key=project_key, workspace_id=workspace_id, user_id=user_id,
        product_id=int(library_product.id) if library_product else None,
        title=title.strip(), product_name=product, market=selected_market,
        product_brief=(product_brief or "").strip() or None, status="draft", current_stage=start_stage,
        config_json={
            "workflow_version": "2.0", "video_count": target_count,
            "max_api_video_variants_in_flight": variant_parallelism,
            "content_mode": normalized_content_mode,
            "product_required": product_required,
            "video_duration_min_seconds": duration_min,
            "video_duration_max_seconds": duration_max,
            "video_model": model,
            "video_resolution": resolution,
            "video_aspect_ratio": aspect_ratio,
            "video_language": language,
            "content_execution_key": f"{project_key}:run:{uuid4().hex[:16]}",
            "video_reference_limit": max(1, min(model_limit, int(video_reference_limit))),
            "video_frame_mode": video_frame_mode if video_frame_mode in {"reference", "first_last"} else "reference",
            "allow_reference_video": bool(allow_reference_video),
            "visual_reference_generation_mode": reference_generation_mode,
            "visual_image_model_chain": image_model_chain,
            "confirmed_claims": (confirmed_claims or "").strip() or None,
            "confirmed_selling_points": (confirmed_selling_points or "").strip() or None,
            "confirmed_promotions": (confirmed_promotions or "").strip() or None,
            "promotion_cta": (promotion_cta or "").strip() or None,
            "allow_promotional_cta": bool(allow_promotional_cta),
            "publishing_profile": dict(publishing_profile or {}),
            "content_objective": (
                str(content_objective or "").strip() or title.strip()
            ),
            "target_audience": (
                str(target_audience or "").strip() or None
            ),
            "creative_copy_contract": dict(creative_copy_contract or {}),
            "creative_cast_policy": dict(creative_cast_policy or {}),
            "product_presentation_policy": dict(
                product_presentation_policy or {}
            ),
            "content_director_mode": normalized_director_mode,
            "director_series_brief": normalized_series_brief,
            "director_series_brief_source": director_series_brief_source,
            "director_briefs_by_variant": normalized_director_briefs,
            "director_loop_policy": normalized_director_policy,
            "director_creative_constraints": list(
                director_creative_constraints or []
            ),
            "director_copy_review_criteria": list(
                director_copy_review_criteria or []
            ),
            "director_series_page_review_criteria": list(
                director_series_page_review_criteria or []
            ),
            "director_series_global_review_criteria": list(
                director_series_global_review_criteria or []
            ),
            "director_diversity_requirements": list(
                director_diversity_requirements or []
            ),
            "director_structured_intent_contract_required": (
                director_structured_intent_contract_required
            ),
            "user_confirmed_marketing": bool(
                (product_brief or "").strip()
                or (confirmed_claims or "").strip()
                or (confirmed_selling_points or "").strip()
                or (confirmed_promotions or "").strip()
                or (promotion_cta or "").strip()
            ),
            "auto_run": bool(auto_run),
            "auto_start_on_upload": bool(auto_run),
            "brand_name": brand or None,
            "product_id": int(library_product.id) if library_product else None,
            "product_library_key": product_key,
            "uses_product_library": bool(product_required and product_knowledge),
            "knowledge_namespace": f"workspace_{workspace_id}/pending",
        }, state_json=project_state,
    )
    db.add(project)
    db.flush()
    _ensure_storage_dir(STORAGE_ROOT / f"workspace_{workspace_id}" / project.project_key / "assets")
    _ensure_storage_dir(BROWSER_INBOX / f"workspace_{workspace_id}" / project.project_key)
    if library_product:
        copy_product_assets_to_project(db, project=project, product=library_product, user_id=user_id)
    config = dict(project.config_json or {})
    config["knowledge_namespace"] = f"workspace_{workspace_id}/{project.project_key}"
    project.config_json = config
    return project


def _record_project_transition(
    project: HermesContentFactoryProject, *, stage: str | None = None,
    status: str | None = None, reason: str,
) -> None:
    state = dict(project.state_json or {})
    machine = dict(state.get("state_machine") or {})
    history = list(machine.get("history") or [])[-49:]
    previous_stage = str(machine.get("stage") or project.current_stage)
    next_stage = str(stage or project.current_stage)
    history.append({
        "from": previous_stage,
        "to": next_stage,
        "status": str(status or project.status),
        "reason": reason,
        "at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    })
    machine.update({
        "revision": int(machine.get("revision") or 0) + 1,
        "stage": next_stage,
        "status": str(status or project.status),
        "updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "history": history,
    })
    state["state_machine"] = machine
    project.state_json = state


def update_project(
    db: Session, project: HermesContentFactoryProject, *, values: dict[str, Any],
) -> HermesContentFactoryProject:
    if project.status in {"queued", "running", "generating_video"}:
        raise APIError("CONTENT_PROJECT_BUSY", "Pause the project before editing its settings.", 409)
    existing_config = dict(project.config_json or {})
    production_contract_defaults: dict[str, Any] = {
        "video_duration_min_seconds": 10,
        "video_duration_max_seconds": 10,
        "video_model": "omni_flash",
        "video_resolution": "720p",
        "video_aspect_ratio": "9:16",
        "video_language": "en-US",
        "video_reference_limit": 7,
        "video_frame_mode": "reference",
        "allow_reference_video": False,
        "visual_reference_generation_mode": "individual",
        "visual_image_model_chain": ["gpt-image-2", "nano_banana_pro"],
    }
    production_contract_keys = {
        "video_count",
        "video_duration_min_seconds",
        "video_duration_max_seconds",
        "video_model",
        "video_resolution",
        "video_aspect_ratio",
        "video_language",
        "video_reference_limit",
        "video_frame_mode",
        "allow_reference_video",
        "visual_reference_generation_mode",
        "visual_image_model_chain",
        "content_objective",
        "target_audience",
        "content_mode",
        "product_required",
        "confirmed_claims",
        "confirmed_selling_points",
        "confirmed_promotions",
        "promotion_cta",
        "allow_promotional_cta",
        "publishing_profile",
        "creative_copy_contract",
        "creative_cast_policy",
        "product_presentation_policy",
        "content_director_mode",
        "director_series_brief",
        "director_briefs_by_variant",
        "director_loop_policy",
        "director_creative_constraints",
        "director_copy_review_criteria",
        "director_series_page_review_criteria",
        "director_series_global_review_criteria",
        "director_diversity_requirements",
        "director_structured_intent_contract_required",
    }
    changed_production_contract_keys = sorted(
        key
        for key in production_contract_keys
        if key in values
        and values[key] is not None
        and values[key]
        != existing_config.get(
            key,
            production_contract_defaults.get(key),
        )
    )
    if "product_brief" in values and (
        str(values.get("product_brief") or "").strip()
        != str(project.product_brief or "").strip()
    ):
        changed_production_contract_keys.append("product_brief")
    if changed_production_contract_keys:
        media_history = (
            db.query(HermesContentFactoryStage.id)
            .filter(
                HermesContentFactoryStage.project_id == int(project.id),
                HermesContentFactoryStage.stage.in_(
                    {
                        "VISUAL_PREVIEW",
                        "CREATIVE_REVIEW",
                        "FINAL_ASSETS",
                        "VIDEO_PROMPTS",
                        "EDIT_PACKAGE",
                        "COMPLETE",
                    }
                ),
            )
            .first()
        )
        if media_history is None:
            media_history = (
                db.query(HermesContentFactoryAsset.id)
                .filter(
                    HermesContentFactoryAsset.project_id == int(project.id),
                    HermesContentFactoryAsset.stage.in_(
                        {
                            "VISUAL_PREVIEW",
                            "CREATIVE_REVIEW",
                            "FINAL_ASSETS",
                            "VIDEO_PROMPTS",
                            "EDIT_PACKAGE",
                        }
                    ),
                )
                .first()
            )
        if media_history is not None:
            raise APIError(
                "CONTENT_MEDIA_CONTRACT_FROZEN",
                "Production settings cannot change after image or video "
                "production has started. Create a new project for a different "
                "production contract. Locked fields: "
                + ", ".join(sorted(set(changed_production_contract_keys))),
                409,
            )

    if "title" in values and values["title"] is not None:
        project.title = str(values["title"]).strip()
    if "product_brief" in values:
        project.product_brief = str(values.get("product_brief") or "").strip() or None

    config = existing_config
    for key in (
        "video_count", "max_api_video_variants_in_flight",
        "video_duration_min_seconds", "video_duration_max_seconds",
        "video_model", "video_resolution", "video_aspect_ratio", "video_language", "video_reference_limit",
        "video_frame_mode", "allow_reference_video", "confirmed_claims",
        "visual_reference_generation_mode", "visual_image_model_chain",
        "confirmed_selling_points", "confirmed_promotions", "promotion_cta",
        "allow_promotional_cta", "publishing_profile",
        "content_objective", "target_audience",
        "content_mode", "product_required",
        "creative_copy_contract", "creative_cast_policy",
        "product_presentation_policy", "content_director_mode",
        "director_series_brief", "director_briefs_by_variant",
        "director_loop_policy", "director_creative_constraints",
        "director_copy_review_criteria",
        "director_series_page_review_criteria",
        "director_series_global_review_criteria",
        "director_diversity_requirements",
        "director_structured_intent_contract_required", "auto_run",
    ):
        if key in values and values[key] is not None:
            config[key] = values[key]

    aspect_ratio = str(config.get("video_aspect_ratio") or "9:16").strip()
    if aspect_ratio not in {"9:16", "16:9", "1:1"}:
        raise APIError(
            "CONTENT_VIDEO_ASPECT_RATIO_INVALID",
            "Video aspect ratio must be 9:16, 16:9, or 1:1.",
            400,
        )
    config["video_aspect_ratio"] = aspect_ratio

    requested_director_mode = values.get("content_director_mode")
    if (
        requested_director_mode is not None
        and str(requested_director_mode).strip().lower() != "enforce"
    ):
        raise APIError(
            "CONTENT_DIRECTOR_REQUIRED",
            "The universal Director cannot be disabled for an active project.",
            400,
        )
    director_mode = "enforce"
    config["content_director_mode"] = director_mode
    if director_mode == "enforce":
        from app.services.hermes_agent.content_capabilities import (
            validate_brief_capabilities_against_registry,
        )
        from app.services.hermes_agent.content_director import (
            DirectorProjectBrief,
            DirectorSeriesBrief,
        )
        from app.services.hermes_agent.content_director_runtime import (
            DirectorLoopPolicy,
        )

        raw_briefs = config.get("director_briefs_by_variant")
        raw_series_brief = config.get("director_series_brief")
        source = str(
            config.get("director_series_brief_source") or ""
        ).strip()
        explicit_series_update = isinstance(
            values.get("director_series_brief"),
            dict,
        )
        if explicit_series_update:
            config["director_series_brief_source"] = "explicit"
        elif (
            source == "universal_profile"
            or (
                not isinstance(raw_series_brief, dict)
                and (not isinstance(raw_briefs, dict) or not raw_briefs)
            )
        ):
            from app.services.hermes_agent.content_director_profile import (
                compile_universal_director_series_brief,
                default_director_loop_policy,
            )

            publishing = dict(config.get("publishing_profile") or {})
            state = dict(project.state_json or {})
            compiled = compile_universal_director_series_brief(
                series_id=f"{project.project_key}.series",
                objective=(
                    str(config.get("content_objective") or "").strip()
                    or project.title
                ),
                platform=(
                    str(publishing.get("platform") or "").strip()
                    or "short-video"
                ),
                locale=str(
                    config.get("video_language") or "en-US"
                ),
                audience=(
                    str(config.get("target_audience") or "").strip()
                    or (
                        "Use only audience details explicitly supplied in the "
                        f"project truth for market {project.market}."
                    )
                ),
                target_count=int(config.get("video_count") or 1),
                minimum_duration_seconds=int(
                    config.get("video_duration_min_seconds") or 10
                ),
                maximum_duration_seconds=int(
                    config.get("video_duration_max_seconds") or 10
                ),
                video_model=str(
                    config.get("video_model") or "omni_flash"
                ),
                video_reference_limit=int(
                    config.get("video_reference_limit") or 7
                ),
                allow_reference_video=bool(
                    config.get("allow_reference_video", False)
                ),
                aspect_ratio=aspect_ratio,
                product_required=bool(
                    config.get("product_required", True)
                ),
                brand_name=config.get("brand_name"),
                product_name=project.product_name,
                market=project.market,
                project_brief=project.product_brief,
                confirmed_claims=config.get("confirmed_claims"),
                confirmed_selling_points=config.get(
                    "confirmed_selling_points"
                ),
                confirmed_promotions=config.get(
                    "confirmed_promotions"
                ),
                promotion_cta=config.get("promotion_cta"),
                allow_promotional_cta=bool(
                    config.get("allow_promotional_cta", True)
                ),
                creative_copy_contract=dict(
                    config.get("creative_copy_contract") or {}
                ),
                creative_cast_policy=dict(
                    config.get("creative_cast_policy") or {}
                ),
                product_presentation_policy=dict(
                    config.get("product_presentation_policy") or {}
                ),
                product_truth=dict(
                    state.get("product_knowledge") or {}
                ),
                additional_creative_constraints=list(
                    config.get("director_creative_constraints") or []
                ),
                additional_copy_review_criteria=list(
                    config.get("director_copy_review_criteria") or []
                ),
                additional_series_page_review_criteria=list(
                    config.get(
                        "director_series_page_review_criteria"
                    ) or []
                ),
                additional_series_global_review_criteria=list(
                    config.get(
                        "director_series_global_review_criteria"
                    ) or []
                ),
                diversity_requirements_override=list(
                    config.get("director_diversity_requirements") or []
                ),
                structured_intent_contract_required=(
                    config.get(
                        "director_structured_intent_contract_required"
                    )
                ),
            )
            raw_series_brief = compiled.model_dump(mode="json")
            config["director_series_brief"] = raw_series_brief
            config["director_series_brief_source"] = (
                "universal_profile"
            )
            if not config.get("director_loop_policy"):
                config["director_loop_policy"] = (
                    default_director_loop_policy()
                )
        if (
            not isinstance(raw_series_brief, dict)
            and (not isinstance(raw_briefs, dict) or not raw_briefs)
        ):
            raise APIError(
                "CONTENT_DIRECTOR_BRIEF_REQUIRED",
                "Enforced director mode requires either a project series brief "
                "or explicit per-variant briefs.",
                400,
            )
        try:
            if isinstance(raw_series_brief, dict):
                series_brief = DirectorSeriesBrief.model_validate(
                    raw_series_brief
                )
                validate_brief_capabilities_against_registry(series_brief)
                if series_brief.target_count != int(
                    config.get("video_count") or 1
                ):
                    raise ValueError(
                        "series brief target_count must match video_count"
                    )
                if series_brief.locale != str(
                    config.get("video_language") or "en-US"
                ):
                    raise ValueError(
                        "series brief locale must match video_language"
                    )
                if series_brief.aspect_ratio != aspect_ratio:
                    raise ValueError(
                        "series brief aspect_ratio must match video_aspect_ratio"
                    )
            for raw in dict(raw_briefs or {}).values():
                brief = DirectorProjectBrief.model_validate(raw)
                validate_brief_capabilities_against_registry(brief)
                if brief.aspect_ratio != aspect_ratio:
                    raise ValueError(
                        "director brief aspect_ratio must match video_aspect_ratio"
                    )
            DirectorLoopPolicy.model_validate(
                config.get("director_loop_policy")
            )
        except (TypeError, ValueError) as exc:
            raise APIError(
                "CONTENT_DIRECTOR_CONFIG_INVALID",
                str(exc),
                400,
            ) from exc

    duration_min = int(config.get("video_duration_min_seconds") or 10)
    duration_max = int(config.get("video_duration_max_seconds") or duration_min)
    if duration_min > duration_max:
        raise APIError("CONTENT_DURATION_RANGE_INVALID", "Minimum duration cannot exceed maximum duration.", 400)
    model = normalize_video_model_id(str(config.get("video_model") or "omni_flash"))
    if model == "omni_flash" and not any(duration_min <= value <= duration_max for value in range(10, 121, 10)):
        raise APIError("CONTENT_DURATION_RANGE_INVALID", "Omni Flash duration range must include a multiple of 10 seconds.", 400)
    if model not in {"omni_flash", "seedance_2_0_mini"}:
        model = "omni_flash"
    config["video_model"] = model
    config["max_api_video_variants_in_flight"] = (
        resolve_project_variant_parallelism(
            model_id=model,
            requested=config.get("max_api_video_variants_in_flight"),
            target_count=int(config.get("video_count") or 1),
        )
    )
    model_limit = 9 if model == "seedance_2_0_mini" else 7
    config["video_reference_limit"] = max(1, min(model_limit, int(config.get("video_reference_limit") or model_limit)))
    reference_generation_mode = str(
        config.get("visual_reference_generation_mode") or "individual"
    ).strip().lower()
    if reference_generation_mode not in {"individual", "board"}:
        raise APIError(
            "CONTENT_VISUAL_REFERENCE_MODE_INVALID",
            "Visual reference generation mode must be individual or board.",
            400,
        )
    config["visual_reference_generation_mode"] = reference_generation_mode
    image_model_aliases = {
        "gpt-image-2.0": "gpt-image-2",
        "gpt-image-2": "gpt-image-2",
        "nano_banana_pro": "nano_banana_pro",
        "nano_banana_2": "nano_banana_2",
    }
    image_model_chain: list[str] = []
    for raw_model in list(
        config.get("visual_image_model_chain")
        or ["gpt-image-2", "nano_banana_pro"]
    ):
        normalized_image_model = image_model_aliases.get(
            str(raw_model or "").strip().lower()
        )
        if normalized_image_model and normalized_image_model not in image_model_chain:
            image_model_chain.append(normalized_image_model)
    if not image_model_chain:
        raise APIError(
            "CONTENT_VISUAL_IMAGE_MODEL_CHAIN_INVALID",
            "At least one supported visual image model is required.",
            400,
        )
    config["visual_image_model_chain"] = image_model_chain
    config["auto_start_on_upload"] = bool(config.get("auto_run", True))
    config["user_confirmed_marketing"] = bool(
        str(project.product_brief or "").strip()
        or
        str(config.get("confirmed_claims") or "").strip()
        or str(config.get("confirmed_selling_points") or "").strip()
        or str(config.get("confirmed_promotions") or "").strip()
        or str(config.get("promotion_cta") or "").strip()
    )
    project.config_json = config
    _record_project_transition(project, reason="project_settings_updated")
    db.flush()
    return project


def _clear_creative_visual_recovery_state(state: dict[str, Any]) -> dict[str, Any]:
    """Reset bounded creative/visual repair counters for a genuinely fresh plan."""
    cleaned = dict(state or {})
    for key in (
        "creative_replan_counts",
        "creative_review_visual_repair_counts",
        "creative_visual_replan_exhausted",
        "api_browser_cycle_exhausted",
        "semantic_creative_replan_counts",
        "last_semantic_api_exhaustion",
        "last_creative_review",
        "last_visual_preview_asset_recovered",
    ):
        cleaned.pop(key, None)
    return cleaned


def _clear_project_pause_metadata(state: dict[str, Any]) -> dict[str, Any]:
    """Remove pause ownership metadata when an operator resumes or restarts."""
    cleaned = dict(state or {})
    for key in (
        "paused_at",
        "pause_note",
        "pause_reason_code",
        "manual_paused_at",
        "automatic_quality_paused_at",
    ):
        cleaned.pop(key, None)
    return cleaned


def _lock_project_for_operator_control(
    db: Session,
    project: HermesContentFactoryProject,
) -> HermesContentFactoryProject:
    """Serialize pause/resume/restart against a stage's final commit.

    Stage workers intentionally do not hold a row lock while waiting on an
    external model.  They reacquire the project row only when they are ready to
    advance durable workflow state.  Operator control actions must use the
    same row so either the stage commits first and the later pause wins, or the
    pause commits first and the stage observes it at its completion fence.
    ``populate_existing`` is essential because the request session may already
    have loaded the row before the worker changed it.
    """
    locked = (
        db.query(HermesContentFactoryProject)
        .filter(HermesContentFactoryProject.id == int(project.id))
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if locked is None:
        raise APIError(
            "CONTENT_PROJECT_NOT_FOUND",
            "The content project no longer exists.",
            404,
        )
    return locked


def restart_project(
    db: Session, project: HermesContentFactoryProject, *, stage: str,
    instruction: str | None = None,
    allowed_audio_modes: list[str] | None = None,
) -> HermesContentFactoryProject:
    project = _lock_project_for_operator_control(db, project)
    target_stage = str(stage or "DIRECTOR").upper()
    if target_stage not in RESTARTABLE_STAGES:
        raise APIError("CONTENT_STAGE_INVALID", "The requested restart stage is invalid.", 400)

    state = _clear_project_pause_metadata(dict(project.state_json or {}))
    config = dict(project.config_json or {})
    pipeline = dict(state.get("video_variant_pipeline") or {})
    target_count = max(
        1,
        int(config.get("video_count") or pipeline.get("target_count") or 1),
    )
    active_variant = _project_active_variant_index(project)
    continuation_series_restart = bool(
        target_count > 1 and target_stage == "SERIES_DIRECTOR"
    )
    if allowed_audio_modes is not None and target_stage != "SERIES_DIRECTOR":
        raise APIError(
            "CONTENT_AUDIO_POLICY_STAGE_INVALID",
            "Audio delivery policy can change only with a SERIES_DIRECTOR "
            "restart so every replacement brief inherits one signed policy.",
            409,
        )
    completed_variant_indices: set[int] = set()
    incomplete_variant_indices: set[int] = set()
    if continuation_series_restart:
        deliverables = project_deliverables(db, project)
        completed_variant_indices = {
            int(item["index"])
            for item in list(deliverables.get("items") or [])[:target_count]
            if str(item.get("status") or "") == "complete"
        }
        incomplete_variant_indices = (
            set(range(1, target_count + 1)) - completed_variant_indices
        )
        if not incomplete_variant_indices:
            raise APIError(
                "CONTENT_SERIES_ALREADY_COMPLETE",
                "All requested videos and guides are already complete.",
                409,
            )
        active_variant = min(incomplete_variant_indices)
        for raw_group in list(state.get("ai_video_groups") or []):
            if not isinstance(raw_group, dict):
                continue
            try:
                group_variant = int(
                    raw_group.get("video_index")
                    or raw_group.get("variant_index")
                    or 0
                )
            except (TypeError, ValueError):
                group_variant = 0
            if group_variant not in incomplete_variant_indices:
                continue
            task_ids = [
                int(value)
                for value in list(raw_group.get("task_ids") or [])
                if str(value).strip().isdigit()
            ]
            if not task_ids:
                continue
            active_provider_count = (
                db.query(KieTask.id)
                .filter(
                    KieTask.id.in_(task_ids),
                    KieTask.workspace_id == int(project.workspace_id),
                    KieTask.created_by_user_id == project.user_id,
                    KieTask.state.notin_(tuple(CONTENT_PROVIDER_TERMINAL_STATES)),
                )
                .count()
            )
            if active_provider_count:
                raise APIError(
                    "CONTENT_SERIES_REPLAN_MEDIA_IN_FLIGHT",
                    "Drain or explicitly stop unfinished provider work before "
                    "replanning the remaining series.",
                    409,
                )
    if target_stage == "SERIES_DIRECTOR":
        state.pop("approved_series_slate", None)
        config.pop("approved_series_slate_sha256", None)
        if allowed_audio_modes is not None:
            from app.services.hermes_agent.content_director import (
                DirectorSeriesBrief,
            )

            raw_series_brief = config.get("director_series_brief")
            if not isinstance(raw_series_brief, dict):
                raise APIError(
                    "CONTENT_SERIES_AUDIO_POLICY_BRIEF_MISSING",
                    "A project-owned series brief is required before its "
                    "audio delivery policy can change.",
                    409,
                )
            try:
                revised_series_brief = DirectorSeriesBrief.model_validate({
                    **raw_series_brief,
                    "allowed_audio_modes": list(allowed_audio_modes),
                })
            except (TypeError, ValueError) as exc:
                raise APIError(
                    "CONTENT_SERIES_AUDIO_POLICY_INVALID",
                    str(exc),
                    400,
                ) from exc
            config["director_series_brief"] = (
                revised_series_brief.model_dump(mode="json")
            )
            state["series_audio_policy_override"] = {
                "allowed_audio_modes": list(
                    revised_series_brief.allowed_audio_modes
                ),
                "applies_to_variant_indices": sorted(
                    incomplete_variant_indices
                ),
                "at": datetime.now(timezone.utc)
                .replace(tzinfo=None)
                .isoformat(),
            }
        if continuation_series_restart:
            # An approved slate is an immutable audit record.  A continuation
            # replan must therefore receive a fresh version before the new
            # Director run starts; reusing the last approved version lets the
            # expensive control loop finish and only then fail at persistence.
            # Keep an already-unpersisted version stable across retries, while
            # allocating max+1 when the configured version is already owned.
            raw_series_brief = config.get("director_series_brief")
            if isinstance(raw_series_brief, dict):
                next_series_brief = dict(raw_series_brief)
                series_id = str(
                    next_series_brief.get("series_id") or ""
                ).strip()
                configured_version = max(
                    1,
                    int(next_series_brief.get("series_version") or 1),
                )
                if series_id:
                    persisted_version = int(
                        db.query(
                            func.max(
                                HermesContentSeriesSlate.series_version
                            )
                        )
                        .filter(
                            HermesContentSeriesSlate.project_id
                            == int(project.id),
                            HermesContentSeriesSlate.series_id == series_id,
                        )
                        .scalar()
                        or 0
                    )
                    allocated_version = (
                        persisted_version + 1
                        if persisted_version >= configured_version
                        else configured_version
                    )
                    next_series_brief["series_version"] = (
                        allocated_version
                    )
                    config["director_series_brief"] = next_series_brief
                    state["series_director_version_allocation"] = {
                        "series_id": series_id,
                        "previous_configured_version": configured_version,
                        "latest_persisted_version": persisted_version,
                        "allocated_version": allocated_version,
                        "reason": "continuation_replan",
                        "at": datetime.now(timezone.utc)
                        .replace(tzinfo=None)
                        .isoformat(),
                    }
        existing_briefs = dict(
            config.get("director_briefs_by_variant") or {}
        )
        if continuation_series_restart:
            config["director_briefs_by_variant"] = {
                str(index): dict(
                    existing_briefs.get(str(index))
                    or existing_briefs.get(index)
                    or {}
                )
                for index in sorted(completed_variant_indices)
                if isinstance(
                    existing_briefs.get(str(index))
                    or existing_briefs.get(index),
                    dict,
                )
            }
        else:
            config["director_briefs_by_variant"] = {}
    elif target_stage == "DIRECTOR":
        approved_by_variant = dict(
            state.get("approved_director_artifacts_by_variant") or {}
        )
        approved_by_variant.pop(str(active_variant), None)
        state["approved_director_artifacts_by_variant"] = (
            approved_by_variant
        )
        legacy_pointer = dict(
            state.get("approved_director_artifact") or {}
        )
        if int(legacy_pointer.get("variant_index") or 0) == active_variant:
            state.pop("approved_director_artifact", None)
    variant_scoped_restart = (
        target_count > 1 and target_stage in VARIANT_RESTART_STAGES
    )
    scoped_restart = variant_scoped_restart or continuation_series_restart

    active_stages = db.query(HermesContentFactoryStage).filter(
        HermesContentFactoryStage.project_id == project.id,
        HermesContentFactoryStage.status.in_(("queued", "running", "retrying", "paused")),
    ).all()
    for row in active_stages:
        if continuation_series_restart:
            row_stage = str(row.stage or "").upper()
            row_variant = _stage_variant_index(
                row,
                fallback=active_variant,
            )
            if (
                row_stage != "SERIES_DIRECTOR"
                and row_variant not in incomplete_variant_indices
            ):
                continue
        if (
            variant_scoped_restart
            and _stage_variant_index(row, fallback=active_variant) != active_variant
        ):
            continue
        if row.celery_task_id:
            _safe_revoke_task(str(row.celery_task_id), terminate=False)

    wait_task_id = state.get("ai_video_wait_task_id")
    if wait_task_id:
        _safe_revoke_task(str(wait_task_id), terminate=False)

    stage_rank = {name: index for index, name in enumerate(STAGE_ORDER)}
    target_rank = stage_rank[target_stage]
    for row in db.query(HermesContentFactoryStage).filter(HermesContentFactoryStage.project_id == project.id).all():
        row_stage = str(row.stage or "")
        if continuation_series_restart:
            row_variant = _stage_variant_index(
                row,
                fallback=active_variant,
            )
            if (
                row_stage != "SERIES_DIRECTOR"
                and row_variant not in incomplete_variant_indices
            ):
                continue
        if (
            variant_scoped_restart
            and _stage_variant_index(row, fallback=active_variant) != active_variant
        ):
            continue
        if stage_rank.get(row_stage, -1) >= target_rank and row.status != "superseded":
            row.status = "superseded"
            row.error_message = f"Superseded by restart from {target_stage}."
            row.completed_at = row.completed_at or _now()

    asset_stage_rank = {
        "VISUAL_PREVIEW": stage_rank["VISUAL_PREVIEW"],
        "CREATIVE_REVIEW": stage_rank["CREATIVE_REVIEW"],
        "FINAL_ASSETS": stage_rank["FINAL_ASSETS"],
        "VIDEO_PROMPTS": stage_rank["VIDEO_PROMPTS"],
        "EDIT_PACKAGE": stage_rank["EDIT_PACKAGE"],
    }
    project_root = (STORAGE_ROOT / f"workspace_{project.workspace_id}" / project.project_key).resolve()
    stale_assets = [
        asset for asset in db.query(HermesContentFactoryAsset).filter(HermesContentFactoryAsset.project_id == project.id).all()
        if asset_stage_rank.get(str(asset.stage or ""), -1) >= target_rank
        and asset.kind not in {"source", "reference_video", "character_reference"}
        and (
            not scoped_restart
            or _asset_variant_index(asset, fallback=active_variant)
            in (
                incomplete_variant_indices
                if continuation_series_restart
                else {active_variant}
            )
        )
    ]
    deleted_asset_ids: set[int] = set()
    deleted_guidance_indices: set[int] = set()
    for asset in stale_assets:
        if int(asset.id or 0) > 0:
            deleted_asset_ids.add(int(asset.id))
        if str(asset.kind or "") == "edit_guidance":
            deleted_guidance_indices.add(
                _asset_variant_index(asset, fallback=active_variant)
            )
        try:
            path = Path(asset.file_path).resolve()
            if path.is_file() and project_root in path.parents:
                path.unlink()
        except OSError:
            pass
        db.delete(asset)

    for key in ("ai_video_final_asset_ids", "editor_guidance_asset_ids"):
        state[key] = [
            int(value)
            for value in list(state.get(key) or [])
            if str(value).strip().isdigit()
            and int(value) not in deleted_asset_ids
        ]
    if deleted_guidance_indices:
        state["editor_guidance_ready_video_indices"] = [
            int(value)
            for value in list(state.get("editor_guidance_ready_video_indices") or [])
            if str(value).strip().isdigit()
            and int(value) not in deleted_guidance_indices
        ]
        state["editor_guidance_ready_count"] = len(
            state["editor_guidance_ready_video_indices"]
        )

    if not scoped_restart:
        for key in list(state):
            if key.startswith("ai_video_") or key in {"approvals", "last_video_generation_error"}:
                state.pop(key, None)
    else:
        for key in (
            "pending_visual_api_resume",
            "pending_visual_partial_repair",
            "last_creative_review",
            "creative_visual_replan_exhausted",
            "quality_pause_preserved_visual_asset_ids",
        ):
            state.pop(key, None)
        if continuation_series_restart:
            for key in (
                "content_series_quality_pause",
                "content_director_quality_pause",
                "approved_director_artifact",
                "approved_production_plan",
            ):
                state.pop(key, None)
            for key in (
                "approved_director_artifacts_by_variant",
                "approved_production_plans_by_variant",
            ):
                by_variant = dict(state.get(key) or {})
                state[key] = {
                    str(index): value
                    for index, value in by_variant.items()
                    if str(index).isdigit()
                    and int(index) in completed_variant_indices
                }
            pipeline = dict(state.get("video_variant_pipeline") or {})
            pipeline["active_index"] = active_variant
            pipeline["failed_indices"] = [
                int(value)
                for value in list(pipeline.get("failed_indices") or [])
                if str(value).strip().isdigit()
                and int(value) in completed_variant_indices
            ]
            pipeline["submitted_indices"] = [
                int(value)
                for value in list(pipeline.get("submitted_indices") or [])
                if str(value).strip().isdigit()
                and int(value) in completed_variant_indices
            ]
            pipeline["completion_blocked_missing_indices"] = sorted(
                incomplete_variant_indices
            )
            for key in (
                "awaiting_completed_variant_index",
                "awaiting_completion_since",
                "last_submitted_variant_index",
                "next_variant_queued_at",
            ):
                pipeline.pop(key, None)
            state["video_variant_pipeline"] = pipeline
            state["active_variant_index"] = active_variant
    if target_stage in {
        "DIRECTOR",
        "PRODUCTION_PLAN",
        "VISUAL_PREVIEW",
        "CREATIVE_REVIEW",
    }:
        if not variant_scoped_restart:
            state = _clear_creative_visual_recovery_state(state)
        else:
            # A scoped operator restart from review is a fresh audit of the
            # current downloaded references. Prior repair attempts may have
            # been consumed by a now-fixed reviewer/provider bug; carrying
            # that counter forward would immediately discard a valid script
            # instead of allowing the corrected targeted-repair path to run.
            repair_counts = dict(
                state.get("creative_review_visual_repair_counts") or {}
            )
            repair_counts.pop(str(active_variant), None)
            state["creative_review_visual_repair_counts"] = repair_counts
    state["approvals"] = {}
    state["restart_count"] = int(state.get("restart_count") or 0) + 1
    state["last_restart"] = {
        "stage": target_stage,
        "instruction": (instruction or "").strip() or None,
        "at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }
    project.state_json = state
    config["manual_paused"] = False
    project.config_json = config
    project.current_stage = target_stage
    project.status = "ready"
    project.last_error = None
    _record_project_transition(project, stage=target_stage, status="ready", reason="project_restarted")
    db.flush()
    return project


def create_product_facts_project(
    db: Session, *, product: HermesContentProduct, user_id: int,
) -> HermesContentFactoryProject:
    asset_count = (
        db.query(HermesContentProductAsset.id)
        .filter(HermesContentProductAsset.product_id == product.id)
        .count()
    )
    if asset_count <= 0:
        raise APIError("CONTENT_PRODUCT_ASSETS_REQUIRED", "Upload product documents or images before generating product facts.", 400)
    active = (
        db.query(HermesContentFactoryProject)
        .filter(
            HermesContentFactoryProject.workspace_id == product.workspace_id,
            HermesContentFactoryProject.product_id == product.id,
            HermesContentFactoryProject.status.in_(("queued", "running")),
        )
        .order_by(HermesContentFactoryProject.id.desc())
        .all()
    )
    for project in active:
        if is_product_facts_project(project):
            return project
    project = HermesContentFactoryProject(
        project_key=f"cf_{uuid4().hex[:20]}",
        workspace_id=int(product.workspace_id),
        user_id=int(user_id),
        product_id=int(product.id),
        title=f"{product.product_name} 产品事实",
        product_name=product.product_name,
        market=product.market,
        product_brief=product.product_brief,
        status="draft",
        current_stage="FACTS",
        config_json={
            "workflow_version": "2.0",
            "purpose": "product_facts",
            "brand_name": product.brand_name,
            "product_id": int(product.id),
            "product_library_key": product.product_key,
            "uses_product_library": False,
            "auto_run": False,
            "auto_start_on_upload": False,
            "knowledge_namespace": f"workspace_{product.workspace_id}/pending",
        },
        state_json={
            "approvals": {},
            "repair_counts": {},
            "product_facts_source": "product_library_refresh",
        },
    )
    db.add(project)
    db.flush()
    _ensure_storage_dir(STORAGE_ROOT / f"workspace_{product.workspace_id}" / project.project_key / "assets")
    _ensure_storage_dir(BROWSER_INBOX / f"workspace_{product.workspace_id}" / project.project_key)
    copy_product_assets_to_project(db, project=project, product=product, user_id=user_id)
    config = dict(project.config_json or {})
    config["knowledge_namespace"] = f"workspace_{product.workspace_id}/{project.project_key}"
    project.config_json = config
    product.meta_json = {
        **dict(product.meta_json or {}),
        "facts_status": "queued",
        "facts_project_key": project.project_key,
        "facts_error": None,
    }
    return project


def _project_provider_tasks(
    db: Session,
    project: HermesContentFactoryProject,
) -> list[KieTask]:
    state = dict(project.state_json or {})
    task_ids = {
        int(value)
        for value in list(state.get("ai_video_task_ids") or [])
        if str(value or "").strip().isdigit() and int(value) > 0
    }
    task_ids.update({
        int(value)
        for (value,) in db.query(HermesContentSegmentRun.provider_task_row_id)
        .filter(
            HermesContentSegmentRun.project_id == int(project.id),
            HermesContentSegmentRun.provider_task_row_id.isnot(None),
        )
        .all()
        if value is not None
    })
    if not task_ids:
        return []
    return (
        db.query(KieTask)
        .filter(
            KieTask.id.in_(sorted(task_ids)),
            KieTask.workspace_id == int(project.workspace_id),
            KieTask.created_by_user_id == project.user_id,
        )
        .all()
    )


def _project_storage_targets(
    project: HermesContentFactoryProject,
) -> tuple[Path, ...]:
    workspace_id = int(project.workspace_id)
    project_key = str(project.project_key)
    if not re.fullmatch(r"cf_[A-Za-z0-9_-]{8,64}", project_key):
        raise APIError(
            "CONTENT_PROJECT_STORAGE_KEY_INVALID",
            "The project storage key is invalid; no files were removed.",
            500,
        )
    candidates = (
        STORAGE_ROOT / f"workspace_{workspace_id}" / project_key,
        BROWSER_INBOX / f"workspace_{workspace_id}" / project_key,
        STORAGE_ROOT / "browser_outbox" / f"workspace_{workspace_id}" / project_key,
    )
    safe_targets: list[Path] = []
    for candidate in candidates:
        root = (
            BROWSER_INBOX
            if candidate.parts[: len(BROWSER_INBOX.parts)] == BROWSER_INBOX.parts
            else STORAGE_ROOT
        ).resolve()
        resolved = candidate.resolve(strict=False)
        if resolved == root or root not in resolved.parents:
            raise APIError(
                "CONTENT_PROJECT_STORAGE_SCOPE_INVALID",
                "The project storage path escaped its repository boundary.",
                500,
            )
        safe_targets.append(resolved)
    return tuple(safe_targets)


def delete_project(db: Session, project: HermesContentFactoryProject) -> None:
    """Make a project invisible and stop new work before physical cleanup.

    Database deletion and recursive filesystem removal cannot share one atomic
    transaction.  Persist a durable ``deleted`` tombstone first; the route and
    periodic healer then call :func:`finalize_deleted_project`.  A crash or
    storage error therefore leaves a hidden, retryable cleanup record instead
    of an orphan directory or a half-restored user project.
    """
    project = _lock_project_for_operator_control(db, project)
    _release_bridge_for_project(db, project_id=int(project.id))
    now = _now().isoformat()
    stages = (
        db.query(HermesContentFactoryStage)
        .filter(HermesContentFactoryStage.project_id == int(project.id))
        .all()
    )
    for stage in stages:
        if stage.celery_task_id:
            _safe_revoke_task(str(stage.celery_task_id), terminate=False)
        if str(stage.status or "").lower() in {"queued", "running", "retrying"}:
            stage.status = "failed"
            stage.error_message = "Project deleted by its owner before this stage completed."
            stage.completed_at = _now()
        db.add(stage)

    local_states = {"queued_local", "waiting_dependency"}
    for task in _project_provider_tasks(db, project):
        if str(task.state or "").strip().lower() not in local_states:
            continue
        task.state = "failed"
        task.fail_code = "project_deleted"
        task.fail_msg = "Project deleted before provider submission."
        db.add(task)

    config = dict(project.config_json or {})
    config["manual_paused"] = True
    project.config_json = config
    state = dict(project.state_json or {})
    state.update({
        "deletion_requested_at": now,
        "deletion_cleanup_pending": True,
        "browser_slot_mode": "dormant",
    })
    state.pop("ai_video_wait_task_id", None)
    project.state_json = state
    project.status = "deleted"
    project.last_error = "Project deletion is committed; storage cleanup is pending."
    db.add(project)
    db.flush()


def finalize_deleted_project(
    db: Session,
    project: HermesContentFactoryProject,
) -> bool:
    """Remove one tombstoned project once paid provider work has drained."""
    project = (
        db.query(HermesContentFactoryProject)
        .filter(HermesContentFactoryProject.id == int(project.id))
        .with_for_update()
        .one_or_none()
    )
    if project is None:
        return True
    if str(project.status or "").lower() != "deleted":
        raise APIError(
            "CONTENT_PROJECT_NOT_DELETED",
            "Only a tombstoned project can be finalized.",
            409,
        )

    active_provider_tasks = [
        task
        for task in _project_provider_tasks(db, project)
        if str(task.state or "").strip().lower()
        not in CONTENT_PROVIDER_TERMINAL_STATES
    ]
    if active_provider_tasks:
        state = dict(project.state_json or {})
        state["deletion_cleanup_pending"] = True
        state["deletion_waiting_provider_task_ids"] = [
            int(task.id) for task in active_provider_tasks
        ]
        project.state_json = state
        project.last_error = (
            "Project is hidden; storage cleanup is waiting for already-submitted provider work to drain."
        )
        db.add(project)
        db.flush()
        return False

    for target in _project_storage_targets(project):
        if target.is_symlink():
            raise APIError(
                "CONTENT_PROJECT_STORAGE_SYMLINK_FORBIDDEN",
                "Refusing to recursively remove a symlinked project directory.",
                500,
            )
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            raise APIError(
                "CONTENT_PROJECT_STORAGE_TYPE_INVALID",
                "A project storage target is not a directory.",
                500,
            )
    db.delete(project)
    db.flush()
    return True


def pause_project(db: Session, project: HermesContentFactoryProject, *, note: str | None = None) -> HermesContentFactoryProject:
    project = _lock_project_for_operator_control(db, project)
    _release_bridge_for_project(db, project_id=int(project.id))
    config = dict(project.config_json or {})
    config["manual_paused"] = True
    project.config_json = config
    state = dict(project.state_json or {})
    paused_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    state["paused_at"] = paused_at
    state["manual_paused_at"] = paused_at
    state["pause_reason_code"] = "manual"
    state["pause_note"] = (note or "").strip() or None
    project.state_json = state
    project.status = "paused"
    project.last_error = (note or "Project paused manually.")[:4000]

    active_stages = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == project.id,
            HermesContentFactoryStage.status.in_(("queued", "running", "retrying")),
        )
        .all()
    )
    task_ids = [str(stage.celery_task_id) for stage in active_stages if stage.celery_task_id]
    if task_ids:
        for task_id in task_ids:
            _safe_revoke_task(task_id, terminate=False)
    for stage in active_stages:
        stage.status = "paused"
        stage.error_message = "Paused manually; resume the project to continue from this stage."
    _record_project_transition(project, status="paused", reason="manual_pause")
    db.flush()
    return project


def configure_variant_rollout_gate(
    db: Session,
    project: HermesContentFactoryProject,
    *,
    authorized_variant_indices: list[int],
    batch_id: str | None = None,
    pause_when_complete: bool = True,
    released_by_user_id: int | None = None,
) -> HermesContentFactoryProject:
    """Persist one explicit release manifest while production is stopped.

    Batch admission is intentionally separate from resume.  A successful API
    response therefore cannot accidentally start media work if the caller
    disconnects or submits a malformed follow-up request.
    """
    project = _lock_project_for_operator_control(db, project)
    config = dict(project.config_json or {})
    if (
        str(project.status or "").lower() != "paused"
        and not bool(config.get("manual_paused", False))
    ):
        raise APIError(
            "CONTENT_ROLLOUT_GATE_PROJECT_NOT_PAUSED",
            "Pause the project before changing its rollout release manifest.",
            409,
        )

    active_stage_count = (
        db.query(func.count(HermesContentFactoryStage.id))
        .filter(
            HermesContentFactoryStage.project_id == int(project.id),
            HermesContentFactoryStage.status.in_(("queued", "running", "retrying")),
        )
        .scalar()
        or 0
    )
    if int(active_stage_count) > 0:
        raise APIError(
            "CONTENT_ROLLOUT_GATE_ACTIVE_STAGE",
            "Wait for active content stages to stop before releasing a rollout batch.",
            409,
        )

    state = dict(project.state_json or {})
    recorded_task_ids = {
        int(value)
        for value in list(state.get("ai_video_task_ids") or [])
        if str(value).strip().isdigit()
    }
    active_task_count = 0
    if recorded_task_ids:
        scoped_tasks = (
            db.query(KieTask)
            .filter(
                KieTask.id.in_(recorded_task_ids),
                KieTask.workspace_id == int(project.workspace_id),
                KieTask.created_by_user_id == project.user_id,
            )
            .all()
        )
        active_task_count = sum(
            1
            for task in scoped_tasks
            if str(task.state or "").strip().lower()
            not in CONTENT_PROVIDER_TERMINAL_STATES
        )
    if int(active_task_count) > 0:
        raise APIError(
            "CONTENT_ROLLOUT_GATE_ACTIVE_MEDIA",
            "Wait for already-submitted media tasks to drain before releasing another batch.",
            409,
        )

    target = _target_deliverable_count(project)
    normalized_batch_id = str(batch_id or f"rollout-{uuid4().hex[:16]}").strip()
    candidate = {
        "enabled": True,
        "schema_version": "1.0",
        "batch_id": normalized_batch_id,
        "authorized_variant_indices": list(authorized_variant_indices or []),
        "pause_when_complete": bool(pause_when_complete),
    }
    try:
        gate = parse_variant_rollout_gate(
            {"variant_rollout_gate": candidate},
            target_count=target,
        )
    except ValueError as exc:
        raise APIError(str(exc), "The rollout release manifest is invalid.", 400) from exc
    if gate is None:  # pragma: no cover - candidate is always enabled
        raise APIError(
            "CONTENT_ROLLOUT_GATE_INVALID",
            "The rollout release manifest is invalid.",
            400,
        )

    deliverables = project_deliverables(db, project)
    status_by_index = {
        int(item.get("index") or 0): str(item.get("status") or "")
        for item in list(deliverables.get("items") or [])
        if isinstance(item, dict)
    }
    already_complete = sorted(
        index
        for index in gate.authorized_variant_indices
        if status_by_index.get(int(index)) == "complete"
    )
    if already_complete:
        raise APIError(
            "CONTENT_ROLLOUT_GATE_VARIANT_ALREADY_COMPLETE",
            "Completed deliverables cannot be released again: "
            + ", ".join(str(value) for value in already_complete),
            409,
        )

    previous = dict(config.get("variant_rollout_gate") or {})
    checkpoint = dict(state.get("variant_rollout_checkpoint") or {})
    history = [
        dict(item)
        for item in list(state.get("variant_rollout_gate_history") or [])
        if isinstance(item, dict)
    ][-49:]
    if previous:
        history.append({
            "release_manifest": previous,
            "checkpoint": checkpoint or None,
            "replaced_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "replaced_by_user_id": released_by_user_id,
        })

    released_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    config["variant_rollout_gate"] = candidate
    state["variant_rollout_gate_history"] = history
    state["variant_rollout_release"] = {
        "schema_version": "1.0",
        "batch_id": gate.batch_id,
        "authorized_variant_indices": list(gate.authorized_variant_indices),
        "pause_when_complete": gate.pause_when_complete,
        "released_at": released_at,
        "released_by_user_id": released_by_user_id,
    }
    state.pop("variant_rollout_checkpoint", None)
    project.config_json = config
    project.state_json = state
    project.last_error = (
        f"已授权发布批次 {gate.batch_id}：视频 "
        f"{list(gate.authorized_variant_indices)}。项目仍保持暂停，恢复后才会执行。"
    )[:4000]
    _record_project_transition(
        project,
        status="paused",
        reason="variant_rollout_batch_released",
    )
    db.add(project)
    db.flush()
    return project


def _promote_approved_paused_control_stage(
    db: Session,
    project: HermesContentFactoryProject,
    stage: HermesContentFactoryStage | None,
    state: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Resume an already-approved control artifact without another model turn.

    Manual pause may win after Director/Critic work has returned but before its
    successor is queued. The signed output and immutable audit are complete in
    that case. Re-running the model would create a second revision for no
    creative reason and can break the audit-to-stage binding.
    """

    if stage is None or str(stage.status or "") != "paused":
        return state, False
    envelope = dict(stage.output_json or {})
    result = dict(envelope.get("result") or {})
    if (
        str(envelope.get("status") or "").upper() != "PASS"
        or str(result.get("loop_status") or "").lower() != "approved"
    ):
        return state, False
    variant_index = max(
        1,
        int(
            dict(stage.input_json or {}).get("variant_index")
            or envelope.get("content_factory_variant_index")
            or 1
        ),
    )
    values = dict(state or {})
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    if stage.stage == "DIRECTOR":
        from app.services.hermes_agent.content_director import (
            DirectedContentArtifact,
        )

        raw_artifact = result.get("director_artifact")
        if not isinstance(raw_artifact, dict):
            return state, False
        artifact = DirectedContentArtifact.model_validate(raw_artifact)
        audit = (
            db.query(HermesContentDirectorArtifact)
            .filter(
                HermesContentDirectorArtifact.project_id == int(project.id),
                HermesContentDirectorArtifact.variant_index
                == int(variant_index),
                HermesContentDirectorArtifact.artifact_sha256
                == artifact.artifact_sha256,
                HermesContentDirectorArtifact.accepted.is_(True),
            )
            .one_or_none()
        )
        if audit is None:
            return state, False
        pointer = {
            "variant_index": int(variant_index),
            "brief_id": result.get("brief_id"),
            "brief_version": int(result.get("brief_version") or 1),
            "artifact_sha256": artifact.artifact_sha256,
            "script_sha256": artifact.script.canonical_text_sha256,
            "audit_brief_row_id": int(audit.brief_id),
            "director_stage_id": int(stage.id),
        }
        by_variant = dict(
            values.get("approved_director_artifacts_by_variant") or {}
        )
        by_variant[str(int(variant_index))] = pointer
        values["approved_director_artifacts_by_variant"] = by_variant
        values["approved_director_artifact"] = pointer
        project.current_stage = "PRODUCTION_PLAN"
    elif stage.stage == "PRODUCTION_PLAN":
        from app.services.hermes_agent.content_production_plan import (
            DirectedProductionPlan,
        )

        raw_plan = result.get("production_plan")
        compiled = result.get("compiled_media_design")
        if not isinstance(raw_plan, dict) or not isinstance(compiled, dict):
            return state, False
        plan = DirectedProductionPlan.model_validate(raw_plan)
        lock = dict(compiled.get("production_plan_lock") or {})
        if (
            str(lock.get("plan_id") or "") != plan.plan_id
            or str(lock.get("plan_sha256") or "") != plan.plan_sha256
            or str(lock.get("director_artifact_sha256") or "")
            != plan.visual.director_artifact_sha256
        ):
            return state, False
        audit = (
            db.query(HermesContentProductionPlanAudit)
            .filter(
                HermesContentProductionPlanAudit.project_id
                == int(project.id),
                HermesContentProductionPlanAudit.stage_id == int(stage.id),
                HermesContentProductionPlanAudit.variant_index
                == int(variant_index),
                HermesContentProductionPlanAudit.plan_sha256
                == plan.plan_sha256,
                HermesContentProductionPlanAudit.accepted.is_(True),
            )
            .one_or_none()
        )
        if audit is None:
            return state, False
        director_by_variant = dict(
            values.get("approved_director_artifacts_by_variant") or {}
        )
        director_pointer = dict(
            director_by_variant.get(str(int(variant_index))) or {}
        )
        if (
            str(director_pointer.get("artifact_sha256") or "")
            != plan.visual.director_artifact_sha256
        ):
            return state, False
        pointer = {
            "variant_index": int(variant_index),
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "director_artifact_sha256": (
                plan.visual.director_artifact_sha256
            ),
            "production_plan_stage_id": int(stage.id),
            "audit_row_id": int(audit.id),
        }
        by_variant = dict(
            values.get("approved_production_plans_by_variant") or {}
        )
        by_variant[str(int(variant_index))] = pointer
        values["approved_production_plans_by_variant"] = by_variant
        values["approved_production_plan"] = pointer
        project.current_stage = "VISUAL_PREVIEW"
    else:
        return state, False
    stage.status = "success"
    stage.error_message = None
    stage.completed_at = stage.completed_at or _now()
    values["last_manual_resume_control_promotion"] = {
        "stage": str(stage.stage),
        "stage_id": int(stage.id),
        "variant_index": int(variant_index),
        "next_stage": str(project.current_stage),
        "at": now,
        "reused_signed_result": True,
    }
    return values, True


def _resume_series_slate_is_authoritative(
    db: Session,
    project: HermesContentFactoryProject,
    state: dict[str, Any],
) -> bool:
    pointer = dict(state.get("approved_series_slate") or {})
    audit_id = int(pointer.get("audit_row_id") or 0)
    stage_id = int(pointer.get("series_director_stage_id") or 0)
    slate_sha256 = str(pointer.get("slate_sha256") or "")
    if not audit_id or not stage_id or not slate_sha256:
        return False
    slate = (
        db.query(HermesContentSeriesSlate)
        .filter(
            HermesContentSeriesSlate.id == audit_id,
            HermesContentSeriesSlate.project_id == project.id,
            HermesContentSeriesSlate.status == "approved",
            HermesContentSeriesSlate.slate_sha256 == slate_sha256,
        )
        .one_or_none()
    )
    stage = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.id == stage_id,
            HermesContentFactoryStage.project_id == project.id,
            HermesContentFactoryStage.stage == "SERIES_DIRECTOR",
            HermesContentFactoryStage.status == "success",
        )
        .one_or_none()
    )
    return slate is not None and stage is not None


def _refresh_stale_universal_director_profile(
    db: Session,
    project: HermesContentFactoryProject,
) -> bool:
    """Version a universal Director contract when shipped policy changes.

    Approved slates and per-video briefs are immutable audit records.  Reusing
    them after the universal review profile changes would silently authorize
    new media under old quality rules.  Recompile only profile-owned projects,
    advance the immutable series version, and retire the active pointer; the
    next SERIES_DIRECTOR run plans only still-missing deliverables.
    """
    config = dict(project.config_json or {})
    if str(config.get("director_series_brief_source") or "").strip() != "universal_profile":
        return False
    raw_brief = config.get("director_series_brief")
    if not isinstance(raw_brief, dict):
        return False

    from app.services.hermes_agent.content_director_profile import (
        load_universal_director_profile,
        refresh_project_director_brief_from_facts,
    )

    current_profile_id = load_universal_director_profile().profile_id
    truth_payload = dict(raw_brief.get("truth_payload") or {})
    previous_profile_id = str(truth_payload.get("profile_id") or "").strip()
    if previous_profile_id == current_profile_id:
        return False

    version_candidates = [int(raw_brief.get("series_version") or 1)]
    for brief_payload in dict(config.get("director_briefs_by_variant") or {}).values():
        if isinstance(brief_payload, dict):
            version_candidates.append(int(brief_payload.get("brief_version") or 1))
    persisted_max = (
        db.query(func.max(HermesContentSeriesSlate.series_version))
        .filter(HermesContentSeriesSlate.project_id == int(project.id))
        .scalar()
    )
    if persisted_max is not None:
        version_candidates.append(int(persisted_max))
    next_series_version = max(version_candidates) + 1

    state = dict(project.state_json or {})
    refreshed = refresh_project_director_brief_from_facts(
        project,
        product_truth=dict(state.get("product_knowledge") or {}),
    )
    if not refreshed:
        return False
    config = dict(project.config_json or {})
    upgraded_brief = dict(config.get("director_series_brief") or {})
    upgraded_brief["series_version"] = next_series_version
    config["director_series_brief"] = upgraded_brief
    config.pop("approved_series_slate_sha256", None)
    project.config_json = config

    state.pop("approved_series_slate", None)
    state["director_profile_upgrade"] = {
        "from_profile_id": previous_profile_id or None,
        "to_profile_id": current_profile_id,
        "series_version": next_series_version,
        "at": _now().isoformat(),
        "policy": "immutable_replan_of_missing_deliverables",
    }
    project.state_json = state
    return True


def _resume_production_plan_is_authoritative(
    db: Session,
    project: HermesContentFactoryProject,
    state: dict[str, Any],
    *,
    variant_index: int,
) -> bool:
    by_variant = dict(state.get("approved_production_plans_by_variant") or {})
    pointer = dict(by_variant.get(str(int(variant_index))) or {})
    audit_id = int(pointer.get("audit_row_id") or 0)
    stage_id = int(pointer.get("production_plan_stage_id") or 0)
    plan_sha256 = str(pointer.get("plan_sha256") or "")
    if not audit_id or not stage_id or not plan_sha256:
        return False
    audit = (
        db.query(HermesContentProductionPlanAudit)
        .filter(
            HermesContentProductionPlanAudit.id == audit_id,
            HermesContentProductionPlanAudit.project_id == project.id,
            HermesContentProductionPlanAudit.stage_id == stage_id,
            HermesContentProductionPlanAudit.variant_index
            == int(variant_index),
            HermesContentProductionPlanAudit.plan_sha256 == plan_sha256,
            HermesContentProductionPlanAudit.accepted.is_(True),
        )
        .one_or_none()
    )
    stage = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.id == stage_id,
            HermesContentFactoryStage.project_id == project.id,
            HermesContentFactoryStage.stage == "PRODUCTION_PLAN",
            HermesContentFactoryStage.status == "success",
        )
        .one_or_none()
    )
    return audit is not None and stage is not None


def resume_project(db: Session, project: HermesContentFactoryProject) -> HermesContentFactoryProject:
    project = _lock_project_for_operator_control(db, project)
    config = dict(project.config_json or {})
    config["manual_paused"] = False
    project.config_json = config
    original_state = dict(project.state_json or {})
    state = _clear_project_pause_metadata(original_state)
    recorded_video_task_ids = [
        int(value)
        for value in list(state.get("ai_video_task_ids") or [])
        if str(value).strip().isdigit()
    ]
    declared_pending_ids = {
        int(value)
        for value in list(state.get("ai_video_pending_task_ids") or [])
        if str(value).strip().isdigit()
    }
    task_by_id = {
        int(task.id): task
        for task in (
            db.query(KieTask)
            .filter(
                KieTask.id.in_(recorded_video_task_ids),
                KieTask.workspace_id == int(project.workspace_id),
                KieTask.created_by_user_id == project.user_id,
            )
            .all()
            if recorded_video_task_ids
            else []
        )
    }
    active_video_task_ids = sorted({
        task_id
        for task_id in recorded_video_task_ids
        if (
            (
                task_id in task_by_id
                and str(task_by_id[task_id].state or "").lower()
                not in CONTENT_PROVIDER_TERMINAL_STATES
            )
            or (
                task_id not in task_by_id
                and task_id in declared_pending_ids
            )
        )
    })
    state["ai_video_pending_task_ids"] = active_video_task_ids
    failed_video_recovery_ids = sorted({
        task_id
        for task_id in declared_pending_ids
        if (
            task_id in task_by_id
            and str(task_by_id[task_id].state or "").lower()
            in {"failed", "fail", "error", "timeout"}
        )
    })
    if failed_video_recovery_ids:
        state["ai_video_resume_failed_task_ids"] = failed_video_recovery_ids
    else:
        state.pop("ai_video_resume_failed_task_ids", None)
    if active_video_task_ids or failed_video_recovery_ids:
        # A manual pause ends the current waiter even when the parallel
        # pipeline has already advanced current_stage back to DIRECTOR.
        # Invalidate its fresh heartbeat so the resume endpoint can publish an
        # immediate replacement instead of leaving dependency-chained segments
        # stranded until periodic self-heal declares the old waiter stale.
        prior_waiter = str(state.get("ai_video_wait_task_id") or "").strip()
        if prior_waiter:
            state["ai_video_wait_takeover_from"] = prior_waiter
        state.pop("ai_video_wait_task_id", None)
        state.pop("ai_video_wait_heartbeat_at", None)
        state["ai_video_wait_resume_requested_at"] = (
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        )
    else:
        state.pop("ai_video_wait_task_id", None)
        state.pop("ai_video_wait_heartbeat_at", None)
        state.pop("ai_video_wait_resume_requested_at", None)
    paused_current_stage = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == project.id,
            HermesContentFactoryStage.stage == project.current_stage,
            HermesContentFactoryStage.status == "paused",
        )
        .order_by(HermesContentFactoryStage.id.desc())
        .first()
    )
    visual_checkpoint = _latest_resumable_visual_api_checkpoint(
        db,
        project,
        paused_current_stage,
    )
    if visual_checkpoint:
        state["pending_visual_api_resume"] = visual_checkpoint
    state, _control_stage_promoted = _promote_approved_paused_control_stage(
        db,
        project,
        paused_current_stage,
        state,
    )
    pipeline = dict(state.get("video_variant_pipeline") or {})
    active_variant = max(
        1,
        int(
            pipeline.get("active_index")
            or state.get("active_variant_index")
            or 1
        ),
    )
    latest_current_stage = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == project.id,
            HermesContentFactoryStage.stage == project.current_stage,
        )
        .order_by(HermesContentFactoryStage.id.desc())
        .first()
    )
    if (
        visual_checkpoint
        and str(project.current_stage or "").upper() == "PRODUCTION_PLAN"
        and latest_current_stage is not None
        and str(latest_current_stage.status or "").lower() == "failed"
        and _resume_production_plan_is_authoritative(
            db,
            project,
            state,
            variant_index=active_variant,
        )
    ):
        # A product-surface replan can exhaust while the prior signed plan and
        # already-paid product-free scene remain valid. Resume at the local
        # placement boundary so a newer resolver policy can re-analyze those
        # pixels before either another Director turn or another image render.
        project.current_stage = "VISUAL_PREVIEW"
        state["resume_control_reset"] = {
            "reason": "recover_paid_product_scene_after_failed_replan",
            "variant_index": active_variant,
            "next_stage": "VISUAL_PREVIEW",
            "source_stage_id": int(
                visual_checkpoint.get("source_stage_id") or 0
            ),
            "at": _now().isoformat(),
        }
    # Preserve the resume-state normalization above before the profile helper
    # records its own immutable upgrade event.
    project.state_json = state
    profile_upgraded = _refresh_stale_universal_director_profile(db, project)
    if profile_upgraded:
        state = dict(project.state_json or {})
    configured_series = bool(
        dict(project.config_json or {}).get("director_series_brief")
    )
    series_ready = _resume_series_slate_is_authoritative(
        db,
        project,
        state,
    )
    media_or_legacy_stage = str(project.current_stage or "").upper() in {
        "CREATIVE",
        "VISUAL_PREVIEW",
        "CREATIVE_REVIEW",
        "FINAL_ASSETS",
        "VIDEO_PROMPTS",
    }
    if configured_series and not series_ready:
        project.current_stage = "SERIES_DIRECTOR"
        state.pop("pending_visual_api_resume", None)
        state.pop("pending_visual_partial_repair", None)
        state["resume_control_reset"] = {
            "reason": "approved_series_slate_missing",
            "variant_index": active_variant,
            "next_stage": "SERIES_DIRECTOR",
            "at": _now().isoformat(),
        }
    elif media_or_legacy_stage and not _resume_production_plan_is_authoritative(
        db,
        project,
        state,
        variant_index=active_variant,
    ):
        project.current_stage = "DIRECTOR"
        state.pop("pending_visual_api_resume", None)
        state.pop("pending_visual_partial_repair", None)
        state["resume_control_reset"] = {
            "reason": "approved_production_plan_missing",
            "variant_index": active_variant,
            "next_stage": "DIRECTOR",
            "at": _now().isoformat(),
        }
    state["resumed_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    state["resume_priority"] = 9
    state["resume_generation"] = int(state.get("resume_generation") or 0) + 1
    project.state_json = state
    if project.current_stage == "COMPLETE":
        project.status = "complete"
    elif project.current_stage in WAITING_STAGES:
        project.status = "ready"
    else:
        project.status = "ready"
    project.last_error = None
    for stage in (
        db.query(HermesContentFactoryStage)
        .filter(HermesContentFactoryStage.project_id == project.id, HermesContentFactoryStage.status == "paused")
        .all()
    ):
        stage.status = "failed"
        stage.error_message = "Superseded by manual resume."
        stage.completed_at = _now()
    current = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == project.id,
            HermesContentFactoryStage.stage == project.current_stage,
        )
        .order_by(HermesContentFactoryStage.id.desc())
        .first()
    )
    if current is not None:
        stage_input = dict(current.input_json or {})
        stage_input["self_heal_count"] = 0
        stage_input["manual_resume_generation"] = int(state["resume_generation"])
        stage_input.pop("retry_after", None)
        current.input_json = stage_input
    _record_project_transition(project, status=project.status, reason="manual_resume")
    db.flush()
    return project


def resume_waiting_project_production(
    db: Session,
    project: HermesContentFactoryProject,
) -> str | None:
    """Immediately release one authorized missing video after a checkpoint.

    A completed rollout checkpoint intentionally retires its terminal provider
    task ledger.  Resuming such a project therefore has no waiter to wake. The
    control plane must explicitly create and publish the next Director stage;
    otherwise the project remains ``ready`` at ``WAITING_VIDEO_INPUT`` forever.
    """
    if (
        str(project.current_stage or "").upper() != "WAITING_VIDEO_INPUT"
        or str(project.status or "").lower() != "ready"
    ):
        return None
    state = dict(project.state_json or {})
    if state.get("ai_video_pending_task_ids") or state.get("ai_video_resume_failed_task_ids"):
        return None

    # Imported lazily to preserve the service/task dependency boundary during
    # API and migration startup.  The task module owns stage leases, rollout
    # authorization, idempotent repair cleanup, and broker publication.
    from app.tasks.hermes_agent.content_factory_tasks import (
        _publish_stage,
        _queue_missing_serial_video_variant_if_needed,
    )

    stage = _queue_missing_serial_video_variant_if_needed(
        db,
        project,
        reason=(
            "operator resumed an automatic rollout checkpoint; release the "
            "first authorized incomplete video"
        ),
    )
    if stage is None:
        return None
    task_id = _publish_stage(stage, project)
    db.flush()
    return str(task_id or "") or None


def _content_factory_asset_out(asset: HermesContentFactoryAsset) -> dict[str, Any]:
    return {
        "id": int(asset.id),
        "stage": asset.stage,
        "kind": asset.kind,
        "original_name": asset.original_name,
        "mime_type": asset.mime_type,
        "size_bytes": int(asset.size_bytes or 0) if asset.size_bytes is not None else None,
        "meta_json": asset.meta_json,
        "created_at": asset.created_at,
    }


def _deliverable_video_index(asset: HermesContentFactoryAsset) -> int:
    meta = dict(asset.meta_json or {})
    for key in ("content_factory_video_index", "video_index"):
        try:
            value = int(meta.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    name = str(asset.original_name or "")
    match = re.search(r"(?:^|[-_])v(?:ideo)?0*([1-9]\d*)\b", name, flags=re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    return 0


def _current_asset_id_set(project: HermesContentFactoryProject, state_key: str) -> set[int]:
    state = dict(project.state_json or {})
    ids: set[int] = set()
    for value in state.get(state_key) or []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            ids.add(parsed)
    return ids


def _target_deliverable_count(project: HermesContentFactoryProject) -> int:
    config = dict(project.config_json or {})
    state = dict(project.state_json or {})
    pipeline = dict(state.get("video_variant_pipeline") or {})
    for value in (
        config.get("video_count"),
        pipeline.get("target_count"),
        state.get("ai_video_target_count"),
    ):
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return max(1, min(50, parsed))
    return 1


def _prefer_latest_by_video_index(
    assets: list[HermesContentFactoryAsset],
    *,
    preferred_ids: set[int],
) -> dict[int, HermesContentFactoryAsset]:
    selected: dict[int, HermesContentFactoryAsset] = {}
    fallback_index = 1
    for asset in sorted(assets, key=lambda item: int(item.id or 0)):
        index = _deliverable_video_index(asset)
        if index <= 0:
            while fallback_index in selected:
                fallback_index += 1
            index = fallback_index
        previous = selected.get(index)
        if previous is None:
            selected[index] = asset
            continue
        previous_preferred = int(previous.id or 0) in preferred_ids
        current_preferred = int(asset.id or 0) in preferred_ids
        if (current_preferred and not previous_preferred) or (
            current_preferred == previous_preferred and int(asset.id or 0) > int(previous.id or 0)
        ):
            selected[index] = asset
    return selected


def project_deliverables(
    db: Session,
    project: HermesContentFactoryProject,
    *,
    assets: list[HermesContentFactoryAsset] | None = None,
) -> dict[str, Any]:
    all_assets = assets
    if all_assets is None:
        all_assets = (
            db.query(HermesContentFactoryAsset)
            .filter(HermesContentFactoryAsset.project_id == project.id)
            .order_by(HermesContentFactoryAsset.id.asc())
            .all()
        )
    existing_assets = [
        asset
        for asset in all_assets
        if _stored_file_available(asset.file_path)
    ]
    preferred_video_ids = _current_asset_id_set(project, "ai_video_final_asset_ids")
    preferred_guide_ids = _current_asset_id_set(project, "editor_guidance_asset_ids")
    videos = [
        asset for asset in existing_assets
        if asset.kind == "video" and (asset.mime_type or "").startswith("video/")
    ]
    guides = [asset for asset in existing_assets if asset.kind == "edit_guidance"]
    if preferred_video_ids:
        preferred_videos = [asset for asset in videos if int(asset.id or 0) in preferred_video_ids]
        if preferred_videos:
            videos = preferred_videos
    if preferred_guide_ids:
        preferred_guides = [asset for asset in guides if int(asset.id or 0) in preferred_guide_ids]
        if preferred_guides:
            guides = preferred_guides

    video_by_index = _prefer_latest_by_video_index(videos, preferred_ids=preferred_video_ids)
    guide_by_index = _prefer_latest_by_video_index(guides, preferred_ids=preferred_guide_ids)
    target_count = _target_deliverable_count(project)
    max_index = max([target_count, *video_by_index.keys(), *guide_by_index.keys()] or [target_count])
    items: list[dict[str, Any]] = []
    for index in range(1, max_index + 1):
        video = video_by_index.get(index)
        guide = guide_by_index.get(index)
        if video and guide:
            status = "complete"
        elif video:
            status = "waiting_guidance"
        elif guide:
            status = "guide_only"
        else:
            status = "missing"
        items.append({
            "index": index,
            "status": status,
            "video": _content_factory_asset_out(video) if video else None,
            "guidance": _content_factory_asset_out(guide) if guide else None,
        })
    complete_count = len([item for item in items[:target_count] if item["status"] == "complete"])
    return {
        "target_count": target_count,
        "complete_count": complete_count,
        "video_count": len(video_by_index),
        "guidance_count": len(guide_by_index),
        "missing_indices": [item["index"] for item in items[:target_count] if item["status"] == "missing"],
        "items": items,
    }


def build_project_deliverables_zip(
    db: Session,
    project: HermesContentFactoryProject,
    *,
    kind: str = "all",
) -> Path:
    normalized_kind = str(kind or "all").lower().strip()
    if normalized_kind not in {"all", "videos", "guides"}:
        raise APIError("CONTENT_DELIVERABLE_KIND_INVALID", "kind must be all, videos, or guides.", 400)
    all_assets = (
        db.query(HermesContentFactoryAsset)
        .filter(HermesContentFactoryAsset.project_id == project.id)
        .order_by(HermesContentFactoryAsset.id.asc())
        .all()
    )
    deliverables = project_deliverables(db, project, assets=all_assets)
    files: list[tuple[str, Path]] = []
    for item in deliverables.get("items") or []:
        index = int(item.get("index") or 0)
        if normalized_kind in {"all", "videos"} and item.get("video"):
            asset = next((row for row in all_assets if int(row.id or 0) == int(item["video"]["id"])), None)
            if asset is not None and _stored_file_available(asset.file_path):
                suffix = Path(asset.original_name or "video.mp4").suffix or ".mp4"
                files.append((f"videos/V{index:02d}-{_safe_name(Path(asset.original_name or 'video').stem)}{suffix}", Path(asset.file_path)))
        if normalized_kind in {"all", "guides"} and item.get("guidance"):
            asset = next((row for row in all_assets if int(row.id or 0) == int(item["guidance"]["id"])), None)
            if asset is not None and _stored_file_available(asset.file_path):
                files.append((f"guides/V{index:02d}-{_safe_name(Path(asset.original_name or 'guide').stem)}.md", Path(asset.file_path)))
    if not files:
        raise APIError("CONTENT_DELIVERABLES_EMPTY", "No downloadable videos or guidance files are ready yet.", 404)
    downloads_dir = STORAGE_ROOT / f"workspace_{project.workspace_id}" / project.project_key / "generated" / "downloads"
    _ensure_storage_dir(downloads_dir)
    archive_path = downloads_dir / f"{_safe_name(project.title or project.project_key)}-{normalized_kind}-deliverables.zip"
    manifest = {
        "project_key": project.project_key,
        "title": project.title,
        "product_name": project.product_name,
        "target_count": deliverables.get("target_count"),
        "complete_count": deliverables.get("complete_count"),
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "kind": normalized_kind,
    }
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        used_names: set[str] = {"manifest.json"}
        for archive_name, path in files:
            safe_archive_name = archive_name
            counter = 2
            while safe_archive_name in used_names:
                stem = Path(archive_name).stem
                suffix = Path(archive_name).suffix
                parent = Path(archive_name).parent.as_posix()
                safe_archive_name = f"{parent}/{stem}-{counter}{suffix}"
                counter += 1
            used_names.add(safe_archive_name)
            archive.write(path, safe_archive_name)
    _mark_group_writable(archive_path)
    return archive_path


def project_out(db: Session, project: HermesContentFactoryProject) -> dict[str, Any]:
    stages = db.query(HermesContentFactoryStage).filter(HermesContentFactoryStage.project_id == project.id).order_by(HermesContentFactoryStage.id.asc()).all()
    assets = db.query(HermesContentFactoryAsset).filter(HermesContentFactoryAsset.project_id == project.id).order_by(HermesContentFactoryAsset.id.asc()).all()
    assets = [
        asset
        for asset in assets
        if _stored_file_available(asset.file_path)
    ]
    state = dict(project.state_json or {})
    bridge_id = str(state.get("browser_bridge_id") or "")
    browser_slot = bridge_id or None
    return {
        "project_key": project.project_key, "workspace_id": project.workspace_id, "user_id": project.user_id,
        "product_id": project.product_id,
        "title": project.title, "product_name": project.product_name, "market": project.market,
        "status": project.status, "current_stage": project.current_stage, "product_brief": project.product_brief,
        "state_json": project.state_json, "config_json": project.config_json,
        "browser_slot": browser_slot, "browser_cdp_url": state.get("browser_cdp_url") or None,
        "last_error": project.last_error, "created_at": project.created_at, "updated_at": project.updated_at,
        "stages": stages, "assets": assets,
        "deliverables": project_deliverables(db, project, assets=assets),
    }


def _safe_name(value: str) -> str:
    name = Path(value or "file").name
    return re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", name)[:180] or f"file_{uuid4().hex[:8]}"


async def _write_upload_bounded(
    upload: UploadFile,
    target: Path,
    *,
    max_bytes: int,
) -> int:
    """Stream an upload with a server-enforced limit and no partial file.

    ``UploadFile.size`` is optional and cannot be treated as an enforcement
    boundary.  Count the bytes actually received so chunked or malformed
    requests cannot fill the shared content volume.
    """
    declared_size = getattr(upload, "size", None)
    if declared_size is not None and int(declared_size) > int(max_bytes):
        raise APIError(
            "CONTENT_ASSET_TOO_LARGE",
            f"The uploaded file exceeds the {max(1, int(max_bytes) // (1024 * 1024))} MB limit.",
            413,
        )
    written = 0
    try:
        with target.open("xb") as stream:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > int(max_bytes):
                    raise APIError(
                        "CONTENT_ASSET_TOO_LARGE",
                        f"The uploaded file exceeds the {max(1, int(max_bytes) // (1024 * 1024))} MB limit.",
                        413,
                    )
                stream.write(chunk)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return written


def cleanup_uncommitted_asset_files(rows: list[Any]) -> None:
    """Remove files created by an upload transaction that did not commit."""
    root = STORAGE_ROOT.resolve()
    for row in rows:
        candidates = [str(getattr(row, "file_path", "") or "")]
        candidates.append(str(dict(getattr(row, "meta_json", None) or {}).get("bridge_path") or ""))
        for raw_path in candidates:
            if not raw_path:
                continue
            path = Path(raw_path).resolve(strict=False)
            if root not in path.parents:
                continue
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            except OSError:
                # A later orphan scan can report a filesystem permission
                # problem, but never escape the repository to compensate.
                continue


def _asset_role(filename: str, mime_type: str | None) -> str:
    name = unicodedata.normalize("NFKC", str(filename or "")).lower()
    mime = str(mime_type or "").lower()
    ext = Path(name).suffix.lower()
    fact_tokens = (
        "supplement", "facts", "fact", "ingredient", "ingredients", "nutrition", "panel",
        "direction", "warning", "claim", "claims", "guardrail", "handoff", "readme",
        "brand", "promotion", "promo", "control", "sheet", "pri", "成分", "营养",
        "配料", "标签", "说明", "警告", "知识库", "合规", "促销", "优惠",
    )
    product_tokens = (
        "白底", "透明", "主图", "瓶", "罐", "包装", "产品", "product", "bottle",
        "jar", "pack", "package", "main", "hero", "transparent",
    )
    if mime == "application/pdf" or ext == ".pdf":
        return "fact_source"
    if any(token in name for token in fact_tokens):
        return "fact_source"
    if mime.startswith("image/") and any(token in name for token in product_tokens):
        return "product_visual"
    if mime.startswith("image/"):
        return "product_visual"
    if mime.startswith("video/"):
        return "reference_video"
    return "fact_source"


async def save_product_asset(db: Session, *, product: HermesContentProduct, user_id: int, upload: UploadFile, kind: str = "source"):
    filename = _safe_name(upload.filename or "file")
    disk_name = f"{uuid4().hex[:10]}_{filename}"
    product_dir = STORAGE_ROOT / "product_library" / f"workspace_{product.workspace_id}" / product.product_key
    _ensure_storage_dir(product_dir)
    target = product_dir / disk_name
    await _write_upload_bounded(
        upload,
        target,
        max_bytes=MAX_PRODUCT_ASSET_BYTES,
    )
    _mark_group_writable(target)
    row = HermesContentProductAsset(
        product_id=product.id, workspace_id=product.workspace_id, user_id=user_id,
        kind=kind, original_name=filename, file_path=str(target), mime_type=upload.content_type,
        size_bytes=target.stat().st_size,
        meta_json={"asset_role": _asset_role(filename, upload.content_type), "source": "product_library"},
    )
    db.add(row)
    product.meta_json = {
        **dict(product.meta_json or {}),
        "facts_status": "needs_update",
        "facts_error": None,
    }
    try:
        db.flush()
    except Exception:
        cleanup_uncommitted_asset_files([row])
        raise
    return row


def copy_product_assets_to_project(
    db: Session, *, project: HermesContentFactoryProject, product: HermesContentProduct, user_id: int,
) -> list[HermesContentFactoryAsset]:
    product_assets = (
        db.query(HermesContentProductAsset)
        .filter(HermesContentProductAsset.product_id == product.id)
        .order_by(HermesContentProductAsset.id.asc())
        .all()
    )
    project_dir = STORAGE_ROOT / f"workspace_{project.workspace_id}" / project.project_key / "assets"
    bridge_dir = BROWSER_INBOX / f"workspace_{project.workspace_id}" / project.project_key
    _ensure_storage_dir(project_dir)
    _ensure_storage_dir(bridge_dir)
    rows: list[HermesContentFactoryAsset] = []
    for product_asset in product_assets:
        source = Path(product_asset.file_path)
        if not source.is_file():
            continue
        disk_name = f"product_{int(product_asset.id)}_{_safe_name(product_asset.original_name)}"
        target = project_dir / disk_name
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
        _mark_group_writable(target)
        bridge_target = bridge_dir / disk_name
        if not bridge_target.exists() or bridge_target.stat().st_size != target.stat().st_size:
            shutil.copy2(target, bridge_target)
        _mark_group_writable(bridge_target)
        existing = None
        for candidate in db.query(HermesContentFactoryAsset).filter(
            HermesContentFactoryAsset.project_id == project.id,
            HermesContentFactoryAsset.kind == "source",
        ).all():
            if int(dict(candidate.meta_json or {}).get("product_asset_id") or 0) == int(product_asset.id):
                existing = candidate
                break
        meta = {
            "bridge_path": str(bridge_target),
            "browser_inbox_relative": f"workspace_{project.workspace_id}/{project.project_key}/{disk_name}",
            "product_id": int(product.id),
            "product_asset_id": int(product_asset.id),
            "source": "product_library",
            "asset_role": dict(product_asset.meta_json or {}).get("asset_role")
            or _asset_role(product_asset.original_name, product_asset.mime_type),
        }
        if existing:
            existing.file_path = str(target)
            existing.mime_type = product_asset.mime_type
            existing.size_bytes = target.stat().st_size
            existing.meta_json = {**dict(existing.meta_json or {}), **meta}
            rows.append(existing)
            continue
        row = HermesContentFactoryAsset(
            project_id=project.id, workspace_id=project.workspace_id, user_id=user_id,
            stage="FACTS", kind="source", original_name=product_asset.original_name,
            file_path=str(target), mime_type=product_asset.mime_type, size_bytes=target.stat().st_size,
            meta_json=meta,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


async def save_asset(
    db: Session,
    *,
    project: HermesContentFactoryProject,
    user_id: int,
    upload: UploadFile,
    kind: str = "source",
    extra_meta: dict[str, Any] | None = None,
):
    filename = _safe_name(upload.filename or "file")
    disk_name = f"{uuid4().hex[:10]}_{filename}"
    project_dir = STORAGE_ROOT / f"workspace_{project.workspace_id}" / project.project_key / "assets"
    bridge_dir = BROWSER_INBOX / f"workspace_{project.workspace_id}" / project.project_key
    _ensure_storage_dir(project_dir)
    _ensure_storage_dir(bridge_dir)
    target = project_dir / disk_name
    if kind == "reference_video":
        max_bytes = MAX_REFERENCE_VIDEO_BYTES
    elif kind == "character_reference":
        max_bytes = MAX_CHARACTER_REFERENCE_BYTES
    else:
        max_bytes = MAX_PROJECT_SOURCE_BYTES
    await _write_upload_bounded(upload, target, max_bytes=max_bytes)
    _mark_group_writable(target)
    bridge_target = bridge_dir / disk_name
    try:
        shutil.copy2(target, bridge_target)
    except Exception:
        target.unlink(missing_ok=True)
        bridge_target.unlink(missing_ok=True)
        raise
    _mark_group_writable(bridge_target)
    if kind == "reference_video":
        stage_name = "REFERENCE_VIDEO"
        asset_role = "reference_video"
    elif kind == "character_reference":
        stage_name = "CHARACTER_REFERENCE"
        asset_role = "character_reference"
    else:
        stage_name = project.current_stage
        asset_role = _asset_role(filename, upload.content_type)
    row = HermesContentFactoryAsset(
        project_id=project.id, workspace_id=project.workspace_id, user_id=user_id,
        stage=stage_name,
        kind=kind, original_name=filename, file_path=str(target), mime_type=upload.content_type,
        size_bytes=target.stat().st_size,
        meta_json={
            "bridge_path": str(bridge_target),
            "browser_inbox_relative": f"workspace_{project.workspace_id}/{project.project_key}/{disk_name}",
            "asset_role": asset_role,
            "source": "project_upload",
            **dict(extra_meta or {}),
        },
    )
    db.add(row)
    try:
        db.flush()
    except Exception:
        cleanup_uncommitted_asset_files([row])
        raise
    return row



async def bridge_status(db: Session | None = None, *, workspace_id: int | None = None, user_id: int | None = None) -> dict[str, Any]:
    # This endpoint is polled by every open Content Factory page. Keep it read-only:
    # bridge agents own heartbeat writes, and queue/acquire paths release expired leases.
    # Mutating last_seen/status here can contend with the agent heartbeat and stall API
    # workers under multi-user polling.
    usage = _active_bridge_usage(db, workspace_id=int(workspace_id), user_id=user_id) if db is not None and workspace_id is not None else {}
    slots: list[dict[str, Any]] = []
    connected_any = False
    first_browser = None
    detail = None
    rows: list[HermesBrowserBridge] = []
    if db is not None and workspace_id is not None:
        query = db.query(HermesBrowserBridge).filter(
            HermesBrowserBridge.workspace_id == int(workspace_id),
            HermesBrowserBridge.status != "retired",
        )
        if user_id is not None:
            query = query.filter(HermesBrowserBridge.user_id == int(user_id))
        rows = query.order_by(HermesBrowserBridge.last_seen_at.desc(), HermesBrowserBridge.id.desc()).all()
    for bridge in rows:
        if not _bridge_is_displayable(bridge) or not _bridge_device_bound(bridge):
            continue
        url = bridge.cdp_url
        slot_usage = usage.get(str(bridge.bridge_id), [])
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{url.rstrip('/')}/json/version")
                response.raise_for_status()
                data = response.json()
            browser = data.get("Browser")
            slots.append(_bridge_out(bridge, connected=True, browser=browser, queue=slot_usage))
            connected_any = True
            first_browser = first_browser or browser
        except Exception as exc:
            load = dict(bridge.load_json or {})
            agent_error = str(load.get("agent_error") or "").strip()
            if _bridge_agent_recent(bridge):
                shown_detail = (
                    "浏览器桥程序已在线，但 Chrome/CDP 隧道未连通："
                    + (agent_error or str(exc))
                )[:300]
            else:
                shown_detail = str(exc)[:300]
            slots.append(_bridge_out(bridge, connected=False, detail=shown_detail, queue=slot_usage))
            detail = detail or shown_detail
    load = _server_load_snapshot()
    active_leases = sum(1 for slot in slots if slot.get("connected") and slot.get("active_project_id"))
    devices = browser_devices(db, workspace_id=int(workspace_id), user_id=int(user_id)) if db is not None and workspace_id is not None and user_id is not None else []
    selected_device_id, selection_required = _effective_browser_device_id(devices)
    return {
        "connected": connected_any,
        "browser": first_browser,
        "detail": None if connected_any else detail,
        "slots": slots,
        "capacity": load["capacity"],
        "active_slots": active_leases,
        "load": load,
        "mode": "dynamic_user_device_bridge",
        "devices": devices,
        "selected_device_id": selected_device_id,
        "selection_required": selection_required,
    }



def project_hermes_queue(project: HermesContentFactoryProject) -> str:
    # Browser isolation is now controlled by bridge leases, not by fixed
    # Celery slot queues. A worker receives the stage and connects to the
    # bridge CDP URL persisted on that stage.
    return HERMES_QUEUE_BASE


def _stage_api_route(db: Session, stage: str) -> str | None:
    stage_name = str(stage or "").upper()
    if stage_name in {"SERIES_DIRECTOR", "DIRECTOR", "PRODUCTION_PLAN"}:
        return "hermes:content-director"
    if stage_name == "CREATIVE_REVIEW":
        logical_model = str(
            os.getenv("HERMES_CREATIVE_REVIEW_MODEL") or ""
        ).strip()
        if logical_model:
            return f"ai-routing:{logical_model}"
        # Missing role configuration must fail explicitly on the API path;
        # it must never wake a browser slot as an accidental fallback.
        return "ai-routing:unconfigured"
    if stage_name == "VISUAL_PREVIEW" and has_active_key(
        db, provider_key=BANDIANWA_PROVIDER_KEY
    ):
        return "bandianwa:gpt-image-2"
    if stage_name in {"FACTS", "EDIT_PACKAGE"} and has_active_key(
        db, provider_key=TOAPIS_PROVIDER_KEY
    ):
        return "toapis:text"
    return None


def _select_visual_variant_api_route(
    prior_stage_inputs: list[dict[str, Any]],
    *,
    default_route: str | None,
    toapis_available: bool,
) -> str | None:
    """Keep one variant on the API provider that already owns its visuals.

    A partial repair or material concept replan is not a new provider-selection
    event. Resetting to the global default made a ToAPIs-owned variant jump
    back to Bandianwa even after Bandianwa had exhausted its bounded budget.
    A genuinely new variant has no matching history and uses the default.
    """
    if toapis_available:
        for raw in prior_stage_inputs:
            values = dict(raw or {})
            visual_api = dict(values.get("visual_api") or {})
            if (
                str(values.get("api_route") or "").strip() == "toapis:gpt-image-2"
                or str(visual_api.get("provider") or "").strip() == TOAPIS_PROVIDER_KEY
                or bool(values.get("visual_api_skip_bandianwa"))
            ):
                return "toapis:gpt-image-2"
    return default_route


def _visual_variant_api_route(
    db: Session,
    project: HermesContentFactoryProject,
    *,
    variant_index: int,
    default_route: str | None,
) -> str | None:
    candidates = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == int(project.id),
            HermesContentFactoryStage.stage == "VISUAL_PREVIEW",
        )
        .order_by(HermesContentFactoryStage.id.desc())
        .limit(20)
        .all()
    )
    inputs = [
        dict(row.input_json or {})
        for row in candidates
        if int(dict(row.input_json or {}).get("variant_index") or variant_index)
        == int(variant_index)
    ]
    return _select_visual_variant_api_route(
        inputs,
        default_route=default_route,
        toapis_available=has_active_key(db, provider_key=TOAPIS_PROVIDER_KEY),
    )


def _control_transition_checkpoint_clear_keys(
    *,
    production_plan_external_repair: bool,
) -> tuple[str, ...]:
    """Keep paid sibling references during a targeted plan revision."""
    if production_plan_external_repair:
        return ("pending_visual_api_resume",)
    return (
        "pending_visual_api_resume",
        "pending_visual_partial_repair",
        "last_creative_review",
        "quality_pause_preserved_visual_asset_ids",
    )


def queue_stage(
    db: Session, *, project: HermesContentFactoryProject, user_id: int,
    instruction: str | None, target_stage: str | None = None, continue_workflow: bool = True,
    queue_priority: int = 5,
    force_browser: bool = False,
):
    # Stage creation is an idempotent project transition. The browser worker,
    # video completion gate, API request, and periodic self-heal can all reach
    # this function at nearly the same time, so serialize them on the project
    # row and decide from freshly committed state.
    locked_project = (
        db.query(HermesContentFactoryProject)
        .filter(HermesContentFactoryProject.id == int(project.id))
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if locked_project is None:
        raise APIError("CONTENT_PROJECT_NOT_FOUND", "The content project no longer exists.", 404)
    project = locked_project
    if project.status == "paused" or bool(dict(project.config_json or {}).get("manual_paused", False)):
        raise APIError("CONTENT_PROJECT_PAUSED", "This project is paused. Resume it before running a stage.", 409)
    intended_stage = target_stage or project.current_stage
    if target_stage:
        if target_stage not in STAGE_ORDER or target_stage == "COMPLETE":
            raise APIError("CONTENT_STAGE_INVALID", "The requested stage is invalid.", 400)
    if str(intended_stage or "").upper() == "CREATIVE":
        raise APIError(
            "CONTENT_LEGACY_CREATIVE_REMOVED",
            "Projects must run DIRECTOR then PRODUCTION_PLAN; the legacy "
            "CREATIVE authoring stage has been removed.",
            409,
        )
    pre_transition_state = dict(project.state_json or {})
    pre_transition_review = dict(
        pre_transition_state.get("last_creative_review") or {}
    )
    production_plan_external_repair = bool(
        str(intended_stage or "").upper() == "PRODUCTION_PLAN"
        and bool(str(instruction or "").strip())
        and pre_transition_review.get("approved_for_split") is False
    )
    active_rows = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == project.id,
            HermesContentFactoryStage.status.in_(("queued", "running", "retrying")),
        )
        .order_by(HermesContentFactoryStage.id.desc())
        .all()
    )
    for stale_stage in active_rows:
        if stale_stage.stage != intended_stage:
            stale_stage.status = "failed"
            stale_stage.error_message = (
                f"Superseded by project transition to {intended_stage}; "
                "old active stage released before queuing the current breakpoint."
            )
            stale_stage.completed_at = _now()
    db.flush()
    active_stage = next((stage for stage in active_rows if stage.stage == intended_stage and stage.status in {"queued", "running", "retrying"}), None)
    if active_stage is not None:
        # Another caller already won this transition. Reuse the durable row
        # instead of revoking it and opening a second ChatGPT conversation.
        return active_stage
    if project.status in {"queued", "running"}:
        project.status = "ready"
    if target_stage:
        project.current_stage = target_stage
    if str(intended_stage or "").upper() in {
        "SERIES_DIRECTOR",
        "DIRECTOR",
        "PRODUCTION_PLAN",
    }:
        # A paid visual checkpoint is resumable only while continuing the
        # exact creative that produced it. If an operator or quality gate
        # returns to the control plane, carrying a rejected visual checkpoint
        # forward would seed the replacement plan with stale composition.
        fresh_creative_state = dict(project.state_json or {})
        for key in _control_transition_checkpoint_clear_keys(
            production_plan_external_repair=(
                production_plan_external_repair
            ),
        ):
            fresh_creative_state.pop(key, None)
        project.state_json = fresh_creative_state
    if project.current_stage in WAITING_STAGES or project.current_stage == "COMPLETE":
        raise APIError("CONTENT_PROJECT_WAITING", "This project is in a waiting state and cannot queue now.", 409)
    attempt = int(db.query(func.max(HermesContentFactoryStage.attempt)).filter(HermesContentFactoryStage.project_id == project.id, HermesContentFactoryStage.stage == project.current_stage).scalar() or 0) + 1
    last_restart = dict(dict(project.state_json or {}).get("last_restart") or {})
    force_fresh_response = str(last_restart.get("stage") or "").upper() == str(project.current_stage or "").upper()
    state = dict(project.state_json or {})
    config = dict(project.config_json or {})
    creative_replan_requested = bool(production_plan_external_repair)
    force_fresh_response = force_fresh_response or creative_replan_requested
    last_creative_review = dict(state.get("last_creative_review") or {})
    partial_visual_repair = dict(
        state.get("pending_visual_partial_repair")
        or last_creative_review.get("partial_repair")
        or {}
    )
    rejected_visual_repair = (
        str(project.current_stage or "").upper() == "VISUAL_PREVIEW"
        and last_creative_review.get("approved_for_split") is False
        and bool(str(last_creative_review.get("repair_brief") or "").strip())
    )
    force_fresh_response = force_fresh_response or rejected_visual_repair
    variant_pipeline = dict(state.get("video_variant_pipeline") or {})
    variant_total = max(1, min(50, int(config.get("video_count") or variant_pipeline.get("target_count") or 1)))
    variant_index = max(1, min(variant_total, int(variant_pipeline.get("active_index") or state.get("active_variant_index") or 1)))
    if (
        partial_visual_repair
        and int(partial_visual_repair.get("variant_index") or variant_index)
        != int(variant_index)
    ):
        partial_visual_repair = {}
    api_route = None if force_browser else _stage_api_route(db, project.current_stage)
    if (
        str(project.current_stage or "").upper() == "VISUAL_PREVIEW"
        and not force_browser
    ):
        api_route = _visual_variant_api_route(
            db,
            project,
            variant_index=variant_index,
            default_route=api_route,
        )
    execution_backend = stage_execution_backend(
        project.current_stage,
        api_route=api_route,
    )
    if execution_backend != "browser":
        stage = HermesContentFactoryStage(
            project_id=project.id,
            workspace_id=project.workspace_id,
            user_id=user_id,
            stage=project.current_stage,
            attempt=attempt,
            status="queued",
            instruction=(instruction or "").strip() or None,
            input_json={
                "continue_workflow": bool(continue_workflow),
                "execution_backend": execution_backend,
                "api_route": api_route,
                "queue": project_hermes_queue(project),
                "queue_priority": int(queue_priority),
                "self_heal_count": 0,
                "self_heal_policy_version": SELF_HEAL_POLICY_VERSION,
                "restart_generation": int(state.get("restart_count") or 0),
                "force_fresh_response": force_fresh_response,
                "variant_index": variant_index,
                "variant_total": variant_total,
                "variant_mode": "serial_one_complete_video_at_a_time" if variant_total > 1 else "single_batch",
            },
        )
        if creative_replan_requested:
            stage_input = dict(stage.input_json or {})
            semantic_copy_replan = bool(
                "spoken-copy" in str(instruction or "").lower()
                or "spoken copy" in str(instruction or "").lower()
                or "per-segment" in str(instruction or "").lower()
            )
            stage_input.update({
                "force_fresh_response": True,
                "discard_durable_response_capture": True,
                "production_plan_external_repair": (
                    production_plan_external_repair
                ),
                "production_plan_repair_instruction": (
                    str(instruction or "").strip()[:4000]
                    if production_plan_external_repair
                    else None
                ),
                "self_heal_action": (
                    "revise_signed_production_plan_after_visual_rejection"
                    if production_plan_external_repair
                    else "regenerate_materially_new_creative_after_semantic_copy_rejection"
                    if semantic_copy_replan
                    else "regenerate_materially_new_creative_after_visual_rejection"
                ),
            })
            stage.input_json = stage_input
        if (
            str(project.current_stage or "").upper() == "SERIES_DIRECTOR"
            and not force_fresh_response
        ):
            prior_series_stage = (
                db.query(HermesContentFactoryStage)
                .filter(
                    HermesContentFactoryStage.project_id == project.id,
                    HermesContentFactoryStage.stage == "SERIES_DIRECTOR",
                    HermesContentFactoryStage.status.in_((
                        "failed",
                        "paused",
                    )),
                )
                .order_by(HermesContentFactoryStage.id.desc())
                .first()
            )
            prior_input = dict(
                prior_series_stage.input_json or {}
            ) if prior_series_stage is not None else {}
            series_checkpoint = prior_input.get(
                "series_director_page_checkpoint"
            )
            if isinstance(series_checkpoint, dict) and series_checkpoint:
                # Manual resume creates a successor stage, but it must retain
                # the immutable, same-project zero-media checkpoint instead of
                # paying to recreate already reviewed coverage/pages. The
                # stage runner recomputes the plan signature and discards this
                # checkpoint if truth, history, profile, or missing indices
                # changed. An explicit restart sets force_fresh_response and
                # deliberately bypasses this inheritance.
                stage_input = dict(stage.input_json or {})
                stage_input[
                    "series_director_page_checkpoint"
                ] = dict(series_checkpoint)
                for checkpoint_key in (
                    "series_director_plan_signature",
                    "series_director_planned_variant_indices",
                    "completed_content_history_sha256",
                ):
                    if checkpoint_key in prior_input:
                        stage_input[checkpoint_key] = prior_input[
                            checkpoint_key
                        ]
                stage_input[
                    "resumed_series_checkpoint_stage_id"
                ] = int(prior_series_stage.id)
                stage_input["self_heal_action"] = (
                    "resume_series_director_from_quality_checkpoint"
                )
                stage.input_json = stage_input
        pending_visual_resume = dict(state.get("pending_visual_api_resume") or {})
        if (
            str(project.current_stage or "").upper() == "VISUAL_PREVIEW"
            and int(pending_visual_resume.get("variant_index") or 0) == int(variant_index)
            and isinstance(pending_visual_resume.get("visual_api"), dict)
        ):
            resumed_visual_api = dict(pending_visual_resume["visual_api"])
            resumed_boards = {
                str(index): dict(board)
                for index, board in dict(resumed_visual_api.get("boards") or {}).items()
                if isinstance(board, dict)
                and _is_resumable_visual_board(board)
            }
            if resumed_boards:
                resumed_visual_api["boards"] = resumed_boards
                resumed_visual_api["status"] = "partial_resumable"
                stage_input = dict(stage.input_json or {})
                stage_input = _restore_visual_resume_instruction(
                    stage,
                    pending_visual_resume,
                    stage_input,
                )
                stage_input["visual_api"] = resumed_visual_api
                stage_input["resumed_visual_checkpoint_stage_id"] = int(
                    pending_visual_resume.get("source_stage_id") or 0
                )
                stage_input["self_heal_action"] = (
                    "resume_visual_api_from_downloaded_reference_checkpoint"
                )
                if str(api_route or "").strip() == "toapis:gpt-image-2":
                    stage_input["visual_api_skip_bandianwa"] = True
                # The checkpoint contains provider progress, never execution
                # policy authority. A stale paused delivery must not downgrade
                # the successor row's policy stamp.
                stage_input["self_heal_policy_version"] = (
                    SELF_HEAL_POLICY_VERSION
                )
                stage.input_json = stage_input
                state.pop("pending_visual_api_resume", None)
                project.state_json = state
        if rejected_visual_repair:
            stage_input = dict(stage.input_json or {})
            stage_input.update(
                {
                    "force_fresh_response": True,
                    "self_heal_action": "regenerate_visual_after_blocking_creative_review",
                    "visual_repair_instruction": str(last_creative_review.get("repair_brief") or "")[:4000],
                    "visual_repair_failed_indices": list(
                        partial_visual_repair.get("failed_indices") or []
                    ),
                    "visual_repair_preserved_references": list(
                        partial_visual_repair.get("preserved_references") or []
                    ),
                    "visual_repair_source_review_stage_id": int(
                        partial_visual_repair.get("source_review_stage_id") or 0
                    ),
                    "visual_api_skip_bandianwa": (
                        str(api_route or "").strip() == "toapis:gpt-image-2"
                    ),
                }
            )
            stage.input_json = stage_input
        db.add(stage)
        project.status = "queued"
        project.last_error = None
        _record_project_transition(
            project, stage=project.current_stage, status="queued", reason="api_stage_queued"
        )
        # A sticky browser profile remains owned by this project, but API work
        # must not keep Chrome or its SSH tunnel alive. Reassert dormancy on
        # every API stage transition because a prior browser fallback may have
        # explicitly woken this exact slot.
        hibernate_project_browser_slot_for_api_video(
            db,
            project=project,
            active_stage=stage,
        )
        db.flush()
        db.commit()
        from app.tasks.hermes_agent.content_factory_tasks import run_content_factory_stage

        run_token = uuid4().hex
        stage_input = dict(stage.input_json or {})
        stage_input["run_token"] = run_token
        stage.input_json = stage_input
        db.commit()
        task = run_content_factory_stage.apply_async(
            kwargs={"stage_id": int(stage.id), "run_token": run_token},
            queue=project_hermes_queue(project),
            priority=max(0, min(9, int(queue_priority))),
        )
        stage.celery_task_id = task.id
        db.commit()
        db.refresh(stage)
        return stage
    slot_request_state = dict(project.state_json or {})
    preferred_device_id = str(slot_request_state.get("preferred_browser_device_id") or "").strip()
    if not preferred_device_id:
        preferred_device_id, selection_required = _effective_browser_device_id(
            browser_devices(db, workspace_id=int(project.workspace_id), user_id=int(user_id))
        )
        if selection_required:
            raise APIError(
                "CONTENT_BROWSER_DEVICE_SELECTION_REQUIRED",
                "多个已绑定设备在线，请先在内容工厂选择本项目使用的设备。",
                409,
            )
        if preferred_device_id:
            slot_request_state["preferred_browser_device_id"] = preferred_device_id[:128]
    if preferred_device_id:
        slot_request_state["browser_slot_requested_at"] = _now().isoformat()
        slot_request_state["browser_slot_request_stage"] = str(project.current_stage or intended_stage)
        project.state_json = slot_request_state
        db.add(project)
        # The local agent reads this request in a separate heartbeat transaction.
        db.commit()
    try:
        bridge = _acquire_project_bridge(db, project=project, user_id=int(user_id))
    except APIError as exc:
        if exc.code in {
            "CONTENT_BROWSER_BRIDGE_REQUIRED",
            "CONTENT_BROWSER_BRIDGE_OFFLINE",
            "CONTENT_BROWSER_CAPACITY_FULL",
            "CONTENT_BROWSER_LOGIN_REQUIRED",
        }:
            wait_state = dict(project.state_json or slot_request_state)
            wait_state["browser_slot_requested_at"] = str(
                wait_state.get("browser_slot_requested_at") or _now().isoformat()
            )
            wait_state["browser_slot_request_stage"] = str(
                wait_state.get("browser_slot_request_stage") or project.current_stage or intended_stage
            )
            wait_state["browser_slot_wait_error_code"] = str(exc.code)
            wait_state["browser_slot_wait_error_at"] = _now().isoformat()
            project.state_json = wait_state
            project.status = "waiting_bridge"
            project.last_error = str(exc.message or "Waiting for an available browser slot.")[:4000]
            db.add(project)
            db.commit()
        raise
    stage = HermesContentFactoryStage(
        project_id=project.id, workspace_id=project.workspace_id, user_id=user_id,
        stage=project.current_stage, attempt=attempt, status="queued",
        instruction=(instruction or "").strip() or None,
        input_json={
            "continue_workflow": bool(continue_workflow),
            "browser_slot": bridge.bridge_id,
            "browser_bridge_id": bridge.bridge_id,
            "browser_device_id": bridge.device_id,
            "browser_device_name": bridge.device_name,
            "browser_cdp_url": bridge.cdp_url,
            "browser_inbox_root": bridge.inbox_root,
            "queue": project_hermes_queue(project),
            "queue_priority": int(queue_priority),
            "self_heal_count": 0,
            "self_heal_policy_version": SELF_HEAL_POLICY_VERSION,
            "restart_generation": int(state.get("restart_count") or 0),
            "force_fresh_response": force_fresh_response,
            "variant_index": variant_index,
            "variant_total": variant_total,
            "variant_mode": "serial_one_complete_video_at_a_time" if variant_total > 1 else "single_batch",
            "api_fallback_to_browser": bool(force_browser),
        },
    )
    if rejected_visual_repair:
        stage_input = dict(stage.input_json or {})
        stage_input.update({
            "force_fresh_response": True,
            "clear_stale_composer_before_send": True,
            "allow_visible_visual_recovery": False,
            "self_heal_action": "regenerate_visual_after_blocking_creative_review",
            "visual_repair_instruction": str(last_creative_review.get("repair_brief") or "")[:4000],
            "visual_repair_failed_indices": list(
                partial_visual_repair.get("failed_indices") or []
            ),
            "visual_repair_preserved_references": list(
                partial_visual_repair.get("preserved_references") or []
            ),
            "visual_repair_source_review_stage_id": int(
                partial_visual_repair.get("source_review_stage_id") or 0
            ),
        })
        stage.input_json = stage_input
    db.add(stage)
    project.status = "queued"
    project.last_error = None
    _record_project_transition(project, stage=project.current_stage, status="queued", reason="stage_queued")
    db.flush()
    bridge.active_stage_id = int(stage.id)
    db.add(bridge)
    db.flush()
    # Persist the stage before publishing its message so a fast worker can always load it.
    db.commit()
    from app.tasks.hermes_agent.content_factory_tasks import run_content_factory_stage
    run_token = uuid4().hex
    stage_input = dict(stage.input_json or {})
    stage_input["run_token"] = run_token
    stage.input_json = stage_input
    db.commit()
    task = run_content_factory_stage.apply_async(
        kwargs={"stage_id": int(stage.id), "run_token": run_token},
        queue=project_hermes_queue(project),
        priority=max(0, min(9, int(queue_priority))),
    )
    stage.celery_task_id = task.id
    db.commit()
    db.refresh(stage)
    return stage
