# app/services/ai_video/accounts.py
from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.kie_api import AiProviderModelSetting, KieApiKey
from app.data.models.ai_routing import AiModelRoute
from app.services.audit import log_event

BANDIANWA_PROVIDER_KEY = "bandianwa"
KYY_PROVIDER_KEY = "kyy"
VOLCENGINE_PROVIDER_KEY = "volcengine"
GOOGLE_GEMINI_PROVIDER_KEY = "google-gemini"
TOAPIS_PROVIDER_KEY = "toapis"
OPENROUTER_PROVIDER_KEY = "openrouter"
COULTRA_PROVIDER_KEY = "coultra"
SUB2API_PROVIDER_KEY = "sub2api"
FLOW2API_PROVIDER_KEY = "flow2api"
DOUBAO_PROVIDER_KEY = "doubao"
GLOBALAIOPC_OMNI_FLASH_PROVIDER_KEY = KYY_PROVIDER_KEY
DEFAULT_PROVIDER_KEY = BANDIANWA_PROVIDER_KEY

OMNI_FLASH_MODEL = "omni_flash"
SEEDANCE_2_0_MINI_MODEL = "seedance_2_0_mini"
GPT_IMAGE_2_MODEL = "gpt-image-2"
NANO_BANANA_PRO_MODEL = "nano_banana_pro"
VIDEO_GENERATE_SCOPE = "video:generate"

AI_PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    BANDIANWA_PROVIDER_KEY: {
        "label": "斑点蛙",
        "provider_type": "aggregator",
        "capabilities": ["image", "video"],
        "image_models": [GPT_IMAGE_2_MODEL],
    },
    KYY_PROVIDER_KEY: {
        "label": "客易云",
        "provider_type": "aggregator",
        "capabilities": ["video"],
    },
    TOAPIS_PROVIDER_KEY: {
        "label": "ToAPIs",
        "provider_type": "aggregator",
        "capabilities": ["text", "image", "video"],
        "hermes_env_var": "TOAPIS_API_KEY",
        "supports_model_discovery": True,
        "auto_discovery": True,
    },
    OPENROUTER_PROVIDER_KEY: {
        "label": "OpenRouter",
        "provider_type": "aggregator",
        "capabilities": ["text", "image", "multimodal"],
        "hermes_env_var": "OPENROUTER_API_KEY",
        "supports_model_discovery": True,
        "auto_discovery": False,
    },
    COULTRA_PROVIDER_KEY: {
        "label": "Coultra",
        "provider_type": "aggregator",
        "capabilities": ["text", "multimodal"],
        "supports_model_discovery": True,
        "auto_discovery": True,
    },
    SUB2API_PROVIDER_KEY: {
        "label": "Sub2API（自建）",
        "provider_type": "self_hosted",
        "capabilities": ["text", "image", "multimodal", "video"],
        "image_models": [GPT_IMAGE_2_MODEL],
        "supports_model_discovery": True,
        "auto_discovery": True,
    },
    FLOW2API_PROVIDER_KEY: {
        "label": "Flow2API（自建图片号池）",
        "provider_type": "self_hosted",
        "capabilities": ["image"],
        "image_models": [NANO_BANANA_PRO_MODEL],
        "supports_model_discovery": True,
        "auto_discovery": True,
    },
    GOOGLE_GEMINI_PROVIDER_KEY: {
        "label": "Google Gemini",
        "provider_type": "official",
        "capabilities": ["text", "image", "video"],
    },
    VOLCENGINE_PROVIDER_KEY: {
        "label": "火山引擎 Ark",
        "provider_type": "official",
        "capabilities": ["text", "image", "video"],
    },
    DOUBAO_PROVIDER_KEY: {
        "label": "豆包 Seedance（自建号池）",
        "provider_type": "self_hosted",
        "capabilities": ["video"],
    },
}

VIDEO_MODEL_CATALOG: dict[str, dict[str, Any]] = {
    OMNI_FLASH_MODEL: {
        "id": OMNI_FLASH_MODEL,
        "label": "Omni Flash",
        "scope": f"video:{OMNI_FLASH_MODEL}",
        "reference_image_limit": 7,
        "providers": [
            SUB2API_PROVIDER_KEY,
            BANDIANWA_PROVIDER_KEY,
            GOOGLE_GEMINI_PROVIDER_KEY,
            KYY_PROVIDER_KEY,
            TOAPIS_PROVIDER_KEY,
        ],
    },
    SEEDANCE_2_0_MINI_MODEL: {
        "id": SEEDANCE_2_0_MINI_MODEL,
        "label": "Seedance 2.0 Mini",
        "scope": f"video:{SEEDANCE_2_0_MINI_MODEL}",
        "reference_image_limit": 10,
        "providers": [DOUBAO_PROVIDER_KEY, VOLCENGINE_PROVIDER_KEY],
    },
}

PROVIDER_DEFAULT_PRIORITIES: dict[str, dict[str, int]] = {
    SUB2API_PROVIDER_KEY: {OMNI_FLASH_MODEL: 1},
    BANDIANWA_PROVIDER_KEY: {OMNI_FLASH_MODEL: 10},
    # The official Gemini route is intentionally the final Omni fallback.
    # It is operationally independent from the self-hosted Flow pool, but it
    # must not displace the lower-cost routes merely because it shares the
    # same logical model.
    GOOGLE_GEMINI_PROVIDER_KEY: {OMNI_FLASH_MODEL: 1000},
    KYY_PROVIDER_KEY: {OMNI_FLASH_MODEL: 30},
    TOAPIS_PROVIDER_KEY: {OMNI_FLASH_MODEL: 40},
    VOLCENGINE_PROVIDER_KEY: {SEEDANCE_2_0_MINI_MODEL: 10},
    DOUBAO_PROVIDER_KEY: {SEEDANCE_2_0_MINI_MODEL: 10},
}

PROVIDER_IMAGE_DEFAULT_PRIORITIES: dict[str, dict[str, int]] = {
    SUB2API_PROVIDER_KEY: {GPT_IMAGE_2_MODEL: 1},
    FLOW2API_PROVIDER_KEY: {NANO_BANANA_PRO_MODEL: 5},
    BANDIANWA_PROVIDER_KEY: {GPT_IMAGE_2_MODEL: 10},
}

