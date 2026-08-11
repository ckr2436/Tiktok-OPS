from pathlib import Path

import yaml

from scripts import configure_hermes_unified_gateway as service


def test_async_hermes_worker_enables_every_content_role_used_by_its_tasks():
    repository_root = Path(__file__).resolve().parents[2]
    worker_drop_in = repository_root.joinpath(
        "ops/systemd/gmv-celery-hermes-content-roles.conf"
    ).read_text(encoding="utf-8")

    assert "HERMES_CONTENT_PRODUCER_AGENT_ENABLED=true" in worker_drop_in
    assert "HERMES_CONTENT_DIRECTOR_AGENT_ENABLED=true" in worker_drop_in
    assert "HERMES_CONTENT_CRITIC_AGENT_ENABLED=true" in worker_drop_in


def test_configure_runtime_replaces_direct_provider_and_strips_upstream_keys(
    tmp_path, monkeypatch,
):
    home = tmp_path / "hermes"
    home.mkdir()
    config = {
        "model": {
            "default": "gpt-5.6-terra",
            "provider": "custom:toapis",
            "base_url": "https://toapis.example/v1",
            "api_mode": "chat_completions",
            "max_tokens": 8192,
        },
        "providers": {
            "toapis": {
                "base_url": "https://toapis.example/v1",
                "key_env": "TOAPIS_API_KEY",
            }
        },
        "fallback_providers": [{"provider": "custom:toapis", "model": "luna"}],
        "auxiliary": {
            "vision": {"provider": "custom:toapis", "model": "luna"},
            "compression": {"provider": "custom:toapis", "model": "luna"},
        },
        "agent": {"max_turns": 90},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (home / ".env").write_text(
        "TOAPIS_API_KEY=secret\nOPENROUTER_API_KEY=secret2\nKEEP_ME=yes\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(
        service.RUNTIMES,
        "test_general",
        {
            "home": home,
            "logical_model_id": "gmv-hermes-general-v1",
            "max_tokens": 8192,
            "migrate_auxiliary": True,
        },
    )

    result = service.configure_runtime(
        "test_general",
        backup_root=tmp_path / "backup",
        dry_run=False,
    )

    updated = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert updated["agent"] == {"max_turns": 90}
    assert updated["model"]["default"] == "gmv-hermes-general-v1"
    assert updated["model"]["base_url"] == "http://127.0.0.1:8650/v1"
    assert list(updated["providers"]) == ["gmv_gateway"]
    assert updated["fallback_providers"] == []
    assert updated["auxiliary"]["vision"]["model"] == "gmv-hermes-general-vision-v1"
    assert updated["auxiliary"]["compression"]["model"] == "gmv-hermes-general-aux-v1"
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "TOAPIS_API_KEY" not in env_text
    assert "OPENROUTER_API_KEY" not in env_text
    assert env_text == "KEEP_ME=yes\n"
    assert result["upstream_keys_removed"] is True
    assert Path(result["backup"]).joinpath("config.yaml").exists()
