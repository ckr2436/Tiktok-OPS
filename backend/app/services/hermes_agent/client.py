from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import APIError


class HermesAgentClient:
    """Small typed wrapper around Hermes Agent's OpenAI-compatible Responses API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.base_url = (base_url or settings.HERMES_AGENT_BASE_URL).rstrip("/")
        self.api_key = settings.HERMES_AGENT_API_KEY if api_key is None else api_key
        self.model = model or settings.HERMES_AGENT_MODEL
        self.timeout = float(timeout if timeout is not None else settings.HERMES_AGENT_TIMEOUT_SECONDS)
        self.enabled = bool(settings.HERMES_AGENT_ENABLED if enabled is None else enabled)

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            raise APIError("HERMES_DISABLED", "Hermes Agent is not enabled.", 503)
        async with httpx.AsyncClient(timeout=min(self.timeout, 10.0)) as client:
            resp = await client.get(
                f"{self.base_url}/health",
                headers=self._headers(),
            )
        if resp.status_code >= 400:
            raise APIError(
                "HERMES_UPSTREAM_ERROR",
                "Hermes Agent health check failed.",
                502,
                {"status_code": resp.status_code, "body": resp.text[:1000]},
            )
        return resp.json()

    async def create_response(
        self,
        *,
        input_text: str,
        instructions: str,
        input_items: list[dict[str, Any]] | None = None,
        conversation: str | None = None,
        previous_response_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        store: bool = True,
        idempotency_key: str | None = None,
        session_key: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not self.enabled:
            raise APIError("HERMES_DISABLED", "Hermes Agent is not enabled.", 503)
        if not self.api_key:
            raise APIError("HERMES_MISCONFIGURED", "Hermes Agent API key is missing.", 500)
        if not self.base_url:
            raise APIError("HERMES_MISCONFIGURED", "Hermes Agent base URL is missing.", 500)

        payload: dict[str, Any] = {
            "model": self.model,
            "input": input_items if input_items is not None else input_text,
            "instructions": instructions,
            "store": bool(store),
        }
        if conversation:
            payload["conversation"] = conversation
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if metadata:
            payload["metadata"] = metadata

        started = time.monotonic()
        headers = self._headers()
        if idempotency_key:
            normalized_key = str(idempotency_key).strip()
            if not normalized_key or len(normalized_key) > 255 or any(
                character in normalized_key
                for character in "\r\n\x00"
            ):
                raise APIError(
                    "HERMES_INVALID_IDEMPOTENCY_KEY",
                    "Hermes idempotency key is invalid.",
                    400,
                )
            headers["Idempotency-Key"] = normalized_key
        if session_key:
            normalized_session_key = str(session_key).strip()
            if not normalized_session_key or len(normalized_session_key) > 512 or any(
                character in normalized_session_key
                for character in "\r\n\x00"
            ):
                raise APIError(
                    "HERMES_INVALID_SESSION_KEY",
                    "Hermes session key is invalid.",
                    400,
                )
            headers["X-Hermes-Session-Key"] = normalized_session_key
        try:
            # httpx timeouts are inactivity limits for individual socket
            # operations.  A gateway can keep a request alive between those
            # operations and exceed the intended role budget.  The outer
            # asyncio deadline is a true wall-clock fence, so a large model
            # response cannot hold a content-control worker indefinitely.
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        f"{self.base_url}/responses",
                        headers=headers,
                        json=payload,
                    )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise APIError("HERMES_TIMEOUT", "Hermes Agent request timed out.", 504) from exc
        except httpx.HTTPError as exc:
            raise APIError("HERMES_NETWORK_ERROR", "Hermes Agent is unreachable.", 502, {"detail": str(exc)}) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            raise APIError(
                "HERMES_UPSTREAM_ERROR",
                "Hermes Agent request failed.",
                502,
                {"status_code": resp.status_code, "body": resp.text[:2000]},
            )
        try:
            payload_out = resp.json()
        except ValueError as exc:
            raise APIError("HERMES_BAD_RESPONSE", "Hermes Agent returned invalid JSON.", 502) from exc
        if isinstance(payload_out, dict):
            payload_out.setdefault(
                "_gmv_meta",
                {
                    "model": self.model,
                    "latency_ms": latency_ms,
                    "request_id": str((metadata or {}).get("request_id") or ""),
                    "agent_role": str((metadata or {}).get("agent_role") or "primary"),
                    "prompt_version": str((metadata or {}).get("prompt_version") or ""),
                },
            )
            failure = hermes_response_failure(payload_out)
            if failure is not None:
                code, _provider_message = failure
                # Do not echo an upstream body into application errors; it may
                # contain account identifiers.  The role and status are enough
                # for orchestration, while provider detail remains in the
                # isolated gateway journal.
                raise APIError(
                    code,
                    "Hermes content model provider is temporarily unavailable.",
                    503,
                    {
                        "agent_role": str((metadata or {}).get("agent_role") or "primary"),
                        "upstream_status": str(payload_out.get("status") or "failed"),
                    },
                )
        return payload_out, latency_ms

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


class _HermesContentIsolatedClient(HermesAgentClient):
    """Stateless content role that never falls back to another Hermes."""

    role = "content"

    async def create_response(self, **kwargs: Any) -> tuple[dict[str, Any], int]:
        if kwargs.get("conversation") or kwargs.get("previous_response_id"):
            raise APIError(
                "HERMES_CONTENT_CONTEXT_FORBIDDEN",
                "Content director roles require an explicit stateless input packet.",
                400,
            )
        metadata = dict(kwargs.pop("metadata", None) or {})
        idempotency_key = str(kwargs.get("idempotency_key") or "").strip()
        if not idempotency_key:
            input_items = kwargs.get("input_items")
            input_material = (
                repr(input_items)
                if input_items is not None
                else str(kwargs.get("input_text") or "")
            )
            digest = hashlib.sha256(
                "\n".join([
                    self.role,
                    self.model,
                    str(kwargs.get("instructions") or ""),
                    input_material,
                ]).encode("utf-8")
            ).hexdigest()
            idempotency_key = f"gmv-content-{self.role}-{digest}"
            kwargs["idempotency_key"] = idempotency_key
        metadata.setdefault("agent_role", self.role)
        metadata.setdefault("request_id", idempotency_key)
        kwargs["metadata"] = metadata
        kwargs["store"] = False
        kwargs["conversation"] = None
        kwargs["previous_response_id"] = None
        return await super().create_response(**kwargs)


class HermesContentDirectorClient(_HermesContentIsolatedClient):
    """Creates a complete program and immutable script before media spend."""

    role = "content_director"

    def __init__(self) -> None:
        super().__init__(
            base_url=settings.HERMES_CONTENT_DIRECTOR_AGENT_BASE_URL,
            api_key=settings.HERMES_AGENT_API_KEY,
            model=settings.HERMES_CONTENT_DIRECTOR_AGENT_MODEL,
            timeout=float(settings.HERMES_CONTENT_DIRECTOR_AGENT_TIMEOUT_SECONDS),
            enabled=bool(settings.HERMES_CONTENT_DIRECTOR_AGENT_ENABLED),
        )


class HermesContentProducerClient(HermesAgentClient):
    """Conversational intake role with a physically isolated response store.

    Producer continuity is scoped to one workspace/user/intake session.  It
    cannot use the browser, create media, or mutate a project.  Director and
    Critic deliberately keep their stronger stateless boundary.
    """

    role = "content_producer"

    async def create_response(self, **kwargs: Any) -> tuple[dict[str, Any], int]:
        metadata = dict(kwargs.pop("metadata", None) or {})
        idempotency_key = str(kwargs.get("idempotency_key") or "").strip()
        if not idempotency_key:
            input_items = kwargs.get("input_items")
            input_material = (
                repr(input_items)
                if input_items is not None
                else str(kwargs.get("input_text") or "")
            )
            digest = hashlib.sha256(
                "\n".join([
                    self.role,
                    self.model,
                    str(kwargs.get("instructions") or ""),
                    input_material,
                ]).encode("utf-8")
            ).hexdigest()
            idempotency_key = f"gmv-content-{self.role}-{digest}"
            kwargs["idempotency_key"] = idempotency_key
        metadata.setdefault("agent_role", self.role)
        metadata.setdefault("request_id", idempotency_key)
        kwargs["metadata"] = metadata
        kwargs["store"] = True
        return await super().create_response(**kwargs)

    def __init__(self) -> None:
        super().__init__(
            base_url=settings.HERMES_CONTENT_PRODUCER_AGENT_BASE_URL,
            api_key=settings.HERMES_AGENT_API_KEY,
            model=settings.HERMES_CONTENT_PRODUCER_AGENT_MODEL,
            timeout=float(settings.HERMES_CONTENT_PRODUCER_AGENT_TIMEOUT_SECONDS),
            enabled=bool(settings.HERMES_CONTENT_PRODUCER_AGENT_ENABLED),
        )


class HermesContentCriticClient(_HermesContentIsolatedClient):
    """Independently reviews an explicit director artifact without author context."""

    role = "content_critic"

    def __init__(self) -> None:
        super().__init__(
            base_url=settings.HERMES_CONTENT_CRITIC_AGENT_BASE_URL,
            api_key=settings.HERMES_AGENT_API_KEY,
            model=settings.HERMES_CONTENT_CRITIC_AGENT_MODEL,
            timeout=float(settings.HERMES_CONTENT_CRITIC_AGENT_TIMEOUT_SECONDS),
            enabled=bool(settings.HERMES_CONTENT_CRITIC_AGENT_ENABLED),
        )


class HermesVideoAnalystClient(_HermesContentIsolatedClient):
    """Stateless, vision-capable analyst isolated from every authoring role."""

    role = "video_analyst"

    def __init__(self) -> None:
        super().__init__(
            base_url=settings.HERMES_VIDEO_ANALYST_AGENT_BASE_URL,
            api_key=settings.HERMES_AGENT_API_KEY,
            model=settings.HERMES_VIDEO_ANALYST_AGENT_MODEL,
            timeout=float(settings.HERMES_VIDEO_ANALYST_AGENT_TIMEOUT_SECONDS),
            enabled=bool(settings.HERMES_VIDEO_ANALYST_AGENT_ENABLED),
        )


class _HermesAdsIsolatedClient(HermesAgentClient):
    """Ads client that never falls back to the tool-enabled primary agent."""

    role = "ads"

    async def create_response(self, **kwargs: Any) -> tuple[dict[str, Any], int]:
        metadata = dict(kwargs.pop("metadata", None) or {})
        metadata.setdefault("agent_role", self.role)
        metadata.setdefault("request_id", str(uuid.uuid4()))
        kwargs["metadata"] = metadata
        kwargs.setdefault("store", False)
        return await super().create_response(**kwargs)


class HermesAdsRealtimeClient(_HermesAdsIsolatedClient):
    """Small-context endpoint for bounded, latency-sensitive ad decisions."""

    role = "ads_realtime"

    def __init__(self) -> None:
        super().__init__(
            base_url=settings.HERMES_ADS_REALTIME_AGENT_BASE_URL,
            api_key=settings.HERMES_AGENT_API_KEY,
            model=settings.HERMES_ADS_REALTIME_AGENT_MODEL,
            timeout=float(settings.HERMES_ADS_REALTIME_AGENT_TIMEOUT_SECONDS),
            enabled=bool(settings.HERMES_AGENT_ENABLED and settings.HERMES_ADS_REALTIME_AGENT_ENABLED),
        )


class HermesAdsReviewClient(_HermesAdsIsolatedClient):
    """Long-context endpoint for daily reports and retrospective analysis."""

    role = "ads_review"

    def __init__(self) -> None:
        super().__init__(
            base_url=settings.HERMES_ADS_REVIEW_AGENT_BASE_URL,
            api_key=settings.HERMES_AGENT_API_KEY,
            model=settings.HERMES_ADS_REVIEW_AGENT_MODEL,
            timeout=float(settings.HERMES_ADS_REVIEW_AGENT_TIMEOUT_SECONDS),
            enabled=bool(settings.HERMES_AGENT_ENABLED and settings.HERMES_ADS_REVIEW_AGENT_ENABLED),
        )


def extract_output_text(payload: dict[str, Any]) -> str:
    """Extract assistant text from Hermes/OpenAI Responses-compatible output."""
    if not payload:
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct

    chunks: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            text = part.get("text") or part.get("output_text")
                            if isinstance(text, str):
                                chunks.append(text)
                elif isinstance(content, str):
                    chunks.append(content)
            elif item.get("type") in {"output_text", "text"}:
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    if chunks:
        return "\n".join(chunks).strip()

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def hermes_response_failure(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return a safe failure class/message for a Responses-compatible result.

    Older Hermes gateways wrapped a failed provider run in an HTTP-200
    ``status=completed`` envelope whose assistant text was the upstream error.
    Newer gateways expose ``status=failed`` and a structured error.  Accept
    both shapes during the rolling upgrade so billing/auth failures never enter
    a Director JSON-contract repair loop.
    """
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    status = str(payload.get("status") or "").strip().lower()
    message = ""
    structured_failure_type = ""
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("detail") or "")
        structured_failure_type = str(
            error.get("type") or error.get("code") or ""
        ).strip().upper()
    elif isinstance(error, str):
        message = error
    raw_text = extract_output_text(payload)
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    total_tokens = int((usage or {}).get("total_tokens") or 0)
    legacy_failed_envelope = bool(
        total_tokens == 0
        and raw_text
        and (
            raw_text.lstrip().lower().startswith("error code:")
            or "non-retryable client error" in raw_text.lower()
            or "all eligible ai routes failed" in raw_text.lower()
        )
    )
    if status != "failed" and not error and not legacy_failed_envelope:
        return None
    message = (message or raw_text or "Hermes upstream execution failed")[:2000]
    lowered = message.lower()
    quota_markers = (
        "insufficient_user_quota",
        "insufficient quota",
        "insufficient balance",
        "remaining balance",
        "pre-charge failed",
        "预扣费额度失败",
        "用户剩余额度",
        "需要预扣费额度",
        "余额不足",
    )
    auth_markers = (
        "invalid api key",
        "unauthorized",
        "authentication",
        "invalid gateway token",
    )
    policy_markers = (
        "content_policy_violation",
        "content policy violation",
        "prompt policy violation",
        "prompt violation",
        "prompt rejected by safety",
        "prompt blocked by safety",
        "moderation_blocked",
        "moderation blocked",
        "提示词违规",
        "违反内容政策",
        "内容审核未通过",
    )
    if structured_failure_type == "POLICY" or any(
        marker in lowered for marker in policy_markers
    ):
        return "HERMES_PROMPT_POLICY_VIOLATION", message
    if structured_failure_type == "QUOTA" or any(
        marker in lowered for marker in quota_markers
    ):
        return "HERMES_UPSTREAM_QUOTA", message
    if structured_failure_type == "AUTH" or any(
        marker in lowered for marker in auth_markers
    ):
        return "HERMES_UPSTREAM_AUTH", message
    return "HERMES_UPSTREAM_EXECUTION_FAILED", message


def extract_usage(payload: dict[str, Any]) -> dict[str, int | None]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
    completion = usage.get("completion_tokens") or usage.get("output_tokens")
    total = usage.get("total_tokens")
    if total is None and (prompt is not None or completion is not None):
        total = int(prompt or 0) + int(completion or 0)
    return {
        "prompt_tokens": int(prompt) if prompt is not None else None,
        "completion_tokens": int(completion) if completion is not None else None,
        "total_tokens": int(total) if total is not None else None,
    }
