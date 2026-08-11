from __future__ import annotations

from typing import Any, Mapping


LOCAL_WORKER_STAGES = frozenset({"FINAL_ASSETS", "VIDEO_PROMPTS"})

EXTERNAL_RETRY_BARRIER_KEYS = frozenset({
    "retry_after",
    "retry_release_strategy",
    "recovery_api_probe_pending",
    "recovery_api_probe_reason",
    "recovery_api_last_probe_at",
    "self_heal_circuit_open",
    "api_force_browser_fallback",
    "visual_api_force_browser_fallback",
    "force_browser",
})


def is_local_worker_stage(
    stage: str | None,
    stage_input: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether a stage can run without a user's browser bridge.

    FINAL_ASSETS normally validates and splits the approved preview on the
    server. It only needs ChatGPT when a repair explicitly requests a visual
    rebuild. VIDEO_PROMPTS is the historical storage name for the signed-plan
    segment compiler and must never activate an API or browser author.
    """

    stage_name = str(stage or "").strip().upper()
    if stage_name not in LOCAL_WORKER_STAGES:
        return False
    values = dict(stage_input or {})
    if stage_name == "FINAL_ASSETS":
        return not bool(values.get("force_chatgpt_rebuild"))
    if stage_name == "VIDEO_PROMPTS":
        return True
    return True


def clear_external_retry_barriers_for_local_stage(
    stage: str | None,
    stage_input: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove stale provider/browser waits from a server-local stage.

    A retry row can outlive the worker version that classified it.  In
    particular, normal FINAL_ASSETS splitting used to inherit API cooldown and
    browser fallback fields even though it only materializes already-approved
    files on local RAID.  Keep the durable stage payload, but discard routing
    barriers that this execution path can never satisfy or use.
    """

    values = dict(stage_input or {})
    if not is_local_worker_stage(stage, values):
        return values
    for key in EXTERNAL_RETRY_BARRIER_KEYS:
        values.pop(key, None)
    if str(values.get("execution_backend") or "").strip().lower() == "browser":
        values.pop("execution_backend", None)
    return values


def stage_execution_backend(
    stage: str | None,
    *,
    api_route: str | None = None,
    stage_input: Mapping[str, Any] | None = None,
) -> str:
    values = dict(stage_input or {})
    stage_name = str(stage or "").strip().upper()
    if stage_name == "VIDEO_PROMPTS":
        # No legacy override may resurrect a fourth creative authority.  A
        # malformed old retry must fail at the signed-plan gate rather than
        # opening ChatGPT.
        return "local"
    explicit_browser_fallback = bool(
        values.get("api_fallback_to_browser")
        or values.get("api_force_browser_fallback")
        or values.get("visual_api_force_browser_fallback")
        or values.get("force_browser")
        or str(values.get("execution_backend") or "").strip().lower() == "browser"
    )
    if explicit_browser_fallback:
        api_route = None
    if str(api_route or "").strip():
        return "api"
    if is_local_worker_stage(stage, stage_input):
        return "local"
    return "browser"