PROVIDER_MODEL_DEFAULT_ENABLED: dict[str, dict[str, bool]] = {
    provider: {model: True for model in models}
    for provider, models in PROVIDER_DEFAULT_PRIORITIES.items()
}
# ToAPIs remains available to Hermes, text and image workloads, but its
# three-image Omni route is opt-in because it cannot satisfy most projects.
PROVIDER_MODEL_DEFAULT_ENABLED[TOAPIS_PROVIDER_KEY][OMNI_FLASH_MODEL] = False

PROVIDER_REFERENCE_LIMITS: dict[str, dict[str, int]] = {
    SUB2API_PROVIDER_KEY: {
        OMNI_FLASH_MODEL: max(0, int(settings.SUB2API_FLOW_REFERENCE_IMAGE_LIMIT)),
    },
    BANDIANWA_PROVIDER_KEY: {OMNI_FLASH_MODEL: 7},
    GOOGLE_GEMINI_PROVIDER_KEY: {OMNI_FLASH_MODEL: 7},
    KYY_PROVIDER_KEY: {OMNI_FLASH_MODEL: 5},
    TOAPIS_PROVIDER_KEY: {OMNI_FLASH_MODEL: 3},
    VOLCENGINE_PROVIDER_KEY: {SEEDANCE_2_0_MINI_MODEL: 9},
    DOUBAO_PROVIDER_KEY: {SEEDANCE_2_0_MINI_MODEL: 10},
}

PROVIDER_MODEL_CAPABILITIES: dict[str, dict[str, dict[str, Any]]] = {
    SUB2API_PROVIDER_KEY: {
        OMNI_FLASH_MODEL: {
            "aspect_ratios": ["9:16", "16:9"],
            "reference_video": False,
            "reference_modes": ["reference"],
            "generation_modes": [
                "text_to_video",
                "image_to_video",
                "video_to_video",
            ],
            "durations": [4, 6, 8, 10],
            "resolutions": ["720p", "1080p"],
        },
    },
    BANDIANWA_PROVIDER_KEY: {
        OMNI_FLASH_MODEL: {
            "aspect_ratios": ["9:16", "16:9", "1:1"],
            "reference_video": True,
            "reference_modes": ["reference", "first_last"],
            "generation_modes": [
                "text_to_video",
                "image_to_video",
                "video_to_video",
            ],
            "durations": [10],
        },
    },
    GOOGLE_GEMINI_PROVIDER_KEY: {
        OMNI_FLASH_MODEL: {
            "aspect_ratios": ["9:16", "16:9"],
            "reference_video": False,
            "reference_modes": ["reference"],
            "generation_modes": ["text_to_video", "image_to_video"],
            "durations": [10],
        },
    },
    KYY_PROVIDER_KEY: {
        OMNI_FLASH_MODEL: {
            "aspect_ratios": ["9:16", "16:9", "1:1"],
            "reference_video": True,
            "reference_modes": ["reference", "first_last"],
            "generation_modes": [
                "text_to_video",
                "image_to_video",
                "video_to_video",
            ],
            "durations": [10],
        },
    },
    TOAPIS_PROVIDER_KEY: {
        OMNI_FLASH_MODEL: {
            "aspect_ratios": ["9:16", "16:9"],
            "reference_video": False,
            "reference_modes": ["reference"],
            "generation_modes": ["text_to_video", "image_to_video"],
            "reference_image_counts": [0, 1, 3],
            "durations": [10],
            "resolutions": ["720p", "1080p"],
            "resolution_aspect_ratios": {"1080p": ["16:9"]},
        },
    },
    VOLCENGINE_PROVIDER_KEY: {
        SEEDANCE_2_0_MINI_MODEL: {
            "aspect_ratios": ["9:16", "16:9", "1:1"],
            "reference_video": True,
            "reference_modes": ["reference", "first_last"],
            "generation_modes": [
                "text_to_video",
                "image_to_video",
                "video_to_video",
            ],
        },
    },
    DOUBAO_PROVIDER_KEY: {
        SEEDANCE_2_0_MINI_MODEL: {
            "aspect_ratios": ["9:16", "16:9", "1:1"],
            "reference_video": False,
            "reference_modes": ["reference"],
            "generation_modes": ["text_to_video", "image_to_video"],
            "reference_image_counts": list(range(0, 11)),
            # Protocol maximum. The effective route is clamped to the account
            # pool below, so missing/legacy accounts still expose only 4-10.
            "durations": list(range(4, 16)),
            "resolutions": ["720p"],
            "prompt_max_characters": 500,
        },
    },
}

API_KEY_ENCRYPTION_PREFIX = "enc:v1:"


def normalize_provider_key(provider_key: str | None) -> str:
    key = (provider_key or DEFAULT_PROVIDER_KEY).strip().lower()
    if not key:
        key = DEFAULT_PROVIDER_KEY
    if key in {"globalaiopc", "globalaiopc-omni-flash", "kyy-ai", "keyiyun", "keyiyun-ai"}:
        key = KYY_PROVIDER_KEY
    if key in {"google", "gemini", "google-gemini-ai", "google-ai", "google_ai", "google_gemini"}:
        key = GOOGLE_GEMINI_PROVIDER_KEY
    if key in {"to-api", "to-api-s", "to_api", "to_api_s", "toapis.com"}:
        key = TOAPIS_PROVIDER_KEY
    if key in {"openroute", "open-route", "open_route", "open-router", "open_router"}:
        key = OPENROUTER_PROVIDER_KEY
    if key in {"coultra-api", "coultra_api", "blueshirtmap", "coultra.blueshirtmap.com"}:
        key = COULTRA_PROVIDER_KEY
    if key in {"sub-2-api", "sub_2_api", "local-sub2api", "self-hosted-sub2api"}:
        key = SUB2API_PROVIDER_KEY
    if key in {"flow-2-api", "flow_2_api", "local-flow2api", "self-hosted-flow2api"}:
        key = FLOW2API_PROVIDER_KEY
    if key in {"doubao-web", "doubao_pool", "doubao-pool", "doubao-seedance"}:
        key = DOUBAO_PROVIDER_KEY
    return key


def provider_catalog() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for provider_key, config in AI_PROVIDER_CATALOG.items():
        models = supported_models_for_provider(provider_key)
        result.append({
            "id": provider_key,
            "label": str(config.get("label") or provider_key),
            "provider_type": str(config.get("provider_type") or "official"),
            "capabilities": list(config.get("capabilities") or []),
            "image_models": list(config.get("image_models") or []),
            "video_models": list(models),
            "hermes_managed": bool(config.get("hermes_env_var")),
            "supports_model_discovery": bool(config.get("supports_model_discovery")),
            "auto_discovery": bool(config.get("auto_discovery")),
        })
    return result


