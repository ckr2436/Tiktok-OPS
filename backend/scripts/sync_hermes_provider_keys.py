from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.data.db import SessionLocal  # noqa: E402
from app.services.kie_api.accounts import (  # noqa: E402
    OPENROUTER_PROVIDER_KEY,
    TOAPIS_PROVIDER_KEY,
    decrypt_api_key,
    get_effective_key,
    provider_env_var,
)
from app.services.ai_routing.role_groups import sync_role_model_group  # noqa: E402


ENV_FILES = (
    Path("/home/hermes/.hermes/.env"),
    Path("/home/hermes/.hermes-ads/.env"),
    Path("/home/hermes/.hermes-ads-review/.env"),
)
SERVICES = ("hermes-agent.service", "hermes-ads-agent.service", "hermes-ads-review-agent.service")
TRIGGER = Path("/run/gmv/hermes-provider-sync.request")
CONTENT_ROLE_POLICY = BACKEND_ROOT.parent / "ops/hermes-content-director/routing-policy.json"
CONTENT_ROLES = ("director", "critic", "visual_inspector")


def _desired_values() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    with SessionLocal() as db:
        for provider in (TOAPIS_PROVIDER_KEY, OPENROUTER_PROVIDER_KEY):
            env_var = provider_env_var(provider)
            if not env_var:
                continue
            try:
                key = get_effective_key(db, provider_key=provider, require_active=True)
            except ValueError:
                result[env_var] = None
            else:
                result[env_var] = decrypt_api_key(key.api_key_ciphertext).strip() or None
    return result


def _render_env(path: Path, desired: dict[str, str | None]) -> str:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    managed = set(desired)
    lines = [line for line in original.splitlines() if line.split("=", 1)[0].strip() not in managed]
    for name, value in desired.items():
        if value:
            lines.append(f"{name}={value}")
    return "\n".join(lines).rstrip() + "\n"


def _replace_file(path: Path, content: str) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    stat = path.stat() if path.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        if stat is not None:
            os.chown(temp_name, stat.st_uid, stat.st_gid)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return True


def _sync_content_role_routes() -> None:
    policy = json.loads(CONTENT_ROLE_POLICY.read_text(encoding="utf-8"))
    with SessionLocal() as db:
        try:
            for role in CONTENT_ROLES:
                sync_role_model_group(db, role=role, policy=policy)
        except Exception:
            db.rollback()
            raise


def main() -> int:
    desired = _desired_values()
    _sync_content_role_routes()
    changed = False
    for path in ENV_FILES:
        changed = _replace_file(path, _render_env(path, desired)) or changed
    TRIGGER.unlink(missing_ok=True)
    if changed:
        subprocess.run(["systemctl", "restart", *SERVICES], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
