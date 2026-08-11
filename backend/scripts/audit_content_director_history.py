#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from app.data.db import SessionLocal
from app.data.models.hermes_agent import (
    HermesContentFactoryProject,
    HermesContentFactoryStage,
)
from app.services.hermes_agent.client import (
    HermesContentCriticClient,
    extract_output_text,
)
from app.services.hermes_agent.content_director import (
    DirectorCapabilityNode,
    DirectorProjectBrief,
    VideoProgramSpec,
    build_independent_copy_critic_packet,
    parse_independent_copy_critic_response,
    preflight_script_copy,
    script_package_from_creative_result,
)
from app.services.hermes_agent.content_director_profile import (
    compile_universal_director_series_brief,
)


_CRITIC_INSTRUCTIONS = (
    "Act only as independent_copy_critic. Score every review_criteria "
    "criterion_id and obey its project-owned threshold. Return exactly one "
    "raw JSON object with approved, scores, blocking_issues containing code, "
    "line_ids, evidence, and repair_instruction, plus repair_scope as a "
    "required top-level field. Match output_contract exactly. Do not use "
    "markdown and do not rewrite the script."
)


def _latest_creatives(
    db,
    *,
    project_id: int,
    maximum_variant: int,
) -> dict[int, HermesContentFactoryStage]:
    rows = (
        db.query(HermesContentFactoryStage)
        .filter(
            HermesContentFactoryStage.project_id == project_id,
            HermesContentFactoryStage.stage == "CREATIVE",
        )
        .order_by(HermesContentFactoryStage.id.asc())
        .all()
    )
    latest: dict[int, HermesContentFactoryStage] = {}
    for row in rows:
        output = dict(row.output_json or {})
        result = output.get("result")
        if not isinstance(result, dict):
            continue
        variant = int(
            output.get("content_factory_variant_index")
            or dict(row.input_json or {}).get("variant_index")
            or 0
        )
        if 1 <= variant <= maximum_variant:
            latest[variant] = row
    return latest


def _series_brief(project, *, target_count: int):
    config = dict(project.config_json or {})
    state = dict(project.state_json or {})
    publishing = dict(config.get("publishing_profile") or {})
    return compile_universal_director_series_brief(
        series_id=f"{project.project_key}.history-shadow",
        objective=str(
            config.get("content_objective") or project.title
        ),
        platform=str(
            publishing.get("platform") or "short-video"
        ),
        locale=str(config.get("video_language") or "en-US"),
        audience=str(
            config.get("target_audience")
            or (
                "Use only audience details explicitly supplied in project "
                "truth."
            )
        ),
        target_count=target_count,
        minimum_duration_seconds=float(
            config.get("video_duration_min_seconds") or 10
        ),
        maximum_duration_seconds=float(
            config.get("video_duration_max_seconds") or 10
        ),
        video_model=str(
            config.get("video_model") or "omni_flash"
        ),
        video_reference_limit=int(
            config.get("video_reference_limit") or 7
        ),
        allow_reference_video=bool(
            config.get("allow_reference_video", False)
        ),
        product_required=bool(
            config.get("product_required", True)
        ),
        brand_name=config.get("brand_name"),
        product_name=project.product_name,
        market=project.market,
        project_brief=project.product_brief,
        confirmed_claims=config.get("confirmed_claims"),
        confirmed_selling_points=config.get(
            "confirmed_selling_points"
        ),
        confirmed_promotions=config.get("confirmed_promotions"),
        promotion_cta=config.get("promotion_cta"),
        allow_promotional_cta=bool(
            config.get("allow_promotional_cta", True)
        ),
        creative_copy_contract=dict(
            config.get("creative_copy_contract") or {}
        ),
        creative_cast_policy=dict(
            config.get("creative_cast_policy") or {}
        ),
        product_presentation_policy=dict(
            config.get("product_presentation_policy") or {}
        ),
        product_truth=dict(state.get("product_knowledge") or {}),
        additional_creative_constraints=list(
            config.get("director_creative_constraints") or []
        ),
        additional_copy_review_criteria=list(
            config.get("director_copy_review_criteria") or []
        ),
        diversity_requirements_override=list(
            config.get("director_diversity_requirements") or []
        ),
    )


