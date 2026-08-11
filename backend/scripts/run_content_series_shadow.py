#!/usr/bin/env python3
"""Run a resumable, zero-media whole-series Director/Critic canary."""

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
from app.services.hermes_agent.content_director import DirectorSeriesBrief
from app.services.hermes_agent.content_director_runtime import DirectorLoopPolicy
from app.services.hermes_agent.content_series_runtime import (
    run_content_series_slate_loop,
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _json_object(json.loads(path.read_text(encoding="utf-8")))


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
        brief = DirectorSeriesBrief.model_validate(
            config.get("director_series_brief")
        )
        if args.target_count is not None:
            payload = brief.model_dump(mode="json")
            target_count = max(
                1,
                min(1000, int(args.target_count)),
            )
            payload["series_id"] = (
                f"{brief.series_id}.shadow-{target_count}"
            )[:128]
            payload["target_count"] = target_count
            for requirement in payload["diversity_requirements"]:
                requirement["minimum_unique_values"] = min(
                    target_count,
                    int(requirement["minimum_unique_values"]),
                )
            brief = DirectorSeriesBrief.model_validate(payload)
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

    checkpoint = _load_checkpoint(args.checkpoint)

    def save_checkpoint(value: dict[str, Any] | None) -> None:
        if value is None:
            args.checkpoint.unlink(missing_ok=True)
            return
        _atomic_private_json(args.checkpoint, dict(value))

    result = await run_content_series_slate_loop(
        brief=brief,
        policy=policy,
        resume_page_checkpoint=checkpoint,
        page_checkpoint_callback=save_checkpoint,
    )
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_id": args.project_id,
        "media_authorized": False,
        "database_mutated": False,
        "brief": brief.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--current-profile",
        action="store_true",
        help=(
            "Compile the latest universal profile in memory without "
            "committing the project."
        ),
    )
    parser.add_argument("--target-count", type=int)
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
    result = payload["result"]
    print(json.dumps({
        "status": result["status"],
        "reason": result["reason"],
        "intent_count": len(
            _json_object(result.get("final_slate")).get("intents") or []
        ),
        "attempt_count": len(result.get("attempts") or []),
        "coverage_review_count": len(
            result.get("coverage_reviews") or []
        ),
        "page_review_count": len(
            result.get("page_reviews") or []
        ),
        "review_count": len(result.get("reviews") or []),
        "checkpoint_exists": args.checkpoint.exists(),
        "output": str(args.output),
        "media_authorized": False,
    }))
    return 0 if result["status"] == "approved" else 2


if __name__ == "__main__":
    raise SystemExit(main())
