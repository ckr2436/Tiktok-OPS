from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from enum import Enum
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field, model_validator

from app.services.hermes_agent.client import (
    HermesContentRecoverySupervisorClient,
    extract_output_text,
)
from app.services.hermes_agent.content_autonomy import (
    is_external_operator_blocker,
)


RECOVERY_SUPERVISOR_POLICY_VERSION = "content-recovery-supervisor-v5-single-writer-probes"


class RecoveryAction(str, Enum):
    WAIT_AND_RETRY_API = "WAIT_AND_RETRY_API"
    SWITCH_TO_API = "SWITCH_TO_API"
    SWITCH_TO_BROWSER = "SWITCH_TO_BROWSER"
    RETRY_BROWSER = "RETRY_BROWSER"
    WAIT_FOR_BROWSER = "WAIT_FOR_BROWSER"
    ROTATE_PROVIDER = "ROTATE_PROVIDER"
    SEMANTIC_PROMPT_REPAIR = "SEMANTIC_PROMPT_REPAIR"
    RECOMPILE_STAGE_INPUT = "RECOMPILE_STAGE_INPUT"
    RECONCILE_LATE_RESULT = "RECONCILE_LATE_RESULT"
    PAUSE_NONRETRYABLE = "PAUSE_NONRETRYABLE"


class RecoveryIncident(BaseModel):
    incident_id: str = Field(min_length=8, max_length=96)
    project_id: int = Field(gt=0)
    stage_id: int = Field(gt=0)
    stage: str = Field(min_length=1, max_length=64)
    variant_index: int = Field(ge=0, le=100000)
    source_backend: str = Field(pattern="^(api|browser)$")
    fault_class: str = Field(min_length=1, max_length=64)
    fault_code: str = Field(default="", max_length=128)
    attempt_count: int = Field(default=0, ge=0, le=100000)
    recovery_cycle: int = Field(default=0, ge=0, le=100000)
    max_recovery_cycles: int = Field(default=24, ge=1, le=1000)
    api_available: bool = False
    # ``api_available`` means a route is eligible at this instant.  A route
    # inventory can still exist while every account/circuit is cooling.  The
    # recovery role needs that distinction or a secondary browser outage can
    # incorrectly replace a transient API fault with WAIT_FOR_BROWSER.
    api_configured: bool = False
    browser_eligible: bool = False
    browser_reachable: bool | None = None
    browser_upload_available: bool | None = None
    browser_login_available: bool | None = None
    ambiguous_billable_submission: bool = False
    manual_pause: bool = False
    recent_actions: list[str] = Field(default_factory=list, max_length=12)
    error_summary: str = Field(default="", max_length=1200)
    active_input_summary: str = Field(default="", max_length=4000)
    input_fingerprint: str = Field(default="", max_length=64)
    active_route: str = Field(default="", max_length=255)
    provider_attempts: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    evidence_manifest: list[dict[str, Any]] = Field(default_factory=list, max_length=12)


class RecoveryDecision(BaseModel):
    action: RecoveryAction
    wait_seconds: int = Field(default=0, ge=0, le=3600)
    reason_code: str = Field(min_length=1, max_length=96)
    rationale: str = Field(min_length=1, max_length=600)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    policy_version: str = RECOVERY_SUPERVISOR_POLICY_VERSION
    decision_source: str = Field(default="model", pattern="^(model|safe_fallback)$")
    model: str | None = Field(default=None, max_length=255)
    latency_ms: int | None = Field(default=None, ge=0)
    diagnosis: str = Field(default="", max_length=1000)
    repair_directive: str = Field(default="", max_length=1600)
    evidence_used: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def normalize_wait(self) -> "RecoveryDecision":
        if self.action in {
            RecoveryAction.WAIT_AND_RETRY_API,
            RecoveryAction.WAIT_FOR_BROWSER,
        }:
            self.wait_seconds = max(15, int(self.wait_seconds or 0))
        else:
            self.wait_seconds = max(0, int(self.wait_seconds or 0))
        return self


