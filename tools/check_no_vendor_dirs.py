import sys
import pathlib
import re


def parse_requirement_names(requirements_path: pathlib.Path):
    names = set()
    if not requirements_path.exists():
        return names
    requirement_pattern = re.compile(r"^[A-Za-z0-9_.-]+")
    for raw_line in requirements_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = requirement_pattern.match(line)
        if not match:
            continue
        package_name = match.group(0)
        base_name = package_name.split("[")[0]
        module_name = base_name.replace("-", "_").lower()
        if module_name:
            names.add(module_name)
    return names

root = pathlib.Path(__file__).resolve().parents[1]
bad = []
for p in root.iterdir():
    name = p.name
    if p.is_dir() and (re.fullmatch(r'(cryptography|email_validator)(-|_)?[\w\.]*', name) or name.endswith('.dist-info')):
        bad.append(str(p))
backend_shadowing = []
backend_dir = root / "backend"
if backend_dir.is_dir():
    requirement_modules = parse_requirement_names(backend_dir / "requirements.txt")
    for module_name in sorted(requirement_modules):
        candidate = backend_dir / f"{module_name}.py"
        if candidate.exists():
            backend_shadowing.append(str(candidate.relative_to(root)))
if bad:
    print("Found vendored third-party directories:", *bad, sep="\n- ")
    sys.exit(1)
if backend_shadowing:
    print(
        "Found backend modules shadowing third-party packages:",
        *backend_shadowing,
        sep="\n- ",
    )
    sys.exit(1)
print("OK: no vendored dirs or shadowing modules detected.")
