from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Literal
from uuid import uuid4

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from app.core.errors import APIError
from app.data.models.hermes_agent import (
    HermesAgentConversation,
    HermesAgentMessage,
    HermesContentFactoryAsset,
    HermesContentFactoryProject,
    HermesContentProducerAttachment,
    HermesContentProduct,
)
from app.services.hermes_agent import repository
from app.services.hermes_agent.client import (
    HermesContentProducerClient,
    extract_output_text,
)


PRODUCER_TASK_TYPE = "content_producer"
PRODUCER_PROMPT_VERSION = "content_producer_v4"
MAX_HISTORY_MESSAGES = 40
MAX_MODEL_CONVERSATION_CHARS = 120000
MAX_MODEL_SOURCE_TEXT_CHARS = 120000
MAX_SOURCE_TEXT_ASSETS = 20
PRODUCER_STORAGE_ROOT = Path(
    os.getenv("CONTENT_FACTORY_STORAGE_ROOT", "/data/gmv_ops/hermes_content_factory")
).expanduser() / "producer_intake"
MAX_PRODUCER_ATTACHMENTS = 20
MAX_PRODUCER_REFERENCE_VIDEO_BYTES = 200 * 1024 * 1024
MAX_PRODUCER_CHARACTER_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PRODUCER_REFERENCE_VIDEOS = 1
MAX_PRODUCER_CHARACTER_IMAGES = 16
MAX_FAST_ENGLISH_WORDS_PER_MINUTE = 220
MAX_FAST_CHINESE_CHARACTERS_PER_MINUTE = 320
MAX_AUTHORITATIVE_SCRIPT_VERSIONS = 50
PRODUCER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PRODUCER_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}