def classify_recovery_fault(message: str, *, code: str | None = None) -> str:
    """Convert provider/browser prose into a small non-secret incident class."""

    value = f"{code or ''} {message or ''}".strip().lower()
    if any(marker in value for marker in (
        "chatgpt_upload_limit",
        "maximum of 0 files",
        "upload up to 0 files",
        "最多可上传 0 个文件",
        "一次最多可上传 0 个文件",
    )):
        return "BROWSER_UPLOAD_UNAVAILABLE"
    if any(marker in value for marker in (
        "chatgpt_session_login_required",
        "content_browser_login_required",
        "login required",
        "not logged in",
        "未登录",
        "请登录",
    )):
        return "BROWSER_LOGIN_REQUIRED"
    if any(marker in value for marker in (
        "content_browser_bridge_required",
        "content_browser_bridge_offline",
        "content_browser_capacity_full",
        "content_browser_locked_slot_unavailable",
        "browser bridge offline",
        "browser bridge unavailable",
        "browser bridge required",
        "connection refused",
        "failed to connect to cdp",
        "all cdp discovery methods failed",
        "browser_bridge_route_missing",
        "请先在当前电脑创建并连接浏览器桥",
    )):
        return "BROWSER_OFFLINE"
    if any(marker in value for marker in (
        "prompt policy violation",
        "prompt violation",
        "content policy violation",
        "safety policy violation",
        "moderation_blocked",
        "unsafe_generation",
        "public_error_unsafe_generation",
        "unsafe generation",
        "提示词违规",
        "内容违规",
    )):
        return "PROMPT_POLICY"
    if any(marker in value for marker in (
        "insufficient balance",
        "insufficient quota",
        "insufficient_user_quota",
        "quota_not_enough",
        "quota is not enough",
        "remaining balance",
        "pre-charge failed",
        "余额不足",
        "预扣费额度失败",
    )):
        return "ACCOUNT_QUOTA"
    if any(marker in value for marker in (
        "unauthorized",
        "forbidden",
        "invalid api key",
        "authentication",
        "permission_denied",
        "verify your account",
    )):
        return "AUTH"
    if any(marker in value for marker in (
        "semantic",
        "contract incomplete",
        "schema",
        "validation",
        "required field",
    )):
        return "OUTPUT_CONTRACT"
    # A provider rejecting the submitted request is not a transport outage.
    # Classify explicit 4xx request failures before the broad ``upstream``
    # network marker so the supervisor may inspect the evidence and choose a
    # provider rotation, semantic repair, or stage-input recompilation instead
    # of resending the identical invalid request forever.
    if any(marker in value for marker in (
        "http 400",
        "http 404",
        "http 409",
        "http 422",
        "bad request",
        "invalid request",
        "request rejected",
        "unprocessable entity",
    )):
        return "API_REQUEST_REJECTED"
    if any(marker in value for marker in (
        "rate limit",
        "too many requests",
        "request too frequent",
        "请求过于频繁",
    )):
        return "RATE_LIMIT"
    if any(marker in value for marker in (
        "timeout",
        "timed out",
        "deadline exceeded",
    )):
        return "TIMEOUT"
    if any(marker in value for marker in (
        "network",
        "connection reset",
        "connection error",
        "temporarily unavailable",
        "service unavailable",
        "upstream",
    )):
        return "NETWORK"
    return "UNKNOWN"