def _execution_graph(series_brief) -> list[DirectorCapabilityNode]:
    capabilities = {
        item.capability: item
        for item in series_brief.capability_catalog
    }
    copy = capabilities["copy.write"]
    review = capabilities["copy.review"]
    return [
        DirectorCapabilityNode(
            node_id="copy",
            capability=copy.capability,
            input_contract=copy.input_contract,
            output_contract=copy.output_contract,
            policy=copy.policy,
        ),
        DirectorCapabilityNode(
            node_id="review",
            capability=review.capability,
            depends_on=["copy"],
            input_contract=review.input_contract,
            output_contract=review.output_contract,
            policy=review.policy,
        ),
    ]


def _content_type(result: dict[str, Any]) -> str:
    concepts = list(result.get("concepts") or [])
    first = (
        dict(concepts[0])
        if concepts and isinstance(concepts[0], dict)
        else {}
    )
    return str(
        first.get("title") or "historic-shadow"
    )[:128]


def _build_variant(
    *,
    variant: int,
    stage: HermesContentFactoryStage,
    series_brief,
    execution_graph: list[DirectorCapabilityNode],
):
    result = dict(dict(stage.output_json or {}).get("result") or {})
    script_payload = dict(result.get("complete_video_script") or {})
    duration = float(
        script_payload.get("duration_seconds")
        or result.get("recommended_duration_seconds")
        or series_brief.default_duration_seconds
    )
    program = VideoProgramSpec(
        program_id=f"history-v{variant:03d}",
        objective=series_brief.objective,
        content_type=_content_type(result),
        platform=series_brief.platform,
        locale=series_brief.locale,
        audience=series_brief.audience,
        target_duration_seconds=duration,
        aspect_ratio=series_brief.aspect_ratio,
        creative_strategy={
            "shadow_source_stage_id": int(stage.id),
            "media_spend_authorized": False,
        },
        conversion=series_brief.conversion,
        execution_graph=execution_graph,
        copy_review_criteria=series_brief.copy_review_criteria,
        quality_rubric=series_brief.quality_rubric,
        source_truth_refs=series_brief.source_truth_refs,
    )
    script = script_package_from_creative_result(
        result,
        script_id=f"history-v{variant:03d}",
        program_id=program.program_id,
        locale=program.locale,
        edit_headroom_seconds=min(
            series_brief.edit_headroom_seconds,
            max(0.0, duration - 0.5),
        ),
    )
    brief = DirectorProjectBrief(
        brief_id=f"{series_brief.series_id}.v{variant:03d}",
        brief_version=series_brief.series_version,
        objective=program.objective,
        content_type_hint=program.content_type,
        platform=program.platform,
        locale=program.locale,
        audience=program.audience,
        target_duration_seconds=program.target_duration_seconds,
        edit_headroom_seconds=script.edit_headroom_seconds,
        speech_rate_wpm=script.speech_rate_wpm,
        aspect_ratio=program.aspect_ratio,
        conversion=program.conversion,
        truth_payload=series_brief.truth_payload,
        creative_constraints=series_brief.creative_constraints,
        capability_catalog=series_brief.capability_catalog,
        copy_review_criteria=series_brief.copy_review_criteria,
        quality_rubric=series_brief.quality_rubric,
        source_truth_refs=series_brief.source_truth_refs,
    )
    return brief, program, script