def provider_env_var(provider_key: str | None) -> str | None:
    provider = normalize_provider_key(provider_key)
    value = AI_PROVIDER_CATALOG.get(provider, {}).get("hermes_env_var")
    return str(value).strip() if value else None


def request_hermes_provider_sync(provider_key: str | None) -> None:
    if not provider_env_var(provider_key):
        return
    trigger = Path("/run/gmv/hermes-provider-sync.request")
    try:
        trigger.touch(exist_ok=True)
    except OSError:
        # Credential storage remains authoritative even if the deployment-side
        # watcher is temporarily unavailable.
        return


def normalize_video_model_id(model_id: str | None) -> str:
    value = str(model_id or "").strip().lower().replace("-", "_")
    aliases = {
        "omni": OMNI_FLASH_MODEL,
        "omni_flash_preview": OMNI_FLASH_MODEL,
        "gemini_omni_flash": OMNI_FLASH_MODEL,
        "google_omni_flash": OMNI_FLASH_MODEL,
        "google_gemini_omni": OMNI_FLASH_MODEL,
        "gemini_omni_flash_preview": OMNI_FLASH_MODEL,
        "seedance_2_0": SEEDANCE_2_0_MINI_MODEL,
        "seedence_2_0": SEEDANCE_2_0_MINI_MODEL,
        "seedence_2_0_mini": SEEDANCE_2_0_MINI_MODEL,
        "doubao_seedance_2_0_mini_260615": SEEDANCE_2_0_MINI_MODEL,
    }
    return aliases.get(value, value)


def model_scope(model_id: str) -> str:
    model = normalize_video_model_id(model_id)
    return str(VIDEO_MODEL_CATALOG.get(model, {}).get("scope") or f"video:{model}")


def supported_models_for_provider(provider_key: str | None) -> tuple[str, ...]:
    provider = normalize_provider_key(provider_key)
    return tuple(PROVIDER_DEFAULT_PRIORITIES.get(provider, {}))


def supported_image_models_for_provider(
    provider_key: str | None,
) -> tuple[str, ...]:
    provider = normalize_provider_key(provider_key)
    return tuple(
        str(model).strip().lower().replace("-", "_")
        for model in AI_PROVIDER_CATALOG.get(provider, {}).get(
            "image_models",
            [],
        )
        if str(model or "").strip()
    )


def _routing_model_scope(provider_key: str, model_id: str) -> str:
    model = str(model_id or "").strip().lower().replace("-", "_")
    if model in set(supported_image_models_for_provider(provider_key)):
        return f"image:{model}"
    return model_scope(model)


def normalize_reference_mode(value: str | None) -> str:
    mode = str(value or "reference").strip().lower().replace("-", "_")
    if mode in {"first_last", "first_and_last", "first_last_frame", "first_last_frames"}:
        return "first_last"
    return "reference"


def default_scopes_for_provider(provider_key: str | None) -> list[str]:
    provider = normalize_provider_key(provider_key)
    models = PROVIDER_DEFAULT_PRIORITIES.get(provider, {})
    video_scopes = (
        [VIDEO_GENERATE_SCOPE, *(model_scope(model) for model in models)]
        if models
        else []
    )
    image_scopes = [
        f"image:{model}"
        for model in supported_image_models_for_provider(provider)
    ]
    return [*video_scopes, *image_scopes]


def default_model_priorities_for_provider(provider_key: str | None) -> dict[str, int]:
    provider = normalize_provider_key(provider_key)
    return {
        **dict(PROVIDER_DEFAULT_PRIORITIES.get(provider, {})),
        **dict(PROVIDER_IMAGE_DEFAULT_PRIORITIES.get(provider, {})),
    }


def _configured_image_scopes(key: KieApiKey) -> list[str]:
    """Return only operator-granted image scopes supported by this provider.

    Video capability remains code-authoritative so stale rows cannot silently
    disable a deployed provider. Image credentials are intentionally narrower:
    a Sub2API key can point at a Gemini-only account group, so it must opt in to
    that image model without making every Flow/OpenAI key image-eligible.
    """
    allowed = {
        f"image:{model}"
        for model in supported_image_models_for_provider(key.provider_key)
    }
    result: list[str] = []
    for raw in list(key.scopes_json or []):
        value = str(raw or "").strip().lower().replace(" ", "_")[:128]
        if value in allowed and value not in result:
            result.append(value)
    return result


def _configured_image_priorities(key: KieApiKey) -> dict[str, int]:
    allowed = set(supported_image_models_for_provider(key.provider_key))
    result: dict[str, int] = {}
    for raw_model, raw_priority in dict(key.model_priorities_json or {}).items():
        model = str(raw_model or "").strip().lower().replace("-", "_")
        if model not in allowed:
            continue
        try:
            priority = int(raw_priority)
        except (TypeError, ValueError):
            continue
        result[model] = max(1, min(9999, priority))
    return result


def provider_model_default_enabled(provider_key: str, model_id: str) -> bool:
    provider = normalize_provider_key(provider_key)
    model = normalize_video_model_id(model_id)
    return bool(PROVIDER_MODEL_DEFAULT_ENABLED.get(provider, {}).get(model, False))


def provider_model_is_enabled(db: Session, provider_key: str, model_id: str) -> bool:
    provider = normalize_provider_key(provider_key)
    model = normalize_video_model_id(model_id)
    row = (
        db.query(AiProviderModelSetting)
        .filter(
            AiProviderModelSetting.provider_key == provider,
            AiProviderModelSetting.model_id == model,
        )
        .one_or_none()
    )
    return bool(row.is_enabled) if row is not None else provider_model_default_enabled(provider, model)


