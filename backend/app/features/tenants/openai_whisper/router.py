"""FastAPI router exposing the tenant level Whisper APIs."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.deps import ADMIN_ROLES, SessionUser, require_tenant_member
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


def _owner_filter(me: SessionUser) -> int | None:
    """Admins manage the workspace; ordinary members can only see themselves."""
    return None if str(me.role) in ADMIN_ROLES else int(me.id)


@router.get("/languages", response_model=LanguageListResponse)
def list_languages(workspace_id: int, _: SessionUser = Depends(require_tenant_member)):
    del workspace_id
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
    do_subtitle: bool = Form(True),
    do_contact_sheet: bool = Form(False),
    contact_interval: Optional[float] = Form(None),
    do_download_only: bool = Form(False),
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
        do_subtitle=do_subtitle,
        do_contact_sheet=do_contact_sheet,
        contact_interval=contact_interval,
        do_download_only=do_download_only,
        db=db,
    )


@router.get("/jobs", response_model=TranscriptionJobListResponse)
def list_jobs(
    workspace_id: int,
    limit: int = Query(20, ge=1, le=100),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return service.list_jobs(workspace_id, limit, db, user_id=_owner_filter(me))


@router.delete("/jobs")
def clear_jobs(
    workspace_id: int,
    scope: str = Query("terminal", pattern="^(terminal|failed|success|all)$"),
    force: bool = Query(False),
    limit: int = Query(500, ge=1, le=1000),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return service.clear_jobs(
        workspace_id,
        db,
        scope=scope,
        force=force,
        limit=limit,
        user_id=_owner_filter(me),
    )


@router.get("/jobs/{job_id}", response_model=TranscriptionJobStatusResponse)
def get_job_status(
    workspace_id: int,
    job_id: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return service.get_job(workspace_id, job_id, db, user_id=_owner_filter(me))


@router.delete("/jobs/{job_id}")
def delete_job(
    workspace_id: int,
    job_id: str,
    force: bool = Query(False),
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    return service.delete_job(
        workspace_id,
        job_id,
        db,
        force=force,
        user_id=_owner_filter(me),
    )


@router.get("/jobs/{job_id}/subtitles")
def download_subtitles(
    workspace_id: int,
    job_id: str,
    variant: str = "source",
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    path = service.build_download(
        workspace_id,
        job_id,
        variant,
        db,
        user_id=_owner_filter(me),
    )
    filename = f"{job_id}-{variant}.srt"
    return FileResponse(path, filename=filename, media_type="text/plain")


@router.get("/jobs/{job_id}/contact-sheet")
def download_contact_sheet(
    workspace_id: int,
    job_id: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    path = service.build_contact_sheet_download(
        workspace_id,
        job_id,
        db,
        user_id=_owner_filter(me),
    )
    filename = f"{job_id}-contact-sheet.png"
    return FileResponse(path, filename=filename, media_type="image/png")


@router.get("/jobs/{job_id}/video")
def download_video(
    workspace_id: int,
    job_id: str,
    me: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    path, filename, content_type = service.build_video_download(
        workspace_id,
        job_id,
        db,
        user_id=_owner_filter(me),
    )
    return FileResponse(path, filename=filename, media_type=content_type)
