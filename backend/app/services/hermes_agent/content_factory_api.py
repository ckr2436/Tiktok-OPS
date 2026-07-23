from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import httpx

from app.services.ai_routing.router import AiGatewayError, call_chat_with_failover
from app.services.hermes_agent.storyboard_split import expected_row_columns
from sqlalchemy.orm import Session

from app.services.kie_api.accounts import (
    TOAPIS_PROVIDER_KEY,
    decrypt_api_key,
    get_effective_key,
)


TEXT_API_STAGES = {"FACTS", "CREATIVE_REVIEW", "EDIT_PACKAGE"}
CONTENT_FACTORY_CONTEXT_COMPILER_VERSION = 3
PRODUCT_REFERENCE_VIDEO_REVIEW_POLICY_VERSION = (
    "2026-07-22-provider-product-tolerant-v1"
)

STRICT_BENCHMARK_PATTERNS = (
    r"1\s*[:：比]\s*1",
    r"一\s*比\s*一",
    r"逐(?:镜|帧|句)",
    r"精准复刻",
    r"完全复刻",
    r"原样复刻",
)
ADAPTIVE_BENCHMARK_PATTERNS = (
    r"模仿",
    r"仿写",
    r"参考(?:对标|这个|该)?视频",
    r"参考.*(?:风格|节奏|结构|文案)",
    r"对标",
)


class ContentFactoryApiError(RuntimeError):
    pass




def _explicit_model_rejection(response: httpx.Response | None) -> bool:
    """Return true only when the provider confirms that it rejected the model.

    Transport failures, timeouts, 5xx responses, rate limits and malformed
    success responses are ambiguous: the provider may already be processing the
    request, so failing over in those cases can execute one stage twice.
    """
    if response is None or int(response.status_code) not in {400, 404, 422}:
        return False
    body = str(response.text or "").lower()
    if "model" not in body:
        return False
    return any(
        marker in body
        for marker in (
            "model not found",
            "model_not_found",
            "unsupported model",
            "unsupported_model",
            "invalid model",
            "invalid_model",
            "unknown model",
            "unknown_model",
            "model does not exist",
            "model is not available",
        )
    )


def _text(value: Any, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    return clean[:limit]


def creative_spoken_copy_budget_limits(
    packet: dict[str, Any],
) -> dict[str, Any]:
    """Return the authoritative per-segment speech limits.

    Prompt construction and deterministic output validation must consume the
    same values. Otherwise the model can explicitly acknowledge that it
    exceeded a hard prompt limit while the server accepts the longer copy
    under a separate voice-rate estimate.
    """
    durations: list[int] = []
    for raw in list(packet.get("video_segment_durations_seconds") or []):
        try:
            duration = int(float(raw))
        except (TypeError, ValueError):
            continue
        if duration > 0:
            durations.append(duration)
    if not durations:
        return {}

    total_duration = max(1, sum(durations))
    target_edit = max(1, min(total_duration, round(total_duration * 0.84)))
    language = str(
        packet.get("video_language")
        or packet.get("video_language_label")
        or ""
    ).lower()
    chinese = language.startswith("zh") or "chinese" in language
    rate = 260 if chinese else 110
    unit_label = "Chinese characters" if chinese else "English words"
    limits: list[dict[str, Any]] = []
    for index, duration in enumerate(durations, 1):
        capacity = max(
            1,
            int((target_edit * (duration / total_duration)) * rate / 60.0),
        )
        tolerance = max(2, math.ceil(capacity * 0.25))
        max_units = capacity + tolerance
        if not chinese:
            max_units = min(18, max_units)
        # Models routinely treat a printed maximum as a writing target and
        # overshoot it by one or two units. Keep the deterministic validator's
        # hard ceiling unchanged, but give generation a two-unit safety margin
        # around the natural voice-rate capacity.
        target_center = min(capacity, max(1, max_units - 2))
        target_min_units = max(1, target_center - 1)
        target_max_units = max(
            target_min_units,
            min(max_units - 2, target_center + 1),
        )
        limits.append({
            "segment_index": index,
            "duration_seconds": duration,
            "target_min_units": target_min_units,
            "target_max_units": target_max_units,
            "max_units": max_units,
        })
    return {
        "target_edit_duration_seconds": target_edit,
        "unit_label": unit_label,
        "limits": limits,
    }


def creative_spoken_copy_budget_contract(packet: dict[str, Any]) -> str:
    """Return one concrete speech budget shared by API and browser prompts."""
    budget = creative_spoken_copy_budget_limits(packet)
    if not budget:
        return ""
    limits = [
        (
            f"segment {int(item['segment_index'])} "
            f"({int(item['duration_seconds'])}s): target "
            f"{int(item['target_min_units'])}-"
            f"{int(item['target_max_units'])} {budget['unit_label']}; "
            f"hard maximum "
            f"{int(item['max_units'])} {budget['unit_label']}"
        )
        for item in list(budget["limits"])
    ]
    return (
        "Set target_edit_duration_seconds to "
        f"{int(budget['target_edit_duration_seconds'])}. "
        "Write to each target range, not to its ceiling. These hard maximum "
        "spoken-copy limits count every narration, "
        "quote, product feature, price, and CTA: "
        + "; ".join(limits)
        + ". Every segment must contain at least one complete natural spoken "
        "sentence. Keep the final CTA to one compact natural sentence inside "
        "its segment limit."
    )


def benchmark_imitation_mode(requirement: Any, *, has_benchmark: bool) -> str:
    """Classify user intent without letting generic benchmark rules override it."""
    if not has_benchmark:
        return "none"
    text = str(requirement or "").strip()
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in STRICT_BENCHMARK_PATTERNS):
        return "exact"
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in ADAPTIVE_BENCHMARK_PATTERNS):
        return "adaptive"
    return "adaptive"


def _json_dict(value: Any) -> dict[str, Any]:
    """Return JSON objects only; model-produced strings/lists are not mappings."""
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []



def _compact(value: Any, *, depth: int = 0, max_items: int = 10, max_text: int = 800) -> Any:
    if depth >= 4:
        return _text(value, max_text)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:max_items]:
            if item not in (None, "", [], {}):
                result[str(key)] = _compact(
                    item, depth=depth + 1, max_items=max_items, max_text=max_text
                )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _compact(item, depth=depth + 1, max_items=max_items, max_text=max_text)
            for item in list(value)[:max_items]
        ]
    if isinstance(value, str):
        return _text(value, max_text)
    return value


def _json_object_from_text(text: Any, *, error_prefix: str) -> dict[str, Any]:
    """Parse one provider JSON object, tolerating a fenced or prefixed object."""
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
    try:
        value = json.loads(candidate)
    except ValueError as exc:
        value = None
        decoder = json.JSONDecoder()
        for offset, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                recovered, _end = decoder.raw_decode(candidate[offset:])
            except ValueError:
                continue
            if isinstance(recovered, dict):
                value = recovered
                break
        if value is None:
            raise ContentFactoryApiError(
                f"{error_prefix} returned invalid JSON"
            ) from exc
    if not isinstance(value, dict):
        raise ContentFactoryApiError(
            f"{error_prefix} did not return a JSON object"
        )
    return value


def _previous(packet: dict[str, Any], stage: str) -> dict[str, Any]:
    previous = _json_dict(packet.get("previous_outputs"))
    if stage == "CREATIVE_REVIEW":
        return {"MEDIA_DESIGN": _compact(previous.get("MEDIA_DESIGN") or {}, max_items=14, max_text=800)}
    if stage == "EDIT_PACKAGE":
        return {
            "VIDEO_PROMPTS": _compact(previous.get("VIDEO_PROMPTS") or {}, max_items=12, max_text=700),
        }
    return {}


def minimal_stage_context(packet: dict[str, Any], stage: str) -> dict[str, Any]:
    """Stage whitelist used by both API execution and prompt-size regression tests."""
    common = {
        "project_id": packet.get("project_id"),
        "execution_id": packet.get("execution_id"),
        "content": {
            "mode": packet.get("content_mode") or "general",
            "product_required": bool(packet.get("product_required", False)),
        },
        "product": packet.get("product"),
        "market": packet.get("market"),
        "project_requirement": _text(
            packet.get("project_requirements") or packet.get("brief") or "", 2400
        ),
        "stage_instruction": _text(packet.get("user_instruction") or "", 600),
        "variant": {
            "index": packet.get("video_variant_index"),
            "total": packet.get("video_variant_total"),
            "do_not_repeat": _compact(
                packet.get("previous_variant_briefs") or [], max_items=8, max_text=260
            ),
        },
        "video": {
            "model": packet.get("video_model"),
            "duration_range_seconds": packet.get("video_duration_range_seconds"),
            "segment_durations_seconds": packet.get("video_segment_durations_seconds"),
            "segment_count": packet.get("video_segment_count"),
            "reference_limit": packet.get("video_reference_limit"),
            "resolution": packet.get("video_resolution"),
            "language": packet.get("video_language_label") or packet.get("video_language"),
        },
        "marketing": _compact(
            packet.get("marketing_authorization") or {}, max_items=10, max_text=600
        ),
        "creative_copy_contract": _compact(
            packet.get("creative_copy_contract") or {},
            max_items=40,
            max_text=1200,
        ),
        "creative_cast_policy": _compact(
            packet.get("creative_cast_policy") or {},
            max_items=30,
            max_text=700,
        ),
        "product_presentation_policy": _compact(
            packet.get("product_presentation_policy") or {},
            max_items=30,
            max_text=900,
        ),
        "required_result_fields": list(packet.get("required_result_fields") or []),
        "required_next_stage": packet.get("required_next_stage"),
        "previous": _previous(packet, stage),
    }
    if stage in TEXT_API_STAGES and packet.get("semantic_validation_last_error"):
        common["semantic_recovery"] = {
            "generation": packet.get("api_regeneration_generation"),
            "retry_count": packet.get("semantic_api_retry_count"),
            "previous_validation_error": _text(
                packet.get("semantic_validation_last_error"),
                1200,
            ),
            "instruction": (
                "Correct this exact deterministic validation failure in a fresh "
                "response. Do not replay or minimally patch the rejected payload."
            ),
        }
    if stage == "FACTS":
        common["source_assets"] = _compact(
            packet.get("browser_assets") or [], max_items=20, max_text=320
        )
    elif stage == "CREATIVE_REVIEW":
        common["visual_assets"] = _compact(
            packet.get("browser_assets") or [], max_items=12, max_text=260
        )
        # A compact creative summary is not enough for a visual acceptance
        # decision.  Keep the normalized, ordered still-frame contract
        # alongside the images so the reviewer must compare every rendered
        # file with the exact terminal state that it was meant to depict.
        common["reference_plan"] = _reference_plan(packet)
    elif stage == "EDIT_PACKAGE":
        state = dict(packet.get("project_state") or {})
        common["completed_video"] = _compact(
            state.get("active_variant_video")
            or state.get("completed_video_manifest")
            or state.get("variant_video_manifest")
            or {},
            max_items=14,
            max_text=700,
        )
    context = _compact(common, max_items=24, max_text=2400)
    return context








