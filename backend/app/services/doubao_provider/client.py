from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any


HELPER_PYTHON = "/opt/apps/doubao2api-lab/.venv/bin/python"
HELPER_SCRIPT = "/opt/apps/doubao2api-lab/scripts/context_generate.py"
logger = logging.getLogger(__name__)


class DoubaoProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "doubao_provider_error") -> None:
        super().__init__(message)
        self.code = str(code or "doubao_provider_error")[:64]


def validate_doubao_helper_runtime() -> None:
    """Fail clearly when the Celery service identity cannot read the helper."""
    for path in (HELPER_PYTHON, HELPER_SCRIPT):
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            raise DoubaoProviderError(
                "豆包本地协议服务不可读取。",
                code="doubao_helper_unavailable",
            )


def invoke_doubao_helper(payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    """Invoke the isolated protocol helper without persisting browser secrets.

    Cookies travel over stdin only.  stdout is a deliberately redacted result
    envelope; stderr is never copied into a task because an upstream library
    may include request details in diagnostics.
    """
    validate_doubao_helper_runtime()
    try:
        completed = subprocess.run(
            [HELPER_PYTHON, HELPER_SCRIPT],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(10, int(timeout_seconds)),
            check=False,
            close_fds=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise DoubaoProviderError("豆包请求超时，将由统一调度重试。", code="doubao_timeout") from exc
    except OSError as exc:
        raise DoubaoProviderError("豆包本地协议服务不可用。", code="doubao_helper_unavailable") from exc
    raw = completed.stdout.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0 and not raw:
        raise DoubaoProviderError(
            "豆包本地协议服务启动失败。",
            code="doubao_helper_runtime_failed",
        )
    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise DoubaoProviderError("豆包协议服务返回了无效结果。", code="doubao_invalid_response") from exc
    state = str(result.get("status") or "failed").strip().lower()
    if state == "captcha_required":
        raise DoubaoProviderError(
            "豆包账号需要人工完成 CAPTCHA，已暂停该账号并切换下一账号。",
            code="doubao_captcha_required",
        )
    if state == "auth_required":
        raise DoubaoProviderError(
            "豆包账号登录态已失效，已暂停该账号并切换下一账号。",
            code="doubao_auth_required",
        )
    if state == "failed":
        error = str(result.get("error") or "Doubao request failed")[:500]
        diagnostic = str(result.get("diagnostic") or "").strip()[:500]
        if diagnostic:
            # The isolated helper emits only a bounded, credential-free stage
            # marker.  Keep it out of task/UI state but retain it in the
            # operator journal so a browser contract drift is diagnosable
            # without replaying a paid generation by hand.
            logger.warning(
                "Doubao helper failed code=%s diagnostic=%s",
                str(result.get("error_code") or "doubao_failed")[:64],
                diagnostic,
            )
        raise DoubaoProviderError(error, code=str(result.get("error_code") or "doubao_failed"))
    return result


__all__ = ["DoubaoProviderError", "invoke_doubao_helper", "validate_doubao_helper_runtime"]
