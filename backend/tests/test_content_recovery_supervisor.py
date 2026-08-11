from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services.hermes_agent.content_recovery_supervisor import (
    RecoveryAction,
    RecoveryIncident,
    allowed_recovery_actions,
    classify_recovery_fault,
    decide_content_recovery,
)
from app.tasks.hermes_agent.content_factory_tasks import (
    _content_factory_api_route,
    _handoff_api_stage_to_browser,
    _api_retry_input_after_recovery,
    _waiting_bridge_api_retry_input,
    _recovery_supervisor_incident,
    _recovery_api_probe_delay,
    _recovery_transition_has_live_cooldown,
    _legacy_reasonless_recovery_pause,
    _run_single_writer_recovery_transition,
    _schedule_supervised_api_retry,
    _schedule_supervised_provider_rotation,
    _text_api_fault_needs_recovery_supervisor,
    _visual_api_recovery_epoch,
)


def _incident(**overrides) -> RecoveryIncident:
    values = {
        "incident_id": "cf-recovery-test-0001",
        "project_id": 170,
        "stage_id": 2511,
        "stage": "CREATIVE_REVIEW",
        "variant_index": 3,
        "source_backend": "api",
        "fault_class": "NETWORK",
        "api_available": True,
        "browser_eligible": True,
        "recovery_cycle": 1,
    }
    values.update(overrides)
    return RecoveryIncident.model_validate(values)


