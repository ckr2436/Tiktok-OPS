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
    source_language: Optional[str] = None
    detected_language: Optional[str] = None
    target_language: Optional[str] = None
    translation_language: Optional[str] = None
    translate: bool = False
    show_bilingual: bool = False
    celery_task_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
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
                "source_language": meta.get("source_language"),
                "detected_language": result.get("detected_language")
                or meta.get("source_language"),
                "target_language": meta.get("target_language"),
                "translation_language": result.get("translation_language"),
                "translate": bool(meta.get("translate")),
                "show_bilingual": bool(meta.get("show_bilingual")),
                "celery_task_id": meta.get("celery_task_id"),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
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

