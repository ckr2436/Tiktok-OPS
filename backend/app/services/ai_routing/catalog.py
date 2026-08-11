from __future__ import annotations

from dataclasses import dataclass

from app.services.ai_video.accounts import (
    COULTRA_PROVIDER_KEY,
    FLOW2API_PROVIDER_KEY,
    OPENROUTER_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
    SUB2API_PROVIDER_KEY,
    normalize_provider_key,
)
from app.core.config import settings


@dataclass(frozen=True, slots=True)
class ProviderTransportSpec:
    provider_key: str
    base_url: str
    models_path: str = "/models"
    chat_path: str = "/chat/completions"
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    automatic_discovery: bool = True


PROVIDER_TRANSPORTS: dict[str, ProviderTransportSpec] = {
    TOAPIS_PROVIDER_KEY: ProviderTransportSpec(
        provider_key=TOAPIS_PROVIDER_KEY,
        base_url="https://toapis.com/v1",
    ),
    # OpenRouter stays available for existing records but is intentionally not
    # contacted automatically. The operator explicitly removed it from the
    # active video-analysis route.
    OPENROUTER_PROVIDER_KEY: ProviderTransportSpec(
        provider_key=OPENROUTER_PROVIDER_KEY,
        base_url="https://openrouter.ai/api/v1",
        automatic_discovery=False,
    ),
    COULTRA_PROVIDER_KEY: ProviderTransportSpec(
        provider_key=COULTRA_PROVIDER_KEY,
        base_url="https://coultra.blueshirtmap.com/v1",
    ),
    SUB2API_PROVIDER_KEY: ProviderTransportSpec(
        provider_key=SUB2API_PROVIDER_KEY,
        base_url=str(settings.SUB2API_API_BASE_URL).rstrip("/"),
    ),
    FLOW2API_PROVIDER_KEY: ProviderTransportSpec(
        provider_key=FLOW2API_PROVIDER_KEY,
        base_url=str(settings.FLOW2API_API_BASE_URL).rstrip("/"),
    ),
}


def provider_transport(provider_key: str | None) -> ProviderTransportSpec | None:
    return PROVIDER_TRANSPORTS.get(normalize_provider_key(provider_key))


def model_capabilities(model_id: str) -> list[str]:
    value = str(model_id or "").strip().lower()
    if not value:
        return []
    if value in {
        "nano_banana_pro",
        "nano-banana-pro",
        "gemini-3.0-pro-image",
        "gemini-3.1-flash-image",
    }:
        return ["image"]
    video_tokens = (
        "video", "veo", "sora", "seedance", "kling", "vidu", "omni",
    )
    image_tokens = (
        "image", "flux", "midjourney", "dall-e", "dalle", "ideogram",
    )
    if any(token in value for token in video_tokens):
        return ["video"]
    if any(token in value for token in image_tokens):
        return ["image"]
    result = ["text"]
    if any(token in value for token in ("gpt-4o", "gpt-5", "gemini", "vision", "claude")):
        result.append("multimodal")
    return result


def endpoint_modes(capabilities: list[str]) -> list[str]:
    modes: list[str] = []
    if "text" in capabilities or "multimodal" in capabilities:
        modes.append("chat_completions")
    if "image" in capabilities:
        modes.append("images")
    if "video" in capabilities:
        modes.append("provider_specific_video")
    return modes


__all__ = [
    "PROVIDER_TRANSPORTS",
    "ProviderTransportSpec",
    "endpoint_modes",
    "model_capabilities",
    "provider_transport",
]