class _FakeClient:
    model = "fake-recovery-model"

    def __init__(self, response: dict | Exception):
        self.response = response

    async def create_response(self, **_kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        return {
            "output_text": json.dumps(self.response),
            "_gmv_meta": {"model": self.model},
        }, 17


def test_recovery_supervisor_receives_bounded_visual_evidence(tmp_path):
    evidence = tmp_path / "failed-board.png"
    Image.new("RGB", (24, 40), "purple").save(evidence, format="PNG")
    captured = {}

    class Client:
        model = "vision-recovery-model"

        async def create_response(self, **kwargs):
            captured.update(kwargs)
            return ({
                "output_text": json.dumps({
                    "action": "SEMANTIC_PROMPT_REPAIR",
                    "wait_seconds": 0,
                    "reason_code": "UNSAFE_REQUEST_REPAIRABLE",
                    "rationale": "Rewrite accidental unsafe framing.",
                    "confidence": 0.98,
                    "diagnosis": "The wording, not the user intent, triggered safety.",
                    "repair_directive": "Use a safe adult editorial pose.",
                    "evidence_used": ["failed board"],
                }),
                "_gmv_meta": {"model": self.model},
            }, 23)

    decision = asyncio.run(decide_content_recovery(
        _incident(
            stage="VISUAL_PREVIEW",
            fault_class="PROMPT_POLICY",
            error_summary="PUBLIC_ERROR_UNSAFE_GENERATION",
            active_input_summary=(
                "reference 2: adult animated woman reaches for a bedside product"
            ),
        ),
        client=Client(),
        evidence_images=[("failed board", str(evidence))],
    ))

    assert decision.action == RecoveryAction.SEMANTIC_PROMPT_REPAIR
    assert decision.repair_directive.startswith("Use a safe")
    assert captured["input_items"][0]["content"][2]["type"] == "input_image"
    packet = json.loads(captured["input_text"])
    assert "adult animated woman" in packet["incident"]["active_input_summary"]


def test_transient_api_fault_allows_model_to_choose_cooldown():
    incident = _incident()
    assert allowed_recovery_actions(incident) == (
        RecoveryAction.WAIT_AND_RETRY_API,
        RecoveryAction.SWITCH_TO_BROWSER,
    )

    decision = asyncio.run(
        decide_content_recovery(
            incident,
            client=_FakeClient({
                "action": "WAIT_AND_RETRY_API",
                "wait_seconds": 180,
                "reason_code": "TRANSIENT_API_NETWORK",
                "rationale": "The API fault is transient and browser use is unnecessary.",
                "confidence": 0.96,
            }),
        )
    )

    assert decision.action == RecoveryAction.WAIT_AND_RETRY_API
    assert decision.wait_seconds == 180
    assert decision.decision_source == "model"


def test_browser_upload_zero_returns_to_api_and_never_retries_browser():
    incident = _incident(
        source_backend="browser",
        fault_class="BROWSER_UPLOAD_UNAVAILABLE",
        browser_upload_available=False,
    )
    assert allowed_recovery_actions(incident) == (
        RecoveryAction.SWITCH_TO_API,
    )

    decision = asyncio.run(
        decide_content_recovery(
            incident,
            client=_FakeClient({
                "action": "SWITCH_TO_API",
                "wait_seconds": 0,
                "reason_code": "BROWSER_UPLOAD_UNAVAILABLE",
                "rationale": "The browser cannot accept the required reference image.",
                "confidence": 0.99,
            }),
        )
    )
    assert decision.action == RecoveryAction.SWITCH_TO_API


def test_disallowed_model_action_fails_closed_to_api_retry():
    decision = asyncio.run(
        decide_content_recovery(
            _incident(browser_upload_available=False),
            client=_FakeClient({
                "action": "RETRY_BROWSER",
                "wait_seconds": 0,
                "reason_code": "BAD_MODEL_CHOICE",
                "rationale": "Try the browser anyway.",
                "confidence": 0.7,
            }),
        )
    )
    assert decision.action == RecoveryAction.WAIT_AND_RETRY_API
    assert decision.decision_source == "safe_fallback"


def test_quota_and_cycle_limit_open_cooled_recovery_not_operator_pause():
    assert allowed_recovery_actions(
        _incident(fault_class="ACCOUNT_QUOTA")
    ) == (
        RecoveryAction.ROTATE_PROVIDER,
        RecoveryAction.WAIT_AND_RETRY_API,
    )
    assert allowed_recovery_actions(
        _incident(recovery_cycle=25, max_recovery_cycles=24)
    ) == (RecoveryAction.WAIT_AND_RETRY_API,)


def test_manual_pause_remains_an_operator_boundary():
    assert allowed_recovery_actions(
        _incident(manual_pause=True)
    ) == (RecoveryAction.PAUSE_NONRETRYABLE,)


def test_prompt_policy_is_rewritten_via_api_instead_of_paused():
    assert allowed_recovery_actions(
        _incident(fault_class="PROMPT_POLICY", stage="VISUAL_PREVIEW")
    ) == (RecoveryAction.SEMANTIC_PROMPT_REPAIR,)
    assert allowed_recovery_actions(
        _incident(fault_class="PROMPT_POLICY", stage="DIRECTOR")
    ) == (RecoveryAction.RECOMPILE_STAGE_INPUT,)


def test_http_400_is_request_rejection_and_opens_model_directed_repair():
    assert classify_recovery_fault(
        "Flow2API image HTTP 400: upstream request failed"
    ) == "API_REQUEST_REJECTED"
    assert allowed_recovery_actions(
        _incident(
            fault_class="API_REQUEST_REJECTED",
            stage="VISUAL_PREVIEW",
            api_available=True,
            browser_eligible=False,
        )
    ) == (
        RecoveryAction.ROTATE_PROVIDER,
        RecoveryAction.SEMANTIC_PROMPT_REPAIR,
        RecoveryAction.RECOMPILE_STAGE_INPUT,
        RecoveryAction.WAIT_AND_RETRY_API,
    )


def test_upstream_auth_fault_can_rotate_api_instead_of_waking_logged_out_browser():
    assert allowed_recovery_actions(
        _incident(
            source_backend="api",
            fault_class="AUTH",
            api_available=True,
            browser_eligible=False,
            browser_login_available=False,
        )
    ) == (
        RecoveryAction.ROTATE_PROVIDER,
        RecoveryAction.WAIT_AND_RETRY_API,
    )


def test_quota_fault_uses_cooled_api_when_browser_is_logged_out():
    assert allowed_recovery_actions(
        _incident(
            source_backend="api",
            fault_class="ACCOUNT_QUOTA",
            api_available=True,
            browser_eligible=False,
            browser_login_available=False,
        )
    ) == (
        RecoveryAction.ROTATE_PROVIDER,
        RecoveryAction.WAIT_AND_RETRY_API,
    )


def test_quota_fault_uses_cooled_api_when_browser_bridge_is_offline():
    assert allowed_recovery_actions(
        _incident(
            source_backend="api",
            fault_class="ACCOUNT_QUOTA",
            api_available=True,
            browser_eligible=False,
            browser_reachable=False,
        )
    ) == (
        RecoveryAction.ROTATE_PROVIDER,
        RecoveryAction.WAIT_AND_RETRY_API,
    )


def test_browser_login_block_cannot_wait_when_api_is_available():
    assert allowed_recovery_actions(
        _incident(
            source_backend="browser",
            fault_class="BROWSER_LOGIN_REQUIRED",
            browser_login_available=False,
            api_available=True,
        )
    ) == (RecoveryAction.SWITCH_TO_API,)


def test_exhausted_browser_response_cannot_reopen_browser_retry():
    assert allowed_recovery_actions(
        _incident(
            source_backend="browser",
            fault_class="TIMEOUT",
            browser_eligible=False,
            api_available=True,
        )
    ) == (RecoveryAction.SWITCH_TO_API,)
    assert allowed_recovery_actions(
        _incident(recovery_cycle=25, max_recovery_cycles=24)
    ) == (RecoveryAction.WAIT_AND_RETRY_API,)


def test_browser_outage_waits_for_configured_api_inventory_to_cool():
    incident = _incident(
        source_backend="browser",
        fault_class="BROWSER_OFFLINE",
        api_available=False,
        api_configured=True,
        browser_eligible=False,
        browser_reachable=False,
    )
    assert allowed_recovery_actions(incident) == (
        RecoveryAction.WAIT_AND_RETRY_API,
    )


def test_api_probe_delay_is_bounded_exponential():
    assert _recovery_api_probe_delay({"recovery_api_probe_count": 0}) == 60
    assert _recovery_api_probe_delay({"recovery_api_probe_count": 1}) == 120
    assert _recovery_api_probe_delay({"recovery_api_probe_count": 9}) == 1800
    assert _recovery_api_probe_delay(
        {"recovery_api_probe_count": 0},
        requested_wait_seconds=300,
    ) == 300


def test_browser_handoff_marker_does_not_hide_api_from_supervisor(monkeypatch):
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks.has_active_key",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._browser_login_blocked_for_stage",
        lambda *_args, **_kwargs: False,
    )
    project = SimpleNamespace(
        id=176,
        project_key="cf_browser_api_return",
        config_json={},
        state_json={"video_variant_pipeline": {"active_index": 5}},
        status="waiting_bridge",
    )
    stage = SimpleNamespace(
        id=2613,
        stage="VISUAL_PREVIEW",
        attempt=8,
        input_json={
            "variant_index": 5,
            "execution_backend": "browser",
            "api_fallback_to_browser": True,
        },
    )

    incident = _recovery_supervisor_incident(
        object(),
        project,
        stage,
        source_backend="browser",
        reason="CHATGPT_SESSION_LOGIN_REQUIRED",
    )

    assert incident.api_available is True
    assert incident.browser_login_available is False
    assert allowed_recovery_actions(incident) == (
        RecoveryAction.SWITCH_TO_API,
    )


