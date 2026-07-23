from __future__ import annotations

from typing import Any, Mapping


LOCAL_WORKER_STAGES = frozenset({"FINAL_ASSETS", "VIDEO_PROMPTS"})


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
