from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
from typing import Any, Callable, Literal
from urllib.parse import urlparse
from uuid import uuid4
import xml.etree.ElementTree as ET
import zipfile

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
from app.services.ai_video.accounts import video_model_routing_catalog
from app.features.tenants.openai_whisper.url_security import (
    UnsafeShareURLError,
    validate_share_url,
)


PRODUCER_TASK_TYPE = "content_producer"
PRODUCER_PROMPT_VERSION = "content_producer_v19_fast_product_grounding"

from app.services.hermes_agent.content_intent import (
    CreativeIntentManifest,
    sign_creative_intent_manifest,
)
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
MAX_PRODUCER_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_PRODUCER_DOCUMENT_TEXT_CHARS = 100000
MAX_PRODUCER_DOCUMENT_XML_BYTES = 20 * 1024 * 1024
MAX_PRODUCER_DOCUMENT_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_PRODUCER_REFERENCE_VIDEOS = 1
MAX_PRODUCER_CHARACTER_IMAGES = 16
MAX_PRODUCER_DOCUMENTS = 8
MAX_FAST_ENGLISH_WORDS_PER_MINUTE = 220
MAX_FAST_CHINESE_CHARACTERS_PER_MINUTE = 320
MAX_AUTHORITATIVE_SCRIPT_VERSIONS = 50
PRODUCER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PRODUCER_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
PRODUCER_WORD_EXTENSIONS = {".docx", ".docm", ".dotx", ".dotm"}
PRODUCER_SPREADSHEET_EXTENSIONS = {
    ".xlsx", ".xlsm", ".xltx", ".xltm",
}
PRODUCER_PRESENTATION_EXTENSIONS = {
    ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm",
}
PRODUCER_OPEN_DOCUMENT_EXTENSIONS = {".odt", ".ods", ".odp"}
PRODUCER_PLAIN_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".tex", ".log",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".css", ".go",
    ".java", ".js", ".jsx", ".php", ".py", ".rb", ".sh", ".sql",
    ".ts", ".tsx",
}
PRODUCER_DOCUMENT_EXTENSIONS = {
    ".pdf",
    *PRODUCER_WORD_EXTENSIONS,
    *PRODUCER_SPREADSHEET_EXTENSIONS,
    *PRODUCER_PRESENTATION_EXTENSIONS,
    *PRODUCER_OPEN_DOCUMENT_EXTENSIONS,
    *PRODUCER_PLAIN_TEXT_EXTENSIONS,
}


class ContentProducerProposal(BaseModel):
    """User-facing choices only; product truth is never authored here."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    content_objective: str = Field(min_length=1, max_length=255)
    target_audience: str = Field(min_length=1, max_length=1000)
    content_mode: Literal["product", "general"]
    product_use_mode: Literal["required", "context_only", "none"] = "required"
    platform: str = Field(default="tiktok", min_length=1, max_length=64)
    video_count: int = Field(ge=1, le=50)
    video_duration_min_seconds: int = Field(ge=1, le=120)
    video_duration_max_seconds: int = Field(ge=1, le=120)
    video_model: Literal["omni_flash", "seedance_2_0_mini"]
    video_duration_strategy: Literal[
        "creative_flexibility", "cross_provider_portable"
    ] = "creative_flexibility"
    preferred_segment_durations_seconds: list[int] = Field(
        default_factory=list,
        max_length=30,
    )
    video_resolution: Literal["480p", "720p"] = "720p"
    video_aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    video_language: Literal["en-US", "zh-CN"] = "en-US"
    visual_style: str = Field(min_length=1, max_length=1000)
    pacing: str = Field(min_length=1, max_length=500)
    spoken_density: Literal["sparse", "balanced", "dense"] = "balanced"
    spoken_density_reason: str = Field(default="", max_length=500)
    audio_direction: str = Field(min_length=1, max_length=1000)
    conversion_direction: str | None = Field(default=None, max_length=1000)
    creative_constraints: list[str] = Field(default_factory=list, max_length=32)
    video_generation_mode: Literal[
        "text_to_video", "image_to_video", "video_to_video"
    ] = "image_to_video"
    visual_reference_generation_mode: Literal["individual", "board"] = "board"
    confirmed_offer: str | None = Field(default=None, max_length=500)
    promotion_evidence_quote: str | None = Field(default=None, max_length=1000)
    confidence: float = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _default_product_use_mode(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("product_use_mode"):
            return value
        data = dict(value)
        data["product_use_mode"] = (
            "required"
            if str(data.get("content_mode") or "").strip() == "product"
            else "none"
        )
        return data

    @model_validator(mode="after")
    def _duration_order(self) -> "ContentProducerProposal":
        if self.content_mode == "general" and self.product_use_mode != "none":
            raise ValueError("general content requires product_use_mode=none")
        if self.content_mode == "product" and self.product_use_mode == "none":
            raise ValueError(
                "product content requires product_use_mode=required or context_only"
            )
        if self.video_duration_min_seconds > self.video_duration_max_seconds:
            raise ValueError("minimum video duration cannot exceed maximum")
        if any(
            int(value) < 1 or int(value) > 120
            for value in self.preferred_segment_durations_seconds
        ):
            raise ValueError("preferred segment durations must be 1-120 seconds")
        if self.preferred_segment_durations_seconds:
            preferred_total = sum(self.preferred_segment_durations_seconds)
            if not (
                self.video_duration_min_seconds
                <= preferred_total
                <= self.video_duration_max_seconds
            ):
                raise ValueError(
                    "preferred segment durations must total inside the "
                    "proposed video duration range"
                )
        # Provider timing is live route data. The front-desk Producer records
        # the user's acceptable range; project confirmation later resolves one
        # legal total against enabled provider/model capabilities.
        return self


class ContentProducerDeliverableSpec(BaseModel):
    """One user-facing output compiled from the conversation.

    This is intentionally media-agnostic.  It records what must be delivered,
    which parts are locked, and how this output relates to its siblings; the
    downstream Director still owns the creative and production execution that
    the user left open.
    """

    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(ge=1, le=50)
    label: str = Field(min_length=1, max_length=255)
    objective: str = Field(min_length=1, max_length=1000)
    relationship: Literal[
        "standalone",
        "independent",
        "series_episode",
        "visual_variant",
    ] = "independent"
    script_text: str | None = Field(default=None, max_length=50000)
    source_message_id: int | None = Field(default=None, ge=1)
    target_duration_seconds: int | None = Field(default=None, ge=1, le=120)
    must_preserve: list[str] = Field(default_factory=list, max_length=32)
    differentiation: list[str] = Field(default_factory=list, max_length=32)


class ContentProducerIntentSpec(BaseModel):
    """Effective brief plus its authoritative, evidence-backed intent graph."""

    model_config = ConfigDict(extra="forbid")

    delivery_mode: Literal[
        "single",
        "independent_videos",
        "series_episodes",
        "visual_variants",
    ]
    source_material_mode: Literal[
        "requirements",
        "single_script",
        "multi_script_package",
        "reference_copy",
        "none",
    ] = "requirements"
    user_goal: str = Field(min_length=1, max_length=2000)
    intent_manifest: CreativeIntentManifest
    deliverables: list[ContentProducerDeliverableSpec] = Field(
        default_factory=list,
        max_length=50,
    )

    @model_validator(mode="after")
    def _deliverable_coordinates(self) -> "ContentProducerIntentSpec":
        ordinals = [item.ordinal for item in self.deliverables]
        if ordinals and ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError(
                "intent deliverable ordinals must be contiguous and start at 1"
            )
        total_script_chars = sum(
            len(str(item.script_text or "")) for item in self.deliverables
        )
        if total_script_chars > MAX_MODEL_SOURCE_TEXT_CHARS:
            raise ValueError("intent deliverable scripts exceed the safe context bound")
        highest_ordinal = max(ordinals, default=0)
        invalid_requirement_ordinals = sorted({
            ordinal
            for requirement in self.intent_manifest.requirements
            for ordinal in requirement.deliverable_ordinals
            if ordinal > highest_ordinal
        })
        if invalid_requirement_ordinals:
            raise ValueError(
                "intent requirements cite unknown deliverable ordinals: "
                f"{invalid_requirement_ordinals}"
            )
        return self


class ContentProducerProductSelection(BaseModel):
    """Catalog-grounded product choice inferred from the user's own words."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["keep", "select", "clear", "unresolved"] = "keep"
    product_id: int | None = Field(default=None, ge=1)
    evidence_quote: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _selection_contract(self) -> "ContentProducerProductSelection":
        if self.action == "select" and self.product_id is None:
            raise ValueError("select requires one catalog product_id")
        if self.action != "select" and self.product_id is not None:
            raise ValueError("only select may provide product_id")
        if self.action in {"select", "clear"} and not str(
            self.evidence_quote or ""
        ).strip():
            raise ValueError(f"{self.action} requires an exact user evidence quote")
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
    intent_spec: ContentProducerIntentSpec | None = None
    product_selection: ContentProducerProductSelection = Field(
        default_factory=ContentProducerProductSelection
    )
    pending_decision_id: str | None = Field(default=None, max_length=80)

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


class ContentProducerSemanticReview(BaseModel):
    """Independent semantic audit of one Producer handoff.

    The first Producer pass owns creative interpretation.  This second pass has
    no authority to invent a new brief; it only reconciles message chronology
    and removes contradictions that would otherwise strand downstream roles
    behind an impossible immutable contract.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["pass", "revised", "needs_input"]
    issues: list[str] = Field(default_factory=list, max_length=8)
    reviewed_intent_spec: ContentProducerIntentSpec | None = None
    reviewed_proposal: ContentProducerProposal | None = None
    assistant_question: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _review_contract(self) -> "ContentProducerSemanticReview":
        if self.verdict == "needs_input" and not str(
            self.assistant_question or ""
        ).strip():
            raise ValueError(
                "needs_input semantic review requires one assistant_question"
            )
        if self.verdict in {"pass", "revised"} and (
            self.reviewed_intent_spec is None
        ):
            raise ValueError(
                "pass or revised semantic review requires reviewed_intent_spec"
            )
        if self.verdict in {"pass", "revised"} and self.reviewed_proposal is None:
            raise ValueError(
                "pass or revised semantic review requires reviewed_proposal"
            )
        if self.verdict == "pass" and self.issues:
            raise ValueError("pass semantic review cannot report issues")
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
        if start < 0:
            raise ValueError("model response did not contain a JSON object")
        try:
            # Some gateways append a harmless explanation or duplicate JSON
            # object after the requested payload.  Decode the first complete
            # object instead of widening from the first ``{`` to the last
            # ``}``, which turns two valid adjacent objects into invalid JSON.
            parsed, _end = json.JSONDecoder().raw_decode(raw[start:])
        except json.JSONDecodeError as exc:
            raise ValueError(
                "model response did not contain one complete JSON object"
            ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("model response must be one JSON object")
    return parsed


def _normalize_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize redundant intent coordinates without changing their meaning.

    A model can correctly attach ``start_seconds`` and ``end_seconds`` to a
    requirement while accidentally leaving its scope as ``project`` or
    ``deliverable``.  The intent schema permits timing only on a
    ``time_window`` requirement.  When both ordered coordinates are present,
    the coordinates themselves are unambiguous authority, so promote only the
    scope instead of spending a second model call to rewrite the same intent.
    Incomplete or unordered timing remains invalid and still follows the
    ordinary bounded repair path.
    """

    normalized = copy.deepcopy(payload)
    intent_spec = normalized.get("intent_spec")
    if not isinstance(intent_spec, dict):
        return normalized
    manifest = intent_spec.get("intent_manifest")
    if not isinstance(manifest, dict):
        return normalized
    requirements = manifest.get("requirements")
    if not isinstance(requirements, list):
        return normalized
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        start = requirement.get("start_seconds")
        end = requirement.get("end_seconds")
        if (
            requirement.get("scope") != "time_window"
            and isinstance(start, (int, float))
            and not isinstance(start, bool)
            and isinstance(end, (int, float))
            and not isinstance(end, bool)
            and float(start) >= 0
            and float(end) > float(start)
        ):
            requirement["scope"] = "time_window"
    return normalized


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
    analysis = dict(row.analysis_json or {})
    # The browser needs extraction metadata, not another full copy of a large
    # project brief. The authoritative text remains server-side and is sent
    # only to the scoped Producer model packet.
    analysis.pop("document_text", None)
    return {
        "attachment_key": row.attachment_key,
        "kind": row.kind,
        "original_name": row.original_name,
        "mime_type": row.mime_type,
        "size_bytes": int(row.size_bytes or 0),
        "analysis_status": row.analysis_status,
        "analysis": analysis,
        "character_name": meta.get("character_name"),
        "character_description": meta.get("character_description"),
        "locked": row.project_asset_id is not None,
        "active": meta.get("active_for_current_requirement", True) is not False,
        "created_at": row.created_at,
    }


