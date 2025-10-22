import sys
import pathlib
import re

root = pathlib.Path(__file__).resolve().parents[1]
bad = []
for p in root.iterdir():
    name = p.name
    if p.is_dir() and (re.fullmatch(r'(cryptography|email_validator)(-|_)?[\w\.]*', name) or name.endswith('.dist-info')):
        bad.append(str(p))
if bad:
    print("Found vendored third-party directories:", *bad, sep="\n- ")
    sys.exit(1)
print("OK: no vendored dirs detected.")
