from __future__ import annotations

import asyncio

from app.celery_app import celery_app
from app.data.db import SessionLocal
from app.data.models.kie_api import KieApiKey
from app.services.ai_routing.catalog import PROVIDER_TRANSPORTS
from app.services.ai_routing.discovery import (
    AiModelDiscoveryError,
    discover_models_for_key,
    discover_models_for_provider,
    ensure_builtin_routes,
)
from app.services.ai_routing.overview import attempt_retention_cleanup


@celery_app.task(name="ai_provider.discover_key", queue="gmv.tasks.default")
def discover_key_models(*, key_id: int) -> dict:
    with SessionLocal() as db:
        key = db.get(KieApiKey, int(key_id))
        if key is None:
            return {"key_id": int(key_id), "skipped": True, "reason": "KEY_NOT_FOUND"}
        try:
            result = asyncio.run(discover_models_for_key(db, key=key))
        except AiModelDiscoveryError as exc:
            db.rollback()
            return {"key_id": int(key_id), "provider_key": key.provider_key, "ok": False, "error": str(exc)[:500]}
        db.commit()
        result.pop("_model_ids", None)
        return {**result, "ok": True}


@celery_app.task(name="ai_provider.discover_all", queue="gmv.tasks.default")
def discover_all_models() -> dict:
    results: list[dict] = []
    with SessionLocal() as db:
        ensure_builtin_routes(db)
        db.commit()
        for provider_key, spec in PROVIDER_TRANSPORTS.items():
            if not spec.automatic_discovery:
                continue
            try:
                result = asyncio.run(
                    discover_models_for_provider(
                        db,
                        provider_key=provider_key,
                        automatic_only=True,
                    )
                )
                db.commit()
            except AiModelDiscoveryError as exc:
                db.rollback()
                result = {"provider_key": provider_key, "ok": False, "error": str(exc)[:500]}
            results.append(result)
        deleted_attempts = attempt_retention_cleanup(db, keep_days=30)
        db.commit()
    return {"providers": results, "deleted_attempts": deleted_attempts}


__all__ = ["discover_all_models", "discover_key_models"]
