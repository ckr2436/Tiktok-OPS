from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

import httpx
from sqlalchemy.orm import Session

from app.data.models.ai_routing import AiModelRoute, AiProviderModel
from app.data.models.kie_api import KieApiKey
from app.services.ai_routing.catalog import (
    endpoint_modes,
    model_capabilities,
    provider_transport,
)
from app.services.ai_video.accounts import (
    COULTRA_PROVIDER_KEY,
    FLOW2API_PROVIDER_KEY,
    SUB2API_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
    VIDEO_MODEL_CATALOG,
    decrypt_api_key,
    key_scopes,
    key_model_priorities,
    key_supports_model,
    normalize_provider_key,
    provider_model_is_enabled,
    supported_models_for_provider,
)


class AiModelDiscoveryError(RuntimeError):
    pass


COULTRA_TEXT_BACKUP_PRIORITIES = {
    "gpt-5.6-terra": 20,
    "gpt-5.6-luna": 20,
}
COULTRA_TEXT_BACKUP_ALIASES = {
    "gpt-5.4-mini": ("gpt-5.4-mini-2026-03-17", 20),
}
DEFAULT_PROVIDER_PRIORITIES = {
    SUB2API_PROVIDER_KEY: 1,
    FLOW2API_PROVIDER_KEY: 5,
    TOAPIS_PROVIDER_KEY: 10,
}


def utcnow() -> datetime:
    return datetime.now()


def _safe_model_raw(item: Mapping[str, Any]) -> dict[str, Any]:
    allowed = ("id", "name", "display_name", "owned_by", "object", "type", "created")
    return {
        key: item.get(key)
        for key in allowed
        if item.get(key) is not None and not isinstance(item.get(key), (dict, list))
    }


def _model_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        source = payload.get("data") or payload.get("models") or payload.get("items") or []
    else:
        source = payload
    if not isinstance(source, list):
        raise AiModelDiscoveryError("Provider model response does not contain a list")
    result: list[dict[str, Any]] = []
    for raw in source:
        if isinstance(raw, str):
            model_id = raw.strip()
            item: dict[str, Any] = {"id": model_id}
        elif isinstance(raw, Mapping):
            item = dict(raw)
            model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
        else:
            continue
        if model_id and len(model_id) <= 191:
            item["id"] = model_id
            result.append(item)
    return result


def _route(
    db: Session,
    *,
    key: KieApiKey,
    logical_model_id: str,
    provider_model_id: str,
    capability: str,
    adapter_type: str,
    priority: int,
    enabled: bool,
    verified: bool,
    workload: str = "default",
) -> AiModelRoute:
    row = (
        db.query(AiModelRoute)
        .filter(
            AiModelRoute.key_id == int(key.id),
            AiModelRoute.workload == workload,
            AiModelRoute.logical_model_id == logical_model_id,
            AiModelRoute.provider_model_id == provider_model_id,
            AiModelRoute.capability == capability,
        )
        .one_or_none()
    )
    if row is None:
        row = AiModelRoute(
            key_id=int(key.id),
            provider_key=normalize_provider_key(key.provider_key),
            workload=workload,
            logical_model_id=logical_model_id,
            provider_model_id=provider_model_id,
            capability=capability,
            adapter_type=adapter_type,
            priority=max(1, min(9999, int(priority))),
            is_enabled=bool(enabled),
            is_verified=bool(verified),
            health_status="HEALTHY" if verified else "UNKNOWN",
        )
    else:
        row.provider_key = normalize_provider_key(key.provider_key)
        row.adapter_type = adapter_type
        # Discovery may promote a known production contract, but it never
        # overrides an operator's later priority or disabled state.
        if not row.is_verified and verified:
            row.is_verified = True
            row.health_status = "HEALTHY"
    db.add(row)
    db.flush()
    return row