def test_automatic_quality_pause_is_not_misclassified_as_manual(monkeypatch):
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks.has_active_key",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._browser_login_blocked_for_stage",
        lambda *_args, **_kwargs: False,
    )
    project = SimpleNamespace(
        id=177,
        project_key="cf_automatic_quality_pause",
        config_json={"manual_paused": False},
        state_json={
            "video_variant_pipeline": {"active_index": 1},
            "pause_reason_code": "creative_visual_replan_exhausted",
        },
        status="paused",
    )
    stage = SimpleNamespace(
        id=2614,
        stage="VISUAL_PREVIEW",
        attempt=4,
        instruction="Repair the failed visual while preserving the user intent.",
        input_json={
            "variant_index": 1,
            "execution_backend": "api",
            "api_route": "flow2api:nano_banana_pro",
        },
    )

    incident = _recovery_supervisor_incident(
        object(),
        project,
        stage,
        source_backend="api",
        reason="PUBLIC_ERROR_UNSAFE_GENERATION",
    )

    assert incident.manual_pause is False
    assert incident.active_input_summary.startswith("Repair the failed visual")
    assert allowed_recovery_actions(incident) == (
        RecoveryAction.SEMANTIC_PROMPT_REPAIR,
    )


def test_api_retry_transition_clears_browser_authority_and_capture_replay():
    output = _api_retry_input_after_recovery(
        {
            "execution_backend": "browser",
            "api_force_browser_fallback": True,
            "api_fallback_to_browser": True,
            "browser_slot": "br_test",
            "browser_bridge_id": "br_test",
            "browser_cdp_url": "http://127.0.0.1:9327",
            "replayed_durable_response_capture": True,
            "retry_after": "2026-08-06T02:23:00",
            "run_token": "stale",
        },
        api_route="toapis:text",
        decision_action="SWITCH_TO_API",
    )

    assert output["execution_backend"] == "api"
    assert output["api_route"] == "toapis:text"
    assert output["api_force_browser_fallback"] is False
    assert output["discard_durable_response_capture"] is True
    assert output["force_fresh_response"] is True
    assert "browser_slot" not in output
    assert "browser_cdp_url" not in output
    assert "replayed_durable_response_capture" not in output
    assert "retry_after" not in output
    assert "run_token" not in output


