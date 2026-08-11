from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import app.services.ai_video.accounts as accounts_module
from app.data.models.ai_routing import AiModelRoute
from app.data.models.kie_api import AiProviderModelSetting, KieApiKey
from app.data.models.users import User
from app.core.deps import SessionUser
from app.core.errors import APIError
from app.services.ai_video.accounts import (
    OMNI_FLASH_MODEL,
    decrypt_api_key,
    encrypt_api_key,
    key_supports_model,
    key_model_priorities,
    key_scopes,
    list_model_keys,
    normalize_model_priorities,
    normalize_routing_config,
    normalize_scopes,
    normalize_video_model_id,
    provider_model_capabilities,
    provider_catalog,
    legacy_route_is_eligible,
)
from app.features.tenants.ai_video.router import (
    AiVideoBatchItem,
    _assert_persisted_tenant_actor,
    _normalize_item,
)
from app.tasks.ai_video.video_tasks import (
    _content_factory_provider_prompt_contract_error,
)


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self.rows)

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class _FakeDb:
    def __init__(self, rows, settings=None):
        self.rows = rows
        self.settings = settings or []

    def query(self, *args, **_kwargs):
        if args and args[0] is AiProviderModelSetting:
            return _FakeQuery(self.settings)
        if args and args[0] is AiModelRoute:
            return _FakeQuery([])
        return _FakeQuery(self.rows)


class _ActorDb:
    def __init__(self, user):
        self.user = user

    def get(self, model, identity):
        assert model is User
        return self.user if int(identity) == int(self.user.id) else None


def _session_user(*, user_id: int, workspace_id: int, platform: bool) -> SessionUser:
    return SessionUser(
        id=user_id,
        email="actor@example.test",
        username="actor",
        display_name="Actor",
        usercode="000000001",
        is_platform_admin=platform,
        workspace_id=workspace_id,
        role="owner",
        is_active=True,
    )


def test_persisted_tenant_actor_rejects_direct_platform_admin_call():
    user = User(
        id=1,
        workspace_id=1,
        email="platform@example.test",
        username="platform",
        display_name="Platform",
        password_hash="test",
        is_active=True,
        is_platform_admin=True,
        role="owner",
        usercode="000000001",
    )

    with pytest.raises(APIError) as exc_info:
        _assert_persisted_tenant_actor(
            _ActorDb(user),
            workspace_id=3,
            me=_session_user(user_id=1, workspace_id=3, platform=False),
        )

    assert exc_info.value.status_code == 403


def test_persisted_tenant_actor_accepts_matching_tenant_user():
    user = User(
        id=6,
        workspace_id=3,
        email="tenant@example.test",
        username="tenant",
        display_name="Tenant",
        password_hash="test",
        is_active=True,
        is_platform_admin=False,
        role="owner",
        usercode="000200002",
    )

    result = _assert_persisted_tenant_actor(
        _ActorDb(user),
        workspace_id=3,
        me=_session_user(user_id=6, workspace_id=3, platform=False),
    )

    assert result is user


def _key(key_id: int, provider: str, priority: int, model: str = "omni_flash") -> KieApiKey:
    key = KieApiKey(
        name=f"key-{key_id}",
        provider_key=provider,
        api_key_ciphertext="secret",
        is_active=True,
        is_default=False,
        scopes_json=["video:generate", f"video:{model}"],
        model_priorities_json={model: priority},
    )
    key.id = key_id
    return key


def _setting(provider: str, model: str, enabled: bool) -> AiProviderModelSetting:
    row = AiProviderModelSetting(provider_key=provider, model_id=model, is_enabled=enabled)
    row.id = 1
    return row


def test_model_aliases_are_canonicalized():
    assert normalize_video_model_id("gemini_omni_flash") == "omni_flash"
    assert normalize_video_model_id("doubao-seedance-2-0-mini-260615") == "seedance_2_0_mini"


def test_content_factory_provider_io_rejects_unvalidated_or_mutated_prompt():
    task = type("Task", (), {})()
    task.input_json = {
        "content_factory_project_id": 184,
        "prompt": "Refs: @image1=scene",
        "content_factory_reference_manifest": [{"alias": "@image1"}],
    }
    assert "missing validated" in str(
        _content_factory_provider_prompt_contract_error(task)
    )

    task.input_json["content_factory_provider_prompt_contract"] = {
        "validated": True,
        "actual_sha256": hashlib.sha256(
            task.input_json["prompt"].encode("utf-8")
        ).hexdigest(),
    }
    assert _content_factory_provider_prompt_contract_error(task) is None

    task.input_json["prompt"] += " changed"
    assert "changed after semantic validation" in str(
        _content_factory_provider_prompt_contract_error(task)
    )


