import json

import pytest

from app.services.ai_routing.video_provider_recovery import (
    VideoProviderIncident,
    VideoRecoveryAction,
    classify_video_provider_fault,
    decide_video_provider_recovery,
    inspect_local_provider_health,
)


class _Client:
    model = "test-model"

    def __init__(self, action: str):
        self.action = action

    async def create_response(self, **_kwargs):
        return (
            {
                "output": [{
                    "type": "message",
                    "content": [{
                        "type": "output_text",
                        "text": json.dumps({
                            "action": self.action,
                            "wait_seconds": 90,
                            "reason_code": "LOCAL_POOL_DEGRADED",
                            "rationale": "Repeated local failures justify provider rotation.",
                            "decision_source": "model",
                        }),
                    }]
                }],
            },
            4,
        )


def _incident(**overrides):
    values = {
        "incident_id": "video-3004-sub2api-2",
        "provider": "sub2api",
        "fault_class": "UPSTREAM_TRANSIENT",
        "status_code": 502,
        "retry_number": 2,
        "fallback_available": True,
        "local_health": {
            "sub2api": {"reachable": True, "status": "ok"},
            "flow2api": {"reachable": True, "status": "degraded", "active_accounts": 0},
        },
    }
    values.update(overrides)
    return VideoProviderIncident(**values)


@pytest.mark.asyncio
async def test_model_can_choose_only_bounded_provider_switch():
    decision = await decide_video_provider_recovery(
        _incident(), client=_Client("SWITCH_PROVIDER")
    )
    assert decision.action == VideoRecoveryAction.SWITCH_PROVIDER
    assert decision.decision_source == "model"


@pytest.mark.asyncio
async def test_disallowed_model_action_falls_back_safely():
    decision = await decide_video_provider_recovery(
        _incident(fallback_available=False), client=_Client("SWITCH_PROVIDER")
    )
    assert decision.action == VideoRecoveryAction.WAIT_RETRY_SAME
    assert decision.decision_source == "safe_fallback"


def test_fault_classification_separates_auth_and_transient():
    auth = RuntimeError("HTTP 401 invalid authentication credentials")
    transient = RuntimeError("HTTP 503 Service temporarily unavailable")
    assert classify_video_provider_fault(auth) == "AUTH"
    assert classify_video_provider_fault(transient) == "UPSTREAM_TRANSIENT"


def test_fault_classification_uses_stable_provider_error_code():
    class _ProviderError(RuntimeError):
        code = "doubao_quota_exhausted"

    assert classify_video_provider_fault(_ProviderError("capacity unavailable")) == "QUOTA"


def test_fault_classification_marks_local_prompt_contract_as_request_invalid():
    class _ProviderError(RuntimeError):
        code = "doubao_prompt_contract_invalid"

    assert (
        classify_video_provider_fault(_ProviderError("missing dialogue line"))
        == "REQUEST_INVALID"
    )


@pytest.mark.asyncio
async def test_request_invalid_never_retries_same_provider():
    decision = await decide_video_provider_recovery(
        _incident(fault_class="REQUEST_INVALID", fallback_available=False),
        client=_Client("WAIT_RETRY_SAME"),
    )

    assert decision.action == VideoRecoveryAction.PAUSE_POLICY


@pytest.mark.asyncio
async def test_local_health_includes_sanitized_pool_diagnostics(monkeypatch):
    class _Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "status": "degraded",
                "capacity": {
                    "active_accounts": 1,
                    "total_accounts": 5,
                    "total_credits": 20,
                    "auth_blocked_accounts": 2,
                    "blocked_accounts_by_reason": {"GRANT_EXPIRED": 2},
                },
            }

    async def _get(_self, url):
        return _Response()

    monkeypatch.setattr("httpx.AsyncClient.get", _get)
    health = await inspect_local_provider_health("sub2api")

    assert health["flow2api"]["total_accounts"] == 5
    assert health["flow2api"]["auth_blocked_accounts"] == 2
    assert health["flow2api"]["blocked_accounts_by_reason"] == {
        "GRANT_EXPIRED": 2
    }
