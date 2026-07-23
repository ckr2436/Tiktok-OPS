from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


TARGET_DIRS = [
    ROOT / "backend" / "app" / "services" / "hermes_agent",
    ROOT / "backend" / "app" / "features" / "tenants" / "hermes_agent",
    ROOT / "backend" / "app" / "tasks" / "hermes_agent",
    ROOT / "gmv-frontend" / "src" / "features" / "tenants" / "hermes_agent",
]


CHECK_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".md"}


FORBIDDEN_PATTERNS = [
    ("approveContentFactory", re.compile(r"approveContentFactory")),
    ("ContentFactoryApproval", re.compile(r"ContentFactoryApproval")),
    ("apply_approval", re.compile(r"\bapply_approval\b")),
    ("CONTENT_APPROVAL_NOT_EXPECTED", re.compile(r"CONTENT_APPROVAL_NOT_EXPECTED")),
    ("VIDEO_QA", re.compile(r"\bVIDEO_QA\b")),
    ("WAITING_VISUAL_APPROVAL", re.compile(r"\bWAITING_VISUAL_APPROVAL\b")),
    ("WAITING_VIDEO_APPROVAL", re.compile(r"\bWAITING_VIDEO_APPROVAL\b")),
    (
        "legacy_content_factory_approve_route",
        re.compile(r"/content-factory/projects/.*/approve"),
    ),
    ("requires approval", re.compile(r"requires approval", re.IGNORECASE)),
    ("legacy_creative_target", re.compile(r"target_stage\s*=\s*[\"']CREATIVE[\"']")),
    ("legacy_creative_resume", re.compile(r"return\s+[\"']CREATIVE[\"']")),
    ("legacy_creative_current_stage", re.compile(r"current_stage\s*=\s*[\"']CREATIVE[\"']")),
    (
        "legacy_creative_transport_key",
        re.compile(r"previous_outputs(?:\.get\(|\[)[\"']CREATIVE[\"']"),
    ),
    ("legacy_reuse_source", re.compile(r"\breuse_source\b")),
    (
        "legacy_kie_ai_queue",
        re.compile(r"gmv\.tasks\.kie_ai|queue[_ -]?name\s*[:=]\s*[\"']kie_ai[\"']", re.IGNORECASE),
    ),
    (
        "legacy_content_skill_brand_lock",
        re.compile(r"myupona-content-factory", re.IGNORECASE),
    ),
    ("legacy_private_gpt", re.compile(r"private[_ -]?gpt", re.IGNORECASE)),
    (
        "hardcoded_provider_output_ratio",
        re.compile(r"Output:\s*9:16", re.IGNORECASE),
    ),
    (
        "hardcoded_portrait_repair_contract",
        re.compile(r"strict_portrait_grid|INDIVIDUAL PORTRAIT REFERENCE", re.IGNORECASE),
    ),
    (
        "hardcoded_product_or_campaign_copy",
        re.compile(
            r"sleep\s+ease|melatonin|gumm(?:y|ies)|\$?7\.99|insomnia|失眠|软糖",
            re.IGNORECASE,
        ),
    ),
]


def scan() -> tuple[int, list[str]]:
    hits = 0
    report: list[str] = []
    for root in TARGET_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in CHECK_EXTENSIONS:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = path.relative_to(ROOT)
            for name, pattern in FORBIDDEN_PATTERNS:
                for i, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        hits += 1
                        report.append(f"{rel}:{i}: [{name}] {line.strip()}")
    return hits, report


def main() -> int:
    missing = [path for path in TARGET_DIRS if not path.is_dir()]
    if missing:
        print("ERROR: content-factory legacy scan targets are missing:")
        for path in missing:
            print(path)
        return 2

    hits, report = scan()
    if hits:
        print(f"ERROR: found {hits} forbidden legacy symbols in content-factory scope:")
        for item in report:
            print(item)
        return 1

    print("OK: no forbidden legacy content-factory symbols detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