class ContentProducerProposal(BaseModel):
    """User-facing choices only; product truth is never authored here."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    content_objective: str = Field(min_length=1, max_length=255)
    target_audience: str = Field(min_length=1, max_length=1000)
    content_mode: Literal["product", "general"]
    platform: str = Field(default="tiktok", min_length=1, max_length=64)
    video_count: int = Field(ge=1, le=50)
    video_duration_min_seconds: int = Field(ge=1, le=120)
    video_duration_max_seconds: int = Field(ge=1, le=120)
    video_model: Literal["omni_flash", "seedance_2_0_mini"] = "omni_flash"
    video_resolution: Literal["480p", "720p"] = "720p"
    video_aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    video_language: Literal["en-US", "zh-CN"] = "en-US"
    visual_style: str = Field(min_length=1, max_length=1000)
    pacing: str = Field(min_length=1, max_length=500)
    audio_direction: str = Field(min_length=1, max_length=1000)
    conversion_direction: str | None = Field(default=None, max_length=1000)
    creative_constraints: list[str] = Field(default_factory=list, max_length=32)
    visual_reference_generation_mode: Literal["individual", "board"] = "individual"
    confirmed_offer: str | None = Field(default=None, max_length=500)
    promotion_evidence_quote: str | None = Field(default=None, max_length=1000)
    confidence: float = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="after")
    def _duration_order(self) -> "ContentProducerProposal":
        if self.video_duration_min_seconds > self.video_duration_max_seconds:
            raise ValueError("minimum video duration cannot exceed maximum")
        if self.video_model == "omni_flash" and not any(
            self.video_duration_min_seconds <= value <= self.video_duration_max_seconds
            for value in range(10, 121, 10)
        ):
            raise ValueError(
                "Omni Flash duration range must contain a multiple of 10 seconds"
            )
        return self


class ContentProducerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["needs_input", "proposal_ready"]
    assistant_message: str = Field(min_length=1, max_length=4000)
    missing_information: list[str] = Field(default_factory=list, max_length=5)
    proposal: ContentProducerProposal | None = None
    changed_fields: list[str] = Field(default_factory=list, max_length=32)
    change_evidence: dict[str, str] = Field(default_factory=dict)
    authoritative_script_message_id: int | None = Field(default=None, ge=1)
    revised_authoritative_script: str | None = Field(default=None, max_length=50000)
    script_revision_evidence: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _status_contract(self) -> "ContentProducerDecision":
        if self.status == "proposal_ready" and self.proposal is None:
            raise ValueError("proposal_ready requires proposal")
        if self.status == "needs_input" and not self.missing_information:
            raise ValueError("needs_input requires one concise missing-information item")
        invalid_fields = sorted(
            set(self.changed_fields) - set(ContentProducerProposal.model_fields)
        )
        if invalid_fields:
            raise ValueError(f"unknown changed proposal fields: {invalid_fields}")
        return self


def _compact(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return str(value)[:500]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _compact(item, depth=depth + 1)
            for key, item in list(value.items())[:40]
        }
    if isinstance(value, list):
        return [_compact(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def _json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model response did not contain a JSON object")
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model response must be one JSON object")
    return parsed


def _conversation_for_user(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    session_key: str,
) -> HermesAgentConversation:
    key = repository.conversation_key(
        workspace_id=workspace_id,
        user_id=user_id,
        task_type=PRODUCER_TASK_TYPE,
        key=session_key,
    )
    row = (
        db.query(HermesAgentConversation)
        .filter(
            HermesAgentConversation.conversation_key == key,
            HermesAgentConversation.workspace_id == int(workspace_id),
            HermesAgentConversation.user_id == int(user_id),
            HermesAgentConversation.task_type == PRODUCER_TASK_TYPE,
        )
        .one_or_none()
    )
    if row is None:
        raise APIError(
            "CONTENT_PRODUCER_SESSION_NOT_FOUND",
            "The AI producer conversation was not found.",
            404,
        )
    return row


def _history(
    db: Session,
    *,
    conversation: HermesAgentConversation,
) -> list[HermesAgentMessage]:
    rows = (
        db.query(HermesAgentMessage)
        .filter(
            HermesAgentMessage.conversation_id == int(conversation.id),
            HermesAgentMessage.workspace_id == int(conversation.workspace_id),
            HermesAgentMessage.user_id == int(conversation.user_id),
        )
        .order_by(HermesAgentMessage.id.desc())
        .limit(MAX_HISTORY_MESSAGES)
        .all()
    )
    rows.reverse()
    return rows


def _deduplicate_adjacent_messages(
    rows: list[HermesAgentMessage],
) -> list[HermesAgentMessage]:
    deduplicated: list[HermesAgentMessage] = []
    for row in rows:
        if (
            deduplicated
            and deduplicated[-1].role == row.role
            and str(deduplicated[-1].content_text or "").strip()
            == str(row.content_text or "").strip()
        ):
            deduplicated[-1] = row
        else:
            deduplicated.append(row)
    return deduplicated


def _safe_attachment_name(value: str) -> str:
    name = Path(str(value or "file")).name
    return re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", name)[:180] or f"file_{uuid4().hex[:8]}"


def _attachment_rows(
    db: Session,
    *,
    conversation: HermesAgentConversation,
) -> list[HermesContentProducerAttachment]:
    return list(
        db.query(HermesContentProducerAttachment)
        .filter(
            HermesContentProducerAttachment.conversation_id == int(conversation.id),
            HermesContentProducerAttachment.workspace_id == int(conversation.workspace_id),
            HermesContentProducerAttachment.user_id == int(conversation.user_id),
        )
        .order_by(HermesContentProducerAttachment.id.asc())
        .all()
    )


def producer_attachment_out(row: HermesContentProducerAttachment) -> dict[str, Any]:
    meta = dict(row.meta_json or {})
    return {
        "attachment_key": row.attachment_key,
        "kind": row.kind,
        "original_name": row.original_name,
        "mime_type": row.mime_type,
        "size_bytes": int(row.size_bytes or 0),
        "analysis_status": row.analysis_status,
        "analysis": dict(row.analysis_json or {}),
        "character_name": meta.get("character_name"),
        "character_description": meta.get("character_description"),
        "created_at": row.created_at,
    }


def producer_attachments(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    session_key: str,
) -> tuple[HermesAgentConversation, list[HermesContentProducerAttachment]]:
    conversation = _conversation_for_user(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        session_key=session_key,
    )
    return conversation, _attachment_rows(db, conversation=conversation)


def get_or_create_producer_conversation(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    session_key: str,
) -> HermesAgentConversation:
    normalized_session = re.sub(
        r"[^a-zA-Z0-9_.-]+", "-", str(session_key or "").strip()
    ).strip("-._")[:48]
    if not normalized_session:
        raise APIError(
            "CONTENT_PRODUCER_SESSION_KEY_REQUIRED",
            "A valid producer session key is required.",
            400,
        )
    conversation = repository.get_or_create_conversation(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        task_type=PRODUCER_TASK_TYPE,
        title="AI制片助理",
        key=normalized_session,
    )
    meta = dict(conversation.meta_json or {})
    meta.setdefault("session_key", normalized_session)
    meta.setdefault("status", "idle")
    conversation.meta_json = meta
    db.add(conversation)
    db.flush()
    return conversation


def _ensure_intake_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o775)
    except OSError:
        pass


def _mark_intake_file(path: Path) -> None:
    try:
        path.chmod(0o664)
    except OSError:
        pass


async def _write_intake_upload(upload: UploadFile, target: Path, *, max_bytes: int) -> tuple[int, str]:
    declared = getattr(upload, "size", None)
    if declared is not None and int(declared) > int(max_bytes):
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENT_TOO_LARGE",
            f"The attachment exceeds the {max_bytes // (1024 * 1024)} MB limit.",
            413,
        )
    written = 0
    digest = hashlib.sha256()
    try:
        with target.open("xb") as stream:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > int(max_bytes):
                    raise APIError(
                        "CONTENT_PRODUCER_ATTACHMENT_TOO_LARGE",
                        f"The attachment exceeds the {max_bytes // (1024 * 1024)} MB limit.",
                        413,
                    )
                digest.update(chunk)
                stream.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if written <= 0:
        target.unlink(missing_ok=True)
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENT_EMPTY",
            "The uploaded attachment is empty.",
            400,
        )
    _mark_intake_file(target)
    return written, digest.hexdigest()


def _render_image_preview(source: Path, target: Path) -> dict[str, Any]:
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
            if width < 64 or height < 64:
                raise ValueError("image is too small")
            image = image.convert("RGB")
            image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            image.save(target, format="JPEG", quality=84, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise APIError(
            "CONTENT_PRODUCER_CHARACTER_REFERENCE_INVALID",
            "Character reference must be a valid JPG, PNG, or WebP image.",
            400,
        ) from exc
    _mark_intake_file(target)
    return {"width": int(width), "height": int(height), "preview_available": True}


def _probe_reference_video(source: Path) -> dict[str, Any]:
    command = [
        "/opt/apps/bin/ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json",
        str(source),
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        payload = json.loads(completed.stdout or "{}")
        stream = list(payload.get("streams") or [{}])[0]
        duration = float(dict(payload.get("format") or {}).get("duration") or 0)
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise APIError(
            "CONTENT_PRODUCER_REFERENCE_VIDEO_INVALID",
            "Reference video must be a readable MP4, MOV, or WebM file.",
            400,
        ) from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise APIError(
            "CONTENT_PRODUCER_REFERENCE_VIDEO_INVALID",
            "Reference video does not contain a readable video track.",
            400,
        )
    return {"duration_seconds": round(duration, 2), "width": width, "height": height}


def _render_video_preview(source: Path, target: Path, *, duration_seconds: float) -> None:
    sampling_fps = max(0.01, min(12.0, 12.0 / max(float(duration_seconds), 1.0)))
    video_filter = (
        f"fps={sampling_fps:.6f},"
        "scale=320:240:force_original_aspect_ratio=decrease,"
        "pad=320:240:(ow-iw)/2:(oh-ih)/2:color=black,"
        "tile=3x4:padding=4:margin=4"
    )
    command = [
        "/opt/apps/bin/ffmpeg",
        "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-vf", video_filter,
        "-frames:v", "1",
        str(target),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=90)
    except subprocess.SubprocessError as exc:
        raise APIError(
            "CONTENT_PRODUCER_REFERENCE_VIDEO_PREVIEW_FAILED",
            "The reference video could not be prepared for AI review.",
            400,
        ) from exc
    if not target.is_file() or target.stat().st_size <= 1024:
        raise APIError(
            "CONTENT_PRODUCER_REFERENCE_VIDEO_PREVIEW_FAILED",
            "The reference video did not produce a usable visual preview.",
            400,
        )
    _mark_intake_file(target)


async def save_producer_attachment(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    user_id: int,
    upload: UploadFile,
    kind: str,
    character_key: str | None = None,
    character_name: str | None = None,
    character_description: str | None = None,
) -> HermesContentProducerAttachment:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {"reference_video", "character_reference"}:
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENT_KIND_INVALID",
            "Choose reference_video or character_reference.",
            400,
        )
    existing = _attachment_rows(db, conversation=conversation)
    if len(existing) >= MAX_PRODUCER_ATTACHMENTS:
        raise APIError(
            "CONTENT_PRODUCER_TOO_MANY_ATTACHMENTS",
            f"Upload at most {MAX_PRODUCER_ATTACHMENTS} attachments per conversation.",
            400,
        )
    filename = _safe_attachment_name(upload.filename or "file")
    extension = Path(filename).suffix.lower()
    mime = str(upload.content_type or "").lower()
    if normalized_kind == "reference_video":
        if sum(1 for item in existing if item.kind == normalized_kind) >= MAX_PRODUCER_REFERENCE_VIDEOS:
            raise APIError(
                "CONTENT_PRODUCER_TOO_MANY_REFERENCE_VIDEOS",
                "Upload one benchmark/reference video per conversation.",
                400,
            )
        if extension not in PRODUCER_VIDEO_EXTENSIONS and not mime.startswith("video/"):
            raise APIError(
                "CONTENT_PRODUCER_REFERENCE_VIDEO_INVALID",
                "Reference video must be MP4, MOV, or WebM.",
                400,
            )
        max_bytes = MAX_PRODUCER_REFERENCE_VIDEO_BYTES
    else:
        if sum(1 for item in existing if item.kind == normalized_kind) >= MAX_PRODUCER_CHARACTER_IMAGES:
            raise APIError(
                "CONTENT_PRODUCER_TOO_MANY_CHARACTER_REFERENCES",
                f"Upload at most {MAX_PRODUCER_CHARACTER_IMAGES} character images.",
                400,
            )
        if extension not in PRODUCER_IMAGE_EXTENSIONS and not mime.startswith("image/"):
            raise APIError(
                "CONTENT_PRODUCER_CHARACTER_REFERENCE_INVALID",
                "Character reference must be JPG, PNG, or WebP.",
                400,
            )
        max_bytes = MAX_PRODUCER_CHARACTER_IMAGE_BYTES

    session_key = str(dict(conversation.meta_json or {}).get("session_key") or "session")
    intake_dir = (
        PRODUCER_STORAGE_ROOT
        / f"workspace_{int(conversation.workspace_id)}"
        / f"user_{int(user_id)}"
        / re.sub(r"[^A-Za-z0-9_.-]+", "-", session_key)[:64]
    )
    _ensure_intake_dir(intake_dir)
    attachment_key = f"pa_{uuid4().hex}"
    source = intake_dir / f"{attachment_key}_{filename}"
    preview = intake_dir / f"{attachment_key}_preview.jpg"
    written = 0
    try:
        written, digest = await _write_intake_upload(upload, source, max_bytes=max_bytes)
        if normalized_kind == "character_reference":
            analysis = _render_image_preview(source, preview)
        else:
            analysis = _probe_reference_video(source)
            _render_video_preview(
                source,
                preview,
                duration_seconds=float(analysis["duration_seconds"]),
            )
            analysis["preview_available"] = True
            analysis["transcript_status"] = "queued"
        normalized_character_key = re.sub(
            r"[^A-Za-z0-9_-]+", "_", str(character_key or "")
        ).strip("-_")[:64]
        row = HermesContentProducerAttachment(
            attachment_key=attachment_key,
            conversation_id=int(conversation.id),
            workspace_id=int(conversation.workspace_id),
            user_id=int(user_id),
            kind=normalized_kind,
            original_name=filename,
            file_path=str(source),
            preview_path=str(preview),
            mime_type=upload.content_type,
            size_bytes=written,
            sha256=digest,
            analysis_status=(
                "processing" if normalized_kind == "reference_video" else "ready"
            ),
            analysis_json=analysis,
            meta_json={
                "source": "producer_intake",
                "character_key": normalized_character_key or f"character_{uuid4().hex[:12]}",
                "character_name": str(character_name or Path(filename).stem or "Character")[:120],
                "character_description": str(character_description or "")[:2000],
            } if normalized_kind == "character_reference" else {"source": "producer_intake"},
        )
        db.add(row)
        meta = dict(conversation.meta_json or {})
        meta.update({"status": "idle", "attachments_updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
        meta.pop("proposal", None)
        meta.pop("proposal_sha256", None)
        conversation.meta_json = meta
        db.add(conversation)
        db.flush()
        return row
    except Exception:
        source.unlink(missing_ok=True)
        preview.unlink(missing_ok=True)
        raise


def delete_producer_attachment(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    attachment_key: str,
) -> list[Path]:
    row = (
        db.query(HermesContentProducerAttachment)
        .filter(
            HermesContentProducerAttachment.attachment_key == str(attachment_key),
            HermesContentProducerAttachment.conversation_id == int(conversation.id),
            HermesContentProducerAttachment.workspace_id == int(conversation.workspace_id),
            HermesContentProducerAttachment.user_id == int(conversation.user_id),
        )
        .one_or_none()
    )
    if row is None:
        raise APIError("CONTENT_PRODUCER_ATTACHMENT_NOT_FOUND", "Attachment not found.", 404)
    if row.project_asset_id is not None or dict(conversation.meta_json or {}).get("created_project_id"):
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENT_LOCKED",
            "The attachment is already bound to a confirmed project.",
            409,
        )
    paths = [Path(row.file_path), Path(row.preview_path)] if row.preview_path else [Path(row.file_path)]
    db.delete(row)
    meta = dict(conversation.meta_json or {})
    meta.update({"status": "idle", "attachments_updated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
    meta.pop("proposal", None)
    meta.pop("proposal_sha256", None)
    conversation.meta_json = meta
    db.add(conversation)
    db.flush()
    return paths


def _attachment_data_url(path: Path) -> str:
    payload = path.read_bytes()
    if len(payload) > 4 * 1024 * 1024:
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENT_PREVIEW_TOO_LARGE",
            "The attachment preview is too large for AI review.",
            400,
        )
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _staged_attachment_path(path: str) -> Path:
    root = PRODUCER_STORAGE_ROOT.resolve()
    source = Path(path).resolve()
    if source == root or root not in source.parents:
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENT_PATH_INVALID",
            "The staged attachment path is outside the producer intake area.",
            409,
        )
    return source


def _producer_input_items(
    packet_json: str,
    attachments: list[HermesContentProducerAttachment],
) -> list[dict[str, Any]] | None:
    ready = [row for row in attachments if row.analysis_status == "ready" and row.preview_path]
    if not ready:
        return None
    content: list[dict[str, Any]] = [{"type": "input_text", "text": packet_json}]
    for index, row in enumerate(ready[:17], start=1):
        label = (
            f"Attachment {index}: {row.kind}; authoritative user upload name={row.original_name}. "
            "Inspect visible pixels and use them only for the requested creative planning role."
        )
        content.append({"type": "input_text", "text": label})
        content.append({"type": "input_image", "image_url": _attachment_data_url(Path(row.preview_path)), "detail": "high"})
    return [{"role": "user", "content": content}]


def copy_producer_attachments_to_project(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    project: HermesContentFactoryProject,
    user_id: int,
    storage_root: Path,
    browser_inbox: Path,
) -> list[HermesContentFactoryAsset]:
    project_dir = storage_root / f"workspace_{project.workspace_id}" / project.project_key / "assets"
    bridge_dir = browser_inbox / f"workspace_{project.workspace_id}" / project.project_key
    _ensure_intake_dir(project_dir)
    _ensure_intake_dir(bridge_dir)
    rows: list[HermesContentFactoryAsset] = []
    for attachment in _attachment_rows(db, conversation=conversation):
        if attachment.project_asset_id is not None:
            existing = db.query(HermesContentFactoryAsset).filter(
                HermesContentFactoryAsset.id == int(attachment.project_asset_id),
                HermesContentFactoryAsset.project_id == int(project.id),
            ).one_or_none()
            if existing is not None:
                rows.append(existing)
                continue
            raise APIError(
                "CONTENT_PRODUCER_ATTACHMENT_BINDING_INVALID",
                "A staged attachment has an invalid project binding.",
                409,
            )
        source = _staged_attachment_path(attachment.file_path)
        if not source.is_file() or _file_sha256(source) != attachment.sha256:
            raise APIError(
                "CONTENT_PRODUCER_ATTACHMENT_FILE_MISSING",
                "A staged attachment is missing or changed. Upload it again before confirmation.",
                409,
            )
        disk_name = f"producer_{attachment.attachment_key}_{_safe_attachment_name(attachment.original_name)}"
        target = project_dir / disk_name
        bridge_target = bridge_dir / disk_name
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        shutil.copy2(source, bridge_target)
        _mark_intake_file(target)
        _mark_intake_file(bridge_target)
        meta = dict(attachment.meta_json or {})
        meta.update({
            "source": "producer_intake",
            "producer_attachment_key": attachment.attachment_key,
            "bridge_path": str(bridge_target),
            "browser_inbox_relative": f"workspace_{project.workspace_id}/{project.project_key}/{disk_name}",
            "asset_role": attachment.kind,
        })
        row = HermesContentFactoryAsset(
            project_id=int(project.id),
            workspace_id=int(project.workspace_id),
            user_id=int(user_id),
            stage="REFERENCE_VIDEO" if attachment.kind == "reference_video" else "CHARACTER_REFERENCE",
            kind=attachment.kind,
            original_name=attachment.original_name,
            file_path=str(target),
            mime_type=attachment.mime_type,
            size_bytes=int(attachment.size_bytes),
            meta_json=meta,
        )
        db.add(row)
        db.flush()
        attachment.project_asset_id = int(row.id)
        db.add(attachment)
        rows.append(row)
    return rows


def _selected_product(
    db: Session,
    *,
    workspace_id: int,
    product_id: int | None,
) -> HermesContentProduct | None:
    if product_id is None:
        return None
    row = (
        db.query(HermesContentProduct)
        .filter(
            HermesContentProduct.id == int(product_id),
            HermesContentProduct.workspace_id == int(workspace_id),
            HermesContentProduct.status == "active",
        )
        .one_or_none()
    )
    if row is None:
        raise APIError(
            "CONTENT_PRODUCT_NOT_FOUND",
            "The selected product is not available in this company library.",
            404,
        )
    return row


def _catalog(db: Session, *, workspace_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(HermesContentProduct)
        .filter(
            HermesContentProduct.workspace_id == int(workspace_id),
            HermesContentProduct.status == "active",
        )
        .order_by(HermesContentProduct.updated_at.desc())
        .limit(30)
        .all()
    )
    return [
        {
            "id": int(row.id),
            "brand_name": row.brand_name,
            "product_name": row.product_name,
            "market": row.market,
            "facts_ready": bool(row.facts_json),
        }
        for row in rows
    ]


def _instructions() -> str:
    return """