def test_waiting_bridge_returns_to_current_api_authority(monkeypatch):
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._content_factory_api_route",
        lambda *_args, **_kwargs: "flow2api:nano_banana_pro",
    )

    output = _waiting_bridge_api_retry_input(
        object(),
        stage="VISUAL_PREVIEW",
        stage_input={
            "execution_backend": "api",
            "api_route": "flow2api:nano_banana_pro",
            "visual_api": {
                "all_providers_exhausted": True,
                "all_providers_exhausted_at": "2026-08-06T02:23:00",
            },
        },
    )

    assert output is not None
    assert output["execution_backend"] == "api"
    assert output["api_route"] == "flow2api:nano_banana_pro"
    assert "all_providers_exhausted" not in output["visual_api"]
    assert "all_providers_exhausted_at" not in output["visual_api"]


def test_image_api_return_starts_a_fresh_provider_recovery_epoch():
    output = _api_retry_input_after_recovery(
        {
            "execution_backend": "browser",
            "api_fallback_to_browser": True,
            "visual_api_skip_bandianwa": True,
            "visual_api": {
                "account_quota_exhausted": True,
                "api_recovery_epoch": 3,
                "provider_retry_generation": 4,
                "provider_failures": {
                    "sub2api": {
                        "account_quota_exhausted": True,
                        "error": "No available compatible accounts",
                    },
                    "toapis": {
                        "account_quota_exhausted": True,
                        "error": "quota_not_enough",
                    },
                },
                "all_providers_exhausted": True,
                "all_providers_exhausted_at": "2026-08-06T02:23:00",
                "boards": {
                    "1": {
                        "status": "failed",
                        "task_id": "old-provider-task",
                        "last_error": "quota_not_enough",
                        "prompt_digest": "same-prompt",
                    }
                },
            },
        },
        api_route="bandianwa:gpt-image-2",
        decision_action="SWITCH_TO_API",
    )

    assert output["visual_api_skip_bandianwa"] is False
    assert output["visual_api"]["api_recovery_epoch"] == 4
    assert "account_quota_exhausted" not in output["visual_api"]
    assert "provider_retry_generation" not in output["visual_api"]
    assert "all_providers_exhausted" not in output["visual_api"]
    assert "all_providers_exhausted_at" not in output["visual_api"]
    assert output["visual_api"]["boards"]["1"] == {
        "status": "pending",
        "prompt_digest": "same-prompt",
    }
    assert output["visual_api_failure_history"] == [{
        "api_recovery_epoch": 3,
        "providers": {
            "sub2api": {
                "account_quota_exhausted": True,
                "error": "No available compatible accounts",
            },
            "toapis": {
                "account_quota_exhausted": True,
                "error": "quota_not_enough",
            },
        },
        "all_providers_exhausted_at": "2026-08-06T02:23:00",
        "archived_at": output["visual_api_failure_history"][0]["archived_at"],
    }]


