"""FastAPI router exposing the tenant level Whisper APIs."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.deps import SessionUser, require_tenant_member
from app.data.db import get_db

from . import service
from .schemas import (
    LanguageListResponse,
    TranscriptionJobCreatedResponse,
    TranscriptionJobListResponse,
    TranscriptionJobStatusResponse,
    UploadedVideoResponse,
)

router = APIRouter(prefix="/api/v1/tenants/{workspace_id}/openai-whisper", tags=["Tenant / openai-whisper"])


@router.get("/languages", response_model=LanguageListResponse)
def list_languages(workspace_id: int, _: SessionUser = Depends(require_tenant_member)):
    del workspace_id  # workspace_scope already enforced by dependency
    return service.get_languages()


@router.post("/uploads", response_model=UploadedVideoResponse)
async def upload_video(
    workspace_id: int,
    file: UploadFile = File(...),
    me: SessionUser = Depends(require_tenant_member),
):
    return await service.upload_video(
        workspace_id=workspace_id,
        user_id=me.id,
        upload=file,
    )


@router.post("/jobs", response_model=TranscriptionJobCreatedResponse)
async def enqueue_job(
    workspace_id: int,
    file: Optional[UploadFile] = File(None),
    upload_id: Optional[str] = Form(None),
    share_url: Optional[str] = Form(None),
    source_language: Optional[str] = Form(None),
    translate: bool = Form(False),
    target_language: Optional[str] = Form(None),
    show_bilingual: bool = Form(False),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return await service.create_job(
        workspace_id=workspace_id,
        user_id=me.id,
        upload=file,
        upload_id=upload_id,
        share_url=share_url,
        source_language=source_language,
        translate=translate,
        target_language=target_language,
        show_bilingual=show_bilingual,
        db=db,
    )


@router.get("/jobs", response_model=TranscriptionJobListResponse)
def list_jobs(
    workspace_id: int,
    limit: int = Query(20, ge=1, le=100),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return service.list_jobs(workspace_id, limit, db)


@router.get("/jobs/{job_id}", response_model=TranscriptionJobStatusResponse)
def get_job_status(
    workspace_id: int,
    job_id: str,
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return service.get_job(workspace_id, job_id, db)


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