You are the front-desk AI Creative Producer for a general short-video factory.
Understand the user's real goal and turn the conversation into a production
proposal. Output exactly one JSON object matching the supplied contract.

Rules:
- Be concise and friendly. assistant_message must be Simplified Chinese.
- The packet is the authoritative current working brief. Hermes conversation
  history helps natural continuity, but it never overrides the packet.
- On an existing proposal, interpret the latest_user_message as a DELTA. Keep
  every field the user did not ask to change exactly unchanged. Never silently
  replace an agreed duration, count, audience, character, style, voice, script,
  conversion direction or constraint with a fresh recommendation.
- For every changed proposal field, list that field in changed_fields and put
  one exact verbatim quote from latest_user_message in change_evidence. Do not
  cite an older user message, an assistant message or your own inference. If no
  proposal field changes, return empty changed_fields and change_evidence.
- authoritative_source_texts are immutable user source assets such as supplied
  scripts. Preserve them verbatim and design around them unless the latest user
  message explicitly asks to revise them.
- current_authoritative_script is the latest durable version of the locked
  script. It overrides the original immutable source text after an authorized
  revision. Never fall back to an older script version.
- If one authoritative_source_text is a complete audience-facing script the
  user wants produced, set authoritative_script_message_id to its message_id.
  Keep the existing authoritative script id on later turns unless the latest
  user explicitly replaces or removes that script. Do not mark a planning brief
  or general requirements as a verbatim script.
