from __future__ import annotations

import ast
import sys
from pathlib import Path


def _iter_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _positional_args(args: ast.arguments):
    positional = list(args.posonlyargs) + list(args.args)
    defaults = list(args.defaults)
    num_with_defaults = len(defaults)
    default_start = len(positional) - num_with_defaults
    result = []
    for index, param in enumerate(positional):
        has_default = index >= default_start
        default_node = None
        if has_default and defaults:
            default_node = defaults[index - default_start]
        result.append((param, has_default, default_node))
    return result


def _annotation_name(param: ast.arg) -> str | None:
    if param.annotation is None:
        return None
    try:
        return ast.unparse(param.annotation)
    except Exception:  # pragma: no cover - fallback for unsupported nodes
        return None


def _is_request_like(param: ast.arg) -> bool:
    annotation = _annotation_name(param)
    if not annotation:
        return False
    return annotation.split(".")[-1] == "Request"


def _is_response_like(param: ast.arg) -> bool:
    annotation = _annotation_name(param)
    if not annotation:
        return False
    return annotation.split(".")[-1] == "Response"


def check_function(path: Path, fn: ast.FunctionDef | ast.AsyncFunctionDef):
    args = fn.args
    positional = _positional_args(args)

    violations: list[str] = []

    # General Python rule: positional parameters without defaults must not appear after defaults.
    seen_default = False
    for param, has_default, _ in positional:
        if has_default:
            seen_default = True
        elif seen_default:
            violations.append(
                "positional parameter without default follows parameter with default"
            )
            break

    # FastAPI-specific rule for request/response ordering.
    for target_check, label in ((
        _is_request_like,
        "request",
    ), (
        _is_response_like,
        "response",
    )):
        index = None
        for idx, (param, _, _) in enumerate(positional):
            if target_check(param):
                index = idx
                break
        if index is None:
            continue
        # Any defaulted positional parameter before the request/response argument is invalid.
        for prior_idx in range(index):
            prior_param, has_default, _ = positional[prior_idx]
            if has_default:
                violations.append(
                    f"{label} parameter appears after parameter with default"
                )
                break

    if violations:
        loc = f"{path}:{fn.lineno}:{fn.name}"
        for reason in violations:
            print(f"{loc}: {reason}")

    return violations


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    target = repo_root / "backend" / "app"
    if not target.exists():
        print(f"Target directory not found: {target}", file=sys.stderr)
        return 1

    any_errors = False
    for file_path in sorted(target.rglob("*.py")):
        try:
            source = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        try:
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError as exc:  # pragma: no cover - propagate syntax errors
            print(f"{file_path}:{exc.lineno}: syntax error: {exc.msg}")
            any_errors = True
            continue

        for fn in _iter_functions(tree):
            if check_function(file_path, fn):
                any_errors = True

    return 1 if any_errors else 0


if __name__ == "__main__":
    sys.exit(main())

