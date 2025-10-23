import os
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parents[1]
vendored_pattern = re.compile(r'(cryptography|email_validator)(-|_)?[\w\.]*')
ignored_dir_names = {
    '.git',
    '.mypy_cache',
    '.pytest_cache',
    '.tox',
    '.venv',
    '__pycache__',
    'node_modules',
}
bad = []
for current_root, dirnames, _ in os.walk(root):
    current_path = pathlib.Path(current_root)
    pruned = []
    for dirname in dirnames:
        if dirname in ignored_dir_names:
            continue
        candidate = current_path / dirname
        if vendored_pattern.fullmatch(dirname) or dirname.endswith('.dist-info'):
            bad.append(str(candidate))
            continue
        pruned.append(dirname)
    dirnames[:] = pruned
if bad:
    print("Found vendored third-party directories:", *bad, sep="\n- ")
    sys.exit(1)
print("OK: no vendored dirs detected.")