def _task_contract(stage: str, packet: dict[str, Any]) -> str:
    product_required = bool(packet.get("product_required", True))
    product_presentation_policy = _json_dict(
        packet.get("product_presentation_policy")
    )
    review_product_rule = (
        "Every reference_plan row is a newly generated scene/action reference. "
        "For a product-visible row, compare the product rendered in the scene "
        "with the separate uploaded product_visual authority. The media model "
        "must place the product naturally in the scripted scene; never accept "
        "a pasted source rectangle, white-background packshot, product card, "
        "or full-frame package hold. Block a missing product, wrong product "
        "type, wrong brand identity, materially wrong package silhouette, cap, "
        "dominant colors, or gross deformation. Minor micro-label illegibility, "
        "small decorative-detail drift, and subtle generative variation are "
        "acceptable when the advertised product remains unmistakably the same. "
        "Judge the scene, action, composition, natural product interaction, and "
        "identity consistency without demanding pixel-for-pixel reproduction. "
        "product_presentation_policy="
        + json.dumps(
            product_presentation_policy,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + ". "
        if product_required
        else "Reject any product, package, label, offer, sales card, or shopping UI. "
    )
    visual_requirement_text = " ".join(
        str(packet.get(key) or "")
        for key in ("user_instruction", "project_requirements", "brief")
    )
    illustration_required = bool(re.search(
        r"\b(?:2d|2\.5d|animated|animation|illustrat(?:ed|ion)|cartoon|graphic novel)\b|"
        r"动画|插画|卡通|美式动画",
        visual_requirement_text,
        flags=re.IGNORECASE,
    ))
    review_medium_rule = (
        "The requested medium is American adult 2D/2.5D editorial illustration. Reject any photorealistic, "
        "live-action, photographic, or real-person-looking board even when its story beats are otherwise correct. "
        "The repair brief must explicitly require visibly illustrated characters, environment, and lighting. "
        if illustration_required
        else ""
    )
    individual_reference_review = bool(packet.get("render_reference_images_individually"))
    contracts = {
        "FACTS": (
            "Analyze only the supplied company product sources. Return product_passport, approved_claims, prohibited_claims, "
            "visual_invariants, application_rules, source_map, and product_truth_handoff. Distinguish product/package images from "
            "ingredient, supplement-facts, compliance, and promotional material. Promotions are not product facts. Never invent a "
            "claim that is absent from the supplied sources."
        ),
        "CREATIVE_REVIEW": (
            (
                "Act only as a visual acceptance inspector. Never rewrite, extend, or replace the approved story, copy, "
                "conversion, audio, or production plan. Inspect the attached native target-aspect reference IMAGE FILES in file order against the selected "
                "production "
                "reference_plan. Return: creative_review, approved_for_split, reference_image_count, repair_brief, and "
                "reference_checks. reference_checks must contain exactly one ordered object per reference-plan row with: "
                "index, character_scene_verdict, terminal_action_verdict, continuity_verdict, emotional_beat_verdict, "
                "placement_surface_verdict, observed_characters, observed_terminal_state, observed_gaze_expression, "
                "observed_placement_surface, observed_facts, and missing_or_wrong_facts. Verdict values are only match, "
                "mismatch, uncertain, or not_required. Describe only pixels visibly present in that one still image. Do not "
                "infer narrative intent, motion that might happen later, a future video animation, an off-screen prop, a "
                "relationship, a reconnection, eye contact, a smile, or a placement surface that is not visibly shown. "
                "These files are controllable input anchors for a video model, not final edited-video screenshots. Set "
                "terminal_action_verdict=match when the still contains the correct adult, scene, essential action prop, and "
                "a coherent immediately animatable starting or adjacent pose from which the segment prompt can perform the "
                "planned motion. Do not require the exact final hand position, a paper to be fully hidden inside a pocket, "
                "the exact degree of a fold, an exact warm-versus-cool light temperature, an optional secondary prop, or two "
                "moments of one action to coexist in one still. Those non-blocking degree/state differences must not be placed "
                "in missing_or_wrong_facts. Reject only a missing/wrong adult, scene, essential action prop, contradictory "
                "action, or unrelated pose that cannot guide the planned segment. Uncertain is a rejection only for an "
                "essential required fact that can reasonably be proven by one still image. A row may contain story context "
                "or a sequence of actions for the later video, but one still image is required to show its essential "
                "single_frame_terminal_state. Earlier or intermediate actions are context, are not required to coexist in the "
                "still, and their absence must never be reported as a mismatch. Conversely, an earlier or intermediate action "
                "does not satisfy the named essential action anchor when that action is unrelated to the planned segment. "
                "When the plan specifies gaze, expression, a smile, relief, or reconnection, emotional_beat_verdict may be "
                "match only when the required faces, gaze directions, and expression are visibly readable. When a product "
                "reference is required, placement_surface_verdict judges whether the generated product has coherent "
                "contact, perspective, lighting, and occlusion in the scripted scene; reject a floating, pasted, or "
                "white-background package. A generic surrounding surface or an implied off-screen surface is not evidence "
                "of the scripted product placement surface. approved_for_split may be true only when every required verdict is match and every "
                "missing_or_wrong_facts list is empty. "
                "For character_scene_verdict, hard character appearance authority comes only from an attached "
                "character_reference or an explicit user/project requirement. Creative-model-invented hair texture, hair "
                "shade, exact wardrobe color, and accessory micro-details are continuity guidance, not pixel-exact authority. "
                "Do not reject harmless invented appearance changes when the same fictional adults remain visually consistent "
                "across all reference files. Do reject a minor-looking person, wrong adult count/role, duplicate/missing adult, "
                "material identity drift between files, or mismatch to an attached/user-authoritative character anchor. "
                "Exactly one file represents each planned scene. Count files, not visual regions inside an image. Never reinterpret "
                "multiple adults, furniture, windows, shadows, foreground/background areas, or separate actions in one continuous "
                f"composition as extra panels. Each file must be one uninterrupted native {_packet_aspect_ratio(packet)[1]} frame; reject only an actual collage, "
                "grid, split screen, nested frame, wrong/missing file, unrelated scene, duplicated/missing beat, or broken "
                "character/scene continuity. The file count and global chronological order must exactly match the creative plan. "
                if individual_reference_review
                else
                "Inspect every attached preview board in board_index order against the selected creative reference_plan. Return: "
                "creative_review, approved_for_split, reference_image_count, repair_brief, and reference_checks. reference_checks "
                "must contain exactly one ordered object per reference-plan row with index, character_scene_verdict, "
                "terminal_action_verdict, continuity_verdict, emotional_beat_verdict, placement_surface_verdict, "
                "observed_characters, observed_terminal_state, observed_gaze_expression, observed_placement_surface, "
                "observed_facts, and missing_or_wrong_facts. Verdicts are match, mismatch, uncertain, or not_required only. "
                "Describe only visible pixels. These boards are controllable video-model anchors, not final edited-video "
                "screenshots. Set terminal_action_verdict=match when the correct adult, scene, essential action prop, and an "
                "immediately animatable starting or adjacent pose are visible. Do not reject an exact-hand-position, partly "
                "versus fully tucked paper, exact fold degree, minor light-temperature, or optional-secondary-prop difference; "
                "do not list those non-blocking differences in missing_or_wrong_facts. Treat uncertain as rejection only for "
                "an essential required fact that one still can reasonably prove. Earlier/intermediate actions are story "
                "context, need not coexist in the still, and their absence is not a mismatch. Never infer motion, off-screen "
                "essential props, eye contact, smiles, emotional reconnection, or a hidden placement surface. Hard character "
                "appearance authority comes only from an attached "
                "character_reference or explicit user/project requirement. Do not reject harmless creative-model-invented hair, "
                "wardrobe-color, or accessory micro-detail changes when the same fictional adults remain consistent across panels; "
                "do reject adult-count/role errors, duplicates, minors, cross-panel identity drift, or authoritative-anchor mismatch. "
                "Count the visible panels exactly. "
                "The combined panel count and global chronological order must exactly match the creative plan. Reject for wrong/missing "
                "panel count, unrelated imagery, nested frames, duplicated/missing beats, or broken character/scene continuity. "
            )
            + review_product_rule + review_medium_rule + "If rejected, give one actionable regeneration instruction."
        ),
        "EDIT_PACKAGE": (
            "Write one concise human-editor handoff for the corresponding completed video. Return edit_guidance, "
            "publish_title, publish_caption, hashtags. Use the actual segment story and dialogue, not generic labels. "
            "edit_guidance contains only useful chapter ranges with short overlay wording, font/placement, and the final "
            "spoken CTA subtitle when appropriate. Use at most five hashtags. No media buying, testing, budget or ROAS advice."
        ),
    }
    return contracts[stage]


def build_text_api_request(packet: dict[str, Any], stage: str) -> tuple[str, str]:
    if stage not in TEXT_API_STAGES:
        raise ContentFactoryApiError(f"Unsupported text API stage: {stage}")
    system = (
        "You are the GMV OPS content-factory stage engine. Execute one stage only. "
        "Treat user-confirmed requirements in the supplied context as authoritative. "
        "Return one strict JSON object only, with schema_version, execution_id, project_id, stage, status, result, "
        "evidence, issues, repair_brief, next_stage. Do not include markdown or commentary."
    )
    try:
        regeneration_generation = max(0, int(packet.get("api_regeneration_generation") or 0))
    except (TypeError, ValueError):
        regeneration_generation = 0
    regeneration_note = (
        f"\n\nSEMANTIC_REGENERATION_GENERATION: {regeneration_generation}. "
        "This is a fresh semantic correction request; do not replay an earlier rejected completion."
        if regeneration_generation
        else ""
    )
    user = (
        _task_contract(stage, packet)
        + regeneration_note
        + "\n\nMINIMAL_STAGE_CONTEXT:\n"
        + json.dumps(minimal_stage_context(packet, stage), ensure_ascii=False, separators=(",", ":"))
    )
    return system, user


def _text_api_idempotency_key(
    *,
    execution_key: str,
    stage_key: str,
    regeneration_generation: int,
    attempt_index: int,
    model: str,
) -> str:
    """Return a bounded key whose semantic generation cannot be truncated.

    Some compatible gateways cap idempotency keys at 64 characters. The old
    key appended ``:regen:N`` and ``:model-fallback:...`` after a long project
    execution id, so truncation could collapse every correction into the
    original bad completion. Keep the readable legacy key for the primary
    generation when it fits; semantic regeneration/failover always uses a
    short digest over every identity component.
    """
    legacy = f"content-factory:{execution_key}:{stage_key}"
    if not regeneration_generation and not attempt_index and len(legacy) <= 64:
        return legacy
    material = (
        f"{execution_key}|{stage_key}|regen={int(regeneration_generation)}|"
        f"attempt={int(attempt_index)}|model={model}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    mode = f"r{int(regeneration_generation)}" if regeneration_generation else "base"
    route = f"f{int(attempt_index)}" if attempt_index else "p"
    key = f"cf:{stage_key.lower()}:{mode}:{route}:{digest}"
    if len(key) > 64:
        raise ContentFactoryApiError("Text API idempotency key exceeded 64 characters")
    return key


def _data_url(path: str) -> str:
    from app.services.bandianwa.client import image_file_data_url

    return image_file_data_url(path)


def _routed_multimodal_completion(
    db: Session,
    *,
    payload: dict[str, Any],
    request_id: str,
    logical_model: str | None = None,
    workload: str | None = None,
    source: str = "content_visual_review",
    error_prefix: str = "CONTENT_VISUAL_REVIEW",
) -> dict[str, Any]:
    """Run pixel inspection through the shared, circuit-aware route layer."""

    resolved_model = str(
        logical_model
        or os.getenv("HERMES_CONTENT_VISUAL_INSPECTOR_MODEL")
        # Temporary environment compatibility; the old name is not emitted
        # into new task or audit metadata.
        or os.getenv("HERMES_PRODUCT_COMPOSITE_MODEL")
        or ""
    ).strip()
    prefix = str(error_prefix or "MULTIMODAL").strip().upper()
    if not resolved_model:
        raise ContentFactoryApiError(
            f"{prefix}_ROUTING_MODEL_NOT_CONFIGURED"
        )
    resolved_workload = str(
        workload
        or os.getenv("HERMES_CONTENT_VISUAL_INSPECTOR_WORKLOAD")
        or os.getenv("HERMES_PRODUCT_COMPOSITE_WORKLOAD")
        or "content_visual_inspector"
    ).strip().lower()
    messages = list(payload.get("messages") or [])
    overrides = {
        key: value
        for key, value in payload.items()
        if key not in {"model", "messages", "stream"}
        and value is not None
    }
    try:
        result = asyncio.run(
            call_chat_with_failover(
                db,
                logical_model_id=resolved_model,
                messages=messages,
                capability="multimodal",
                workload=resolved_workload,
                request_id=str(request_id)[:96],
                payload_overrides=overrides,
                metadata={
                    "source": str(source or "content_multimodal")[:64],
                    "workload": resolved_workload,
                },
                timeout_seconds=150,
                max_routes=4,
            )
        )
    except AiGatewayError as exc:
        # Route health retains bounded metadata. Do not copy provider bodies,
        # balances or request identifiers into project/stage error text.
        raise ContentFactoryApiError(
            f"{prefix}_ROUTING_FAILED: "
            f"{str(exc.error_class or 'UPSTREAM')}"
        ) from exc
    if not isinstance(result, dict) or not isinstance(
        result.get("choices"), list
    ):
        raise ContentFactoryApiError(
            f"{prefix}_ROUTING_INVALID_RESPONSE"
        )
    return result






def review_provider_rendered_product_video_api(
    db: Session,
    *,
    contact_sheet_path: str,
    product_reference_path: str,
    execution_id: str,
) -> dict[str, Any]:
    """Grade a provider-rendered product with deliberate minor-drift tolerance.

    This gate protects product identity and scene realism without requiring
    pixel-for-pixel label reproduction, which generative video cannot promise.
    """
    sheet = Path(str(contact_sheet_path or ""))
    product = Path(str(product_reference_path or ""))
    if not sheet.is_file() or sheet.stat().st_size <= 1024:
        raise ContentFactoryApiError("PRODUCT_VIDEO_REVIEW_CONTACT_SHEET_MISSING")
    if not product.is_file() or product.stat().st_size <= 1024:
        raise ContentFactoryApiError("PRODUCT_VIDEO_REVIEW_AUTHORITY_MISSING")
    system = (
        "You are an independent product-in-video quality inspector. The first "
        "image is an ordered contact sheet sampled from one short video segment. "
        "The second image is the authoritative uploaded product package. Judge "
        "only visible pixels. Return strict JSON. Minor generative imperfections "
        "are acceptable: micro-label text may be partly illegible, tiny decorative "
        "details may drift, and subtle frame-to-frame shimmer may occur. Blocking "
        "defects are: product missing from the intended product segment; clearly "
        "wrong product type or brand; materially wrong package silhouette, cap, "
        "dominant colors, or primary identity; gross melting/deformation; duplicate "
        "packages when not scripted; a pasted rectangle, visible white source "
        "background, packshot card, or full-frame product still; physically "
        "impossible placement or interaction; or severe temporal identity changes."
    )
    user = (
        "Classify the generated product across the ordered frames. Return keys: "
        "status ('pass' or 'fail'), product_present (boolean), identity_verdict "
        "('match', 'minor_drift', or 'blocking_mismatch'), scene_integration "
        "('natural', 'minor_artifact', or 'blocking_artifact'), temporal_consistency "
        "('stable', 'minor_drift', or 'blocking'), pasted_or_white_background "
        "(boolean), gross_deformation (boolean), duplicate_unscripted_product "
        "(boolean), confidence (0..1), observed_facts (array), blocking_reasons "
        "(array). status must be pass when defects are only within the explicitly "
        "allowed minor-drift tolerance."
    )
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {
                        "type": "image_url",
                        "image_url": {"url": _data_url(str(sheet)), "detail": "high"},
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": _data_url(str(product)), "detail": "high"},
                    },
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }
    digest = hashlib.sha256(
        (
            f"{PRODUCT_REFERENCE_VIDEO_REVIEW_POLICY_VERSION}|{execution_id}|"
            f"{hashlib.sha256(sheet.read_bytes()).hexdigest()}|"
            f"{hashlib.sha256(product.read_bytes()).hexdigest()}"
        ).encode("utf-8")
    ).hexdigest()[:28]
    body = _routed_multimodal_completion(
        db,
        payload=payload,
        request_id=f"cf-product-video:{digest}",
        source="content_product_video_review",
        error_prefix="PRODUCT_VIDEO_REVIEW",
    )
    result = _json_object_from_text(
        str(body["choices"][0]["message"]["content"] or ""),
        error_prefix="Product video review",
    )
    status = str(result.get("status") or "").strip().lower()
    identity = str(result.get("identity_verdict") or "").strip().lower()
    integration = str(result.get("scene_integration") or "").strip().lower()
    temporal = str(result.get("temporal_consistency") or "").strip().lower()
    blocking = bool(
        status != "pass"
        or not bool(result.get("product_present"))
        or identity not in {"match", "minor_drift"}
        or integration not in {"natural", "minor_artifact"}
        or temporal not in {"stable", "minor_drift"}
        or bool(result.get("pasted_or_white_background"))
        or bool(result.get("gross_deformation"))
        or bool(result.get("duplicate_unscripted_product"))
    )
    return {
        **result,
        "status": "fail" if blocking else "pass",
        "policy_version": PRODUCT_REFERENCE_VIDEO_REVIEW_POLICY_VERSION,
        "blocking": blocking,
    }


def execute_text_stage_api(
    db: Session,
    packet: dict[str, Any],
    stage: str,
) -> tuple[str, dict[str, Any]]:
    system, user = build_text_api_request(packet, stage)
    preferred_model = {
        "FACTS": "gpt-5.6-terra",
        "CREATIVE_REVIEW": "gpt-5.6-luna",
        "EDIT_PACKAGE": "gpt-5.6-luna",
    }[stage]
    max_tokens = {
        "FACTS": 3200,
        "CREATIVE_REVIEW": 1200,
        "EDIT_PACKAGE": 1400,
    }[stage]
    content: str | list[dict[str, Any]] = user
    if stage in {"FACTS", "CREATIVE_REVIEW"}:
        content = [{"type": "text", "text": user}]
        manifests = list(packet.get("browser_assets") or [])
        paths = list(packet.get("browser_asset_paths") or [])
        selected_paths: list[str] = []
        for manifest, path in zip(manifests, paths):
            item = manifest if isinstance(manifest, dict) else {}
            kind = str(item.get("kind") or "").lower()
            role = str(item.get("role") or item.get("asset_role") or "").lower()
            mime_type = str(item.get("mime_type") or "").lower()
            if not (mime_type.startswith("image/") or Path(str(path)).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}):
                continue
            if stage == "CREATIVE_REVIEW" and kind not in {"visual_preview", "preview_canvas", "source"} and role not in {"visual_preview", "preview_canvas", "product_visual"}:
                continue
            selected_paths.append(str(path))
        for path in selected_paths[:12]:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _data_url(str(path)),
                        # Low-detail vision was sufficient for counting files
                        # but repeatedly hallucinated facial expressions and
                        # small placement surfaces.  Creative acceptance is a
                        # quality gate, so preserve image detail here.
                        "detail": "high" if stage == "CREATIVE_REVIEW" else "low",
                    },
                }
            )
    payload = {
        "model": preferred_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if stage == "CREATIVE_REVIEW":
        logical_model = str(
            os.getenv("HERMES_CREATIVE_REVIEW_MODEL") or ""
        ).strip()
        workload = str(
            os.getenv("HERMES_CREATIVE_REVIEW_WORKLOAD") or ""
        ).strip().lower()
        if not workload:
            raise ContentFactoryApiError(
                "CREATIVE_REVIEW_ROUTING_WORKLOAD_NOT_CONFIGURED"
            )
        request_digest = hashlib.sha256(
            (
                f"{packet.get('execution_id') or ''}|CREATIVE_REVIEW|"
                f"{int(packet.get('api_regeneration_generation') or 0)}"
            ).encode("utf-8")
        ).hexdigest()[:28]
        body = _routed_multimodal_completion(
            db,
            payload=payload,
            request_id=f"cf-creative-review:{request_digest}",
            logical_model=logical_model,
            workload=workload,
            source="content_creative_review",
            error_prefix="CREATIVE_REVIEW",
        )
        try:
            text = str(body["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ContentFactoryApiError(
                "CREATIVE_REVIEW_ROUTING_INVALID_RESPONSE"
            ) from exc
        if not text:
            raise ContentFactoryApiError(
                "CREATIVE_REVIEW_ROUTING_EMPTY_RESPONSE"
            )
        used_model = str(body.get("model") or logical_model)
        provider_name = "ai_routing"
        prefer_alternate_model = False
    else:
        key = get_effective_key(
            db,
            provider_key=TOAPIS_PROVIDER_KEY,
            require_active=True,
        )
        token = decrypt_api_key(key.api_key_ciphertext)
        provider_name = TOAPIS_PROVIDER_KEY
        text = ""
        body = {}
    base_url = str(os.getenv("TOAPIS_API_BASE_URL") or "https://toapis.com/v1").rstrip("/")
    headers = (
        {}
        if stage == "CREATIVE_REVIEW"
        else {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    )
    alternate_model = "gpt-5.6-luna" if preferred_model != "gpt-5.6-luna" else "gpt-5.6-terra"
    prefer_alternate_model = bool(
        stage != "CREATIVE_REVIEW"
        and packet.get("text_api_prefer_alternate_model", False)
    )
    model_order = (
        (alternate_model, preferred_model)
        if prefer_alternate_model
        else (preferred_model, alternate_model)
    )
    errors: list[str] = []
    used_model = used_model if stage == "CREATIVE_REVIEW" else preferred_model
    execution_key = str(packet.get("execution_id") or "").strip()
    stage_key = str(stage or "").strip().upper()
    try:
        regeneration_generation = max(0, int(packet.get("api_regeneration_generation") or 0))
    except (TypeError, ValueError):
        regeneration_generation = 0
    for attempt_index, model in enumerate(
        () if stage == "CREATIVE_REVIEW" else dict.fromkeys(model_order)
    ):
        payload["model"] = model
        idempotency_key = _text_api_idempotency_key(
            execution_key=execution_key,
            stage_key=stage_key,
            regeneration_generation=regeneration_generation,
            attempt_index=attempt_index,
            model=model,
        )
        attempt_headers = {
            **headers,
            "Idempotency-Key": idempotency_key,
        }
        response: httpx.Response | None = None
        try:
            response = httpx.post(
                f"{base_url}/chat/completions",
                headers=attempt_headers,
                json=payload,
                timeout=240.0,
            )
            response.raise_for_status()
            candidate_body = response.json()
            candidate_text = str(candidate_body["choices"][0]["message"]["content"] or "").strip()
            if not candidate_text:
                raise ValueError("empty output")
            body = candidate_body if isinstance(candidate_body, dict) else {}
            text = candidate_text
            used_model = model
            break
        except Exception as exc:  # noqa: BLE001
            detail = str(response.text if response is not None else "")[:800]
            errors.append(f"{model}: {exc}; {detail}")
            if not _explicit_model_rejection(response):
                raise ContentFactoryApiError(
                    f"ToAPIs {stage} failed without safe model failover: {errors[-1]}"
                ) from exc
    if not text:
        raise ContentFactoryApiError(f"ToAPIs {stage} failed: {' | '.join(errors)}")
    if not text:
        raise ContentFactoryApiError(f"ToAPIs {stage} returned empty output")
    envelope = _json_object_from_text(
        text,
        error_prefix=(
            "routed CREATIVE_REVIEW"
            if stage == "CREATIVE_REVIEW"
            else f"ToAPIs {stage}"
        ),
    )
    if not isinstance(envelope.get("result"), dict):
        result = {
            key: envelope.pop(key)
            for key in list(envelope)
            if key in set(packet.get("required_result_fields") or [])
        }
        envelope["result"] = result
    if stage == "CREATIVE_REVIEW":
        envelope = _apply_creative_review_reference_gate(envelope, packet)
    envelope.update(
        {
            "schema_version": str(envelope.get("schema_version") or "1.0"),
            "execution_id": str(packet.get("execution_id") or ""),
            "project_id": str(packet.get("project_id") or ""),
            "stage": stage,
            "status": str(envelope.get("status") or "PASS"),
            "next_stage": str(packet.get("required_next_stage") or ""),
        }
    )
    envelope.setdefault("evidence", {})
    envelope.setdefault("issues", [])
    envelope.setdefault("repair_brief", None)
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    usage = body.get("usage") if isinstance(body, dict) else {}
    return text, {
        "provider": provider_name,
        "model": used_model,
        "logical_model": (
            logical_model if stage == "CREATIVE_REVIEW" else None
        ),
        "model_failover_used": used_model != preferred_model,
        "semantic_model_route": (
            "alternate_preferred" if prefer_alternate_model else "default"
        ),
        "usage": usage if isinstance(usage, dict) else {},
        "prompt_chars": len(system) + len(user),
    }




# Keep this source ASCII-safe while still recognizing common Chinese product
# terms returned by upstream models.
_VISUAL_PRODUCT_TERMS = re.compile(
    r"\b(?:products?|packages?|packaging|bottles?|jars?|tubes?|labels?|logos?|gumm(?:y|ies)|supplements?|"
    r"capsules?|pills?|tablets?|boxes?|cartons?|cleaning\s+tablets?|cleaning\s+pods?|laundry\s+pods?)\b|"
    r"\u4ea7\u54c1|\u5546\u54c1|\u5305\u88c5|\u74f6|\u7f50|\u8f6f\u7cd6|"
    r"\u8865\u5145\u5242|\u6807\u7b7e|\u5546\u6807|\u6e05\u6d01\u7247|\u6d17\u8863\u7247|\u6d17\u8863\u51dd\u73e0",
    flags=re.IGNORECASE,
)

# Positive package-authority detection must be narrower than the vocabulary
# used for negative/exclusion parsing. Ordinary story props such as a water
# bottle, shipping box, gallery label, or jar are not the advertised product
# and must never cause an authoritative product upload on an early beat.
_VISUAL_PRODUCT_AUTHORITY_TERMS = re.compile(
    r"\b(?:products?|gumm(?:y|ies)|supplements?|capsules?|pills?|tablets?|"
    r"cleaning\s+tablets?|cleaning\s+pods?|laundry\s+pods?)\b|"
    r"\b(?:approved|uploaded|authoritative|actual|branded)\s+"
    r"(?:product|package|packaging|bottle|jar|tube|label|box|carton)\b|"
    r"\b(?:product|package)\s+(?:bottle|jar|tube|label|box|carton|anchor|reference|image)\b|"
    r"\u4ea7\u54c1|\u5546\u54c1|\u8f6f\u7cd6|\u8865\u5145\u5242|"
    r"\u4ea7\u54c1\u5305\u88c5|\u4ea7\u54c1\u74f6|\u6e05\u6d01\u7247|\u6d17\u8863\u7247|\u6d17\u8863\u51dd\u73e0",
    flags=re.IGNORECASE,
)


_VISUAL_STANDALONE_PRODUCT_TERMS = re.compile(
    r"\b(?:standalone|stand-alone|product[- ]?only|packshot|hero product|product hero|"
    r"white[- ]?background|label study|logo study|sales card)\b|"
    r"\u72ec\u7acb\u4ea7\u54c1|\u4ea7\u54c1\u4e3b\u89c6\u89c9|\u767d\u5e95\u4ea7\u54c1|"
    r"\u767d\u5e95\u5305\u88c5|\u6807\u7b7e\u5c55\u793a|\u5546\u6807\u5c55\u793a",
    flags=re.IGNORECASE,
)

_VISUAL_PRODUCT_EXCLUSION_TERMS = re.compile(
    r"\bno\s+(?:product(?:s|-like(?:\s+(?:items?|objects?))?)?|package|packaging|bottle|jar|label|logo)"
    r"(?:\s+(?:or|and)\s+(?:branding|brand|logo))?(?=\s*(?:[.,;:!?)]|$))|"
    r"\bno\s+(?:product|package|packaging|bottle|jar)\s+(?:appears?|shown|visible)\b|"
    r"\bwithout\s+(?:any\s+|the\s+)?(?:product|package|packaging|bottle|jar|branding|logo)\b|"
    r"\b(?:do\s+not|don't|never)\s+(?:show|include|display|add|create|generate|feature)\s+"
    r"(?:any\s+|the\s+|an?\s+)?(?:standalone\s+|stand-alone\s+|product-only\s+)?"
    r"(?:product|package|packaging|bottle|jar|branding|logo|packshot)\b|"
    r"\b(?:product|package|packaging|bottle|jar)\s+"
    r"(?:(?:must|should|does|do|is|are)\s+)?(?:not|never)\s+"
    r"(?:appear|be\s+shown|be\s+visible|be\s+included)\b|"
    r"\bproduct[- ]free(?:\s+(?:board|panel|frame))?\b|"
    r"\b(?:completely\s+)?free\s+of\s+(?:any\s+)?(?:products?|packages?|packaging|bottles?|jars?|labels?|logos?|branding)\b|"
    r"\u4e0d\u8981\u51fa\u73b0\u4ea7\u54c1|\u4e0d\u5c55\u793a\u4ea7\u54c1|"
    r"\u65e0\u4ea7\u54c1|\u4e0d\u542b\u4ea7\u54c1|\u7981\u6b62\u4ea7\u54c1",
    flags=re.IGNORECASE,
)


_VISUAL_REFERENCE_CONTRACT_MARKERS = (
    "If the product appears in this keyframe,",
    "This keyframe is product-free:",
    "Character, scene, or action continuity for this scripted beat.",
)


def _visual_reference_semantic_text(text: str) -> str:
    """Return the scripted keyframe description without appended policy text."""
    value = _text(text, 1200)
    marker_positions = [
        value.find(marker)
        for marker in _VISUAL_REFERENCE_CONTRACT_MARKERS
        if value.find(marker) >= 0
    ]
    if marker_positions:
        value = value[:min(marker_positions)]
    return value.strip()


def _visual_reference_excludes_product(text: str) -> bool:
    value = _text(text, 1200)
    if _VISUAL_PRODUCT_EXCLUSION_TERMS.search(value):
        return True
    # Models often write list-style exclusions such as "no readable text,
    # products, labels, or branding". The leading "no" and the product noun
    # are not adjacent, so inspect each sentence/semicolon clause as one
    # semantic unit instead of relying only on an adjacent-word regex.
    for clause in re.split(r"(?<=[.!?])\s+|[;|\n]+", value):
        if not _VISUAL_PRODUCT_TERMS.search(clause):
            continue
        # A scene may prohibit opening, loose contents, or consumption while
        # still requiring the package. That handling constraint is not a
        # product-free instruction; interpreting
        # it as one drops the authoritative package reference from the final
        # conversion beat.
        if re.search(
            r"\bno\s+(?:loose|spilled|removed)\s+[a-z][\w-]*\b|"
            r"\bno\s+consumption\b|\bdo\s+not\s+(?:open|consume|use)\b",
            clause,
            re.IGNORECASE,
        ):
            continue
        # A manner phrase such as "straightens the bottle without opening it"
        # constrains product handling; it does not remove the product from the
        # scene. Explicit "without [a] product" forms are already handled by
        # _VISUAL_PRODUCT_EXCLUSION_TERMS above. Keep this list-style fallback
        # for actual omission vocabulary, but never treat a bare "without"
        # elsewhere in a product sentence as a product-free instruction.
        if re.search(
            r"\bno\b|\bfree\s+of\b|\bexclude(?:s|d)?\b|\bomit(?:s|ted)?\b",
            clause,
            re.IGNORECASE,
        ):
            return True
    return False


def visual_reference_mentions_product(text: str) -> bool:
    """Whether this scripted frame needs the user's product authority attached.

    Uploading the real product reference and allowing AI to create a standalone
    product anchor are separate decisions. A product may appear or dissolve in
    a scripted scene even though generated packshots remain forbidden.
    """
    value = _visual_reference_semantic_text(text)
    # Narrative labels such as "bridge before product introduction" describe
    # a product-free beat; they must not turn that entire segment into a
    # package-visible image merely because they contain the word "product".
    if re.search(
        r"\b(?:before|prior to) (?:the )?product(?: introduction| appears?| entry| reveal)?\b|"
        r"\bproduct[- ]free\b|\bdo not (?:show|introduce) (?:the )?product\b",
        value,
        flags=re.IGNORECASE,
    ):
        return False
    branded_package = bool(re.search(
        r"\b[A-Z][A-Z0-9_-]{2,}\s+"
        r"(?:product|package|packaging|bottle|jar|tube|box|carton)\b",
        value,
    ))
    return bool(
        value
        and (_VISUAL_PRODUCT_AUTHORITY_TERMS.search(value) or branded_package)
        and not _visual_reference_excludes_product(value)
    )


def visual_reference_requires_product(text: str) -> bool:
    value = _visual_reference_semantic_text(text)
    return bool(
        visual_reference_mentions_product(value)
        and not _VISUAL_STANDALONE_PRODUCT_TERMS.search(value)
    )


def visual_reference_description(text: str, *, index: int) -> str:
    """Keep scripted product interaction, but reject generated product anchors.

    The user's uploaded packshot is the only product-identity authority. A
    generated keyframe may include that exact product when the scene needs a
    handoff, reveal, or use beat. A product-only hero/packshot panel is replaced
    with a useful action reference so the model never invents a second product
    anchor or a synthetic white-background package image.
    """
    value = _text(text, 700)
    # Policy sentences are appended by this normalizer and may survive a
    # creative replan. They must never overrule the actual shot. In particular,
    # a stale "product-free" suffix previously converted "tablet appears" into
    # a contradictory product-free panel on the next normalization pass.
    semantic = _visual_reference_semantic_text(value)
    semantic = semantic or f"Character, scene, or action continuity reference {index}"
    mentions_product = visual_reference_mentions_product(semantic)
    excludes_product = _visual_reference_excludes_product(semantic)

    if mentions_product and not excludes_product and not _VISUAL_STANDALONE_PRODUCT_TERMS.search(semantic):
        return (
            semantic.rstrip(" .")
            + ". If the product appears in this keyframe, copy it only from the uploaded product reference; "
            "do not redesign its package, label, cap, geometry, colors, or proportions."
        )[:900]
    if excludes_product:
        explicit = (
            "This keyframe is product-free: do not show any product, package, bottle, label, logo, or branding."
        )
        preserved = semantic.strip(" .")
        if explicit.lower() in preserved.lower():
            return preserved[:900]
        return ((preserved + ". ") if preserved else "") + explicit
    if not mentions_product:
        return semantic[:900]

    safe_clauses = [
        clause.strip(" .;|-")
        for clause in re.split(r"(?<=[.!?])\s+|[;|\n]+", semantic)
        if clause.strip() and not _VISUAL_PRODUCT_TERMS.search(clause)
    ]
    safe_prefix = (". ".join(safe_clauses).rstrip(" .") + ". ") if safe_clauses else ""
    return (
        safe_prefix
        + "Character, scene, or action continuity for this scripted beat. Do not create a standalone product, "
        "packshot, white-background package image, label study, logo study, or product-only panel; the user's "
        "uploaded product image remains the separate product anchor."
    )[:900]


def _static_reference_action_state(text: Any, *, index: int) -> str:
    """Turn a video beat into one visible still-image aftermath.

    Reference images lock appearance and the decisive end state; they do not
    prove camera motion or an object's trajectory.  Creative providers still
    occasionally put tracking, pans, and multi-step movement in
    ``reference_plan`` even though the complete script already owns that
    timeline.  Compile those phrases into a static state before either the
    image model or the pixel reviewer sees them.
    """
    value = _visual_reference_semantic_text(_text(text, 1400))
    value = re.sub(
        r"^\s*(?:loss hook|knife[- ]?twist (?:moment|incident)|"
        r"recognition and (?:routine )?bridge|"
        r"product routine and cta|resolution|opening|development|payoff)\s*:\s*",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    )
    value = re.split(r"\s*\|\s*", value, maxsplit=1)[0].strip()
    value = re.split(
        r"(?<=[.!?])\s+(?=(?:cut|transition|move|pan|zoom|dolly|camera)\b)",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    value = re.sub(
        r"\bshow\s+(?:a\s+series\s+of\s+)?close[- ]?ups?\s+of\b",
        "One extreme macro still shows one combined physical mass containing",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+enters?\s+([^.;,]+?)\s+carrying\s+([^.;,]+)",
        lambda match: (
            f"{match.group(1)} stands in {match.group(2).strip()} holding "
            f"{match.group(3).strip()}"
        ),
        value,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+opens?\s+(his|her|their)\s+"
        r"([^.;,]+?)\s+into\s+([^.;,]+?)\s+and\s+freezes?\s+when\s+"
        r"(?:he|she|they)\s+sees?\s+([A-Z][A-Za-z0-9'_-]*)\s+([^.;]+)",
        lambda match: (
            f"{match.group(1)} stands beside {match.group(2)} open "
            f"{match.group(3).strip()} in {match.group(4).strip()}, facing "
            f"{match.group(5)} with a visibly startled expression; "
            f"{match.group(5)} stands {match.group(6).strip()}"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+(?:unexpectedly\s+)?steps?\s+out\s+from\s+"
        r"[^.;,]+,\s*sees?\s+([A-Z][A-Za-z0-9'_-]*),\s*then\s+(?:quietly\s+)?"
        r"passes?\s+(?:her|him|them)\s+toward\s+([^.;]+?)\s+without\s+([^.;]+)",
        lambda match: (
            f"{match.group(1)} stands near {match.group(3).strip()}, turned away from "
            f"{match.group(2)}, without {match.group(4).strip()}"
        ),
        value,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+turns?\s+and\s+follows?\s+"
        r"([A-Z][A-Za-z0-9'_-]*)\b[^.]*\.\s*\2\s+stops?\s+at\s+"
        r"([^.;]+?)\s+but\s+does\s+not\s+turn\s+fully\s+around\s+as\s+"
        r"(?:she|he|they)\s+speaks?",
        lambda match: (
            f"At {match.group(3).strip()}, {match.group(2)} stands partly turned away "
            f"from {match.group(1)}; {match.group(1)} stands several feet behind "
            f"{match.group(2)}"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+follows?\s+"
        r"([A-Z][A-Za-z0-9'_-]*)\s+through\s+[^.;,]+,\s*"
        r"catching\s+(?:her|him|them)\s+at\s+([^.;]+)",
        lambda match: (
            f"At {match.group(3).strip()}, {match.group(1)} stands near "
            f"{match.group(2)}"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+keeps?\s+"
        r"(?:the\s+)?([^.;,]+)\s+at\s+(?:her|his|their)\s+side\s+and\s+"
        r"delivers?\s+[^.;]+",
        lambda match: (
            f"{match.group(1)} holds the {match.group(2).strip()} at her side "
            "with a guarded, resolved expression"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(She|He|They)\s+looks?\s+toward\s+[^.;,]+,\s*walks?\s+there,\s*"
        r"(?:has\s+)?clos(?:es|ed)\s+(her|his|their)\s+laptop,\s*"
        r"silences?\s+(?:her|his|their)\s+phone,\s*turns?\s+off\s+"
        r"(?:the\s+)?([^.;,]+),\s*then\s+returns?\s+to\s+(?:the\s+)?"
        r"([^.;,]+)\s+and\s+(?:has\s+visibly\s+)?dims?\s+(?:an?\s+|the\s+)?([^.;]+)",
        lambda match: (
            f"{match.group(1)} stands in the {match.group(4).strip()} beside the visibly dimmed "
            f"{match.group(5).strip()}; {match.group(2)} laptop is closed; "
            f"{match.group(2)} phone screen is dark; the {match.group(3).strip()} is off"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+(?:lightly\s+)?touches?\s+"
        r"(?:the\s+)?([^.;,]+),\s*knocks?,\s*and\s+"
        r"([A-Z][A-Za-z0-9'_-]*)\s+opens?\s+(?:the\s+)?([^.;]+)",
        lambda match: (
            f"{match.group(3)} stands beside the open {match.group(4).strip()}; "
            f"{match.group(1)} stands at the threshold beside the {match.group(2).strip()}"
        ),
        value,
    )
    # Collapse a common out-and-back story action into the only state a still
    # image can prove. Keeping both "carrying toward" and "returned to" in one
    # prompt asks the image model for mutually exclusive moments and has caused
    # deterministic provider failures.
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+exits\s+([^.;,]+?)\s+carrying\s+"
        r"((?:an?|the)\s+[^.;,]+?)\s+toward\s+[^.;,]+,\s*then\s+turns?\s+back\s+"
        r"and\s+returns?\s+it\s+(untouched\s+)?to\s+([^.;]+)",
        lambda match: (
            f"{match.group(1)} stands at {match.group(5).strip()} after returning "
            f"{match.group(3).strip()} {str(match.group(4) or '').strip()}; "
            f"{match.group(3).strip()} rests there"
        ),
        value,
    )
    # When a later clause explicitly closes the object just inspected, the
    # final keyframe owns the closed state; remove the obsolete look-at action.
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+looks?\s+at\s+"
        r"(?:his|her|their|the)\s+([^,.;]+),\s*(?:has\s+)?clos(?:es|ed)\s+it,\s*",
        lambda match: f"{match.group(1)} has closed the {match.group(2).strip()}, ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r",\s*looks?\s+at\s+(?:his|her|their|the)\s+([^,.;]+),\s*"
        r"(?:has\s+)?clos(?:es|ed)\s+it,\s*",
        lambda match: f", the {match.group(1).strip()} is closed, ",
        value,
        flags=re.IGNORECASE,
    )
    # If the next sentence already fixes both adults at the destination, the
    # preceding pursuit is timeline-only context and must not survive into the
    # keyframe description.
    value = re.sub(
        r"^.*?\b[A-Z][A-Za-z0-9'_-]*\s+follows\s+"
        r"[A-Z][A-Za-z0-9'_-]*\b[^.]*\.\s*"
        r"(?=At\s+[^.]+(?:threshold|doorway))",
        "",
        value,
    )
    rolling_object = ""
    rolling_match = re.search(
        r"\b(?:the\s+)?([a-z][a-z0-9' -]{0,36}?)\s+roll(?:s|ing|ed)?\b",
        value,
        flags=re.IGNORECASE,
    )
    if rolling_match:
        rolling_object = re.sub(r"\s+", " ", rolling_match.group(1)).strip()
        # Avoid swallowing a preceding camera phrase into the prop name.
        rolling_object = re.split(
            r"\b(?:with|as|while|and)\b",
            rolling_object,
            flags=re.IGNORECASE,
        )[-1].strip()
        rolling_object = re.sub(
            r"^(?:the|a|an)\s+", "", rolling_object, flags=re.IGNORECASE
        )

    def _recognition(match: re.Match[str]) -> str:
        person = str(match.group(1) or "The adult").strip()
        target = f"the {rolling_object}" if rolling_object else "the decisive prop"
        return f"{person} looks directly at {target} with immediate recognition"

    value = re.sub(
        r"(?:the\s+)?camera\s+catches\s+([A-Z][A-Za-z0-9'_-]*)['’]s\s+immediate\s+recognition",
        _recognition,
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:fast\s+)?(?:backward|forward)?\s*(?:tracking|track(?:ing)?)"
        r"(?:\s+(?:shot|camera))?\s+(?:retreats?|moves?|accelerates?|travels?)"
        r"(?:\s+door\s+to\s+door)?\s+(?:down|through|along)\s+the\s+",
        "In the ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:the\s+)?(?:backward|forward)\s+track\s+"
        r"(?:accelerates?|moves?|travels?)(?:\s+door\s+to\s+door)?\s+with\s+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:the\s+)?(?:camera|viewpoint)\s+(?:stabilizes?|settles?|pans?|"
        r"tilts?|zooms?|dollies?|tracks?|moves?|pushes?|pulls?)(?:\s+[^.;,]+)?[.;]?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:the\s+)?([A-Za-z][A-Za-z0-9' -]{0,36}?)\s+rolls?\s+into\s+(?:the\s+)?(?:frame|view)\b",
        lambda match: f"the {match.group(1).strip()} is stopped and fully visible",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:the\s+)?([A-Za-z][A-Za-z0-9' -]{0,36}?)\s+rolling\s+(?:ahead|away|forward)\b",
        lambda match: f"the {match.group(1).strip()} is stopped nearby",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+follows\s+(.+?)\s+toward\s+"
        r"(the\s+[^;,]+)(?=[;,])",
        r"\1 stands beside \3 with \2 visible nearby",
        value,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+catches\s+it\s+beside\s+([^,]+),\s*"
        r"kneels,\s*and\s+holds\s+the\s+silence\s+of\s+(.+?)(?=[.;]|$)",
        r"\1 is kneeling beside \2, holding it in \3",
        value,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+catches\s+it\b",
        r"\1 is holding it",
        value,
    )
    if rolling_object:
        value = re.sub(
            r"\bholding\s+it\b",
            f"holding the {rolling_object}",
            value,
            flags=re.IGNORECASE,
        )
    value = re.sub(r"\b([A-Z][A-Za-z0-9'_-]*)\s+kneels\b", r"\1 is kneeling", value)
    value = re.sub(r"\bturns?\s+toward\b", "faces", value, flags=re.IGNORECASE)
    value = re.sub(r"\bsteps?\s+to\b", "stands at", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+approaches?\s+(?:the\s+)?([^.;]+)",
        r"\1 stands at the \2",
        value,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+opens?\s+(?:the\s+)?door\b",
        r"\1 stands beside the open door",
        value,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+stops?\s+without\s+turning\s+fully\s+toward\s+"
        r"(?:him|her|them)\b",
        r"\1 stands partly turned away",
        value,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+stops?\s+([^.;,]+?)\s+and\s+lowers?\s+"
        r"(?:his|her|their)\s+hand\s+before\s+knocking\b",
        r"\1 stands \2 with one hand lowered, without knocking",
        value,
    )
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+(?:slowly\s+)?returns?\s+to\s+",
        r"\1 stands in ",
        value,
    )
    value = re.sub(
        r"\band\s+(?:slowly\s+)?returns?\s+to\s+(?:the\s+)?([^.;]+)",
        r"and stands in the \1",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bturns?\s+(?:his|her|their|the)\s+([^,.;]+?)\s+face-down\b",
        r"the \1 lies face-down",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bswitches?\s+off\s+(?:the\s+)?([^,.;]+)",
        r"the \1 is visibly off",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bturns?\s+off\s+(?:the\s+)?([^,.;]+)",
        r"the \1 is visibly off",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bsilences?\s+(?:his|her|their|the)\s+phone\b",
        "the phone screen is dark",
        value,
        flags=re.IGNORECASE,
    )
    # A closed drawer hides the phone and therefore cannot be proved by one
    # reference image. Preserve the wind-down state as a visible terminal fact
    # instead of asking the image model to depict an impossible hidden object.
    value = re.sub(
        r"\b(His|Her|Their|The)\s+phone\s+is\s+put\s+away\s+in\s+"
        r"(?:his|her|their|the|its)\s+closed\s+drawer\b",
        lambda match: (
            f"{match.group(1)} smartphone lies screen-down flat against the wood "
            "beside the visibly closed drawer; the black protective back and three "
            "circular rear lenses face the viewer, while the entire glass screen is "
            "pressed against the tabletop and cannot be seen"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:the\s+)?([A-Za-z0-9-]*key\s+hook)\s+is\s+empty\b",
        lambda match: (
            f"the empty {match.group(1).strip()} is clearly visible in the foreground, "
            "with its bare metal hook fully unobstructed and no keys hanging from it"
        ),
        value,
        flags=re.IGNORECASE,
    )
    # A nod is motion, not a stable image state. Compile it into visible
    # eye-line, expression, and head posture that communicate the same beat.
    value = re.sub(
        r"\b([A-Z][A-Za-z0-9'_-]*)\s+and\s+([A-Z][A-Za-z0-9'_-]*)\s+"
        r"share\s+a\s+calm\s+nod\b",
        lambda match: (
            f"{match.group(1)} and {match.group(2)} face each other with clear "
            "eye contact and calm softened expressions"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bthen\s+returns?\s+to\s+(?:the\s+)?([^,.;]+)\s+and\s+"
        r"(?:has\s+visibly\s+)?dims?\s+(?:an?\s+|the\s+)?([^.;]+)",
        r"stands in the \1 beside the visibly dimmed \2",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"(\b([A-Z][A-Za-z0-9'_-]*)\s+stands\b[^.]*?\bin\s+the\s+([^,.;]+))"
        r"(?P<middle>.*?),\s*and\s+stands\s+in\s+the\s+\3\b",
        lambda match: f"{match.group(1)}{match.group('middle')}",
        value,
    )
    # Only rewrite a present-tense action when the token is followed by an
    # explicit direct-object determiner.  Blanket token replacement corrupts
    # stable nouns and idioms such as "place cards", "held in place", "film
    # sets", "close friend", and "dim light".
    direct_object = (
        r"(?=\s+(?:a|an|the|it|them|his|her|their|its|my|our|your|this|that|these|those|"
        r"product|package)\b)"
    )
    value = re.sub(
        rf"\bplaces?\b{direct_object}",
        "has placed",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        rf"\bsets?\b{direct_object}",
        "has set",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        rf"\bcloses?\b(?![- ]?ups?\b){direct_object}",
        "has closed",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        rf"\bdims?\b{direct_object}",
        "has visibly dimmed",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:[A-Z][A-Za-z0-9'_-]*|She|He|They)\s+has\s+visibly\s+dimmed\s+"
        r"(?:an?\s+|the\s+)?([^.;]+)",
        lambda match: (
            f"The {match.group(1).strip()} emits only a faint low glow; "
            "the surrounding room is mostly dark"
        ),
        value,
    )
    value = re.sub(
        r"\bwithout\s+(?:their|his|her|the)\s+familiar\s+greeting\b",
        "while keeping visible physical distance and looking away",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bwithout\s+(?:a\s+)?greeting\b",
        "while keeping visible physical distance and looking away",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bbeside\s+the\s+visibly\s+dimmed\s+([^.;,]+)",
        lambda match: (
            f"beside the {match.group(1).strip()} emitting only a faint low glow; "
            "the surrounding room is mostly dark"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:the\s+)?([^.;,]*(?:sconce|lamp|light))\s+is\s+lowered\b",
        lambda match: (
            f"exactly one {match.group(1).strip()} emits a faint low glow while every "
            "other hallway light is visibly off; the hallway remains readable but dark"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(?:tracking|track|pan|panning|zoom|dolly|camera|shot|frame)\b",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\bhallway\s+as\s+(?=[A-Z])", "hallway, ", value)
    value = re.sub(r"\s+([,.;])", r"\1", value)
    value = re.sub(r"\s+", " ", value).strip(" .;")
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", value)
        if sentence.strip()
    ]
    if len(sentences) > 1 and any(
        re.search(r"\bstands?\s+in\s+the\b", sentence, flags=re.IGNORECASE)
        for sentence in sentences[1:]
    ):
        sentences = [
            sentence
            for sentence in sentences
            if not re.search(
                r"\bthen\s+(?:walks?|heads?|moves?|returns?)\b",
                sentence,
                flags=re.IGNORECASE,
            )
        ]
        value = " ".join(sentences)
    value = re.sub(
        r"^(She|He|They)\b(.+?),\s*and\s+stands\s+",
        lambda match: f"{match.group(1)}{match.group(2)}; {match.group(1)} stands ",
        value,
        flags=re.IGNORECASE,
    )
    if value:
        value = value[0].upper() + value[1:]
    if not value:
        value = f"Required static continuity state {index}"
    return _text(value, 900)


def visual_reference_static_state(text: Any, *, index: int) -> str:
    """Public compiler shared by creative normalization and visual execution."""
    return _static_reference_action_state(text, index=index)


def _single_frame_visual_reference_description(text: str, *, index: int) -> str:
    """Compile a creative beat into one still-image moment.

    Creative plans may legitimately describe cuts, camera moves, or a sequence.
    Passing that language to an image model encourages nested frames and mini
    montages. Keep the first semantic shot, remove motion-direction clauses, and
    preserve the product/product-free contract exactly once.
    """
    value = visual_reference_description(text, index=index)
    contract = ""
    contract_markers = (
        "If the product appears in this keyframe,",
        "This keyframe is product-free:",
        "Character, scene, or action continuity for this scripted beat.",
    )
    marker_positions = [value.find(marker) for marker in contract_markers if marker in value]
    if marker_positions:
        contract_start = min(position for position in marker_positions if position >= 0)
        contract = value[contract_start:].strip()
        value = value[:contract_start].strip()

    # Pipe-delimited creative metadata describes purpose/direction, not
    # additional still images. Keep only the actual scene description.
    value = re.split(r"\s*\|\s*", value, maxsplit=1)[0].strip()
    # A second sentence beginning with an edit command is another shot. The
    # reference board needs the first locked moment only.
    value = re.split(
        r"(?<=[.!?])\s+(?=(?:cut|then|transition|move|pan|zoom|dolly|camera)\b)",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    value = re.sub(
        r"\bshow\s+(?:a\s+series\s+of\s+)?close[- ]?ups?\s+of\b",
        "One extreme macro still shows one combined physical mass containing",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\bfantasy\s+sequence\b", "single suspended fantasy moment", value, flags=re.IGNORECASE)
    value = re.sub(r"\bsequence\b", "single suspended moment", value, flags=re.IGNORECASE)
    value = re.sub(r"\bproduct\s+rotation\b", "fixed three-quarter product view", value, flags=re.IGNORECASE)
    value = _static_reference_action_state(value, index=index)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = f"Character, scene, or action continuity reference {index}"
    result = value + "."
    if contract:
        result += " " + contract
    return result[:900]


def _single_frame_terminal_state_hint(text: Any) -> str:
    """Extract the last decisive still state from a multi-action story beat.

    Creative owns the full motion sequence, while one reference image can show
    only one instant. The visual generator already follows that rule; expose
    the same compact terminal-state hint to the reviewer so it never demands
    every earlier action in one impossible still.
    """
    return _static_reference_action_state(text, index=1)


def _visual_segment_number(value: Any, fallback: int) -> int:
    """Coerce a creative segment identifier without interpreting timecodes."""
    if isinstance(value, bool):
        return max(1, int(fallback))
    if isinstance(value, (int, float)):
        return max(1, int(value))
    text = str(value or "").strip()
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        return max(1, int(float(text)))
    match = re.search(
        r"(?:segment|seg|\u7247\u6bb5|\u955c\u5934)\s*#?\s*(\d+)",
        text,
        flags=re.IGNORECASE,
    )
    return max(1, int(match.group(1))) if match else max(1, int(fallback))


def _shot_visual_states_by_segment(creative: dict[str, Any]) -> dict[int, str]:
    """Return only concrete static scene state, never camera or spoken copy."""
    states: dict[int, str] = {}
    for position, raw in enumerate(_json_list(creative.get("shot_plan"))[:64], 1):
        shot = _json_dict(raw)
        if not shot:
            continue
        state = _text(
            shot.get("visual_state")
            or shot.get("terminal_state")
            or shot.get("final_state")
            or "",
            900,
        )
        if state:
            states[_visual_segment_number(shot.get("segment"), position)] = state
    return states


def _reference_plan(packet: dict[str, Any]) -> list[dict[str, Any]]:
    product_allowed = bool(packet.get("product_required", True))
    previous = _json_dict(packet.get("previous_outputs"))
    creative = _json_dict(previous.get("MEDIA_DESIGN"))
    ticket = _json_dict(creative.get("visual_job_ticket"))
    signed_production_plan = (
        str(ticket.get("source") or "").strip().lower()
        == "directed_production_plan"
    )
    raw = _json_list(ticket.get("reference_plan") or creative.get("reference_plan"))
    detailed = _json_list(ticket.get("reference_panels"))
    shot_visual_states = _shot_visual_states_by_segment(creative)
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw[:64], 1):
        if not isinstance(item, dict):
            continue
        detail = detailed[index - 1] if index <= len(detailed) and isinstance(detailed[index - 1], dict) else {}
        description = _text(
            item.get("single_frame_terminal_state")
            or item.get("description")
            or item.get("shot")
            or "",
            520,
        )
        if not description or re.fullmatch(r"reference\s+panel\s+\d+", description, flags=re.IGNORECASE):
            description = _text(
                ". ".join(
                    str(detail.get(key) or "").strip()
                    for key in (
                        "name", "purpose", "composition", "camera", "performance",
                        "product_visibility", "ending_action", "final_frame",
                    )
                    if str(detail.get(key) or "").strip()
                ),
                700,
            )
        segment_number = _visual_segment_number(item.get("segment"), index)
        # Legacy creative envelopes can contain an abstract reference-plan row
        # even though the matching shot_plan owns a concrete visual_state. A
        # signed Production Plan is the opposite: its per-reference row is the
        # immutable still-image authority and several distinct references may
        # intentionally share one video segment. Overwriting those rows by
        # segment collapses, for example, an entry-table seal insert and a
        # later bedside product anchor into the same mixed timeline.
        concrete_shot_state = shot_visual_states.get(segment_number, "")
        if concrete_shot_state and not signed_production_plan:
            description = concrete_shot_state
        roles = [
            str(role).strip().lower()
            for role in list(item.get("roles") or item.get("semantic_roles") or [])[:4]
            if str(role).strip().lower() in {"character_anchor", "scene_anchor", "action_anchor"}
        ]
        if not roles:
            roles = ["action_anchor"]
        source_asset_refs = [
            str(value).strip()
            for value in list(item.get("source_asset_refs") or [])[:64]
            if str(value).strip()
        ]
        generation_mode = str(
            item.get("generation_mode") or "generate"
        ).strip().lower()
        if generation_mode != "generate":
            raise ValueError(
                "CONTENT_PRODUCTION_PLAN_LEGACY_SOURCE_REUSE_UNSUPPORTED: "
                "every visual reference must be a generated scene; uploaded "
                "assets are guidance or authoritative product pixels only"
            )
        requires_product_reference = (
            product_allowed
            and (
                visual_reference_requires_product(description)
                or (
                    signed_production_plan
                    and bool(item.get("requires_product_reference"))
                )
            )
        )
        compiled_description = _single_frame_visual_reference_description(
            description,
            index=index,
        )
        terminal_state = _single_frame_terminal_state_hint(description)
        if (
            terminal_state
            and visual_reference_mentions_product(terminal_state)
            and not requires_product_reference
        ):
            terminal_state = ""
        result.append(
            {
                "index": int(item.get("index") or index),
                "reference_id": _text(
                    item.get("reference_id") or f"reference-{index}",
                    128,
                ),
                "segment": segment_number,
                "segments": [
                    int(value)
                    for value in list(item.get("segments") or [])[:64]
                    if str(value).strip().isdigit() and int(value) > 0
                ] or [segment_number],
                "description": compiled_description,
                "single_frame_terminal_state": terminal_state,
                "roles": roles,
                "source_asset_refs": source_asset_refs,
                "generation_mode": generation_mode,
                # Only a signed Production Plan may supplement description-
                # based product detection. Historical creative payloads may
                # not re-enable a standalone package panel with one boolean.
                "requires_product_reference": requires_product_reference,
                "product_render_mode": "provider_reference",
            }
        )
    expected = int(
        ticket.get("reference_image_count")
        or ticket.get("final_reference_count")
        or creative.get("reference_image_count")
        or len(result)
        or 1
    )
    while len(result) < expected:
        index = len(result) + 1
        result.append(
            {
                "index": index,
                "segment": f"reference {index}",
                "description": f"Required continuity or action reference {index}",
                "single_frame_terminal_state": "",
                "roles": ["action_anchor"],
                "reference_id": f"reference-{index}",
                "segments": [index],
                "source_asset_refs": [],
                "generation_mode": "generate",
                "requires_product_reference": False,
            }
        )
    return result[:expected]


_EMOTIONAL_REFERENCE_RE = re.compile(
    r"\b(?:smiles?|eye[- ]?contact|mutual gaze|look(?:s|ed|ing)? at (?:each other|one another)|"
    r"reconnect(?:s|ed|ion)?|reconcil(?:e|es|ed|iation)|relief|reassur(?:e|es|ed|ing)|"
    r"affection|warm expression|soft expression)\b",
    flags=re.IGNORECASE,
)

_INVENTED_APPEARANCE_DETAIL_RE = re.compile(
    r"\b(?:wardrobe|clothing|outfit|attire|cardigan|sweater|shirt|blouse|t-?shirt|"
    r"trousers?|pants|jeans|skirt|dress|shoes?|sneakers?|boots?|hair(?:style|cut|"
    r"texture|shade|color)?|accessor(?:y|ies)|earrings?|necklace|bracelet|watch|"
    r"handbag|tote|backpack|exact color|colour)\b",
    flags=re.IGNORECASE,
)
_AUTHORITATIVE_CHARACTER_APPEARANCE_RE = re.compile(
    r"\b(?:must wear|must be wearing|required wardrobe|exact wardrobe|character "
    r"reference|match the uploaded character|wearing a|wearing an|dressed in)\b",
    flags=re.IGNORECASE,
)
_NON_APPEARANCE_VISUAL_DEFECT_RE = re.compile(
    r"\b(?:child|teen|minor|age|adult count|extra (?:adult|person|character)|"
    r"missing (?:adult|person|character)|different (?:adult|person|character)|"
    r"identity drift|wrong (?:adult|person|character|scene|location|action|beat)|"
    r"scene|location|terminal state|terminal action|action|gaze|expression|emotion|"
    r"surface|phone|keys?|card|drawer|chair|board|notice|product|bottle|package|"
    r"label|logo|collage|grid|split screen|portrait|landscape)\b",
    flags=re.IGNORECASE,
)


def _packet_has_authoritative_character_appearance(packet: dict[str, Any]) -> bool:
    assets = list(packet.get("browser_assets") or [])
    if any(
        isinstance(asset, dict)
        and str(asset.get("role") or asset.get("asset_role") or asset.get("kind") or "").strip().lower()
        == "character_reference"
        for asset in assets
    ):
        return True
    explicit_requirements = " ".join(
        str(packet.get(key) or "")
        for key in ("project_requirements", "user_instruction")
    )
    return bool(_AUTHORITATIVE_CHARACTER_APPEARANCE_RE.search(explicit_requirements))


def _harmless_invented_appearance_difference(value: Any) -> bool:
    text = _text(value, 800)
    return bool(
        text
        and _INVENTED_APPEARANCE_DETAIL_RE.search(text)
        and not _NON_APPEARANCE_VISUAL_DEFECT_RE.search(text)
    )


_UNOBSERVABLE_STILL_FACT_RE = re.compile(
    r"(?:"
    r"\b(?:exhale|inhale|breath(?:e|es|ing)?|breathing)\b"
    r"|small (?:phone|screen|ui) text"
    r"|(?:phone|screen|message|sender|notification|app|ui).{0,48}"
    r"(?:unreadable|not readable|cannot be read|not visibly readable|"
    r"cannot be verified|cannot be confirmed|not visibly confirmable|"
    r"disabled|turned off|switched off)"
    r"|(?:notification|app|ui) (?:setting|settings|state|status)"
    r"|(?:sender name|named sender)"
    r")",
    flags=re.IGNORECASE,
)


def _unobservable_still_fact(value: Any) -> bool:
    """Return true only for facts a silent still intrinsically cannot prove."""
    text = _text(value, 1000)
    return bool(text and _UNOBSERVABLE_STILL_FACT_RE.search(text))


def _reference_requires_visible_character(item: dict[str, Any]) -> bool:
    """Let the signed reference roles decide whether a person must be visible."""
    roles = {
        str(value or "").strip().lower().replace("-", "_")
        for value in list(item.get("roles") or [])
        if str(value or "").strip()
    }
    return any(
        role == "character" or role.startswith("character_")
        for role in roles
    )


def _review_observes_no_visible_character(row: dict[str, Any]) -> bool:
    value = _text(row.get("observed_characters"), 600).lower()
    return bool(
        value
        and re.search(
            r"\b(?:no|without)\s+(?:visible\s+)?(?:person|people|adult|character|human|face)\b|"
            r"\bonly\s+(?:desk|scene|room|object|prop)",
            value,
        )
    )


def _apply_creative_review_reference_gate(
    envelope: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed unless the vision reviewer proves every visible contract.

    A narrative summary such as "the doorway reconnection occurs" is not
    evidence that both characters, their gaze/expression, the decisive action,
    and the scripted product-placement surface are actually visible.  This
    gate requires a per-image observation record and treats omissions or
    uncertainty as a repair request.
    """
    normalized = dict(envelope or {})
    result = _json_dict(normalized.get("result"))
    plan = _reference_plan(packet)
    raw_checks = result.get("reference_checks")
    checks = list(raw_checks) if isinstance(raw_checks, list) else []
    by_index: dict[int, dict[str, Any]] = {}
    failures: list[str] = []
    structural_failures: list[str] = []

    for raw in checks:
        row = _json_dict(raw)
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            structural_failures.append("a reference check has no valid index")
            continue
        if index in by_index:
            structural_failures.append(f"reference {index} has duplicate checks")
            continue
        by_index[index] = row

    expected_indices = [int(item.get("index") or position) for position, item in enumerate(plan, 1)]
    if len(checks) != len(plan):
        structural_failures.append(
            f"reference check count is {len(checks)} but the plan requires {len(plan)}"
        )
    if sorted(by_index) != sorted(expected_indices):
        structural_failures.append("reference check indices do not exactly match the ordered plan")
    for index in expected_indices:
        row = by_index.get(index)
        if row is None:
            continue
        if not isinstance(row.get("observed_facts"), list):
            structural_failures.append(
                f"reference {index} observed_facts must be a JSON list"
            )
        if not isinstance(row.get("missing_or_wrong_facts"), list):
            structural_failures.append(
                f"reference {index} missing_or_wrong_facts must be a JSON list"
            )
    if structural_failures:
        # Missing/duplicate per-image rows mean the reviewer failed its output
        # contract. Wrong evidence-container types are the same class of
        # reviewer failure: a prose string cannot be treated as a list of
        # pixel-grounded defects and then used to redraw a valid image.
        # These failures are not evidence that any source image is wrong.
        # Raise into the text semantic-regeneration path instead of creating a
        # visual repair request that would spend image quota blindly.
        concise = "; ".join(dict.fromkeys(structural_failures))[:1600]
        raise ContentFactoryApiError(
            "CREATIVE_REVIEW reference_checks contract incomplete: " + concise
        )

    ignored_appearance_differences: list[dict[str, Any]] = []
    ignored_unobservable_still_facts: list[dict[str, Any]] = []
    appearance_is_authoritative = _packet_has_authoritative_character_appearance(packet)
    if not appearance_is_authoritative:
        for index, row in by_index.items():
            missing = row.get("missing_or_wrong_facts")
            missing_rows = (
                [_text(value, 800) for value in missing if _text(value, 800)]
                if isinstance(missing, list)
                else []
            )
            if (
                missing_rows
                and all(_harmless_invented_appearance_difference(value) for value in missing_rows)
                and str(row.get("character_scene_verdict") or "").strip().lower() == "mismatch"
                and _text(row.get("observed_characters"), 600)
                and _text(row.get("observed_terminal_state"), 900)
            ):
                ignored_appearance_differences.append(
                    {"index": index, "reported_differences": missing_rows}
                )
                # A reviewer that incorrectly promotes creative-invented hair
                # or wardrobe into hard identity authority commonly marks all
                # three dependent verdicts as mismatch.  When its complete
                # defect list contains appearance details only, those verdicts
                # carry the same false premise.  Normalize them together.
                # Real action, scene, product, adult-count, or role defects
                # remain in missing_or_wrong_facts and therefore never enter
                # this branch.
                row["character_scene_verdict"] = "match"
                if str(row.get("continuity_verdict") or "").strip().lower() in {
                    "mismatch",
                    "uncertain",
                }:
                    row["continuity_verdict"] = "match"
                if str(row.get("terminal_action_verdict") or "").strip().lower() in {
                    "mismatch",
                    "uncertain",
                }:
                    row["terminal_action_verdict"] = "match"
                row["missing_or_wrong_facts"] = []

        # Keep the normalized rows in the returned audit record. `_json_dict`
        # deliberately copies reviewer objects, so returning the original
        # `checks` list would silently discard the normalization.
        checks = [by_index[index] for index in expected_indices]

    # Vision reviewers sometimes reject a visually complete frame because a
    # silent still cannot prove breathing, tiny UI copy, a sender name, or an
    # app/notification setting.  Normalize only rows whose *entire* defect set
    # is intrinsically unobservable and whose visible character, continuity,
    # emotional, and placement checks already pass.  A wrong pose, missing
    # object, scene mismatch, or absent product surface remains blocking.
    for index, row in by_index.items():
        missing = row.get("missing_or_wrong_facts")
        missing_rows = (
            [_text(value, 1000) for value in missing if _text(value, 1000)]
            if isinstance(missing, list)
            else []
        )
        terminal_verdict = str(row.get("terminal_action_verdict") or "").strip().lower()
        supporting_verdicts = [
            str(row.get(key) or "").strip().lower()
            for key in (
                "character_scene_verdict",
                "continuity_verdict",
                "emotional_beat_verdict",
                "placement_surface_verdict",
            )
        ]
        if (
            terminal_verdict in {"mismatch", "uncertain"}
            and missing_rows
            and all(_unobservable_still_fact(value) for value in missing_rows)
            and all(value in {"match", "not_required"} for value in supporting_verdicts)
            and _text(row.get("observed_terminal_state"), 900)
        ):
            ignored_unobservable_still_facts.append(
                {"index": index, "reported_differences": missing_rows}
            )
            row["terminal_action_verdict"] = "match"
            row["missing_or_wrong_facts"] = []
    if ignored_unobservable_still_facts:
        checks = [by_index[index] for index in expected_indices]

    for position, item in enumerate(plan, 1):
        index = int(item.get("index") or position)
        row = by_index.get(index)
        if row is None:
            failures.append(f"reference {index} has no visible-fact check")
            continue
        character_required = _reference_requires_visible_character(item)
        missing = row.get("missing_or_wrong_facts")
        missing_rows = (
            [_text(value, 400) for value in missing if _text(value, 400)]
            if isinstance(missing, list)
            else []
        )
        if (
            not character_required
            and not missing_rows
            and _review_observes_no_visible_character(row)
            and str(row.get("character_scene_verdict") or "").strip().lower()
            in {"mismatch", "uncertain"}
        ):
            # A phone/product/action close-up can intentionally contain no
            # person. The signed plan's roles, not a critic's generic person
            # rubric, decide whether that absence is a defect.
            row["character_scene_verdict"] = "not_required"
        required_verdicts = {
            "terminal_action_verdict": True,
            "continuity_verdict": True,
            "character_scene_verdict": character_required,
        }
        for field, required in required_verdicts.items():
            verdict = str(row.get(field) or "").strip().lower()
            accepted = {"match"} if required else {"match", "not_required"}
            if verdict not in accepted:
                # ``not_required`` is never valid for these three mandatory
                # fields.  Canonicalize it into a blocking row verdict so the
                # downstream partial-repair planner can identify the exact
                # image instead of seeing a contradictory top-level failure
                # with no failed reference index.
                if verdict not in {"mismatch", "uncertain"}:
                    row[field] = "mismatch"
                failures.append(f"reference {index} {field} is not match")

        observed_characters = _text(row.get("observed_characters"), 600)
        observed_terminal_state = _text(row.get("observed_terminal_state"), 900)
        observed_facts = row.get("observed_facts")
        if character_required and not observed_characters:
            row["character_scene_verdict"] = "mismatch"
            failures.append(f"reference {index} does not describe visible characters")
        if not observed_terminal_state:
            row["terminal_action_verdict"] = "mismatch"
            failures.append(f"reference {index} does not describe the visible terminal state")
        if not isinstance(observed_facts, list) or not any(_text(value, 300) for value in observed_facts):
            row["character_scene_verdict"] = "mismatch"
            failures.append(f"reference {index} has no pixel-grounded observed facts")

        if not isinstance(missing, list):
            row["terminal_action_verdict"] = "mismatch"
            failures.append(f"reference {index} missing_or_wrong_facts is not a list")
        elif any(_text(value, 400) for value in missing):
            failures.append(f"reference {index} reports missing or wrong visible facts")

        description = str(item.get("description") or "")
        emotion_required = bool(_EMOTIONAL_REFERENCE_RE.search(description))
        emotion_verdict = str(row.get("emotional_beat_verdict") or "").strip().lower()
        if emotion_required:
            if emotion_verdict != "match":
                row["emotional_beat_verdict"] = "mismatch"
                failures.append(f"reference {index} emotional beat is not visibly matched")
            if not _text(row.get("observed_gaze_expression"), 700):
                row["emotional_beat_verdict"] = "mismatch"
                failures.append(f"reference {index} does not describe visible gaze/expression")
        elif emotion_verdict not in {"match", "not_required"}:
            failures.append(f"reference {index} emotional verdict is invalid")

        product_surface_required = bool(item.get("requires_product_reference"))
        surface_verdict = str(row.get("placement_surface_verdict") or "").strip().lower()
        if product_surface_required:
            if surface_verdict != "match":
                row["placement_surface_verdict"] = "mismatch"
                failures.append(f"reference {index} product placement surface is not visibly matched")
            if not _text(row.get("observed_placement_surface"), 700):
                row["placement_surface_verdict"] = "mismatch"
                failures.append(f"reference {index} does not describe the visible placement surface")
        elif surface_verdict not in {"match", "not_required"}:
            failures.append(f"reference {index} placement-surface verdict is invalid")

    if failures:
        result["approved_for_split"] = False
        concise = "; ".join(dict.fromkeys(failures))[:2200]
        result["repair_brief"] = (
            "Regenerate only the failed native "
            f"{_packet_aspect_ratio(packet)[1]} reference frame(s) and make every planned terminal "
            f"state visibly explicit. Pixel-grounded review failures: {concise}."
        )
        normalized["repair_brief"] = result["repair_brief"]
        evidence = _json_dict(normalized.get("evidence"))
        evidence["pixel_grounded_reference_gate_passed"] = False
        evidence["pixel_grounded_reference_gate_failures"] = list(dict.fromkeys(failures))[:32]
        normalized["evidence"] = evidence
        issues = [
            issue
            for issue in list(normalized.get("issues") or [])
            if not (
                isinstance(issue, dict)
                and str(issue.get("code") or "") == "PIXEL_GROUNDED_REFERENCE_MISMATCH"
            )
        ]
        issues.append(
            {
                "severity": "blocker",
                "code": "PIXEL_GROUNDED_REFERENCE_MISMATCH",
                "issue": concise,
            }
        )
        normalized["issues"] = issues
    else:
        evidence = _json_dict(normalized.get("evidence"))
        evidence["pixel_grounded_reference_gate_passed"] = True
        evidence["pixel_grounded_reference_check_count"] = len(checks)
        if ignored_appearance_differences:
            evidence["ignored_harmless_invented_appearance_differences"] = (
                ignored_appearance_differences
            )
        if ignored_unobservable_still_facts:
            evidence["ignored_unobservable_still_facts"] = (
                ignored_unobservable_still_facts
            )
        if (
            ignored_appearance_differences
            or ignored_unobservable_still_facts
        ):
            evidence["original_creative_review"] = str(
                result.get("creative_review") or ""
            )[:4000]
            result["creative_review"] = (
                "Approved after server normalization: the reviewer reported only harmless "
                "creative-model-invented appearance details, facts a silent still cannot "
                "prove, while every physical visible contract matched."
            )
        if not bool(result.get("approved_for_split")):
            evidence["normalized_inconsistent_top_level_review_decision"] = True
        # The row-by-row pixel gate is authoritative.  A text model can return
        # approved_for_split=false while every required row verdict passes,
        # leaving the state machine with a rejection that has no repairable
        # image.  Recompute the top-level decision atomically from the
        # normalized rows and remove only stale pixel-gate issues.
        result["approved_for_split"] = True
        result["repair_brief"] = None
        normalized["repair_brief"] = None
        normalized["status"] = "PASS"
        normalized["issues"] = (
            []
            if (
                ignored_appearance_differences
                or ignored_unobservable_still_facts
            )
            else [
                issue
                for issue in list(normalized.get("issues") or [])
                if not (
                    isinstance(issue, dict)
                    and str(issue.get("code") or "")
                    == "PIXEL_GROUNDED_REFERENCE_MISMATCH"
                )
            ]
        )
        normalized["evidence"] = evidence

    # Always return the canonical rows.  ``_json_dict`` copies reviewer
    # objects, so returning the original list would otherwise discard verdict
    # corrections whenever a user-supplied character anchor is present.
    checks = [by_index[index] for index in expected_indices]
    result["reference_checks"] = checks
    # The vision model is evidence-only.  Even if it tries to return a revised
    # script, CTA, shot plan, or product strategy, those fields must not cross
    # this boundary and become a second creative authority.
    allowed_result_fields = {
        "creative_review",
        "approved_for_split",
        "reference_image_count",
        "repair_brief",
        "reference_checks",
    }
    dropped = sorted(set(result) - allowed_result_fields)
    normalized["result"] = {
        key: value
        for key, value in result.items()
        if key in allowed_result_fields
    }
    if dropped:
        evidence = _json_dict(normalized.get("evidence"))
        evidence["visual_acceptance_discarded_non_visual_fields"] = dropped
        normalized["evidence"] = evidence
    return normalized


def _packet_aspect_ratio(packet: dict[str, Any]) -> tuple[float, str]:
    value = str(packet.get("video_aspect_ratio") or "9:16").strip().lower()
    value = value.replace("x", ":").replace("/", ":")
    try:
        width_text, height_text = value.split(":", 1)
        width = float(width_text)
        height = float(height_text)
    except (TypeError, ValueError):
        return 9 / 16, "9:16"
    if width <= 0 or height <= 0:
        return 9 / 16, "9:16"
    return width / height, f"{width_text}:{height_text}"


def _single_visual_board_spec(
    plan: list[dict[str, Any]],
    *,
    board_index: int,
    board_count: int,
    aspect_ratio: tuple[float, str],
) -> dict[str, Any]:
    count = max(1, len(plan))
    row_columns = expected_row_columns(count)
    columns = max(row_columns)
    rows = len(row_columns)
    cell_ratio, aspect_ratio_label = aspect_ratio
    ratio = (columns * cell_ratio) / rows
    if ratio < 0.72:
        size = "1024x1792"
    elif ratio > 1.28:
        size = "1792x1024"
    else:
        size = "1024x1024"
    return {
        "count": count,
        "columns": columns,
        "rows": rows,
        "row_columns": row_columns,
        "size": size,
        "aspect_ratio": aspect_ratio_label,
        "plan": plan,
        "board_index": int(board_index),
        "board_count": int(board_count),
        "global_start_index": int(plan[0].get("index") or 1) if plan else 1,
        "global_end_index": int(plan[-1].get("index") or count) if plan else count,
    }


def visual_board_specs(packet: dict[str, Any], *, max_panels_per_board: int = 7) -> list[dict[str, Any]]:
    plan = _reference_plan(packet)
    aspect_ratio = _packet_aspect_ratio(packet)
    if not plan:
        plan = [{"index": 1, "segment": "reference 1", "description": "Required continuity reference", "roles": ["action_anchor"]}]
    # Four-panel contact sheets look compact, but image models routinely add
    # extra cells, leak a product into an earlier beat, or merge adjacent
    # actions.  For short-form segmented product videos, render one native
    # target-aspect reference per segment instead. That makes the reference-to-
    # provider binding explicit and lets the final product beat alone receive
    # the authoritative package image.
    if bool(packet.get("render_reference_images_individually")):
        return [
            _single_visual_board_spec(
                [item],
                board_index=index,
                board_count=len(plan),
                aspect_ratio=aspect_ratio,
            )
            for index, item in enumerate(plan, 1)
        ]
    limit = max(4, min(7, int(max_panels_per_board or 7)))
    board_count = max(1, math.ceil(len(plan) / limit))
    base, remainder = divmod(len(plan), board_count)
    specs: list[dict[str, Any]] = []
    offset = 0
    for board_index in range(1, board_count + 1):
        size = base + (1 if board_index <= remainder else 0)
        chunk = plan[offset:offset + size]
        specs.append(_single_visual_board_spec(
            chunk,
            board_index=board_index,
            board_count=board_count,
            aspect_ratio=aspect_ratio,
        ))
        offset += size
    return specs


def visual_board_spec(packet: dict[str, Any]) -> dict[str, Any]:
    return visual_board_specs(packet)[0]


def _layout_instruction(
    count: int,
    columns: int,
    rows: int,
    *,
    panel_orientation: str = "vertical",
) -> str:
    panel_label = f"{panel_orientation} panels"
    if count == 5:
        return (
            f"Use two rows: exactly three equal {panel_label} in the first row and two centered {panel_label} in the second row. "
            "Every second-row panel must have exactly the same width and height as a first-row panel; leave pure-white outer margins "
            "on both sides of the second row. Never stretch either lower panel to half-canvas width; no sixth cell."
        )
    if count == 7:
        return (
            f"Use two rows: exactly four equal {panel_label} in the first row and three centered {panel_label} in the second row. "
            "Every second-row panel must have exactly the same width and height as a first-row panel; leave pure-white outer margins "
            "on both sides of the second row. Never stretch the lower panels to fill the row; no eighth cell."
        )
    if count == 11:
        return (
            f"Use three rows containing exactly four, four, and three centered {panel_label}. Every third-row panel must have exactly "
            "the same width and height as a panel in the first two rows; leave pure-white outer margins on both sides of the third row. "
            "Never stretch the final row to fill the canvas; no twelfth cell."
        )
    return f"Use exactly {columns} columns and {rows} rows."


def _visual_generation_reference_entries(
    packet: dict[str, Any],
    *,
    plan: list[dict[str, Any]] | None = None,
    include_product: bool = True,
) -> list[tuple[dict[str, Any], str]]:
    """Return only explicit character and product-authority inputs.

    Fact/ingredient images never enter visual generation. Product images are
    included only so scripted interaction keyframes can copy the real package;
    the original upload still remains the separate downstream product anchor.
    """
    manifests = list(packet.get("browser_assets") or [])
    paths = list(packet.get("browser_asset_paths") or [])
    scoped_plan = plan if plan is not None else _reference_plan(packet)
    include_product_reference = (
        bool(include_product)
        and bool(packet.get("product_required", True))
        and any(
        bool(item.get("requires_product_reference"))
        for item in scoped_plan
        )
    )
    selected: list[tuple[dict[str, Any], str]] = []
    for asset, path in zip(manifests, paths):
        if not isinstance(asset, dict):
            continue
        role = str(asset.get("role") or asset.get("asset_role") or "").strip().lower()
        kind = str(asset.get("kind") or "").strip().lower()
        is_character = role == "character_reference" or kind == "character_reference"
        is_product = role == "product_visual" or kind == "product_visual"
        if is_character or (include_product and include_product_reference and is_product):
            selected.append((asset, str(path)))
    return selected[:5]


def visual_generation_reference_paths(
    packet: dict[str, Any],
    *,
    plan: list[dict[str, Any]] | None = None,
    include_product: bool = True,
) -> list[str]:
    """Return upload paths in exactly the order named by the visual prompt."""
    return [
        path
        for _asset, path in _visual_generation_reference_entries(
            packet, plan=plan, include_product=include_product,
        )
    ]


_IMAGE_PROMPT_PRODUCT_TRIGGER_RE = re.compile(
    r"\b(?:product|package|packaging|packshot|bottle|jar|label|logo|"
    r"branding|ingredient|offer|price|cta|shopping\s+cart|cart)\b|\$\s*\d",
    flags=re.IGNORECASE,
)

_IMAGE_PROMPT_PRODUCT_OBJECT_RE = re.compile(
    r"\b(?:(?:the|a|an)\s+)?"
    r"(?:(?:exact|sealed|closed|approved|original|uploaded|authoritative|"
    r"branded|labelled|labeled|deep[- ]?blue|navy[- ]?blue|blue|purple)\s+)*"
    r"(?:product(?:\s+package)?|package|packaging|packshot|bottle|jar)\b",
    flags=re.IGNORECASE,
)


def _image_prompt_without_product_triggers(
    value: Any,
    *,
    resolution_scene: bool = False,
    product_terms: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Keep scene direction while removing words that make image models invent packaging.

    Negative package instructions still put the package concept into the image
    model's attention.  Product-visible video segments therefore use an
    affirmative scene-only prompt and receive the untouched uploaded package
    as a separate downstream image input.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    for term in sorted(
        {
            str(item).strip()
            for item in list(product_terms or [])
            if str(item).strip()
        },
        key=len,
        reverse=True,
    ):
        text = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
            "",
            text,
            flags=re.IGNORECASE,
        )
    if resolution_scene:
        text = re.sub(
            r"^\s*(?:product|approved\s+details|cta|offer|conversion|sales)"
            r"[^:]{0,120}:\s*",
            "RESOLUTION: ",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    # Preserve the complete non-product action in a product-bearing sentence.
    # Replacing only the package noun phrase is safer than dropping its whole
    # coordinated clause. For example, "Mara lowers the task lamp, turns her
    # phone face-down, and holds the sealed product package beside the plaque"
    # must retain Mara, lamp, phone, counter and plaque. The exact uploaded
    # package is supplied separately to video generation.
    text = _IMAGE_PROMPT_PRODUCT_OBJECT_RE.sub(
        "a clear empty placement area",
        text,
    )
    # Repair/model prose can hyphenate the removed noun ("product-free",
    # "package-like") or describe a separate package input. Normalize those
    # remnants into affirmative scene language instead of feeding the image
    # model malformed phrases such as "placement area-free".
    text = re.sub(
        r"\b(?:a\s+)?clear empty placement area-free\b",
        "clear and empty",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:a\s+)?clear empty placement area-like object\b",
        "unwanted box-like object",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bseparate\s+(?:a\s+)?clear empty placement area\s+input\b",
        "separate downstream reference input",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(a clear empty placement area)"
        r"(?:\s+(?:and\s+)?a clear empty placement area)+\b",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(a clear empty placement area)\s+(?:lightly\s+)?"
        r"(?:held|gripped|carried|raised)(?:\s+at\s+[^,.;]+)?",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(a clear empty placement area)\s+"
        r"(?:rests?|resting|sits?|sitting|stands?|standing|is\s+placed)\b",
        r"\1 remains",
        text,
        flags=re.IGNORECASE,
    )
    # Split coordinated clauses too.  Product beats often combine a useful
    # scene state and the package action in one sentence ("closed score beside
    # the metronome and sets the bottle on the console"). Dropping the whole
    # sentence erased the scripted console and made the replacement frame
    # impossible to review.
    placement_surface = ""
    if resolution_scene:
        surface_match = re.search(
            r"\b(?:on|onto|at)\s+(?:the\s+)?"
            r"([a-z][a-z -]{0,50}?(?:console|tabletop|table|counter|nightstand|"
            r"desk|shelf|surface))\b",
            text,
            flags=re.IGNORECASE,
        )
        if surface_match:
            placement_surface = re.sub(
                r"\s+", " ", surface_match.group(1)
            ).strip()
            placement_surface = re.sub(
                r"^(?:the|a|an)\s+", "", placement_surface, flags=re.IGNORECASE
            )
    if placement_surface:
        # Replacing the package noun alone can leave an impossible instruction
        # such as "sets a clear empty placement area on the console". Compile
        # that package action into the static proof the downstream compositor
        # actually needs: the named surface exists and is unobstructed.
        text = re.sub(
            r"\b(?:(?:has|had)\s+)?(?:sets?|places?|placed|set)\s+"
            r"(?:(?:the|a|an)\s+)?clear empty placement area\s+"
            r"(?:on|onto|at)\s+(?:(?:the|a|an)\s+)?"
            + re.escape(placement_surface)
            + r"\b",
            f"leaves the {placement_surface} bare",
            text,
            flags=re.IGNORECASE,
        )
    sentences = re.split(r"(?<=[.!?])\s+|\s+-\s+|\n+", text)
    parts: list[str] = []
    for sentence in sentences:
        if not _IMAGE_PROMPT_PRODUCT_TRIGGER_RE.search(sentence):
            parts.append(sentence)
            continue
        coordinated = re.split(
            r"\s*;\s*|\s*,\s*then\s+(?=[A-Z])",
            sentence,
            flags=re.IGNORECASE,
        )
        for clause in coordinated:
            parts.extend(re.split(
                r"\s*,?\s+\band\b\s+(?=(?:(?:has|is)\s+)?(?:sets?|set|places?|placed|"
                r"holds?|opens?|shows?|adds?|displays?|rests?)\b)",
                clause,
                flags=re.IGNORECASE,
            ))
    safe = [
        re.sub(r"\s+", " ", part).strip(" -")
        for part in parts
        if str(part or "").strip()
        and not _IMAGE_PROMPT_PRODUCT_TRIGGER_RE.search(str(part))
    ]
    normalized_safe = [
        part[:1].upper() + part[1:]
        for part in safe
        if part
    ]
    result = ". ".join(part.rstrip(" .") for part in normalized_safe).strip()
    placement_contract = (
        f"{placement_surface} is visibly present with a clear, empty, "
        "unobstructed placement area"
        if placement_surface
        else ""
    )
    if placement_contract and placement_contract.lower() not in result.lower():
        result = (
            result.rstrip(" .")
            + f". The {placement_contract}."
        ).lstrip(". ")
    return result


def _static_image_style_requirement(value: Any) -> str:
    """Keep illustration identity while removing video-directing language."""
    text = _text(value, 1800)
    text = re.sub(r"\bcinematic\b", "editorial", text, flags=re.IGNORECASE)
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if re.search(
            r"\b(?:first\s+\d+\s+seconds?|shots?|provider\s+segments?|"
            r"segment\s+\d+|camera|tracking|dolly|pan|zoom|whip[- ]?pan|"
            r"child|children|kid|kids|teen|minor)\b",
            sentence,
            flags=re.IGNORECASE,
        ):
            continue
        cleaned = re.sub(r"\s+", " ", sentence).strip()
        if cleaned:
            kept.append(cleaned)
    return _text(" ".join(kept), 1000)


def _static_visual_continuity_lock(value: Any, *, kind: str) -> str:
    """Extract visible cast/location facts and discard timeline-only rules."""
    rows: list[str] = []
    if isinstance(value, dict):
        candidates = list(value.values())
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = [value]
    for candidate in candidates:
        if isinstance(candidate, dict):
            text = ", ".join(
                f"{key}: {item}"
                for key, item in candidate.items()
                if str(item or "").strip()
            )
        else:
            text = str(candidate or "").strip()
        if not text:
            continue
        if re.search(
            r"\b(?:camera|tracking|dolly|pan|zoom|shot|segments?|narrator|"
            r"voice|spoken|product|package|bottle|gumm(?:y|ies))\b",
            text,
            flags=re.IGNORECASE,
        ):
            continue
        rows.append(re.sub(r"\s+", " ", text).strip())
    prefix = (
        "AUTHORITATIVE CAST APPEARANCE: "
        if kind == "character"
        else "AUTHORITATIVE LOCATION APPEARANCE: "
    )
    return _text(
        prefix + " ".join(rows[:8]),
        1200 if kind == "character" else 900,
    ) if rows else ""


def _single_reference_repair_instruction(
    repair: str,
    *,
    reference_index: int,
) -> str:
    """Keep only this native image's targeted pixel-grounded correction.

    Whole-board review language must not be copied into every individual
    request because it can make the image model draw a grid.  Conversely,
    dropping the repair text entirely turns a targeted repair into an
    unchanged stochastic resample.  Server-generated targeted briefs identify
    each failed frame as ``Reference N:``; extract only the current frame's
    clause and discard the set-level wording.
    """
    clean = _text(repair, 4000)
    if not clean or reference_index <= 0:
        return ""
    marker = re.compile(
        rf"(?:\bReference\s+{int(reference_index)}\s*:\s*|"
        rf"\bRegenerate\s+only\s+reference\s+{int(reference_index)}\s*[.:;-]?\s*)",
        flags=re.IGNORECASE,
    )
    match = marker.search(clean)
    if match is None:
        return ""
    tail = clean[match.end():]
    boundary = re.search(
        r"\s+\bReference\s+\d+\s*:|\s+Preserve all passed references\b",
        tail,
        flags=re.IGNORECASE,
    )
    if boundary is not None:
        tail = tail[:boundary.start()]
    correction = _text(tail, 620)
    if not correction:
        return ""
    return _text(
        f"Correct only reference {int(reference_index)}: {correction} "
        "Keep one continuous native target-aspect scene; do not create a board, grid, collage, or split screen.",
        700,
    )


def _build_visual_api_prompt_for_spec(packet: dict[str, Any], spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    aspect_ratio_value, packet_aspect_ratio_label = _packet_aspect_ratio(packet)
    aspect_ratio_label = str(
        spec.get("aspect_ratio") or packet_aspect_ratio_label
    )
    aspect_orientation = (
        "vertical"
        if aspect_ratio_value < 0.98
        else "horizontal"
        if aspect_ratio_value > 1.02
        else "square"
    )
    product_allowed = bool(packet.get("product_required", True))
    include_product = product_allowed and any(
        bool(item.get("requires_product_reference"))
        for item in list(spec["plan"])
    )
    repair = _text(packet.get("visual_repair_instruction") or "", 700)
    visual_direction = _text(packet.get("user_instruction") or "", 500)
    visual_style_requirement = _text(packet.get("visual_style_requirement") or "", 1200)
    creative = _json_dict(_json_dict(packet.get("previous_outputs")).get("MEDIA_DESIGN"))
    visual_ticket = _json_dict(creative.get("visual_job_ticket"))
    continuity_rules = _json_dict(creative.get("continuity_rules"))
    cast_lock = _static_visual_continuity_lock(
        continuity_rules.get("character_continuity")
        or continuity_rules.get("character_lock")
        or continuity_rules.get("characters")
        or continuity_rules.get("character_rules"),
        kind="character",
    )
    location_lock = _static_visual_continuity_lock(
        continuity_rules.get("location_continuity")
        or continuity_rules.get("location_lock")
        or continuity_rules.get("locations")
        or continuity_rules.get("scene_continuity"),
        kind="location",
    )
    # The project-level visual direction is authoritative even when this
    # particular repair stage has no user_instruction.  Looking only at the
    # latter silently dropped the requested American adult animation medium
    # and the image API returned photorealistic people.
    medium_context = "\n".join(
        str(value or "")
        for value in (
            visual_direction,
            visual_style_requirement,
            packet.get("project_requirements"),
            visual_ticket.get("visual_style"),
        )
    )
    illustration_requested = bool(re.search(
        r"\b(?:2d|2\.5d|animated|animation|illustrat(?:ed|ion)|cartoon|graphic novel)\b|"
        r"动画|插画|卡通|美式动画",
        medium_context,
        flags=re.IGNORECASE,
    ))
    medium_rule = (
        "MANDATORY VISUAL MEDIUM: American adult 2D/2.5D editorial illustration. Render visibly illustrated "
        "characters, environments, light, and props; this is not live action and not a photograph. Do not use "
        "photorealistic skin, photographic lighting, real-person portraits, or a photographic scene. "
        if illustration_requested
        else ""
    )
    copy_contract = _json_dict(packet.get("creative_copy_contract"))
    product_prompt_terms = list(dict.fromkeys(
        str(item).strip()
        for item in (
            list(copy_contract.get("pre_reveal_forbidden_terms") or [])
            + [copy_contract.get("product_identity")]
        )
        if str(item or "").strip()
    ))
    single_frame = int(spec["count"]) == 1
    if single_frame:
        # Review repair briefs are written for the whole preview set and often
        # ask for "one 4-panel board". Repeating that sentence inside each
        # native-image request makes the image model draw four panels per file.
        # Keep only a server-generated, index-addressed correction for this
        # exact reference. The current reference-plan row owns the base scene.
        repair = _single_reference_repair_instruction(
            repair,
            reference_index=int(spec.get("global_start_index") or 0),
        )
    if single_frame:
        # Keep shared project prose scene/style-only. The current plan row and
        # attached product identity reference own product presence, preventing
        # a shared sentence from leaking the product into earlier scenes.
        visual_direction = _image_prompt_without_product_triggers(
            visual_direction,
            product_terms=product_prompt_terms,
        )
        visual_style_requirement = _image_prompt_without_product_triggers(
            _static_image_style_requirement(visual_style_requirement),
            product_terms=product_prompt_terms,
        )
        repair = _image_prompt_without_product_triggers(
            repair,
            product_terms=product_prompt_terms,
        )
    input_names = []
    for index, (asset, _path) in enumerate(
        _visual_generation_reference_entries(
            packet,
            plan=list(spec["plan"]),
            include_product=True,
        ),
        1,
    ):
        role = str(asset.get("role") or asset.get("asset_role") or asset.get("kind") or "").strip().lower()
        input_names.append(
            f"input {index}: {_text(asset.get('name') or asset.get('filename') or role, 100)} [{role}]"
        )
    panel_lines = []
    for item in list(spec["plan"]):
        index = int(item.get("index") or len(panel_lines) + 1)
        roles = ", ".join(str(value) for value in list(item.get("roles") or [])[:3])
        # _reference_plan already normalizes product policy and compiles the
        # creative beat into one still moment. Do not run that normalization a
        # second time or duplicate/truncate its product contract.
        description = _text(
            item.get("single_frame_terminal_state")
            or item.get("description")
            or "",
            520,
        )
        frame_label = "ONE STATIC ILLUSTRATED IMAGE" if illustration_requested else "ONE STATIC STILL IMAGE"
        panel_lines.append(
            f"{index}. {frame_label}: {description}"
            + (f" [{roles}]" if roles else "")
        )
    presentation_policy = _json_dict(packet.get("product_presentation_policy"))
    product_rule = (
        "Show the product only in a panel whose shot explicitly requires product interaction. In that case use the uploaded "
        "product reference as the authoritative identity for package form, cap, dominant colors, brand, and primary label, and "
        "render it naturally with the scene lighting, perspective, contact, and occlusion. Do not paste the uploaded image or its "
        "white background into the scene. Never invent, redesign, materially deform, recolor, or replace it. Do not create a standalone "
        "product-only panel, white-background packshot, label study, sales card, or placeholder. Obey this saved product "
        "presentation policy exactly: "
        + json.dumps(
            presentation_policy,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "."
        if include_product
        else
        "This entire board is product-free. Do not show or invent any advertised product, package, label, logo, branding, "
        "packshot, sales card, or product placeholder in any panel."
    )
    if single_frame and not include_product:
        product_rule = (
            "Render only the people, furniture, clothing, props, and room details named in this scene. "
            "Do not add sales graphics, readable words, labels, logos, or extra merchandise."
        )
    canvas_shape = {
        "1024x1024": "a square 1:1 board",
        "1024x1792": "a portrait board",
        "1792x1024": "a landscape board",
    }.get(str(spec["size"]), "the requested board shape")
    if single_frame:
        canvas_shape = f"one native {aspect_ratio_label} image"
    outer_canvas_rule = (
        f"Render one complete native {aspect_ratio_label} reference frame with one continuous scene."
        if single_frame
        else (
            "Keep the outer portrait canvas as a multi-panel storyboard board; do not turn it into one single full-frame scene."
            if str(spec["size"]) == "1024x1792"
            else "Never make the whole canvas a single scene."
        )
    )
    layout_rule = "No gutters, dividers, frames, or borders are needed. " if single_frame else (
        f"{_layout_instruction(spec['count'], spec['columns'], spec['rows'], panel_orientation=aspect_orientation)} Read panels left-to-right, top-to-bottom. "
        f"Use equal {aspect_orientation} {aspect_ratio_label} cells and clean white gutters. "
    )
    frame_intro = (
        f"Generate one {aspect_ratio_label} reference image for scene {spec['global_start_index']}. "
        if single_frame
        else f"Generate storyboard board {spec['board_index']} of {spec['board_count']} with exactly {spec['count']} separate {aspect_orientation} {aspect_ratio_label} panels. "
    )
    coverage_rule = (
        f"This file represents scene {spec['global_start_index']} only. "
        if single_frame
        else f"This board covers global reference panels {spec['global_start_index']} through {spec['global_end_index']} only. "
    )
    composition_rule = (
        "This is one complete still image with one continuous static composition. Do not create subframes, borders, "
        "before/after views, a collage, grid, split screen, montage, diagram, contact sheet, or multi-frame page. "
        "Keep the established cast, faces, hair, wardrobe, room, lighting, and viewpoint consistent with the other scene files. "
        if single_frame
        else
        "Each numbered image is one complete static composition with no subpanels, before/after views, montage, diagram, "
        "or content crossing a gutter. Keep the same cast, faces, hair, wardrobe, room, lighting, and viewpoint across the board. "
    )
    keyframe_rule = (
        "SINGLE-FRAME KEYFRAME RULE: this is a still image, not a timeline. If the scene description contains "
        "several actions, then/after/finally wording, or an approach followed by an interaction, depict ONLY the "
        "last decisive state after every described action has happened. Do not depict an earlier or intermediate "
        "action. The final character placement, interaction, prop state, and emotional beat are authoritative. "
        if single_frame
        else ""
    )
    scene_heading = "SCENE REQUIREMENT:\n" if single_frame else "ORDERED PANEL SHOTS:\n"
    scene_lines = (
        "\n".join(line.split(": ", 1)[1] if ": " in line else line for line in panel_lines)
        if single_frame
        else "\n".join(panel_lines)
    )
    prompt = (
        frame_intro
        + coverage_rule
        + f"The ENTIRE output canvas must be {canvas_shape} at {spec['size']}. {outer_canvas_rule} "
        + layout_rule
        + composition_rule
        + keyframe_rule
        + "Keep faces, hands, and actions fully inside the image. "
        + medium_rule
        + product_rule
        + (
            " No visible text, captions, numbers, UI, watermark, overlays, or picture-in-picture. "
            if single_frame
            else " No panel numbers, text, captions, nested frames, UI, watermark, or picture-in-picture. "
        )
        + "Uploaded character images guide identity only; never paste them.\n"
        + ("PROJECT VISUAL DIRECTION: " + visual_direction + "\n" if visual_direction else "")
        + ("PROJECT VISUAL STYLE CONTRACT: " + visual_style_requirement + "\n" if visual_style_requirement else "")
        + (cast_lock + " Do not change age, skin tone, hair, wardrobe, or body build between scene files.\n" if cast_lock else "")
        + (location_lock + " Keep the named architecture, furniture, palette, and lighting identity consistent.\n" if location_lock else "")
        + ("MANDATORY REPAIR OVERRIDE: " + repair + "\n" if repair else "")
        + scene_heading
        + scene_lines
        + ("\nINPUT REFERENCES:\n" + "\n".join(input_names) if input_names else "")
    )
    return prompt, spec


def build_visual_api_prompts(packet: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        _build_visual_api_prompt_for_spec(packet, spec)
        for spec in visual_board_specs(packet)
    ]


def build_visual_api_prompt(packet: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Compatibility wrapper for callers/tests that use a single board."""
    return build_visual_api_prompts(packet)[0]


__all__ = [
    "ContentFactoryApiError",
    "TEXT_API_STAGES",
    "benchmark_imitation_mode",
    "build_text_api_request",
    "build_visual_api_prompt",
    "build_visual_api_prompts",
    "execute_text_stage_api",
    "minimal_stage_context",
    "visual_reference_description",
    "visual_reference_static_state",
    "visual_generation_reference_paths",
    "visual_board_spec",
    "visual_board_specs",
    "visual_reference_requires_product",
]
