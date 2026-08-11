from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import time
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.models.hermes_agent import HermesBrowserBridge
from app.data.models.kie_api import KieFile, KieTask
from app.services.doubao_provider.client import DoubaoProviderError, invoke_doubao_helper
from app.services.doubao_provider.pool import (
    DoubaoPoolBusyError,
    account_request_payload,
    claim_account,
    leased_account,
    record_submit_observation,
    release_account,
)
from app.services.doubao_provider.membership import FREE_DURATIONS
from app.services.ai_video.local_storage import (
    get_local_path,
    mark_result_file_pending,
    set_task_local_meta,
)
from app.services.ai_video.prompt_budget import (
    compact_structured_video_prompt,
    is_structured_video_prompt,
    localize_structured_video_prompt_for_doubao,
)
from app.services.ai_video.accounts import (
    DOUBAO_PROVIDER_KEY,
    provider_model_capabilities,
)
from app.services.ai_video.task_state import reset_video_task_for_retry


_ROTATE_ACCOUNT_ERROR_CODES = {
    "doubao_quota_exhausted",
    "doubao_captcha_required",
    "doubao_auth_required",
    "doubao_account_context_invalid",
    "doubao_risk_rate_limited",
    "doubao_region_restricted",
    "doubao_browser_unstable",
    "doubao_composer_unavailable",
    # The helper emits this only after proving that no Seedance task or
    # pollable conversation was created.  It is therefore safe to rotate to
    # another account in the same delivery without a duplicate paid submit.
    "doubao_submit_unconfirmed",
}
_RELEASE_ACCOUNT_ERROR_CODES = {
    *_ROTATE_ACCOUNT_ERROR_CODES,
    "doubao_membership_required",
    "doubao_face_ref_unsupported",
    "doubao_content_rejected",
    "doubao_text_only_response",
}

_OUTPUT_ASPECT_TOLERANCE = 0.08
_COMPOSER_WARMUP_RETRY_CODES = {
    "doubao_failed",
    "doubao_timeout",
    "doubao_browser_unstable",
    "doubao_composer_unavailable",
}


