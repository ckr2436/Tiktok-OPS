#!/usr/bin/env python3
"""Run one zero-media Director/Critic copy canary from an existing slate.

This command is deliberately read-only with respect to the database.  It
loads the current project-owned brief, selects one intent from a shadow or
approved SeriesSlate file, and writes only a local audit JSON result.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.data.db import SessionLocal
from app.data.models.hermes_agent import HermesContentFactoryProject
from app.services.hermes_agent.content_director import (
    DirectorProjectBrief,
    DirectorSeriesBrief,
    SeriesSlateIntent,
)
from app.services.hermes_agent.content_director_runtime import (
    DirectorLoopPolicy,
    run_content_director_copy_loop,
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _load_intent(path: Path, variant_index: int) -> SeriesSlateIntent:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = _json_object(payload.get("result"))
    slate = _json_object(
        result.get("final_slate") or payload.get("final_slate")
    )
    for raw_intent in list(slate.get("intents") or []):
        intent = SeriesSlateIntent.model_validate(raw_intent)
        if intent.variant_index == variant_index:
            return intent
    raise ValueError(
        f"variant {variant_index} is missing from slate {path}"
    )


def _variant_brief(
    *,
    series_brief: DirectorSeriesBrief,
    intent: SeriesSlateIntent,
) -> DirectorProjectBrief:
    return DirectorProjectBrief(
        brief_id=(
            f"{series_brief.series_id}.variant-"
            f"{intent.variant_index:03d}.shadow"
        ),
        brief_version=series_brief.series_version,
        objective=intent.objective,
        content_type_hint=intent.content_type,
        platform=series_brief.platform,
        locale=series_brief.locale,
        audience=intent.audience,
        target_duration_seconds=intent.target_duration_seconds,
        edit_headroom_seconds=series_brief.edit_headroom_seconds,
        speech_rate_wpm=series_brief.speech_rate_wpm,
        display_reading_rate_wpm=(
            series_brief.display_reading_rate_wpm
        ),
        aspect_ratio=series_brief.aspect_ratio,
        production_contract=series_brief.production_contract,
        conversion=series_brief.conversion,
        truth_payload={
            **series_brief.truth_payload,
            "series_intent": {
                "series_id": series_brief.series_id,
                "series_version": series_brief.series_version,
                "intent_id": intent.intent_id,
                "variant_index": intent.variant_index,
                "creative_strategy": intent.creative_strategy,
                "differentiation": intent.differentiation,
                "pain_hypothesis": (
                    intent.pain_hypothesis.model_dump(mode="json")
                    if intent.pain_hypothesis is not None
                    else None
                ),
                "conversion_hypothesis": (
                    intent.conversion_hypothesis.model_dump(mode="json")
                    if intent.conversion_hypothesis is not None
                    else None
                ),
            },
        },
        creative_constraints=list(dict.fromkeys([
            *series_brief.creative_constraints,
            *intent.creative_constraints,
        ])),
        capability_catalog=series_brief.capability_catalog,
        copy_review_criteria=series_brief.copy_review_criteria,
        quality_rubric=series_brief.quality_rubric,
        source_truth_refs=list(dict.fromkeys([
            *series_brief.source_truth_refs,
            *intent.source_truth_refs,
        ])),
    )


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    with SessionLocal() as db:
        project = db.get(HermesContentFactoryProject, args.project_id)
        if project is None:
            raise ValueError(f"project {args.project_id} does not exist")
        config = _json_object(project.config_json)
        if args.current_profile:
            from app.services.hermes_agent.content_director_profile import (
                refresh_project_director_brief_from_facts,
            )

            state = _json_object(project.state_json)
            refresh_project_director_brief_from_facts(
                project,
                product_truth=_json_object(
                    state.get("product_knowledge")
                ),
            )
            config = _json_object(project.config_json)
        series_brief = DirectorSeriesBrief.model_validate(
            config.get("director_series_brief")
        )
        policy = DirectorLoopPolicy.model_validate(
            config.get("director_loop_policy")
        )
        if args.maximum_revisions is not None:
            policy_payload = policy.model_dump(mode="json")
            policy_payload["maximum_revisions"] = max(
                0,
                min(10, int(args.maximum_revisions)),
            )
            policy = DirectorLoopPolicy.model_validate(policy_payload)
        project_key = project.project_key

    intent = _load_intent(args.slate, args.variant)
    brief = _variant_brief(
        series_brief=series_brief,
        intent=intent,
    )
    result = await run_content_director_copy_loop(
        brief=brief,
        artifact_id=(
            f"{project_key}.variant-{args.variant:03d}."
            "zero-media-shadow"
        ),
        policy=policy,
    )
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_id": args.project_id,
        "variant_index": args.variant,
        "media_authorized": False,
        "source_slate": str(args.slate),
        "brief": brief.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--variant", type=int, required=True)
    parser.add_argument("--slate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--current-profile", action="store_true")
    parser.add_argument(
        "--maximum-revisions",
        type=int,
        help=(
            "Explicit zero-media canary override; does not mutate the "
            "project policy."
        ),
    )
    args = parser.parse_args()

    payload = asyncio.run(_run(args))
    _atomic_private_json(args.output, payload)
    print(json.dumps({
        "status": payload["result"]["status"],
        "reason": payload["result"]["reason"],
        "output": str(args.output),
        "media_authorized": False,
    }))
    return 0 if payload["result"]["status"] == "approved" else 2


if __name__ == "__main__":
    raise SystemExit(main())
