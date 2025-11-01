# app/features/healthz/router.py
from __future__ import annotations
from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/healthz")
def healthz():
    return {"ok": True}

