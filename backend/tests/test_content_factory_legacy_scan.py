from __future__ import annotations

from pathlib import Path
import re

from scripts.check_content_factory_legacy_scan import TARGET_DIRS as GUARD_TARGET_DIRS


ROOT = Path(__file__).resolve().parents[1]

TARGET_DIRS = [
    ROOT / "app" / "services" / "hermes_agent",
    ROOT / "app" / "features" / "tenants" / "hermes_agent",
    ROOT / "app" / "tasks" / "hermes_agent",
]

CHECK_EXTENSIONS = {".py", ".md"}


FORBIDDEN = [
    re.compile(r"approveContentFactory"),
    re.compile(r"ContentFactoryApproval"),
    re.compile(r"\bapply_approval\b"),
    re.compile(r"CONTENT_APPROVAL_NOT_EXPECTED"),
    re.compile(r"\bVIDEO_QA\b"),
    re.compile(r"\bWAITING_VISUAL_APPROVAL\b"),
    re.compile(r"\bWAITING_VIDEO_APPROVAL\b"),
    re.compile(r"/content-factory/projects/.*/approve"),
    re.compile(r"requires approval", re.IGNORECASE),
    re.compile(r"target_stage\s*=\s*[\"']CREATIVE[\"']"),
    re.compile(r"return\s+[\"']CREATIVE[\"']"),
    re.compile(r"current_stage\s*=\s*[\"']CREATIVE[\"']"),
    re.compile(r"previous_outputs(?:\.get\(|\[)[\"']CREATIVE[\"']"),
]


def test_legacy_guard_scans_real_backend_and_frontend_roots() -> None:
    expected = {
        (ROOT / "app" / "services" / "hermes_agent").resolve(),
        (ROOT / "app" / "features" / "tenants" / "hermes_agent").resolve(),
        (ROOT / "app" / "tasks" / "hermes_agent").resolve(),
        (ROOT.parent / "gmv-frontend" / "src" / "features" / "tenants" / "hermes_agent").resolve(),
    }
    actual = {path.resolve() for path in GUARD_TARGET_DIRS}

    assert actual == expected
    assert all(path.is_dir() for path in actual)


def test_no_legacy_approval_symbols_in_content_factory_scope() -> None:
    hits: list[str] = []

    for root in TARGET_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in CHECK_EXTENSIONS:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            rel = path.relative_to(ROOT)
            for index, line in enumerate(text.splitlines(), start=1):
                for pattern in FORBIDDEN:
                    if pattern.search(line):
                        hits.append(f"{rel}:{index}:{line.strip()}")

    assert not hits, "Legacy approval symbols were found in content-factory scope:\n" + "\n".join(hits)


def test_content_factory_frontend_has_no_approval_api_call() -> None:
    hits: list[str] = []
    path = ROOT / ".." / "gmv-frontend" / "src" / "features" / "tenants" / "hermes_agent" / "api.js"
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for index, line in enumerate(text.splitlines(), start=1):
            if "approve" in line.lower() and "content-factory" in line.lower():
                hits.append(f"{path.relative_to(ROOT)}:{index}:{line.strip()}")

    assert not hits, "Hermes content factory API should not include approval entry:\n" + "\n".join(hits)