def ensure_builtin_routes(db: Session, keys: Iterable[KieApiKey] | None = None) -> int:
    rows = list(keys) if keys is not None else db.query(KieApiKey).all()
    created_or_seen = 0
    for key in rows:
        provider = normalize_provider_key(key.provider_key)
        for model_id in supported_models_for_provider(provider):
            if not key_supports_model(key, model_id):
                continue
            enabled = bool(key.is_active) and provider_model_is_enabled(db, provider, model_id)
            _route(
                db,
                key=key,
                logical_model_id=model_id,
                provider_model_id=model_id,
                capability="video",
                adapter_type="legacy_video",
                priority=key_model_priorities(key).get(model_id, 100),
                enabled=enabled,
                verified=True,
            )
            created_or_seen += 1

        if provider == TOAPIS_PROVIDER_KEY and key.is_active:
            for model_id, priority in (
                ("gpt-5.4-mini", 10),
                ("gpt-5.6-terra", 10),
                ("gpt-5.6-luna", 10),
            ):
                _route(
                    db,
                    key=key,
                    logical_model_id=model_id,
                    provider_model_id=model_id,
                    capability="text",
                    adapter_type="openai_chat_completions",
                    priority=priority,
                    enabled=True,
                    verified=True,
                )
                created_or_seen += 1
            _route(
                db,
                key=key,
                logical_model_id="video-analyst-gpt-5.4-mini",
                provider_model_id="gpt-5.4-mini",
                capability="multimodal",
                adapter_type="openai_chat_completions",
                priority=10,
                enabled=True,
                verified=True,
                workload="video_analyst",
            )
            created_or_seen += 1
        if (
            provider == FLOW2API_PROVIDER_KEY
            and key.is_active
            and "image:nano_banana_pro" in set(key_scopes(key))
        ):
            # This scope is assigned only to the independent Flow2API image
            # account-pool credential. Materialize the model into the catalog
            # as every other platform AI capability, but fail closed until a
            # real image probe verifies that at least one Gemini account can
            # render. `_route` preserves an operator-enabled verified row on
            # subsequent discovery runs.
            existing = (
                db.query(AiModelRoute)
                .filter(
                    AiModelRoute.key_id == int(key.id),
                    AiModelRoute.workload == "content_factory_visual",
                    AiModelRoute.logical_model_id == "nano_banana_pro",
                    AiModelRoute.provider_model_id == "gemini-3.0-pro-image",
                    AiModelRoute.capability == "image",
                )
                .one_or_none()
            )
            _route(
                db,
                key=key,
                logical_model_id="nano_banana_pro",
                provider_model_id="gemini-3.0-pro-image",
                capability="image",
                adapter_type="flow2api_openai_images",
                priority=key_model_priorities(key).get(
                    "nano_banana_pro",
                    5,
                ),
                enabled=bool(existing and existing.is_enabled),
                verified=bool(existing and existing.is_verified),
                workload="content_factory_visual",
            )
            created_or_seen += 1
    return created_or_seen


