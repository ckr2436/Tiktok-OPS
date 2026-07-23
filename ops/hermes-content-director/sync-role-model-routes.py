#!/opt/gmv/python3.13/bin/python
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
ORIGINAL_CWD = Path.cwd()
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
os.chdir(BACKEND_ROOT)

from app.data.db import SessionLocal
from app.services.ai_routing.role_groups import sync_role_model_group


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--role",
        choices=("director", "critic", "visual_inspector"),
        required=True,
    )
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    policy_path = args.policy
    if not policy_path.is_absolute():
        policy_path = ORIGINAL_CWD / policy_path
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    db = SessionLocal()
    try:
        result = sync_role_model_group(db, role=args.role, policy=policy)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