def test_seedance_direct_prompt_is_rejected_instead_of_silently_truncated():
    with pytest.raises(HTTPException) as exc_info:
        _normalize_item(
            AiVideoBatchItem(
                model="seedance_2_0_mini",
                prompt="x" * 496,
                seconds=10,
                aspect_ratio="9:16",
            )
        )

    assert exc_info.value.status_code == 400
    assert "495" in str(exc_info.value.detail)


def test_expired_local_time_circuit_becomes_eligible(monkeypatch):
    route = AiModelRoute(
        key_id=1,
        provider_key="sub2api",
        workload="video",
        logical_model_id="omni_flash",
        provider_model_id="omni_flash",
        capability="video",
        adapter_type="sub2api",
        priority=1,
        is_enabled=True,
        is_verified=True,
        circuit_open_until=datetime.now() - timedelta(seconds=1),
    )
    monkeypatch.setattr(accounts_module, "_legacy_route_row", lambda *_args: route)

    assert legacy_route_is_eligible(object(), _key(1, "sub2api", 1), "omni_flash")


def test_provider_code_is_authoritative_for_scope_but_operator_priority_wins():
    key = _key(1, "google-gemini", 10)
    key.scopes_json = ["video:generate"]
    key.model_priorities_json = {"omni_flash": 1}
    assert key_supports_model(key, OMNI_FLASH_MODEL)
    assert key_model_priorities(key) == {"omni_flash": 1}


def test_flow2api_image_scope_is_credential_specific():
    image_key = _key(1, "flow2api", 10)
    image_key.scopes_json = [
        "image:nano_banana_pro",
        "image:not-a-deployed-model",
    ]
    image_key.model_priorities_json = {
        "nano_banana_pro": 5,
        "not-a-deployed-model": 1,
    }

    assert key_scopes(image_key) == [
        "image:nano_banana_pro",
    ]
    assert key_model_priorities(image_key) == {
        "nano_banana_pro": 5,
    }


def test_flow2api_image_only_key_is_not_eligible_for_flow_video():
    image_key = _key(1, "flow2api", 10)
    image_key.scopes_json = ["image:nano_banana_pro"]
    image_key.model_priorities_json = {"nano_banana_pro": 5}

    assert key_scopes(image_key) == ["image:nano_banana_pro"]
    assert key_model_priorities(image_key) == {"nano_banana_pro": 5}
    assert key_supports_model(image_key, OMNI_FLASH_MODEL) is False


def test_routes_apply_capability_filters_before_priority():
    google = _key(1, "google-gemini", 5)
    bandianwa = _key(2, "bandianwa", 10)
    kyy = _key(3, "kyy", 20)
    db = _FakeDb([kyy, bandianwa, google])

    routes = list_model_keys(db, model_id="omni_flash", reference_count=6, aspect_ratio="9:16")
    assert [key.provider_key for key in routes] == ["google-gemini", "bandianwa"]

    routes = list_model_keys(
        db,
        model_id="omni_flash",
        reference_count=5,
        reference_video_count=1,
        aspect_ratio="9:16",
    )
    assert [key.provider_key for key in routes] == ["bandianwa", "kyy"]

    routes = list_model_keys(db, model_id="omni_flash", reference_count=5, aspect_ratio="1:1")
    assert [key.provider_key for key in routes] == ["bandianwa", "kyy"]


def test_first_last_mode_excludes_google_omni():
    google = _key(1, "google-gemini", 5)
    bandianwa = _key(2, "bandianwa", 10)
    db = _FakeDb([google, bandianwa])

    routes = list_model_keys(
        db,
        model_id="omni_flash",
        reference_count=2,
        reference_mode="first_last",
        aspect_ratio="9:16",
    )
    assert [key.provider_key for key in routes] == ["bandianwa"]


