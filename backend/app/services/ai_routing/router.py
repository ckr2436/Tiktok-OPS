from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.ai_routing import AiModelRoute, AiProviderModel, AiRouteAttempt
from app.data.models.kie_api import KieApiKey
from app.services.ai_routing.catalog import provider_transport
from app.services.kie_api.accounts import decrypt_api_key


class AiGatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_class: str = "UPSTREAM",
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def utcnow() -> datetime:
    return datetime.now()


def _retry_after(response: httpx.Response) -> int | None:
    try:
        return max(1, min(3600, int(float(response.headers.get("retry-after", "")))))
    except (TypeError, ValueError):
        return None


_POLICY_ERROR_MARKERS = (
    "content_policy_violation",
    "content policy violation",
    "prompt policy violation",
    "prompt violation",
    "prompt rejected by safety",
    "prompt blocked by safety",
    "moderation_blocked",
    "moderation blocked",
    "content_filter",
    "content filter blocked",
    "prohibited content",
    "提示词违规",
    "提示词包含违规",
    "违反内容政策",
    "违反安全策略",
    "内容审核未通过",
    "安全审核未通过",
)

_QUOTA_ERROR_MARKERS = (
    "insufficient_user_quota",
    "insufficient quota",
    "insufficient balance",
    "remaining balance",
    "pre-charge failed",
    "credit balance",
    "预扣费额度失败",
    "用户剩余额度",
    "需要预扣费额度",
    "余额不足",
)


def _explicit_error_class(error_text: str) -> str | None:
    bounded_error = str(error_text or "")[:4000].lower()
    if any(marker in bounded_error for marker in _POLICY_ERROR_MARKERS):
        return "POLICY"
    if any(marker in bounded_error for marker in _QUOTA_ERROR_MARKERS):
        return "QUOTA"
    return None


def _error_from_response(response: httpx.Response) -> AiGatewayError:
    status = int(response.status_code)
    # Several OpenAI-compatible aggregators report account balance failures as
    # HTTP 403 instead of 402.  Classifying every 403 as an invalid key opens a
    # 24-hour auth circuit and hides the healthy secondary route.  Inspect only
    # bounded error text for stable billing markers; never retain the provider
    # body in route health or audit rows.
    explicit_class = _explicit_error_class(response.text or "")
    if explicit_class is not None:
        error_class = explicit_class
    elif status in {401, 403}:
        error_class = "AUTH"
    elif status == 402:
        error_class = "QUOTA"
    elif status == 429:
        error_class = "RATE_LIMIT"
    elif status >= 500:
        error_class = "UPSTREAM_5XX"
    elif status in {400, 404, 409, 422}:
        error_class = "REQUEST"
    else:
        error_class = "UPSTREAM"
    return AiGatewayError(
        f"Provider request failed with HTTP {status}",
        error_class=error_class,
        status_code=status,
        retry_after_seconds=_retry_after(response),
    )


def _eligible_routes(
    db: Session,
    *,
    logical_model_id: str,
    capability: str,
    workload: str,
    route_id: int | None = None,
) -> list[tuple[AiModelRoute, KieApiKey]]:
    now = utcnow()
    query = (
        db.query(AiModelRoute, KieApiKey)
        .join(KieApiKey, KieApiKey.id == AiModelRoute.key_id)
        .filter(
            AiModelRoute.logical_model_id == str(logical_model_id),
            AiModelRoute.capability == str(capability),
            AiModelRoute.is_enabled.is_(True),
            AiModelRoute.is_verified.is_(True),
            KieApiKey.is_active.is_(True),
            or_(
                AiModelRoute.circuit_open_until.is_(None),
                AiModelRoute.circuit_open_until <= now,
            ),
        )
    )
    if route_id is not None:
        query = query.filter(AiModelRoute.id == int(route_id))
    elif workload == "default":
        query = query.filter(AiModelRoute.workload == "default")
    else:
        query = query.filter(AiModelRoute.workload.in_((str(workload), "default")))
    rows = list(query.all())
    rows.sort(
        key=lambda pair: (
            0 if pair[0].workload == workload else 1,
            int(pair[0].priority),
            int(pair[0].consecutive_failures),
            int(pair[0].latency_ema_ms or 0),
            int(pair[0].id),
        )
    )
    return rows


