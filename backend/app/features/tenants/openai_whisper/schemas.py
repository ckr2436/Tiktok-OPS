"""Pydantic schemas for the OpenAI Whisper tenant APIs."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

from .languages import get_language_label


class LanguageOption(BaseModel):
    code: str
    name: str


class LanguageListResponse(BaseModel):
    languages: List[LanguageOption]


MediaStatus = Literal["pending", "processing", "success", "failed", "skipped"]


class UploadedVideoResponse(BaseModel):
    upload_id: str
    filename: str
    size: int
    content_type: Optional[str] = None


class TranscriptionSegment(BaseModel):
    index: int
    start: float
    end: float
    text: str


class TranscriptionJob(BaseModel):
    job_id: str
    workspace_id: int
    status: Literal["pending", "processing", "success", "failed"]
    error: Optional[str] = None
    subtitle_error: Optional[str] = None
    contact_sheet_error: Optional[str] = None
    download_error: Optional[str] = None
    filename: Optional[str] = None
    size: Optional[int] = None
    content_type: Optional[str] = None
    source_language: Optional[str] = None
    detected_language: Optional[str] = None
    target_language: Optional[str] = None
    translation_language: Optional[str] = None
    translate: bool = False
    show_bilingual: bool = False
    do_subtitle: bool = True
    do_contact_sheet: bool = False
    do_download_only: bool = False
    contact_interval: Optional[float] = None
    subtitle_status: MediaStatus = "pending"
    contact_sheet_status: MediaStatus = "skipped"
    download_status: MediaStatus = "skipped"
    contact_sheet_url: Optional[str] = None
    download_url: Optional[str] = None
    share_url: Optional[str] = None
    celery_task_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    segments: Optional[List[TranscriptionSegment]] = None
    translation_segments: Optional[List[TranscriptionSegment]] = None

    @classmethod
    def from_metadata(cls, meta: Dict[str, Any]) -> "TranscriptionJob":
        result = meta.get("result") or {}
        segments = result.get("segments")
        translation_segments = result.get("translation_segments")
        return cls.model_validate(
            {
                "job_id": meta.get("job_id"),
                "workspace_id": meta.get("workspace_id"),
                "status": meta.get("status", "pending"),
                "error": meta.get("error"),
                "subtitle_error": meta.get("subtitle_error"),
                "contact_sheet_error": meta.get("contact_sheet_error"),
                "download_error": meta.get("download_error"),
                "filename": meta.get("filename"),
                "size": meta.get("size"),
                "content_type": meta.get("content_type"),
                "source_language": meta.get("source_language"),
                "detected_language": result.get("detected_language")
                or meta.get("source_language"),
                "target_language": meta.get("target_language"),
                "translation_language": result.get("translation_language"),
                "translate": bool(meta.get("translate")),
                "show_bilingual": bool(meta.get("show_bilingual")),
                "do_subtitle": bool(meta.get("do_subtitle", True)),
                "do_contact_sheet": bool(meta.get("do_contact_sheet", False)),
                "do_download_only": bool(meta.get("do_download_only", False)),
                "contact_interval": meta.get("contact_interval"),
                "subtitle_status": meta.get("subtitle_status", "pending"),
                "contact_sheet_status": meta.get("contact_sheet_status", "skipped"),
                "download_status": meta.get("download_status", "skipped"),
                "contact_sheet_url": meta.get("contact_sheet_url"),
                "download_url": meta.get("download_url"),
                "share_url": meta.get("share_url"),
                "celery_task_id": meta.get("celery_task_id"),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "started_at": meta.get("started_at"),
                "completed_at": meta.get("completed_at"),
                "segments": segments,
                "translation_segments": translation_segments,
            }
        )

    def source_language_label(self) -> Optional[str]:
        return get_language_label(self.source_language)

    def translation_language_label(self) -> Optional[str]:
        return get_language_label(self.translation_language)


class TranscriptionJobCreatedResponse(TranscriptionJob):
    pass


class TranscriptionJobStatusResponse(TranscriptionJob):
    pass


class TranscriptionJobSummary(BaseModel):
    job_id: str
    filename: Optional[str] = None
    status: Literal["pending", "processing", "success", "failed"]
    error: Optional[str] = None
    translate: bool = False
    show_bilingual: bool = False
    source_language: Optional[str] = None
    detected_language: Optional[str] = None
    target_language: Optional[str] = None
    translation_language: Optional[str] = None
    do_subtitle: bool = True
    do_contact_sheet: bool = False
    do_download_only: bool = False
    subtitle_status: MediaStatus = "pending"
    contact_sheet_status: MediaStatus = "skipped"
    download_status: MediaStatus = "skipped"
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TranscriptionJobListResponse(BaseModel):
    jobs: List[TranscriptionJobSummary]

