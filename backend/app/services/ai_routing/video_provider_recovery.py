from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.services.hermes_agent.client import (
    HermesVideoProviderRecoveryClient,
    extract_output_text,
)


POLICY_VERSION = "video-provider-recovery-v1"
SELF_HOSTED_PROVIDERS = {"sub2api", "doubao"}


class VideoRecoveryAction(str, Enum):
    WAIT_RETRY_SAME = "WAIT_RETRY_SAME"
    SWITCH_PROVIDER = "SWITCH_PROVIDER"
    PAUSE_AUTH = "PAUSE_AUTH"
    PAUSE_POLICY = "PAUSE_POLICY"


class VideoProviderIncident(BaseModel):
    incident_id: str = Field(min_length=8, max_length=96)
    provider: str = Field(min_length=1, max_length=32)
    fault_class: str = Field(min_length=1, max_length=64)
    status_code: int | None = Field(default=None, ge=100, le=599)
    retry_number: int = Field(ge=0, le=100)
    fallback_available: bool = False
    local_health: dict[str, Any] = Field(default_factory=dict)


class VideoProviderDecision(BaseModel):
    action: VideoRecoveryAction
    wait_seconds: int = Field(default=60, ge=15, le=1800)
    reason_code: str = Field(min_length=1, max_length=96)
    rationale: str = Field(min_length=1, max_length=500)
    decision_source: str = Field(default="model", pattern="^(model|safe_fallback)$")


def classify_video_provider_fault(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "") or "").strip().lower()
    value = f"{code} {str(exc or '')}".lower()
    if code in {"doubao_prompt_contract_invalid", "doubao_prompt_too_long"}:
        return "REQUEST_INVALID"
    if status in {401, 403} or "authentication" in value or "auth_required" in value:
        return "AUTH"
    if "prompt" in value and any(item in value for item in ("policy", "violation", "blocked")):
        return "POLICY"
    if "quota" in value or "balance" in value or "余额" in value:
        return "QUOTA"
    if status == 429 or "rate limit" in value:
        return "RATE_LIMIT"
    if status in {500, 502, 503, 504, 529} or "temporarily unavailable" in value:
        return "UPSTREAM_TRANSIENT"
    if "timeout" in value or "transport error" in value or "connection" in value:
        return "NETWORK"
    return "UNKNOWN"


async def inspect_local_provider_health(provider: str) -> dict[str, Any]:
    """Probe only fixed loopback services and return a credential-free snapshot."""
    targets = (
        [("sub2api", "http://127.0.0.1:19080/health"), ("flow2api", "http://127.0.0.1:19082/healthz")]
        if provider == "sub2api"
        else []
    )
    result: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
        for name, url in targets:
            try:
                response = await client.get(url)
                payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                item: dict[str, Any] = {"reachable": True, "status_code": int(response.status_code)}
                if isinstance(payload, dict):
                    item["status"] = str(payload.get("status") or "")[:32]
                    capacity = payload.get("capacity")
                    if isinstance(capacity, dict):
                        item["active_accounts"] = int(capacity.get("active_accounts") or 0)
                        item["total_accounts"] = int(capacity.get("total_accounts") or 0)
                        item["total_credits"] = int(capacity.get("total_credits") or 0)
                        item["auth_blocked_accounts"] = int(
                            capacity.get("auth_blocked_accounts") or 0
                        )
                        blocked = capacity.get("blocked_accounts_by_reason")
                        if isinstance(blocked, dict):
                            item["blocked_accounts_by_reason"] = {
                                str(reason)[:64]: int(count or 0)
                                for reason, count in blocked.items()
                            }
                result[name] = item
            except (httpx.HTTPError, ValueError, TypeError):
                result[name] = {"reachable": False}
    return result


def _allowed_actions(incident: VideoProviderIncident) -> tuple[VideoRecoveryAction, ...]:
    if incident.fault_class == "POLICY":
        return (VideoRecoveryAction.PAUSE_POLICY,)
    if incident.fault_class == "REQUEST_INVALID":
        return (
            (VideoRecoveryAction.SWITCH_PROVIDER, VideoRecoveryAction.PAUSE_POLICY)
            if incident.fallback_available
            else (VideoRecoveryAction.PAUSE_POLICY,)
        )
    if incident.fault_class in {"AUTH", "QUOTA"}:
        return (
            (VideoRecoveryAction.SWITCH_PROVIDER, VideoRecoveryAction.PAUSE_AUTH)
            if incident.fallback_available
            else (VideoRecoveryAction.PAUSE_AUTH,)
        )
    actions = [VideoRecoveryAction.WAIT_RETRY_SAME]
    if incident.fallback_available and incident.retry_number >= 2:
        actions.append(VideoRecoveryAction.SWITCH_PROVIDER)
    return tuple(actions)


def _fallback(incident: VideoProviderIncident, allowed: tuple[VideoRecoveryAction, ...], reason: str) -> VideoProviderDecision:
    if VideoRecoveryAction.SWITCH_PROVIDER in allowed and incident.retry_number >= 2:
        action = VideoRecoveryAction.SWITCH_PROVIDER
    else:
        action = allowed[0]
    return VideoProviderDecision(
        action=action,
        wait_seconds=min(300, 60 * max(1, incident.retry_number + 1)),
        reason_code="SAFE_DETERMINISTIC_FALLBACK",
        rationale=str(reason or "Recovery model unavailable")[:500],
        decision_source="safe_fallback",
    )


async def decide_video_provider_recovery(
    incident: VideoProviderIncident,
    *,
    client: HermesVideoProviderRecoveryClient | None = None,
) -> VideoProviderDecision:
    allowed = _allowed_actions(incident)
    packet = {
        "policy_version": POLICY_VERSION,
        "incident": incident.model_dump(mode="json"),
        "allowed_actions": [item.value for item in allowed],
        "output_contract": VideoProviderDecision.model_json_schema(),
    }
    digest = hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()[:32]
    runtime = client or HermesVideoProviderRecoveryClient()
    try:
        payload, _ = await runtime.create_response(
            input_text=json.dumps(packet, ensure_ascii=False, separators=(",", ":")),
            instructions=(
                "You are a stateless video-provider incident adviser. Never execute tools, open a browser, "
                "change prompts, expose credentials, or submit media. Choose exactly one allowed action. "
                "Prefer a cooled retry when the local service and account capacity are healthy; switch after "
                "repeated failures or when capacity is unavailable. Return strict JSON only."
            ),
            idempotency_key=f"gmv-video-recovery-{digest}",
            metadata={"agent_role": "video_provider_recovery", "request_id": incident.incident_id},
        )
        raw = str(extract_output_text(payload) or "").strip()
        if raw.startswith("```"):
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        decision = VideoProviderDecision.model_validate(json.loads(raw))
        if decision.action not in allowed:
            raise ValueError("model selected an action outside the server envelope")
        return decision
    except Exception as exc:  # noqa: BLE001
        return _fallback(incident, allowed, f"{type(exc).__name__}: {str(exc)[:400]}")


__all__ = [
    "SELF_HOSTED_PROVIDERS",
    "VideoProviderDecision",
    "VideoProviderIncident",
    "VideoRecoveryAction",
    "classify_video_provider_fault",
    "decide_video_provider_recovery",
    "inspect_local_provider_health",
]