- If the latest user message explicitly asks to edit the locked script, return
  the complete revised script in revised_authoritative_script, changing only
  what the user authorized. script_revision_evidence must be one exact quote
  from latest_user_message that authorizes that edit. Otherwise both fields
  must be null. Never merely describe a script edit without returning the full
  revised script.
- Infer professional defaults for duration, quantity, style, pacing, language,
  aspect ratio and audio. Do not make the user fill a technical form.
- Ask at most one short grouped question only when a true blocker remains.
  Missing an exact duration, count or style is not a blocker; recommend them.
- Do not use a fixed story template. Select a format from the user's goal,
  audience, platform and supplied truth.
- Inspect every supplied attachment preview. Character images are identity and
  appearance references, not product or claim evidence. A reference-video
  preview is a sampled visual contact sheet; use it for visible style and
  framing. When technical_summary.transcript_status is success, also use its
  timestamped transcript as the video's speech evidence. When it is no_speech
  or failed, never claim to know audio that is not present in the packet.
- Summarize how the attachments will be used in assistant_message so the user
  can correct your interpretation before confirming the project.
- selected_product.stable_facts are authoritative for durable product identity,
  ingredients, package details, approved claims and warnings. Never invent,
  rewrite or strengthen them.
- selected_product.attribute_notes are stable product background only. They are
  never authority for a price, discount, shipping offer, video duration,
  platform, character, visual style, pacing or CTA. The latest explicit user
  statement controls project-specific commercial terms. If the user cancels a
  prior offer, remove it from the proposal and revised script.
- promotion_evidence_quote must be null unless it is an exact verbatim quote
  from a USER message. Never quote an assistant message or product metadata.
- confirmed_offer is the normalized, currently active project offer only. It
  may contain only price and shipping terms supported by
  promotion_evidence_quote. Never include an old, canceled or comparison offer
  in confirmed_offer. Evidence is for audit; confirmed_offer is what production
  may say.
- If a selected product is supplied, product mode may use only that product.
  If product content is requested but no product is selected and several are
  available, ask which one. General non-product content is allowed.
- For TikTok, normally use 9:16 and en-US only when the intended audience is
  the US; respect explicit user instructions over defaults.
- Omni Flash projects require a duration range containing a multiple of ten.
- A complete locked script must fit the proposed duration at a fast but
  intelligible speaking rate. Never compress a full script into an impossible
  duration. If the user explicitly demands an incompatible duration, ask
  whether to extend the video or authorize shortening the script.
- Audio direction must make narrator voice, character dialogue and identity
  continuity explicit. Do not assume that narrator gender changes a speaking
  character's gender.
- Prefer proposal_ready once the goal is understandable. The user will review
  and explicitly confirm before any project is created or any media is spent.

