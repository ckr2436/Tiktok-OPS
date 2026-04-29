from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import APIError


class HermesAgentClient:
    """Small typed wrapper around Hermes Agent's OpenAI-compatible Responses API."""

    def __init__(self) -> None:
        self.base_url = settings.HERMES_AGENT_BASE_URL.rstrip("/")
        self.api_key = settings.HERMES_AGENT_API_KEY
        self.model = settings.HERMES_AGENT_MODEL
        self.timeout = float(settings.HERMES_AGENT_TIMEOUT_SECONDS)

    async def health(self) -> dict[str, Any]:
        if not settings.HERMES_AGENT_ENABLED:
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
        conversation: str | None = None,
        previous_response_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not settings.HERMES_AGENT_ENABLED:
            raise APIError("HERMES_DISABLED", "Hermes Agent is not enabled.", 503)
        if not self.api_key:
            raise APIError("HERMES_MISCONFIGURED", "Hermes Agent API key is missing.", 500)
        if not self.base_url:
            raise APIError("HERMES_MISCONFIGURED", "Hermes Agent base URL is missing.", 500)

        payload: dict[str, Any] = {
            "model": self.model,
            "input": input_text,
            "instructions": instructions,
            "store": True,
        }
        if conversation:
            payload["conversation"] = conversation
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if metadata:
            payload["metadata"] = metadata

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/responses",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
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
            return resp.json(), latency_ms
        except ValueError as exc:
            raise APIError("HERMES_BAD_RESPONSE", "Hermes Agent returned invalid JSON.", 502) from exc

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


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