def test_visual_api_recovery_epoch_is_bounded_and_tolerates_bad_state():
    assert _visual_api_recovery_epoch(
        {"visual_api": {"api_recovery_epoch": "3"}}
    ) == 3
    assert _visual_api_recovery_epoch(
        {"visual_api": {"api_recovery_epoch": "not-a-number"}}
    ) == 0
    assert _visual_api_recovery_epoch(None) == 0


def test_nano_image_api_recovery_clears_exact_route_failures_and_keeps_completed():
    output = _api_retry_input_after_recovery(
        {
            "execution_backend": "browser",
            "visual_api": {
                "api_recovery_epoch": 1,
                "route_failures": {
                    "sub2api:gpt-image-2": {"retry_budget_exhausted": True},
                    "flow2api:nano_banana_pro": {"retry_budget_exhausted": True},
                },
                "boards": {
                    "1": {
                        "status": "completed",
                        "output_path": "/tmp/already-complete.png",
                    },
                    "2": {
                        "status": "failed",
                        "task_id": "old-provider-task",
                    },
                },
            },
        },
        api_route="flow2api:nano_banana_pro",
        decision_action="SWITCH_TO_API",
    )

    assert output["visual_api"]["api_recovery_epoch"] == 2
    assert "route_failures" not in output["visual_api"]
    assert output["visual_api"]["boards"]["1"]["status"] == "completed"
    assert output["visual_api"]["boards"]["2"] == {"status": "pending"}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("CREATIVE_REVIEW_ROUTING_FAILED: NETWORK", "NETWORK"),
        ("CHATGPT_UPLOAD_LIMIT: maximum of 0 files", "BROWSER_UPLOAD_UNAVAILABLE"),
        ("CHATGPT_SESSION_LOGIN_REQUIRED", "BROWSER_LOGIN_REQUIRED"),
        ("CONTENT_BROWSER_BRIDGE_REQUIRED: 请先在当前电脑创建并连接浏览器桥", "BROWSER_OFFLINE"),
        ("provider says insufficient balance", "ACCOUNT_QUOTA"),
        ('provider says {"code":"quota_not_enough"}', "ACCOUNT_QUOTA"),
        ("provider says insufficient_user_quota", "ACCOUNT_QUOTA"),
        ("403 PERMISSION_DENIED: Verify your account to continue", "AUTH"),
        ("explicit prompt policy violation", "PROMPT_POLICY"),
        ("PUBLIC_ERROR_UNSAFE_GENERATION", "PROMPT_POLICY"),
    ],
)
def test_fault_classification(message: str, expected: str):
    assert classify_recovery_fault(message) == expected


def test_text_api_network_fault_enters_supervisor_on_first_failure():
    assert _text_api_fault_needs_recovery_supervisor(
        api_route="toapis:text",
        stage="CREATIVE_REVIEW",
        error=RuntimeError("CREATIVE_REVIEW_ROUTING_FAILED: NETWORK"),
    ) is True