def set_provider_model_enabled(
    db: Session,
    *,
    provider_key: str,
    model_id: str,
    is_enabled: bool,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
) -> AiProviderModelSetting:
    provider = normalize_provider_key(provider_key)
    model = normalize_video_model_id(model_id)
    if model not in set(supported_models_for_provider(provider)):
        raise ValueError(f"Provider {provider} does not support video model {model}")
    row = (
        db.query(AiProviderModelSetting)
        .filter(
            AiProviderModelSetting.provider_key == provider,
            AiProviderModelSetting.model_id == model,
        )
        .one_or_none()
    )
    if row is None:
        row = AiProviderModelSetting(provider_key=provider, model_id=model, is_enabled=bool(is_enabled))
    else:
        row.is_enabled = bool(is_enabled)
    db.add(row)
    db.flush()
    log_event(
        db,
        action="ai_provider.model_route.update",
        resource_type="ai_provider_model_setting",
        resource_id=int(row.id),
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id,
        workspace_id=None,
        details={"provider_key": provider, "model_id": model, "is_enabled": bool(is_enabled)},
    )
    return row


def provider_model_settings_catalog(db: Session) -> list[dict[str, Any]]:
    provider_meta = {item["id"]: item for item in provider_catalog()}
    result: list[dict[str, Any]] = []
    for provider, models in PROVIDER_DEFAULT_PRIORITIES.items():
        for model, priority in models.items():
            model_config = VIDEO_MODEL_CATALOG.get(model, {})
            route_rows = (
                db.query(AiModelRoute, KieApiKey)
                .join(KieApiKey, KieApiKey.id == AiModelRoute.key_id)
                .filter(
                    AiModelRoute.provider_key == provider,
                    AiModelRoute.logical_model_id == model,
                    AiModelRoute.capability == "video",
                    AiModelRoute.workload == "default",
                )
                .order_by(AiModelRoute.priority.asc(), AiModelRoute.id.asc())
                .all()
            )
            result.append({
                "provider_key": provider,
                "provider_label": str(provider_meta.get(provider, {}).get("label") or provider),
                "model_id": model,
                "model_label": str(model_config.get("label") or model),
                "is_enabled": provider_model_is_enabled(db, provider, model),
                "default_enabled": provider_model_default_enabled(provider, model),
                "priority": min(
                    (int(route.priority) for route, _key in route_rows),
                    default=int(priority),
                ),
                "routes": [
                    {
                        "id": int(route.id),
                        "key_id": int(key.id),
                        "key_name": key.name,
                        "priority": int(route.priority),
                        "is_enabled": bool(route.is_enabled),
                        "is_verified": bool(route.is_verified),
                        "capabilities": effective_provider_model_capabilities(
                            db,
                            key,
                            model,
                        ),
                    }
                    for route, key in route_rows
                ],
                "capabilities": provider_model_capabilities(provider, model),
                "reference_image_limit": provider_reference_limit(provider, model),
            })
    return result


def normalize_scopes(scopes: Iterable[str] | None, *, provider_key: str | None = None) -> list[str]:
    provider = normalize_provider_key(provider_key)
    source = default_scopes_for_provider(provider) if scopes is None else list(scopes)
    video_models = supported_models_for_provider(provider)
    image_models = supported_image_models_for_provider(provider)
    allowed = {
        VIDEO_GENERATE_SCOPE,
        *(model_scope(model) for model in video_models),
        *(f"image:{model}" for model in image_models),
    }
    result: list[str] = []
    for raw in source:
        value = str(raw or "").strip().lower().replace(" ", "_")[:128]
        if (video_models or image_models) and value not in allowed:
            continue
        if value and value not in result:
            result.append(value)
    return result[:32]


def normalize_model_priorities(
    priorities: Mapping[str, Any] | None,
    *,
    provider_key: str | None = None,
) -> dict[str, int]:
    provider = normalize_provider_key(provider_key)
    source = default_model_priorities_for_provider(provider) if priorities is None else priorities
    supported = {
        *supported_models_for_provider(provider),
        *supported_image_models_for_provider(provider),
    }
    result: dict[str, int] = {}
    for raw_model, raw_priority in dict(source or {}).items():
        model = normalize_video_model_id(str(raw_model))
        if not model or (supported and model not in supported):
            continue
        try:
            priority = int(raw_priority)
        except (TypeError, ValueError):
            continue
        result[model] = max(1, min(9999, priority))
    return result


def normalize_routing_config(
    *,
    provider_key: str | None,
    scopes: Iterable[str] | None,
    model_priorities: Mapping[str, Any] | None,
) -> tuple[list[str], dict[str, int]]:
    provider = normalize_provider_key(provider_key)
    normalized_scopes = normalize_scopes(scopes, provider_key=provider)
    normalized_priorities = normalize_model_priorities(model_priorities, provider_key=provider)
    supported = (
        *supported_models_for_provider(provider),
        *supported_image_models_for_provider(provider),
    )
    if not supported:
        return normalized_scopes, normalized_priorities
    enabled = {
        model
        for model in supported
        if _routing_model_scope(provider, model) in set(normalized_scopes)
    }
    if not enabled:
        raise ValueError("At least one supported model scope is required")
    normalized_priorities = {
        model: normalized_priorities.get(
            model,
            default_model_priorities_for_provider(provider).get(model, 10),
        )
        for model in enabled
    }
    return normalized_scopes, normalized_priorities


def key_scopes(key: KieApiKey) -> list[str]:
    # Deployed code remains authoritative for video capabilities. A credential
    # may additionally opt into a provider-declared image model because the
    # upstream account group behind that key can be image-only.
    image_scopes = _configured_image_scopes(key)
    stored = normalize_scopes(key.scopes_json, provider_key=key.provider_key)
    # An explicit image-only credential points at a different upstream group
    # and must never be selected for Flow video. Existing video credentials
    # retain code-authoritative defaults even if an old row omitted a model
    # scope, preserving the deployed video routing contract.
    video_scopes = (
        []
        if image_scopes and VIDEO_GENERATE_SCOPE not in set(stored)
        else default_scopes_for_provider(key.provider_key)
    )
    return [*video_scopes, *image_scopes]


def key_model_priorities(key: KieApiKey) -> dict[str, int]:
    defaults = (
        default_model_priorities_for_provider(key.provider_key)
        if VIDEO_GENERATE_SCOPE in set(key_scopes(key))
        else {}
    )
    stored = normalize_model_priorities(
        key.model_priorities_json,
        provider_key=key.provider_key,
    )
    return {**defaults, **stored, **_configured_image_priorities(key)}


def key_supports_model(key: KieApiKey, model_id: str) -> bool:
    model = normalize_video_model_id(model_id)
    scopes = set(key_scopes(key))
    return VIDEO_GENERATE_SCOPE in scopes and model_scope(model) in scopes


