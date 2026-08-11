from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from app.services.hermes_agent.client import (
    HermesVisualPromptRepairClient,
    extract_output_text,
)


class VisualPromptRepair(BaseModel):
    """One bounded, auditable rewrite of a provider-rejected image prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repaired_prompt: str = Field(min_length=80, max_length=2400)
    diagnosis: str = Field(min_length=1, max_length=600)
    change_summary: str = Field(min_length=1, max_length=600)
    evidence_used: list[str] = Field(default_factory=list, max_length=10)


def _response_meta(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("_gmv_meta") if isinstance(payload, dict) else None
    return dict(value) if isinstance(value, dict) else {}


def _image_data_url(path_value: str) -> str | None:
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
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _repair_input_items(
    packet_json: str,
    reference_paths: list[str] | None,
) -> list[dict[str, Any]] | None:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": packet_json}]
    for index, value in enumerate(list(reference_paths or [])[:8], 1):
        data_url = _image_data_url(value)
        if not data_url:
            continue
        content.append({
            "type": "input_text",
            "text": f"Authoritative visual reference {index}; inspect pixels before repairing prompt.",
        })
        content.append({"type": "input_image", "image_url": data_url, "detail": "high"})
    return [{"role": "user", "content": content}] if len(content) > 1 else None


async def repair_rejected_visual_prompt(
    *,
    current_prompt: str,
    provider_error: str,
    project_key: str,
    stage_id: int,
    reference_index: int,
    provider_key: str,
    visual_model: str,
    reference_paths: list[str] | None = None,
    client: Any | None = None,
) -> tuple[VisualPromptRepair, dict[str, Any]]:
    """Ask an isolated Hermes role to simplify one failed visual request.

    The model may only change rendering language.  Story intent, product
    presence/absence, character identity, medium, aspect ratio, and the one-
    image boundary remain immutable.  The original image task is already
    terminal, so this returns a new prompt identity for one fresh paid task.
    """

    source_prompt = str(current_prompt or "").strip()
    if not source_prompt:
        raise ValueError("visual prompt repair requires a source prompt")
    source_digest = hashlib.sha256(source_prompt.encode("utf-8")).hexdigest()
    request_packet = {
        "operation": "repair_provider_rejected_image_prompt",
        "immutable_contract": {
            "project_key": str(project_key),
            "stage_id": int(stage_id),
            "reference_index": int(reference_index),
            "provider": str(provider_key),
            "model": str(visual_model),
            "source_prompt_sha256": source_digest,
            "preserve_story_and_scene": True,
            "preserve_product_presence_or_absence": True,
            "preserve_character_and_location_identity": True,
            "preserve_medium_aspect_ratio_and_single_image_boundary": True,
            "do_not_evade_safety_policy": True,
        },
        "provider_error": str(provider_error or "")[:1000],
        "source_prompt": source_prompt[:5000],
    }
    repair_client = client or HermesVisualPromptRepairClient()
    packet_json = json.dumps(
        request_packet,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    response, latency_ms = await repair_client.create_response(
        input_text=packet_json,
        input_items=_repair_input_items(packet_json, reference_paths),
        instructions=(
            "Act only as visual_prompt_repair. The image provider rejected one "
            "terminal render and asked for a revised prompt. Diagnose ambiguity, "
            "contradiction, excessive repetition, accidental multi-frame language, "
            "or needless policy-sensitive wording, then rewrite the prompt so it is "
            "concise and directly renderable. Preserve the exact story beat, final "
            "static state, product presence or absence, authoritative references, "
            "adult cast identity, location, requested visual medium, aspect ratio, "
            "and single-image boundary. Never add claims, offers, logos, readable "
            "text, people, products, or actions. Do not weaken or evade a safety "
            "rule. Return exactly one raw JSON object with only repaired_prompt, "
            "diagnosis, change_summary, and evidence_used; no markdown."
        ),
        metadata={
            "project_key": str(project_key),
            "stage_id": int(stage_id),
            "reference_index": int(reference_index),
            "provider": str(provider_key),
            "visual_model": str(visual_model),
            "source_prompt_sha256": source_digest,
            "operation": "visual_prompt_repair",
        },
        idempotency_key=(
            "gmv-visual-prompt-repair-"
            + hashlib.sha256(
                (
                    f"{project_key}:{stage_id}:{reference_index}:"
                    f"{provider_key}:{visual_model}:{source_digest}:"
                    f"{str(provider_error or '')[:1000]}"
                ).encode("utf-8")
            ).hexdigest()
        ),
    )
    raw = extract_output_text(response).strip()
    if not raw or raw.startswith("```") or raw.endswith("```"):
        raise ValueError("visual prompt repair response must be raw JSON")
    try:
        parsed = VisualPromptRepair.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("visual prompt repair response is not valid contract JSON") from exc
    repaired = parsed.repaired_prompt.strip()
    if hashlib.sha256(repaired.encode("utf-8")).hexdigest() == source_digest:
        raise ValueError("visual prompt repair returned the unchanged prompt")
    meta = _response_meta(response)
    meta.update({
        "latency_ms": int(meta.get("latency_ms") or latency_ms or 0),
        "source_prompt_sha256": source_digest,
        "repaired_prompt_sha256": hashlib.sha256(
            repaired.encode("utf-8")
        ).hexdigest(),
    })
    return parsed.model_copy(update={"repaired_prompt": repaired}), meta


__all__ = ["VisualPromptRepair", "repair_rejected_visual_prompt"]