def test_scope_and_priority_normalization_rejects_cross_provider_models():
    scopes = normalize_scopes(
        ["video:generate", "video:omni_flash", "video:seedance_2_0_mini"],
        provider_key="bandianwa",
    )
    assert scopes == ["video:generate", "video:omni_flash"]
    priorities = normalize_model_priorities(
        {"omni_flash": 8, "seedance_2_0_mini": 1},
        provider_key="bandianwa",
    )
    assert priorities == {"omni_flash": 8}

    try:
        normalize_routing_config(
            provider_key="bandianwa",
            scopes=["video:generate", "video:seedance_2_0_mini"],
            model_priorities={"seedance_2_0_mini": 1},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("cross-provider scopes must not create a routable key")


def test_stored_scope_cannot_disable_a_code_configured_provider_route():
    missing_scope = _key(1, "bandianwa", 1)
    missing_scope.scopes_json = ["video:generate"]
    valid = _key(2, "bandianwa", 10)
    db = _FakeDb([missing_scope, valid])

    routes = list_model_keys(db, model_id="omni_flash", exclude_key_ids=[2])
    assert routes == [missing_scope]


def test_reference_limit_can_make_all_routes_unavailable():
    db = _FakeDb([
        _key(1, "google-gemini", 5),
        _key(2, "bandianwa", 10),
        _key(3, "kyy", 20),
    ])
    assert list_model_keys(db, model_id="omni_flash", reference_count=8) == []


def test_sub2api_flow_accepts_the_configured_seven_reference_images():
    db = _FakeDb([_key(1, "sub2api", 1)])

    routes = list_model_keys(
        db,
        model_id="omni_flash",
        reference_count=7,
        aspect_ratio="9:16",
        duration=8,
        resolution="720p",
    )

    assert [key.provider_key for key in routes] == ["sub2api"]
    assert list_model_keys(db, model_id="omni_flash", reference_count=8) == []


def test_doubao_seedance_is_first_for_up_to_ten_references():
    doubao = _key(1, "doubao", 1, "seedance_2_0_mini")
    volcengine = _key(2, "volcengine", 10, "seedance_2_0_mini")
    db = _FakeDb([volcengine, doubao])

    routes = list_model_keys(
        db,
        model_id="seedance_2_0_mini",
        reference_count=0,
        duration=4,
        aspect_ratio="9:16",
        resolution="720p",
    )
    assert [key.provider_key for key in routes] == ["doubao", "volcengine"]
    assert provider_model_capabilities(
        "doubao", "seedance_2_0_mini"
    )["prompt_max_characters"] == 500

    routes = list_model_keys(
        db,
        model_id="seedance_2_0_mini",
        reference_count=1,
        duration=4,
        aspect_ratio="9:16",
        resolution="720p",
    )
    assert [key.provider_key for key in routes] == ["doubao", "volcengine"]

    routes = list_model_keys(
        db,
        model_id="seedance_2_0_mini",
        reference_count=2,
        duration=4,
        aspect_ratio="9:16",
        resolution="720p",
    )
    assert [key.provider_key for key in routes] == ["doubao", "volcengine"]

    routes = list_model_keys(
        db,
        model_id="seedance_2_0_mini",
        reference_count=10,
        duration=10,
        aspect_ratio="9:16",
        resolution="720p",
    )
    assert [key.provider_key for key in routes] == ["doubao"]
    assert list_model_keys(
        db,
        model_id="seedance_2_0_mini",
        reference_count=10,
        duration=15,
        aspect_ratio="9:16",
        resolution="720p",
    ) == []
    assert list_model_keys(
        db,
        model_id="seedance_2_0_mini",
        reference_count=11,
        duration=10,
        aspect_ratio="9:16",
        resolution="720p",
    ) == []


def test_toapis_omni_filters_exact_reference_count_duration_and_resolution():
    db = _FakeDb(
        [_key(1, "toapis", 1)],
        settings=[_setting("toapis", "omni_flash", True)],
    )
    assert [key.provider_key for key in list_model_keys(
        db,
        model_id="omni_flash",
        reference_count=3,
        duration=10,
        resolution="720p",
        aspect_ratio="9:16",
    )] == ["toapis"]
    assert list_model_keys(db, model_id="omni_flash", reference_count=2, duration=10) == []
    assert list_model_keys(db, model_id="omni_flash", reference_count=3, duration=8) == []
    assert list_model_keys(
        db,
        model_id="omni_flash",
        reference_count=3,
        duration=10,
        resolution="1080p",
        aspect_ratio="9:16",
    ) == []


def test_toapis_omni_is_disabled_by_default():
    db = _FakeDb([_key(1, "toapis", 1)])
    assert list_model_keys(db, model_id="omni_flash", reference_count=3, duration=10) == []


def test_unified_provider_catalog_includes_hermes_aggregators():
    providers = {item["id"]: item for item in provider_catalog()}
    assert providers["toapis"]["capabilities"] == ["text", "image", "video"]
    assert providers["toapis"]["hermes_managed"] is True
    assert providers["openrouter"]["hermes_managed"] is True
    assert providers["openrouter"]["video_models"] == []


def test_api_key_encryption_round_trip_and_legacy_read():
    plaintext = "test-secret-value"
    ciphertext = encrypt_api_key(plaintext)
    assert ciphertext.startswith("enc:v1:")
    assert plaintext not in ciphertext
    assert decrypt_api_key(ciphertext) == plaintext
    assert decrypt_api_key(plaintext) == plaintext