def provider_reference_limit(provider_key: str, model_id: str) -> int:
    provider = normalize_provider_key(provider_key)
    model = normalize_video_model_id(model_id)
    return int(
        PROVIDER_REFERENCE_LIMITS.get(provider, {}).get(
            model,
            VIDEO_MODEL_CATALOG.get(model, {}).get("reference_image_limit", 0),
        )
        or 0
    )


def provider_model_capabilities(provider_key: str, model_id: str) -> dict[str, Any]:
    provider = normalize_provider_key(provider_key)
    model = normalize_video_model_id(model_id)
    return dict(PROVIDER_MODEL_CAPABILITIES.get(provider, {}).get(model, {}))


_VIDEO_CAPABILITY_OVERRIDE_FIELDS = frozenset({
    "aspect_ratios",
    "reference_video",
    "reference_modes",
    "reference_image_counts",
    "generation_modes",
    "durations",
    "resolutions",
    "resolution_aspect_ratios",
    "prompt_max_characters",
})


def effective_provider_model_capabilities(
    db: Session,
    key: KieApiKey,
    model_id: str,
) -> dict[str, Any]:
    """Return the provider/model contract for one concrete enabled route.

    Deployed provider defaults remain the safe fallback, while an operator may
    attach a verified ``video_capabilities`` object to the normal
    ``AiModelRoute.config_json`` row.  Keeping the override on the route makes
    capability, priority and health one auditable unit and avoids treating a
    logical model name as if every vendor implemented the same request shape.
    Unknown keys are ignored deliberately; they can never widen execution.
    """
    model = normalize_video_model_id(model_id)
    capabilities = provider_model_capabilities(key.provider_key, model)
    route = _legacy_route_row(db, key, model)
    route_config = dict(route.config_json or {}) if route is not None else {}
    override = route_config.get("video_capabilities")
    if isinstance(override, dict):
        for field in _VIDEO_CAPABILITY_OVERRIDE_FIELDS:
            if field in override:
                capabilities[field] = override[field]
    if (
        normalize_provider_key(key.provider_key) == DOUBAO_PROVIDER_KEY
        and model == SEEDANCE_2_0_MINI_MODEL
    ):
        # Route overrides may narrow a contract, but cannot silently widen a
        # free account pool to paid-only durations.
        from app.services.doubao_provider.pool import pool_supported_durations

        supported = set(pool_supported_durations(db))
        configured = {
            int(value)
            for value in list(capabilities.get("durations") or [])
            if str(value).strip().isdigit()
        }
        if supported and configured:
            capabilities["durations"] = sorted(supported.intersection(configured))
        else:
            capabilities["durations"] = sorted(supported)
    return capabilities


def _legacy_route_row(db: Session, key: KieApiKey, model_id: str) -> AiModelRoute | None:
    return (
        db.query(AiModelRoute)
        .filter(
            AiModelRoute.key_id == int(key.id),
            AiModelRoute.workload == "default",
            AiModelRoute.logical_model_id == normalize_video_model_id(model_id),
            AiModelRoute.capability == "video",
            AiModelRoute.workload == "default",
        )
        .order_by(AiModelRoute.id.asc())
        .first()
    )


def effective_key_model_priority(db: Session, key: KieApiKey, model_id: str) -> int:
    row = _legacy_route_row(db, key, model_id)
    if row is not None:
        return int(row.priority)
    return int(key_model_priorities(key).get(normalize_video_model_id(model_id), 9999))


def legacy_route_is_eligible(db: Session, key: KieApiKey, model_id: str) -> bool:
    row = _legacy_route_row(db, key, model_id)
    if row is None:
        return True
    if not row.is_enabled or not row.is_verified:
        return False
    # AiModelRoute circuit timestamps are written with the server-local naive
    # clock by the unified routing service.  Comparing them to utcnow() keeps a
    # two-minute circuit open for roughly eight extra hours on this host.
    return not bool(row.circuit_open_until and row.circuit_open_until > datetime.now())


def list_model_keys(
    db: Session,
    *,
    model_id: str,
    reference_count: int = 0,
    reference_video_count: int = 0,
    aspect_ratio: str | None = None,
    reference_mode: str | None = None,
    duration: int | None = None,
    resolution: str | None = None,
    generation_mode: str | None = None,
    require_active: bool = True,
    exclude_key_ids: Iterable[int] | None = None,
) -> list[KieApiKey]:
    model = normalize_video_model_id(model_id)
    catalog = VIDEO_MODEL_CATALOG.get(model)
    if catalog is None:
        return []
    query = db.query(KieApiKey).filter(KieApiKey.provider_key.in_(list(catalog["providers"])))
    if require_active:
        query = query.filter(KieApiKey.is_active.is_(True))
    excluded = {int(value) for value in (exclude_key_ids or [])}
    requested_ratio = str(aspect_ratio or "").strip()
    requested_mode = normalize_reference_mode(reference_mode)
    requested_resolution = str(resolution or "").strip().lower()
    requested_generation_mode = str(generation_mode or "").strip().lower()
    try:
        requested_duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        requested_duration = None
    rows = []
    for key in query.all():
        capabilities = effective_provider_model_capabilities(db, key, model)
        supported_ratios = set(capabilities.get("aspect_ratios") or [])
        if (
            int(key.id) in excluded
            or not key_supports_model(key, model)
            or not provider_model_is_enabled(db, key.provider_key, model)
            or not legacy_route_is_eligible(db, key, model)
        ):
            continue
        limit = provider_reference_limit(key.provider_key, model)
        if limit > 0 and int(reference_count) > limit:
            continue
        allowed_counts = {int(value) for value in capabilities.get("reference_image_counts") or []}
        if allowed_counts and int(reference_count) not in allowed_counts:
            continue
        if requested_ratio and supported_ratios and requested_ratio not in supported_ratios:
            continue
        if int(reference_video_count) > 0 and not bool(capabilities.get("reference_video")):
            continue
        supported_modes = set(capabilities.get("reference_modes") or [])
        if requested_mode and supported_modes and requested_mode not in supported_modes:
            continue
        supported_generation_modes = {
            str(value).strip().lower()
            for value in capabilities.get("generation_modes") or []
        }
        if (
            requested_generation_mode
            and supported_generation_modes
            and requested_generation_mode not in supported_generation_modes
        ):
            continue
        supported_durations = {int(value) for value in capabilities.get("durations") or []}
        if requested_duration is not None and supported_durations and requested_duration not in supported_durations:
            continue
        supported_resolutions = {str(value).lower() for value in capabilities.get("resolutions") or []}
        if requested_resolution and supported_resolutions and requested_resolution not in supported_resolutions:
            continue
        resolution_ratios = dict(capabilities.get("resolution_aspect_ratios") or {})
        allowed_ratios = set(resolution_ratios.get(requested_resolution) or [])
        if requested_resolution and requested_ratio and allowed_ratios and requested_ratio not in allowed_ratios:
            continue
        rows.append(key)
    rows.sort(
        key=lambda key: (
            effective_key_model_priority(db, key, model),
            0 if key.is_default else 1,
            int(key.id),
        )
    )
    return rows


