#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import resource
import sys
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services.hermes_agent.content_capabilities import (
    load_content_capability_manifest,
)
from app.services.hermes_agent.content_director import (
    DirectedContentArtifact,
)
from app.services.hermes_agent.content_director_profile import (
    load_universal_director_profile,
)
from app.services.hermes_agent.content_director_runtime import (
    DirectorLoopPolicy,
)
from app.services.hermes_agent.content_production_plan_runtime import (
    run_content_production_plan_loop,
)


def _artifact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        payload,
        payload.get("final_artifact"),
        dict(payload.get("result") or {}).get("final_artifact"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and {
            "artifact_id",
            "artifact_sha256",
            "program",
            "script",
        }.issubset(candidate):
            return candidate
    raise ValueError(
        "input JSON does not contain a DirectedContentArtifact"
    )


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input).resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    artifact = DirectedContentArtifact.model_validate(
        _artifact_payload(payload)
    )
    profile = load_universal_director_profile()
    manifest = load_content_capability_manifest()
    result = await run_content_production_plan_loop(
        artifact=artifact,
        plan_id=str(args.plan_id or f"{artifact.artifact_id}.production"),
        policy=DirectorLoopPolicy(
            maximum_revisions=int(args.maximum_revisions),
            maximum_contract_repairs_per_revision=int(
                args.maximum_contract_repairs
            ),
            series_page_size=10,
        ),
        review_criteria=list(profile.production_plan_review_criteria),
        capability_catalog=[
            item.model_dump(mode="json")
            for item in manifest.capabilities
            if item.capability in {
                "visual.plan",
                "audio.design",
                "copy.delivery.plan",
            }
        ],
        authorized_asset_refs=list(dict.fromkeys(
            str(value).strip()
            for value in args.authorized_asset_ref
            if str(value).strip()
        )),
        authoritative_product_asset_refs=list(dict.fromkeys(
            str(value).strip()
            for value in args.product_asset_ref
            if str(value).strip()
        )),
    )
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input": str(input_path),
        "artifact_sha256": artifact.artifact_sha256,
        "media_authorized": False,
        "database_mutated": False,
        "peak_rss_kb": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "policy": {
            "maximum_revisions": int(args.maximum_revisions),
            "maximum_contract_repairs": int(
                args.maximum_contract_repairs
            ),
        },
        "result": result.model_dump(mode="json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run Director and Critic production planning without media or DB writes."
        )
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--plan-id")
    parser.add_argument(
        "--authorized-asset-ref",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--product-asset-ref",
        action="append",
        default=[],
    )
    parser.add_argument("--maximum-revisions", type=int, default=2)
    parser.add_argument(
        "--maximum-contract-repairs",
        type=int,
        default=1,
    )
    args = parser.parse_args()
    output = asyncio.run(_run(args))
    output_path = Path(args.output).resolve()
    _atomic_private_json(output_path, output)
    print(json.dumps({
        "output": str(output_path),
        "status": output["result"]["status"],
        "reason": output["result"]["reason"],
        "media_authorized": False,
        "database_mutated": False,
        "attempts": len(output["result"]["attempts"]),
        "reviews": len(output["result"]["reviews"]),
        "peak_rss_kb": output["peak_rss_kb"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