def test_text_api_contract_failure_stays_in_semantic_repair_path():
    assert _text_api_fault_needs_recovery_supervisor(
        api_route="toapis:text",
        stage="CREATIVE_REVIEW",
        error=ValueError(
            "CREATIVE_REVIEW reference_checks contract incomplete"
        ),
    ) is False


def test_visual_account_quota_is_delegated_and_executes_provider_rotation(monkeypatch):
    project = type("Project", (), {})()
    stage = type("Stage", (), {})()
    stage.stage = "VISUAL_PREVIEW"
    stage.input_json = {
        "execution_backend": "api",
        "api_route": "toapis:gpt-image-2",
        "visual_api": {"account_quota_exhausted": True},
    }
    rotated = {}

    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._supervised_recovery_decision",
        lambda *_args, **_kwargs: (
            _incident(stage="VISUAL_PREVIEW", fault_class="ACCOUNT_QUOTA"),
            SimpleNamespace(
                action=RecoveryAction.ROTATE_PROVIDER,
                reason_code="ACCOUNT_QUOTA",
                decision_source="model",
                wait_seconds=30,
            ),
        ),
    )
    def _rotate(_db, _project, _stage, *, decision, reason):
        rotated.update({"decision": decision.action, "reason": reason})
        return {"status": "provider_rotation_scheduled"}

    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._schedule_supervised_provider_rotation",
        _rotate,
    )

    result = _handoff_api_stage_to_browser(
        object(),
        project,
        stage,
        reason="Every available image API exhausted its bounded provider retry budget.",
    )

    assert result == {"status": "provider_rotation_scheduled"}
    assert rotated["decision"] == RecoveryAction.ROTATE_PROVIDER
    assert "retry budget" in rotated["reason"]


def test_provider_rotation_keeps_completed_board_and_changes_exact_route(monkeypatch):
    class _Db:
        def __init__(self):
            self.commits = 0

        def add(self, _value):
            return None

        def commit(self):
            self.commits += 1

    project = SimpleNamespace(
        id=170,
        project_key="cf_provider_rotation",
        status="failed",
        current_stage="VISUAL_PREVIEW",
        last_error="",
    )
    stage = SimpleNamespace(
        id=2511,
        stage="VISUAL_PREVIEW",
        status="failed",
        celery_task_id="old-delivery",
        started_at=object(),
        completed_at=object(),
        error_message="",
        input_json={
            "execution_backend": "api",
            "api_route": "toapis:gpt-image-2",
            "visual_api": {
                "provider": "toapis",
                "boards": {
                    "1": {"status": "completed", "task_id": "paid-ok"},
                    "2": {"status": "failed", "task_id": "paid-failed"},
                },
            },
        },
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._next_visual_api_route",
        lambda *_args, **_kwargs: "flow2api:nano_banana_pro",
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks.hibernate_project_browser_slot_for_api_video",
        lambda *_args, **_kwargs: None,
    )
    decision = SimpleNamespace(
        action=RecoveryAction.ROTATE_PROVIDER,
        reason_code="ACCOUNT_QUOTA",
        decision_source="model",
        wait_seconds=30,
    )

    result = _schedule_supervised_provider_rotation(
        _Db(),
        project,
        stage,
        decision=decision,
        reason="insufficient quota",
    )

    assert result["to_route"] == "flow2api:nano_banana_pro"
    assert stage.input_json["api_route"] == "flow2api:nano_banana_pro"
    assert stage.input_json["visual_api"]["boards"]["1"]["task_id"] == "paid-ok"
    assert stage.input_json["visual_api"]["boards"]["2"]["task_id"] is None
    assert stage.status == "retrying"