def resolve_video_model_key(
    db: Session,
    *,
    model_id: str,
    reference_count: int = 0,
    reference_video_count: int = 0,
    aspect_ratio: str | None = None,
    reference_mode: str | None = None,
    duration: int | None = None,
    resolution: str | None = None,
    generation_mode: str | None = None,
    key_id: int | None = None,
    exclude_key_ids: Iterable[int] | None = None,
) -> KieApiKey:
    model = normalize_video_model_id(model_id)
    catalog = VIDEO_MODEL_CATALOG.get(model)
    if catalog is None:
        raise ValueError(f"Unsupported video model: {model}")
    if key_id is not None:
        key = get_key_by_id(db, key_id=int(key_id))
        provider = normalize_provider_key(key.provider_key) if key is not None else ""
        if (
            key is None
            or provider not in set(catalog["providers"])
            or not key.is_active
            or not key_supports_model(key, model)
            or not provider_model_is_enabled(db, provider, model)
            or not legacy_route_is_eligible(db, key, model)
        ):
            raise ValueError(f"API key is unavailable or lacks scope {model_scope(model)}")
        limit = provider_reference_limit(key.provider_key, model)
        if limit and int(reference_count) > limit:
            raise ValueError(f"{key.provider_key} supports at most {limit} reference images for {model}")
        capabilities = effective_provider_model_capabilities(db, key, model)
        allowed_counts = {int(value) for value in capabilities.get("reference_image_counts") or []}
        if allowed_counts and int(reference_count) not in allowed_counts:
            raise ValueError(
                f"{key.provider_key} supports reference image counts {sorted(allowed_counts)} for {model}"
            )
        supported_ratios = set(capabilities.get("aspect_ratios") or [])
        if aspect_ratio and supported_ratios and str(aspect_ratio) not in supported_ratios:
            raise ValueError(f"{key.provider_key} does not support aspect ratio {aspect_ratio} for {model}")
        if int(reference_video_count) > 0 and not bool(capabilities.get("reference_video")):
            raise ValueError(f"{key.provider_key} does not support reference video for {model}")
        requested_mode = normalize_reference_mode(reference_mode)
        supported_modes = set(capabilities.get("reference_modes") or [])
        if requested_mode and supported_modes and requested_mode not in supported_modes:
            raise ValueError(f"{key.provider_key} does not support reference mode {requested_mode} for {model}")
        requested_generation_mode = str(generation_mode or "").strip().lower()
        supported_generation_modes = {
            str(value).strip().lower()
            for value in capabilities.get("generation_modes") or []
        }
        if (
            requested_generation_mode
            and supported_generation_modes
            and requested_generation_mode not in supported_generation_modes
        ):
            raise ValueError(
                f"{key.provider_key} does not support generation mode "
                f"{requested_generation_mode} for {model}"
            )
        supported_durations = {int(value) for value in capabilities.get("durations") or []}
        if duration is not None and supported_durations and int(duration) not in supported_durations:
            raise ValueError(f"{key.provider_key} does not support duration {duration} for {model}")
        requested_resolution = str(resolution or "").strip().lower()
        supported_resolutions = {str(value).lower() for value in capabilities.get("resolutions") or []}
        if requested_resolution and supported_resolutions and requested_resolution not in supported_resolutions:
            raise ValueError(f"{key.provider_key} does not support resolution {resolution} for {model}")
        resolution_ratios = dict(capabilities.get("resolution_aspect_ratios") or {})
        allowed_ratios = set(resolution_ratios.get(requested_resolution) or [])
        if requested_resolution and aspect_ratio and allowed_ratios and str(aspect_ratio) not in allowed_ratios:
            raise ValueError(
                f"{key.provider_key} does not support {resolution} at aspect ratio {aspect_ratio} for {model}"
            )
        return key
    rows = list_model_keys(
        db,
        model_id=model,
        reference_count=reference_count,
        reference_video_count=reference_video_count,
        aspect_ratio=aspect_ratio,
        reference_mode=reference_mode,
        duration=duration,
        resolution=resolution,
        generation_mode=generation_mode,
        require_active=True,
        exclude_key_ids=exclude_key_ids,
    )
    if not rows:
        raise ValueError(f"No active API key with scope {model_scope(model)}")
    return rows[0]


def video_model_routing_catalog(db: Session) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for model, config in VIDEO_MODEL_CATALOG.items():
        routes = list_model_keys(db, model_id=model, require_active=True)
        route_payloads = [
            {
                "key_id": int(key.id),
                "provider_key": key.provider_key,
                "provider_label": str(
                    AI_PROVIDER_CATALOG.get(key.provider_key, {}).get("label")
                    or key.provider_key
                ),
                "priority": effective_key_model_priority(db, key, model),
                "capabilities": effective_provider_model_capabilities(
                    db, key, model
                ),
                "reference_image_limit": provider_reference_limit(
                    key.provider_key, model
                ),
            }
            for key in routes
        ]
        duration_values = sorted({
            int(value)
            for route in route_payloads
            for value in list(route["capabilities"].get("durations") or [])
            if str(value).strip().isdigit() and int(value) > 0
        })
        generation_modes = sorted({
            str(value)
            for route in route_payloads
            for value in list(
                route["capabilities"].get("generation_modes") or []
            )
            if str(value).strip()
        })
        result.append({
            "id": model,
            "label": config["label"],
            "scope": config["scope"],
            "reference_image_limit": int(config["reference_image_limit"]),
            "available": bool(routes),
            "route_count": len(routes),
            "available_durations_seconds": duration_values,
            "available_generation_modes": generation_modes,
            "routes": route_payloads,
        })
    return result


