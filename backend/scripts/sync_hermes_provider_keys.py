from __future__ import annotations

import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.data.db import SessionLocal  # noqa: E402
from app.services.ai_routing.role_groups import sync_role_policy_file  # noqa: E402


TRIGGER = Path("/run/gmv/hermes-provider-sync.request")
CONTENT_ROLE_POLICY = BACKEND_ROOT.parent / "ops/hermes-content-director/routing-policy.json"
PLATFORM_ROLE_POLICY = BACKEND_ROOT.parent / "ops/hermes-unified-routing/routing-policy.json"
ROLE_POLICIES = (CONTENT_ROLE_POLICY, PLATFORM_ROLE_POLICY)


def _sync_role_routes() -> None:
    with SessionLocal() as db:
        try:
            for policy_path in ROLE_POLICIES:
                sync_role_policy_file(db, policy_path)
        except Exception:
            db.rollback()
            raise


def main() -> int:
    # Provider credentials stay in the encrypted platform registry. Hermes
    # runtimes receive only the local GMV gateway token via systemd.
    _sync_role_routes()
    TRIGGER.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