JSON contract:
{
  "status": "needs_input" | "proposal_ready",
  "assistant_message": "short Chinese summary or one grouped question",
  "missing_information": ["only true blockers"],
  "changed_fields": ["only fields explicitly changed this turn"],
  "change_evidence": {"video_count": "exact quote from latest_user_message"},
  "authoritative_script_message_id": null | 123,
  "revised_authoritative_script": null | "complete revised script",
  "script_revision_evidence": null | "exact quote authorizing the edit",
  "proposal": null | {
    "title": "...",
    "content_objective": "...",
    "target_audience": "...",
    "content_mode": "product" | "general",
    "platform": "tiktok",
    "video_count": 1,
    "video_duration_min_seconds": 10,
    "video_duration_max_seconds": 10,
    "video_model": "omni_flash" | "seedance_2_0_mini",
    "video_resolution": "480p" | "720p",
    "video_aspect_ratio": "9:16" | "16:9" | "1:1",
    "video_language": "en-US" | "zh-CN",
    "visual_style": "...",
    "pacing": "...",
    "audio_direction": "...",
    "conversion_direction": null | "...",
    "creative_constraints": ["..."],
    "visual_reference_generation_mode": "individual" | "board",
    "confirmed_offer": null | "$14.99 shipped",
    "promotion_evidence_quote": null | "exact user quote",
    "confidence": 0.0
  }
}
""".strip()


def _script_source_row(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    message_id: int,
) -> HermesAgentMessage | None:
    return (
        db.query(HermesAgentMessage)
        .filter(
            HermesAgentMessage.id == int(message_id),
            HermesAgentMessage.conversation_id == int(conversation.id),
            HermesAgentMessage.workspace_id == int(conversation.workspace_id),
            HermesAgentMessage.user_id == int(conversation.user_id),
            HermesAgentMessage.role == "user",
        )
        .one_or_none()
    )


def _authoritative_script_record(
    db: Session,
    *,
    conversation: HermesAgentConversation,
) -> dict[str, Any] | None:
    meta = dict(conversation.meta_json or {})
    message_id = meta.get("authoritative_script_message_id")
    if message_id is None:
        return None
    versions = [
        item
        for item in list(meta.get("authoritative_script_versions") or [])
        if isinstance(item, dict) and item.get("version")
    ]
    current_version = int(meta.get("authoritative_script_current_version") or 1)
    selected_version = next(
        (
            item
            for item in reversed(versions)
            if int(item.get("version") or 0) == current_version
        ),
        None,
    )
    version_text = str((selected_version or {}).get("text") or "").strip()
    if version_text:
        return {
            "source_message_id": int(message_id),
            "version": current_version,
            "sha256": hashlib.sha256(version_text.encode("utf-8")).hexdigest(),
            "text": version_text,
            "revision_evidence": (selected_version or {}).get("revision_evidence"),
        }
    row = _script_source_row(
        db,
        conversation=conversation,
        message_id=int(message_id),
    )
    source_text = str(row.content_text or "").strip() if row is not None else ""
    if not source_text:
        return None
    return {
        "source_message_id": int(message_id),
        "version": 1,
        "sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "text": source_text,
        "revision_evidence": None,
    }


def _minimum_script_seconds(script: str) -> int:
    value = str(script or "").strip()
    if not value:
        return 0
    english_words = re.findall(
        r"[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*",
        value,
    )
    chinese_characters = re.findall(r"[\u3400-\u9fff]", value)
    seconds = (
        len(english_words) * 60 / MAX_FAST_ENGLISH_WORDS_PER_MINUTE
        + len(chinese_characters) * 60 / MAX_FAST_CHINESE_CHARACTERS_PER_MINUTE
    )
    return max(1, int(math.ceil(seconds)))


def _decision_script_text(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    decision: ContentProducerDecision,
) -> str:
    revised = str(decision.revised_authoritative_script or "").strip()
    if revised:
        return revised
    message_id = decision.authoritative_script_message_id
    if message_id is not None:
        row = _script_source_row(
            db,
            conversation=conversation,
            message_id=int(message_id),
        )
        if row is not None:
            return str(row.content_text or "").strip()
    current = _authoritative_script_record(db, conversation=conversation)
    return str((current or {}).get("text") or "").strip()


def _validate_script_decision(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    decision: ContentProducerDecision,
    latest_user_message: str,
) -> None:
    revised = str(decision.revised_authoritative_script or "").strip()
    evidence = str(decision.script_revision_evidence or "").strip()
    if bool(revised) != bool(evidence):
        raise ValueError(
            "revised_authoritative_script and script_revision_evidence must be supplied together"
        )
    if evidence and evidence not in latest_user_message:
        raise ValueError(
            "script_revision_evidence must be an exact quote from latest_user_message"
        )
    if revised and _authoritative_script_record(db, conversation=conversation) is None:
        raise ValueError("a locked authoritative script is required before revising it")
    script = _decision_script_text(
        db,
        conversation=conversation,
        decision=decision,
    )
    if decision.proposal is None or not script:
        return
    minimum_seconds = _minimum_script_seconds(script)
    if decision.proposal.video_duration_max_seconds < minimum_seconds:
        raise ValueError(
            "the complete locked script requires at least "
            f"{minimum_seconds} seconds even at the configured fast intelligible "
            "speech ceiling; increase the duration or return needs_input to ask "
            "for permission to shorten the script"
        )


def _packet(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    selected_product: HermesContentProduct | None,
) -> dict[str, Any]:
    history = _history(db, conversation=conversation)
    eligible = [row for row in history if row.role in {"user", "assistant"}]
    # Failed/retried turns can leave two adjacent copies from older clients.
    # Retain the newest durable copy for the model while preserving every row
    # in the audit transcript shown by the session API.
    deduplicated = _deduplicate_adjacent_messages(eligible)

    # Bound context by omitting whole old turns, never by silently slicing an
    # individual script or message.  The latest turn is always retained.
    selected_rows: list[HermesAgentMessage] = []
    used_chars = 0
    omitted_ids: list[int] = []
    for row in reversed(deduplicated):
        content = str(row.content_text or "")
        if selected_rows and used_chars + len(content) > MAX_MODEL_CONVERSATION_CHARS:
            omitted_ids.append(int(row.id))
            continue
        selected_rows.append(row)
        used_chars += len(content)
    selected_rows.reverse()
    conversation_rows = [
        {
            "message_id": int(row.id),
            "role": row.role,
            "content": str(row.content_text or ""),
        }
        for row in selected_rows
    ]

    meta = dict(conversation.meta_json or {})
    current_proposal = (
        dict(meta["proposal"])
        if isinstance(meta.get("proposal"), dict)
        else None
    )
    source_manifest = [
        item
        for item in list(meta.get("source_text_assets") or [])
        if isinstance(item, dict) and item.get("message_id")
    ][-MAX_SOURCE_TEXT_ASSETS:]
    source_ids = [int(item["message_id"]) for item in source_manifest]
    source_rows_by_id: dict[int, HermesAgentMessage] = {}
    if source_ids:
        source_rows_by_id = {
            int(row.id): row
            for row in db.query(HermesAgentMessage)
            .filter(
                HermesAgentMessage.conversation_id == int(conversation.id),
                HermesAgentMessage.id.in_(source_ids),
                HermesAgentMessage.role == "user",
            )
            .all()
        }
    authoritative_sources_reversed: list[dict[str, Any]] = []
    source_chars = 0
    omitted_source_ids: list[int] = []
    locked_script_id = (
        int(meta["authoritative_script_message_id"])
        if meta.get("authoritative_script_message_id") is not None
        else None
    )
    for item in reversed(source_manifest):
        message_id = int(item["message_id"])
        row = source_rows_by_id.get(message_id)
        if row is None:
            continue
        content = str(row.content_text or "")
        if (
            authoritative_sources_reversed
            and source_chars + len(content) > MAX_MODEL_SOURCE_TEXT_CHARS
            and message_id != locked_script_id
        ):
            omitted_source_ids.append(message_id)
            continue
        authoritative_sources_reversed.append({
            "message_id": message_id,
            "sha256": str(item.get("sha256") or ""),
            "character_count": int(item.get("character_count") or 0),
            "content": content,
        })
        source_chars += len(content)
    authoritative_sources = list(reversed(authoritative_sources_reversed))
    # The full text now lives once in authoritative_source_texts; retain a
    # visible pointer in ordinary conversation instead of paying twice for the
    # same script tokens.
    included_source_ids = {
        int(item["message_id"]) for item in authoritative_sources
    }
    for item in conversation_rows:
        if int(item["message_id"]) in included_source_ids:
            item["content"] = (
                f"[Full immutable source text is in authoritative_source_texts "
                f"message_id={item['message_id']}]"
            )
    catalog = _catalog(db, workspace_id=int(conversation.workspace_id))
    product_context = None
    if selected_product is not None:
        product_context = {
            "id": int(selected_product.id),
            "brand_name": selected_product.brand_name,
            "product_name": selected_product.product_name,
            "market": selected_product.market,
            "attribute_notes": selected_product.product_brief,
            "stable_facts": _compact(selected_product.facts_json or {}),
            "authority_policy": {
                "stable_product_facts_are_authoritative": True,
                "attribute_notes_are_not_commercial_authority": True,
                "project_price_offer_shipping_require_latest_user_authorization": True,
                "project_duration_style_pacing_require_conversation_context": True,
            },
        }
    attachments = _attachment_rows(db, conversation=conversation)
    current_script = _authoritative_script_record(
        db,
        conversation=conversation,
    )
    return {
        "operation": "content_factory_project_intake",
        "prompt_version": PRODUCER_PROMPT_VERSION,
        "latest_user_message": (
            str(next((row.content_text for row in reversed(deduplicated) if row.role == "user"), "") or "")
        ),
        "current_working_proposal": current_proposal,
        "current_authoritative_script_message_id": (
            int(meta["authoritative_script_message_id"])
            if meta.get("authoritative_script_message_id") is not None
            else None
        ),
        "current_authoritative_script": current_script,
        "authoritative_source_texts": authoritative_sources,
        "conversation": conversation_rows,
        "context_manifest": {
            "messages_included": len(conversation_rows),
            "whole_old_message_ids_omitted": sorted(omitted_ids),
            "whole_source_text_ids_omitted": sorted(omitted_source_ids),
            "individual_messages_truncated": False,
        },
        "selected_product": product_context,
        "available_products": catalog,
        "user_attachments": [
            {
                "attachment_key": row.attachment_key,
                "kind": row.kind,
                "original_name": row.original_name,
                "mime_type": row.mime_type,
                "size_bytes": int(row.size_bytes or 0),
                "analysis_status": row.analysis_status,
                "technical_summary": _compact(row.analysis_json or {}),
                "character_name": dict(row.meta_json or {}).get("character_name"),
                "character_description": dict(row.meta_json or {}).get("character_description"),
                "visual_preview_attached": bool(row.preview_path),
            }
            for row in attachments
        ],
        "execution_boundaries": {
            "media_authorized": False,
            "browser_authorized": False,
            "project_creation_requires_explicit_confirmation": True,
        },
    }


def _proposal_digest(proposal: ContentProducerProposal) -> str:
    payload = json.dumps(
        proposal.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _all_user_rows(
    db: Session,
    conversation: HermesAgentConversation,
) -> list[HermesAgentMessage]:
    return list(
        db.query(HermesAgentMessage)
        .filter(
            HermesAgentMessage.conversation_id == int(conversation.id),
            HermesAgentMessage.workspace_id == int(conversation.workspace_id),
            HermesAgentMessage.user_id == int(conversation.user_id),
            HermesAgentMessage.role == "user",
        )
        .order_by(HermesAgentMessage.id.asc())
        .all()
    )


def _user_text(db: Session, conversation: HermesAgentConversation) -> str:
    values: list[str] = []
    for row in _all_user_rows(db, conversation):
        content = str(row.content_text or "").strip()
        if content and (not values or values[-1] != content):
            values.append(content)
    return "\n".join(values)


def _looks_like_source_text(message: str) -> bool:
    value = str(message or "").strip()
    if len(value) >= 600:
        return True
    line_count = len([line for line in value.splitlines() if line.strip()])
    return len(value) >= 280 and line_count >= 5


def _remember_source_text(
    conversation: HermesAgentConversation,
    message: HermesAgentMessage,
) -> None:
    content = str(message.content_text or "").strip()
    if not _looks_like_source_text(content):
        return
    meta = dict(conversation.meta_json or {})
    assets = [
        item
        for item in list(meta.get("source_text_assets") or [])
        if isinstance(item, dict) and item.get("message_id")
    ]
    if not any(int(item["message_id"]) == int(message.id) for item in assets):
        assets.append({
            "message_id": int(message.id),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "character_count": len(content),
            "immutable": True,
        })
    meta["source_text_assets"] = assets[-MAX_SOURCE_TEXT_ASSETS:]
    conversation.meta_json = meta


def _refresh_source_text_index(
    db: Session,
    conversation: HermesAgentConversation,
) -> None:
    """Lazily index full source texts from conversations created before v2."""
    for row in _all_user_rows(db, conversation):
        _remember_source_text(conversation, row)


def _reconcile_proposal_delta(
    decision: ContentProducerDecision,
    *,
    prior_proposal: ContentProducerProposal | None,
    latest_user_message: str,
) -> ContentProducerDecision:
    if prior_proposal is None or decision.proposal is None:
        return decision
    incoming = decision.proposal.model_dump(mode="python")
    prior = prior_proposal.model_dump(mode="python")
    declared = set(decision.changed_fields)
    evidence = dict(decision.change_evidence or {})
    accepted: set[str] = set()
    for field in declared:
        quote = str(evidence.get(field) or "").strip()
        if quote and quote in latest_user_message:
            accepted.add(field)
    merged = dict(prior)
    for field in accepted:
        merged[field] = incoming[field]
    rejected_changes = {
        field
        for field in incoming
        if incoming[field] != prior[field] and field not in accepted
    }
    message = decision.assistant_message
    if rejected_changes:
        message = (
            f"{message.rstrip()} 未经你本轮明确修改的既有设置已保持不变。"
        )[:4000]
    return decision.model_copy(
        update={
            "assistant_message": message,
            "proposal": ContentProducerProposal.model_validate(merged),
            "changed_fields": sorted(accepted),
            "change_evidence": {
                field: evidence[field]
                for field in sorted(accepted)
            },
        }
    )


_CANCELLED_OFFER_PATTERN = re.compile(
    r"(?:取消|不使用|不要|移除|\bcancel(?:led)?\b|\bremove(?:d)?\b|\bdo\s+not\b)",
    re.I,
)
_SHIPPING_OFFER_PATTERN = re.compile(
    r"(?:包邮|\bshipped\b|\bfree\s+shipping\b)",
    re.I,
)
_COMMERCIAL_NUMBER_PATTERN = re.compile(
    r"(?<![\d.])(?:[$€£¥]\s*)?(\d+(?:\.\d{1,2})?)(?![\d.])"
)


def _validate_promotion_authorization(
    proposal: ContentProducerProposal,
    *,
    latest_user_message: str,
    prior_proposal: ContentProducerProposal | None,
) -> None:
    quote = str(proposal.promotion_evidence_quote or "").strip()
    offer = str(proposal.confirmed_offer or "").strip()
    prior_quote = str(
        (prior_proposal.promotion_evidence_quote if prior_proposal else None) or ""
    ).strip()
    prior_offer = str(
        (prior_proposal.confirmed_offer if prior_proposal else None) or ""
    ).strip()
    if quote and quote != prior_quote and quote not in latest_user_message and offer:
        raise ValueError(
            "promotion_evidence_quote must be an exact quote from latest_user_message"
        )
    if offer == prior_offer and quote == prior_quote:
        return
    if offer and not quote:
        raise ValueError("confirmed_offer requires promotion_evidence_quote")
    if offer and _CANCELLED_OFFER_PATTERN.search(offer):
        raise ValueError("confirmed_offer must exclude canceled or negative offers")
    if offer:
        offer_numbers = set(_COMMERCIAL_NUMBER_PATTERN.findall(offer))
        evidence_numbers = set(_COMMERCIAL_NUMBER_PATTERN.findall(quote))
        if not offer_numbers.issubset(evidence_numbers):
            raise ValueError(
                "every numeric price in confirmed_offer must appear in promotion_evidence_quote"
            )
        if _SHIPPING_OFFER_PATTERN.search(offer) and not _SHIPPING_OFFER_PATTERN.search(quote):
            raise ValueError(
                "shipping language in confirmed_offer requires shipping evidence in the user quote"
            )


def _strip_unverified_promotion_evidence(
    proposal: ContentProducerProposal,
    *,
    latest_user_message: str,
    prior_proposal: ContentProducerProposal | None,
) -> ContentProducerProposal:
    quote = str(proposal.promotion_evidence_quote or "").strip()
    prior_quote = str(
        (prior_proposal.promotion_evidence_quote if prior_proposal else None) or ""
    ).strip()
    if not quote or quote == prior_quote or quote in latest_user_message:
        return proposal
    data = proposal.model_dump(mode="python")
    data["promotion_evidence_quote"] = None
    data["confirmed_offer"] = None
    return ContentProducerProposal.model_validate(data)


async def run_producer_turn(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    message: str,
    session_key: str | None,
    product_id: int | None,
    product_selection_explicit: bool = False,
    client_turn_id: str | None = None,
) -> tuple[HermesAgentConversation, ContentProducerDecision]:
    normalized_message = str(message or "").strip()
    if not normalized_message:
        raise APIError(
            "CONTENT_PRODUCER_MESSAGE_REQUIRED",
            "Tell the AI producer what you want to make.",
            400,
        )
    if len(normalized_message) > 50000:
        raise APIError(
            "CONTENT_PRODUCER_MESSAGE_TOO_LARGE",
            "The message is too long. Keep one turn under 50,000 characters.",
            413,
        )
    normalized_session = re.sub(
        r"[^a-zA-Z0-9_.-]+", "-", str(session_key or "").strip()
    ).strip("-._")[:48]
    if not normalized_session:
        normalized_session = f"intake-{uuid4().hex[:24]}"

    conversation = get_or_create_producer_conversation(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        session_key=normalized_session,
    )
    # Serialize only the short local turn-registration transaction.  The lock
    # is released by the commit below before the slow upstream model call, so
    # two retries cannot create duplicate user rows while ordinary reads stay
    # available during reasoning.
    conversation = (
        db.query(HermesAgentConversation)
        .filter(
            HermesAgentConversation.id == int(conversation.id),
            HermesAgentConversation.workspace_id == int(workspace_id),
            HermesAgentConversation.user_id == int(user_id),
        )
        .with_for_update()
        .one()
    )
    meta = dict(conversation.meta_json or {})
    if meta.get("created_project_id"):
        raise APIError(
            "CONTENT_PRODUCER_SESSION_ALREADY_CREATED",
            "This conversation already created a project. Start a new conversation for another project.",
            409,
        )
    if isinstance(meta.get("proposal"), dict) and str(
        meta.get("proposal_prompt_version") or ""
    ) != PRODUCER_PROMPT_VERSION:
        # Prompt contracts are state schemas.  Carrying a proposal across a
        # schema revision can preserve obsolete commercial or timing choices
        # even though the new model behaves correctly.
        meta.pop("proposal", None)
        meta.pop("proposal_sha256", None)
        meta.pop("proposal_prompt_version", None)
        meta["status"] = "needs_input"
    if product_selection_explicit and product_id is None:
        # A deliberate "no product" selection must clear an earlier choice.
        # Otherwise an old product could silently return at confirmation time.
        meta.pop("selected_product_id", None)
        meta.pop("proposal", None)
        meta.pop("proposal_sha256", None)
        meta.pop("proposal_prompt_version", None)
    effective_product_id = product_id
    if not product_selection_explicit and effective_product_id is None:
        prior_product_id = meta.get("selected_product_id")
        effective_product_id = (
            int(prior_product_id) if prior_product_id is not None else None
        )
    selected = _selected_product(
        db,
        workspace_id=workspace_id,
        product_id=effective_product_id,
    )
    if selected is not None:
        previous_product_id = meta.get("selected_product_id")
        if previous_product_id and int(previous_product_id) != int(selected.id):
            meta.pop("proposal", None)
            meta.pop("proposal_sha256", None)
            meta.pop("proposal_prompt_version", None)
        meta["selected_product_id"] = int(selected.id)
    prior_proposal: ContentProducerProposal | None = None
    if isinstance(meta.get("proposal"), dict):
        try:
            prior_proposal = ContentProducerProposal.model_validate(meta["proposal"])
        except ValidationError:
            meta.pop("proposal", None)
            meta.pop("proposal_sha256", None)
            meta.pop("proposal_prompt_version", None)
    meta["session_key"] = normalized_session
    conversation.meta_json = meta
    normalized_turn_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(client_turn_id or ""))[:32]
    if not normalized_turn_id:
        normalized_turn_id = uuid4().hex
    processing_attachments = [
        row.original_name
        for row in _attachment_rows(db, conversation=conversation)
        if row.analysis_status in {"queued", "processing"}
    ]
    if processing_attachments:
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENTS_PROCESSING",
            "Wait for the uploaded reference video analysis before continuing the conversation.",
            409,
            {"attachments": processing_attachments[:5]},
        )
    user_message = (
        db.query(HermesAgentMessage)
        .filter(
            HermesAgentMessage.conversation_id == int(conversation.id),
            HermesAgentMessage.workspace_id == int(workspace_id),
            HermesAgentMessage.user_id == int(user_id),
            HermesAgentMessage.role == "user",
            HermesAgentMessage.run_id == normalized_turn_id,
        )
        .one_or_none()
    )
    if user_message is not None:
        if str(user_message.content_text or "") != normalized_message:
            raise APIError(
                "CONTENT_PRODUCER_TURN_ID_REUSED",
                "This producer turn identifier belongs to different text.",
                409,
            )
        existing_assistant = (
            db.query(HermesAgentMessage)
            .filter(
                HermesAgentMessage.conversation_id == int(conversation.id),
                HermesAgentMessage.role == "assistant",
                HermesAgentMessage.run_id == normalized_turn_id,
            )
            .order_by(HermesAgentMessage.id.desc())
            .first()
        )
        if existing_assistant is not None and isinstance(existing_assistant.content_json, dict):
            return conversation, ContentProducerDecision.model_validate(existing_assistant.content_json)
    else:
        user_message = repository.add_message(
            db,
            conversation=conversation,
            workspace_id=workspace_id,
            user_id=user_id,
            role="user",
            content_text=normalized_message,
            content_json={"selected_product_id": int(selected.id) if selected else None},
            run_id=normalized_turn_id,
        )
    _refresh_source_text_index(db, conversation)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    packet = _packet(db, conversation=conversation, selected_product=selected)
    packet_json = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    attachments = _attachment_rows(db, conversation=conversation)
    client = HermesContentProducerClient()
    scope_digest = hashlib.sha256(
        f"{workspace_id}:{user_id}:{conversation.id}:{normalized_session}".encode("utf-8")
    ).hexdigest()
    hermes_conversation = f"gmv-cf-producer-{scope_digest[:40]}"
    hermes_session_key = f"gmv-cf-producer-{scope_digest}"
    idempotency_base = (
        f"content-producer:{workspace_id}:{user_id}:{conversation.id}:"
        f"{user_message.id}:{PRODUCER_PROMPT_VERSION}"
    )
    validation_error: str | None = None
    raw_text = ""
    decision: ContentProducerDecision | None = None
    for attempt in range(2):
        input_text = packet_json
        if attempt and validation_error:
            input_text = json.dumps(
                {
                    "original_packet": packet,
                    "invalid_response": raw_text[:12000],
                    "validation_error": validation_error[:4000],
                    "repair_instruction": (
                        "Return a corrected JSON object only. Preserve the "
                        "user meaning and do not add facts."
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        response, latency_ms = await client.create_response(
            input_text=input_text,
            input_items=_producer_input_items(input_text, attachments),
            instructions=_instructions(),
            metadata={
                "prompt_version": PRODUCER_PROMPT_VERSION,
                "workspace_id": str(workspace_id),
                "user_id": str(user_id),
            },
            conversation=hermes_conversation,
            session_key=hermes_session_key,
            store=True,
            idempotency_key=f"{idempotency_base}:attempt:{attempt + 1}",
        )
        raw_text = extract_output_text(response)
        try:
            decision = ContentProducerDecision.model_validate(_json_object(raw_text))
            if decision.proposal is not None:
                _validate_promotion_authorization(
                    decision.proposal,
                    latest_user_message=normalized_message,
                    prior_proposal=prior_proposal,
                )
            _validate_script_decision(
                db,
                conversation=conversation,
                decision=decision,
                latest_user_message=normalized_message,
            )
            break
        except (ValueError, ValidationError) as exc:
            validation_error = str(exc)
    if decision is None:
        raise APIError(
            "CONTENT_PRODUCER_RESPONSE_INVALID",
            "The AI producer could not form a safe project proposal. Please retry this message.",
            502,
            {"validation_error": str(validation_error or "unknown")[:1000]},
        )

    if decision.proposal is not None:
        decision = _reconcile_proposal_delta(
            decision,
            prior_proposal=prior_proposal,
            latest_user_message=normalized_message,
        )
        decision = decision.model_copy(
            update={
                "proposal": _strip_unverified_promotion_evidence(
                    decision.proposal,
                    latest_user_message=normalized_message,
                    prior_proposal=prior_proposal,
                )
            }
        )
        _validate_promotion_authorization(
            decision.proposal,
            latest_user_message=normalized_message,
            prior_proposal=prior_proposal,
        )
        _validate_script_decision(
            db,
            conversation=conversation,
            decision=decision,
            latest_user_message=normalized_message,
        )
    meta = dict(conversation.meta_json or {})
    prior_script_id = (
        int(meta["authoritative_script_message_id"])
        if meta.get("authoritative_script_message_id") is not None
        else None
    )
    proposed_script_id = decision.authoritative_script_message_id
    if proposed_script_id is not None:
        valid_source_ids = {
            int(item["message_id"])
            for item in list(meta.get("source_text_assets") or [])
            if isinstance(item, dict) and item.get("message_id")
        }
        if int(proposed_script_id) not in valid_source_ids:
            raise APIError(
                "CONTENT_PRODUCER_SCRIPT_SOURCE_INVALID",
                "The AI producer selected a script that was not supplied by the user.",
                502,
            )
        meta["authoritative_script_message_id"] = int(proposed_script_id)
    elif prior_script_id is not None:
        decision = decision.model_copy(
            update={"authoritative_script_message_id": prior_script_id}
        )
    active_script_id = (
        int(meta["authoritative_script_message_id"])
        if meta.get("authoritative_script_message_id") is not None
        else None
    )
    revised_script = str(decision.revised_authoritative_script or "").strip()
    if revised_script:
        if active_script_id is None:
            raise APIError(
                "CONTENT_PRODUCER_SCRIPT_REVISION_WITHOUT_SOURCE",
                "A locked source script is required before applying a revision.",
                409,
            )
        source_row = _script_source_row(
            db,
            conversation=conversation,
            message_id=active_script_id,
        )
        source_text = str(source_row.content_text or "").strip() if source_row else ""
        if not source_text:
            raise APIError(
                "CONTENT_PRODUCER_SCRIPT_SOURCE_MISSING",
                "The locked source script is missing.",
                409,
            )
        versions = [
            dict(item)
            for item in list(meta.get("authoritative_script_versions") or [])
            if isinstance(item, dict) and item.get("version")
        ]
        if not versions:
            versions.append({
                "version": 1,
                "source_message_id": active_script_id,
                "sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "revised": False,
            })
        next_version = max(int(item.get("version") or 0) for item in versions) + 1
        versions.append({
            "version": next_version,
            "source_message_id": active_script_id,
            "sha256": hashlib.sha256(revised_script.encode("utf-8")).hexdigest(),
            "text": revised_script,
            "revised": True,
            "revision_evidence": str(decision.script_revision_evidence or "").strip(),
            "revised_by_message_id": int(user_message.id),
            "prompt_version": PRODUCER_PROMPT_VERSION,
        })
        meta["authoritative_script_versions"] = versions[
            -MAX_AUTHORITATIVE_SCRIPT_VERSIONS:
        ]
        meta["authoritative_script_current_version"] = next_version
    meta.update(
        {
            "session_key": normalized_session,
            "producer_prompt_version": PRODUCER_PROMPT_VERSION,
            "proposal_prompt_version": PRODUCER_PROMPT_VERSION,
            "status": decision.status,
            "selected_product_id": int(selected.id) if selected else meta.get("selected_product_id"),
            "proposal": (
                decision.proposal.model_dump(mode="json")
                if decision.proposal is not None
                else None
            ),
            "proposal_sha256": (
                _proposal_digest(decision.proposal)
                if decision.proposal is not None
                else None
            ),
            "last_latency_ms": int(latency_ms),
        }
    )
    conversation.meta_json = meta
    existing_assistant = (
        db.query(HermesAgentMessage)
        .filter(
            HermesAgentMessage.conversation_id == int(conversation.id),
            HermesAgentMessage.role == "assistant",
            HermesAgentMessage.run_id == normalized_turn_id,
        )
        .order_by(HermesAgentMessage.id.desc())
        .first()
    )
    if existing_assistant is not None and isinstance(existing_assistant.content_json, dict):
        return conversation, ContentProducerDecision.model_validate(existing_assistant.content_json)
    repository.add_message(
        db,
        conversation=conversation,
        workspace_id=workspace_id,
        user_id=user_id,
        role="assistant",
        content_text=decision.assistant_message,
        content_json=decision.model_dump(mode="json"),
        run_id=normalized_turn_id,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation, decision


def producer_session(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    session_key: str,
) -> tuple[HermesAgentConversation, list[HermesAgentMessage]]:
    conversation = _conversation_for_user(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        session_key=session_key,
    )
    return conversation, _deduplicate_adjacent_messages(
        _history(db, conversation=conversation)
    )


def authoritative_producer_script(
    db: Session,
    *,
    conversation: HermesAgentConversation,
) -> tuple[int, str] | None:
    record = _authoritative_script_record(
        db,
        conversation=conversation,
    )
    if record is None:
        return None
    message_id = int(record["source_message_id"])
    text_value = str(record.get("text") or "").strip()
    if not text_value:
        raise APIError(
            "CONTENT_PRODUCER_SCRIPT_SOURCE_MISSING",
            "The confirmed source script is missing. Ask the producer to select it again.",
            409,
        )
    return message_id, text_value


def confirmed_project_parameters(
    db: Session,
    *,
    workspace_id: int,
    user_id: int,
    session_key: str,
) -> tuple[HermesAgentConversation, ContentProducerProposal, HermesContentProduct | None, str]:
    key = repository.conversation_key(
        workspace_id=workspace_id,
        user_id=user_id,
        task_type=PRODUCER_TASK_TYPE,
        key=session_key,
    )
    conversation = (
        db.query(HermesAgentConversation)
        .filter(
            HermesAgentConversation.conversation_key == key,
            HermesAgentConversation.workspace_id == int(workspace_id),
            HermesAgentConversation.user_id == int(user_id),
            HermesAgentConversation.task_type == PRODUCER_TASK_TYPE,
        )
        .with_for_update()
        .one_or_none()
    )
    if conversation is None:
        raise APIError(
            "CONTENT_PRODUCER_SESSION_NOT_FOUND",
            "The AI producer conversation was not found.",
            404,
        )
    meta = dict(conversation.meta_json or {})
    if meta.get("status") not in {"proposal_ready", "created"} or not isinstance(meta.get("proposal"), dict):
        raise APIError(
            "CONTENT_PRODUCER_PROPOSAL_NOT_READY",
            "Finish the discussion with the AI producer before creating the project.",
            409,
        )
    try:
        proposal = ContentProducerProposal.model_validate(meta["proposal"])
    except ValidationError as exc:
        raise APIError(
            "CONTENT_PRODUCER_PROPOSAL_INVALID",
            "The saved project proposal is invalid. Ask the AI producer to refresh it.",
            409,
        ) from exc
    if _proposal_digest(proposal) != str(meta.get("proposal_sha256") or ""):
        raise APIError(
            "CONTENT_PRODUCER_PROPOSAL_CHANGED",
            "The saved project proposal changed and must be reviewed again.",
            409,
        )
    product = _selected_product(
        db,
        workspace_id=workspace_id,
        product_id=(
            int(meta["selected_product_id"])
            if meta.get("selected_product_id") is not None
            else None
        ),
    )
    if proposal.content_mode == "product" and product is None:
        raise APIError(
            "CONTENT_PRODUCT_REQUIRED",
            "Choose the company-library product before creating this project.",
            409,
        )
    return conversation, proposal, product, _user_text(db, conversation)


__all__ = [
    "ContentProducerDecision",
    "ContentProducerProposal",
    "PRODUCER_TASK_TYPE",
    "copy_producer_attachments_to_project",
    "confirmed_project_parameters",
    "delete_producer_attachment",
    "get_or_create_producer_conversation",
    "producer_attachment_out",
    "producer_attachments",
    "producer_session",
    "run_producer_turn",
    "save_producer_attachment",
]
