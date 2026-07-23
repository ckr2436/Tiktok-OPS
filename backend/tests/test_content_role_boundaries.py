from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _normalized(relative_path: str) -> str:
    return " ".join(_read(relative_path).lower().split())


def test_director_is_a_universal_author_not_a_critic_or_template_runner():
    soul = _normalized("ops/hermes-content-director/SOUL.md")

    assert "universal content showrunner" in soul
    assert "whole-series strategy" in soul
    assert "complete script" in soul
    assert "production direction" in soul
    assert "do not copy a fixed mother template" in soul
    assert "never review or approve your own work" in soul
    assert "independent content critic" not in soul
    assert "do not infer a product, platform, audience, duration, video format" not in soul


def test_critic_remains_physically_independent_and_cannot_author():
    soul = _normalized("ops/hermes-content-critic/SOUL.md")
    config = _normalized("ops/hermes-content-critic/config.yaml")

    assert "isolated independent content critic" in soul
    assert "never act as the author" in soul
    assert "rewrite the script" in soul
    assert "fallback_providers: []" in config
    assert "provider: custom:gmv_ai_gateway" in config
    assert "base_url: http://127.0.0.1:8650/v1" in config
    assert "key_env: gmv_ai_gateway_key" in config


def test_showrunner_runtime_has_no_tools_memory_or_fallback_provider():
    config = _normalized("ops/hermes-content-director/config.yaml")

    assert "fallback_providers: []" in config
    assert "api_server: []" in config
    assert "memory_enabled: false" in config
    assert "provider: custom:gmv_ai_gateway" in config
    assert "base_url: http://127.0.0.1:8650/v1" in config
    assert "key_env: gmv_ai_gateway_key" in config


def test_content_roles_receive_the_local_gateway_token_at_process_start():
    for unit in (
        "ops/systemd/hermes-content-director.service",
        "ops/systemd/hermes-content-critic.service",
    ):
        normalized = _normalized(unit)
        assert "environmentfile=/etc/gmv/ai-gateway.env" in normalized
    installer = _normalized(
        "ops/hermes-content-director/install-isolated-content-role.sh"
    )
    assert 'systemctl restart "${role_dir}.service"' in installer
