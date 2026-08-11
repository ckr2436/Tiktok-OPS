from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


GATEWAY_BASE_URL = "http://127.0.0.1:8650/v1"
GATEWAY_PROVIDER = "gmv_gateway"
GATEWAY_KEY_ENV = "GMV_AI_GATEWAY_KEY"
REMOVED_UPSTREAM_ENV_KEYS = frozenset(
    {
        "TOAPIS_API_KEY",
        "OPENROUTER_API_KEY",
        "COULTRA_API_KEY",
        "SUB2API_API_KEY",
    }
)
RUNTIMES: dict[str, dict[str, Any]] = {
    "general": {
        "home": Path("/home/hermes/.hermes"),
        "logical_model_id": "gmv-hermes-general-v1",
        "max_tokens": 8192,
        "migrate_auxiliary": True,
    },
    "ads_realtime": {
        "home": Path("/home/hermes/.hermes-ads"),
        "logical_model_id": "gmv-ads-realtime-v1",
        "max_tokens": 2048,
        "migrate_auxiliary": False,
    },
    "ads_review": {
        "home": Path("/home/hermes/.hermes-ads-review"),
        "logical_model_id": "gmv-ads-review-v1",
        "max_tokens": 8192,
        "migrate_auxiliary": False,
    },
    "video_analyst": {
        "home": Path("/home/hermes/.hermes-video-analyst"),
        "logical_model_id": "gmv-shop-video-analyst-v1",
        "max_tokens": 4000,
        "migrate_auxiliary": False,
    },
}


def _gateway_provider(logical_model_id: str) -> dict[str, Any]:
    return {
        "name": "GMV AI Gateway",
        "base_url": GATEWAY_BASE_URL,
        "key_env": GATEWAY_KEY_ENV,
        "transport": "chat_completions",
        "default_model": logical_model_id,
        "models": {logical_model_id: {}},
    }


def _rewrite_config(data: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    logical_model_id = str(runtime["logical_model_id"])
    model = dict(data.get("model") or {})
    model.update(
        {
            "default": logical_model_id,
            "provider": f"custom:{GATEWAY_PROVIDER}",
            "base_url": GATEWAY_BASE_URL,
            "api_mode": "chat_completions",
            "max_tokens": int(runtime["max_tokens"]),
        }
    )
    data["model"] = model
    data["providers"] = {GATEWAY_PROVIDER: _gateway_provider(logical_model_id)}
    data["fallback_providers"] = []

    if runtime.get("migrate_auxiliary"):
        auxiliary = dict(data.get("auxiliary") or {})
        for task, raw in auxiliary.items():
            if not isinstance(raw, dict):
                continue
            task_config = dict(raw)
            task_model = (
                "gmv-hermes-general-vision-v1"
                if str(task) == "vision"
                else "gmv-hermes-general-aux-v1"
            )
            task_config.update(
                {
                    "provider": f"custom:{GATEWAY_PROVIDER}",
                    "model": task_model,
                    "base_url": "",
                    "api_key": "",
                }
            )
            auxiliary[task] = task_config
        data["auxiliary"] = auxiliary
    return data


def _atomic_write(path: Path, content: str, *, mode: int, uid: int, gid: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _strip_upstream_keys(env_path: Path) -> bool:
    if not env_path.exists():
        return False
    stat = env_path.stat()
    original = env_path.read_text(encoding="utf-8")
    kept = [
        line
        for line in original.splitlines()
        if line.split("=", 1)[0].strip() not in REMOVED_UPSTREAM_ENV_KEYS
    ]
    updated = "\n".join(kept).rstrip()
    updated = f"{updated}\n" if updated else ""
    if updated == original:
        return False
    _atomic_write(
        env_path,
        updated,
        mode=stat.st_mode & 0o777,
        uid=stat.st_uid,
        gid=stat.st_gid,
    )
    return True


def configure_runtime(name: str, *, backup_root: Path, dry_run: bool) -> dict[str, Any]:
    runtime = RUNTIMES[name]
    home = Path(runtime["home"])
    config_path = home / "config.yaml"
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    original = config_path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(original)
    if not isinstance(loaded, dict):
        raise ValueError(f"{config_path} must contain a YAML object")
    updated = yaml.safe_dump(
        _rewrite_config(loaded, runtime),
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )
    changed = updated != original
    env_path = home / ".env"
    env_has_removed_keys = False
    if env_path.exists():
        env_has_removed_keys = any(
            line.split("=", 1)[0].strip() in REMOVED_UPSTREAM_ENV_KEYS
            for line in env_path.read_text(encoding="utf-8").splitlines()
        )
    if dry_run:
        return {
            "runtime": name,
            "config_changed": changed,
            "upstream_keys_to_remove": env_has_removed_keys,
        }

    backup_dir = backup_root / name
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    shutil.copy2(config_path, backup_dir / "config.yaml")
    if env_path.exists():
        shutil.copy2(env_path, backup_dir / ".env")
    stat = config_path.stat()
    if changed:
        _atomic_write(
            config_path,
            updated,
            mode=stat.st_mode & 0o777,
            uid=stat.st_uid,
            gid=stat.st_gid,
        )
    removed = _strip_upstream_keys(env_path)
    return {
        "runtime": name,
        "config_changed": changed,
        "upstream_keys_removed": removed,
        "backup": str(backup_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime",
        action="append",
        choices=sorted(RUNTIMES),
        help="Runtime to configure; repeat the option or omit it for all runtimes.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    selected = args.runtime or list(RUNTIMES)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = Path(f"/data/gmv_ops/deploy_backups/hermes_unified_routing_{timestamp}")
    for name in selected:
        print(configure_runtime(name, backup_root=backup_root, dry_run=bool(args.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