async def discover_models_for_key(
    db: Session,
    *,
    key: KieApiKey,
    timeout_seconds: float = 30,
    mark_stale: bool = False,
) -> dict[str, Any]:
    provider = normalize_provider_key(key.provider_key)
    spec = provider_transport(provider)
    if spec is None:
        ensure_builtin_routes(db, [key])
        return {"provider_key": provider, "key_id": int(key.id), "discovered": 0, "source": "STATIC"}
    if not key.is_active:
        raise AiModelDiscoveryError("API key is disabled")
    token = decrypt_api_key(key.api_key_ciphertext)
    if not token:
        raise AiModelDiscoveryError("API key is empty")
    headers = {spec.auth_header: f"{spec.auth_prefix}{token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(base_url=spec.base_url, timeout=timeout_seconds) as client:
            response = await client.get(spec.models_path, headers=headers)
    except httpx.HTTPError as exc:
        raise AiModelDiscoveryError(f"{provider} model discovery transport error: {exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        raise AiModelDiscoveryError(f"{provider} model discovery HTTP {response.status_code}")
    try:
        items = _model_items(response.json())
    except ValueError as exc:
        raise AiModelDiscoveryError(f"{provider} model discovery returned invalid JSON") from exc
    if not items:
        raise AiModelDiscoveryError(f"{provider} returned no models")

    now = utcnow()
    seen: set[str] = set()
    for item in items:
        model_id = str(item["id"])
        seen.add(model_id)
        capabilities = model_capabilities(model_id)
        row = (
            db.query(AiProviderModel)
            .filter(
                AiProviderModel.provider_key == provider,
                AiProviderModel.provider_model_id == model_id,
            )
            .one_or_none()
        )
        if row is None:
            row = AiProviderModel(
                provider_key=provider,
                provider_model_id=model_id,
                first_seen_at=now,
            )
        row.display_name = str(item.get("display_name") or item.get("name") or model_id)[:255]
        row.capabilities_json = capabilities
        row.endpoint_modes_json = endpoint_modes(capabilities)
        row.raw_json = _safe_model_raw(item)
        row.discovery_source = "UPSTREAM"
        row.lifecycle_status = "DISCOVERED" if row.last_verified_at is None else "VERIFIED"
        row.is_available = True
        row.discovered_by_key_id = int(key.id)
        row.last_seen_at = now
        db.add(row)
        for capability in capabilities:
            if capability not in {"text", "multimodal"}:
                continue
            priority = DEFAULT_PROVIDER_PRIORITIES.get(provider, 100)
            if provider == COULTRA_PROVIDER_KEY and capability == "text":
                priority = COULTRA_TEXT_BACKUP_PRIORITIES.get(model_id, priority)
            _route(
                db,
                key=key,
                logical_model_id=model_id,
                provider_model_id=model_id,
                capability=capability,
                adapter_type="openai_chat_completions",
                priority=priority,
                enabled=False,
                verified=False,
            )
        if provider == COULTRA_PROVIDER_KEY and model_id == "gpt-5.4-mini":
            _route(
                db,
                key=key,
                logical_model_id="video-analyst-gpt-5.4-mini",
                provider_model_id=model_id,
                capability="multimodal",
                adapter_type="openai_chat_completions",
                priority=20,
                enabled=False,
                verified=False,
                workload="video_analyst",
            )

    if provider == COULTRA_PROVIDER_KEY:
        for logical_model_id, (provider_model_id, priority) in COULTRA_TEXT_BACKUP_ALIASES.items():
            if provider_model_id not in seen:
                continue
            _route(
                db,
                key=key,
                logical_model_id=logical_model_id,
                provider_model_id=provider_model_id,
                capability="text",
                adapter_type="openai_chat_completions",
                priority=priority,
                enabled=False,
                verified=False,
            )

    if provider == FLOW2API_PROVIDER_KEY:
        # Flow publishes aspect/resolution-specific image ids (for example
        # ``gemini-3.0-pro-image-portrait-2k``), while its OpenAI endpoint
        # accepts the base alias and derives the concrete variant from
        # ``generationConfig.imageConfig``. Record that alias as discovered
        # only when at least one live upstream variant advertises it.
        provider_model_id = "gemini-3.0-pro-image"
        source_models = sorted(
            model_id
            for model_id in seen
            if model_id.startswith(f"{provider_model_id}-")
        )
        if source_models and provider_model_id not in seen:
            row = (
                db.query(AiProviderModel)
                .filter(
                    AiProviderModel.provider_key == provider,
                    AiProviderModel.provider_model_id == provider_model_id,
                )
                .one_or_none()
            )
            if row is None:
                row = AiProviderModel(
                    provider_key=provider,
                    provider_model_id=provider_model_id,
                    first_seen_at=now,
                )
            row.display_name = "Gemini 3 Pro Image"
            row.capabilities_json = ["image"]
            row.endpoint_modes_json = endpoint_modes(["image"])
            row.raw_json = {
                "virtual_alias": True,
                "source_models": source_models,
            }
            row.discovery_source = "UPSTREAM_ALIAS"
            row.lifecycle_status = (
                "DISCOVERED"
                if row.last_verified_at is None
                else "VERIFIED"
            )
            row.is_available = True
            row.discovered_by_key_id = int(key.id)
            row.last_seen_at = now
            db.add(row)
            seen.add(provider_model_id)

    stale: list[AiProviderModel] = []
    if mark_stale:
        stale = (
            db.query(AiProviderModel)
            .filter(
                AiProviderModel.provider_key == provider,
                AiProviderModel.is_available.is_(True),
                ~AiProviderModel.provider_model_id.in_(seen),
            )
            .all()
        )
    for row in stale:
        row.is_available = False
        row.lifecycle_status = "UNAVAILABLE"
        db.add(row)
    ensure_builtin_routes(db, [key])
    db.flush()
    return {
        "provider_key": provider,
        "key_id": int(key.id),
        "discovered": len(seen),
        "unavailable": len(stale),
        "source": "UPSTREAM",
        "_model_ids": sorted(seen),
    }


async def discover_models_for_provider(
    db: Session,
    *,
    provider_key: str,
    automatic_only: bool = False,
) -> dict[str, Any]:
    provider = normalize_provider_key(provider_key)
    spec = provider_transport(provider)
    if automatic_only and (spec is None or not spec.automatic_discovery):
        return {"provider_key": provider, "skipped": True, "reason": "AUTOMATIC_DISCOVERY_DISABLED"}
    keys = (
        db.query(KieApiKey)
        .filter(KieApiKey.provider_key == provider, KieApiKey.is_active.is_(True))
        .order_by(KieApiKey.is_default.desc(), KieApiKey.id.asc())
        .all()
    )
    if not keys:
        return {"provider_key": provider, "skipped": True, "reason": "NO_ACTIVE_KEY"}
    discovered: set[str] = set()
    key_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for key in keys:
        try:
            item = await discover_models_for_key(db, key=key, mark_stale=False)
        except AiModelDiscoveryError as exc:
            db.rollback()
            errors.append({"key_id": int(key.id), "error": str(exc)[:500]})
            continue
        discovered.update(str(value) for value in item.pop("_model_ids", []))
        key_results.append(item)
        db.commit()
    if not key_results:
        raise AiModelDiscoveryError(f"{provider} model discovery failed for every active key")

    unavailable = 0
    # Only a complete multi-key scan may declare a provider model unavailable.
    # If one credential failed, retain the previous catalog state fail-closed.
    if not errors:
        stale_query = db.query(AiProviderModel).filter(
            AiProviderModel.provider_key == provider,
            AiProviderModel.is_available.is_(True),
        )
        if discovered:
            stale_query = stale_query.filter(~AiProviderModel.provider_model_id.in_(discovered))
        for row in stale_query.all():
            row.is_available = False
            row.lifecycle_status = "UNAVAILABLE"
            db.add(row)
            unavailable += 1
        db.flush()
    return {
        "provider_key": provider,
        "discovered": len(discovered),
        "unavailable": unavailable,
        "keys_scanned": len(key_results),
        "partial": bool(errors),
        "errors": errors,
        "source": "UPSTREAM",
    }


__all__ = [
    "AiModelDiscoveryError",
    "discover_models_for_key",
    "discover_models_for_provider",
    "ensure_builtin_routes",
]