def test_exhausted_visual_route_cannot_be_reopened_by_browser_recovery(monkeypatch):
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks.has_active_key",
        lambda *_args, **_kwargs: True,
    )

    assert _content_factory_api_route(
        object(),
        "VISUAL_PREVIEW",
        {
            "execution_backend": "api",
            "api_route": "toapis:gpt-image-2",
            "visual_api": {"account_quota_exhausted": True},
        },
    ) is None

    # Reversal is explicit and only granted to the Recovery Supervisor after
    # the browser itself is known unusable. Ordinary routing and self-heal
    # still respect the durable API-to-browser handoff above.
    assert _content_factory_api_route(
        object(),
        "VISUAL_PREVIEW",
        {
            "execution_backend": "browser",
            "api_force_browser_fallback": True,
            "visual_api": {"account_quota_exhausted": True},
        },
        allow_browser_handoff_reversal=True,
    ) == "sub2api:gpt-image-2"
    # The supervisor decides before browser handoff markers are necessarily
    # persisted. Explicit reversal still starts a fresh provider epoch.
    assert _content_factory_api_route(
        object(),
        "VISUAL_PREVIEW",
        {
            "execution_backend": "api",
            "api_route": "toapis:gpt-image-2",
            "visual_api": {
                "provider": "toapis",
                "account_quota_exhausted": True,
            },
        },
        allow_browser_handoff_reversal=True,
    ) == "sub2api:gpt-image-2"
    assert _content_factory_api_route(
        object(),
        "VISUAL_PREVIEW",
        {
            "execution_backend": "browser",
            "api_force_browser_fallback": True,
            "visual_api": {
                "provider": "bandianwa",
                "account_quota_exhausted": True,
            },
        },
        allow_browser_handoff_reversal=True,
    ) == "sub2api:gpt-image-2"
    assert _content_factory_api_route(
        object(),
        "CREATIVE_REVIEW",
        {
            "execution_backend": "browser",
            "api_force_browser_fallback": True,
        },
    ) is None


def test_api_supervisor_wait_starts_new_epoch_when_browser_state_is_unknown(
    monkeypatch,
):
    project = SimpleNamespace(id=185, project_key="cf_wait", config_json={})
    stage = SimpleNamespace(
        id=3118,
        stage="VISUAL_PREVIEW",
        input_json={
            "api_route": "toapis:gpt-image-2",
            "visual_api": {"account_quota_exhausted": True},
        },
    )
    decision = SimpleNamespace(action=RecoveryAction.WAIT_AND_RETRY_API)
    incident = SimpleNamespace(browser_login_available=None)
    captured = {}

    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._browser_login_blocked_for_stage",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._browser_bridge_fresh_for_stage",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._browser_cdp_reachable_for_stage",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._supervised_recovery_decision",
        lambda *_args, **_kwargs: (incident, decision),
    )

    def _schedule(_db, _project, _stage, *, decision, allow_browser_handoff_reversal):
        captured["decision"] = decision.action
        captured["allow_reversal"] = allow_browser_handoff_reversal
        return {"status": "cooled_api_retry"}

    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._schedule_supervised_api_retry",
        _schedule,
    )

    result = _handoff_api_stage_to_browser(
        object(),
        project,
        stage,
        reason="ToAPIs quota_not_enough",
    )

    assert result == {"status": "cooled_api_retry"}
    assert captured == {
        "decision": RecoveryAction.WAIT_AND_RETRY_API,
        "allow_reversal": True,
    }