def allowed_recovery_actions(incident: RecoveryIncident) -> tuple[RecoveryAction, ...]:
    """Build the model's action envelope from deterministic safety facts."""

    if incident.manual_pause:
        return (RecoveryAction.PAUSE_NONRETRYABLE,)
    if incident.ambiguous_billable_submission:
        return (RecoveryAction.RECONCILE_LATE_RESULT,)
    if incident.recovery_cycle > incident.max_recovery_cycles:
        # The bounded cycle protects the upstream from a hot loop.  It is a
        # circuit-breaker boundary, not an operator boundary: after a longer
        # cooldown the scheduler may try a fresh provider/account/candidate.
        if (
            incident.source_backend == "browser"
            and not incident.api_available
            and not incident.api_configured
        ):
            return (RecoveryAction.WAIT_FOR_BROWSER,)
        return (RecoveryAction.WAIT_AND_RETRY_API,)
    if incident.fault_class in {"ACCOUNT_QUOTA", "AUTH", "RATE_LIMIT"}:
        # Exhausting the current API account does not make an explicitly
        # logged-out browser usable.  When the routing layer can still expose
        # a compatible API recovery route, cool down and rotate/retry that
        # chain instead of waking Chrome or permanently pausing the project.
        if incident.api_available:
            return (
                RecoveryAction.ROTATE_PROVIDER,
                RecoveryAction.WAIT_AND_RETRY_API,
            )
        if incident.browser_eligible:
            return (RecoveryAction.SWITCH_TO_BROWSER,)
        return (RecoveryAction.WAIT_AND_RETRY_API,)
    if incident.fault_class == "PROMPT_POLICY":
        # Policy failures are repairable content faults.  A fresh semantic
        # rewrite is safer than sending the identical prompt to a browser.
        if incident.stage == "VISUAL_PREVIEW":
            return (RecoveryAction.SEMANTIC_PROMPT_REPAIR,)
        return (RecoveryAction.RECOMPILE_STAGE_INPUT,)
    if incident.fault_class == "OUTPUT_CONTRACT":
        return (RecoveryAction.RECOMPILE_STAGE_INPUT,)
    if incident.fault_class == "API_REQUEST_REJECTED":
        actions = []
        if incident.api_available:
            actions.append(RecoveryAction.ROTATE_PROVIDER)
        if incident.stage == "VISUAL_PREVIEW":
            actions.append(RecoveryAction.SEMANTIC_PROMPT_REPAIR)
        actions.append(RecoveryAction.RECOMPILE_STAGE_INPUT)
        # The model may still judge a single provider-side 4xx to be a stale
        # account/capability view.  A cooled retry remains available, but it is
        # no longer the only possible action.
        if incident.api_available:
            actions.append(RecoveryAction.WAIT_AND_RETRY_API)
        return tuple(dict.fromkeys(actions))

    actions: list[RecoveryAction] = []
    if incident.source_backend == "browser":
        browser_blocked = incident.fault_class in {
            "BROWSER_UPLOAD_UNAVAILABLE",
            "BROWSER_LOGIN_REQUIRED",
            "BROWSER_OFFLINE",
        }
        if incident.api_available:
            actions.append(RecoveryAction.SWITCH_TO_API)
        elif incident.api_configured and browser_blocked:
            # The API inventory is temporarily cooling rather than absent.
            # Keep the browser dormant and let the scheduler probe the API
            # pool after its durable cooldown.
            return (RecoveryAction.WAIT_AND_RETRY_API,)
        # A known-unusable browser must not remain a competing action when an
        # API route exists.  Keeping WAIT_FOR_BROWSER in this envelope allowed
        # the model to strand projects indefinitely on a logged-out or unsafe
        # login page even though the administrator had an enabled API route.
        if browser_blocked and incident.api_available:
            return tuple(actions)
        if browser_blocked:
            actions.append(RecoveryAction.WAIT_FOR_BROWSER)
        elif incident.browser_eligible:
            actions.append(RecoveryAction.RETRY_BROWSER)
        if not actions:
            if is_external_operator_blocker(
                fault_class=incident.fault_class,
            ):
                actions.append(RecoveryAction.PAUSE_NONRETRYABLE)
            else:
                actions.append(RecoveryAction.WAIT_FOR_BROWSER)
        return tuple(dict.fromkeys(actions))

    if incident.api_available:
        actions.append(RecoveryAction.WAIT_AND_RETRY_API)
    browser_usable = bool(
        incident.browser_eligible
        and incident.browser_upload_available is not False
        and incident.browser_login_available is not False
        and not incident.ambiguous_billable_submission
    )
    if browser_usable:
        actions.append(RecoveryAction.SWITCH_TO_BROWSER)
    if not actions:
        if is_external_operator_blocker(
            fault_class=incident.fault_class,
        ):
            actions.append(RecoveryAction.PAUSE_NONRETRYABLE)
        else:
            # No route is healthy *now*.  Persist a cooled probe instead of
            # turning a transient routing outage into a human ticket.
            actions.append(RecoveryAction.WAIT_AND_RETRY_API)
    return tuple(dict.fromkeys(actions))


def _safe_fallback_decision(
    incident: RecoveryIncident,
    allowed: tuple[RecoveryAction, ...],
    *,
    reason: str,
) -> RecoveryDecision:
    for preferred in (
        RecoveryAction.SEMANTIC_PROMPT_REPAIR,
        RecoveryAction.RECOMPILE_STAGE_INPUT,
        RecoveryAction.RECONCILE_LATE_RESULT,
        RecoveryAction.ROTATE_PROVIDER,
        RecoveryAction.WAIT_AND_RETRY_API,
        RecoveryAction.SWITCH_TO_API,
        RecoveryAction.WAIT_FOR_BROWSER,
        RecoveryAction.RETRY_BROWSER,
        RecoveryAction.SWITCH_TO_BROWSER,
        RecoveryAction.PAUSE_NONRETRYABLE,
    ):
        if preferred in allowed:
            action = preferred
            break
    else:  # pragma: no cover - allowed_recovery_actions is never empty
        action = RecoveryAction.PAUSE_NONRETRYABLE
    wait_seconds = 0
    if action == RecoveryAction.WAIT_AND_RETRY_API:
        wait_seconds = min(1800, 60 * max(1, min(10, incident.recovery_cycle + 1)))
    elif action == RecoveryAction.WAIT_FOR_BROWSER:
        wait_seconds = 300
    return RecoveryDecision(
        action=action,
        wait_seconds=wait_seconds,
        reason_code="RECOVERY_SUPERVISOR_SAFE_FALLBACK",
        rationale=str(reason or "Recovery model unavailable; applied safest allowed action.")[:600],
        confidence=1.0,
        decision_source="safe_fallback",
        diagnosis=str(reason or "")[:1000],
    )