def _active_attachment_rows(
    db: Session,
    *,
    conversation: HermesAgentConversation,
) -> list[HermesContentProducerAttachment]:
    return [
        row
        for row in _attachment_rows(db, conversation=conversation)
        if dict(row.meta_json or {}).get(
            "active_for_current_requirement", True
        ) is not False
    ]


def _deactivate_prior_reference_videos(
    db: Session,
    *,
    conversation: HermesAgentConversation,
) -> None:
    for row in _attachment_rows(db, conversation=conversation):
        if row.kind != "reference_video":
            continue
        meta = dict(row.meta_json or {})
        if meta.get("active_for_current_requirement", True) is False:
            continue
        meta["active_for_current_requirement"] = False
        meta["superseded_for_current_requirement_at"] = (
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        )
        row.meta_json = meta
        db.add(row)


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


def begin_producer_followup(
    conversation: HermesAgentConversation,
) -> bool:
    """Open a new execution cycle without erasing the Producer conversation.

    A confirmed project is an immutable delivery milestone, not the end of the
    customer relationship.  The first later turn or upload archives that
    milestone, keeps its confirmed brief as the delta baseline, and clears only
    the active confirmation token.  This prevents a late duplicate confirm
    from creating another project while allowing the same scoped conversation
    to create an explicitly reviewed follow-up project.
    """

    meta = dict(conversation.meta_json or {})
    project_id = meta.get("created_project_id")
    if project_id is None:
        return False
    project_key = str(meta.get("created_project_key") or "").strip()
    history = [
        dict(item)
        for item in list(meta.get("created_projects") or [])
        if isinstance(item, dict) and item.get("project_id") is not None
    ]
    if not any(int(item.get("project_id") or 0) == int(project_id) for item in history):
        history.append({
            "project_id": int(project_id),
            "project_key": project_key or None,
            "proposal_sha256": str(meta.get("proposal_sha256") or "") or None,
            "created_at": str(meta.get("project_created_at") or "") or None,
        })
    meta["created_projects"] = history[-50:]
    if isinstance(meta.get("proposal"), dict):
        meta["baseline_proposal"] = dict(meta["proposal"])
    if isinstance(meta.get("intent_spec"), dict):
        meta["baseline_intent_spec"] = dict(meta["intent_spec"])
    meta.update({
        "status": "needs_input",
        "followup_parent_project_id": int(project_id),
        "followup_parent_project_key": project_key or None,
        "followup_started_at": datetime.now(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(),
    })
    for key in (
        "created_project_id",
        "created_project_key",
        "project_created_at",
        "proposal",
        "proposal_sha256",
        "pending_decision_id",
    ):
        meta.pop(key, None)
    conversation.meta_json = meta
    return True


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


def _normalize_extracted_text(value: str) -> str:
    text = str(value or "").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _validate_document_archive(archive: zipfile.ZipFile) -> set[str]:
    members = archive.infolist()
    if sum(int(item.file_size or 0) for item in members) > MAX_PRODUCER_DOCUMENT_ARCHIVE_BYTES:
        raise ValueError("document archive is too large")
    return {item.filename for item in members}


def _read_document_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    info = archive.getinfo(name)
    if int(info.file_size or 0) > MAX_PRODUCER_DOCUMENT_XML_BYTES:
        raise ValueError("document XML is too large")
    return ET.fromstring(archive.read(info))


def _extract_docx_text(source: Path) -> str:
    try:
        with zipfile.ZipFile(source) as archive:
            names = _validate_document_archive(archive)
            if "word/document.xml" not in names:
                raise ValueError("word/document.xml is missing")
            ordered = ["word/document.xml"] + sorted(
                name
                for name in names
                if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
                or name in {"word/footnotes.xml", "word/endnotes.xml"}
            )
            blocks: list[str] = []
            for name in ordered:
                root = _read_document_xml(archive, name)
                output: list[str] = []
                for node in root.iter():
                    local = str(node.tag).rsplit("}", 1)[-1]
                    if local == "t" and node.text:
                        output.append(node.text)
                    elif local == "tab":
                        output.append("\t")
                    elif local in {"br", "cr"}:
                        output.append("\n")
                    elif local in {"p", "tr"}:
                        output.append("\n")
                rendered = _normalize_extracted_text("".join(output))
                if rendered:
                    blocks.append(rendered)
            return _normalize_extracted_text("\n\n".join(blocks))
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile, ValueError) as exc:
        raise APIError(
            "CONTENT_PRODUCER_DOCUMENT_INVALID",
            "The DOCX attachment is not a readable Word document.",
            400,
        ) from exc


def _spreadsheet_column_index(cell_reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", str(cell_reference or ""))
    if not letters:
        return 0
    value = 0
    for character in letters.group(0).upper():
        value = value * 26 + (ord(character) - ord("A") + 1)
    return max(0, value - 1)


def _spreadsheet_cell_text(
    cell: ET.Element,
    *,
    shared_strings: list[str],
) -> str:
    cell_type = str(cell.attrib.get("t") or "")
    formula = next(
        (str(node.text or "") for node in cell if str(node.tag).rsplit("}", 1)[-1] == "f"),
        "",
    ).strip()
    value = next(
        (str(node.text or "") for node in cell if str(node.tag).rsplit("}", 1)[-1] == "v"),
        "",
    )
    if cell_type == "inlineStr":
        value = "".join(
            str(node.text or "")
            for node in cell.iter()
            if str(node.tag).rsplit("}", 1)[-1] == "t"
        )
    elif cell_type == "s":
        try:
            value = shared_strings[int(value)]
        except (ValueError, IndexError):
            value = ""
    elif cell_type == "b":
        value = "TRUE" if value == "1" else "FALSE"
    value = _normalize_extracted_text(value).replace("\n", " ")
    if formula and value:
        return f"={formula} -> {value}"
    if formula:
        return f"={formula}"
    return value


def _extract_spreadsheet_text(source: Path) -> str:
    try:
        with zipfile.ZipFile(source) as archive:
            names = _validate_document_archive(archive)
            required = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
            if not required.issubset(names):
                raise ValueError("workbook metadata is missing")

            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_root = _read_document_xml(archive, "xl/sharedStrings.xml")
                for item in shared_root:
                    if str(item.tag).rsplit("}", 1)[-1] != "si":
                        continue
                    shared_strings.append("".join(
                        str(node.text or "")
                        for node in item.iter()
                        if str(node.tag).rsplit("}", 1)[-1] == "t"
                    ))

            relationships_root = _read_document_xml(
                archive, "xl/_rels/workbook.xml.rels"
            )
            relationships = {
                str(node.attrib.get("Id") or ""): str(node.attrib.get("Target") or "")
                for node in relationships_root
                if str(node.tag).rsplit("}", 1)[-1] == "Relationship"
            }
            workbook_root = _read_document_xml(archive, "xl/workbook.xml")
            sheets = [
                node for node in workbook_root.iter()
                if str(node.tag).rsplit("}", 1)[-1] == "sheet"
            ]
            blocks: list[str] = []
            rendered_characters = 0
            relationship_namespace = (
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            for sheet_index, sheet in enumerate(sheets, start=1):
                sheet_name = str(sheet.attrib.get("name") or f"Sheet {sheet_index}")
                relationship_id = str(sheet.attrib.get(relationship_namespace) or "")
                target = relationships.get(relationship_id, "")
                if not target:
                    continue
                normalized_target = posixpath.normpath(
                    target.lstrip("/")
                    if target.startswith("/")
                    else posixpath.join("xl", target)
                )
                if normalized_target not in names:
                    continue
                sheet_root = _read_document_xml(archive, normalized_target)
                lines = [f"[Sheet: {sheet_name}]"]
                for row in (
                    node for node in sheet_root.iter()
                    if str(node.tag).rsplit("}", 1)[-1] == "row"
                ):
                    row_number = str(row.attrib.get("r") or "")
                    cells: list[str] = []
                    for cell in row:
                        if str(cell.tag).rsplit("}", 1)[-1] != "c":
                            continue
                        reference = str(cell.attrib.get("r") or "")
                        value = _spreadsheet_cell_text(
                            cell,
                            shared_strings=shared_strings,
                        )
                        if value:
                            column_index = _spreadsheet_column_index(reference)
                            column_label = re.sub(r"\d+$", "", reference) or str(
                                column_index + 1
                            )
                            cells.append(f"{column_label}={value}")
                    if cells:
                        lines.append(f"Row {row_number or '?'}: " + " | ".join(cells))
                    rendered_characters += sum(len(value) for value in cells)
                    if rendered_characters > MAX_PRODUCER_DOCUMENT_TEXT_CHARS * 2:
                        break
                blocks.append("\n".join(lines))
                if rendered_characters > MAX_PRODUCER_DOCUMENT_TEXT_CHARS * 2:
                    break
            return _normalize_extracted_text("\n\n".join(blocks))
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile, ValueError) as exc:
        raise APIError(
            "CONTENT_PRODUCER_DOCUMENT_INVALID",
            "The spreadsheet attachment is not a readable XLSX workbook.",
            400,
        ) from exc


def _extract_presentation_text(source: Path) -> str:
    try:
        with zipfile.ZipFile(source) as archive:
            names = _validate_document_archive(archive)
            slide_names = sorted(
                (
                    name for name in names
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ),
                key=lambda value: int(re.search(r"(\d+)", value).group(1)),
            )
            if not slide_names:
                raise ValueError("presentation slides are missing")
            blocks: list[str] = []
            for index, name in enumerate(slide_names, start=1):
                root = _read_document_xml(archive, name)
                paragraphs = []
                for paragraph in (
                    node for node in root.iter()
                    if str(node.tag).rsplit("}", 1)[-1] == "p"
                ):
                    rendered = "".join(
                        str(node.text or "")
                        for node in paragraph.iter()
                        if str(node.tag).rsplit("}", 1)[-1] == "t"
                    ).strip()
                    if rendered:
                        paragraphs.append(rendered)
                blocks.append(f"[Slide {index}]\n" + "\n".join(paragraphs))
            notes = sorted(
                (
                    name for name in names
                    if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)
                ),
                key=lambda value: int(re.search(r"(\d+)", value).group(1)),
            )
            for index, name in enumerate(notes, start=1):
                root = _read_document_xml(archive, name)
                rendered = "\n".join(
                    str(node.text or "").strip()
                    for node in root.iter()
                    if str(node.tag).rsplit("}", 1)[-1] == "t" and str(node.text or "").strip()
                )
                if rendered:
                    blocks.append(f"[Speaker notes {index}]\n{rendered}")
            return _normalize_extracted_text("\n\n".join(blocks))
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile, ValueError) as exc:
        raise APIError(
            "CONTENT_PRODUCER_DOCUMENT_INVALID",
            "The presentation attachment is not a readable PPTX file.",
            400,
        ) from exc


