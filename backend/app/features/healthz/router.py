# app/features/healthz/router.py
from __future__ import annotations
from fastapi import APIRouter

router = APIRouter()

@router.get("/healthz")
def healthz():
    return {"ok": True}