async def _semantic_review(
    *,
    project_id: int,
    variant: int,
    stage_id: int,
    brief,
    program,
    script,
    preflight,
) -> dict[str, Any]:
    packet = build_independent_copy_critic_packet(
        program,
        script,
        preflight,
        brief=brief,
    )
    response, latency_ms = await HermesContentCriticClient().create_response(
        input_text=json.dumps(
            packet,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        instructions=_CRITIC_INSTRUCTIONS,
        metadata={
            "agent_role": "content_critic",
            "operation": "history_shadow",
            "project_id": str(project_id),
            "variant_index": str(variant),
            "stage_id": str(stage_id),
            "prompt_version": "history-shadow-v3",
        },
        idempotency_key=(
            f"content-history-shadow-v3:{project_id}:{variant}:"
            f"{script.canonical_text_sha256}"
        ),
    )
    verdict = parse_independent_copy_critic_response(
        extract_output_text(response),
        packet=packet,
        script=script,
        preflight=preflight,
    )
    return {
        "latency_ms": latency_ms,
        "approved": verdict.approved,
        "scores": verdict.scores,
        "blocking_issues": [
            item.model_dump(mode="json")
            for item in verdict.blocking_issues
        ],
        "repair_scope": verdict.repair_scope,
    }


async def _run(args) -> dict[str, Any]:
    db = SessionLocal()
    try:
        project = (
            db.query(HermesContentFactoryProject)
            .filter(HermesContentFactoryProject.id == args.project_id)
            .one()
        )
        target = max(
            1,
            min(
                int(args.maximum_variant),
                int(
                    dict(project.config_json or {}).get("video_count")
                    or args.maximum_variant
                ),
            ),
        )
        series_brief = _series_brief(
            project,
            target_count=target,
        )
        graph = _execution_graph(series_brief)
        stages = _latest_creatives(
            db,
            project_id=project.id,
            maximum_variant=target,
        )
        rows: list[dict[str, Any]] = []
        deterministic_codes: Counter[str] = Counter()
        semantic_codes: Counter[str] = Counter()
        for variant in sorted(stages):
            stage = stages[variant]
            row: dict[str, Any] = {
                "variant_index": variant,
                "creative_stage_id": int(stage.id),
                "source_stage_status": stage.status,
            }
            try:
                brief, program, script = _build_variant(
                    variant=variant,
                    stage=stage,
                    series_brief=series_brief,
                    execution_graph=graph,
                )
                preflight = preflight_script_copy(program, script)
                codes = [item.code for item in preflight.issues]
                deterministic_codes.update(codes)
                row.update({
                    "script_sha256": script.canonical_text_sha256,
                    "spoken_word_count": preflight.spoken_word_count,
                    "spoken_budget_words": preflight.spoken_budget_words,
                    "deterministic_approved": preflight.approved,
                    "deterministic_issue_codes": codes,
                })
                if args.semantic:
                    try:
                        semantic = await _semantic_review(
                            project_id=int(project.id),
                            variant=variant,
                            stage_id=int(stage.id),
                            brief=brief,
                            program=program,
                            script=script,
                            preflight=preflight,
                        )
                        row["semantic"] = semantic
                        semantic_codes.update(
                            item["code"]
                            for item in semantic["blocking_issues"]
                        )
                    except Exception as exc:  # noqa: BLE001
                        row["semantic_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )[:2000]
            except Exception as exc:  # noqa: BLE001
                row["adapter_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )[:2000]
            rows.append(row)
    finally:
        db.close()
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_id": args.project_id,
        "maximum_variant": target,
        "semantic_enabled": bool(args.semantic),
        "found_variants": len(rows),
        "missing_variants": sorted(
            set(range(1, target + 1))
            - {row["variant_index"] for row in rows}
        ),
        "deterministic_issue_totals": dict(deterministic_codes),
        "semantic_issue_totals": dict(semantic_codes),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay completed creative outputs through the scene-free "
            "Director copy gates. This command never creates media."
        )
    )
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--maximum-variant", type=int, default=50)
    parser.add_argument(
        "--semantic",
        action="store_true",
        help="Also call the isolated content critic API.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