def _sanitized_submission_contract(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Accept only the non-secret AI Creation attestation from the helper."""
    raw = payload.get("submission_contract")
    if not isinstance(raw, Mapping):
        return None
    if (
        str(raw.get("surface") or "") != "ai_creation"
        or int(raw.get("ability_type") or 0) != 17
        or str(raw.get("model") or "") != "seedance_v2.0_mini"
    ):
        return None
    ratio = str(raw.get("ratio") or "")
    duration = int(raw.get("duration") or 0)
    if ratio not in {"9:16", "16:9", "1:1"} or not 4 <= duration <= 15:
        return None
    return {
        "surface": "ai_creation",
        "ability_type": 17,
        "model": "seedance_v2.0_mini",
        "ratio": ratio,
        "duration": duration,
        "reference_count": max(0, min(10, int(raw.get("reference_count") or 0))),
    }


def _provider_browser_generation_ready(
    meta: Mapping[str, Any], *, task_id: int
) -> bool:
    """Accept only the Bridge runtime opened for this exact production task."""
    generation = str(int(task_id))
    return bool(
        str(meta.get("doubao_pool_lease_task_id") or "") == generation
        and str(meta.get("doubao_provider_browser_task_id") or "") == generation
        and str(meta.get("doubao_browser_capture_id") or "") == generation
        and str(meta.get("doubao_browser_status") or "").lower() == "ready"
        and "doubao.com" in str(meta.get("doubao_page_url") or "").lower()
    )


def _provider_browser_generation_error(
    meta: Mapping[str, Any], *, task_id: int
) -> DoubaoProviderError | None:
    """Return an account-scoped terminal result for the exact browser run.

    The Bridge can prove that Chrome opened the requested Profile while also
    proving that the Profile is no longer authenticated.  Waiting until the
    generic browser timeout in that case misclassifies an account login result
    as a physical-device outage and opens the device circuit for every account
    on the same Windows host.  Only observations fenced to this task generation
    are actionable; stale reports from an earlier task remain harmless.
    """

    generation = str(int(task_id))
    if (
        str(meta.get("doubao_pool_lease_task_id") or "") != generation
        or str(meta.get("doubao_provider_browser_task_id") or "") != generation
        or str(meta.get("doubao_browser_capture_id") or "") != generation
    ):
        return None
    page_url = str(meta.get("doubao_page_url") or "").strip().lower()
    if "/security/doubao-region-ban" in page_url:
        return DoubaoProviderError(
            "当前豆包账号的固定网络出口不可用，正在切换账号。",
            code="doubao_region_restricted",
        )
    browser_status = str(meta.get("doubao_browser_status") or "").strip().lower()
    if browser_status == "login_required":
        return DoubaoProviderError(
            "当前豆包账号的浏览器登录态已失效，正在切换账号。",
            code="doubao_auth_required",
        )
    return None


def _restore_provider_browser_session(
    db: Session,
    account: HermesBrowserBridge,
) -> None:
    """Restore the leased Profile from the encrypted account capture once.

    A freshly restarted Chrome Profile can be cookie-empty while the account
    pool still owns a valid encrypted session capture.  Rehydrate that exact
    Profile before declaring the account logged out.  The helper receives
    secrets over stdin only and returns a redacted status envelope.
    """

    try:
        provider_context = account_request_payload(db, account)
    except (RuntimeError, ValueError) as exc:
        raise DoubaoProviderError(
            str(exc), code="doubao_account_context_invalid"
        ) from exc
    browser_cdp_url = str(account.cdp_url or "")
    # Do not keep an ORM transaction open while Playwright attaches to the
    # remote Profile. Browser reports must remain free to update this row.
    db.rollback()
    result = invoke_doubao_helper(
        {
            **provider_context,
            "action": "restore_session",
            "browser_cdp_url": browser_cdp_url,
        },
        timeout_seconds=75,
    )
    if str(result.get("status") or "").strip().lower() != "restored":
        raise DoubaoProviderError(
            "豆包账号浏览器登录态恢复失败，正在切换账号。",
            code="doubao_auth_required",
        )


def _release_provider_browser_hold_if_due(
    db: Session,
    account: HermesBrowserBridge,
    *,
    task_id: int,
    now: datetime | None = None,
    force: bool = False,
) -> bool:
    """Close only the short browser phase while preserving the remote lease."""
    meta = dict(account.meta_json or {})
    generation = str(int(task_id))
    if str(meta.get("doubao_provider_browser_task_id") or "") != generation:
        return False
    hold_raw = str(meta.get("doubao_provider_browser_hold_until") or "").strip()
    hold_until = None
    if hold_raw:
        try:
            hold_until = datetime.fromisoformat(hold_raw.replace("Z", "+00:00"))
            if hold_until.tzinfo is not None:
                hold_until = hold_until.astimezone().replace(tzinfo=None)
        except ValueError:
            hold_until = None
    current = now or datetime.now().astimezone().replace(tzinfo=None)
    if not force and (hold_until is None or hold_until > current):
        return False
    meta["doubao_provider_browser_task_id"] = None
    meta["doubao_provider_browser_hold_until"] = None
    meta["doubao_provider_submission_accepted_at"] = None
    account.meta_json = meta
    db.add(account)
    return True


def _wait_for_provider_browser_generation(
    db: Session, *, account_id: int, task_id: int, timeout_seconds: int = 75
) -> HermesBrowserBridge:
    """Fence a paid submit behind the exact Bridge browser generation."""
    generation = str(int(task_id))
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    restore_attempted = False
    restore_grace_until = 0.0
    while time.monotonic() < deadline:
        row = (
            db.query(HermesBrowserBridge)
            .filter(HermesBrowserBridge.id == int(account_id))
            .populate_existing()
            .one_or_none()
        )
        if row is None:
            raise DoubaoProviderError(
                "豆包生产账号已不存在。", code="doubao_account_context_invalid"
            )
        meta = dict(row.meta_json or {})
        if str(meta.get("doubao_pool_lease_task_id") or "") != generation:
            raise DoubaoProviderError(
                "豆包生产账号租约已被取消。", code="doubao_account_lease_expired"
            )
        if _provider_browser_generation_ready(meta, task_id=int(task_id)):
            return row
        generation_error = _provider_browser_generation_error(
            meta, task_id=int(task_id)
        )
        if generation_error is not None:
            if generation_error.code == "doubao_auth_required":
                if not restore_attempted:
                    restore_attempted = True
                    _restore_provider_browser_session(db, row)
                    restore_grace_until = min(
                        deadline,
                        time.monotonic() + 15,
                    )
                    time.sleep(0.5)
                    continue
                if time.monotonic() < restore_grace_until:
                    db.rollback()
                    time.sleep(0.5)
                    continue
            raise generation_error
        db.rollback()
        time.sleep(0.5)
    raise DoubaoProviderError(
        "豆包生产浏览器尚未完成当前任务的 Profile 切换。",
        code="doubao_browser_unstable",
    )


def _normalize_account_submit_error(
    exc: DoubaoProviderError,
    *,
    requested_duration: int,
) -> DoubaoProviderError:
    """Interpret an upgrade card inside the account, not as model truth.

    The web UI uses the same membership card when a free account has no
    remaining generation capacity.  For durations in the platform's verified
    free lane, this is an account-capacity result: cool that account and rotate
    the pool.  Only a duration outside the free lane is a terminal request-tier
    rejection.
    """

    if (
        str(exc.code or "").strip().lower()
        == "doubao_membership_required"
        and int(requested_duration) in set(FREE_DURATIONS)
    ):
        return DoubaoProviderError(
            "当前豆包账号的免费生成容量不可用，正在切换号池账号。",
            code="doubao_quota_exhausted",
        )
    return exc


def _ensure_live_video_composer(
    provider_context: Mapping[str, Any], *, browser_cdp_url: str
) -> None:
    """Wake and verify the exact leased Profile immediately before submit.

    A capability result stored minutes earlier cannot prove that a cold
    Windows Slot has finished reopening its browser and React composer.  Keep
    this quota-free probe inside the production account lease; the Bridge
    therefore leaves the same Profile online between readiness and the paid
    submit, eliminating the stop/start race without creating a remote task.
    """
    payload = {
        **dict(provider_context),
        "action": "probe",
        "browser_cdp_url": str(browser_cdp_url or ""),
    }
    last_error: DoubaoProviderError | None = None
    for attempt in range(2):
        try:
            result = invoke_doubao_helper(
                payload,
                timeout_seconds=max(
                    30,
                    int(settings.DOUBAO_COMPOSER_PROBE_TIMEOUT_SECONDS),
                ),
            )
            if str(result.get("status") or "").strip().lower() != "capable":
                raise DoubaoProviderError(
                    "豆包 Seedance 编辑器尚未就绪，系统将切换账号重试。",
                    code="doubao_composer_unavailable",
                )
            return
        except DoubaoProviderError as exc:
            last_error = exc
            if attempt == 0 and exc.code in _COMPOSER_WARMUP_RETRY_CODES:
                # Keep the same account lease and managed browser Profile.
                # The first probe may be the action that wakes a cold Slot;
                # the second observes the now-loaded composer without rotating
                # accounts or risking a duplicate paid generation request.
                time.sleep(5)
                continue
            raise
    raise last_error or DoubaoProviderError(
        "豆包 Seedance 编辑器能力探测失败。",
        code="doubao_capability_probe_failed",
    )


def _prompt(params: Mapping[str, Any], task: KieTask) -> str:
    for name in ("provider_prompt", "full_prompt", "prompt"):
        value = params.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(task.prompt or "").strip()


def _request(params: Mapping[str, Any], task: KieTask) -> dict[str, Any]:
    prompt = _prompt(params, task)
    if not prompt:
        raise DoubaoProviderError("豆包 Seedance 提示词必须为 1 到 500 字符。")
    provider_prompt_limit = int(
        provider_model_capabilities(DOUBAO_PROVIDER_KEY, task.model).get(
            "prompt_max_characters"
        )
        or 500
    )
    # Keep the existing 495-character signed application budget for cross-build
    # safety, but do not reserve or inject a visible mode command. The current
    # AI Creation page owns video intent through ability_type=17 and renders its
    # own ``生成视频：`` label.
    prompt_limit = max(1, min(provider_prompt_limit, 495))
    # Structured Content Factory prompts may be semantically compacted to the
    # provider's verified budget. Direct user prose is authoritative and must
    # never be silently truncated or rewritten.
    structured_prompt = is_structured_video_prompt(prompt)
    if structured_prompt:
        try:
            prompt = compact_structured_video_prompt(
                prompt,
                max_characters=prompt_limit,
            )
            prompt = localize_structured_video_prompt_for_doubao(prompt)
            if len(prompt) > prompt_limit:
                raise ValueError(
                    "localized structured provider prompt exceeds the "
                    f"declared {prompt_limit}-character prompt limit"
                )
        except ValueError as exc:
            code = (
                "doubao_prompt_contract_invalid"
                if "no approved dialogue line" in str(exc).lower()
                else "doubao_prompt_too_long"
            )
            raise DoubaoProviderError(str(exc), code=code) from exc
    elif len(prompt) > prompt_limit:
        raise DoubaoProviderError(
            f"豆包 Seedance 普通提示词不能超过 {prompt_limit} 字符。",
            code="doubao_prompt_too_long",
        )
    try:
        duration = int(params.get("seconds") or params.get("duration") or 4)
    except (TypeError, ValueError) as exc:
        raise DoubaoProviderError("豆包 Seedance 时长无效。") from exc
    ratio = str(params.get("aspect_ratio") or "9:16").strip()
    if duration not in set(range(4, 16)) or ratio not in {"9:16", "16:9", "1:1"}:
        raise DoubaoProviderError("豆包 Seedance 仅支持 4-15 秒和 9:16、16:9、1:1。")
    return {"prompt": prompt, "duration": duration, "ratio": ratio}


def _reference_paths(db: Session, task: KieTask) -> list[str]:
    references = (
        db.query(KieFile)
        .filter(
            KieFile.task_id == int(task.id),
            KieFile.workspace_id == int(task.workspace_id),
            KieFile.kind == "reference_upload",
        )
        .order_by(KieFile.id.asc())
        .all()
    )
    params = dict(task.input_json or {})
    if list(params.get("reference_videos") or []):
        raise DoubaoProviderError(
            "豆包自建 Seedance Mini 当前不支持参考视频。",
            code="doubao_reference_video_unsupported",
        )
    requested_refs = [
        ref
        for ref in list(params.get("reference_file_paths") or [])
        if isinstance(ref, Mapping) and str(ref.get("path") or "").strip()
    ]
    # The ordered task input is the user-visible @imageN authority. Database
    # ids normally happen to follow it, but edits, cloned retries and record
    # repair must never be allowed to silently renumber reference images.
    if requested_refs:
        by_path: dict[str, KieFile] = {}
        for reference in references:
            path = str(reference.file_url or "").strip()
            if not path or path in by_path:
                raise DoubaoProviderError(
                    "豆包参考图记录存在重复路径，无法保证 @imageN 顺序。",
                    code="doubao_reference_order_invalid",
                )
            by_path[path] = reference
        ordered: list[KieFile] = []
        for ref in requested_refs:
            path = str(ref.get("path") or "").strip()
            reference = by_path.get(path)
            if reference is None:
                raise DoubaoProviderError(
                    "豆包参考图顺序清单与任务文件不一致，请重新提交。",
                    code="doubao_reference_order_invalid",
                )
            ordered.append(reference)
        if len(ordered) != len(references):
            raise DoubaoProviderError(
                "豆包参考图记录包含未编号文件，无法保证 @imageN 顺序。",
                code="doubao_reference_order_invalid",
            )
        references = ordered
    if len(references) > 10:
        raise DoubaoProviderError(
            "豆包自建 Seedance Mini 当前最多支持 10 张参考图。",
            code="doubao_reference_limit",
        )
    paths: list[str] = []
    seen_digests: dict[str, int] = {}
    for position, reference in enumerate(references, start=1):
        local_path = get_local_path(reference)
        if local_path is None:
            raise DoubaoProviderError(
                "豆包参考图不在受管存储中。",
                code="doubao_reference_unavailable",
            )
        digest = hashlib.sha256()
        with local_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        if value in seen_digests:
            raise DoubaoProviderError(
                "豆包参考图第 "
                f"{position} 张与第 {seen_digests[value]} 张内容完全相同；"
                "为避免 @imageN 错位，任务未提交。",
                code="doubao_reference_duplicate",
            )
        seen_digests[value] = position
        paths.append(str(local_path))
    return paths


def _remember_failed_account(task: KieTask, bridge_id: str) -> None:
    """Fence one account from the rest of this logical task's retry round."""

    local = dict(dict(task.result_json or {}).get("__local") or {})
    failed = [
        str(value)
        for value in list(local.get("doubao_failed_account_bridge_ids") or [])
        if str(value).strip()
    ]
    value = str(bridge_id or "").strip()
    if value and value not in failed:
        failed.append(value)
    set_task_local_meta(task, doubao_failed_account_bridge_ids=failed[-32:])


def _start_submit_attempt(task: KieTask, account: HermesBrowserBridge) -> int:
    """Create one bounded, secret-free account submit audit row."""

    local = dict(dict(task.result_json or {}).get("__local") or {})
    attempts = [
        dict(item)
        for item in list(local.get("doubao_account_attempts") or [])
        if isinstance(item, Mapping)
    ][-15:]
    attempt_number = int(attempts[-1].get("attempt") or 0) + 1 if attempts else 1
    attempts.append(
        {
            "attempt": attempt_number,
            "account_id": int(account.id),
            "phase": "starting_profile",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    set_task_local_meta(
        task,
        doubao_account_attempts=attempts,
        doubao_submit_phase="starting_profile",
    )
    return len(attempts) - 1


def _update_submit_attempt(
    task: KieTask,
    attempt_index: int,
    **updates: Any,
) -> None:
    local = dict(dict(task.result_json or {}).get("__local") or {})
    attempts = [
        dict(item)
        for item in list(local.get("doubao_account_attempts") or [])
        if isinstance(item, Mapping)
    ]
    if not (0 <= int(attempt_index) < len(attempts)):
        return
    attempts[int(attempt_index)].update(
        {key: value for key, value in updates.items() if value is not None}
    )
    phase = str(updates.get("phase") or "").strip()
    set_task_local_meta(
        task,
        doubao_account_attempts=attempts[-16:],
        **({"doubao_submit_phase": phase} if phase else {}),
    )


def _record_nonvideo_remote_response(
    db: Session,
    *,
    task: KieTask,
    account: HermesBrowserBridge,
    error_code: str,
    message: str,
    timestamp_key: str,
) -> KieTask:
    """Close a confirmed non-video conversation and rotate its account."""

    _remember_failed_account(task, str(account.bridge_id))
    release_account(
        db,
        account,
        task_id=int(task.id),
        success=False,
        error_code=error_code,
    )
    task.state = "failed"
    task.fail_code = error_code
    task.fail_msg = str(message)[:512]
    set_task_local_meta(
        task,
        **{
            timestamp_key: datetime.now(timezone.utc).isoformat(),
            "poll_owner_task_id": None,
            "poll_heartbeat_at": None,
            "poll_heartbeat_provider": None,
        },
    )
    db.add(task)
    db.flush()
    return task


def _validate_output_aspect(task: KieTask, result: Mapping[str, Any]) -> None:
    """Reject a completed provider result that violates the requested canvas.

    Doubao reports the original resource geometry before local download, so a
    mismatch can still use the provider-neutral retry/failover state machine
    instead of being accepted as a successful local file.
    """
    expected = str(dict(task.input_json or {}).get("aspect_ratio") or "").strip()
    if expected not in {"9:16", "16:9", "1:1"}:
        return
    try:
        expected_width, expected_height = (int(value) for value in expected.split(":", 1))
        width = int(result.get("width") or 0)
        height = int(result.get("height") or 0)
    except (TypeError, ValueError):
        return
    if width <= 0 or height <= 0:
        return
    expected_value = expected_width / expected_height
    actual_value = width / height
    relative_error = abs(actual_value - expected_value) / expected_value
    if relative_error > _OUTPUT_ASPECT_TOLERANCE:
        raise DoubaoProviderError(
            f"豆包返回视频比例不符合请求：要求 {expected}，实际 {width}x{height}。",
            code="doubao_output_aspect_mismatch",
        )


def _ensure_result(db: Session, task: KieTask, *, result: dict[str, Any]) -> KieTask:
    _validate_output_aspect(task, result)
    url = str(result.get("video_url") or "").strip()
    if not url.startswith("https://"):
        raise DoubaoProviderError("豆包没有返回可下载的原始视频。", code="doubao_result_missing")
    row = (
        db.query(KieFile)
        .filter(KieFile.task_id == int(task.id), KieFile.kind == "result", KieFile.file_url == url)
        .one_or_none()
    )
    if row is None:
        row = KieFile(
            workspace_id=int(task.workspace_id),
            key_id=int(task.key_id),
            task_id=int(task.id),
            kind="result",
            file_url=url,
            mime_type="video/mp4",
            meta_json={
                "source": "doubao_seedance_pool",
                "original_resource": True,
                "watermark_removed_by_inpainting": False,
            },
        )
    local_meta = dict(dict(task.result_json or {}).get("__local") or {})
    mark_result_file_pending(
        row, filename=str(local_meta.get("download_name_base") or task.id)
    )
    db.add(row)
    task.state = "downloading"
    task.fail_code = None
    task.fail_msg = None
    task.result_json = {
        **dict(task.result_json or {}),
        "doubao_result": {
            "width": int(result.get("width") or 0),
            "height": int(result.get("height") or 0),
            "duration": float(result.get("duration") or 0),
            "original_resource": True,
        },
    }
    set_task_local_meta(
        task,
        download_enqueued_at=None,
        doubao_remote_progress={
            "state": "complete",
            "width": int(result.get("width") or 0),
            "height": int(result.get("height") or 0),
            "duration": float(result.get("duration") or 0),
        },
        doubao_remote_completed_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(task)
    db.flush()
    return task


def _record_output_contract_failure(
    db: Session,
    task: KieTask,
    *,
    result: Mapping[str, Any],
    exc: DoubaoProviderError,
) -> KieTask:
    """Make one unusable completed job retryable without polling it again."""
    task.state = "failed"
    task.fail_code = exc.code
    task.fail_msg = str(exc)[:512]
    set_task_local_meta(
        task,
        doubao_rejected_result={
            "reason": exc.code,
            "width": int(result.get("width") or 0),
            "height": int(result.get("height") or 0),
            "duration": float(result.get("duration") or 0),
            "at": datetime.now(timezone.utc).isoformat(),
        },
        poll_owner_task_id=None,
        poll_heartbeat_at=None,
        poll_heartbeat_provider=None,
    )
    db.add(task)
    db.flush()
    return task


def release_doubao_task_account(
    db: Session,
    *,
    task: KieTask,
    error_code: str,
) -> None:
    """Release the exact account bound to an abandoned durable remote job."""
    bridge_id = str(
        dict(dict(task.result_json or {}).get("__local") or {}).get(
            "doubao_account_bridge_id"
        )
        or ""
    )
    if not bridge_id:
        return
    try:
        account = leased_account(
            db,
            bridge_id=bridge_id,
            task_id=int(task.id),
        )
    except (RuntimeError, ValueError):
        return
    release_account(
        db,
        account,
        task_id=int(task.id),
        success=False,
        error_code=str(error_code or "doubao_abandoned"),
    )


async def submit_doubao_task(db: Session, *, task: KieTask) -> KieTask:
    params = dict(task.input_json or {})
    request = _request(params, task)
    source_prompt = _prompt(params, task)
    set_task_local_meta(
        task,
        provider_prompt_transport={
            "language": (
                "zh-CN structured controls; approved dialogue unchanged"
                if is_structured_video_prompt(source_prompt)
                else "user-authored"
            ),
            "characters": len(request["prompt"]),
            "sha256": hashlib.sha256(
                request["prompt"].encode("utf-8")
            ).hexdigest(),
            "prompt": request["prompt"],
        },
    )
    if request["prompt"] != source_prompt:
        set_task_local_meta(
            task,
            provider_prompt_compaction={
                "source_characters": len(source_prompt),
                "provider_characters": len(request["prompt"]),
                "provider_prompt_sha256": hashlib.sha256(
                    request["prompt"].encode("utf-8")
                ).hexdigest(),
            },
        )
    reference_paths = _reference_paths(db, task)
    attempted_accounts: set[str] = {
        str(value)
        for value in list(
            dict(dict(task.result_json or {}).get("__local") or {}).get(
                "doubao_failed_account_bridge_ids"
            )
            or []
        )
        if str(value).strip()
    }
    set_task_local_meta(task, doubao_submit_phase="selecting_account")
    last_account_error: DoubaoProviderError | None = None
    while True:
        try:
            account = claim_account(
                db,
                task_id=int(task.id),
                excluded_bridge_ids=attempted_accounts,
                requested_duration=int(request["duration"]),
            )
        except DoubaoPoolBusyError as exc:
            # A healthy account exists and another task currently owns its
            # browser/proxy lane.  This is local queue pressure, not route
            # exhaustion.  Preserve the same provider and logical task so the
            # AI-video worker can silently defer it without spending retries.
            raise DoubaoProviderError(
                "豆包生成通道正忙，任务正在自动排队。",
                code="doubao_pool_busy",
            ) from exc
        except (RuntimeError, ValueError) as exc:
            if last_account_error is not None:
                # Every account tried in this submit round failed before a
                # remote conversation id was returned.  ``submitting`` and
                # ``doubao-local-*`` are only durable ambiguity markers while
                # the helper owns the request; they are not pollable remote
                # identities.  Restore the same logical task to local queued
                # state before handing the transient error back to Celery so
                # the next delivery performs a fresh, account-pooled submit
                # instead of entering ``refresh_doubao_task`` with a missing
                # binding.
                task = db.get(KieTask, int(task.id)) or task
                reset_video_task_for_retry(
                    db,
                    task=task,
                    retry_kind="provider_submit_unconfirmed",
                )
                set_task_local_meta(
                    task,
                    provider_submit_unconfirmed_code=str(
                        last_account_error.code or "doubao_submit_failed"
                    ),
                    provider_submit_accounts_exhausted=len(attempted_accounts),
                    provider_submit_unconfirmed_at=datetime.now(
                        timezone.utc
                    ).isoformat(),
                )
                db.commit()
                # Account-level failures are internal pool events.  The
                # caller only needs to know that this provider route was
                # exhausted so unified routing can immediately advance; it
                # must not expose CAPTCHA/auth/account-rotation details in the
                # user-visible task state.
                raise DoubaoProviderError(
                    "豆包视频资源本轮暂不可用，系统将自动尝试其他可用资源。",
                    code="doubao_pool_unavailable",
                ) from exc
            code = (
                "doubao_membership_required"
                if int(request["duration"]) > 10
                else "doubao_pool_unavailable"
            )
            message = (
                str(exc)
                if code == "doubao_membership_required"
                else "豆包视频资源本轮暂不可用，系统将自动尝试其他可用资源。"
            )
            raise DoubaoProviderError(message, code=code) from exc
        account_bridge_id = str(account.bridge_id)
        attempted_accounts.add(account_bridge_id)
        attempt_started = time.monotonic()
        attempt_index = _start_submit_attempt(task, account)
        attempt_phase = "starting_profile"
        set_task_local_meta(
            task,
            active_provider="doubao",
            doubao_account_bridge_id=account_bridge_id,
        )
        task.task_id = f"doubao-local-{int(task.id)}"
        task.state = "submitting"
        # A manual retry or an earlier provider round may have left a public
        # failure on the same logical task.  Account rotation is an internal
        # submitting state, so never leak that stale account error while the
        # next healthy account is being tried.
        task.fail_code = None
        task.fail_msg = None
        account_meta = dict(account.meta_json or {})
        account_meta["doubao_provider_browser_task_id"] = int(task.id)
        account.meta_json = account_meta
        db.add(account)
        db.add(task)
        # Commit the account lease and durable local task identity before the
        # upstream request. A concurrent worker cannot lease the same account.
        db.commit()
        try:
            account = _wait_for_provider_browser_generation(
                db,
                account_id=int(account.id),
                task_id=int(task.id),
                timeout_seconds=max(
                    20,
                    int(settings.DOUBAO_BROWSER_READY_TIMEOUT_SECONDS),
                ),
            )
            browser_ready_ms = max(0, int((time.monotonic() - attempt_started) * 1000))
            attempt_phase = "checking_composer"
            _update_submit_attempt(
                task,
                attempt_index,
                phase=attempt_phase,
                browser_ready_ms=browser_ready_ms,
            )
            db.add(task)
            db.commit()
            try:
                provider_context = account_request_payload(db, account)
            except (RuntimeError, ValueError) as exc:
                raise DoubaoProviderError(
                    str(exc), code="doubao_account_context_invalid"
                ) from exc
            _ensure_live_video_composer(
                provider_context,
                browser_cdp_url=str(account.cdp_url or ""),
            )
            composer_ready_ms = max(
                0,
                int((time.monotonic() - attempt_started) * 1000) - browser_ready_ms,
            )
            attempt_phase = "submitting_request"
            _update_submit_attempt(
                task,
                attempt_index,
                phase=attempt_phase,
                composer_ready_ms=composer_ready_ms,
            )
            db.add(task)
            db.commit()
            payload = invoke_doubao_helper(
                {
                    **provider_context,
                    **request,
                    "action": "submit",
                    "reference_paths": reference_paths,
                    "browser_cdp_url": str(account.cdp_url or ""),
                    "post_submit_observe_seconds": max(
                        1,
                        min(
                            15,
                            int(settings.DOUBAO_POST_SUBMIT_OBSERVE_SECONDS),
                        ),
                    ),
                },
                timeout_seconds=max(
                    60,
                    int(settings.DOUBAO_SUBMIT_TIMEOUT_SECONDS),
                ),
            )
            submission_contract = _sanitized_submission_contract(payload)
            if submission_contract is None:
                raise DoubaoProviderError(
                    "豆包页面没有返回可验证的视频提交凭证，正在切换账号。",
                    code="doubao_composer_unavailable",
                )
            set_task_local_meta(
                task,
                doubao_submission_contract={
                    **submission_contract,
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            submit_total_ms = max(0, int((time.monotonic() - attempt_started) * 1000))
            accepted = bool(
                str(payload.get("conversation_id") or "").strip()
                or str(payload.get("status") or "").strip().lower() == "complete"
            )
            record_submit_observation(
                account,
                duration_ms=submit_total_ms,
                success=accepted,
                error_code=None if accepted else "doubao_task_id_missing",
            )
            _update_submit_attempt(
                task,
                attempt_index,
                phase="remote_accepted" if accepted else "submit_rejected",
                outcome="accepted" if accepted else "failed",
                total_ms=submit_total_ms,
                completed_at=datetime.now(timezone.utc).isoformat(),
                error_code=None if accepted else "doubao_task_id_missing",
            )
            db.add_all((account, task))
            break
        except DoubaoProviderError as exc:
            exc = _normalize_account_submit_error(
                exc,
                requested_duration=int(request["duration"]),
            )
            account = db.get(type(account), int(account.id)) or account
            submit_total_ms = max(0, int((time.monotonic() - attempt_started) * 1000))
            record_submit_observation(
                account,
                duration_ms=submit_total_ms,
                success=False,
                error_code=exc.code,
            )
            _update_submit_attempt(
                task,
                attempt_index,
                phase="switching_account"
                if exc.code in _ROTATE_ACCOUNT_ERROR_CODES
                else "submit_failed",
                failed_phase=attempt_phase,
                outcome="failed",
                total_ms=submit_total_ms,
                completed_at=datetime.now(timezone.utc).isoformat(),
                error_code=str(exc.code or "doubao_submit_failed")[:64],
            )
            _remember_failed_account(task, str(account.bridge_id))
            release_account(
                db,
                account,
                task_id=int(task.id),
                success=False,
                error_code=exc.code,
            )
            db.commit()
            if exc.code in _ROTATE_ACCOUNT_ERROR_CODES:
                last_account_error = exc
                # ``attempted_accounts`` is the finite bound for this logical
                # submission.  Continue immediately until claim_account finds
                # no untried eligible account; do not impose an arbitrary
                # two-account sample or a task-level cooldown while healthy
                # accounts remain in the pool.
                continue
            # The durable ``submitting`` marker was committed before calling
            # the browser helper.  If that call fails before returning a
            # remote conversation id, leaving ``doubao-local-*`` behind makes
            # the next delivery enter the poll path and fail with a missing
            # binding.  Reset the same logical task to provider-neutral local
            # state so a retry submits it again; no second KieTask row is
            # created and the released account lease cannot leak.
            task = db.get(KieTask, int(task.id)) or task
            reset_video_task_for_retry(
                db,
                task=task,
                retry_kind="provider_submit_unconfirmed",
            )
            set_task_local_meta(
                task,
                provider_submit_unconfirmed_code=str(exc.code or "doubao_submit_failed"),
                provider_submit_unconfirmed_at=datetime.now(timezone.utc).isoformat(),
            )
            db.commit()
            raise
    task = db.get(KieTask, int(task.id)) or task
    account = db.get(type(account), int(account.id)) or account
    state = str(payload.get("status") or "pending").lower()
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if state == "complete":
        task.task_id = f"doubao:{conversation_id or int(task.id)}"
        try:
            _ensure_result(db, task, result=payload)
        except DoubaoProviderError as exc:
            release_account(
                db,
                account,
                task_id=int(task.id),
                success=exc.code == "doubao_output_aspect_mismatch",
                error_code=exc.code,
            )
            if exc.code == "doubao_output_aspect_mismatch":
                _record_output_contract_failure(
                    db, task, result=payload, exc=exc
                )
                db.flush()
                return task
            db.commit()
            raise
        release_account(db, account, task_id=int(task.id), success=True)
    elif conversation_id:
        accepted_at = datetime.now(timezone.utc)
        # A conversation id is only an acknowledgement, not proof that
        # Seedance entered the media queue.  Keep the exact Profile alive until
        # the asynchronous poller sees a video_model node or reaches the same
        # bounded non-video timeout used to rotate the account.  A fixed
        # 30-second hold produced permanent two-message shells in production.
        browser_hold_seconds = max(
            60,
            int(settings.DOUBAO_SILENT_CONVERSATION_TIMEOUT_SECONDS),
            int(settings.DOUBAO_TEXT_ONLY_RESPONSE_TIMEOUT_SECONDS),
        )
        browser_hold_until = (
            datetime.now().astimezone().replace(tzinfo=None)
            + timedelta(seconds=browser_hold_seconds)
        )
        account_meta = dict(account.meta_json or {})
        if str(account_meta.get("doubao_provider_browser_task_id") or "") == str(
            int(task.id)
        ):
            account_meta["doubao_provider_browser_hold_until"] = (
                browser_hold_until.isoformat()
            )
            # This releases only the shared proxy submission lane.  The
            # browser ownership marker above deliberately remains active so
            # the Bridge does not close the Profile before media startup.
            account_meta["doubao_provider_submission_accepted_at"] = (
                accepted_at.isoformat()
            )
            account.meta_json = account_meta
            db.add(account)
        task.task_id = f"doubao:{conversation_id}"
        task.state = "queued"
        task.fail_code = None
        task.fail_msg = None
        set_task_local_meta(
            task,
            doubao_remote_accepted_at=accepted_at.isoformat(),
            doubao_browser_hold_until=browser_hold_until.isoformat(),
            doubao_submit_phase="remote_generating",
        )
        task.result_json = {
            **dict(task.result_json or {}),
            "doubao_submit": {"accepted": True},
        }
        db.add(task)
    else:
        release_account(
            db, account, task_id=int(task.id), success=False, error_code="doubao_task_id_missing"
        )
        raise DoubaoProviderError("豆包已接收请求但没有返回任务标识。", code="doubao_task_id_missing")
    db.flush()
    return task


async def refresh_doubao_task(db: Session, *, task: KieTask) -> KieTask:
    if str(task.state or "").lower() in {"downloading", "success"}:
        return task
    bridge_id = str(dict(dict(task.result_json or {}).get("__local") or {}).get("doubao_account_bridge_id") or "")
    if not bridge_id or not str(task.task_id or "").startswith("doubao:"):
        raise DoubaoProviderError("豆包任务缺少账号或远端任务标识。", code="doubao_task_binding_missing")
    conversation_id = str(task.task_id).split(":", 1)[1]
    try:
        account = leased_account(db, bridge_id=bridge_id, task_id=int(task.id))
    except (RuntimeError, ValueError) as exc:
        raise DoubaoProviderError(str(exc), code="doubao_account_lease_expired") from exc
    db.commit()
    try:
        try:
            provider_context = account_request_payload(db, account)
        except (RuntimeError, ValueError) as exc:
            raise DoubaoProviderError(str(exc), code="doubao_account_context_invalid") from exc
        payload = invoke_doubao_helper(
            {
                **provider_context,
                "action": "poll",
                "conversation_id": conversation_id,
            },
            timeout_seconds=120,
        )
    except DoubaoProviderError as exc:
        account = db.get(type(account), int(account.id)) or account
        browser_hold_released = _release_provider_browser_hold_if_due(
            db,
            account,
            task_id=int(task.id),
        )
        # Auth, quota and account-context faults require rotation.  A network,
        # parser or temporary provider fault does not: the accepted remote job
        # still belongs to this exact account, so releasing its lease makes the
        # next poll incapable of retrieving the result.
        if exc.code in _RELEASE_ACCOUNT_ERROR_CODES:
            release_account(
                db,
                account,
                task_id=int(task.id),
                success=False,
                error_code=exc.code,
            )
            db.commit()
        elif browser_hold_released:
            db.commit()
        raise
    task = db.get(KieTask, int(task.id)) or task
    account = db.get(type(account), int(account.id)) or account
    if str(payload.get("status") or "pending").lower() == "complete":
        _release_provider_browser_hold_if_due(
            db,
            account,
            task_id=int(task.id),
            force=True,
        )
        try:
            _ensure_result(db, task, result=payload)
        except DoubaoProviderError as exc:
            release_account(
                db,
                account,
                task_id=int(task.id),
                success=exc.code == "doubao_output_aspect_mismatch",
                error_code=exc.code,
            )
            if exc.code == "doubao_output_aspect_mismatch":
                _record_output_contract_failure(
                    db, task, result=payload, exc=exc
                )
                db.flush()
                return task
            db.commit()
            raise
        release_account(db, account, task_id=int(task.id), success=True)
    else:
        progress = payload.get("progress")
        if isinstance(progress, Mapping):
            set_task_local_meta(task, doubao_remote_progress=dict(progress))
        progress_state = str(
            progress.get("state") if isinstance(progress, Mapping) else ""
        ).strip().lower()
        accepted_raw = str(
            dict(dict(task.result_json or {}).get("__local") or {}).get(
                "doubao_remote_accepted_at"
            )
            or ""
        )
        accepted_at = None
        if accepted_raw:
            try:
                accepted_at = datetime.fromisoformat(
                    accepted_raw.replace("Z", "+00:00")
                )
                if accepted_at.tzinfo is None:
                    accepted_at = accepted_at.replace(tzinfo=timezone.utc)
            except ValueError:
                accepted_at = None
        silent_timeout = int(
            getattr(settings, "DOUBAO_SILENT_CONVERSATION_TIMEOUT_SECONDS", 10 * 60)
        )
        text_only_timeout = int(
            getattr(
                settings,
                "DOUBAO_TEXT_ONLY_RESPONSE_TIMEOUT_SECONDS",
                10 * 60,
            )
        )
        bot_content_count = int(
            (progress.get("bot_content_count") or 0)
            if isinstance(progress, Mapping)
            else 0
        )
        video_model_count = int(
            (progress.get("video_model_count") or 0)
            if isinstance(progress, Mapping)
            else 0
        )
        if video_model_count > 0:
            # The paid media job now exists independently of the browser.  It
            # is safe to close this Profile while preserving the account lease
            # for polling and download.
            _release_provider_browser_hold_if_due(
                db,
                account,
                task_id=int(task.id),
                force=True,
            )
        if progress_state == "content_rejected":
            return _record_nonvideo_remote_response(
                db,
                task=task,
                account=account,
                error_code="doubao_content_rejected",
                message=(
                    "豆包明确拒绝了当前题材，账号本身正常；"
                    "请改写受限制的角色或题材后重试。"
                ),
                timestamp_key="doubao_content_rejected_at",
            )
        if (
            progress_state == "silent_conversation"
            and accepted_at is not None
            and datetime.now(timezone.utc) - accepted_at
            >= timedelta(seconds=max(60, silent_timeout))
        ):
            return _record_nonvideo_remote_response(
                db,
                task=task,
                account=account,
                error_code="doubao_silent_timeout",
                message=(
                    "豆包只创建了空会话，实际视频生成未入队，"
                    "系统将切换账号或供应商。"
                ),
                timestamp_key="doubao_silent_conversation_failed_at",
            )
        if (
            progress_state == "assistant_progress"
            # A bot message shell with empty content is not a text answer.
            # Task 3583 proved that this shell can remain visible for many
            # minutes before the real video node arrives.
            and bot_content_count > 0
            and video_model_count == 0
            and accepted_at is not None
            and datetime.now(timezone.utc) - accepted_at
            >= timedelta(seconds=max(60, text_only_timeout))
        ):
            return _record_nonvideo_remote_response(
                db,
                task=task,
                account=account,
                error_code="doubao_text_only_response",
                message=(
                    "豆包对话已返回普通文字但没有启动 Seedance 视频任务，"
                    "系统将切换账号或供应商。"
                ),
                timestamp_key="doubao_text_only_response_failed_at",
            )
        task.state = "queued"
        db.add(task)
    db.flush()
    return task


__all__ = [
    "refresh_doubao_task",
    "release_doubao_task_account",
    "submit_doubao_task",
]