def test_no_current_api_route_schedules_probe_instead_of_pause(monkeypatch):
    class _Db:
        def __init__(self):
            self.commits = 0

        def add(self, _value):
            return None

        def commit(self):
            self.commits += 1

    project = SimpleNamespace(
        id=187,
        project_key="cf_probe",
        status="failed",
        current_stage="VISUAL_PREVIEW",
        last_error="",
    )
    stage = SimpleNamespace(
        id=3288,
        stage="VISUAL_PREVIEW",
        status="failed",
        celery_task_id="stale",
        started_at=object(),
        completed_at=object(),
        error_message="",
        input_json={
            "execution_backend": "api",
            "api_route": "flow2api:nano_banana_pro",
        },
    )
    decision = SimpleNamespace(
        action=RecoveryAction.WAIT_AND_RETRY_API,
        reason_code="TRANSIENT_API_NETWORK",
        decision_source="model",
        wait_seconds=180,
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks._content_factory_api_route",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks.hibernate_project_browser_slot_for_api_video",
        lambda *_args, **_kwargs: None,
    )

    result = _schedule_supervised_api_retry(
        _Db(),
        project,
        stage,
        decision=decision,
        allow_browser_handoff_reversal=True,
    )

    assert result["status"] == "recovery_supervisor_api_probe_scheduled"
    assert stage.status == "retrying"
    assert stage.input_json["recovery_api_probe_pending"] is True
    assert stage.input_json["api_route"] is None
    assert project.status == "queued"


def test_single_writer_deduplicates_a_live_recovery_cooldown(monkeypatch):
    class _Session:
        def __init__(self):
            self.commits = 0

        def refresh(self, _value):
            return None

        def add(self, _value):
            return None

        def commit(self):
            self.commits += 1

    class _Lock:
        def __init__(self, **_kwargs):
            return None

        def acquire(self, **_kwargs):
            return True

        def verify_ownership(self):
            return True

        def release(self):
            return True

    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks.Session",
        _Session,
    )
    monkeypatch.setattr(
        "app.tasks.hermes_agent.content_factory_tasks.RedisDistributedLock",
        _Lock,
    )
    project = SimpleNamespace(
        id=187,
        project_key="cf_single_writer",
        config_json={},
        state_json={"video_variant_pipeline": {"active_index": 1}},
    )
    stage = SimpleNamespace(
        id=3288,
        stage="VISUAL_PREVIEW",
        status="failed",
        input_json={"variant_index": 1},
    )
    calls = {"count": 0}

    def execute():
        calls["count"] += 1
        stage.status = "retrying"
        stage.input_json = {
            **dict(stage.input_json or {}),
            "retry_after": "2999-01-01T00:00:00",
        }
        return {"status": "recovery_supervisor_api_probe_scheduled"}

    first = _run_single_writer_recovery_transition(
        _Session(),
        project,
        stage,
        source_backend="api",
        reason="temporary upstream outage",
        execute=execute,
    )
    second = _run_single_writer_recovery_transition(
        _Session(),
        project,
        stage,
        source_backend="browser",
        reason="secondary browser bridge offline",
        execute=execute,
    )

    assert first["status"] == "recovery_supervisor_api_probe_scheduled"
    assert second["status"] == "recovery_supervisor_transition_already_scheduled"
    assert calls["count"] == 1
    assert _recovery_transition_has_live_cooldown(
        stage,
        now=datetime(2026, 8, 9),
    ) is True


def test_single_writer_deduplicates_recent_terminal_transition():
    stage = SimpleNamespace(
        status="failed",
        input_json={
            "last_recovery_supervisor_transition": {
                "result_status": "browser_fallback_queued",
                "recorded_at": "2026-08-09T08:00:00",
            },
        },
    )

    assert _recovery_transition_has_live_cooldown(
        stage,
        now=datetime(2026, 8, 9, 8, 4, 59),
    ) is True
    assert _recovery_transition_has_live_cooldown(
        stage,
        now=datetime(2026, 8, 9, 8, 5, 1),
    ) is False


def test_legacy_reasonless_automatic_pause_is_recoverable():
    project = SimpleNamespace(
        state_json={
            "last_recovery_supervisor_decision": {
                "action": "WAIT_FOR_BROWSER",
            },
        },
    )
    stage = SimpleNamespace(
        status="paused",
        error_message=(
            "Recovery Supervisor selected API, but no enabled API route remains."
        ),
    )

    assert _legacy_reasonless_recovery_pause(project, stage) is True
    project.state_json = {"pause_reason_code": "manual"}
    assert _legacy_reasonless_recovery_pause(project, stage) is False