def _attempt(
    db: Session,
    *,
    route: AiModelRoute,
    request_id: str,
    switched_from_route_id: int | None,
    metadata: Mapping[str, Any] | None,
) -> AiRouteAttempt:
    safe_meta = {
        str(key)[:64]: value
        for key, value in dict(metadata or {}).items()
        if key in {
            "source",
            "workload",
            "idempotency_key_hash",
            "retry_attempt",
            "retry_round",
        }
        and isinstance(value, (str, int, float, bool, type(None)))
    }
    row = AiRouteAttempt(
        route_id=int(route.id),
        request_id=str(request_id)[:96],
        switched_from_route_id=switched_from_route_id,
        status="STARTED",
        metadata_json=safe_meta or None,
    )
    db.add(row)
    db.flush()
    return row


def _mark_success(
    route: AiModelRoute,
    attempt: AiRouteAttempt,
    *,
    latency_ms: int,
    usage: Mapping[str, Any] | None,
    source_route: AiModelRoute | None = None,
) -> None:
    now = utcnow()
    previous = route.latency_ema_ms
    route.latency_ema_ms = int(latency_ms if previous is None else (previous * 0.7 + latency_ms * 0.3))
    route.consecutive_failures = 0
    route.total_successes = int(route.total_successes or 0) + 1
    route.health_status = "HEALTHY"
    route.circuit_open_until = None
    route.last_success_at = now
    route.last_error_class = None
    route.last_error_message = None
    attempt.status = "SUCCEEDED"
    attempt.latency_ms = int(latency_ms)
    attempt.prompt_tokens = int((usage or {}).get("prompt_tokens") or 0) or None
    attempt.completion_tokens = int((usage or {}).get("completion_tokens") or 0) or None
    attempt.completed_at = now
    if source_route is not None:
        source_route.latency_ema_ms = route.latency_ema_ms
        source_route.consecutive_failures = 0
        source_route.total_successes = int(source_route.total_successes or 0) + 1
        source_route.health_status = "HEALTHY"
        source_route.circuit_open_until = None
        source_route.last_success_at = now
        source_route.last_error_class = None
        source_route.last_error_message = None


def _mark_failure(
    route: AiModelRoute,
    attempt: AiRouteAttempt,
    error: AiGatewayError,
    *,
    latency_ms: int,
    source_route: AiModelRoute | None = None,
) -> None:
    now = utcnow()
    route.total_failures = int(route.total_failures or 0) + 1
    route.last_failure_at = now
    route.last_error_class = error.error_class
    route.last_error_message = str(error)[:1000]
    route.consecutive_failures = int(route.consecutive_failures or 0) + 1
    open_seconds = 0
    if error.error_class == "AUTH":
        open_seconds = 24 * 60 * 60
    elif error.error_class == "QUOTA":
        open_seconds = 6 * 60 * 60
    elif error.error_class == "RATE_LIMIT":
        open_seconds = int(error.retry_after_seconds or 5 * 60)
    elif error.error_class in {"NETWORK", "UPSTREAM_5XX", "INVALID_RESPONSE"}:
        open_seconds = 2 * 60 if route.consecutive_failures >= 2 else 0
    if error.error_class in {"REQUEST", "POLICY"}:
        # A request-contract error is not evidence that the provider is down.
        route.health_status = "HEALTHY" if route.last_success_at else "UNKNOWN"
        route.consecutive_failures = max(0, route.consecutive_failures - 1)
    elif open_seconds > 0:
        route.health_status = "CIRCUIT_OPEN"
        route.circuit_open_until = now + timedelta(seconds=open_seconds)
    else:
        route.health_status = "DEGRADED"
    attempt.status = "FAILED"
    attempt.error_class = error.error_class
    attempt.upstream_status_code = error.status_code
    attempt.latency_ms = int(latency_ms)
    attempt.completed_at = now
    if source_route is not None:
        source_route.total_failures = int(source_route.total_failures or 0) + 1
        source_route.last_failure_at = now
        source_route.last_error_class = error.error_class
        source_route.last_error_message = str(error)[:1000]
        source_route.consecutive_failures = int(source_route.consecutive_failures or 0) + 1
        source_open_seconds = 0
        if error.error_class == "AUTH":
            source_open_seconds = 24 * 60 * 60
        elif error.error_class == "QUOTA":
            source_open_seconds = 6 * 60 * 60
        elif error.error_class == "RATE_LIMIT":
            source_open_seconds = int(error.retry_after_seconds or 5 * 60)
        elif error.error_class in {"NETWORK", "UPSTREAM_5XX", "INVALID_RESPONSE"}:
            source_open_seconds = 2 * 60 if source_route.consecutive_failures >= 2 else 0
        if error.error_class in {"REQUEST", "POLICY"}:
            source_route.health_status = "HEALTHY" if source_route.last_success_at else "UNKNOWN"
            source_route.consecutive_failures = max(0, source_route.consecutive_failures - 1)
        elif source_open_seconds > 0:
            source_route.health_status = "CIRCUIT_OPEN"
            source_route.circuit_open_until = now + timedelta(seconds=source_open_seconds)
        else:
            source_route.health_status = "DEGRADED"