def _api_key_fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(plaintext: str) -> str:
    value = plaintext.strip()
    if not value:
        return ""
    if value.startswith(API_KEY_ENCRYPTION_PREFIX):
        return value
    token = _api_key_fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return f"{API_KEY_ENCRYPTION_PREFIX}{token}"


def decrypt_api_key(ciphertext: str) -> str:
    value = (ciphertext or "").strip()
    if not value or not value.startswith(API_KEY_ENCRYPTION_PREFIX):
        return value
    token = value[len(API_KEY_ENCRYPTION_PREFIX):]
    try:
        return _api_key_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Stored API key cannot be decrypted") from exc


# === CRUD & 查询工具（平台级，不绑定 workspace）===

def list_keys(db: Session, *, provider_key: str | None = None) -> List[KieApiKey]:
    q = db.query(KieApiKey)
    if provider_key:
        q = q.filter(KieApiKey.provider_key == normalize_provider_key(provider_key))
    return q.order_by(KieApiKey.id.asc()).all()


def get_key_by_id(
    db: Session,
    *,
    key_id: int,
    provider_key: str | None = None,
) -> Optional[KieApiKey]:
    q = db.query(KieApiKey).filter(KieApiKey.id == key_id)
    if provider_key:
        q = q.filter(KieApiKey.provider_key == normalize_provider_key(provider_key))
    return q.one_or_none()


def get_default_key(
    db: Session,
    *,
    require_active: bool = True,
    provider_key: str | None = DEFAULT_PROVIDER_KEY,
) -> Optional[KieApiKey]:
    provider = normalize_provider_key(provider_key)
    q = db.query(KieApiKey).filter(
        KieApiKey.provider_key == provider,
        KieApiKey.is_default.is_(True),
    )
    if require_active:
        q = q.filter(KieApiKey.is_active.is_(True))
    return q.one_or_none()


def get_effective_key(
    db: Session,
    *,
    key_id: int | None = None,
    require_active: bool = True,
    provider_key: str | None = DEFAULT_PROVIDER_KEY,
    model_id: str | None = None,
) -> KieApiKey:
    """
    统一挑选要用的 key：

    - 如果传了 key_id，则直接按 id 查，并校验 active（若 require_active=True）
    - 否则：优先用 is_default 为 True 且 active 的 key
    - 再否则：取第一个 active 的 key
    """
    provider = normalize_provider_key(provider_key)

    if model_id:
        model = normalize_video_model_id(model_id)
        if key_id is not None:
            return resolve_video_model_key(db, model_id=model, key_id=key_id)
        candidates = [
            key for key in list_model_keys(db, model_id=model, require_active=require_active)
            if key.provider_key == provider
        ]
        if candidates:
            return candidates[0]
        raise ValueError(f"No active API key configured for provider {provider} with scope {model_scope(model)}")

    if key_id is not None:
        k = get_key_by_id(db, key_id=key_id, provider_key=provider)
        if k is None:
            raise ValueError(f"API key not found for provider {provider}")
        if require_active and not k.is_active:
            raise ValueError("API key is not active")
        return k

    k = get_default_key(db, require_active=require_active, provider_key=provider)
    if k is not None:
        return k

    q = db.query(KieApiKey).filter(KieApiKey.provider_key == provider)
    if require_active:
        q = q.filter(KieApiKey.is_active.is_(True))
    k = q.order_by(KieApiKey.id.asc()).first()
    if k is None:
        raise ValueError(f"No active API key configured for provider {provider}")
    return k


def count_keys(
    db: Session,
    *,
    provider_key: str | None = DEFAULT_PROVIDER_KEY,
    require_active: bool = True,
) -> int:
    provider = normalize_provider_key(provider_key)
    q = db.query(KieApiKey).filter(KieApiKey.provider_key == provider)
    if require_active:
        q = q.filter(KieApiKey.is_active.is_(True))
    return int(q.count())


def has_active_key(
    db: Session,
    *,
    provider_key: str | None = DEFAULT_PROVIDER_KEY,
    model_id: str | None = None,
) -> bool:
    if model_id:
        provider = normalize_provider_key(provider_key)
        return any(
            key.provider_key == provider
            for key in list_model_keys(db, model_id=model_id, require_active=True)
        )
    return count_keys(db, provider_key=provider_key, require_active=True) > 0


# === 余额查询 & 自动选择 Key（供平台/租户共享）===


