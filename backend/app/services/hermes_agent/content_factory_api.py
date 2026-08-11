from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.ai_routing.router import AiGatewayError, call_chat_with_failover
from app.services.hermes_agent.client import extract_output_text
from app.services.hermes_agent.storyboard_split import expected_row_columns
from sqlalchemy.orm import Session

from app.services.ai_video.accounts import (
    TOAPIS_PROVIDER_KEY,
    decrypt_api_key,
    get_effective_key,
)


TEXT_API_STAGES = {"FACTS", "CREATIVE_REVIEW", "EDIT_PACKAGE"}
CONTENT_FACTORY_CONTEXT_COMPILER_VERSION = 3
PRODUCT_REFERENCE_VIDEO_REVIEW_POLICY_VERSION = (
    "2026-08-08-use-state-aware-primary-identity-v3"
)
SEGMENT_EXECUTION_VIDEO_REVIEW_POLICY_VERSION = (
    "2026-08-01-audiovisual-delivery-surface-aware-v8"
)
SEGMENT_EXECUTION_REPLAN_POLICY_VERSION = (
    "2026-08-08-multi-line-dialogue-key-v10"
)
FINAL_INTENT_REVIEW_POLICY_VERSION = (
    "2026-08-05-segment-evidence-continuity-v4"
)
FINAL_INTENT_REPAIR_SCOPE_POLICY_VERSION = (
    "2026-08-05-history-aware-repair-scope-v1"
)
FINAL_INTENT_REPAIR_TARGET_POLICY_VERSION = (
    "2026-08-09-evidence-vs-regeneration-target-v1"
)
BENCHMARK_VISUAL_ANALYSIS_POLICY_VERSION = (
    "2026-07-29-source-storyboard-multimodal-v1"
)
SPOKEN_COPY_SEMANTIC_REVIEW_POLICY_VERSION = (
    "2026-07-31-multimodal-asr-evidence-v1"
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


def benchmark_imitation_mode(
    requirement: Any,
    *,
    has_benchmark: bool,
    transformation_contract: Any = None,
) -> str:
    """Read benchmark intent only from the assistant/director contract.

    Free-form requirement prose is deliberately not classified here.  Intent
    interpretation belongs to the multimodal intake/director roles; this
    helper only consumes their typed handoff and supplies the conservative
    adaptive default when an older packet has no structured fidelity field.
    """
    if not has_benchmark:
        return "none"
    if isinstance(transformation_contract, dict):
        fidelity = str(
            transformation_contract.get("fidelity") or ""
        ).strip().lower()
        if fidelity in {"exact", "exact_outside_authorized_changes"}:
            return "exact"
        if fidelity in {"adaptive", "inspiration"}:
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


def _normalized_reason_texts(value: Any) -> list[str]:
    """Normalize loosely structured model reasons into stable audit text.

    Multimodal providers occasionally return a reason object (for example
    ``{"reason": "...", "evidence": "..."}``) even when the requested JSON
    schema says ``array[string]``.  A reason is evidence, not a control-plane
    identifier, so preserve it as deterministic text instead of crashing while
    hashing or silently discarding the review.
    """

    values = list(value) if isinstance(value, (list, tuple, set)) else [value]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        if item in (None, "", [], {}):
            continue
        if isinstance(item, dict):
            preferred = next(
                (
                    str(item.get(key) or "").strip()
                    for key in (
                        "reason",
                        "message",
                        "description",
                        "issue",
                        "rationale",
                    )
                    if str(item.get(key) or "").strip()
                ),
                "",
            )
            text_value = preferred or json.dumps(
                _compact(item, max_items=16, max_text=1200),
                ensure_ascii=False,
                sort_keys=True,
            )
        elif isinstance(item, (list, tuple, set)):
            for nested in _normalized_reason_texts(item):
                if nested not in seen:
                    seen.add(nested)
                    normalized.append(nested)
            continue
        else:
            text_value = str(item).strip()
        if text_value and text_value not in seen:
            seen.add(text_value)
            normalized.append(text_value)
    return normalized


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
        common["producer_intent_authority"] = _compact(
            packet.get("producer_intent_authority") or {},
            max_items=64,
            max_text=1600,
        )
        # A compact creative summary is not enough for a visual acceptance
        # decision.  Keep the normalized, ordered still-frame contract
        # alongside the images so the reviewer must compare every rendered
        # file with the exact terminal state that it was meant to depict.
        common["reference_plan"] = _reference_plan(packet)
        # Raw benchmark semantics belong to Producer/Director interpretation,
        # not rendered-reference acceptance. The signed reference plan and
        # producer_intent_authority are the story authority at this stage.
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
        "type, wrong brand identity, materially wrong package silhouette, "
        "closure/cap silhouette, closure mechanism, cap color, dominant colors, "
        "or gross deformation. Block prominent invented words, logos, bands, "
        "seals, or graphics on the cap/closure or package when absent from the "
        "product_visual authority. Minor illegibility is acceptable only for "
        "genuinely tiny secondary label copy when the advertised product remains "
        "unmistakably the same. Missing, softened, or unreadable secondary "
        "tamper-seal wording printed on an otherwise matching closure is also a "
        "non-blocking generative detail; do not confuse that with an invented "
        "prominent mark or a different closure shape/mechanism. "
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
    video_policy = _json_dict(packet.get("video_model_policy"))
    face_reference_mode = str(
        video_policy.get("human_face_reference_mode") or "allowed"
    ).strip().lower()
    illustration_required = bool(re.search(
        r"\b(?:2d|2\.5d|animated|animation|illustrat(?:ed|ion)|cartoon|graphic novel)\b|"
        r"动画|插画|卡通|美式动画",
        visual_requirement_text,
        flags=re.IGNORECASE,
    )) or face_reference_mode == "stylized_animation_only"
    review_medium_rule = (
        "The selected video-provider medium requires unmistakably fictional adult animation references. Inspect the actual pixels: "
        "accept clearly drawn or rendered 2D/2.5D/3D animation faces, but reject photorealistic, hyperreal, live-action, photographic, "
        "or real-person-looking faces even when the story beats are otherwise correct. Skin, eyes, hair, light, and facial planes must "
        "look intentionally stylized rather than like a synthetic photograph. The product package may remain materially faithful to its "
        "uploaded authority. If rejected, identify the exact reference indices whose human faces are too realistic and require a visibly "
        "animated redraw without changing the approved character, scene, action, hook, or product timing. "
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
                "mismatch, uncertain, or not_required. observed_facts and missing_or_wrong_facts must each be JSON arrays "
                "for every row; use [] when there are no facts, never prose strings or omitted rows. Return all rows even "
                "when a reference fails. Describe only pixels visibly present in that one still image. Do not "
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
                "reference is required, placement_surface_verdict judges coherent physical integration: a product resting "
                "on furniture must have plausible contact, while a scripted hand-held product is supported by the visible "
                "hand/grip and does not also need a table surface. In both cases judge perspective, lighting, and occlusion; "
                "reject a floating, pasted, or white-background package. A generic surrounding surface or implied off-screen "
                "support is not evidence. approved_for_split may be true only when every required verdict is match and every "
                "missing_or_wrong_facts list is empty. "
                "COPY-LANE BOUNDARY: MEDIA_DESIGN.copy_delivery rows whose method is local_overlay are rendered later by the "
                "deterministic local compositor and are intentionally absent from generated reference pixels. Never reject a "
                "reference because exact time, count, quantified consequence, headline, CTA, caption, letters, or numerals "
                "assigned to local_overlay are not baked into the image. Judge visible action, expression, composition, medium, "
                "continuity, and reserved placement space instead. If visual_job_ticket forbids generated text, clock numerals, "
                "screen copy, or captions, that instruction wins and must not be reversed during benchmark-hook review. "
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
                "observed_facts and missing_or_wrong_facts must each be JSON arrays for every row; use [] when empty, never "
                "prose strings or omitted rows. Return every ordered row even when a reference fails. "
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
            + review_product_rule
            + review_medium_rule
            + (
                "Image manifests identify generated visual_preview files, the "
                "separate product_visual authority, and the signed chronological "
                "reference_plan. Count and row-check only generated visual_preview "
                "references. producer_intent_authority is the user-confirmed "
                "boundary, while the signed reference_plan is the executable story "
                "authority for this rendered set. The project target_count is the number "
                "of independent completed videos requested for the whole project; it is "
                "not the number of reference images or rows required by this one variant. "
                "Never demand target_count references. The required count for this review "
                "is exactly len(reference_plan), and a complete ordered check set matching "
                "that signed count must not be rejected as incomplete merely because it is "
                "smaller than target_count. Enforce only visible facts that "
                "the reference_plan assigns to the current row and explicit "
                "observable_checks that apply to this deliverable. Do not recreate, "
                "restore, or infer an earlier source-video story, actor relationship, "
                "prop, interruption, transformation, or ordered state that is absent "
                "from the signed reference_plan. A must_not_reuse or "
                "excluded_source_artifacts item always forbids demanding that source "
                "mechanism. Never demand copied actors, props, setting, UI, captions, "
                "wording, watermarks, or imagery. A generic portrait may fail only "
                "when the signed reference_plan for that exact row requires a more "
                "specific visible action, state, abnormality, or contrast. "
            )
            + "If rejected, give one actionable regeneration instruction and put a complete repair_strategy object inside result "
            "(not only at the envelope top level). "
            "repair_strategy.mode must be regenerate_full_board or regenerate_references; reference_indices must list the exact "
            "ordered references to redraw; continuity_anchor_indices must list only accepted references that should be attached as "
            "real pixel anchors; reason must explain the choice. Choose regenerate_full_board for a whole-set problem such as visual "
            "medium, cast identity, location/style continuity, opening-hook sequence, chronology, panel topology/count, or a defect whose "
            "repair changes multiple rows. Choose regenerate_references only for genuinely local defects when the remaining files are "
            "already mutually consistent and the named continuity anchors can preserve that consistency. Never say preserve or match a "
            "reference unless its index is present in continuity_anchor_indices."
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


def _creative_review_data_url(path: str) -> str:
    """Build a bounded review proxy without modifying the authoritative file.

    CREATIVE_REVIEW used to inline as many as twelve full PNG references.  A
    six-frame 9:16 board can therefore exceed ten megabytes before JSON
    overhead and repeatedly time out otherwise healthy multimodal routes.  The
    reviewer needs the complete composition and visible expressions, not the
    lossless generation payload.  Resize in memory and retain ``detail=high``
    so pixel-grounded inspection remains useful while transport and vision
    token costs stay bounded.
    """

    max_edge = max(
        640,
        min(
            1280,
            int(os.getenv("HERMES_CREATIVE_REVIEW_MAX_LONG_EDGE", "960")),
        ),
    )
    quality = max(
        72,
        min(92, int(os.getenv("HERMES_CREATIVE_REVIEW_JPEG_QUALITY", "84"))),
    )
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                canvas.alpha_composite(rgba)
                image = canvas.convert("RGB")
            else:
                image = image.convert("RGB")
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ContentFactoryApiError(
            "CREATIVE_REVIEW_IMAGE_PROXY_FAILED"
        ) from exc
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _routed_multimodal_completion(
    db: Session,
    *,
    payload: dict[str, Any],
    request_id: str,
    logical_model: str | None = None,
    workload: str | None = None,
    capability: str = "multimodal",
    source: str = "content_visual_review",
    error_prefix: str = "CONTENT_VISUAL_REVIEW",
) -> dict[str, Any]:
    """Run a content role through the shared, circuit-aware route layer.

    Semantic content roles use multimodal-capable routes even when a particular
    repair packet contains text only. This keeps authoring and adjudication on
    the same model class; the presence of media changes the evidence, not the
    authority.
    """

    resolved_model = str(
        logical_model
        or os.getenv("HERMES_CONTENT_VISUAL_INSPECTOR_MODEL")
        # Temporary environment compatibility; the old name is not emitted
        # into new task or audit metadata.
        or os.getenv("HERMES_PRODUCT_COMPOSITE_MODEL")
        or ""
    ).strip()
    resolved_workload = str(
        workload
        or os.getenv("HERMES_CONTENT_VISUAL_INSPECTOR_WORKLOAD")
        or os.getenv("HERMES_PRODUCT_COMPOSITE_WORKLOAD")
        or ""
    ).strip().lower()
    if not resolved_model:
        # Media analysis runs from several worker classes.  Resolve the
        # operator-managed semantic role from the database instead of relying
        # on a service-specific environment file being copied to every worker.
        from app.services.ai_routing.role_groups import managed_role_groups

        visual_group = next(
            (
                group
                for group in managed_role_groups(db)
                if str(group.get("role") or "") == "visual_inspector"
                and str(group.get("capability") or "") == "multimodal"
            ),
            None,
        )
        if visual_group is not None:
            resolved_model = str(
                visual_group.get("logical_model_id") or ""
            ).strip()
            if not resolved_workload:
                resolved_workload = str(
                    visual_group.get("workload") or ""
                ).strip().lower()
    resolved_capability = str(capability or "multimodal").strip().lower()
    if resolved_capability != "multimodal":
        raise ContentFactoryApiError(
            "MULTIMODAL_ROUTING_CAPABILITY_INVALID"
        )
    prefix = str(error_prefix or "MULTIMODAL").strip().upper()
    if not resolved_model:
        raise ContentFactoryApiError(
            f"{prefix}_ROUTING_MODEL_NOT_CONFIGURED"
        )
    if not resolved_workload:
        raise ContentFactoryApiError(
            f"{prefix}_ROUTING_WORKLOAD_NOT_CONFIGURED"
        )
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
                capability=resolved_capability,
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


def analyze_benchmark_storyboard_api(
    db: Session,
    *,
    contact_sheet_paths: list[str],
    transcript: str,
    project_requirement: str,
    transformation_contract: dict[str, Any] | None,
    execution_id: str,
) -> dict[str, Any]:
    """Convert ordered source-video pixels into an abstract production brief.

    Whisper text and timestamped frame paths are not visual understanding.  The
    Director needs a pixel-grounded description of *why* the opening arrests
    attention, how visual states escalate, where the product enters, and which
    source-specific pixels must not be copied.  This call runs before any
    Director or image spend and therefore fails closed when no multimodal route
    can produce a valid analysis.
    """

    sheets = [
        Path(str(path or ""))
        for path in list(contact_sheet_paths or [])[:12]
    ]
    if not sheets or any(
        not path.is_file() or path.stat().st_size <= 1024
        for path in sheets
    ):
        raise ContentFactoryApiError(
            "BENCHMARK_VISUAL_ANALYSIS_CONTACT_SHEETS_MISSING"
        )
    bounded_contract = dict(transformation_contract or {})
    system = (
        "You are a senior short-form video analyst. The attached images are "
        "chronological contact sheets sampled from one user-supplied source "
        "video. Inspect the actual pixels in tile order. Extract the abstract "
        "attention mechanics and production grammar that a new AI-generated "
        "video must transfer without copying source actors, exact props, "
        "setting, wording, platform UI, captions, watermarks, logos, or "
        "signature source imagery unless the transformation contract explicitly "
        "authorizes exact reconstruction. A visual hook is a sequence, not a "
        "topic label: identify its initial abnormality, contrast, scale change, "
        "expression, information density, camera/framing changes, and escalation "
        "inside the first three seconds. Return strict JSON only."
    )
    analysis_request = {
        "project_requirement": _text(project_requirement, 4000),
        "transformation_contract": bounded_contract,
        "transcript": str(transcript or "")[:12000],
        "sheet_count": len(sheets),
        "requested_output": {
            "opening_hook": {
                "time_window_seconds": "[start,end]",
                "visual_premise": "string",
                "ordered_states": [
                    {
                        "state_index": 1,
                        "source_sheet_index": 1,
                        "source_tile_range": "string",
                        "visible_state": "string",
                        "attention_job": "string",
                    }
                ],
                "attention_mechanisms": ["string"],
                "contrast_and_escalation": "string",
                "minimum_distinct_visual_states": 2,
                "recommended_opening_reference_count": 2,
                "failure_if_flattened_to": ["string"],
            },
            "story_progression": [
                {
                    "order": 1,
                    "time_window_seconds": "[start,end]",
                    "narrative_job": "string",
                    "visual_change": "string",
                    "pacing": "string",
                }
            ],
            "product_entry": {
                "first_visible_time_seconds": "number|null",
                "transition_job": "string",
                "presentation_pattern": "string",
            },
            "must_transfer": ["abstract mechanism"],
            "must_not_copy": ["source-specific pixel or device"],
            "storyboard_guidance": {
                "recommended_total_reference_count": 4,
                "opening_reference_roles": ["string"],
                "continuity_anchors": ["string"],
            },
            "confidence": 0.0,
        },
    }
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(
                analysis_request,
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    for index, sheet in enumerate(sheets, 1):
        content.extend([
            {
                "type": "text",
                "text": f"Chronological source contact sheet {index}/{len(sheets)}:",
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": _creative_review_data_url(str(sheet)),
                    "detail": "high",
                },
            },
        ])
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
        "max_tokens": 2400,
        "response_format": {"type": "json_object"},
    }
    digest = hashlib.sha256(
        (
            f"{BENCHMARK_VISUAL_ANALYSIS_POLICY_VERSION}|{execution_id}|"
            + "|".join(
                hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sheets
            )
            + "|"
            + hashlib.sha256(
                json.dumps(
                    bounded_contract,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        ).encode("utf-8")
    ).hexdigest()[:28]
    semantic_errors: list[str] = []
    result: dict[str, Any] | None = None
    for attempt in range(3):
        body = _routed_multimodal_completion(
            db,
            payload=payload,
            request_id=f"cf-benchmark-vision:{digest}:s{attempt + 1}",
            source="content_benchmark_visual_analysis",
            error_prefix="BENCHMARK_VISUAL_ANALYSIS",
        )
        raw = extract_output_text(body).strip()
        if not raw:
            semantic_errors.append("empty assistant output")
            continue
        try:
            candidate = _json_object_from_text(
                raw,
                error_prefix="Benchmark visual analysis",
            )
        except (ContentFactoryApiError, ValueError) as exc:
            semantic_errors.append(str(exc)[:240])
            continue
        opening = dict(candidate.get("opening_hook") or {})
        states = [
            dict(item)
            for item in list(opening.get("ordered_states") or [])
            if isinstance(item, dict)
        ]
        progression = [
            dict(item)
            for item in list(candidate.get("story_progression") or [])
            if isinstance(item, dict)
        ]
        try:
            opening_reference_count = int(
                opening.get("recommended_opening_reference_count") or 0
            )
            distinct_state_count = int(
                opening.get("minimum_distinct_visual_states") or 0
            )
        except (TypeError, ValueError):
            opening_reference_count = 0
            distinct_state_count = 0
        if (
            not states
            or not progression
            or opening_reference_count < 1
            or distinct_state_count < 1
            or not list(candidate.get("must_transfer") or [])
        ):
            semantic_errors.append(
                "missing ordered opening states, progression, or transfer mechanics"
            )
            continue
        result = candidate
        break
    if result is None:
        raise ContentFactoryApiError(
            "BENCHMARK_VISUAL_ANALYSIS_SEMANTIC_RETRY_EXHAUSTED: "
            + "; ".join(semantic_errors[-3:])
        )
    return {
        **result,
        "status": "success",
        "policy_version": BENCHMARK_VISUAL_ANALYSIS_POLICY_VERSION,
        "source_sheet_count": len(sheets),
    }






def review_provider_rendered_product_video_api(
    db: Session,
    *,
    contact_sheet_path: str,
    product_reference_path: str,
    execution_id: str,
    segment_context: dict[str, Any] | None = None,
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
    signed_segment_context = dict(segment_context or {})
    context_json = json.dumps(
        signed_segment_context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )[:12000]
    context_sha256 = hashlib.sha256(
        context_json.encode("utf-8")
    ).hexdigest()
    system = (
        "You are an independent product-in-video quality inspector. The first "
        "image is an ordered contact sheet sampled from one short video segment. "
        "The second image is the authoritative uploaded product package. Judge "
        "only visible pixels. Return strict JSON. First separate PRIMARY PRODUCT "
        "IDENTITY from SECONDARY LABEL DETAIL. Primary identity means the brand "
        "name, product name, package type and silhouette, closure components and "
        "mechanism in the product's applicable state, and "
        "dominant color system. Secondary detail means supporting copy, fine print, "
        "net-weight text, tiny seals, and small decorative wording. Minor generative "
        "imperfections in secondary detail are acceptable even when that small copy "
        "is visible: it may be partly illegible, garbled, missing, or slightly "
        "different, provided it does not become a large competing brand, product "
        "name, claim, logo, band, seal, or graphic and the primary identity still "
        "matches. Subtle frame-to-frame shimmer may also occur. The signed current-"
        "segment context is supplied as text. Use it only to understand an intended "
        "product state or action such as picking up, opening, removing a lid, "
        "sampling, applying, closing, or placing the package. An authoritative "
        "closed packshot must not make an explicitly scripted open state fail by "
        "itself. Conversely, an intended state transition never waives primary "
        "identity: the package body, brand/product identity, dominant colors, and "
        "visible closure components must remain the same physical product. Do not "
        "invent an incompatible neck, dispenser, jar, bottle, tube, cap, lid, ring, "
        "or band. If a legitimately removed lid is off-frame, do not fail merely "
        "because that lid is not visible. Blocking "
        "defects are: product missing from the intended product segment; clearly "
        "wrong product type or brand; materially wrong package silhouette, "
        "closure shape/mechanism, cap color, dominant colors, or "
        "primary identity; a large or visually dominant invented brand, product "
        "name, claim, logo, band, seal, or graphic; gross "
        "melting/deformation; duplicate "
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
        "allowed minor-drift tolerance. Here is the signed CURRENT SEGMENT context "
        "for use-state interpretation (it is not visual evidence and cannot waive "
        "identity defects): " + (context_json or "{}")
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
            f"{hashlib.sha256(product.read_bytes()).hexdigest()}|"
            f"{context_sha256}"
        ).encode("utf-8")
    ).hexdigest()[:28]
    result: dict[str, Any] | None = None
    semantic_errors: list[str] = []
    # Aggregated OpenAI-compatible routes can occasionally return HTTP 200
    # with a Responses-style envelope, an empty assistant message, or malformed
    # JSON.  This is neither a product failure nor permission to skip QA.  Use
    # a fresh idempotency identity for a small semantic retry budget while the
    # route layer independently cycles enabled providers by priority.
    max_semantic_attempts = max(
        1,
        min(
            4,
            int(os.getenv("HERMES_PRODUCT_VIDEO_REVIEW_SEMANTIC_RETRIES", "3")),
        ),
    )
    for semantic_attempt in range(max_semantic_attempts):
        body = _routed_multimodal_completion(
            db,
            payload=payload,
            request_id=(
                f"cf-product-video:{digest}:s{semantic_attempt + 1}"
            ),
            source="content_product_video_review",
            error_prefix="PRODUCT_VIDEO_REVIEW",
        )
        raw = extract_output_text(body).strip()
        if not raw:
            semantic_errors.append("empty assistant output")
            continue
        try:
            result = _json_object_from_text(
                raw,
                error_prefix="Product video review",
            )
            break
        except (ContentFactoryApiError, ValueError) as exc:
            semantic_errors.append(str(exc)[:240])
    if result is None:
        raise ContentFactoryApiError(
            "PRODUCT_VIDEO_REVIEW_SEMANTIC_RETRY_EXHAUSTED: "
            + "; ".join(semantic_errors[-max_semantic_attempts:])
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


def review_spoken_copy_semantics_api(
    db: Session,
    *,
    contact_sheet_path: str,
    expected_text: str,
    primary_transcript: str,
    adjudicated_transcript: str | None,
    transcript_diagnostics: dict[str, Any],
    execution_id: str,
) -> dict[str, Any]:
    """Let the multimodal inspector adjudicate ASR evidence semantically.

    Whisper is evidence collection, not a release authority.  Token counters
    routinely confuse brand names, times, homophones and singular/plural
    endings.  The visual inspector receives the real clip contact sheet, the
    signed copy and independent ASR candidates, then decides whether the
    spoken meaning materially changed.  Local code validates only the JSON
    envelope; it never promotes a missing-token list into a creative veto.
    """

    sheet = Path(str(contact_sheet_path or ""))
    if not sheet.is_file() or sheet.stat().st_size <= 1024:
        raise ContentFactoryApiError(
            "SPOKEN_COPY_REVIEW_CONTACT_SHEET_MISSING"
        )
    signed_copy = str(expected_text or "").strip()
    primary = str(primary_transcript or "").strip()
    adjudicated = str(adjudicated_transcript or "").strip()
    if not signed_copy or not primary:
        raise ContentFactoryApiError("SPOKEN_COPY_REVIEW_EVIDENCE_MISSING")

    system = (
        "You are an independent multimodal short-video spoken-copy reviewer. "
        "The image is an ordered contact sheet from the actual rendered clip. "
        "You cannot listen to the audio directly, so judge the signed copy "
        "against the two independent ASR transcripts and their diagnostics. "
        "ASR is noisy evidence, never an automatic veto. Do not fail harmless "
        "punctuation, capitalization, hyphenation, number formatting, likely "
        "homophones, brand-name phonetic spellings, or singular/plural ASR "
        "drift when the intended meaning remains clear. Block only when the "
        "evidence supports a real omission, substitution, or addition that "
        "materially changes the story, product identity, factual claim, "
        "quantity, price, CTA, or compliance meaning. If transcripts disagree "
        "and the difference is plausibly an ASR error, do not invent a failure. "
        "Return strict JSON only."
    )
    evidence = {
        "signed_copy": signed_copy,
        "primary_asr_transcript": primary,
        "adjudicated_asr_transcript": adjudicated or None,
        "diagnostics": dict(transcript_diagnostics or {}),
    }
    user = (
        "Review spoken-copy semantic fidelity. Return keys: status ('pass' or "
        "'fail'), semantic_fidelity ('exact', 'meaning_preserved', "
        "'material_change', or 'insufficient_evidence'), likely_asr_error "
        "(boolean), material_differences (array), observed_evidence (array), "
        "confidence (0..1), blocking_reasons (array), and repair_instruction "
        "(string or null). A fail requires semantic_fidelity='material_change' "
        "and concrete evidence in blocking_reasons. Evidence:\n"
        + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
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
                        "image_url": {
                            "url": _data_url(str(sheet)),
                            "detail": "high",
                        },
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
            f"{SPOKEN_COPY_SEMANTIC_REVIEW_POLICY_VERSION}|{execution_id}|"
            f"{hashlib.sha256(sheet.read_bytes()).hexdigest()}|"
            + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        ).encode("utf-8")
    ).hexdigest()[:28]
    result: dict[str, Any] | None = None
    semantic_errors: list[str] = []
    for semantic_attempt in range(3):
        body = _routed_multimodal_completion(
            db,
            payload=payload,
            request_id=f"cf-spoken-copy:{digest}:s{semantic_attempt + 1}",
            source="content_spoken_copy_review",
            error_prefix="SPOKEN_COPY_REVIEW",
        )
        raw = extract_output_text(body).strip()
        if not raw:
            semantic_errors.append("empty assistant output")
            continue
        try:
            candidate = _json_object_from_text(
                raw,
                error_prefix="Spoken copy review",
            )
        except (ContentFactoryApiError, ValueError) as exc:
            semantic_errors.append(str(exc)[:240])
            continue
        status = str(candidate.get("status") or "").strip().lower()
        fidelity = str(
            candidate.get("semantic_fidelity") or ""
        ).strip().lower()
        reasons = [
            str(value).strip()
            for value in list(candidate.get("blocking_reasons") or [])
            if str(value).strip()
        ]
        if status not in {"pass", "fail"}:
            semantic_errors.append("invalid status")
            continue
        if fidelity not in {
            "exact",
            "meaning_preserved",
            "material_change",
            "insufficient_evidence",
        }:
            semantic_errors.append("invalid semantic_fidelity")
            continue
        if status == "fail" and (
            fidelity != "material_change" or not reasons
        ):
            semantic_errors.append(
                "fail lacked material-change evidence"
            )
            continue
        result = candidate
        break
    if result is None:
        raise ContentFactoryApiError(
            "SPOKEN_COPY_REVIEW_SEMANTIC_RETRY_EXHAUSTED: "
            + "; ".join(semantic_errors[-3:])
        )
    blocking = bool(
        str(result.get("status") or "").strip().lower() == "fail"
        and str(result.get("semantic_fidelity") or "").strip().lower()
        == "material_change"
        and list(result.get("blocking_reasons") or [])
    )
    return {
        **result,
        "status": "fail" if blocking else "pass",
        "blocking": blocking,
        "policy_version": SPOKEN_COPY_SEMANTIC_REVIEW_POLICY_VERSION,
        "transcript_evidence": evidence,
    }


def review_provider_rendered_segment_execution_api(
    db: Session,
    *,
    contact_sheet_path: str,
    segment_contract: dict[str, Any],
    execution_id: str,
    requirement_contract: list[dict[str, Any]],
    forbid_overlay_bands: bool,
) -> dict[str, Any]:
    """Judge provider pixels with user intent above the AI-authored plan.

    The benchmark transcript, frames, story, roles and props are deliberately
    absent from this request.  For inspiration-only work the source contributes
    attention density and conversion timing upstream.  The Director timeline
    is the preferred realization, while the scoped user requirements remain
    the release authority.  Equivalent original execution is therefore valid;
    a provider is blocked here only when it visibly misses a positive scoped
    requirement or introduces a forbidden artifact.  Whole-video actions are
    reviewed after composition instead of being promoted into a rigid local
    shot contract.
    """

    sheet = Path(str(contact_sheet_path or ""))
    if not sheet.is_file() or sheet.stat().st_size <= 1024:
        raise ContentFactoryApiError(
            "SEGMENT_EXECUTION_REVIEW_CONTACT_SHEET_MISSING"
        )
    bounded_contract = {
        "segment_index": int(segment_contract.get("segment_index") or 0),
        "duration_seconds": float(
            segment_contract.get("duration_seconds") or 0
        ),
        "global_start_seconds": float(
            segment_contract.get("global_start_seconds") or 0
        ),
        "global_end_seconds": float(
            segment_contract.get("global_end_seconds") or 0
        ),
        "provider_pixels_only": bool(
            segment_contract.get("provider_pixels_only")
        ),
        "local_voiceover_pending": bool(
            segment_contract.get("local_voiceover_pending")
        ),
        "local_overlay_pending": bool(
            segment_contract.get("local_overlay_pending")
        ),
        "provider_dialogue_pending_audio_review": bool(
            segment_contract.get("provider_dialogue_pending_audio_review")
        ),
        "pending_local_overlays": [
            {
                "line_id": _text(item.get("line_id"), 80),
                "line": _text(item.get("line"), 300),
                "start_seconds": item.get("start_seconds"),
                "end_seconds": item.get("end_seconds"),
            }
            for item in list(
                segment_contract.get("pending_local_overlays") or []
            )[:16]
            if isinstance(item, dict)
        ],
        "pending_provider_dialogue": [
            {
                "line_id": _text(item.get("line_id"), 80),
                "line": _text(item.get("line"), 500),
                "start_seconds": item.get("start_seconds"),
                "end_seconds": item.get("end_seconds"),
            }
            for item in list(
                segment_contract.get("pending_provider_dialogue") or []
            )[:16]
            if isinstance(item, dict)
        ],
        "authoritative_product_reference_supplied": bool(
            segment_contract.get("authoritative_product_reference_supplied")
        ),
        "segment_goal": _text(segment_contract.get("segment_goal"), 600),
        "timeline": [
            {
                "start_seconds": float(
                    item.get("start_seconds")
                    if item.get("start_seconds") is not None
                    else item.get("start_second") or 0
                ),
                "end_seconds": float(
                    item.get("end_seconds")
                    if item.get("end_seconds") is not None
                    else item.get("end_second") or 0
                ),
                "action": _text(item.get("action"), 900),
                "camera": _text(item.get("camera"), 500),
            }
            for item in list(segment_contract.get("timeline") or [])[:12]
            if isinstance(item, dict)
        ],
        "pacing": _text(segment_contract.get("pacing"), 700),
        "camera_direction": _text(
            segment_contract.get("camera_direction"), 700
        ),
        "requirements": [
            {
                "requirement_id": _text(item.get("requirement_id"), 16),
                "kind": _text(item.get("kind"), 40),
                "priority": _text(item.get("priority"), 20),
                "scope": _text(item.get("scope"), 30),
                "segment_gate_mode": _text(
                    item.get("segment_gate_mode") or "constraint_only", 24
                ),
                "start_seconds": item.get("start_seconds"),
                "end_seconds": item.get("end_seconds"),
                "segment_observable_start_seconds": item.get(
                    "segment_observable_start_seconds"
                ),
                "segment_observable_end_seconds": item.get(
                    "segment_observable_end_seconds"
                ),
                "positive_evidence_deferred_before_segment": bool(
                    item.get("positive_evidence_deferred_before_segment")
                ),
                "positive_evidence_deferred_after_segment": bool(
                    item.get("positive_evidence_deferred_after_segment")
                ),
                "intent": _text(item.get("intent"), 800),
                "interpretation": _text(item.get("interpretation"), 1000),
                "observable_checks": [
                    _text(value, 500)
                    for value in list(item.get("observable_checks") or [])[:16]
                ],
                "must_not_reuse": [
                    _text(value, 400)
                    for value in list(item.get("must_not_reuse") or [])[:16]
                ],
            }
            for item in list(requirement_contract or [])[:64]
            if isinstance(item, dict)
            and str(item.get("requirement_id") or "").strip()
        ],
    }
    system = (
        "You are an independent short-form video execution inspector. The "
        "image is an ordered contact sheet from one newly AI-generated video "
        "segment. Judge visible pixels using this authority order: supplied "
        "user requirement rows first, then the segment goal, then the Director "
        "timeline as a preferred realization rather than an immutable shot "
        "list. Never infer or request a benchmark video's story, actors, "
        "props, dialogue, captions, platform UI, or pixels. The signed semantic "
        "requirements are authoritative at the gate mode supplied with each "
        "requirement. A positive_evidence requirement must preserve its core "
        "intent, but equivalent original visual mechanisms are allowed when "
        "creative freedom permits. A constraint_only requirement can fail only "
        "when its prohibited condition is visibly present; never demand its "
        "later positive actions from this segment. "
        "Judge each requirement by its own observable checks and return concrete "
        "pixel evidence keyed by requirement_id; never replace a hook, emotional, "
        "conversion, or reference-transfer requirement with a generic action-count "
        "proxy. A provider may vary "
        "minor blocking, framing and motion details. Missing a literal planned "
        "action is not blocking by itself when the pixels deliver an equivalent "
        "original mechanism for every applicable positive_evidence requirement. "
        "A single evolving composition is valid when it visibly communicates "
        "the required progression. A static talking head with small pose changes "
        "is blocking only when it also fails a scoped hook or other positive "
        "user requirement. Top or bottom caption/search "
        "bands are not hook evidence and must be rejected when forbidden. Return "
        "strict JSON only. The current segment timeline is the only source of "
        "Director-planned local physical actions, but user requirements are the "
        "release authority and equivalent execution is allowed. Pacing and "
        "camera_direction describe style only: never import an action mentioned "
        "there when that action is absent from this segment's timeline. Judge "
        "product timing against global_start_seconds/global_end_seconds and the "
        "supplied user time-window requirements, not against an invented local "
        "zero point. The contact sheet contains raw provider frames in "
        "chronological tile order; the inspector has added no timestamp badges, "
        "captions, or labels inside those frames. When local_voiceover_pending "
        "is true, audio, spoken words, numeric narration, and CTA wording are "
        "not yet in these provider pixels: mark those checks not_observable and "
        "do not fail an otherwise visibly satisfied requirement for them. When "
        "provider_dialogue_pending_audio_review is true, the provider has been "
        "asked to generate the exact rows in pending_provider_dialogue as native "
        "audio, but a contact sheet cannot prove or disprove those spoken words. "
        "The later audiovisual voice audit owns that evidence. Mark exact product "
        "naming, spoken CTA wording, accent, timing, and delivery assigned to this "
        "surface not_observable here; never demand duplicate visible CTA text, a "
        "product-card graphic, or platform UI merely because the valid contract "
        "allows the same meaning to be delivered audibly. Still judge every "
        "currently visible action and product pixel normally. When "
        "local_overlay_pending is true, every row in pending_local_overlays is "
        "added by the deterministic compositor after provider generation. Do "
        "not require those exact words, numbers, captions, CTA labels, or their "
        "typography in these raw frames; judge only the visual mechanism that "
        "the current segment itself must supply. A partial_positive_evidence "
        "time-window row crosses a segment boundary. Its deferred actions are "
        "owned by another segment or final composed-video QA and can never fail "
        "this provider clip; use it only as advisory context for the intersecting "
        "visual window. When "
        "authoritative_product_reference_supplied is true, wording physically "
        "printed on that package belongs to the uploaded package identity and "
        "must not be reclassified as an invented claim or overlay. Fail only "
        "additional generated sales text, overlays, or wording that visibly "
        "contradicts the authoritative package."
    )
    user = (
        "SIGNED SEGMENT CONTRACT:\n"
        + json.dumps(bounded_contract, ensure_ascii=False, sort_keys=True)
        + "\nREVIEW FLAGS:\n"
        + json.dumps(
            {
                "forbid_overlay_bands": bool(forbid_overlay_bands),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\nReturn keys: status ('pass' or 'fail'), plan_adherence "
        "('match', 'minor_drift', or 'blocking_mismatch'), opening_hook "
        "('strong', 'adequate', 'weak', or 'not_applicable'), "
        "distinct_visual_states (integer 1..20), visible_overlay_bands "
        "(boolean), visible_platform_ui (boolean), requirement_evidence "
        "(object keyed by every supplied requirement_id; each value has status "
        "'pass'|'fail'|'not_observable', observed_evidence array, missing_checks "
        "array, and rationale), observed_execution "
        "(array), missing_planned_execution (array), blocking_reasons "
        "(array), and repair_instruction (one concise provider-facing sentence). "
        "A critical or high positive_evidence requirement needs concrete visual "
        "evidence for its visually applicable core intent. Do not turn deferred "
        "audio or whole-video checks into a segment failure. Treat ordinary "
        "product-label text as part of the physical "
        "product, not as an overlay band."
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
                        "image_url": {
                            "url": _data_url(str(sheet)),
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
    }
    digest = hashlib.sha256(
        (
            f"{SEGMENT_EXECUTION_VIDEO_REVIEW_POLICY_VERSION}|{execution_id}|"
            f"{hashlib.sha256(sheet.read_bytes()).hexdigest()}|"
            + json.dumps(
                bounded_contract,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ).encode("utf-8")
    ).hexdigest()[:28]
    result: dict[str, Any] | None = None
    semantic_errors: list[str] = []
    for semantic_attempt in range(3):
        body = _routed_multimodal_completion(
            db,
            payload=payload,
            request_id=(
                f"cf-segment-execution:{digest}:s{semantic_attempt + 1}"
            ),
            source="content_segment_execution_review",
            error_prefix="SEGMENT_EXECUTION_REVIEW",
        )
        raw = extract_output_text(body).strip()
        if not raw:
            semantic_errors.append("empty assistant output")
            continue
        try:
            result = _json_object_from_text(
                raw,
                error_prefix="Segment execution review",
            )
            break
        except (ContentFactoryApiError, ValueError) as exc:
            semantic_errors.append(str(exc)[:240])
    if result is None:
        raise ContentFactoryApiError(
            "SEGMENT_EXECUTION_REVIEW_SEMANTIC_RETRY_EXHAUSTED: "
            + "; ".join(semantic_errors[-3:])
        )
    adherence = str(result.get("plan_adherence") or "").strip().lower()
    opening = str(result.get("opening_hook") or "").strip().lower()
    try:
        distinct_states = int(result.get("distinct_visual_states") or 0)
    except (TypeError, ValueError):
        distinct_states = 0
    requirement_evidence = dict(result.get("requirement_evidence") or {})
    required_requirement_ids = {
        str(item.get("requirement_id") or "").strip()
        for item in list(bounded_contract.get("requirements") or [])
        if str(item.get("segment_gate_mode") or "").strip().lower()
        == "positive_evidence"
        if str(item.get("priority") or "").strip().lower()
        in {"critical", "high"}
    }
    has_deferred_delivery_surface = bool(
        bounded_contract.get("local_voiceover_pending")
        or bounded_contract.get("local_overlay_pending")
        or bounded_contract.get("provider_dialogue_pending_audio_review")
    )
    failed_requirements: list[str] = []
    for requirement_id in sorted(required_requirement_ids):
        row = dict(requirement_evidence.get(requirement_id) or {})
        status = str(row.get("status") or "").strip().lower()
        observed = list(row.get("observed_evidence") or [])
        if status == "pass" and observed:
            continue
        # ``not_observable`` is a model-authored surface decision, not an
        # approval. It is deferred only when the signed plan declares a real
        # audio/overlay delivery surface that a still contact sheet cannot
        # inspect. Final audiovisual QA remains responsible for that evidence.
        if status == "not_observable" and has_deferred_delivery_surface:
            continue
        failed_requirements.append(requirement_id)
    # The AI-authored timeline is a production plan, not a second source of
    # user requirements.  When no positive requirement is scoped to this
    # provider segment, literal plan drift is advisory and the composed-video
    # inspector owns later conversion/order checks.  This prevents a local
    # Director preference from vetoing a clip that satisfies the actual user
    # contract, while opening hooks and other scoped positive requirements
    # remain fail-closed.
    # The model's top-level status can include advisory plan drift or a
    # cross-segment partial requirement.  Only concrete failures from the
    # fully scoped positive-evidence rows are release blockers here; the final
    # composed-video guardian owns the deferred whole-video requirements.
    plan_gate_failed = False
    blocking = bool(
        plan_gate_failed
        or bool(result.get("visible_platform_ui"))
        or (
            bool(forbid_overlay_bands)
            and bool(result.get("visible_overlay_bands"))
        )
        or failed_requirements
    )
    return {
        **result,
        "status": "fail" if blocking else "pass",
        "policy_version": SEGMENT_EXECUTION_VIDEO_REVIEW_POLICY_VERSION,
        "blocking": blocking,
        "distinct_visual_states": distinct_states,
        "failed_requirement_ids": failed_requirements,
    }


def review_composed_intent_fidelity_api(
    db: Session,
    *,
    contact_sheet_path: str,
    benchmark_contact_sheet_path: str | None,
    intent_requirements: list[dict[str, Any]],
    director_requirement_execution: list[dict[str, Any]],
    production_requirement_execution: list[dict[str, Any]],
    source_transformation_diff: dict[str, Any] | None,
    composed_execution_evidence: dict[str, Any] | None = None,
    segment_contact_sheets: list[dict[str, Any]] | None = None,
    execution_id: str,
) -> dict[str, Any]:
    """Review the actual composed video against the original user intent.

    This is the final semantic gate after provider rendering, local overlays,
    audio assembly and concatenation.  It deliberately separates originality
    from effectiveness: avoiding copied pixels cannot compensate for losing a
    benchmark's requested attention mechanism, pacing intensity, or conversion
    job.
    """

    sheet = Path(str(contact_sheet_path or ""))
    if not sheet.is_file() or sheet.stat().st_size <= 1024:
        raise ContentFactoryApiError("FINAL_INTENT_REVIEW_CONTACT_SHEET_MISSING")
    benchmark = Path(str(benchmark_contact_sheet_path or ""))
    has_benchmark = benchmark.is_file() and benchmark.stat().st_size > 1024
    requirements = [
        dict(item)
        for item in list(intent_requirements or [])[:128]
        if isinstance(item, dict)
        and str(item.get("requirement_id") or "").strip()
    ]
    system = (
        "You are the final intent guardian for a professional short-video "
        "factory. The first contact sheet is the actual composed deliverable in "
        "chronological order. When a second sheet is supplied, it is a user "
        "benchmark and is evidence only for abstract mechanisms explicitly "
        "named in the requirements. Judge every requirement independently from "
        "visible/audible-structure evidence. Separate originality from "
        "effectiveness: a new premise may still fail if it loses the requested "
        "hook force, attention progression, rhythm, emotional escalation, or "
        "conversion bridge; similarity alone must never earn a pass. This call "
        "must also judge temporal continuity across the supplied chronological "
        "frames and signed segment boundaries. Unless the signed execution "
        "explicitly requests a transformation, a recurring cast or central "
        "subject must keep the same identity, age, face, hair, wardrobe and "
        "body design; the location and visual medium must remain coherent. An "
        "unplanned switch such as illustration or animation to photoreal live "
        "action (or the reverse), a replacement actor, or a materially different "
        "room is a blocking current-deliverable failure even when each isolated "
        "segment looks polished. Identify the affected segment indices and "
        "request segment regeneration rather than accepting the discontinuity. "
        "This call "
        "reviews exactly one currently composed video, not completion of the "
        "whole requested batch and not post-publication performance. For every "
        "requirement, separate checks observable in this video now from checks "
        "that require other deliverables, publication, elapsed time, analytics, "
        "or future human work. Those deferred checks must be reported but must "
        "not fail or block the current video. A mixed requirement may pass its "
        "current-video subset while deferring its series or post-publish subset. "
        "Never ask a video generator for missing future videos, publication IDs, "
        "elapsed-day evidence, view-rate data, click data, or campaign results. "
        "Never "
        "demand reuse of benchmark actors, props, setting, wording, UI, captions, "
        "watermarks, or signature metaphors listed under must_not_reuse. Return "
        "strict JSON only."
    )
    audit_packet = {
        "review_scope": {
            "unit": "one_current_composed_video",
            "video_index": int(
                dict(composed_execution_evidence or {}).get("video_index") or 0
            ),
            "series_completion_is_deferred": True,
            "post_publication_measurement_is_deferred": True,
            "blocking_evidence_boundary": (
                "only facts observable in this composed video and its signed "
                "execution metadata"
            ),
        },
        "intent_requirements": requirements,
        "director_requirement_execution": list(
            director_requirement_execution or []
        )[:128],
        "production_requirement_execution": list(
            production_requirement_execution or []
        )[:128],
        "source_transformation_diff": dict(source_transformation_diff or {}),
        "benchmark_supplied": has_benchmark,
        # Contact sheets are authoritative for visible execution. Exact copy,
        # voice identity, delivery mode and segment ownership are represented
        # separately so an audio/copy requirement is never guessed from still
        # frames or marked observable merely because a visual looks plausible.
        "composed_execution_evidence": dict(
            composed_execution_evidence or {}
        ),
        "segment_contact_sheet_indices": [
            int(item.get("segment_index") or 0)
            for item in list(segment_contact_sheets or [])
            if isinstance(item, dict)
            and int(item.get("segment_index") or 0) > 0
        ],
    }
    text_prompt = (
        "AUDIT PACKET:\n"
        + json.dumps(audit_packet, ensure_ascii=False, sort_keys=True)
        + "\nReturn keys: status ('pass' or 'fail'), current_deliverable_status "
        "('pass' or 'fail'), requirement_evidence (object keyed by every "
        "requirement_id; each value contains applicability "
        "'current_deliverable'|'series_aggregate'|'post_publish'|'mixed', "
        "blocking_at_current_stage boolean, status "
        "'pass'|'fail'|'not_observable'|'deferred', observed_evidence array, "
        "missing_checks array, deferred_checks array, and rationale), "
        "current_deliverable_blocking_reasons array, originality "
        "('pass'|'fail'|'not_applicable'), "
        "benchmark_effectiveness_transfer ('pass'|'fail'|'not_applicable'), "
        "blocking_reasons array, repair_scope ('segment_regeneration' or "
        "'director_replan'), evidence_segment_indices array (every segment whose "
        "pixels were used to prove the defect), regenerate_segment_indices array "
        "(the smallest set of defective segments that must be replaced), and "
        "repair_instruction. A neighboring segment used only as continuity "
        "evidence belongs in evidence_segment_indices, not automatically in "
        "regenerate_segment_indices. "
        "Every critical/high current-deliverable check needs concrete evidence. "
        "Independently report any unplanned cross-segment cast, wardrobe, scene, "
        "or visual-medium discontinuity as a current-deliverable blocking reason, "
        "even if no requirement uses those exact words. "
        "For spoken delivery, compare every segment's observed_text against its "
        "signed dialogue_lines and expected_text. Added sales claims, benefits, "
        "testimonials, prices, offers or calls to action are blocking; do not "
        "treat the presence of the expected words as proof that the whole spoken "
        "line is faithful. The per-segment spoken_copy_review is an independent "
        "audio adjudication, not a suggestion. When it reports status='pass', "
        "semantic_fidelity='meaning_preserved', likely_asr_error=true, no "
        "material_differences, and no blocking reasons, do not fail solely "
        "because ASR phonetically rendered a brand or acronym differently. You "
        "may override that adjudication only when the supplied audiovisual "
        "evidence identifies a concrete audible substitution or omitted meaning "
        "beyond the ASR spelling, and must state that independent evidence. "
        "Series-aggregate and post-publish checks must be status='deferred' with "
        "blocking_at_current_stage=false. For a mixed requirement, evaluate the "
        "current-video subset and list all future checks under deferred_checks; "
        "future checks cannot make current_deliverable_status fail. Do not use "
        "generic statements such as 'looks good'."
    )
    content: list[dict[str, Any]] = [
        {"type": "text", "text": text_prompt},
        {
            "type": "image_url",
            "image_url": {"url": _data_url(str(sheet)), "detail": "high"},
        },
    ]
    for item in list(segment_contact_sheets or [])[:16]:
        if not isinstance(item, dict):
            continue
        try:
            segment_index = int(item.get("segment_index") or 0)
        except (TypeError, ValueError):
            continue
        segment_sheet = Path(str(item.get("path") or ""))
        if segment_index <= 0 or not segment_sheet.is_file():
            continue
        content.extend([
            {
                "type": "text",
                "text": (
                    f"The next image contains chronological frames from "
                    f"SEGMENT {segment_index} ONLY. Inspect it at full detail, "
                    "then compare its cast identity, wardrobe, location and "
                    "visual medium against the adjacent segment-only sheets."
                ),
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(str(segment_sheet)),
                    "detail": "high",
                },
            },
        ])
    if has_benchmark:
        content.extend([
            {
                "type": "text",
                "text": (
                    "The next image is the benchmark contact sheet. Compare only "
                    "the explicitly authorized abstract mechanisms."
                ),
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(str(benchmark)),
                    "detail": "high",
                },
            },
        ])
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    digest = hashlib.sha256(
        (
            execution_id
            + hashlib.sha256(sheet.read_bytes()).hexdigest()
            + "".join(
                hashlib.sha256(
                    Path(str(item.get("path") or "")).read_bytes()
                ).hexdigest()
                for item in list(segment_contact_sheets or [])
                if isinstance(item, dict)
                and Path(str(item.get("path") or "")).is_file()
            )
            + json.dumps(requirements, ensure_ascii=False, sort_keys=True)
        ).encode("utf-8")
    ).hexdigest()[:28]
    result: dict[str, Any] | None = None
    errors: list[str] = []
    for attempt in range(3):
        body = _routed_multimodal_completion(
            db,
            payload=payload,
            request_id=f"cf-final-intent:{digest}:s{attempt + 1}",
            source="content_final_intent_review",
            error_prefix="FINAL_INTENT_REVIEW",
        )
        raw = extract_output_text(body).strip()
        if not raw:
            errors.append("empty assistant output")
            continue
        try:
            result = _json_object_from_text(
                raw,
                error_prefix="Final intent review",
            )
            break
        except (ContentFactoryApiError, ValueError) as exc:
            errors.append(str(exc)[:240])
    if result is None:
        raise ContentFactoryApiError(
            "FINAL_INTENT_REVIEW_SEMANTIC_RETRY_EXHAUSTED: "
            + "; ".join(errors[-3:])
        )
    result["blocking_reasons"] = _normalized_reason_texts(
        result.get("blocking_reasons")
    )
    result["current_deliverable_blocking_reasons"] = (
        _normalized_reason_texts(
            result.get("current_deliverable_blocking_reasons")
        )
    )
    valid_segment_sheets = [
        dict(item)
        for item in list(segment_contact_sheets or [])[:16]
        if isinstance(item, dict)
        and int(item.get("segment_index") or 0) > 0
        and Path(str(item.get("path") or "")).is_file()
    ]
    if len(valid_segment_sheets) >= 2:
        continuity_content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                "Independently audit only cross-segment continuity in this one "
                "composed short video. Compare every adjacent segment pair. "
                "Use the signed execution evidence to distinguish an intended "
                "change from an accidental one. A recurring protagonist or "
                "central subject must retain identity, age, face, hair, wardrobe, "
                "body design, location logic and visual medium unless the signed "
                "story explicitly authorizes a transformation. Treat animation "
                "switching to photoreal live action, a replacement actor, or an "
                "unmotivated room change as blocking. Return strict JSON with "
                "blocking boolean, evidence_segment_indices array (both sides "
                "of a boundary when both were inspected), "
                "regenerate_segment_indices array (only the minimal defective "
                "segment or segments to replace), blocking_reasons array, and "
                "repair_instruction. Do not put a valid neighboring segment in "
                "regenerate_segment_indices merely because it was needed for "
                "comparison.\nSIGNED EVIDENCE:\n"
                + json.dumps(
                    dict(composed_execution_evidence or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
        }]
        for item in valid_segment_sheets:
            segment_index = int(item["segment_index"])
            continuity_content.extend([
                {
                    "type": "text",
                    "text": f"SEGMENT {segment_index} ONLY, chronological frames.",
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _data_url(str(item["path"])),
                        "detail": "high",
                    },
                },
            ])
        continuity_payload = {
            "messages": [{
                "role": "system",
                "content": (
                    "You are an independent multimodal continuity inspector. "
                    "Judge actual pixels across adjacent segment-only contact "
                    "sheets, not the quality of each sheet in isolation. Return "
                    "strict JSON only."
                ),
            }, {
                "role": "user",
                "content": continuity_content,
            }],
            "temperature": 0.0,
            "max_tokens": 900,
            "response_format": {"type": "json_object"},
        }
        continuity_result: dict[str, Any] | None = None
        continuity_errors: list[str] = []
        for attempt in range(3):
            body = _routed_multimodal_completion(
                db,
                payload=continuity_payload,
                request_id=(
                    f"cf-final-continuity:{digest}:c{attempt + 1}"
                ),
                source="content_final_continuity_review",
                error_prefix="FINAL_CONTINUITY_REVIEW",
            )
            raw = extract_output_text(body).strip()
            if not raw:
                continuity_errors.append("empty assistant output")
                continue
            try:
                continuity_result = _json_object_from_text(
                    raw,
                    error_prefix="Final continuity review",
                )
                break
            except (ContentFactoryApiError, ValueError) as exc:
                continuity_errors.append(str(exc)[:240])
        if continuity_result is None:
            raise ContentFactoryApiError(
                "FINAL_CONTINUITY_REVIEW_SEMANTIC_RETRY_EXHAUSTED: "
                + "; ".join(continuity_errors[-3:])
            )
        continuity_blocking = bool(continuity_result.get("blocking"))
        result["continuity_review"] = continuity_result
        if continuity_blocking:
            continuity_reasons = _normalized_reason_texts(
                continuity_result.get("blocking_reasons")
            )
            result["status"] = "fail"
            result["current_deliverable_status"] = "fail"
            result["blocking_reasons"] = list(dict.fromkeys([
                *list(result.get("blocking_reasons") or []),
                *continuity_reasons,
            ]))
            result["current_deliverable_blocking_reasons"] = list(
                dict.fromkeys([
                    *list(
                        result.get("current_deliverable_blocking_reasons")
                        or []
                    ),
                    *continuity_reasons,
                ])
            )
            result_evidence_indices = {
                *[
                    int(value)
                    for value in list(
                        result.get("evidence_segment_indices")
                        or result.get("affected_segment_indices")
                        or []
                    )
                    if str(value).isdigit() and int(value) > 0
                ],
                *[
                    int(value)
                    for value in list(
                        continuity_result.get("evidence_segment_indices")
                        or continuity_result.get("affected_segment_indices")
                        or []
                    )
                    if str(value).isdigit() and int(value) > 0
                ],
            }
            result_regenerate_indices = {
                *[
                    int(value)
                    for value in list(
                        result.get("regenerate_segment_indices")
                        or result.get("affected_segment_indices")
                        or []
                    )
                    if str(value).isdigit() and int(value) > 0
                ],
                *[
                    int(value)
                    for value in list(
                        continuity_result.get("regenerate_segment_indices")
                        or continuity_result.get("affected_segment_indices")
                        or []
                    )
                    if str(value).isdigit() and int(value) > 0
                ],
            }
            result["evidence_segment_indices"] = sorted({
                *result_evidence_indices,
                *result_regenerate_indices,
            })
            result["regenerate_segment_indices"] = sorted(
                result_regenerate_indices
            )
            # Compatibility field now has one unambiguous meaning: paid media
            # regeneration scope. Evidence-only neighbors live in the field
            # above and can never silently broaden provider work.
            result["affected_segment_indices"] = list(
                result["regenerate_segment_indices"]
            )
            result["repair_scope"] = "segment_regeneration"
            continuity_instruction = str(
                continuity_result.get("repair_instruction") or ""
            ).strip()
            if continuity_instruction:
                result["repair_instruction"] = " ".join(
                    value
                    for value in (
                        str(result.get("repair_instruction") or "").strip(),
                        continuity_instruction,
                    )
                    if value
                )
    regeneration_indices = sorted({
        int(value)
        for value in list(
            result.get("regenerate_segment_indices")
            or result.get("affected_segment_indices")
            or []
        )
        if str(value).isdigit() and int(value) > 0
    })
    evidence_indices = sorted({
        *[
            int(value)
            for value in list(
                result.get("evidence_segment_indices")
                or result.get("affected_segment_indices")
                or []
            )
            if str(value).isdigit() and int(value) > 0
        ],
        *regeneration_indices,
    })
    result["evidence_segment_indices"] = evidence_indices
    result["regenerate_segment_indices"] = regeneration_indices
    result["affected_segment_indices"] = regeneration_indices
    evidence = dict(result.get("requirement_evidence") or {})
    blocking_ids: list[str] = []
    for item in requirements:
        requirement_id = str(item.get("requirement_id") or "").strip()
        if (
            not requirement_id
            or str(item.get("priority") or "").lower()
            not in {"critical", "high"}
        ):
            continue
        item_evidence = dict(evidence.get(requirement_id) or {})
        # Fail closed for older or malformed responses that omit the explicit
        # scope decision.  A current model may defer only by positively saying
        # that the requirement cannot be judged at this stage.
        blocking_now = item_evidence.get("blocking_at_current_stage")
        if not isinstance(blocking_now, bool):
            blocking_now = True
        if not blocking_now:
            continue
        status = str(item_evidence.get("status") or "").lower()
        observed = list(item_evidence.get("observed_evidence") or [])
        if status != "pass" or not observed:
            blocking_ids.append(requirement_id)
    blocking_ids = sorted(set(blocking_ids))
    current_status = str(
        result.get("current_deliverable_status")
        or result.get("status")
        or ""
    ).lower()
    blocking = bool(
        current_status != "pass"
        or blocking_ids
        or str(result.get("originality") or "").lower() == "fail"
        or str(
            result.get("benchmark_effectiveness_transfer") or ""
        ).lower()
        == "fail"
    )
    return {
        **result,
        "status": "fail" if blocking else "pass",
        "current_deliverable_status": "fail" if blocking else "pass",
        "blocking": blocking,
        "blocking_requirement_ids": blocking_ids,
        "policy_version": FINAL_INTENT_REVIEW_POLICY_VERSION,
    }


def review_final_intent_repair_scope_api(
    db: Session,
    *,
    final_intent_report: dict[str, Any],
    repair_history: dict[str, Any],
    execution_id: str,
) -> dict[str, Any]:
    """Choose the next repair layer from pixels plus prior repair evidence.

    The final guardian sees one rendered artifact at a time. Without the
    project-owned repair history it can repeatedly recommend another local
    segment regeneration after that strategy has already failed. This
    independent multimodal decision runs after the bounded local repair budget
    is exhausted. It never approves media or submits provider work itself.
    """

    report = dict(final_intent_report or {})
    sheet = Path(str(report.get("contact_sheet_path") or ""))
    if not sheet.is_file() or sheet.stat().st_size <= 1024:
        raise ContentFactoryApiError(
            "FINAL_INTENT_REPAIR_SCOPE_CONTACT_SHEET_MISSING"
        )
    packet = {
        "policy_version": FINAL_INTENT_REPAIR_SCOPE_POLICY_VERSION,
        "failed_final_intent_report": report,
        "project_owned_repair_history": dict(repair_history or {}),
        "allowed_repair_scopes": [
            "segment_regeneration",
            "director_replan",
        ],
    }
    system = (
        "You are the multimodal repair supervisor for one failed short-video "
        "deliverable. Inspect the actual chronological contact sheet together "
        "with the signed final-intent failure and the project-owned repair "
        "history. Choose segment_regeneration only when the remaining defect is "
        "local to one executable shot and a materially different local repair "
        "has not already failed. Choose director_replan when repeated local "
        "generations preserve the same weak hook, story mechanism, escalation, "
        "payoff, conversion bridge, or other structural defect. Also choose "
        "director_replan when the bounded "
        "local repair author repeatedly failed to produce an executable provider "
        "contract; project_owned_repair_history.local_repair_compiler_errors is "
        "authoritative evidence of that failure. Do not ask the same invalid local "
        "authoring strategy to repeat indefinitely. Do not pass or waive the "
        "quality failure. Respect project_owned_repair_history. "
        "copy_authority: provider execution must preserve its currently signed "
        "Director line, but an authorized Director replan may replace model-"
        "authored copy when copy_authority=director_model_editable; only "
        "user_verbatim_locked copy remains immutable. Do not infer retry history "
        "or copy authority from pixels; use only the supplied history. Return "
        "strict JSON only."
    )
    prompt = (
        "REPAIR PACKET:\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        + "\nReturn keys: repair_scope ('segment_regeneration' or "
        "'director_replan'), rationale, repair_instruction, "
        "failed_strategy_summary, and evidence_used array."
    )
    payload = {
        "messages": [{
            "role": "system",
            "content": system,
        }, {
            "role": "user",
            "content": [{
                "type": "text",
                "text": prompt,
            }, {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(str(sheet)),
                    "detail": "high",
                },
            }],
        }],
        "temperature": 0.0,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
    }
    digest = hashlib.sha256(
        (
            execution_id
            + hashlib.sha256(sheet.read_bytes()).hexdigest()
            + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        ).encode("utf-8")
    ).hexdigest()[:28]
    errors: list[str] = []
    for attempt in range(3):
        body = _routed_multimodal_completion(
            db,
            payload=payload,
            request_id=f"cf-final-repair-scope:{digest}:s{attempt + 1}",
            source="content_final_intent_repair_scope",
            error_prefix="FINAL_INTENT_REPAIR_SCOPE",
        )
        raw = extract_output_text(body).strip()
        if not raw:
            errors.append("empty assistant output")
            continue
        try:
            result = _json_object_from_text(
                raw,
                error_prefix="Final intent repair scope review",
            )
        except (ContentFactoryApiError, ValueError) as exc:
            errors.append(str(exc)[:240])
            continue
        scope = str(result.get("repair_scope") or "").strip().lower()
        if scope not in {"segment_regeneration", "director_replan"}:
            errors.append(f"invalid repair_scope {scope!r}")
            continue
        return {
            **result,
            "repair_scope": scope,
            "policy_version": FINAL_INTENT_REPAIR_SCOPE_POLICY_VERSION,
        }
    raise ContentFactoryApiError(
        "FINAL_INTENT_REPAIR_SCOPE_SEMANTIC_RETRY_EXHAUSTED: "
        + "; ".join(errors[-3:])
    )


def review_final_intent_repair_targets_api(
    db: Session,
    *,
    final_intent_report: dict[str, Any],
    candidate_segments: list[dict[str, Any]],
    execution_id: str,
) -> dict[str, Any]:
    """Resolve evidence segments versus the minimal paid repair targets.

    Continuity inspection must observe both sides of a boundary, while only
    one side may be defective. Historical reviewer schemas used one ambiguous
    field for both meanings. This multimodal supervisor inspects the same
    composed pixels and returns two explicit sets; Python validates identity
    and ownership only and makes no creative inference from prose.
    """

    report = dict(final_intent_report or {})
    sheet = Path(str(report.get("contact_sheet_path") or ""))
    if not sheet.is_file() or sheet.stat().st_size <= 1024:
        raise ContentFactoryApiError(
            "FINAL_INTENT_REPAIR_TARGET_CONTACT_SHEET_MISSING"
        )
    candidates = [
        {
            "task_id": int(item.get("task_id") or 0),
            "video_index": int(item.get("video_index") or 0),
            "segment_index": int(item.get("segment_index") or 0),
        }
        for item in list(candidate_segments or [])[:32]
        if isinstance(item, dict)
        and int(item.get("task_id") or 0) > 0
        and int(item.get("segment_index") or 0) > 0
    ]
    allowed_indices = {
        int(item["segment_index"])
        for item in candidates
    }
    if not allowed_indices:
        raise ContentFactoryApiError(
            "FINAL_INTENT_REPAIR_TARGET_CANDIDATES_MISSING"
        )
    packet = {
        "policy_version": FINAL_INTENT_REPAIR_TARGET_POLICY_VERSION,
        "failed_final_intent_report": report,
        "candidate_segments": candidates,
    }
    payload = {
        "messages": [{
            "role": "system",
            "content": (
                "You are the fault-handling multimodal supervisor for one "
                "failed composed short video. Inspect the actual chronological "
                "contact sheet and resolve an internally consistent, minimal "
                "segment repair target. Evidence segments are all segments used "
                "to prove a defect. Regeneration segments are only the defective "
                "segments that must be replaced. A valid neighboring segment "
                "used to compare a continuity boundary is evidence, not a paid "
                "regeneration target. Reconcile contradictions between arrays, "
                "blocking reasons and repair instructions. Do not waive the "
                "failure and do not invent segment IDs. Return strict JSON only."
            ),
        }, {
            "role": "user",
            "content": [{
                "type": "text",
                "text": (
                    "REPAIR TARGET PACKET:\n"
                    + json.dumps(packet, ensure_ascii=False, sort_keys=True)
                    + "\nReturn keys: evidence_segment_indices array, "
                    "regenerate_segment_indices array, rationale, and "
                    "repair_instruction. regenerate_segment_indices must be "
                    "the smallest non-empty subset of candidate segment indices "
                    "that directly resolves every blocking reason."
                ),
            }, {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(str(sheet)),
                    "detail": "high",
                },
            }],
        }],
        "temperature": 0.0,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    digest = hashlib.sha256(
        (
            execution_id
            + hashlib.sha256(sheet.read_bytes()).hexdigest()
            + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        ).encode("utf-8")
    ).hexdigest()[:28]
    errors: list[str] = []
    for attempt in range(3):
        body = _routed_multimodal_completion(
            db,
            payload=payload,
            request_id=f"cf-final-repair-target:{digest}:s{attempt + 1}",
            source="content_final_intent_repair_targets",
            error_prefix="FINAL_INTENT_REPAIR_TARGET",
        )
        raw = extract_output_text(body).strip()
        if not raw:
            errors.append("empty assistant output")
            continue
        try:
            result = _json_object_from_text(
                raw,
                error_prefix="Final intent repair target review",
            )
        except (ContentFactoryApiError, ValueError) as exc:
            errors.append(str(exc)[:240])
            continue
        regeneration = sorted({
            int(value)
            for value in list(result.get("regenerate_segment_indices") or [])
            if str(value).strip().isdigit()
            and int(value) in allowed_indices
        })
        if not regeneration:
            errors.append("missing valid regenerate_segment_indices")
            continue
        evidence = sorted({
            *regeneration,
            *[
                int(value)
                for value in list(result.get("evidence_segment_indices") or [])
                if str(value).strip().isdigit()
                and int(value) in allowed_indices
            ],
        })
        return {
            **result,
            "evidence_segment_indices": evidence,
            "regenerate_segment_indices": regeneration,
            "policy_version": FINAL_INTENT_REPAIR_TARGET_POLICY_VERSION,
        }
    raise ContentFactoryApiError(
        "FINAL_INTENT_REPAIR_TARGET_SEMANTIC_RETRY_EXHAUSTED: "
        + "; ".join(errors[-3:])
    )


def replan_failed_segment_execution_api(
    db: Session,
    *,
    segment_contract: dict[str, Any],
    execution_review: dict[str, Any],
    dialogue_lines: list[dict[str, Any]],
    execution_id: str,
    requirement_contract: list[dict[str, Any]],
    forbid_overlay_bands: bool,
    reference_manifest: list[dict[str, Any]] | None = None,
    reference_image_paths: list[str] | None = None,
    provider_key: str = "",
    provider_prompt_language: str = "en",
    provider_prompt_max_characters: int = 495,
    initial_multimodal_authoring: bool = False,
) -> dict[str, Any]:
    """Ask the Director model to make a repeatedly missed segment executable.

    This is deliberately not a template fallback. The model may simplify only
    action and camera choreography after bounded provider attempts have failed;
    the approved story function, exact dialogue, product facts, voice identity,
    reference authority, originality boundary, and conversion intent remain
    immutable.
    """

    provider_character_limit = max(
        256,
        min(12_000, int(provider_prompt_max_characters or 495)),
    )
    provider_action_budget = max(
        180,
        min(4_000, int(provider_character_limit * 0.55)),
    )
    duration = max(
        1.0,
        min(30.0, float(segment_contract.get("duration_seconds") or 10.0)),
    )
    original_timeline: list[dict[str, Any]] = []
    for item in list(segment_contract.get("timeline") or [])[:12]:
        if not isinstance(item, dict):
            continue
        original_timeline.append({
            "start_seconds": float(
                item.get("start_seconds")
                if item.get("start_seconds") is not None
                else item.get("start_second") or 0
            ),
            "end_seconds": float(
                item.get("end_seconds")
                if item.get("end_seconds") is not None
                else item.get("end_second") or 0
            ),
            "action": _text(item.get("action"), 1000),
            "camera": _text(item.get("camera"), 600),
            "dialogue_key": _text(item.get("dialogue_key"), 80),
        })
    immutable_dialogue = [
        {
            "line_id": _text(item.get("line_id"), 80),
            "speaker_id": _text(
                item.get("speaker_id") or item.get("speaker"), 80
            ),
            "line": _text(item.get("line"), 500),
        }
        for item in list(dialogue_lines or [])[:12]
        if isinstance(item, dict) and str(item.get("line") or "").strip()
    ]
    valid_line_ids = {
        str(item.get("line_id") or "") for item in immutable_dialogue
    }

    def normalize_dialogue_key(value: Any) -> str:
        raw = _text(value, 160).strip()
        if not raw or raw in valid_line_ids:
            return raw
        # A signed visual timeline may bind spoken copy and a deterministic
        # local display cue in one legacy comma-separated field. Provider
        # dialogue accepts only immutable spoken line IDs. Preserve the single
        # real spoken ID and leave genuinely unknown/ambiguous keys invalid.
        candidates = [
            item.strip()
            for item in re.split(r"[,|]", raw)
            if item.strip()
        ]
        matching = list(dict.fromkeys(
            item for item in candidates if item in valid_line_ids
        ))
        if matching and len(matching) == len(candidates):
            return ",".join(matching)
        return matching[0] if len(matching) == 1 else raw

    def dialogue_key_is_valid(value: Any) -> bool:
        normalized = normalize_dialogue_key(value)
        if not normalized:
            return True
        return all(
            item.strip() in valid_line_ids
            for item in re.split(r"[,|]", normalized)
            if item.strip()
        )

    for row in original_timeline:
        row["dialogue_key"] = normalize_dialogue_key(
            row.get("dialogue_key")
        )
    failure_evidence = {
        # Final composed-video QA can identify a defect that an earlier
        # segment-only review could not see. Its repair instruction and
        # requirement IDs are authoritative planning evidence. Omitting them
        # made the recovery Director simplify old mechanics while unknowingly
        # deleting the exact hook/product defect it was supposed to repair.
        "repair_instruction": _text(
            execution_review.get("repair_instruction"), 2400
        ),
        "failed_requirement_ids": [
            _text(value, 96)
            for value in list(
                execution_review.get("failed_requirement_ids") or []
            )[:64]
            if str(value or "").strip()
        ],
        "observed_execution": [
            _text(value, 400)
            for value in list(execution_review.get("observed_execution") or [])[:10]
        ],
        "missing_planned_execution": [
            _text(value, 400)
            for value in list(
                execution_review.get("missing_planned_execution") or []
            )[:10]
        ],
        "blocking_reasons": [
            _text(value, 400)
            for value in list(execution_review.get("blocking_reasons") or [])[:8]
        ],
    }
    reference_inventory: list[dict[str, Any]] = []
    for position, raw in enumerate(list(reference_manifest or [])[:10], 1):
        if not isinstance(raw, dict):
            continue
        reference_inventory.append({
            "alias": _text(raw.get("alias") or f"@image{position}", 32),
            "filename": _text(raw.get("filename"), 160),
            "description": _text(raw.get("description"), 700),
            "semantic_roles": [
                _text(value, 48)
                for value in list(raw.get("semantic_roles") or [])[:6]
                if str(value or "").strip()
            ],
            "is_product_anchor": bool(raw.get("is_product_anchor")),
        })
    system = (
        (
            "You are the multimodal execution Director for an AI short-video "
            "factory. Before any billable video request, inspect every attached "
            "reference image and translate the approved signed segment into the "
            "best provider-executable visual and performance choreography. Return "
            "an authored execution plan, "
            if initial_multimodal_authoring
            else
            "You are the recovery Director for an AI short-video factory. A video "
            "provider has failed to execute an approved segment, and the supplied "
            "multimodal evidence identifies the actual visible defect. Return a new, "
        )
        +
        "provider-executable choreography, not a generic template and not a "
        "copy of any benchmark. Preserve the segment's narrative function and "
        "all immutable dialogue exactly. Do not add product facts, claims, offer "
        "terms, characters, settings, overlays, captions, platform UI, or source "
        "video story elements. Use observable physical states and economical "
        "camera language. Timeline beats describe story progression; they are not "
        "a ceiling on edit shots. A single reliable physical action may be shown "
        "through several short, materially different framings or hard-cut states. "
        "Never turn provider reliability into a long unchanged hold. Translate the "
        "signed pacing and visual grammar into an explicit edit plan: how many "
        "distinct shots the current segment needs, the typical or maximum hold "
        "time in seconds, and the ordered framing/camera progression. If the signed "
        "pace is brisk, high-energy, scroll-stopping, or otherwise fast, prohibit "
        "a single slow push-in, a prolonged static composition, and one unchanged "
        "action occupying several seconds. If the signed pace is intentionally "
        "slow, state that measurable choice explicitly instead of guessing. "
        + (
            "The supplied execution evidence is planning context, not proof of a "
            "failed generation. Preserve strong feasible visual hooks and simplify "
            "only genuinely conflicting or overloaded mechanics. Do not weaken a "
            "feasible hook merely because the plan is visually ambitious. "
            if initial_multimodal_authoring
            else
            "The failure evidence is proof that the provider could not reliably perform "
            "those mechanics. Never answer by prescribing the same failed action, "
            "trajectory, object relationship, or camera move with more detail. "
            "The supplied repair_instruction and failed_requirement_ids are the "
            "authoritative recovery contract. A mechanic that this contract explicitly "
            "requires to be preserved is not a failed mechanic and must remain in the "
            "new choreography; simplify only the surrounding execution needed to make "
            "that required mechanic reliable. "
        )
        +
        "When a mechanic is genuinely conflicting, overloaded, or proven unreliable, "
        "remove that mechanic and express its narrative meaning through a simpler single "
        "choice, reaction, reveal, or before/after cut. Exact prop placement is not "
        "a narrative requirement unless a supplied high-priority requirement says "
        "that it is. When no such requirement exists, optimize for visible intent "
        "rather than literal choreography. "
        "Reference images are optional, narrowly scoped appearance anchors, never "
        "a storyboard and never the authority for the complete action sequence. "
        "The authored text timeline is the sole authority for chronology, motion, "
        "editing rhythm, camera movement, performance, dialogue, narration, and "
        "effects. Use references only to lock a recurring fictional character, a "
        "recurring scene, the authoritative product package, or an unusually "
        "complex spatial pose that truly cannot be communicated reliably in text. "
        "An action reference is optional and should normally be omitted when the "
        "action can be described in the provider instruction. Inspect every supplied "
        "reference image at pixel level before selecting it. Explicitly reject a "
        "generated reference with extra or missing arms, hands, fingers or objects; "
        "fused anatomy; duplicated body parts; impossible contact; malformed product "
        "placement; contradictory state; embedded UI; or other visible artifacts. "
        "Never ask the video model to repair a defective still by following it. If a "
        "static reference conflicts with the first chronological state, competes "
        "with the text instruction, or is unnecessary, omit it from "
        "keep_reference_aliases. Keep the minimum usable references needed for the "
        "segment. When a pixel-inspected, usable character anchor is supplied for a "
        "recurring cast, retain at least one such anchor. When a pixel-inspected, "
        "usable scene anchor for the current segment is supplied, retain at least one "
        "such anchor. Zero generated references is valid only when every supplied "
        "anchor for those duties is visibly unusable or conflicting and that finding "
        "is recorded in reference_assessments. A clean authoritative uploaded product "
        "anchor must remain when the "
        "segment visibly includes the product. "
        "Omitting a character or scene reference transfers that appearance duty to "
        "the text lane; it does not erase it. In that case the selected-language "
        "provider_action values must explicitly establish the fictional visual "
        "medium, the visible subject, and the environment before directing motion. "
        "Never rely on a still image to communicate chronology, pace, emotion, "
        "camera movement, edit rhythm, spoken performance, product interaction, or "
        "the complete story. Those duties always remain explicit in text even when "
        "a usable anchor is selected. "
        "Do not require a realistic or photorealistic human face reference. "
        "When dialogue is assigned to provider_dialogue, coordinate the visible "
        "performance, declared screen relation, emotional delivery, and lip sync or "
        "character voiceover as one native audiovisual event. Do not replace it with "
        "silent acting or generic narration. The "
        "replan must remain visually original and must still deliver the approved "
        "hook and conversion function. In addition to the full audit timeline, "
        "author a compact provider_action_en and provider_action_zh for every "
        "timeline row. These are the actual provider-facing video directions, not "
        "summaries and not captions for the references. Across the selected-language "
        "provider_action values, efficiently use the available character budget to "
        "preserve: visual medium; subject and environment; ordered visible action; "
        "emotion or performance; camera and edit rhythm; product interaction and "
        "placement; every visible state, count, time reading, product-presence "
        "boundary, and ordered hard-cut state. Dialogue remains in its immutable "
        "lane, but the visible performance and timing that support it belong here. "
        "Use concise semicolon clauses instead of dropping the end of an action. "
        "Return provider_visual_context_en and provider_visual_context_zh as a "
        "separate compact text-authored setup containing only the fictional visual "
        "medium, recurring visible subject, and environment. It must not delegate "
        "those duties to a reference image. The execution service places this "
        "setup before the first timed provider action. "
        "Return provider_direction_en and provider_direction_zh as one compact, "
        "provider-ready direction line for the complete current segment. It must "
        "explicitly preserve three independently authored duties: the requested "
        "editing pace/rhythm, the camera/shot-change grammar, and the visual "
        "medium/style. Use Arabic numerals. State a concrete shot count and a "
        "concrete hold/cut interval in seconds, followed by an ordered shot or "
        "framing progression. For a deliberately continuous take, explicitly say "
        "one shot and give its duration. Do not use a generic phrase such as "
        "cinematic, dynamic, brisk, fast cuts, or three beats as a substitute for "
        "this measurable plan. Name the actual cut density or action speed, the "
        "actual shot progression or camera changes, and the actual signed visual "
        "medium. Keep each language "
        "version concise enough for the provider transport budget. "
        "Returned fields contain creative execution directions only. Do not add "
        "provider UI commands or mode labels such as '生成视频：', '视频生成：', "
        "or 'Generate video:'; the selected provider mode owns submission intent. "
        "The Chinese action must be natural Simplified Chinese suitable for a "
        "Chinese video composer. Return strict JSON only."
    )
    user = (
        "ORIGINAL SIGNED EXECUTION CONTRACT:\n"
        + json.dumps(
            {
                "segment_index": int(segment_contract.get("segment_index") or 0),
                "duration_seconds": duration,
                "segment_goal": _text(segment_contract.get("segment_goal"), 700),
                "visual_style": _text(
                    segment_contract.get("visual_style"), 1200
                ),
                "visual_grammar": _text(
                    segment_contract.get("visual_grammar"), 1200
                ),
                "project_visual_style_requirement": _text(
                    segment_contract.get("project_visual_style_requirement"),
                    1200,
                ),
                "continuity_note": _text(
                    segment_contract.get("continuity_note"), 700
                ),
                "negative_prompt": _text(
                    segment_contract.get("negative_prompt"), 700
                ),
                "timeline": original_timeline,
                "pacing": _text(segment_contract.get("pacing"), 700),
                "camera_direction": _text(
                    segment_contract.get("camera_direction"), 700
                ),
                "requirements": list(requirement_contract or [])[:64],
                "reference_inventory": reference_inventory,
                "provider_transport": {
                    "provider_key": _text(provider_key, 64),
                    "preferred_language": _text(
                        provider_prompt_language, 16
                    ),
                    "max_characters": provider_character_limit,
                    "visual_action_character_budget": provider_action_budget,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\nIMMUTABLE DIALOGUE:\n"
        + json.dumps(immutable_dialogue, ensure_ascii=False, sort_keys=True)
        + "\nVISIBLE FAILURE EVIDENCE:\n"
        + json.dumps(failure_evidence, ensure_ascii=False, sort_keys=True)
        + "\nFLAGS:\n"
        + json.dumps(
            {
                "forbid_overlay_bands": bool(forbid_overlay_bands),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\nReturn keys: segment_goal, provider_visual_context_en, "
        "provider_visual_context_zh, provider_direction_en, "
        "provider_direction_zh, timeline, pacing, camera_direction, "
        "provider_instruction, keep_reference_aliases, reference_assessments, "
        "reference_rationale, and rationale. keep_reference_aliases must be an "
        "ordered subset of the supplied reference aliases; preserve the clean "
        "authoritative product anchor when product identity appears. "
        "reference_assessments must contain one row per supplied alias with alias, "
        "usable_as_anchor, anchor_duty, visible_defects, and reason. A generated "
        "action still with visible anatomy or object defects must have "
        "usable_as_anchor=false and must not appear in keep_reference_aliases. "
        "timeline must contain 1-3 ordered "
        "objects with start_seconds, end_seconds, action, camera, dialogue_key, "
        "provider_action_en, and provider_action_zh. It must cover exactly 0 "
        "through the supplied duration "
        "without overlaps or gaps. Each action must describe a visibly testable "
        "state change that a single short generation can perform. Do not include "
        "dialogue text in the timeline; reference immutable line_id values only. "
        "Each provider_direction language must use Arabic numerals to state the "
        "segment's model-authored shot count and hold/cut interval in seconds, and "
        "must name the ordered framing/camera progression. This is execution data, "
        "not optional prose. A generic pace adjective alone is invalid. Each "
        "provider_direction language must be no more than 320 characters. "
        "Across the selected-language provider_visual_context plus all matching "
        "provider_action values, use at most "
        f"{provider_action_budget} characters; each "
        "value must be a complete instruction and must not end mid-clause."
    )
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
    for position, path in enumerate(list(reference_image_paths or [])[:10], 1):
        candidate = Path(str(path))
        if not candidate.is_file():
            continue
        alias = (
            reference_inventory[position - 1].get("alias")
            if position <= len(reference_inventory)
            else f"@image{position}"
        )
        user_content.extend([
            {
                "type": "text",
                "text": (
                    f"REFERENCE PIXELS {alias} "
                    "(inspect as a candidate narrow anchor; reject it when "
                    "defective, unnecessary, or in conflict with the text timeline):"
                ),
            },
            {
                "type": "image_url",
                "image_url": {
                    # The execution Director needs inspectable composition and
                    # identity pixels, not lossless generation files. Sending
                    # up to ten original PNGs produced multi-megabyte requests
                    # that exhausted every otherwise healthy multimodal route.
                    # Use the same bounded high-detail review proxy as the
                    # creative reviewer; authoritative source files remain
                    # untouched for downstream image/video generation.
                    "url": _creative_review_data_url(str(candidate)),
                    "detail": "high",
                },
            },
        ])
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": user_content if len(user_content) > 1 else user,
            },
        ],
        "temperature": 0.2,
        "max_tokens": 1400,
        "response_format": {"type": "json_object"},
    }
    digest = hashlib.sha256(
        (
            f"{SEGMENT_EXECUTION_REPLAN_POLICY_VERSION}|"
            f"{'initial' if initial_multimodal_authoring else 'recovery'}|"
            f"{execution_id}|"
            + json.dumps(
                {
                    "contract": segment_contract,
                    "failure": failure_evidence,
                    "dialogue": immutable_dialogue,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ).encode("utf-8")
    ).hexdigest()[:28]
    result: dict[str, Any] | None = None
    semantic_errors: list[str] = []
    request_prefix = (
        f"cf-execution-author:{digest}"
        if initial_multimodal_authoring
        else f"cf-execution-replan:{digest}"
    )
    for semantic_attempt in range(3):
        attempt_payload = payload
        if semantic_attempt:
            prior_error = semantic_errors[-1] if semantic_errors else "invalid execution data"
            correction = {
                "type": "text",
                "text": (
                    "CORRECTION: the prior JSON was rejected as non-executable: "
                    f"{prior_error}. Rewrite the complete JSON. Both "
                    "provider_direction_en and provider_direction_zh must use "
                    "Arabic numerals for shot count and hold/cut seconds, then "
                    "name the ordered framing/camera progression. Preserve the "
                    "signed pace; do not substitute generic words such as "
                    "dynamic, cinematic, brisk, fast cuts, or three beats. Keep "
                    "each provider_direction at no more than 320 characters. "
                    f"Keep the selected-language visual context plus provider "
                    f"actions within {provider_action_budget} characters. Return "
                    f"1-3 contiguous timeline rows covering exactly 0-{duration:g} "
                    "seconds."
                ),
            }
            prior_user_content = payload["messages"][1]["content"]
            corrected_content = (
                [*prior_user_content, correction]
                if isinstance(prior_user_content, list)
                else [
                    {"type": "text", "text": str(prior_user_content)},
                    correction,
                ]
            )
            attempt_payload = {
                **payload,
                "messages": [
                    payload["messages"][0],
                    {"role": "user", "content": corrected_content},
                ],
            }
        body = _routed_multimodal_completion(
            db,
            payload=attempt_payload,
            request_id=f"{request_prefix}:s{semantic_attempt + 1}",
            logical_model=(
                (
                    os.getenv("HERMES_CONTENT_VISUAL_INSPECTOR_MODEL")
                    or os.getenv("HERMES_PRODUCT_COMPOSITE_MODEL")
                )
                if len(user_content) > 1
                else (
                    os.getenv("HERMES_CONTENT_DIRECTOR_ROUTING_MODEL")
                    or "gmv-content-director-v1"
                )
            ),
            workload=(
                (
                    os.getenv("HERMES_CONTENT_VISUAL_INSPECTOR_WORKLOAD")
                    or os.getenv("HERMES_PRODUCT_COMPOSITE_WORKLOAD")
                    or "content_visual_inspector"
                )
                if len(user_content) > 1
                else (
                    os.getenv("HERMES_CONTENT_DIRECTOR_ROUTING_WORKLOAD")
                    or "default"
                )
            ),
            capability="multimodal",
            source=(
                "content_segment_execution_author"
                if initial_multimodal_authoring
                else "content_segment_execution_replan"
            ),
            error_prefix="SEGMENT_EXECUTION_REPLAN",
        )
        try:
            candidate = _json_object_from_text(
                extract_output_text(body).strip(),
                error_prefix="Segment execution replan",
            )
        except (ContentFactoryApiError, ValueError) as exc:
            semantic_errors.append(str(exc)[:240])
            continue
        direction_en = str(candidate.get("provider_direction_en") or "")
        direction_zh = str(candidate.get("provider_direction_zh") or "")
        candidate_errors: list[str] = []
        # The replan model owns the meaning of pace, shot progression and
        # whether one continuous take is creatively appropriate.  Do not scan
        # its natural-language directions with a fixed English/Chinese keyword
        # vocabulary.  Deterministic validation below remains limited to
        # non-empty transport fields, configured byte budgets, signed timeline
        # arithmetic and reference identities.
        if not direction_en.strip() or not direction_zh.strip():
            candidate_errors.append(
                "provider directions must be non-empty in both configured "
                "transport languages"
            )
        if len(re.sub(r"\s+", " ", direction_en).strip()) > 320 or len(
            re.sub(r"\s+", " ", direction_zh).strip()
        ) > 320:
            candidate_errors.append("provider direction exceeded 320 characters")
        candidate_rows = list(candidate.get("timeline") or [])
        if not 1 <= len(candidate_rows) <= 3 or not all(
            isinstance(item, dict) for item in candidate_rows
        ):
            candidate_errors.append("timeline must contain 1-3 object rows")
        else:
            selected_language = str(provider_prompt_language or "en").lower()
            selected_context = str(
                candidate.get(
                    "provider_visual_context_zh"
                    if selected_language.startswith("zh")
                    else "provider_visual_context_en"
                )
                or ""
            )
            selected_key = (
                "provider_action_zh"
                if selected_language.startswith("zh")
                else "provider_action_en"
            )
            selected_size = len(selected_context) + sum(
                len(str(item.get(selected_key) or ""))
                for item in candidate_rows
            )
            if selected_size > provider_action_budget:
                candidate_errors.append(
                    "selected-language visual context and provider actions "
                    f"used {selected_size} characters, above the "
                    f"{provider_action_budget}-character transport budget"
                )
            invalid_dialogue_keys = [
                _text(item.get("dialogue_key"), 160).strip()
                for item in candidate_rows
                if not dialogue_key_is_valid(item.get("dialogue_key"))
            ]
            if invalid_dialogue_keys:
                candidate_errors.append(
                    "timeline dialogue_key must be empty or contain only "
                    "immutable line_id values separated by commas; invalid values: "
                    + ", ".join(invalid_dialogue_keys[:3])
                    + "; allowed line_ids: "
                    + (", ".join(sorted(valid_line_ids)) or "none")
                )
        if not candidate_errors:
            result = candidate
            break
        semantic_errors.append("; ".join(candidate_errors))
    if result is None:
        raise ContentFactoryApiError(
            "SEGMENT_EXECUTION_REPLAN_DIRECTION_RETRY_EXHAUSTED: "
            + "; ".join(semantic_errors[-3:])
        )
    provider_visual_context_en = re.sub(
        r"\s+", " ", str(result.get("provider_visual_context_en") or "")
    ).strip()
    provider_visual_context_zh = re.sub(
        r"\s+", " ", str(result.get("provider_visual_context_zh") or "")
    ).strip()
    provider_direction_en = re.sub(
        r"\s+", " ", str(result.get("provider_direction_en") or "")
    ).strip()
    provider_direction_zh = re.sub(
        r"\s+", " ", str(result.get("provider_direction_zh") or "")
    ).strip()
    rows = list(result.get("timeline") or [])
    if not 1 <= len(rows) <= 3 or not all(isinstance(item, dict) for item in rows):
        raise ContentFactoryApiError(
            "SEGMENT_EXECUTION_REPLAN_TIMELINE_INVALID"
        )
    normalized: list[dict[str, Any]] = []
    prior_end = 0.0
    for index, item in enumerate(rows):
        try:
            start = float(item.get("start_seconds"))
            end = float(item.get("end_seconds"))
        except (TypeError, ValueError) as exc:
            raise ContentFactoryApiError(
                "SEGMENT_EXECUTION_REPLAN_TIMING_INVALID"
            ) from exc
        if abs(start - prior_end) > 0.15 or end <= start or end > duration + 0.15:
            raise ContentFactoryApiError(
                "SEGMENT_EXECUTION_REPLAN_TIMING_INVALID"
            )
        action = _text(item.get("action"), 1000).strip()
        camera = _text(item.get("camera"), 600).strip()
        provider_action_en = re.sub(
            r"\s+", " ", str(item.get("provider_action_en") or "")
        ).strip()
        provider_action_zh = re.sub(
            r"\s+", " ", str(item.get("provider_action_zh") or "")
        ).strip()
        if index == 0:
            # Motion is the provider-facing authority; the inspected pixels
            # already anchor cast and scene appearance.  Putting visual
            # context before the opening action made Doubao's 495-character
            # transport compactor spend the first beat on a static room
            # description and silently discard the model-authored hook.  Keep
            # the action/transition first and append the visual context only
            # as supporting continuity.
            if provider_visual_context_en:
                provider_action_en = (
                    provider_action_en.rstrip(".;；。")
                    + "; Context: "
                    + provider_visual_context_en
                )
            if provider_visual_context_zh:
                provider_action_zh = (
                    provider_action_zh.rstrip(".;；。")
                    + "；场景："
                    + provider_visual_context_zh
                )
        dialogue_key = normalize_dialogue_key(item.get("dialogue_key"))
        if (
            not action
            or not camera
            or not provider_action_en
            or not provider_action_zh
        ):
            raise ContentFactoryApiError(
                "SEGMENT_EXECUTION_REPLAN_BEAT_INVALID"
            )
        # The provider owns one total prompt budget, not an equal quota for
        # every timeline row.  A decisive opening beat may legitimately need
        # more characters than a short closing beat.  The old per-row 120-char
        # Chinese ceiling rejected otherwise complete plans even when their
        # combined transport text was safely within the provider budget. Keep
        # the non-selected translation bounded against pathological output;
        # enforce the real selected-language budget after all rows are known.
        selected_language = str(provider_prompt_language or "en").lower()
        if (
            selected_language.startswith("zh")
            and len(provider_action_en) > 600
        ) or (
            not selected_language.startswith("zh")
            and len(provider_action_zh) > 600
        ):
            raise ContentFactoryApiError(
                "SEGMENT_EXECUTION_REPLAN_PROVIDER_BEAT_TOO_LONG"
            )
        if not dialogue_key_is_valid(dialogue_key):
            raise ContentFactoryApiError(
                "SEGMENT_EXECUTION_REPLAN_DIALOGUE_KEY_INVALID"
            )
        normalized.append({
            "start_second": round(start, 3),
            "end_second": round(end, 3),
            "action": action,
            "camera": camera,
            "provider_action_en": provider_action_en,
            "provider_action_zh": provider_action_zh,
            "dialogue_key": dialogue_key,
        })
        prior_end = end
    if abs(prior_end - duration) > 0.15:
        raise ContentFactoryApiError(
            "SEGMENT_EXECUTION_REPLAN_DURATION_INVALID"
        )
    selected_language = str(provider_prompt_language or "en").lower()
    selected_action_key = (
        "provider_action_zh"
        if selected_language.startswith("zh")
        else "provider_action_en"
    )
    if sum(
        len(str(row.get(selected_action_key) or ""))
        for row in normalized
    ) > provider_action_budget:
        raise ContentFactoryApiError(
            "SEGMENT_EXECUTION_REPLAN_PROVIDER_BEATS_TOO_LONG"
        )
    selected_direction = (
        provider_direction_zh
        if selected_language.startswith("zh")
        else provider_direction_en
    )
    if not selected_direction:
        # Keep the task recoverable when an otherwise valid multimodal result
        # omits the new compact transport field. These values are still the
        # model-authored pacing, camera and visual-context outputs; this is a
        # lossless serialization fallback, not a creative template.
        selected_direction = " | ".join(
            value
            for value in (
                _text(result.get("pacing"), 220),
                _text(result.get("camera_direction"), 220),
                (
                    provider_visual_context_zh
                    if selected_language.startswith("zh")
                    else provider_visual_context_en
                ),
            )
            if str(value or "").strip()
        )
        if selected_language.startswith("zh"):
            provider_direction_zh = selected_direction
        else:
            provider_direction_en = selected_direction
    if not selected_direction:
        raise ContentFactoryApiError(
            "SEGMENT_EXECUTION_REPLAN_PROVIDER_DIRECTION_MISSING"
        )
    if len(selected_direction) > 320:
        raise ContentFactoryApiError(
            "SEGMENT_EXECUTION_REPLAN_PROVIDER_DIRECTION_TOO_LONG"
        )
    available_aliases = [
        str(item.get("alias") or "").strip()
        for item in reference_inventory
        if str(item.get("alias") or "").strip()
    ]
    requested_aliases = [
        str(value or "").strip()
        for value in list(result.get("keep_reference_aliases") or [])[:10]
        if str(value or "").strip() in available_aliases
    ]
    reference_assessments: list[dict[str, Any]] = []
    unusable_aliases: set[str] = set()
    raw_assessments = {
        str(item.get("alias") or "").strip(): item
        for item in list(result.get("reference_assessments") or [])[:10]
        if isinstance(item, dict)
        and str(item.get("alias") or "").strip() in available_aliases
    }
    for alias in available_aliases:
        raw = dict(raw_assessments.get(alias) or {})
        usable = bool(raw.get("usable_as_anchor", True))
        if not usable:
            unusable_aliases.add(alias)
        reference_assessments.append({
            "alias": alias,
            "usable_as_anchor": usable,
            "anchor_duty": _text(raw.get("anchor_duty"), 120),
            "visible_defects": [
                _text(value, 160)
                for value in list(raw.get("visible_defects") or [])[:8]
                if str(value or "").strip()
            ],
            "reason": _text(raw.get("reason"), 300),
        })
    requested_aliases = [
        alias for alias in requested_aliases if alias not in unusable_aliases
    ]
    # The model owns pixel-level usability. The transport layer owns lossless
    # role delivery: a clean recurring character or current-scene anchor must
    # not disappear merely because the model omitted its alias while authoring
    # motion. Action anchors remain optional because text owns choreography.
    selected_roles = {
        role
        for item in reference_inventory
        if str(item.get("alias") or "").strip() in requested_aliases
        for role in list(item.get("semantic_roles") or [])
    }
    for required_role in ("character_anchor", "scene_anchor"):
        if required_role in selected_roles:
            continue
        candidate_alias = next(
            (
                str(item.get("alias") or "").strip()
                for item in reference_inventory
                if required_role in list(item.get("semantic_roles") or [])
                and str(item.get("alias") or "").strip()
                not in unusable_aliases
            ),
            "",
        )
        if candidate_alias and candidate_alias not in requested_aliases:
            requested_aliases.append(candidate_alias)
            selected_roles.add(required_role)
    # Product media is conditional, not a transport invariant. The
    # multimodal Director sees both this segment's timeline and the actual
    # reference pixels, so it owns whether the package belongs in this
    # segment. Re-attaching every available package here used to turn an
    # explicitly product-free opening into product-conditioned generation.
    requested_aliases = [
        alias for alias in available_aliases if alias in requested_aliases
    ]
    return {
        "policy_version": SEGMENT_EXECUTION_REPLAN_POLICY_VERSION,
        "segment_index": int(segment_contract.get("segment_index") or 0),
        "duration_seconds": duration,
        "segment_goal": _text(
            result.get("segment_goal") or segment_contract.get("segment_goal"),
            700,
        ),
        "timeline": normalized,
        "pacing": _text(result.get("pacing"), 700),
        "camera_direction": _text(result.get("camera_direction"), 700),
        "provider_instruction": _text(
            result.get("provider_instruction"), 900
        ),
        "provider_visual_context_en": _text(
            provider_visual_context_en, 600
        ),
        "provider_visual_context_zh": _text(
            provider_visual_context_zh, 600
        ),
        "provider_direction_en": _text(provider_direction_en, 320),
        "provider_direction_zh": _text(provider_direction_zh, 320),
        "keep_reference_aliases": requested_aliases,
        "reference_assessments": reference_assessments,
        "reference_rationale": _text(
            result.get("reference_rationale"), 700
        ),
        "rationale": _text(result.get("rationale"), 700),
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
    if stage == "CREATIVE_REVIEW":
        # The acceptance envelope owns one evidence-rich JSON object per
        # reference. A fixed 1200-token cap truncates ordinary 6-12 image
        # reviews mid-array, which then looks like a reviewer contract error
        # and wastes semantic retries. Scale only this bounded inspection
        # response; the strict parser below still rejects missing rows.
        reference_count = len(_reference_plan(packet))
        max_tokens = max(
            1600,
            min(5200, 600 + (360 * max(1, reference_count))),
        )
    content: str | list[dict[str, Any]] = user
    if stage in {"FACTS", "CREATIVE_REVIEW"}:
        content = [{"type": "text", "text": user}]
        manifests = list(packet.get("browser_assets") or [])
        paths = list(packet.get("browser_asset_paths") or [])
        selected_items: list[tuple[dict[str, Any], str]] = []
        has_native_visual_previews = any(
            str(
                (item if isinstance(item, dict) else {}).get("role")
                or (item if isinstance(item, dict) else {}).get("asset_role")
                or ""
            ).lower()
            == "visual_preview"
            for item in manifests
        )
        for manifest, path in zip(manifests, paths):
            item = manifest if isinstance(manifest, dict) else {}
            kind = str(item.get("kind") or "").lower()
            role = str(item.get("role") or item.get("asset_role") or "").lower()
            mime_type = str(item.get("mime_type") or "").lower()
            if not (mime_type.startswith("image/") or Path(str(path)).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}):
                continue
            if stage == "CREATIVE_REVIEW" and kind not in {"visual_preview", "preview_canvas", "source"} and role not in {"visual_preview", "preview_canvas", "product_visual"}:
                continue
            if stage == "CREATIVE_REVIEW" and (
                kind == "benchmark_keyframes" or role == "benchmark_keyframes"
            ):
                continue
            # A preview canvas is only a convenience view over the native
            # generated references. Sending both makes the vision model count
            # the same generation twice. Prefer the individually indexed
            # references whenever they exist.
            if (
                stage == "CREATIVE_REVIEW"
                and has_native_visual_previews
                and role == "preview_canvas"
            ):
                continue
            selected_items.append((item, str(path)))
        for item, path in selected_items[:12]:
            if stage == "CREATIVE_REVIEW":
                role = str(
                    item.get("role") or item.get("asset_role") or item.get("kind") or "source"
                ).lower()
                reference_index = int(item.get("reference_index") or 0)
                if role == "visual_preview":
                    label = (
                        "IMAGE ROLE: generated visual_preview; "
                        f"REFERENCE INDEX: {reference_index or 'unknown'}; "
                        "count this as exactly one generated reference and emit "
                        "exactly one reference_checks row for this index."
                    )
                elif role == "product_visual":
                    label = (
                        "IMAGE ROLE: authoritative product_visual; comparison-only "
                        "package authority; do not count it as a generated reference "
                        "and do not emit a reference_checks row for it."
                    )
                else:
                    label = (
                        f"IMAGE ROLE: {role}; context-only evidence; do not count it "
                        "as a generated reference unless the role is visual_preview."
                    )
                content.append({"type": "text", "text": label})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": (
                            _creative_review_data_url(str(path))
                            if stage == "CREATIVE_REVIEW"
                            else _data_url(str(path))
                        ),
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
    logical_model = ""
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
        route_meta = dict(body.get("_gmv_route") or {})
        provider_name = str(route_meta.get("provider_key") or "ai_routing")
        prefer_alternate_model = False
    elif db is not None:
        # All production text stages use the shared model router.  Keeping a
        # provider-specific POST here made FACTS and EDIT_PACKAGE bypass
        # priorities, health circuits and the self-hosted Sub2API primary
        # route even though the same logical models were already registered.
        prefer_alternate_model = bool(
            packet.get("text_api_prefer_alternate_model", False)
        )
        alternate_model = (
            "gpt-5.6-luna"
            if preferred_model != "gpt-5.6-luna"
            else "gpt-5.6-terra"
        )
        logical_model = alternate_model if prefer_alternate_model else preferred_model
        # FACTS and EDIT_PACKAGE may have a text-only packet, but they still
        # require the same multimodal-capable content model class used by the
        # rest of the pipeline. Do not silently downgrade semantic authority
        # because one stage has no image attachment.
        capability = "multimodal"
        request_digest = hashlib.sha256(
            (
                f"{packet.get('execution_id') or ''}|{stage}|"
                f"{int(packet.get('api_regeneration_generation') or 0)}|"
                f"{logical_model}"
            ).encode("utf-8")
        ).hexdigest()[:28]
        try:
            body = asyncio.run(
                call_chat_with_failover(
                    db,
                    logical_model_id=logical_model,
                    messages=list(payload["messages"]),
                    capability=capability,
                    workload="default",
                    request_id=f"cf-{stage.lower()}:{request_digest}",
                    payload_overrides={
                        "temperature": payload["temperature"],
                        "max_tokens": payload["max_tokens"],
                        "response_format": payload["response_format"],
                    },
                    metadata={
                        "source": f"content_{stage.lower()}",
                        "workload": "default",
                    },
                    timeout_seconds=240,
                    max_routes=4,
                )
            )
        except AiGatewayError as exc:
            raise ContentFactoryApiError(
                f"AI routing {stage} failed: {str(exc.error_class or 'UPSTREAM')}"
            ) from exc
        try:
            text = str(body["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ContentFactoryApiError(
                f"AI routing {stage} returned an invalid response"
            ) from exc
        if not text:
            raise ContentFactoryApiError(
                f"AI routing {stage} returned an empty response"
            )
        route_meta = dict(body.get("_gmv_route") or {})
        provider_name = str(route_meta.get("provider_key") or "ai_routing")
        used_model = str(
            route_meta.get("provider_model_id")
            or body.get("model")
            or logical_model
        )
    else:
        # Isolated unit tests may call this helper without a database session.
        # Preserve their provider-level fixture path; every production caller
        # supplies a real Session and therefore uses the routed branch above.
        key = get_effective_key(
            db,
            provider_key=TOAPIS_PROVIDER_KEY,
            require_active=True,
        )
        token = decrypt_api_key(key.api_key_ciphertext)
        provider_name = TOAPIS_PROVIDER_KEY
        text = ""
        body = {}
        logical_model = ""
        prefer_alternate_model = bool(
            packet.get("text_api_prefer_alternate_model", False)
        )
    routed_completion = stage == "CREATIVE_REVIEW" or db is not None
    base_url = str(os.getenv("TOAPIS_API_BASE_URL") or "https://toapis.com/v1").rstrip("/")
    headers = (
        {}
        if routed_completion
        else {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    )
    alternate_model = "gpt-5.6-luna" if preferred_model != "gpt-5.6-luna" else "gpt-5.6-terra"
    prefer_alternate_model = bool(prefer_alternate_model)
    model_order = (
        (alternate_model, preferred_model)
        if prefer_alternate_model
        else (preferred_model, alternate_model)
    )
    errors: list[str] = []
    used_model = used_model if routed_completion else preferred_model
    execution_key = str(packet.get("execution_id") or "").strip()
    stage_key = str(stage or "").strip().upper()
    try:
        regeneration_generation = max(0, int(packet.get("api_regeneration_generation") or 0))
    except (TypeError, ValueError):
        regeneration_generation = 0
    for attempt_index, model in enumerate(
        () if routed_completion else dict.fromkeys(model_order)
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
            f"routed {stage}"
            if routed_completion
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
            logical_model if routed_completion else None
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
        # A signed Production Plan already owns the product-visibility truth
        # for every reference.  Do not run its prose back through the legacy
        # keyword detector: product-free directions such as "before product
        # identity appears" or "do not show MYUPONA" contain product words
        # and previously caused the authoritative package image to be attached
        # to exactly the frame where it was forbidden.  Legacy plans still
        # need text inference because they do not carry the signed boolean.
        requires_product_reference = bool(
            product_allowed
            and (
                bool(item.get("requires_product_reference"))
                if signed_production_plan
                else visual_reference_requires_product(description)
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


def _benchmark_hook_transfer_contract(
    packet: dict[str, Any],
) -> dict[str, Any]:
    requirement = " ".join(
        str(packet.get(key) or "")
        for key in ("project_requirements", "user_instruction")
    )
    if not re.search(
        r"\bhook\b|opening.{0,20}(?:impact|attention|visual)|first\s*[23]\s*seconds?|"
        r"前\s*[23]\s*秒|视觉钩子|开头.{0,12}(?:冲击|抓|吸引|钩子)",
        requirement,
        flags=re.IGNORECASE,
    ):
        return {}
    analysis = dict(packet.get("benchmark_video_analysis") or {})
    semantic = dict(analysis.get("visual_semantic_analysis") or {})
    opening = dict(semantic.get("opening_hook") or {})
    states = [
        dict(item)
        for item in list(opening.get("ordered_states") or [])
        if isinstance(item, dict)
    ]
    if semantic.get("status") != "success" or not states:
        return {}
    intent_authority = _json_dict(packet.get("producer_intent_authority"))
    signed_requirements = [
        _json_dict(item)
        for item in list(intent_authority.get("requirements") or [])
        if isinstance(item, dict)
    ]
    reference_requirements = [
        item
        for item in signed_requirements
        if str(item.get("kind") or "").strip().lower()
        == "reference_transfer"
    ]
    # New Producer contracts must explicitly authorize benchmark transfer.
    # The fallback keeps isolated legacy packets and historical tests readable
    # while production projects use their signed manifest.
    if intent_authority and not reference_requirements:
        return {}
    return {
        "multimodal_opening_evidence": {
            "ordered_states": states[:8],
            "attention_mechanisms": list(
                opening.get("attention_mechanisms") or []
            )[:16],
            "contrast_and_escalation": _text(
                opening.get("contrast_and_escalation"), 1600
            ),
            "benchmark_suggestions": list(
                semantic.get("must_transfer") or []
            )[:16],
            "must_not_copy": list(
                semantic.get("must_not_copy") or []
            )[:16],
        },
        "signed_reference_transfer_requirements": reference_requirements[:16],
        "signed_transformation_contract": _json_dict(
            intent_authority.get("transformation_contract")
        ),
        "authority_rule": (
            "Only signed reference-transfer requirements are mandatory; "
            "benchmark suggestions are evidence and cannot override signed "
            "exclusions or prohibitions. The multimodal reviewer judges the "
            "smallest sufficient visual proof without a fixed still count."
        ),
    }


def _opening_local_overlay_texts(packet: dict[str, Any]) -> list[str]:
    """Return signed opening copy that the local renderer owns."""
    media_design = _json_dict(
        _json_dict(packet.get("previous_outputs")).get("MEDIA_DESIGN")
    )
    delivery = _json_dict(media_design.get("copy_delivery"))
    deliveries = [
        _json_dict(item)
        for item in list(delivery.get("deliveries") or [])
        if isinstance(item, dict)
    ]
    try:
        first_segment_end = float(
            list(packet.get("video_segment_durations_seconds") or [0])[0]
            or 0
        )
    except (TypeError, ValueError, IndexError):
        first_segment_end = 0.0
    line_ids: set[str] = set()
    for item in deliveries:
        if str(item.get("method") or "").strip().lower() != "local_overlay":
            continue
        line_id = str(item.get("line_id") or "").strip()
        if not line_id:
            continue
        try:
            start_seconds = float(item.get("start_seconds") or 0)
        except (TypeError, ValueError):
            # A malformed copy-delivery row must not crash visual review. It
            # also must not gain authority to waive a pixel requirement.
            continue
        if first_segment_end <= 0 or start_seconds < first_segment_end:
            line_ids.add(line_id)
    if not line_ids:
        return []

    texts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            line_id = str(value.get("line_id") or "").strip()
            if line_id in line_ids:
                for key in ("line", "text", "copy", "content", "display_text"):
                    text_value = _text(value.get(key), 500)
                    if text_value:
                        texts.append(text_value)
                        break
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(media_design.get("complete_video_script"))
    return list(dict.fromkeys(texts))


def _hook_mechanism_owned_by_local_overlay(
    value: Any,
    *,
    overlay_texts: list[str],
) -> bool:
    """Classify only copy facts that the signed local-overlay lane supplies."""
    mechanism = _text(value, 800).lower()
    if not mechanism or not overlay_texts:
        return False
    overlay = " ".join(overlay_texts).lower()
    asks_for_time_copy = bool(re.search(
        r"\b(?:readable|visible|display|exact|conspicuous)?\s*"
        r"(?:abnormal\s+)?(?:late[- ]night\s+)?time(?:\s+marker|\s+copy|\s+cue)?\b|"
        r"\bclock\s*(?:copy|text|marker|reading)?\b|时间(?:标记|文字|文案)|凌晨.{0,8}(?:文字|时间)",
        mechanism,
        flags=re.IGNORECASE,
    ))
    overlay_has_time = bool(re.search(
        r"\b(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:a\.?m\.?|p\.?m\.?)?\b|"
        r"\b(?:a\.?m\.?|p\.?m\.?)\b|凌晨|深夜",
        overlay,
        flags=re.IGNORECASE,
    ))
    asks_for_count_copy = bool(re.search(
        r"\b(?:readable|visible|display|exact|quantified|numeric|numbered)?\s*"
        r"(?:scroll(?:ing)?\s+)?(?:count|consequence)\b|"
        r"\b\d+\s+(?:videos?|scrolls?)\b|"
        r"(?:数量|量化|数字).{0,10}(?:后果|刷屏|视频)",
        mechanism,
        flags=re.IGNORECASE,
    ))
    overlay_has_count = bool(re.search(
        r"\b\d+\s+(?:videos?|scrolls?|posts?|clips?)\b|"
        r"\b(?:videos?|scrolls?|posts?|clips?)\s*[:=]?\s*\d+\b|"
        r"\d+\s*(?:个视频|次刷屏)",
        overlay,
        flags=re.IGNORECASE,
    ))
    asks_for_readable_copy = bool(re.search(
        r"\b(?:readable|legible|exact)\s+(?:copy|text|words?|letters?|numerals?|caption|overlay)\b|"
        r"(?:可读|清晰).{0,8}(?:文字|文案|字幕|数字)",
        mechanism,
        flags=re.IGNORECASE,
    ))
    return bool(
        (asks_for_time_copy and overlay_has_time)
        or (asks_for_count_copy and overlay_has_count)
        or asks_for_readable_copy
    )


def _apply_creative_review_reference_gate(
    envelope: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, Any]:
    """Validate the multimodal review contract without re-judging its meaning.

    Creative acceptance belongs to the multimodal reviewer. The server only
    verifies that every signed reference received an indexed, typed review and
    that the review's structured verdicts agree with its top-level decision.
    It never scans prose for creative keywords or rewrites a model rejection
    into approval.
    """
    normalized = dict(envelope or {})
    result = _json_dict(normalized.get("result"))
    plan = _reference_plan(packet)
    raw_checks = result.get("reference_checks")
    checks = list(raw_checks) if isinstance(raw_checks, list) else []
    model_approved = bool(result.get("approved_for_split"))
    explicit_model_rejection = (
        "approved_for_split" in result
        and result.get("approved_for_split") is False
        and bool(_text(result.get("creative_review"), 4000))
        and bool(
            _text(
                result.get("repair_brief") or normalized.get("repair_brief"),
                4000,
            )
        )
    )
    # A model-authored rejection is a safe terminal review decision even when
    # the provider omitted the optional per-reference explanation array.  The
    # workflow will redraw the complete board, so retrying the same multimodal
    # call merely to obtain five redundant JSON rows cannot improve safety or
    # quality.  Approval remains strict: every signed reference must still
    # have complete pixel-grounded evidence before it can reach FINAL_ASSETS.
    rejected_review_without_checks = bool(
        explicit_model_rejection and not checks
    )
    expected_indices = [
        int(item.get("index") or position)
        for position, item in enumerate(plan, 1)
    ]
    by_index: dict[int, dict[str, Any]] = {}
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

    if len(checks) != len(plan) and not rejected_review_without_checks:
        structural_failures.append(
            f"reference check count is {len(checks)} but the plan requires {len(plan)}"
        )
    if (
        sorted(by_index) != sorted(expected_indices)
        and not rejected_review_without_checks
    ):
        structural_failures.append(
            "reference check indices do not exactly match the ordered plan"
        )

    verdict_fields = (
        "character_scene_verdict",
        "terminal_action_verdict",
        "continuity_verdict",
        "emotional_beat_verdict",
        "placement_surface_verdict",
    )
    allowed_verdicts = {"match", "mismatch", "uncertain", "not_required"}
    model_failures: list[str] = []
    canonical_checks: list[dict[str, Any]] = []

    for position, item in enumerate(plan, 1):
        index = int(item.get("index") or position)
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
        for field in verdict_fields:
            verdict = str(row.get(field) or "").strip().lower()
            if verdict not in allowed_verdicts:
                structural_failures.append(
                    f"reference {index} {field} has invalid verdict {verdict or '<empty>'}"
                )
                continue
            if verdict in {"mismatch", "uncertain"}:
                model_failures.append(
                    f"reference {index} {field} is {verdict}"
                )
        missing = row.get("missing_or_wrong_facts")
        if isinstance(missing, list) and any(
            _text(value, 500) for value in missing
        ):
            model_failures.append(
                f"reference {index} reports missing or wrong visible facts"
            )
        if bool(item.get("requires_product_reference")):
            placement_verdict = str(
                row.get("placement_surface_verdict") or ""
            ).strip().lower()
            if placement_verdict == "not_required":
                structural_failures.append(
                    f"reference {index} required product placement cannot be not_required"
                )
            elif placement_verdict != "match":
                model_failures.append(
                    f"reference {index} required product placement is not matched"
                )
        canonical_checks.append(row)

    benchmark_hook_contract = _benchmark_hook_transfer_contract(packet)
    benchmark_hook_transfer = _json_dict(result.get("benchmark_hook_transfer"))
    if benchmark_hook_contract and not rejected_review_without_checks:
        if not benchmark_hook_transfer:
            structural_failures.append(
                "benchmark_hook_transfer is required by the signed benchmark contract"
            )
        elif str(benchmark_hook_transfer.get("status") or "").strip().lower() != "pass":
            model_failures.append(
                "multimodal reviewer rejected benchmark hook transfer"
            )

    if structural_failures:
        concise = "; ".join(dict.fromkeys(structural_failures))[:1600]
        raise ContentFactoryApiError(
            "CREATIVE_REVIEW structured multimodal contract incomplete: " + concise
        )

    if model_approved and model_failures:
        concise = "; ".join(dict.fromkeys(model_failures))[:1600]
        raise ContentFactoryApiError(
            "CREATIVE_REVIEW multimodal decision is internally inconsistent: " + concise
        )
    # A complete board can have a model-observed composition, pacing, hook or
    # storytelling defect even when every individual reference row matches its
    # narrow character/action facts.  Do not turn those per-reference fields
    # into a server-side creative gate.  When the multimodal reviewer supplies
    # an explicit top-level rejection plus a repair brief, preserve that
    # decision and let the next visual turn execute it.  Mechanical schema,
    # index and type validation above remains strict; creative meaning belongs
    # to the multimodal reviewer.

    result["reference_checks"] = canonical_checks
    result["reference_image_count"] = len(canonical_checks)
    result["approved_for_split"] = model_approved
    repair_strategy_schema_recovered = False
    if model_approved:
        result["repair_brief"] = None
        result.pop("repair_strategy", None)
        normalized["repair_brief"] = None
    else:
        repair_brief = _text(
            result.get("repair_brief") or normalized.get("repair_brief"),
            4000,
        )
        if not repair_brief:
            raise ContentFactoryApiError(
                "CREATIVE_REVIEW rejected the board without a multimodal repair_brief"
            )
        result["repair_brief"] = repair_brief
        normalized["repair_brief"] = repair_brief
        # The multimodal model remains the sole repair decision-maker.  Some
        # otherwise valid provider responses place the requested strategy at
        # the strict envelope level instead of under ``result``.  Accept that
        # model-authored object and canonicalize its location; do not spend a
        # second model call merely to move unchanged JSON fields.
        strategy = _json_dict(
            result.get("repair_strategy") or normalized.get("repair_strategy")
        )
        mode = str(strategy.get("mode") or "").strip().lower()

        def _strategy_indices(key: str) -> list[int]:
            values: list[int] = []
            for value in list(strategy.get(key) or []):
                try:
                    index = int(value)
                except (TypeError, ValueError):
                    continue
                if index not in values:
                    values.append(index)
            return values

        repair_indices = _strategy_indices("reference_indices")
        anchor_indices = _strategy_indices("continuity_anchor_indices")
        expected_set = set(expected_indices)
        failed_set = {
            int(row.get("index") or 0)
            for row in canonical_checks
            if any(
                str(row.get(field) or "").strip().lower()
                not in {"match", "not_required"}
                for field in verdict_fields
            )
            or any(
                _text(value, 500)
                for value in list(row.get("missing_or_wrong_facts") or [])
            )
        }
        # A rejected review is already a model-authored creative decision.
        # If the provider omits only the execution routing fields, preserve
        # that rejection and its repair brief, then choose the lossless
        # whole-board execution path.  This is schema recovery, not a visual
        # re-judgment: no failed pixels are approved and no repair wording is
        # invented.  Repeatedly asking another model to restate the same
        # rejection wastes tokens and can leave the project in an endless
        # output-contract cooldown.
        if not mode:
            repair_strategy_schema_recovered = True
            mode = "regenerate_full_board"
            repair_indices = list(expected_indices)
            anchor_indices = []
            strategy["reason"] = repair_brief
        elif mode == "regenerate_full_board":
            # ``regenerate_full_board`` already expresses the model's complete
            # execution decision.  Providers occasionally omit the redundant
            # index list (or leave stale continuity anchors) even though the
            # mode has only one lossless interpretation.  Canonicalize that
            # transport/schema detail instead of spending another multimodal
            # call asking the model to enumerate the signed plan.  This does
            # not change the creative verdict or approve any pixels.
            if set(repair_indices) != expected_set or anchor_indices:
                repair_strategy_schema_recovered = True
                repair_indices = list(expected_indices)
                anchor_indices = []
            if not _text(strategy.get("reason"), 2000):
                repair_strategy_schema_recovered = True
                strategy["reason"] = repair_brief
        if mode not in {"regenerate_full_board", "regenerate_references"}:
            structural_failures.append(
                "rejected review requires repair_strategy.mode"
            )
        elif not repair_indices or not set(repair_indices).issubset(expected_set):
            structural_failures.append(
                "repair_strategy.reference_indices must identify valid planned references"
            )
        elif not failed_set.issubset(set(repair_indices)):
            structural_failures.append(
                "repair_strategy omits a reference rejected by the multimodal review"
            )
        if not set(anchor_indices).issubset(
            expected_set - set(repair_indices)
        ):
            structural_failures.append(
                "repair_strategy continuity anchors must be accepted non-repair references"
            )
        if mode == "regenerate_full_board":
            if set(repair_indices) != expected_set or anchor_indices:
                structural_failures.append(
                    "full-board repair must redraw every reference without predecessor anchors"
                )
        elif mode == "regenerate_references" and len(expected_set) > 1:
            has_character_anchor = any(
                str(_json_dict(asset).get("role") or "").strip().lower()
                == "character_reference"
                for asset in list(packet.get("browser_assets") or [])
                if isinstance(asset, dict)
            )
            if not anchor_indices and not has_character_anchor:
                structural_failures.append(
                    "partial repair requires a real accepted continuity anchor or uploaded character anchor"
                )
        reason = _text(strategy.get("reason"), 2000)
        if not reason:
            structural_failures.append("repair_strategy.reason is required")
        result["repair_strategy"] = {
            "mode": mode,
            "reference_indices": repair_indices,
            "continuity_anchor_indices": anchor_indices,
            "reason": reason,
        }

    if structural_failures:
        concise = "; ".join(dict.fromkeys(structural_failures))[:1600]
        raise ContentFactoryApiError(
            "CREATIVE_REVIEW structured multimodal contract incomplete: " + concise
        )

    # Rejection is a valid completed review decision. The workflow router, not
    # the stage execution status, decides whether to redraw or split.
    normalized["status"] = "PASS"
    normalized["next_stage"] = (
        "FINAL_ASSETS" if model_approved else "VISUAL_PREVIEW"
    )
    evidence = _json_dict(normalized.get("evidence"))
    evidence.update(
        {
            "creative_authority": "multimodal_visual_reviewer",
            "deterministic_authority": (
                "schema_index_type_and_decision_consistency_only"
            ),
            "multimodal_reference_check_count": len(canonical_checks),
            "multimodal_model_approved": model_approved,
            "pixel_grounded_reference_gate_passed": model_approved,
            "pixel_grounded_reference_check_count": len(canonical_checks),
            "multimodal_repair_mode": (
                _json_dict(result.get("repair_strategy")).get("mode")
                if not model_approved
                else None
            ),
            "repair_strategy_schema_recovered": (
                repair_strategy_schema_recovered
            ),
            "rejected_review_without_reference_checks_recovered": (
                rejected_review_without_checks
            ),
        }
    )
    if not model_approved:
        evidence["pixel_grounded_reference_gate_failures"] = list(
            dict.fromkeys(model_failures)
        )
    normalized["evidence"] = evidence

    allowed_result_fields = {
        "creative_review",
        "approved_for_split",
        "reference_image_count",
        "repair_brief",
        "reference_checks",
        "benchmark_hook_transfer",
        "repair_strategy",
    }
    dropped = sorted(set(result) - allowed_result_fields)
    normalized["result"] = {
        key: value
        for key, value in result.items()
        if key in allowed_result_fields
    }
    if dropped:
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
        # Sparse repairs are not necessarily contiguous (for example
        # references 1,2,4,5).  Keep the exact signed row mapping so the local
        # splitter never relabels panel 3 as reference 3.
        "global_reference_indices": [
            int(item.get("index") or position)
            for position, item in enumerate(plan, 1)
        ],
    }


def _visual_repair_storyboard_groups(
    plan: list[dict[str, Any]],
    repair_indices: set[int],
) -> list[list[dict[str, Any]]]:
    """Keep failed frames from one signed storyboard on one repair canvas.

    A sparse repair used to turn every rejected panel into an unrelated native
    image request.  That discarded the very cast/location/lighting lock for
    which the Director requested a shared board, and could change a woman into
    a man between adjacent hook states.  Honour an explicit group id when the
    plan provides one and, for already-saved plans, recover the same intent
    from cross-referenced ``ref.*`` ids in a shared-storyboard description.

    Only failed rows enter a repair group.  Passed rows remain immutable and
    are never regenerated merely because they shared the original canvas.
    """
    failed_rows = [
        dict(item)
        for item in plan
        if int(item.get("index") or 0) in repair_indices
    ]
    if not failed_rows:
        return []

    id_to_index = {
        str(item.get("reference_id") or "").strip(): int(item.get("index") or 0)
        for item in plan
        if str(item.get("reference_id") or "").strip()
        and int(item.get("index") or 0) > 0
    }
    grouped_indices: list[set[int]] = []
    explicit_groups: dict[str, set[int]] = {}
    for item in failed_rows:
        index = int(item.get("index") or 0)
        explicit_group = str(
            item.get("storyboard_group_id")
            or item.get("board_group_id")
            or item.get("shared_board_id")
            or ""
        ).strip()
        if explicit_group:
            explicit_groups.setdefault(explicit_group, set()).add(index)

        description = " ".join(
            str(item.get(key) or "")
            for key in ("description", "purpose", "single_frame_terminal_state")
        )
        if not re.search(
            r"\b(?:shared|same|single)\b[^.\n]{0,80}\b(?:storyboard|board|canvas)\b|"
            r"\bpanel\s*\d+\b[^.\n]{0,100}\b(?:storyboard|board|canvas)\b",
            description,
            flags=re.IGNORECASE,
        ):
            continue
        related = {index}
        lowered = description.lower()
        for reference_id, related_index in id_to_index.items():
            if related_index in repair_indices and reference_id.lower() in lowered:
                related.add(related_index)
        if len(related) > 1:
            grouped_indices.append(related)
    grouped_indices.extend(
        indices for indices in explicit_groups.values() if len(indices) > 1
    )

    # Merge overlapping declarations.  A plan may describe the same shared
    # board from every panel, producing {1,2,3}, {1,2}, and {1,3} candidates.
    merged: list[set[int]] = []
    for candidate in grouped_indices:
        overlaps = [group for group in merged if group & candidate]
        if not overlaps:
            merged.append(set(candidate))
            continue
        combined = set(candidate)
        for group in overlaps:
            combined.update(group)
            merged.remove(group)
        merged.append(combined)

    covered = set().union(*merged) if merged else set()
    merged.extend({index} for index in sorted(repair_indices - covered))
    by_index = {int(item.get("index") or 0): item for item in failed_rows}
    return [
        [by_index[index] for index in sorted(indices) if index in by_index]
        for indices in sorted(merged, key=lambda values: min(values))
    ]


def visual_board_specs(packet: dict[str, Any], *, max_panels_per_board: int = 7) -> list[dict[str, Any]]:
    plan = _reference_plan(packet)
    aspect_ratio = _packet_aspect_ratio(packet)
    if not plan:
        plan = [{"index": 1, "segment": "reference 1", "description": "Required continuity reference", "roles": ["action_anchor"]}]
    repair_indices = {
        int(value)
        for value in list(packet.get("visual_repair_failed_indices") or [])
        if str(value).strip().isdigit() and int(value) > 0
    }
    plan_indices = {
        int(item.get("index") or 0)
        for item in plan
        if int(item.get("index") or 0) > 0
    }
    full_board_repair = bool(
        repair_indices
        and plan_indices
        and plan_indices.issubset(repair_indices)
        and not bool(packet.get("render_reference_images_individually"))
    )
    if repair_indices and not full_board_repair:
        # A pixel-grounded repair remains sparse, but failed panels that belong
        # to one signed storyboard must remain on one canvas.  This retains the
        # continuity/token benefit of board generation while preserving every
        # accepted reference as immutable authority.
        repair_groups = _visual_repair_storyboard_groups(plan, repair_indices)
        # Directed Production Plans compiled before storyboard_group_id was
        # introduced still describe one signed reference program.  Preserve
        # that original board relationship rather than degrading a repair to
        # one paid request per failed image.
        if (
            len(repair_groups) > 1
            and len(repair_indices) > 1
            and str(
                dict(
                    dict(packet.get("previous_outputs") or {}).get(
                        "MEDIA_DESIGN"
                    )
                    or {}
                ).get("visual_job_ticket", {}).get("source")
                or ""
            ).strip().lower() == "directed_production_plan"
        ):
            by_index = {
                int(item.get("index") or 0): dict(item)
                for item in plan
                if int(item.get("index") or 0) in repair_indices
            }
            repair_groups = [[
                by_index[index]
                for index in sorted(repair_indices)
                if index in by_index
            ]]
        if repair_groups:
            board_count = len(plan)
            return [
                _single_visual_board_spec(
                    group,
                    board_index=int(group[0].get("index") or 0),
                    board_count=board_count,
                    aspect_ratio=aspect_ratio,
                )
                for group in repair_groups
            ]
    # When every row failed, sparse repair has no accepted visual authority to
    # preserve.  Fall through to the ordinary board path so the complete cast,
    # scene, hook progression, and product transition are redrawn together.
    # This is both cheaper and materially more consistent than N unrelated
    # native renders.
    #
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
    # A full storyboard repair contains one pixel-grounded clause per failed
    # panel.  Truncating the set-level brief to 700 characters silently kept
    # only the first one or two clauses, so later hook/product panels were
    # redrawn without their reviewer evidence.  Individual-image repair still
    # narrows this text below via _single_reference_repair_instruction().
    repair = _text(packet.get("visual_repair_instruction") or "", 4000)
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
    video_policy = _json_dict(packet.get("video_model_policy"))
    face_reference_mode = str(
        video_policy.get("human_face_reference_mode") or "allowed"
    ).strip().lower()
    illustration_requested = bool(re.search(
        r"\b(?:2d|2\.5d|animated|animation|illustrat(?:ed|ion)|cartoon|graphic novel)\b|"
        r"动画|插画|卡通|美式动画",
        medium_context,
        flags=re.IGNORECASE,
    )) or face_reference_mode == "stylized_animation_only"
    medium_rule = (
        "MANDATORY PROVIDER-SAFE VISUAL MEDIUM: unmistakably fictional adult 2D/2.5D/3D animation. Human faces are allowed, "
        "but they must use visibly stylized drawn or rendered facial planes, skin, eyes, hair, expressions, lighting, and surfaces. "
        "This is not live action, not a synthetic photograph, and not a real-person portrait. Do not use photorealistic or hyperreal "
        "skin texture, pores, eyes, hair strands, photographic bokeh, or photographic lighting. Preserve the planned adult character, "
        "emotion, scene, action, hook, and continuity. "
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
    panel_descriptions: list[str] = []
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
        panel_descriptions.append(description)
        frame_label = "ONE STATIC ILLUSTRATED IMAGE" if illustration_requested else "ONE STATIC STILL IMAGE"
        panel_lines.append(
            f"{index}. {frame_label}: {description}"
            + (f" [{roles}]" if roles else "")
        )
    storyboard_scene_text = "\n".join(panel_descriptions)
    requires_diegetic_time_cue = bool(re.search(
        r"\b(?:clock(?:\s+face)?|digital\s+time\s+display|visible\s+time\s+cue)\b|"
        r"时钟|钟面|数字时间|可见时间线索",
        storyboard_scene_text,
        flags=re.IGNORECASE,
    ))
    diegetic_time_rule = (
        " A story-required bedside clock or diegetic late-night time cue is allowed and must be visibly unmistakable; "
        "it is part of the photographed/illustrated room, not a caption, overlay, app UI, or panel label. "
        if requires_diegetic_time_cue
        else ""
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
        + diegetic_time_rule
        + (
            " No editorial text, captions, app UI, watermark, overlays, or picture-in-picture. "
            if single_frame
            else " No panel labels, editorial text, captions, nested frames, app UI, watermark, or picture-in-picture. "
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
    "replan_failed_segment_execution_api",
    "review_provider_rendered_product_video_api",
    "review_provider_rendered_segment_execution_api",
    "review_spoken_copy_semantics_api",
    "review_composed_intent_fidelity_api",
    "review_final_intent_repair_scope_api",
    "review_final_intent_repair_targets_api",
    "minimal_stage_context",
    "visual_reference_description",
    "visual_reference_static_state",
    "visual_generation_reference_paths",
    "visual_board_spec",
    "visual_board_specs",
    "visual_reference_requires_product",
]