def _managed_source_route(db: Session, route: AiModelRoute) -> AiModelRoute | None:
    config = dict(route.config_json or {})
    if config.get("managed_by") != "content_role_model_group_v1":
        return None
    try:
        source_id = int(config.get("source_route_id") or 0)
    except (TypeError, ValueError):
        return None
    if source_id <= 0 or source_id == int(route.id):
        return None
    return db.get(AiModelRoute, source_id)


async def _call_route(
    db: Session,
    *,
    route: AiModelRoute,
    key: KieApiKey,
    payload: Mapping[str, Any],
    request_id: str,
    switched_from_route_id: int | None,
    metadata: Mapping[str, Any] | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    spec = provider_transport(route.provider_key)
    if spec is None or route.adapter_type != "openai_chat_completions":
        raise AiGatewayError("Route adapter is not implemented", error_class="REQUEST", status_code=400)
    attempt = _attempt(
        db,
        route=route,
        request_id=request_id,
        switched_from_route_id=switched_from_route_id,
        metadata=metadata,
    )
    db.commit()
    outbound = dict(payload)
    outbound["model"] = route.provider_model_id
    outbound["stream"] = False
    started = time.monotonic()
    error: AiGatewayError | None = None
    response_payload: dict[str, Any] | None = None
    try:
        try:
            api_key = decrypt_api_key(key.api_key_ciphertext)
        except Exception as exc:
            raise AiGatewayError(
                "Provider credential could not be loaded",
                error_class="AUTH",
            ) from exc
        headers = {
            spec.auth_header: f"{spec.auth_prefix}{api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": str(request_id)[:96],
        }
        async with httpx.AsyncClient(base_url=spec.base_url, timeout=timeout_seconds) as client:
            response = await client.post(spec.chat_path, headers=headers, json=outbound)
        if response.status_code >= 400:
            raise _error_from_response(response)
        try:
            parsed = response.json()
        except ValueError as exc:
            raise AiGatewayError("Provider returned invalid JSON", error_class="INVALID_RESPONSE") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("choices"), list):
            explicit_class = _explicit_error_class(
                json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                if isinstance(parsed, dict)
                else ""
            )
            if explicit_class is not None:
                raise AiGatewayError(
                    "Provider returned an explicit failed completion",
                    error_class=explicit_class,
                    status_code=422 if explicit_class == "POLICY" else 402,
                )
            raise AiGatewayError("Provider returned an invalid chat completion", error_class="INVALID_RESPONSE")
        response_payload = dict(parsed)
    except AiGatewayError as exc:
        error = exc
    except httpx.HTTPError as exc:
        error = AiGatewayError(
            f"Provider transport error: {exc.__class__.__name__}",
            error_class="NETWORK",
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    route = db.get(AiModelRoute, int(route.id)) or route
    attempt = db.get(AiRouteAttempt, int(attempt.id)) or attempt
    source_route = _managed_source_route(db, route)
    if error is not None:
        _mark_failure(
            route,
            attempt,
            error,
            latency_ms=latency_ms,
            source_route=source_route,
        )
        db.add_all(tuple(item for item in (route, attempt, source_route) if item is not None))
        db.commit()
        raise error
    usage = dict((response_payload or {}).get("usage") or {})
    _mark_success(
        route,
        attempt,
        latency_ms=latency_ms,
        usage=usage,
        source_route=source_route,
    )
    catalog_row = (
        db.query(AiProviderModel)
        .filter(
            AiProviderModel.provider_key == route.provider_key,
            AiProviderModel.provider_model_id == route.provider_model_id,
        )
        .one_or_none()
    )
    if catalog_row is not None:
        catalog_row.lifecycle_status = "VERIFIED"
        catalog_row.last_verified_at = route.last_success_at
        catalog_row.is_available = True
        db.add(catalog_row)
    db.add_all(tuple(item for item in (route, attempt, source_route) if item is not None))
    db.commit()
    assert response_payload is not None
    response_payload["_gmv_route"] = {
        "route_id": int(route.id),
        "provider_key": route.provider_key,
        "provider_model_id": route.provider_model_id,
        "logical_model_id": route.logical_model_id,
        "latency_ms": latency_ms,
    }
    return response_payload


async def call_chat_with_failover(
    db: Session,
    *,
    logical_model_id: str,
    messages: list[dict[str, Any]],
    capability: str = "text",
    workload: str = "default",
    request_id: str | None = None,
    payload_overrides: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    timeout_seconds: float = 180,
    max_routes: int = 4,
    max_attempts: int | None = None,
    total_budget_seconds: float | None = None,
    attempt_timeout_seconds: float | None = None,
    retry_base_delay_seconds: float | None = None,
    retry_max_delay_seconds: float | None = None,
) -> dict[str, Any]:
    rid = str(request_id or uuid.uuid4())[:96]
    routes = _eligible_routes(
        db,
        logical_model_id=logical_model_id,
        capability=capability,
        workload=workload,
    )[: max(1, min(8, int(max_routes)))]
    if not routes:
        raise AiGatewayError(
            f"No healthy route for {logical_model_id}/{capability}/{workload}",
            error_class="NO_ROUTE",
            status_code=503,
        )
    payload = {"messages": messages, **dict(payload_overrides or {})}
    configured_max_attempts = max(
        1,
        min(
            32,
            int(
                max_attempts
                if max_attempts is not None
                else settings.AI_ROUTING_RETRY_MAX_ATTEMPTS
            ),
        ),
    )
    configured_budget = max(
        1.0,
        min(
            float(timeout_seconds),
            float(
                total_budget_seconds
                if total_budget_seconds is not None
                else settings.AI_ROUTING_RETRY_TOTAL_BUDGET_SECONDS
            ),
        ),
    )
    configured_attempt_timeout = max(
        1.0,
        min(
            configured_budget,
            float(
                attempt_timeout_seconds
                if attempt_timeout_seconds is not None
                else settings.AI_ROUTING_RETRY_ATTEMPT_TIMEOUT_SECONDS
            ),
        ),
    )
    base_delay = max(
        0.0,
        float(
            retry_base_delay_seconds
            if retry_base_delay_seconds is not None
            else settings.AI_ROUTING_RETRY_BASE_DELAY_SECONDS
        ),
    )
    max_delay = max(
        base_delay,
        float(
            retry_max_delay_seconds
            if retry_max_delay_seconds is not None
            else settings.AI_ROUTING_RETRY_MAX_DELAY_SECONDS
        ),
    )
    errors: list[AiGatewayError] = []
    previous_route_id: int | None = None
    retryable_routes = list(routes)
    started = time.monotonic()
    attempt_number = 0
    retry_round = 0
    while retryable_routes and attempt_number < configured_max_attempts:
        next_round_routes: list[tuple[AiModelRoute, KieApiKey]] = []
        for route, key in retryable_routes:
            if attempt_number >= configured_max_attempts:
                break
            remaining_budget = configured_budget - (time.monotonic() - started)
            if remaining_budget < 1.0:
                break
            attempt_number += 1
            try:
                return await _call_route(
                    db,
                    route=route,
                    key=key,
                    payload=payload,
                    request_id=rid,
                    switched_from_route_id=previous_route_id,
                    metadata={
                        **dict(metadata or {}),
                        "workload": workload,
                        "retry_attempt": attempt_number,
                        "retry_round": retry_round + 1,
                    },
                    timeout_seconds=min(configured_attempt_timeout, remaining_budget),
                )
            except AiGatewayError as exc:
                errors.append(exc)
                previous_route_id = int(route.id)
                if exc.error_class == "POLICY":
                    raise AiGatewayError(
                        "Provider explicitly rejected the prompt under its content policy",
                        error_class="POLICY",
                        status_code=exc.status_code or 422,
                    ) from exc
                if exc.error_class != "QUOTA":
                    next_round_routes.append((route, key))
        if not next_round_routes or attempt_number >= configured_max_attempts:
            break
        remaining_budget = configured_budget - (time.monotonic() - started)
        if remaining_budget < 1.0:
            break
        delay = min(max_delay, base_delay * (2**retry_round))
        if delay > 0:
            await asyncio.sleep(min(delay, max(0.0, remaining_budget - 0.5)))
        retryable_routes = next_round_routes
        retry_round += 1
    if not errors:
        raise AiGatewayError(
            "AI route retry budget expired before an upstream attempt completed",
            error_class="TIMEOUT",
            status_code=504,
        )
    final = errors[-1]
    raise AiGatewayError(
        f"All eligible AI routes failed after retry budget "
        f"({len(errors)} attempts): {final}",
        error_class=final.error_class,
        status_code=final.status_code or 502,
        retry_after_seconds=final.retry_after_seconds,
    )


async def probe_route(
    db: Session,
    *,
    route_id: int,
    enable_on_success: bool = False,
) -> dict[str, Any]:
    pair = (
        db.query(AiModelRoute, KieApiKey)
        .join(KieApiKey, KieApiKey.id == AiModelRoute.key_id)
        .filter(AiModelRoute.id == int(route_id), KieApiKey.is_active.is_(True))
        .one_or_none()
    )
    if pair is None:
        raise AiGatewayError("Route or active key was not found", error_class="NO_ROUTE", status_code=404)
    route, key = pair
    if route.adapter_type != "openai_chat_completions":
        raise AiGatewayError("This route has no safe zero-media probe", error_class="REQUEST", status_code=400)
    original_enabled = bool(route.is_enabled)
    original_verified = bool(route.is_verified)
    try:
        response = await _call_route(
            db,
            route=route,
            key=key,
            payload={
                "messages": [{"role": "user", "content": "Reply with OK only."}],
                "max_tokens": 8,
                "temperature": 0,
            },
            request_id=f"probe-{route.id}-{uuid.uuid4().hex[:16]}",
            switched_from_route_id=None,
            metadata={"source": "platform_route_probe", "workload": route.workload},
            timeout_seconds=45,
        )
    except Exception:
        route = db.get(AiModelRoute, int(route.id)) or route
        route.is_enabled = original_enabled
        route.is_verified = original_verified
        db.add(route)
        db.commit()
        raise
    route = db.get(AiModelRoute, int(route.id)) or route
    route.is_verified = True
    route.is_enabled = bool(enable_on_success or original_enabled)
    route.health_status = "HEALTHY"
    route.last_success_at = utcnow()
    catalog_row = (
        db.query(AiProviderModel)
        .filter(
            AiProviderModel.provider_key == route.provider_key,
            AiProviderModel.provider_model_id == route.provider_model_id,
        )
        .one_or_none()
    )
    if catalog_row is not None:
        catalog_row.lifecycle_status = "VERIFIED"
        catalog_row.last_verified_at = route.last_success_at
        catalog_row.is_available = True
        db.add(catalog_row)
    db.add(route)
    db.commit()
    return {
        "ok": True,
        "route_id": int(route.id),
        "provider_key": route.provider_key,
        "provider_model_id": route.provider_model_id,
        "enabled": bool(route.is_enabled),
        "verified": bool(route.is_verified),
        "route": dict(response.get("_gmv_route") or {}),
    }


__all__ = ["AiGatewayError", "call_chat_with_failover", "probe_route"]