def _evidence_data_url(path_value: str) -> str | None:
    """Return a bounded JPEG proxy; never send arbitrary files to Hermes."""

    path = Path(str(path_value or "")).resolve()
    if not path.is_file():
        return None
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=84, optimize=True)
    except (OSError, ValueError, UnidentifiedImageError):
        return None
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _recovery_input_items(
    packet_json: str,
    evidence_images: list[tuple[str, str]] | None,
) -> list[dict[str, Any]] | None:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": packet_json}]
    for label, path in list(evidence_images or [])[:8]:
        data_url = _evidence_data_url(path)
        if not data_url:
            continue
        content.append({
            "type": "input_text",
            "text": f"Failure evidence image: {str(label)[:160]}",
        })
        content.append({"type": "input_image", "image_url": data_url, "detail": "high"})
    return [{"role": "user", "content": content}] if len(content) > 1 else None


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("recovery supervisor response is not a JSON object")
    return value


async def decide_content_recovery(
    incident: RecoveryIncident,
    *,
    client: HermesContentRecoverySupervisorClient | None = None,
    evidence_images: list[tuple[str, str]] | None = None,
) -> RecoveryDecision:
    """Ask the stateless role for one bounded recommendation.

    A model outage or an out-of-policy answer never blocks self-heal.  The
    deterministic fallback prefers a cooled API retry and never invents an
    action outside the server-provided envelope.
    """

    allowed = allowed_recovery_actions(incident)
    instructions = (
        "You are Hermes Content Recovery Supervisor. Diagnose from the complete "
        "incident packet and any attached visual evidence. You never execute tools, "
        "submit media, or mutate a project. Choose exactly one "
        "action from allowed_actions. Prefer cooled API recovery for transient "
        "API faults. Use browser only when it is eligible and not known to be "
        "logged out or unable to upload. A browser upload/login/offline fault "
        "should return to API when API is available. A current-provider quota "
        "fault may use a cooled API retry only when that is the sole allowed "
        "action because the browser is known logged out. Upstream account "
        "authentication, quota, or verification failures may rotate through the "
        "enabled API priority chain. Prompt-policy or unsafe-generation faults "
        "must request a materially safer semantic rewrite, not replay identical "
        "inputs and never evade a safety policy. Output-contract faults recompile "
        "from the locked user intent. Ambiguous paid submissions must be reconciled "
        "before any new generation. Populate diagnosis, repair_directive, and "
        "evidence_used so the deterministic executor can audit the decision. "
        "Only missing human authority such as an interactive login/CAPTCHA or "
        "an explicit manual pause may require an operator. Return only strict JSON."
    )
    packet = {
        "policy_version": RECOVERY_SUPERVISOR_POLICY_VERSION,
        "incident": incident.model_dump(mode="json"),
        "allowed_actions": [action.value for action in allowed],
        "output_contract": RecoveryDecision.model_json_schema(),
    }
    digest = hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    runtime = client or HermesContentRecoverySupervisorClient()
    try:
        packet_json = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        payload, latency_ms = await runtime.create_response(
            input_text=packet_json,
            input_items=_recovery_input_items(packet_json, evidence_images),
            instructions=instructions,
            idempotency_key=f"gmv-content-recovery-{digest}",
            metadata={
                "agent_role": "content_recovery_supervisor",
                "request_id": incident.incident_id,
                "prompt_version": RECOVERY_SUPERVISOR_POLICY_VERSION,
            },
        )
        parsed = _parse_json_object(extract_output_text(payload))
        parsed["policy_version"] = RECOVERY_SUPERVISOR_POLICY_VERSION
        parsed["decision_source"] = "model"
        parsed["latency_ms"] = int(latency_ms)
        parsed["model"] = str(
            dict(payload.get("_gmv_meta") or {}).get("model") or runtime.model
        )[:255]
        decision = RecoveryDecision.model_validate(parsed)
        if decision.action not in allowed:
            raise ValueError(
                f"model selected disallowed recovery action {decision.action.value}"
            )
        return decision
    except Exception as exc:  # noqa: BLE001
        return _safe_fallback_decision(
            incident,
            allowed,
            reason=f"{type(exc).__name__}: {str(exc)[:420]}",
        )


__all__ = [
    "RECOVERY_SUPERVISOR_POLICY_VERSION",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryIncident",
    "allowed_recovery_actions",
    "classify_recovery_fault",
    "decide_content_recovery",
]
