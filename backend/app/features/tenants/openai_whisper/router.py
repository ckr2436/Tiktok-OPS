"""FastAPI router exposing the tenant level Whisper APIs."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.core.deps import SessionUser, require_tenant_member

from . import service
from .schemas import (
    LanguageListResponse,
    TranscriptionJobCreatedResponse,
    TranscriptionJobStatusResponse,
)

router = APIRouter(prefix="/api/v1/tenants/{workspace_id}/openai-whisper", tags=["openai-whisper"])


@router.get("/languages", response_model=LanguageListResponse)
def list_languages(workspace_id: int, _: SessionUser = Depends(require_tenant_member)):
    del workspace_id  # workspace_scope already enforced by dependency
    return service.get_languages()


@router.post("/jobs", response_model=TranscriptionJobCreatedResponse)
async def enqueue_job(
    workspace_id: int,
    file: UploadFile = File(...),
    source_language: Optional[str] = Form(None),
    translate: bool = Form(False),
    target_language: Optional[str] = Form(None),
    show_bilingual: bool = Form(False),
    me: SessionUser = Depends(require_tenant_member),
):
    return await service.create_job(
        workspace_id=workspace_id,
        user_id=me.id,
        upload=file,
        source_language=source_language,
        translate=translate,
        target_language=target_language,
        show_bilingual=show_bilingual,
    )


@router.get("/jobs/{job_id}", response_model=TranscriptionJobStatusResponse)
def get_job_status(
    workspace_id: int,
    job_id: str,
    _: SessionUser = Depends(require_tenant_member),
):
    return service.get_job(workspace_id, job_id)


@router.get("/jobs/{job_id}/subtitles")
def download_subtitles(
    workspace_id: int,
    job_id: str,
    variant: str = "source",
    _: SessionUser = Depends(require_tenant_member),
):
    path = service.build_download(workspace_id, job_id, variant)
    filename = f"{job_id}-{variant}.srt"
    return FileResponse(path, filename=filename, media_type="text/plain")

