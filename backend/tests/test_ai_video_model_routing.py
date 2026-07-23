from __future__ import annotations

from app.data.models.ai_routing import AiModelRoute
from app.data.models.kie_api import AiProviderModelSetting, KieApiKey
from app.services.kie_api.accounts import (
    OMNI_FLASH_MODEL,
    decrypt_api_key,
    encrypt_api_key,
    key_supports_model,
    key_model_priorities,
    list_model_keys,
    normalize_model_priorities,
    normalize_routing_config,
    normalize_scopes,
    normalize_video_model_id,
    provider_catalog,
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


def _key(key_id: int, provider: str, priority: int) -> KieApiKey:
    key = KieApiKey(
        name=f"key-{key_id}",
        provider_key=provider,
        api_key_ciphertext="secret",
        is_active=True,
        is_default=False,
        scopes_json=["video:generate", "video:omni_flash"],
        model_priorities_json={"omni_flash": priority},
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


def test_provider_code_is_authoritative_for_scope_but_operator_priority_wins():
    key = _key(1, "google-gemini", 10)
    key.scopes_json = ["video:generate"]
    key.model_priorities_json = {"omni_flash": 1}
    assert key_supports_model(key, OMNI_FLASH_MODEL)
    assert key_model_priorities(key) == {"omni_flash": 1}


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