def _extract_open_document_text(source: Path) -> str:
    try:
        with zipfile.ZipFile(source) as archive:
            names = _validate_document_archive(archive)
            if "content.xml" not in names:
                raise ValueError("content.xml is missing")
            root = _read_document_xml(archive, "content.xml")
            blocks: list[str] = []
            for node in root.iter():
                local = str(node.tag).rsplit("}", 1)[-1]
                if local not in {"p", "h"}:
                    continue
                rendered = "".join(str(value or "") for value in node.itertext()).strip()
                if rendered:
                    blocks.append(rendered)
            return _normalize_extracted_text("\n".join(blocks))
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile, ValueError) as exc:
        raise APIError(
            "CONTENT_PRODUCER_DOCUMENT_INVALID",
            "The OpenDocument attachment is not readable.",
            400,
        ) from exc


def _extract_pdf_text(source: Path) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(source), strict=False)
        if len(reader.pages) > 300:
            raise ValueError("PDF has too many pages")
        parts: list[str] = []
        for page in reader.pages:
            parts.append(str(page.extract_text() or ""))
            if sum(len(item) for item in parts) >= MAX_PRODUCER_DOCUMENT_TEXT_CHARS:
                break
        return _normalize_extracted_text("\n\n".join(parts))
    except APIError:
        raise
    except Exception as exc:  # pypdf exposes several parser-specific exceptions
        raise APIError(
            "CONTENT_PRODUCER_DOCUMENT_INVALID",
            "The PDF attachment is encrypted, damaged, or contains no readable text.",
            400,
        ) from exc