def create_kie_key(
    db: Session,
    *,
    name: str,
    api_key_plaintext: str,
    provider_key: str | None = DEFAULT_PROVIDER_KEY,
    is_default: bool = False,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieApiKey:
    """
    新建一个 KIE API key 记录（平台级）。

    注意：这里只做 flush，不 commit。
    """
    name = name.strip()
    if not name:
        raise ValueError("name is required")

    api_key_plaintext = api_key_plaintext.strip()
    if not api_key_plaintext:
        raise ValueError("api_key is required")

    provider = normalize_provider_key(provider_key)
    ciphertext = encrypt_api_key(api_key_plaintext)
    normalized_scopes, normalized_priorities = normalize_routing_config(
        provider_key=provider,
        scopes=None,
        model_priorities=None,
    )

    if is_default:
        # 取消同 provider 下其它默认 key
        existing_defaults: Iterable[KieApiKey] = (
            db.query(KieApiKey)
            .filter(
                KieApiKey.provider_key == provider,
                KieApiKey.is_default.is_(True),
            )
            .all()
        )
        for k in existing_defaults:
            k.is_default = False
            db.add(k)

    key = KieApiKey(
        name=name,
        provider_key=provider,
        api_key_ciphertext=ciphertext,
        is_active=True,
        is_default=is_default,
        scopes_json=normalized_scopes,
        model_priorities_json=normalized_priorities,
    )
    db.add(key)
    db.flush()  # 拿到 id

    log_event(
        db,
        action="kie.key.create",
        resource_type="kie_api_key",
        resource_id=key.id,
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id,
        actor_ip=actor_ip,
        user_agent=user_agent,
        workspace_id=None,  # 平台级配置，不属于某个租户
        details={
            "name": name,
            "provider_key": provider,
            "is_default": is_default,
            "scopes": key.scopes_json,
            "model_priorities": key.model_priorities_json,
        },
    )

    return key


def update_kie_key(
    db: Session,
    *,
    key: KieApiKey,
    name: Optional[str] = None,
    api_key_plaintext: Optional[str] = None,
    provider_key: Optional[str] = None,
    is_active: Optional[bool] = None,
    is_default: Optional[bool] = None,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieApiKey:
    """
    更新 key 信息（名称 / key / 启用状态 / 默认标记）。
    """
    changed: dict[str, object] = {}

    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("name cannot be empty")
        if key.name != name:
            changed["name"] = {"old": key.name, "new": name}
            key.name = name

    if api_key_plaintext is not None:
        api_key_plaintext = api_key_plaintext.strip()
        if not api_key_plaintext:
            raise ValueError("api_key cannot be empty")
        new_cipher = encrypt_api_key(api_key_plaintext)
        if key.api_key_ciphertext != new_cipher:
            changed["api_key"] = "***changed***"
            key.api_key_ciphertext = new_cipher

    if provider_key is not None:
        provider = normalize_provider_key(provider_key)
        if key.provider_key != provider:
            changed["provider_key"] = {"old": key.provider_key, "new": provider}
            key.provider_key = provider
            if key.is_default:
                others = (
                    db.query(KieApiKey)
                    .filter(
                        KieApiKey.id != key.id,
                        KieApiKey.provider_key == provider,
                        KieApiKey.is_default.is_(True),
                    )
                    .all()
                )
                for other in others:
                    other.is_default = False
                    db.add(other)

    image_scopes = _configured_image_scopes(key)
    stored_scopes = set(
        normalize_scopes(key.scopes_json, provider_key=key.provider_key)
    )
    image_only = bool(
        image_scopes and VIDEO_GENERATE_SCOPE not in stored_scopes
    )
    normalized_scopes, normalized_priorities = normalize_routing_config(
        provider_key=key.provider_key,
        scopes=(image_scopes if image_only else None),
        model_priorities=(
            _configured_image_priorities(key) if image_only else None
        ),
    )
    if not image_only:
        normalized_scopes.extend(
            scope for scope in image_scopes if scope not in normalized_scopes
        )
        normalized_priorities.update(_configured_image_priorities(key))
    if key.scopes_json != normalized_scopes:
        changed["scopes"] = {"old": key.scopes_json, "new": normalized_scopes}
        key.scopes_json = normalized_scopes
    if key.model_priorities_json != normalized_priorities:
        changed["model_priorities"] = {"old": key.model_priorities_json, "new": normalized_priorities}
        key.model_priorities_json = normalized_priorities

    if is_active is not None and key.is_active != is_active:
        changed["is_active"] = {"old": key.is_active, "new": is_active}
        key.is_active = is_active

    if is_default is not None and key.is_default != is_default:
        changed["is_default"] = {"old": key.is_default, "new": is_default}
        key.is_default = is_default

        if is_default:
            # 取消同 provider 下其它默认 key
            others = (
                db.query(KieApiKey)
                .filter(
                    KieApiKey.id != key.id,
                    KieApiKey.provider_key == key.provider_key,
                    KieApiKey.is_default.is_(True),
                )
                .all()
            )
            for other in others:
                other.is_default = False
                db.add(other)

    if changed:
        db.add(key)
        db.flush()

        log_event(
            db,
            action="kie.key.update",
            resource_type="kie_api_key",
            resource_id=key.id,
            actor_user_id=actor_user_id,
            actor_workspace_id=actor_workspace_id,
            actor_ip=actor_ip,
            user_agent=user_agent,
            workspace_id=None,
            details=changed,
        )

    return key


def deactivate_kie_key(
    db: Session,
    *,
    key: KieApiKey,
    actor_user_id: int | None = None,
    actor_workspace_id: int | None = None,
    actor_ip: str | None = None,
    user_agent: str | None = None,
) -> KieApiKey:
    """
    停用某个 key。
    """
    if not key.is_active and not key.is_default:
        return key

    key.is_active = False
    key.is_default = False
    db.add(key)
    db.flush()

    log_event(
        db,
        action="kie.key.deactivate",
        resource_type="kie_api_key",
        resource_id=key.id,
        actor_user_id=actor_user_id,
        actor_workspace_id=actor_workspace_id,
        actor_ip=actor_ip,
        user_agent=user_agent,
        workspace_id=None,
        details={"id": key.id},
    )

    return key


__all__ = [
    "BANDIANWA_PROVIDER_KEY",
    "KYY_PROVIDER_KEY",
    "GLOBALAIOPC_OMNI_FLASH_PROVIDER_KEY",
    "GOOGLE_GEMINI_PROVIDER_KEY",
    "GPT_IMAGE_2_MODEL",
    "NANO_BANANA_PRO_MODEL",
    "TOAPIS_PROVIDER_KEY",
    "OPENROUTER_PROVIDER_KEY",
    "COULTRA_PROVIDER_KEY",
    "SUB2API_PROVIDER_KEY",
    "FLOW2API_PROVIDER_KEY",
    "DOUBAO_PROVIDER_KEY",
    "VOLCENGINE_PROVIDER_KEY",
    "DEFAULT_PROVIDER_KEY",
    "OMNI_FLASH_MODEL",
    "SEEDANCE_2_0_MINI_MODEL",
    "VIDEO_GENERATE_SCOPE",
    "VIDEO_MODEL_CATALOG",
    "AI_PROVIDER_CATALOG",
    "PROVIDER_MODEL_DEFAULT_ENABLED",
    "provider_catalog",
    "provider_env_var",
    "provider_model_default_enabled",
    "provider_model_is_enabled",
    "provider_model_settings_catalog",
    "set_provider_model_enabled",
    "request_hermes_provider_sync",
    "normalize_video_model_id",
    "normalize_reference_mode",
    "model_scope",
    "supported_models_for_provider",
    "default_scopes_for_provider",
    "default_model_priorities_for_provider",
    "normalize_scopes",
    "normalize_model_priorities",
    "normalize_routing_config",
    "key_scopes",
    "key_model_priorities",
    "key_supports_model",
    "provider_reference_limit",
    "provider_model_capabilities",
    "effective_provider_model_capabilities",
    "effective_key_model_priority",
    "legacy_route_is_eligible",
    "list_model_keys",
    "resolve_video_model_key",
    "video_model_routing_catalog",
    "normalize_provider_key",
    "encrypt_api_key",
    "decrypt_api_key",
    "list_keys",
    "get_key_by_id",
    "get_default_key",
    "get_effective_key",
    "create_kie_key",
    "update_kie_key",
    "deactivate_kie_key",
]