def _extract_plain_text(source: Path) -> str:
    payload = source.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return _normalize_extracted_text(payload.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise APIError(
        "CONTENT_PRODUCER_DOCUMENT_INVALID",
        "The text attachment must use UTF-8, UTF-16, or GB18030 encoding.",
        400,
    )


def _extract_document_text(source: Path, extension: str) -> dict[str, Any]:
    if extension in PRODUCER_WORD_EXTENSIONS:
        text = _extract_docx_text(source)
    elif extension in PRODUCER_SPREADSHEET_EXTENSIONS:
        text = _extract_spreadsheet_text(source)
    elif extension in PRODUCER_PRESENTATION_EXTENSIONS:
        text = _extract_presentation_text(source)
    elif extension in PRODUCER_OPEN_DOCUMENT_EXTENSIONS:
        text = _extract_open_document_text(source)
    elif extension == ".pdf":
        text = _extract_pdf_text(source)
    else:
        text = _extract_plain_text(source)
    if not text:
        raise APIError(
            "CONTENT_PRODUCER_DOCUMENT_EMPTY",
            "The document does not contain readable text.",
            400,
        )
    original_characters = len(text)
    truncated = original_characters > MAX_PRODUCER_DOCUMENT_TEXT_CHARS
    text = text[:MAX_PRODUCER_DOCUMENT_TEXT_CHARS]
    return {
        "document_text": text,
        "extracted_characters": len(text),
        "original_characters": original_characters,
        "text_truncated": truncated,
        "document_format": extension.lstrip("."),
    }


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
    if normalized_kind not in {
        "reference_video", "character_reference", "supporting_material",
        "brief_document", "creative_reference",
    }:
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENT_KIND_INVALID",
            "Choose reference_video, character_reference, or supporting_material.",
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
    if normalized_kind == "supporting_material":
        if extension in PRODUCER_IMAGE_EXTENSIONS or mime.startswith("image/"):
            normalized_kind = "creative_reference"
        elif extension in PRODUCER_DOCUMENT_EXTENSIONS:
            normalized_kind = "brief_document"
        else:
            raise APIError(
                "CONTENT_PRODUCER_SUPPORTING_MATERIAL_INVALID",
                "Supporting material must be a supported document, spreadsheet, presentation, text, or image file.",
                400,
            )
    if normalized_kind == "reference_video":
        _deactivate_prior_reference_videos(
            db,
            conversation=conversation,
        )
        if extension not in PRODUCER_VIDEO_EXTENSIONS and not mime.startswith("video/"):
            raise APIError(
                "CONTENT_PRODUCER_REFERENCE_VIDEO_INVALID",
                "Reference video must be MP4, MOV, or WebM.",
                400,
            )
        max_bytes = MAX_PRODUCER_REFERENCE_VIDEO_BYTES
    elif normalized_kind in {"character_reference", "creative_reference"}:
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
    else:
        if sum(1 for item in existing if item.kind == "brief_document") >= MAX_PRODUCER_DOCUMENTS:
            raise APIError(
                "CONTENT_PRODUCER_TOO_MANY_DOCUMENTS",
                f"Upload at most {MAX_PRODUCER_DOCUMENTS} documents per conversation.",
                400,
            )
        if extension not in PRODUCER_DOCUMENT_EXTENSIONS:
            raise APIError(
                "CONTENT_PRODUCER_DOCUMENT_INVALID",
                "Document material must be a supported Word, Excel, PowerPoint, PDF, OpenDocument, or text file.",
                400,
            )
        max_bytes = MAX_PRODUCER_DOCUMENT_BYTES

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
        if normalized_kind in {"character_reference", "creative_reference"}:
            analysis = _render_image_preview(source, preview)
        elif normalized_kind == "reference_video":
            analysis = _probe_reference_video(source)
            _render_video_preview(
                source,
                preview,
                duration_seconds=float(analysis["duration_seconds"]),
            )
            analysis["preview_available"] = True
            analysis["transcript_status"] = "queued"
            analysis["multimodal_status"] = "queued"
        else:
            analysis = _extract_document_text(source, extension)
            preview = None
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
            preview_path=str(preview) if preview is not None else None,
            mime_type=upload.content_type,
            size_bytes=written,
            sha256=digest,
            analysis_status=(
                "processing" if normalized_kind == "reference_video" else "ready"
            ),
            analysis_json=analysis,
            meta_json={
                "source": "producer_intake",
                "active_for_current_requirement": True,
                "character_key": normalized_character_key or f"character_{uuid4().hex[:12]}",
                "character_name": str(character_name or Path(filename).stem or "Character")[:120],
                "character_description": str(character_description or "")[:2000],
            } if normalized_kind == "character_reference" else {
                "source": "producer_intake",
                "active_for_current_requirement": True,
            },
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
        if preview is not None:
            preview.unlink(missing_ok=True)
        raise


def stage_producer_reference_link(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    user_id: int,
    source_url: str,
    context_message: str | None = None,
) -> HermesContentProducerAttachment:
    """Stage a supported public benchmark URL for asynchronous yt-dlp intake."""

    try:
        safe_url = validate_share_url(source_url)
    except UnsafeShareURLError as exc:
        raise APIError(
            "CONTENT_PRODUCER_REFERENCE_URL_INVALID",
            str(exc),
            422,
        ) from exc
    url_digest = hashlib.sha256(safe_url.encode("utf-8")).hexdigest()
    for row in _attachment_rows(db, conversation=conversation):
        meta = dict(row.meta_json or {})
        if (
            row.kind == "reference_video"
            and str(meta.get("source_url_sha256") or "") == url_digest
        ):
            _deactivate_prior_reference_videos(db, conversation=conversation)
            meta["active_for_current_requirement"] = True
            if context_message:
                meta["analysis_request_context"] = str(context_message)[:50000]
            row.meta_json = meta
            db.add(row)
            return row

    existing = _attachment_rows(db, conversation=conversation)
    if len(existing) >= MAX_PRODUCER_ATTACHMENTS:
        raise APIError(
            "CONTENT_PRODUCER_TOO_MANY_ATTACHMENTS",
            f"Upload at most {MAX_PRODUCER_ATTACHMENTS} attachments per conversation.",
            400,
        )
    _deactivate_prior_reference_videos(db, conversation=conversation)
    session_key = str(dict(conversation.meta_json or {}).get("session_key") or "session")
    intake_dir = (
        PRODUCER_STORAGE_ROOT
        / f"workspace_{int(conversation.workspace_id)}"
        / f"user_{int(user_id)}"
        / re.sub(r"[^A-Za-z0-9_.-]+", "-", session_key)[:64]
    )
    _ensure_intake_dir(intake_dir)
    attachment_key = f"pa_{uuid4().hex}"
    host = re.sub(
        r"[^A-Za-z0-9.-]+",
        "-",
        str(urlparse(safe_url).hostname or "benchmark"),
    )[:80]
    source = intake_dir / f"{attachment_key}_benchmark.mp4"
    preview = intake_dir / f"{attachment_key}_preview.jpg"
    row = HermesContentProducerAttachment(
        attachment_key=attachment_key,
        conversation_id=int(conversation.id),
        workspace_id=int(conversation.workspace_id),
        user_id=int(user_id),
        kind="reference_video",
        original_name=f"{host}-{url_digest[:10]}.mp4",
        file_path=str(source),
        preview_path=str(preview),
        mime_type="video/mp4",
        size_bytes=0,
        sha256="0" * 64,
        analysis_status="queued",
        analysis_json={
            "download_status": "queued",
            "transcript_status": "queued",
            "multimodal_status": "queued",
            "source_host": host,
        },
        meta_json={
            "source": "producer_reference_url",
            "source_url": safe_url,
            "source_url_sha256": url_digest,
            "analysis_request_context": str(context_message or "")[:50000],
            "active_for_current_requirement": True,
        },
    )
    db.add(row)
    meta = dict(conversation.meta_json or {})
    meta.update({
        "status": "idle",
        "attachments_updated_at": (
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        ),
    })
    meta.pop("proposal", None)
    meta.pop("proposal_sha256", None)
    conversation.meta_json = meta
    db.add(conversation)
    db.flush()
    return row


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
    ready = [
        row for row in attachments
        if row.analysis_status == "ready" and row.preview_path
        and row.kind in {"reference_video", "character_reference", "creative_reference"}
        # A reference video is already inspected by the isolated multimodal
        # analyst before the Producer may continue.  Its structured semantic
        # result is present in user_attachments.  Reattaching the contact sheet
        # on every conversational turn duplicates a large base64 image and can
        # trigger Hermes context compression.  Raw pixels remain appropriate
        # for character/creative images that have not had that specialist pass.
        and not (
            row.kind == "reference_video"
            and str(dict(row.analysis_json or {}).get("multimodal_status") or "")
            == "success"
            and bool(
                dict(row.analysis_json or {}).get("visual_semantic_analysis")
            )
        )
    ]
    if not ready:
        return None
    content: list[dict[str, Any]] = [{"type": "input_text", "text": packet_json}]
    for index, row in enumerate(ready[:17], start=1):
        label = (
            f"Attachment {index}: {row.kind}; authoritative user upload name={row.original_name}. "
            "Inspect visible pixels and use them only for the requested creative planning role. "
            "A creative_reference may define visual style, setting, composition, or another user-stated purpose; do not assume it is a character identity."
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
    for attachment in _active_attachment_rows(db, conversation=conversation):
        bound_to_prior_project = False
        if attachment.project_asset_id is not None:
            existing = db.query(HermesContentFactoryAsset).filter(
                HermesContentFactoryAsset.id == int(attachment.project_asset_id),
            ).one_or_none()
            if existing is None:
                raise APIError(
                    "CONTENT_PRODUCER_ATTACHMENT_BINDING_INVALID",
                    "A staged attachment has an invalid project binding.",
                    409,
                )
            if int(existing.project_id) == int(project.id):
                rows.append(existing)
                continue
            # The same durable user reference may seed a separately confirmed
            # follow-up project.  Clone it into the new project but retain the
            # original attachment binding as the audit anchor for the first
            # project.
            bound_to_prior_project = True
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
        if attachment.kind == "reference_video":
            stage, kind, asset_role = "REFERENCE_VIDEO", "reference_video", "reference_video"
            producer_analysis = dict(attachment.analysis_json or {})
            meta["producer_benchmark_analysis"] = {
                "duration_seconds": producer_analysis.get("duration_seconds"),
                "detected_language": producer_analysis.get("detected_language"),
                "transcript_status": producer_analysis.get("transcript_status"),
                "transcript": str(producer_analysis.get("transcript") or "")[:24000],
                "segments": [
                    dict(item)
                    for item in list(producer_analysis.get("segments") or [])[:160]
                    if isinstance(item, dict)
                ],
                "multimodal_status": producer_analysis.get("multimodal_status"),
                "visual_semantic_analysis": dict(
                    producer_analysis.get("visual_semantic_analysis") or {}
                ),
            }
        elif attachment.kind == "character_reference":
            stage, kind, asset_role = "CHARACTER_REFERENCE", "character_reference", "character_reference"
        else:
            stage, kind = "SOURCE", "source"
            asset_role = "creative_reference" if attachment.kind == "creative_reference" else "project_brief"
        meta.update({
            "source": "producer_intake",
            "producer_attachment_key": attachment.attachment_key,
            "producer_attachment_kind": attachment.kind,
            "bridge_path": str(bridge_target),
            "browser_inbox_relative": f"workspace_{project.workspace_id}/{project.project_key}/{disk_name}",
            "asset_role": asset_role,
        })
        row = HermesContentFactoryAsset(
            project_id=int(project.id),
            workspace_id=int(project.workspace_id),
            user_id=int(user_id),
            stage=stage,
            kind=kind,
            original_name=attachment.original_name,
            file_path=str(target),
            mime_type=attachment.mime_type,
            size_bytes=int(attachment.size_bytes),
            meta_json=meta,
        )
        db.add(row)
        db.flush()
        if not bound_to_prior_project:
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
- A created project is a durable milestone, not the end of the conversation.
  When project_history identifies an earlier project, interpret requests such
  as add more videos, change a requirement, or make another version as a new
  separately confirmed follow-up execution. Inherit the unchanged confirmed
  brief, explain the delta, and never imply that the earlier project or its
  completed files will be mutated or erased.
- For every changed proposal field, list that field in changed_fields and put
  one exact verbatim quote from latest_user_message in change_evidence. Do not
  cite an older user message, an assistant message or your own inference. If no
  proposal field changes, return empty changed_fields and change_evidence.
- authoritative_source_texts are immutable user source assets such as supplied
  scripts. Preserve them verbatim and design around them unless the latest user
  message explicitly asks to revise them.
- authoritative_attachment_texts are text extracted from user-uploaded project
  briefs and handoff documents. Treat the extracted content and its ordering as
  authoritative requirements. A handoff document may contain several locked
  scripts; do not collapse it into one script or silently rewrite it.
- current_authoritative_script is the latest durable version of the locked
  script. It overrides the original immutable source text after an authorized
  revision. Never fall back to an older script version.
- If one authoritative_source_text is a complete audience-facing script the
  user wants produced, set authoritative_script_message_id to its message_id.
  Keep the existing authoritative script id on later turns unless the latest
  user explicitly replaces or removes that script. Do not mark a planning brief
  or general requirements as a verbatim script.
- Compile the effective request into intent_spec.intent_manifest. It is the
  authoritative professional handoff to every downstream AI role, not a fixed
  creative template. Give every independently testable requirement a stable
  R-001 style ID, its exact user evidence, your operational interpretation,
  and observable checks. Preserve user freedom: do not invent locks the user
  did not ask for.
- current_working_intent_spec is the previously accepted handoff. Preserve its
  unchanged requirements, IDs and exact evidence quotes. Add or revise only
  what the latest user delta requires. Evidence quotes may come only from a
  user message or authoritative_attachment_texts; never turn an assistant
  summary or your own paraphrase into evidence.
- Conversation chronology is authoritative when requirements evolve. A later
  explicit request can supersede an earlier preservation rule even when the
  user does not say the word "cancel". For example, a later request for several
  differentiated original videos can broaden an earlier one-variable control
  experiment. Preserve the earlier rule only for the deliverable to which it
  still applies; do not keep it as a project-wide lock that contradicts the
  newer authorized scope.
- Before returning, reconcile intent_manifest requirements, deliverables and
  transformation_contract as one semantic whole. protected_requirements,
  authorized_changes, creative_freedom and excluded_source_artifacts must be
  mutually compatible. If chronology resolves a conflict, revise or remove
  the superseded older requirement and retain its exact evidence only where it
  still applies. If chronology genuinely cannot resolve it, ask one grouped
  question instead of returning an impossible immutable contract.
- A requirement may describe an outcome, mechanism, intensity, preservation
  boundary, conversion job, visual/audio identity, or final acceptance test.
  Do not reduce semantic requests to booleans such as fast_opening=true. For
  example, a benchmark-hook requirement must explain the abstract attention
  mechanism to transfer, the source-specific expression that must be reinvented,
  and the observable first-seconds evidence that would prove success.
- Product truth, safety, provider capability and creative intensity are
  separate dimensions. Never translate a truthful or provider-safe request
  into a calm generic lifestyle scene merely because it is easier to render.
  When the user asks for tension, contradiction, surprise, strong emotion,
  visual abnormality, rapid escalation, high cut density or a forceful hook,
  preserve that requested energy in the requirement interpretation and write
  concrete observable checks for its timing and escalation. Leave the Director
  free to invent an original mechanism; constrain facts and forbidden source
  signatures, not dramatic strength.
- Keep product association separate from product appearance. Set
  product_use_mode=required only when the deliverables must show, name, explain,
  recommend or convert through the selected product. Set
  product_use_mode=context_only when the selected product supplies category,
  audience or campaign context but the requested deliverables must not show or
  name the product, brand, package, offer or CTA. Set product_use_mode=none only
  for general content with no selected-product role. A contextual product must
  never create product-reveal, reason-to-choose or conversion review gates.
- Mark explicit locks and outcome-critical instructions critical, meaningful
  creative/quality requirements high, and preferences normal. Every critical
  or high requirement must be independently actionable by the Director.
- When the user supplies an existing video and asks to preserve, optimize,
  adapt, or change it, create intent_spec.intent_manifest.transformation_contract. This is a
  semantic diff authored from the user's actual words, not a fixed template:
  identify what the source means to the project, the required fidelity, every
  protected requirement, only the authorized changes (with time windows when
  stated), remaining creative freedom, and observable success checks.
- Choose execution_strategy from the requested fidelity. Use local_edit when
  the source must remain exact except for bounded overlays/cuts/crops;
  selective_regeneration only when specific source spans may be replaced; and
  full_regeneration only when the source is inspiration/adaptive. Never use
  full_regeneration to satisfy exact_outside_authorized_changes or exact.
- Preserve semantic structure separately from source media. When the user asks
  AI to remake, reconstruct, re-create, or optimize a reference while retaining
  its hook mechanism, story beats, pacing, shot rhythm, or conversion order,
  use transfer_mode=semantic_structure, source_media_reuse=forbidden,
  fidelity=adaptive, and an AI regeneration strategy. Do not reinterpret those
  structural locks as permission to copy pixels, actors, voices, source audio,
  captions, platform chrome, watermarks, or watermark-covering text bands.
- Do not treat "reference the hook" or "reference the visual pacing" as a lock
  on the source story idea. Unless the user explicitly names a narrative device
  that must remain, hook/pacing reference means abstract attention architecture
  only: conflict speed, surprise strength, cut density, tension curve, reveal
  timing, and conversion timing. The new work must replace the source premise,
  signature metaphor, character roles, setting, props, action sequence,
  dialogue conceit, and product-transition device. Put those distinctive source
  signatures in excluded_source_artifacts. If the user explicitly preserves
  one of them, quote that requirement in protected_requirements; never infer it
  from the word hook alone.
- Put source-only artifacts that must disappear from the new work in
  excluded_source_artifacts. Visible top/bottom banners, embedded captions,
  search bars, usernames, platform UI, logos, and watermark covers are not
  creative content merely because they appear in a reference preview. Never
  copy their pixels, wording, or platform chrome. If the user explicitly says
  those bands serve a functional purpose such as covering a provider watermark
  and wants that purpose retained, record a protected requirement for freshly
  authored local overlays based on the new script; do not record an output-wide
  overlay prohibition. The Director must create new display_lines and local
  overlay presentation, while the video provider still receives no captions,
  search bars, platform UI, or watermark-cover artwork.
- Distinguish these common but materially different requests:
  * independent_videos: each output has its own story or script;
  * series_episodes: separate outputs share a world or series identity;
  * visual_variants: the same locked copy is intentionally rendered several
    ways;
  * single: one output only.
  Never infer visual_variants merely because video_count is greater than one.
- A long handoff or requirements document can contain several scripts without
  being one audience-facing script. Classify it as multi_script_package and
  create one deliverable per script. Put the complete script for that output in
  deliverables[].script_text. Use source_material_mode=requirements when the
  source is a brief rather than spoken copy.
- If distinct locked scripts are supplied for N outputs, intent_spec must carry
  exactly N ordered deliverables and proposal.video_count must equal N. If one
  locked script is intentionally reused, use delivery_mode=visual_variants and
  keep the one authoritative script instead of duplicating it N times.
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
- Missing visual detail is not a blocker when the requested result can be made
  truthfully without showing that detail.  In particular, a negative request
  such as "do not invent an unverified decorative shape" is executable without
  knowing the exact hidden detail: prohibit the invention, avoid a readable
  close-up of the unknown detail, and use the authoritative source asset, an
  occluded handling action, or a later verified reference instead. Ask for another
  asset only when the user explicitly requires a clear view of the unknown
  detail and there is no truthful production alternative.
- Treat an explicit request to generate, make, create, start or execute the
  described videos as execution intent, not an invitation to keep discussing.
  When the effective brief is executable, return proposal_ready with no
  artificial blocker.  The application will still enforce the signed proposal
  and explicit user-authority boundary before it creates media work.
- Do not use a fixed story template. Select a format from the user's goal,
  audience, platform and supplied truth.
- For short-form attention or conversion work, professional defaults must not
  mean a calm sequence of agreeable lifestyle images with no change of state.
  Unless the user explicitly requests a meditative or deliberately low-energy
  form, creative_strategy must give the audience something to experience: a
  recognizable pressure, question, contradiction, unmet expectation or other
  genre-appropriate tension; a visible escalation or turn; and a memorable
  highlight/payoff that earns the product, idea or CTA transition. "Conflict"
  is semantic, not a mandatory argument or danger scene. Choose an original
  mechanism for this audience and product, and describe its observable timing,
  emotional movement and conversion bridge without imposing one reusable plot.
- Inspect every supplied image attachment preview. Character images are identity and
  appearance references, not product or claim evidence. A reference-video
  contact sheet has already been inspected by the isolated multimodal analyst;
  use technical_summary.visual_semantic_analysis as its pixel-grounded visual
  evidence instead of expecting its raw preview here. When
  technical_summary.transcript_status is success, also use its timestamped
  transcript as the video's speech evidence. When it is no_speech or failed,
  never claim to know audio that is not present in the packet.
  Generic creative-reference images may communicate style, scene, composition,
  product presentation, or another user-stated purpose; infer their role from
  the conversation and confirm that interpretation in assistant_message.
- For an active reference video, technical_summary.visual_semantic_analysis is
  the pixel-grounded multimodal authority. Before proposing production, explain
  its opening-hook sequence, story progression, pacing/edit grammar, product or
  conversion transition, transferable mechanisms, and source-specific elements
  that must not be copied. Discuss these findings with the user and ask one
  concise grouped question about what to transfer or change when that intent is
  not already explicit. Do not call a transcript or one preview image a
  completed benchmark analysis, and do not promise a replica before this
  multimodal evidence is present.
- "Replicate" means reproduce the agreed effectiveness mechanism in original
  media unless the user explicitly authorizes a bounded local edit of media
  they own. Never send the benchmark source video to a generation provider by
  default; the factory uses newly generated references plus segment prompts.
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
  Resolve ordinary natural-language product choices against available_products:
  when the latest user clearly names, describes, or answers with exactly one
  catalog product, return product_selection.action=select with that catalog ID
  and one exact quote from latest_user_message. Do not ask the user to repeat a
  choice that is already clear from the conversation. The backend will then
  rerun you with that product's authoritative facts before accepting a proposal.
  On this preliminary select or clear response, keep assistant_message to one
  short confirmation sentence and set intent_spec=null and proposal=null. Do
  not spend tokens drafting a plan that the backend must discard and reground.
  Use action=unresolved and ask one concise question only when two or more
  catalog products remain genuinely plausible. Use action=clear with an exact
  latest-user quote when the user explicitly says the work must not be bound to
  any product. Otherwise use action=keep. Never guess a catalog ID.
  General non-product content is allowed.
- For TikTok, normally use 9:16 and en-US only when the intended audience is
  the US; respect explicit user instructions over defaults.
- Choose proposal.video_model from video_model_catalog, explain the choice in
  assistant_message, and mention one material tradeoff when another available
  model is also compatible. Never default to Omni merely because the user did
  not name a model. Recommend from the requested pacing, generation mode,
  reference needs, target duration, route health and provider capabilities.
- video_duration_strategy=creative_flexibility preserves the preferred
  provider's legal 4/6/8/10-second or continuous segment rhythm and falls back
  only to routes compatible with that exact plan.
  video_duration_strategy=cross_provider_portable may use the common duration
  set when the user values maximum cross-provider portability over edit rhythm.
- preferred_segment_durations_seconds is the structured, user-confirmable
  topology chosen from video_model_catalog. When you recommend an explicit
  rhythm such as 7+7+6, write [7, 7, 6] here; never leave that decision only in
  pacing or assistant prose. Use [] only when segment topology is genuinely
  left to the downstream Director.
- spoken_density describes spoken-copy density, not visual cut speed. Use dense
  for fast continuous TikTok narration, balanced for ordinary narration, and
  sparse only for deliberately quiet or visual-led work. If the user asks for
  fast pacing but supplies a very short locked script for a long duration, do
  not pretend both are compatible: ask whether to shorten the duration, expand
  the script, or keep fast visuals with explicitly sparse speech.
- A complete locked script must fit the proposed duration at a fast but
  intelligible speaking rate. Never compress a full script into an impossible
  duration. If the user explicitly demands an incompatible duration, ask
  whether to extend the video or authorize shortening the script.
- Audio direction must make narrator voice, character dialogue and identity
  continuity explicit. Do not assume that narrator gender changes a speaking
  character's gender.
- video_generation_mode is a production contract, not a visual-style hint.
  Use text_to_video when the user explicitly requests text-to-video, says no
  reference images, or asks for prompt-only generation. In text_to_video mode
  the factory must not generate, upload, extract, or attach any reference image
  or reference video. Use video_to_video only when the user explicitly asks to
  drive generation from an uploaded video. Otherwise use image_to_video.
- visual_reference_generation_mode controls only how still references are
  packaged before local extraction. Prefer board for image_to_video projects:
  one ordered board (or one board per seven references) preserves cast, scene,
  style and chronological order while reducing paid image calls. Choose
  individual only when the user explicitly asks for separately rendered stills
  or a provider cannot produce a splittable board. Pixel-grounded repair may
  still regenerate only failed native panels without changing this project
  preference.
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
  "intent_spec": null | {
    "delivery_mode": "single" | "independent_videos" | "series_episodes" | "visual_variants",
    "source_material_mode": "requirements" | "single_script" | "multi_script_package" | "reference_copy" | "none",
    "user_goal": "plain-language effective goal",
    "intent_manifest": {
      "schema_version": "2.0",
      "objective": "the complete effective creative objective",
      "requirements": [{
        "requirement_id": "R-001",
        "kind": "objective" | "preservation" | "differentiation" | "reference_transfer" | "functional_artifact" | "conversion" | "visual" | "audio" | "acceptance",
        "priority": "critical" | "high" | "normal",
        "scope": "project" | "deliverable" | "time_window" | "final_output",
        "deliverable_ordinals": [],
        "start_seconds": null,
        "end_seconds": null,
        "intent": "what the user needs",
        "evidence_quote": "exact quote from a user message or extracted user document",
        "source_message_id": null | 123,
        "interpretation": "precise professional meaning passed downstream",
        "observable_checks": ["what visible, audible, textual, or structural evidence proves it"],
        "creative_freedom": ["what the downstream AI remains free to invent"],
        "must_not_reuse": ["source-specific expressions that must be reinvented"]
      }],
      "transformation_contract": null | {
      "source_role": "plain role of the supplied source",
      "fidelity": "inspiration" | "adaptive" | "exact_outside_authorized_changes" | "exact",
      "execution_strategy": "director_decides" | "local_edit" | "selective_regeneration" | "full_regeneration",
      "transfer_mode": "inspiration_only" | "semantic_structure" | "selective_elements" | "source_media",
      "source_media_reuse": "director_decides" | "forbidden" | "allowed" | "required",
      "protected_requirements": ["verbatim user-owned preservation rules"],
      "authorized_changes": [{
        "instruction": "the one authorized change",
        "dimensions": ["generic affected media dimensions"],
        "start_seconds": null | 0,
        "end_seconds": null | 2,
        "evidence_quote": null | "exact supporting user quote"
      }],
      "creative_freedom": ["only what remains open"],
      "excluded_source_artifacts": ["source-only pixels, text, UI, watermarks, or identities that must not carry into the result"],
      "success_checks": ["observable source-versus-result checks"],
        "rationale": "why this fidelity and strategy follow the request"
      },
      "manifest_sha256": null
    },
    "deliverables": [{
      "ordinal": 1,
      "label": "user-visible output name",
      "objective": "the job of this output",
      "relationship": "standalone" | "independent" | "series_episode" | "visual_variant",
      "script_text": null | "complete script for this output only",
      "source_message_id": null | 123,
      "target_duration_seconds": null | 50,
      "must_preserve": ["output-specific locks"],
      "differentiation": ["how this output differs"]
    }]
  },
  "product_selection": {
    "action": "keep" | "select" | "clear" | "unresolved",
    "product_id": null | 123,
    "evidence_quote": null | "exact quote from latest_user_message"
  },
  "proposal": null | {
    "title": "...",
    "content_objective": "...",
    "target_audience": "...",
    "content_mode": "product" | "general",
    "product_use_mode": "required" | "context_only" | "none",
    "platform": "tiktok",
    "video_count": 1,
    "video_duration_min_seconds": 10,
    "video_duration_max_seconds": 10,
    "video_model": "omni_flash" | "seedance_2_0_mini",
    "video_duration_strategy": "creative_flexibility" | "cross_provider_portable",
    "preferred_segment_durations_seconds": [7, 7, 6],
    "video_resolution": "480p" | "720p",
    "video_aspect_ratio": "9:16" | "16:9" | "1:1",
    "video_language": "en-US" | "zh-CN",
    "visual_style": "...",
    "pacing": "...",
    "spoken_density": "sparse" | "balanced" | "dense",
    "spoken_density_reason": "why this speech density matches the request",
    "audio_direction": "...",
    "conversion_direction": null | "...",
    "creative_constraints": ["..."],
    "video_generation_mode": "text_to_video" | "image_to_video" | "video_to_video",
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


def _script_delivery_units(script: str, *, language: str) -> tuple[int, str]:
    value = str(script or "").strip()
    if str(language or "").lower().startswith("zh"):
        return len(re.findall(r"[\u3400-\u9fff]", value)), "Chinese characters"
    return len(
        re.findall(r"[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*", value)
    ), "English words"


def _minimum_locked_script_density(
    proposal: ContentProducerProposal,
) -> int:
    density = str(proposal.spoken_density or "balanced").strip().lower()
    chinese = str(proposal.video_language or "").lower().startswith("zh")
    if density == "dense":
        return 180 if chinese else 115
    if density == "balanced":
        return 120 if chinese else 75
    return 0


def _decision_script_text(
    db: Session,
    *,
    conversation: HermesAgentConversation,
    decision: ContentProducerDecision,
) -> str:
    revised = str(decision.revised_authoritative_script or "").strip()
    if revised:
        return revised
    current = _authoritative_script_record(db, conversation=conversation)
    message_id = decision.authoritative_script_message_id
    # A later Producer turn normally echoes the immutable source message id.
    # When that id still owns the current version, the versioned text is the
    # authority.  Reading the original row first silently resurrected v1 after
    # every approved edit.
    if (
        current is not None
        and message_id is not None
        and int(current.get("source_message_id") or 0) == int(message_id)
    ):
        return str(current.get("text") or "").strip()
    if message_id is not None:
        row = _script_source_row(
            db,
            conversation=conversation,
            message_id=int(message_id),
        )
        if row is not None:
            return str(row.content_text or "").strip()
    return str((current or {}).get("text") or "").strip()


def _intent_script_specs(
    decision: ContentProducerDecision,
) -> list[ContentProducerDeliverableSpec]:
    intent = decision.intent_spec
    if intent is None:
        return []
    return [item for item in intent.deliverables if str(item.script_text or "").strip()]


def _validate_intent_spec(
    decision: ContentProducerDecision,
) -> None:
    proposal = decision.proposal
    intent = decision.intent_spec
    if proposal is None or intent is None:
        return
    intent.intent_manifest = sign_creative_intent_manifest(
        intent.intent_manifest
    )
    deliverables = list(intent.deliverables)
    if intent.delivery_mode in {"independent_videos", "series_episodes"}:
        if len(deliverables) != int(proposal.video_count):
            raise ValueError(
                "independent or series delivery requires exactly one ordered "
                "deliverable specification per requested video"
            )
    elif intent.delivery_mode == "single" and int(proposal.video_count) != 1:
        raise ValueError("single delivery mode requires video_count=1")
    elif intent.delivery_mode == "visual_variants" and len(deliverables) > 1:
        raise ValueError(
            "visual_variants uses one shared script/brief; do not create "
            "separate script deliverables"
        )

    if intent.source_material_mode == "multi_script_package":
        if intent.delivery_mode not in {"independent_videos", "series_episodes"}:
            raise ValueError(
                "a multi-script package must produce independent videos or series episodes"
            )
        if len(_intent_script_specs(decision)) != int(proposal.video_count):
            raise ValueError(
                "multi_script_package requires one complete script_text per video"
            )

    for item in deliverables:
        if item.target_duration_seconds is not None and not (
            int(proposal.video_duration_min_seconds)
            <= int(item.target_duration_seconds)
            <= int(proposal.video_duration_max_seconds)
        ):
            raise ValueError(
                f"deliverable {item.ordinal} target duration must be inside "
                "the proposal duration range"
            )
        script_text = str(item.script_text or "").strip()
        if not script_text:
            continue
        minimum_seconds = _minimum_script_seconds(script_text)
        available_seconds = int(
            item.target_duration_seconds
            or proposal.video_duration_max_seconds
        )
        if available_seconds < minimum_seconds:
            raise ValueError(
                f"deliverable {item.ordinal} locked script requires at least "
                f"{minimum_seconds} seconds; increase its duration or return "
                "needs_input to ask for permission to shorten that script"
            )
        minimum_density = _minimum_locked_script_density(proposal)
        units, unit_label = _script_delivery_units(
            script_text,
            language=proposal.video_language,
        )
        actual_density = round(units * 60 / max(1, available_seconds))
        if minimum_density and actual_density < minimum_density:
            maximum_duration = max(
                1,
                int(math.floor(units * 60 / minimum_density)),
            )
            raise ValueError(
                f"deliverable {item.ordinal} locked script has {units} "
                f"{unit_label} across {available_seconds} seconds "
                f"({actual_density} per minute), but spoken_density="
                f"{proposal.spoken_density} requires at least "
                f"{minimum_density} per minute. Return needs_input and ask "
                f"the user to shorten this deliverable to about "
                f"{maximum_duration} seconds, authorize expanding the script, "
                "or explicitly choose sparse spoken delivery. Do not silently "
                "pad the video with empty spoken segments."
            )


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
    _validate_intent_spec(decision)
    if decision.intent_spec is not None:
        manifest = decision.intent_spec.intent_manifest
        user_messages = [
            str(row.content_text or "")
            for row in _history(db, conversation=conversation)
            if row.role == "user"
        ]
        attachment_texts = [
            str(dict(row.analysis_json or {}).get("document_text") or "")
            for row in db.query(HermesContentProducerAttachment)
            .filter(
                HermesContentProducerAttachment.conversation_id
                == int(conversation.id),
                HermesContentProducerAttachment.workspace_id
                == int(conversation.workspace_id),
                HermesContentProducerAttachment.user_id
                == int(conversation.user_id),
            )
            .all()
        ]
        evidence_sources = [*user_messages, *attachment_texts]
        for requirement in manifest.requirements:
            if not any(
                requirement.evidence_quote in message
                for message in evidence_sources
            ):
                raise ValueError(
                    "intent requirement evidence_quote must be an exact "
                    "quote from a user message or extracted user document: "
                    f"{requirement.requirement_id}"
                )
        contract = manifest.transformation_contract
        if contract is not None:
            for change in contract.authorized_changes:
                quote = str(change.evidence_quote or "").strip()
                quote_supported = not quote or any(
                    quote in str(row.content_text or "")
                    for row in _history(db, conversation=conversation)
                    if row.role == "user"
                )
                if not quote_supported:
                    raise ValueError(
                        "authorized source-change evidence_quote must be an "
                        "exact quote from a user message in this conversation"
                    )
    script = _decision_script_text(
        db,
        conversation=conversation,
        decision=decision,
    )
    if decision.proposal is None or not script:
        return
    intent = decision.intent_spec
    # A requirements handoff or package of several distinct scripts is not one
    # audience-facing voiceover.  Each deliverable was validated above.  The
    # old aggregate check was the direct cause of six 45-65 second scripts
    # being measured as one impossible 912-second video.
    if intent is not None and intent.source_material_mode in {
        "requirements",
        "multi_script_package",
        "reference_copy",
        "none",
    }:
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
    proposal_value = meta.get("proposal")
    if not isinstance(proposal_value, dict):
        proposal_value = meta.get("baseline_proposal")
    current_proposal = (
        dict(proposal_value) if isinstance(proposal_value, dict) else None
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
    attachments = _active_attachment_rows(db, conversation=conversation)
    current_script = _authoritative_script_record(
        db,
        conversation=conversation,
    )
    video_models = []
    for item in video_model_routing_catalog(db):
        video_models.append({
            "id": item.get("id"),
            "label": item.get("label"),
            "available": bool(item.get("available")),
            "reference_image_limit": int(
                item.get("reference_image_limit") or 0
            ),
            "available_durations_seconds": list(
                item.get("available_durations_seconds") or []
            ),
            "available_generation_modes": list(
                item.get("available_generation_modes") or []
            ),
            "routes": [
                {
                    "provider": route.get("provider_label")
                    or route.get("provider_key"),
                    "priority": route.get("priority"),
                    "reference_image_limit": route.get(
                        "reference_image_limit"
                    ),
                    "capabilities": route.get("capabilities") or {},
                }
                for route in list(item.get("routes") or [])
            ],
        })
    attachment_texts: list[dict[str, Any]] = []
    attachment_text_chars = 0
    for attachment in attachments:
        analysis = dict(attachment.analysis_json or {})
        content = str(analysis.get("document_text") or "")
        if not content or attachment_text_chars >= MAX_MODEL_SOURCE_TEXT_CHARS:
            continue
        remaining = MAX_MODEL_SOURCE_TEXT_CHARS - attachment_text_chars
        included = content[:remaining]
        attachment_texts.append({
            "attachment_key": attachment.attachment_key,
            "original_name": attachment.original_name,
            "sha256": attachment.sha256,
            "character_count": len(content),
            "content": included,
            "context_truncated": len(included) < len(content),
        })
        attachment_text_chars += len(included)
    return {
        "operation": "content_factory_project_intake",
        "prompt_version": PRODUCER_PROMPT_VERSION,
        "latest_user_message": (
            str(next((row.content_text for row in reversed(deduplicated) if row.role == "user"), "") or "")
        ),
        "current_working_proposal": current_proposal,
        "current_working_intent_spec": (
            dict(meta["intent_spec"])
            if isinstance(meta.get("intent_spec"), dict)
            else None
        ),
        "project_history": {
            "completed_or_started_projects": [
                dict(item)
                for item in list(meta.get("created_projects") or [])[-50:]
                if isinstance(item, dict)
            ],
            "followup_parent_project_id": meta.get("followup_parent_project_id"),
            "followup_parent_project_key": meta.get("followup_parent_project_key"),
            "policy": (
                "Earlier projects are immutable. The current turn may define "
                "a separately confirmed continuation, additional batch, or "
                "revised version without overwriting prior deliverables."
            ),
        },
        "current_authoritative_script_message_id": (
            int(meta["authoritative_script_message_id"])
            if meta.get("authoritative_script_message_id") is not None
            else None
        ),
        "current_authoritative_script": current_script,
        "authoritative_source_texts": authoritative_sources,
        "authoritative_attachment_texts": attachment_texts,
        "conversation": conversation_rows,
        "context_manifest": {
            "messages_included": len(conversation_rows),
            "whole_old_message_ids_omitted": sorted(omitted_ids),
            "whole_source_text_ids_omitted": sorted(omitted_source_ids),
            "individual_messages_truncated": False,
            "attachment_text_characters_included": attachment_text_chars,
        },
        "selected_product": product_context,
        "available_products": catalog,
        "video_model_catalog": video_models,
        "user_attachments": [
            {
                "attachment_key": row.attachment_key,
                "kind": row.kind,
                "original_name": row.original_name,
                "mime_type": row.mime_type,
                "size_bytes": int(row.size_bytes or 0),
                "analysis_status": row.analysis_status,
                "technical_summary": _compact({
                    key: value for key, value in dict(row.analysis_json or {}).items()
                    if key not in {"document_text", "contact_sheet_paths"}
                }),
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


def _proposal_digest(
    proposal: ContentProducerProposal,
    *,
    intent_spec: ContentProducerIntentSpec | None = None,
    authoritative_script_sha256: str | None = None,
) -> str:
    payload = json.dumps(
        {
            "proposal": proposal.model_dump(mode="json"),
            "intent_spec": (
                intent_spec.model_dump(mode="json")
                if intent_spec is not None
                else None
            ),
            "authoritative_script_sha256": (
                str(authoritative_script_sha256 or "").strip() or None
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _intent_spec_from_meta(meta: dict[str, Any]) -> ContentProducerIntentSpec | None:
    value = meta.get("intent_spec")
    if not isinstance(value, dict):
        value = meta.get("baseline_intent_spec")
    if not isinstance(value, dict):
        return None
    try:
        return ContentProducerIntentSpec.model_validate(value)
    except ValidationError:
        return None


def _current_script_sha256_from_meta(meta: dict[str, Any]) -> str | None:
    current_version = int(meta.get("authoritative_script_current_version") or 1)
    for item in reversed(list(meta.get("authoritative_script_versions") or [])):
        if not isinstance(item, dict):
            continue
        if int(item.get("version") or 0) == current_version:
            value = str(item.get("sha256") or "").strip()
            if value:
                return value
    return None


def _proposal_digest_from_meta(
    proposal: ContentProducerProposal,
    meta: dict[str, Any],
) -> str:
    # Prompt revisions change how the Producer reasons and writes, not the
    # integrity schema of an already-reviewed proposal.  Coupling digest
    # selection to PRODUCER_PROMPT_VERSION made an ordinary prompt deploy
    # reinterpret the same persisted proposal with a different hash algorithm
    # and reject an explicit user confirmation as stale.  Validate the saved
    # structured proposal, signed intent and authoritative script uniformly;
    # Pydantic validation remains the schema boundary.
    return _proposal_digest(
        proposal,
        intent_spec=_intent_spec_from_meta(meta),
        authoritative_script_sha256=_current_script_sha256_from_meta(meta),
    )


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


def _semantic_review_instructions() -> str:
    return """
You are the independent semantic-consistency reviewer for a short-video
Creative Producer. Return exactly one JSON object with verdict, issues and
reviewed_intent_spec.

The original packet contains chronological user messages and the candidate
Producer decision. Audit only the effective user intent and production
handoff. You cannot invent a creative requirement, product fact, promotion,
script change or source permission.

Rules:
- Later explicit user instructions take precedence over incompatible earlier
  instructions. They need not contain the word cancel. A later request for
  several differentiated original videos may broaden an earlier single-change
  experiment; keep an older preservation rule only on the deliverable where it
  remains compatible.
- Reconcile the proposal, requirements, deliverables and
  transformation_contract as one semantic whole. protected_requirements,
  authorized_changes,
  creative_freedom, excluded_source_artifacts, fidelity, execution_strategy,
  transfer_mode and source_media_reuse must be mutually compatible.
- Global protection must not contradict a deliverable-specific authorized
  change. Re-scope it to the applicable deliverable through requirements and
  deliverable must_preserve fields, or remove it when later user chronology
  superseded it.
- Preserve every still-valid user requirement, exact evidence quote, stable
  requirement ID, locked script, proposal field and product boundary. In
  particular, product_use_mode must agree with what the requirements and
  deliverables permit: context_only cannot create product appearance or
  conversion gates, while required cannot coexist with a project-wide
  prohibition on showing, naming or selling the product. Make the smallest
  semantic correction. Do not rewrite for style.
- Judge pacing, spoken density, hook intensity and visual energy from the full
  conversational meaning, not by matching isolated words. Fast visual cutting
  may intentionally coexist with sparse narration, while a user asking for
  fast or information-dense spoken delivery must not be silently converted to
  sparse copy. Reconcile these fields semantically or ask one grouped question
  only when the chronology leaves a real ambiguity.
- If the candidate is coherent, verdict=pass, issues=[], and return it exactly
  as reviewed_intent_spec and reviewed_proposal. If chronology resolves a
  contradiction, verdict=revised and return the complete corrected intent_spec
  and proposal. If a real ambiguity remains, verdict=needs_input, both reviewed
  values are null, and put one concise grouped question in Simplified Chinese
  in assistant_question.
- Output exactly these top-level fields: verdict, issues,
  reviewed_intent_spec, reviewed_proposal, assistant_question. The
  reviewed_intent_spec must match
  the supplied candidate_intent_spec shape: delivery_mode,
  source_material_mode, user_goal, intent_manifest and deliverables.
- Use only the supplied schema enums. Never invent per-deliverable enum values.
  When all outputs are newly generated media, keep execution_strategy as
  full_regeneration and source_media_reuse as forbidden; express a
  deliverable-specific semantic story or setting lock in that deliverable's
  must_preserve list, not in execution_strategy or source_media_reuse.
""".strip()


def _normalize_semantic_review_payload(
    payload: dict[str, Any],
    *,
    candidate_intent_spec: ContentProducerIntentSpec,
    candidate_proposal: ContentProducerProposal,
) -> dict[str, Any]:
    """Fence reviewer output to the runtime's generic execution vocabulary.

    The semantic reviewer may correctly describe a mixed deliverable scope but
    accidentally encode that prose inside one of the small operational enums.
    It has no authority to add provider execution modes. Preserve the
    candidate's already-valid operational value while retaining the reviewer's
    semantic requirement and deliverable corrections.
    """

    normalized = copy.deepcopy(payload)
    if (
        normalized.get("verdict") in {"pass", "revised"}
        and not isinstance(normalized.get("reviewed_proposal"), dict)
    ):
        # Accept one response produced during a rolling deploy under the prior
        # reviewer schema without allowing it to erase the validated proposal.
        normalized["reviewed_proposal"] = candidate_proposal.model_dump(
            mode="json"
        )
    reviewed = normalized.get("reviewed_intent_spec")
    if not isinstance(reviewed, dict):
        return normalized
    manifest = reviewed.get("intent_manifest")
    contract = (
        manifest.get("transformation_contract")
        if isinstance(manifest, dict)
        else None
    )
    candidate_contract = (
        candidate_intent_spec.intent_manifest.transformation_contract
    )
    if isinstance(contract, dict) and candidate_contract is not None:
        allowed = {
            "fidelity": {
                "inspiration",
                "adaptive",
                "exact_outside_authorized_changes",
                "exact",
            },
            "execution_strategy": {
                "director_decides",
                "local_edit",
                "selective_regeneration",
                "full_regeneration",
            },
            "transfer_mode": {
                "inspiration_only",
                "semantic_structure",
                "selective_elements",
                "source_media",
            },
            "source_media_reuse": {
                "director_decides",
                "forbidden",
                "allowed",
                "required",
            },
        }
        candidate_values = candidate_contract.model_dump(mode="json")
        for field, accepted in allowed.items():
            if contract.get(field) not in accepted:
                contract[field] = candidate_values[field]
    decision_wrapper = _normalize_decision_payload(
        {"intent_spec": reviewed}
    )
    normalized["reviewed_intent_spec"] = decision_wrapper["intent_spec"]
    return normalized


async def _semantic_review_decision(
    *,
    client: HermesContentProducerClient,
    packet: dict[str, Any],
    decision: ContentProducerDecision,
    idempotency_base: str,
    conversation_scope: str,
    validate_reviewed_decision: (
        Callable[[ContentProducerDecision], None] | None
    ) = None,
) -> tuple[ContentProducerSemanticReview, int]:
    """Run one isolated AI semantic audit before a source project is confirmable."""

    review_packet = {
        "operation": "content_producer_semantic_consistency_review",
        "prompt_version": PRODUCER_PROMPT_VERSION,
        "chronological_user_messages": [
            item
            for item in list(packet.get("conversation") or [])
            if isinstance(item, dict) and item.get("role") == "user"
        ],
        "authoritative_attachment_texts": list(
            packet.get("authoritative_attachment_texts") or []
        ),
        "selected_product": packet.get("selected_product"),
        "proposal_contract": (
            {
                "video_count": decision.proposal.video_count,
                "duration_min_seconds": (
                    decision.proposal.video_duration_min_seconds
                ),
                "duration_max_seconds": (
                    decision.proposal.video_duration_max_seconds
                ),
                "content_mode": decision.proposal.content_mode,
                "product_use_mode": decision.proposal.product_use_mode,
            }
            if decision.proposal is not None
            else None
        ),
        "candidate_intent_spec": (
            decision.intent_spec.model_dump(mode="json")
            if decision.intent_spec is not None
            else None
        ),
        "candidate_proposal": (
            decision.proposal.model_dump(mode="json")
            if decision.proposal is not None
            else None
        ),
    }
    validation_error: str | None = None
    raw_text = ""
    total_latency_ms = 0
    for attempt in range(2):
        input_payload: dict[str, Any] = review_packet
        if attempt and validation_error:
            input_payload = {
                "original_review_packet": review_packet,
                "invalid_response": raw_text[:12000],
                "validation_error": validation_error[:4000],
                "repair_instruction": (
                    "Return a corrected semantic-review JSON object only. "
                    "Do not add facts or requirements."
                ),
            }
        input_text = json.dumps(
            input_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response, latency_ms = await client.create_response(
            input_text=input_text,
            input_items=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": input_text}
                    ],
                }
            ],
            instructions=_semantic_review_instructions(),
            metadata={
                "prompt_version": PRODUCER_PROMPT_VERSION,
                "review_type": "semantic_consistency",
            },
            idempotency_key=(
                f"{idempotency_base}:semantic-review:attempt:{attempt + 1}"
            ),
        )
        total_latency_ms += int(latency_ms)
        raw_text = extract_output_text(response)
        try:
            review = ContentProducerSemanticReview.model_validate(
                _normalize_semantic_review_payload(
                    _json_object(raw_text),
                    candidate_intent_spec=decision.intent_spec,
                    candidate_proposal=decision.proposal,
                )
            )
            if (
                validate_reviewed_decision is not None
                and review.verdict in {"pass", "revised"}
            ):
                validate_reviewed_decision(
                    decision.model_copy(
                        update={
                            "intent_spec": review.reviewed_intent_spec,
                            "proposal": review.reviewed_proposal,
                        }
                    )
                )
            return review, total_latency_ms
        except (ValueError, ValidationError) as exc:
            validation_error = str(exc)
    raise APIError(
        "CONTENT_PRODUCER_SEMANTIC_REVIEW_INVALID",
        "The AI producer could not reconcile the project requirements. "
        "Please retry the latest message.",
        502,
        {"validation_error": str(validation_error or "unknown")[:1000]},
    )


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
        begin_producer_followup(conversation)
        meta = dict(conversation.meta_json or {})
    # A prompt deploy must not erase a proposal the user has already reviewed.
    # The proposal model, signed intent manifest and stored digest below are
    # the durable validation boundary; prompt_version is audit provenance only.
    if product_selection_explicit and product_id is None:
        # A deliberate "no product" selection must clear an earlier choice.
        # Otherwise an old product could silently return at confirmation time.
        meta.pop("selected_product_id", None)
        meta.pop("proposal", None)
        meta.pop("proposal_sha256", None)
        meta.pop("proposal_prompt_version", None)
        meta.pop("intent_spec", None)
        meta.pop("pending_decision_id", None)
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
            meta.pop("intent_spec", None)
            meta.pop("pending_decision_id", None)
        meta["selected_product_id"] = int(selected.id)
    prior_proposal: ContentProducerProposal | None = None
    prior_proposal_value = meta.get("proposal")
    if not isinstance(prior_proposal_value, dict):
        prior_proposal_value = meta.get("baseline_proposal")
    if isinstance(prior_proposal_value, dict):
        try:
            prior_proposal = ContentProducerProposal.model_validate(
                prior_proposal_value
            )
        except ValidationError:
            meta.pop("proposal", None)
            meta.pop("baseline_proposal", None)
            meta.pop("proposal_sha256", None)
            meta.pop("proposal_prompt_version", None)
            meta.pop("intent_spec", None)
            meta.pop("pending_decision_id", None)
    prior_intent_spec = _intent_spec_from_meta(meta)
    meta["session_key"] = normalized_session
    conversation.meta_json = meta
    normalized_turn_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(client_turn_id or ""))[:32]
    if not normalized_turn_id:
        normalized_turn_id = uuid4().hex
    processing_attachments = [
        row.original_name
        for row in _active_attachment_rows(db, conversation=conversation)
        if row.analysis_status in {"queued", "processing"}
    ]
    if processing_attachments:
        raise APIError(
            "CONTENT_PRODUCER_ATTACHMENTS_PROCESSING",
            "Wait for the uploaded reference video analysis before continuing the conversation.",
            409,
            {"attachments": processing_attachments[:5]},
        )
    failed_benchmarks = [
        row.original_name
        for row in _active_attachment_rows(db, conversation=conversation)
        if row.kind == "reference_video"
        and (
            row.analysis_status == "failed"
            or str(dict(row.analysis_json or {}).get("multimodal_status") or "")
            == "failed"
        )
    ]
    if failed_benchmarks:
        raise APIError(
            "CONTENT_PRODUCER_BENCHMARK_ANALYSIS_FAILED",
            "The benchmark video could not complete multimodal analysis. Retry or replace it before continuing.",
            409,
            {"attachments": failed_benchmarks[:5]},
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
    attachments = _active_attachment_rows(db, conversation=conversation)
    client = HermesContentProducerClient()
    scope_digest = hashlib.sha256(
        f"{workspace_id}:{user_id}:{conversation.id}:{normalized_session}".encode("utf-8")
    ).hexdigest()
    # GMV's scoped SQL conversation and the packet above are the Producer's
    # durable memory.  Do not also append every 100k-character packet to one
    # long-lived Hermes response chain: that duplicates state, triggers context
    # compaction, and can turn one front-desk reply into many sequential model
    # calls.  Each validation attempt therefore gets a fresh upstream chain;
    # retries remain idempotent through idempotency_base below.
    hermes_turn_scope = (
        f"gmv-cf-producer-{scope_digest[:24]}-turn-{int(user_message.id)}"
    )
    idempotency_base = (
        f"content-producer:{workspace_id}:{user_id}:{conversation.id}:"
        f"{user_message.id}:{PRODUCER_PROMPT_VERSION}"
    )
    validation_error: str | None = None
    raw_text = ""
    decision: ContentProducerDecision | None = None
    semantic_review: ContentProducerSemanticReview | None = None
    selection_regrounds = 0
    for attempt in range(3):
        packet_json = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
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
            idempotency_key=f"{idempotency_base}:attempt:{attempt + 1}",
        )
        raw_text = extract_output_text(response)
        try:
            candidate = ContentProducerDecision.model_validate(
                _normalize_decision_payload(_json_object(raw_text))
            )
            if candidate.intent_spec is None and prior_intent_spec is not None:
                candidate = candidate.model_copy(
                    update={"intent_spec": prior_intent_spec}
                )
            if candidate.proposal is not None:
                candidate = _reconcile_proposal_delta(
                    candidate,
                    prior_proposal=prior_proposal,
                    latest_user_message=normalized_message,
                )
                candidate = candidate.model_copy(
                    update={
                        "proposal": _strip_unverified_promotion_evidence(
                            candidate.proposal,
                            latest_user_message=normalized_message,
                            prior_proposal=prior_proposal,
                        )
                    }
                )
                _validate_promotion_authorization(
                    candidate.proposal,
                    latest_user_message=normalized_message,
                    prior_proposal=prior_proposal,
                )
            _validate_script_decision(
                db,
                conversation=conversation,
                decision=candidate,
                latest_user_message=normalized_message,
            )

            selection = candidate.product_selection
            evidence = str(selection.evidence_quote or "").strip()
            if selection.action in {"select", "clear"} and evidence not in normalized_message:
                raise ValueError(
                    "product selection evidence must be an exact quote from latest_user_message"
                )
            requested_product: HermesContentProduct | None = selected
            selection_changed = False
            if selection.action == "select":
                requested_product = _selected_product(
                    db,
                    workspace_id=workspace_id,
                    product_id=selection.product_id,
                )
                selection_changed = (
                    selected is None or int(selected.id) != int(requested_product.id)
                )
            elif selection.action == "clear":
                requested_product = None
                selection_changed = selected is not None

            if selection_changed:
                selection_regrounds += 1
                if selection_regrounds > 1:
                    raise ValueError(
                        "product selection changed more than once in one user turn"
                    )
                selected = requested_product
                if selected is None:
                    meta.pop("selected_product_id", None)
                else:
                    meta["selected_product_id"] = int(selected.id)
                meta.pop("proposal", None)
                meta.pop("proposal_sha256", None)
                meta.pop("proposal_prompt_version", None)
                meta.pop("intent_spec", None)
                meta.pop("pending_decision_id", None)
                prior_proposal = None
                prior_intent_spec = None
                conversation.meta_json = dict(meta)
                packet = _packet(
                    db,
                    conversation=conversation,
                    selected_product=selected,
                )
                validation_error = None
                raw_text = ""
                continue

            decision = candidate
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

    requires_semantic_review = bool(
        decision.intent_spec is not None
        and decision.proposal is not None
        # A natural-language catalog choice already consumed one independent
        # AI interpretation followed by a second, product-grounded full
        # rewrite.  A third same-role review duplicates that work and can push
        # a normal front-desk reply beyond the HTTP wall-clock budget.  The
        # grounded second pass still undergoes the complete typed validators.
        and selection_regrounds == 0
    )
    if requires_semantic_review:
        def validate_reviewed_candidate(
            candidate: ContentProducerDecision,
        ) -> None:
            if candidate.proposal is not None:
                _validate_promotion_authorization(
                    candidate.proposal,
                    latest_user_message=normalized_message,
                    prior_proposal=prior_proposal,
                )
            _validate_script_decision(
                db,
                conversation=conversation,
                decision=candidate,
                latest_user_message=normalized_message,
            )

        semantic_review, review_latency_ms = await _semantic_review_decision(
            client=client,
            packet=packet,
            decision=decision,
            idempotency_base=idempotency_base,
            conversation_scope=hermes_turn_scope,
            validate_reviewed_decision=validate_reviewed_candidate,
        )
        latency_ms = int(latency_ms) + int(review_latency_ms)
        if semantic_review.verdict == "needs_input":
            question = str(
                semantic_review.assistant_question or ""
            ).strip()
            decision = decision.model_copy(
                update={
                    "status": "needs_input",
                    "assistant_message": question,
                    "missing_information": [question],
                    "proposal": None,
                    "intent_spec": None,
                }
            )
        else:
            decision = decision.model_copy(
                update={
                    "intent_spec": semantic_review.reviewed_intent_spec,
                    "proposal": semantic_review.reviewed_proposal,
                }
            )
    if decision.proposal is not None:
        decision = decision.model_copy(
            update={
                "proposal": _strip_unverified_promotion_evidence(
                    decision.proposal,
                    latest_user_message=normalized_message,
                    prior_proposal=prior_proposal,
                )
            }
        )
        try:
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
        except (TypeError, ValueError, ValidationError) as exc:
            # A model response that survives both bounded author repair and
            # semantic-review repair must never escape as an opaque HTTP 500.
            # Preserve the user turn and expose a retryable model-contract
            # failure without creating or authorizing media.
            raise APIError(
                "CONTENT_PRODUCER_REVIEWED_DECISION_INVALID",
                "The AI producer could not return a structurally valid final "
                "handoff after bounded semantic repair. Retry this message.",
                502,
                {"validation_error": str(exc)[:1000]},
            ) from exc
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
    if active_script_id is not None:
        versions = [
            dict(item)
            for item in list(meta.get("authoritative_script_versions") or [])
            if isinstance(item, dict) and item.get("version")
        ]
        if not versions:
            source_row = _script_source_row(
                db,
                conversation=conversation,
                message_id=active_script_id,
            )
            source_text = (
                str(source_row.content_text or "").strip()
                if source_row is not None
                else ""
            )
            if source_text:
                versions.append({
                    "version": 1,
                    "source_message_id": active_script_id,
                    "sha256": hashlib.sha256(
                        source_text.encode("utf-8")
                    ).hexdigest(),
                    "text": source_text,
                    "revised": False,
                    "prompt_version": PRODUCER_PROMPT_VERSION,
                })
                meta["authoritative_script_versions"] = versions
                meta["authoritative_script_current_version"] = 1
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
    intent_spec = decision.intent_spec
    proposal_sha256 = (
        _proposal_digest(
            decision.proposal,
            intent_spec=intent_spec,
            authoritative_script_sha256=_current_script_sha256_from_meta(meta),
        )
        if decision.proposal is not None
        else None
    )
    pending_decision_id = (
        f"producer-{proposal_sha256[:40]}"
        if decision.status == "proposal_ready" and proposal_sha256
        else None
    )
    decision = decision.model_copy(
        update={"pending_decision_id": pending_decision_id}
    )
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
            "intent_spec": (
                intent_spec.model_dump(mode="json")
                if intent_spec is not None
                else None
            ),
            "proposal_sha256": proposal_sha256,
            "pending_decision_id": pending_decision_id,
            "last_latency_ms": int(latency_ms),
            "semantic_review": (
                {
                    "verdict": semantic_review.verdict,
                    "issues": list(semantic_review.issues),
                    "reviewed_at": datetime.now(timezone.utc)
                    .replace(tzinfo=None)
                    .isoformat(),
                    "prompt_version": PRODUCER_PROMPT_VERSION,
                }
                if semantic_review is not None
                else None
            ),
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


def compile_confirmed_creative_copy_contract(
    *,
    intent_spec: dict[str, Any],
    authoritative_script: tuple[int, str] | None,
    authoritative_script_version: int = 1,
) -> dict[str, Any]:
    """Compile copy authority from the reviewed semantic intent.

    ``source_material_mode`` is an AI-authored semantic coordinate, not a
    filename category.  In particular, ``reference_copy`` means that the
    supplied text informs the new work; it does not silently regain verbatim
    authority merely because the conversation also retains an authoritative
    source-text pointer for provenance.  The signed intent manifest continues
    to carry the exact preservation, transformation, and success criteria to
    the Director and Critic.

    This compiler deliberately makes no creative decision.  It only translates
    the Producer's already-reviewed authority decision into the runtime's copy
    envelope so a later deterministic preflight cannot contradict the model.
    """

    intent = ContentProducerIntentSpec.model_validate(intent_spec)
    delivery_mode = intent.delivery_mode
    source_material_mode = intent.source_material_mode
    deliverable_voiceovers: list[dict[str, Any]] = []
    for item in intent.deliverables:
        script_text = str(item.script_text or "").strip()
        if not script_text:
            continue
        deliverable_voiceovers.append({
            "deliverable_ordinal": int(item.ordinal),
            "label": str(item.label)[:255],
            "objective": str(item.objective)[:1000],
            "text": script_text,
            "sha256": hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
            "target_duration_seconds": item.target_duration_seconds,
            "must_preserve": list(item.must_preserve)[:32],
            "differentiation": list(item.differentiation)[:32],
            "source_message_id": item.source_message_id,
        })

    user_locked_deliverables = bool(
        deliverable_voiceovers
        and source_material_mode == "multi_script_package"
        and all(
            int(item.get("source_message_id") or 0) > 0
            for item in deliverable_voiceovers
        )
    )
    if user_locked_deliverables:
        return {
            "required_verbatim_voiceovers": deliverable_voiceovers,
            "script_reuse_mode": "distinct_per_deliverable",
            "copy_authority": "user_verbatim",
        }
    if deliverable_voiceovers:
        return {
            "director_seed_voiceovers": deliverable_voiceovers,
            "copy_authority": "producer_draft_editable",
        }
    if authoritative_script is None:
        return {}

    script_message_id, script_text = authoritative_script
    script_text = str(script_text or "").strip()
    if not script_text:
        return {}
    source_payload = {
        "text": script_text,
        "source_message_id": int(script_message_id),
        "sha256": hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
        "source_version": max(1, int(authoritative_script_version or 1)),
    }

    transformation = intent.intent_manifest.transformation_contract
    source_copy_is_verbatim = bool(
        source_material_mode == "single_script"
        and (
            transformation is None
            or transformation.fidelity
            in {"exact", "exact_outside_authorized_changes"}
        )
    )
    source_copy_is_editable = bool(
        source_material_mode in {"reference_copy", "single_script"}
        and not source_copy_is_verbatim
    )

    if source_copy_is_editable:
        # Independent outputs each receive the same source as an editable
        # reference seed.  Their distinct objectives and differentiation axes
        # remain attached, and the per-deliverable Director is free to rewrite
        # the wording under the signed transformation manifest.
        if delivery_mode in {"independent_videos", "series_episodes"}:
            seeds = []
            for item in intent.deliverables:
                seeds.append({
                    **source_payload,
                    "deliverable_ordinal": int(item.ordinal),
                    "label": str(item.label)[:255],
                    "objective": str(item.objective)[:1000],
                    "target_duration_seconds": item.target_duration_seconds,
                    "must_preserve": list(item.must_preserve)[:32],
                    "differentiation": list(item.differentiation)[:32],
                })
            return {
                "director_seed_voiceovers": seeds,
                "copy_authority": "producer_draft_editable",
                "source_copy_role": (
                    "semantic_reference"
                    if source_material_mode == "reference_copy"
                    else "editable_source_script"
                ),
            }
        return {
            "director_seed_voiceover": source_payload,
            "copy_authority": "producer_draft_editable",
            "source_copy_role": (
                "semantic_reference"
                if source_material_mode == "reference_copy"
                else "editable_source_script"
            ),
        }

    if source_copy_is_verbatim:
        return {
            "required_verbatim_voiceover": script_text,
            "source_message_id": int(script_message_id),
            "source_sha256": source_payload["sha256"],
            "source_version": source_payload["source_version"],
            "script_reuse_mode": (
                "same_copy_visual_variants"
                if delivery_mode == "visual_variants"
                else "single"
            ),
            "copy_authority": "user_verbatim",
        }

    # A stale source pointer may remain for provenance after the Producer has
    # reclassified the material as requirements or no source copy.  It must not
    # leak back into audience-facing copy authority.
    return {}


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
    if _proposal_digest_from_meta(proposal, meta) != str(
        meta.get("proposal_sha256") or ""
    ):
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
    "begin_producer_followup",
    "compile_confirmed_creative_copy_contract",
    "copy_producer_attachments_to_project",
    "confirmed_project_parameters",
    "delete_producer_attachment",
    "get_or_create_producer_conversation",
    "producer_attachment_out",
    "producer_attachments",
    "producer_session",
    "run_producer_turn",
    "save_producer_attachment",
    "stage_producer_reference_link",
]
