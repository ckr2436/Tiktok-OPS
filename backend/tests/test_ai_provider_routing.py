from __future__ import annotations

import httpx
import pytest
from datetime import datetime, timedelta
from io import BytesIO
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from app.data.models.ai_routing import AiModelRoute, AiProviderModel, AiRouteAttempt
from app.data.models.kie_api import KieApiKey
from app.services.ai_routing.catalog import (
    model_capabilities,
    provider_transport,
)
from app.services.ai_routing.discovery import discover_models_for_key, discover_models_for_provider, ensure_builtin_routes
from app.services.ai_routing.overview import model_catalog_page, route_catalog_page, routing_overview
from app.services.ai_routing.router import AiGatewayError, call_chat_with_failover, probe_route
from app.services.ai_routing.role_groups import MANAGED_BY
from app.services.ai_video.accounts import (
    encrypt_api_key,
    list_model_keys,
    provider_catalog,
    provider_model_settings_catalog,
    update_kie_key,
)
from app.core.deps import SessionUser


def _key(db, name: str, provider: str, priority: int = 10) -> KieApiKey:
    row = KieApiKey(
        name=name,
        provider_key=provider,
        api_key_ciphertext=encrypt_api_key(f"secret-{name}"),
        is_active=True,
        is_default=False,
        model_priorities_json={"omni_flash": priority},
    )
    db.add(row)
    db.commit()
    return row


def _chat_route(db, key, provider, priority):
    row = AiModelRoute(
        key_id=key.id,
        provider_key=provider,
        workload="default",
        logical_model_id="writer",
        provider_model_id="gpt-5.4-mini",
        capability="text",
        adapter_type="openai_chat_completions",
        priority=priority,
        is_enabled=True,
        is_verified=True,
        health_status="HEALTHY",
    )
    db.add(row)
    db.commit()
    return row


def test_model_capability_classification_covers_non_video_models():
    assert model_capabilities("gpt-5.4-mini") == ["text", "multimodal"]
    assert model_capabilities("flux-image-pro") == ["image"]
    assert model_capabilities("nano_banana_pro") == ["image"]
    assert model_capabilities("gemini-3.0-pro-image") == ["image"]
    assert model_capabilities("seedance-2.0") == ["video"]


def test_self_hosted_sub2api_is_a_discoverable_loopback_provider():
    provider = next(item for item in provider_catalog() if item["id"] == "sub2api")
    assert provider["provider_type"] == "self_hosted"
    assert provider["supports_model_discovery"] is True
    assert provider["auto_discovery"] is True
    assert provider["capabilities"] == ["text", "image", "multimodal", "video"]
    assert provider["image_models"] == ["gpt-image-2"]
    assert provider["video_models"] == ["omni_flash"]
    transport = provider_transport("sub2api")
    assert transport is not None
    assert transport.base_url == "http://127.0.0.1:19080/v1"


def test_self_hosted_flow2api_is_the_nano_banana_pro_provider():
    provider = next(item for item in provider_catalog() if item["id"] == "flow2api")
    assert provider["provider_type"] == "self_hosted"
    assert provider["capabilities"] == ["image"]
    assert provider["image_models"] == ["nano_banana_pro"]
    assert provider["video_models"] == []
    transport = provider_transport("flow2api")
    assert transport is not None
    assert transport.base_url == "http://127.0.0.1:19082/v1"


@pytest.mark.anyio
async def test_403_balance_failure_opens_quota_circuit_and_uses_next_route(
    db_session, monkeypatch,
):
    primary_key = _key(db_session, "toapis-empty", "toapis")
    backup_key = _key(db_session, "coultra-funded", "coultra")
    primary = _chat_route(db_session, primary_key, "toapis", 10)
    backup = _chat_route(db_session, backup_key, "coultra", 20)
    calls: list[str] = []

    class Client:
        def __init__(self, *, base_url, **_kwargs):
            self.base_url = str(base_url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            calls.append(self.base_url)
            if "toapis" in self.base_url:
                return httpx.Response(
                    403,
                    json={"error": {"code": "insufficient_user_quota"}},
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"total_tokens": 1},
                },
            )

    monkeypatch.setattr("app.services.ai_routing.router.httpx.AsyncClient", Client)
    result = await call_chat_with_failover(
        db_session,
        logical_model_id="writer",
        messages=[{"role": "user", "content": "write"}],
        request_id="quota-failover",
    )
    assert result["_gmv_route"]["route_id"] == backup.id
    db_session.refresh(primary)
    assert primary.last_error_class == "QUOTA"
    assert primary.health_status == "CIRCUIT_OPEN"
    assert primary.circuit_open_until is not None
    assert len(calls) == 2


@pytest.mark.anyio
async def test_non_policy_request_error_retries_until_success(db_session, monkeypatch):
    key = _key(db_session, "coultra-request-retry", "coultra")
    route = _chat_route(db_session, key, "coultra", 10)
    calls = 0
    idempotency_keys: list[str] = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **kwargs):
            nonlocal calls
            calls += 1
            idempotency_keys.append(kwargs["headers"]["Idempotency-Key"])
            if calls == 1:
                return httpx.Response(400, json={"error": "temporary relay contract error"})
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"total_tokens": 1},
                },
            )

    monkeypatch.setattr("app.services.ai_routing.router.httpx.AsyncClient", Client)
    result = await call_chat_with_failover(
        db_session,
        logical_model_id="writer",
        messages=[{"role": "user", "content": "write"}],
        request_id="request-error-retry",
        max_attempts=2,
        retry_base_delay_seconds=0,
    )

    assert result["_gmv_route"]["route_id"] == route.id
    assert calls == 2
    assert idempotency_keys == ["request-error-retry", "request-error-retry"]


@pytest.mark.anyio
async def test_transient_failure_reaches_threshold_before_frontend_error(
    db_session, monkeypatch,
):
    key = _key(db_session, "coultra-threshold", "coultra")
    _chat_route(db_session, key, "coultra", 10)
    calls = 0

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(503, json={"error": "relay temporarily unavailable"})

    monkeypatch.setattr("app.services.ai_routing.router.httpx.AsyncClient", Client)
    with pytest.raises(AiGatewayError) as caught:
        await call_chat_with_failover(
            db_session,
            logical_model_id="writer",
            messages=[{"role": "user", "content": "write"}],
            request_id="retry-threshold",
            max_attempts=3,
            retry_base_delay_seconds=0,
        )

    assert calls == 3
    assert caught.value.error_class == "UPSTREAM_5XX"
    assert "3 attempts" in str(caught.value)
    attempts = db_session.query(AiRouteAttempt).order_by(AiRouteAttempt.id.asc()).all()
    assert [row.metadata_json["retry_attempt"] for row in attempts] == [1, 2, 3]
    assert [row.metadata_json["retry_round"] for row in attempts] == [1, 2, 3]


@pytest.mark.anyio
async def test_explicit_policy_rejection_stops_without_retry_or_failover(
    db_session, monkeypatch,
):
    first_key = _key(db_session, "toapis-policy", "toapis")
    second_key = _key(db_session, "coultra-policy-backup", "coultra")
    _chat_route(db_session, first_key, "toapis", 10)
    _chat_route(db_session, second_key, "coultra", 20)
    calls = 0

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={"error": {"code": "content_policy_violation"}},
            )

    monkeypatch.setattr("app.services.ai_routing.router.httpx.AsyncClient", Client)
    with pytest.raises(AiGatewayError) as caught:
        await call_chat_with_failover(
            db_session,
            logical_model_id="writer",
            messages=[{"role": "user", "content": "blocked prompt"}],
            request_id="policy-no-retry",
            max_attempts=8,
            retry_base_delay_seconds=0,
        )

    assert calls == 1
    assert caught.value.error_class == "POLICY"


@pytest.mark.anyio
async def test_explicit_balance_failure_is_not_retried_even_with_500_status(
    db_session, monkeypatch,
):
    key = _key(db_session, "coultra-empty-500", "coultra")
    _chat_route(db_session, key, "coultra", 10)
    calls = 0

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(500, json={"error": "insufficient balance"})

    monkeypatch.setattr("app.services.ai_routing.router.httpx.AsyncClient", Client)
    with pytest.raises(AiGatewayError) as caught:
        await call_chat_with_failover(
            db_session,
            logical_model_id="writer",
            messages=[{"role": "user", "content": "write"}],
            request_id="quota-no-retry",
            max_attempts=8,
            retry_base_delay_seconds=0,
        )

    assert calls == 1
    assert caught.value.error_class == "QUOTA"


@pytest.mark.anyio
async def test_managed_role_route_health_is_propagated_to_its_source(
    db_session, monkeypatch,
):
    primary_key = _key(db_session, "role-toapis-empty", "toapis")
    backup_key = _key(db_session, "role-coultra-funded", "coultra")
    primary_source = _chat_route(db_session, primary_key, "toapis", 10)
    backup_source = _chat_route(db_session, backup_key, "coultra", 20)
    primary = AiModelRoute(
        key_id=primary_key.id,
        provider_key="toapis",
        workload="default",
        logical_model_id="content-director-v1",
        provider_model_id="gpt-5.4-mini",
        capability="text",
        adapter_type="openai_chat_completions",
        priority=10,
        is_enabled=True,
        is_verified=True,
        health_status="HEALTHY",
        config_json={"managed_by": MANAGED_BY, "source_route_id": primary_source.id},
    )
    backup = AiModelRoute(
        key_id=backup_key.id,
        provider_key="coultra",
        workload="default",
        logical_model_id="content-director-v1",
        provider_model_id="gpt-5.4-mini",
        capability="text",
        adapter_type="openai_chat_completions",
        priority=20,
        is_enabled=True,
        is_verified=True,
        health_status="HEALTHY",
        config_json={"managed_by": MANAGED_BY, "source_route_id": backup_source.id},
    )
    db_session.add_all([primary, backup])
    db_session.commit()

    class Client:
        def __init__(self, *, base_url, **_kwargs):
            self.base_url = str(base_url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            if "toapis" in self.base_url:
                return httpx.Response(403, json={"error": {"code": "insufficient_user_quota"}})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 1}},
            )

    monkeypatch.setattr("app.services.ai_routing.router.httpx.AsyncClient", Client)
    result = await call_chat_with_failover(
        db_session,
        logical_model_id="content-director-v1",
        messages=[{"role": "user", "content": "plan"}],
        request_id="managed-role-health",
    )

    assert result["_gmv_route"]["route_id"] == backup.id
    for row in (primary, primary_source, backup, backup_source):
        db_session.refresh(row)
    assert primary.health_status == primary_source.health_status == "CIRCUIT_OPEN"
    assert primary.last_error_class == primary_source.last_error_class == "QUOTA"
    assert primary.total_failures == primary_source.total_failures == 1
    assert backup.health_status == backup_source.health_status == "HEALTHY"
    assert backup.total_successes == backup_source.total_successes == 1


def test_operator_priority_is_used_by_legacy_video_selection(db_session):
    slower_default = _key(db_session, "bandianwa-high", "bandianwa", 90)
    operator_first = _key(db_session, "bandianwa-low", "bandianwa", 2)
    ensure_builtin_routes(db_session)
    db_session.commit()
    rows = list_model_keys(db_session, model_id="omni_flash")
    assert [row.id for row in rows[:2]] == [operator_first.id, slower_default.id]


def test_flow2api_image_scope_materializes_disabled_route(db_session):
    key = _key(db_session, "flow2api-gemini-pool", "flow2api")
    key.scopes_json = ["image:nano_banana_pro"]
    key.model_priorities_json = {"nano_banana_pro": 5}
    db_session.add(key)
    db_session.commit()

    ensure_builtin_routes(db_session, [key])
    db_session.commit()

    route = db_session.query(AiModelRoute).filter(
        AiModelRoute.key_id == key.id,
        AiModelRoute.workload == "content_factory_visual",
        AiModelRoute.logical_model_id == "nano_banana_pro",
        AiModelRoute.capability == "image",
    ).one()
    assert route.provider_key == "flow2api"
    assert route.provider_model_id == "gemini-3.0-pro-image"
    assert route.adapter_type == "flow2api_openai_images"
    assert route.priority == 5
    assert route.is_enabled is False
    assert route.is_verified is False
    assert db_session.query(AiModelRoute).filter(
        AiModelRoute.key_id == key.id,
        AiModelRoute.capability == "video",
    ).count() == 0


def test_editing_flow2api_image_key_preserves_image_only_scope(db_session):
    key = _key(db_session, "flow2api-gemini-edit", "flow2api")
    key.scopes_json = ["image:nano_banana_pro"]
    key.model_priorities_json = {"nano_banana_pro": 5}
    db_session.add(key)
    db_session.commit()

    update_kie_key(db_session, key=key, name="renamed-gemini-pool")
    db_session.commit()
    db_session.refresh(key)

    assert key.scopes_json == ["image:nano_banana_pro"]
    assert key.model_priorities_json == {"nano_banana_pro": 5}


def test_platform_video_priority_edit_updates_route_key_summary_and_catalog(db_session):
    from app.features.platform.ai_providers import routes as platform_routes

    key = _key(db_session, "bandianwa-editable", "bandianwa", 90)
    ensure_builtin_routes(db_session)
    db_session.commit()
    route = db_session.query(AiModelRoute).filter(
        AiModelRoute.key_id == key.id,
        AiModelRoute.logical_model_id == "omni_flash",
        AiModelRoute.capability == "video",
    ).one()
    me = SessionUser(
        id=1,
        email="admin@example.com",
        username="admin",
        display_name="Admin",
        usercode="admin",
        is_platform_admin=True,
        workspace_id=1,
        role="owner",
        is_active=True,
    )

    platform_routes.update_ai_route(
        route.id,
        platform_routes.AiRouteUpdateIn(priority=3),
        me=me,
        db=db_session,
    )

    db_session.refresh(key)
    db_session.refresh(route)
    assert route.priority == 3
    assert key.model_priorities_json["omni_flash"] == 3
    item = next(
        row
        for row in provider_model_settings_catalog(db_session)
        if row["provider_key"] == "bandianwa" and row["model_id"] == "omni_flash"
    )
    assert next(row for row in item["routes"] if row["key_id"] == key.id)["priority"] == 3


def test_platform_video_duration_edit_is_route_owned_and_used_by_catalog(db_session):
    from app.features.platform.ai_providers import routes as platform_routes

    key = _key(db_session, "sub2api-variable-duration", "sub2api", 1)
    ensure_builtin_routes(db_session)
    db_session.commit()
    route = db_session.query(AiModelRoute).filter(
        AiModelRoute.key_id == key.id,
        AiModelRoute.logical_model_id == "omni_flash",
        AiModelRoute.capability == "video",
    ).one()
    me = SessionUser(
        id=1,
        email="admin@example.com",
        username="admin",
        display_name="Admin",
        usercode="admin",
        is_platform_admin=True,
        workspace_id=1,
        role="owner",
        is_active=True,
    )

    platform_routes.update_ai_route(
        route.id,
        platform_routes.AiRouteUpdateIn(
            video_durations_seconds=[4, 6, 8, 10]
        ),
        me=me,
        db=db_session,
    )

    db_session.refresh(route)
    assert route.config_json["video_capabilities"]["durations"] == [
        4,
        6,
        8,
        10,
    ]
    item = next(
        row
        for row in provider_model_settings_catalog(db_session)
        if row["provider_key"] == "sub2api"
        and row["model_id"] == "omni_flash"
    )
    route_item = next(
        row for row in item["routes"] if row["key_id"] == key.id
    )
    assert route_item["capabilities"]["durations"] == [4, 6, 8, 10]


def test_periodic_builtin_sync_preserves_an_operator_disabled_route(db_session):
    key = _key(db_session, "toapis-disabled", "toapis")
    ensure_builtin_routes(db_session)
    db_session.commit()
    route = db_session.query(AiModelRoute).filter(
        AiModelRoute.key_id == key.id,
        AiModelRoute.logical_model_id == "gpt-5.4-mini",
        AiModelRoute.capability == "text",
    ).one()
    route.is_enabled = False
    db_session.commit()
    ensure_builtin_routes(db_session)
    db_session.commit()
    db_session.refresh(route)
    assert route.is_enabled is False


def test_new_toapis_text_groups_start_with_uniform_primary_priority(db_session):
    key = _key(db_session, "toapis-uniform-priority", "toapis")
    ensure_builtin_routes(db_session)
    db_session.commit()
    routes = db_session.query(AiModelRoute).filter(
        AiModelRoute.key_id == key.id,
        AiModelRoute.workload == "default",
        AiModelRoute.capability == "text",
        AiModelRoute.logical_model_id.in_(("gpt-5.4-mini", "gpt-5.6-luna", "gpt-5.6-terra")),
    ).all()
    assert {route.logical_model_id: route.priority for route in routes} == {
        "gpt-5.4-mini": 10,
        "gpt-5.6-luna": 10,
        "gpt-5.6-terra": 10,
    }


def test_platform_key_create_seeds_routes_and_provider_is_immutable(db_session, monkeypatch):
    from app.features.platform.ai_providers import routes as platform_routes

    monkeypatch.setattr(platform_routes, "_queue_discovery_if_supported", lambda _key: None)
    monkeypatch.setattr(platform_routes, "request_hermes_provider_sync", lambda _provider: None)
    me = SessionUser(
        id=1,
        email="admin@example.com",
        username="admin",
        display_name="Admin",
        usercode="admin",
        is_platform_admin=True,
        workspace_id=1,
        role="owner",
        is_active=True,
    )
    created = platform_routes.create_key(
        platform_routes.AiProviderKeyCreateIn(
            name="new-bandianwa",
            api_key="secret-value",
            provider_key="bandianwa",
        ),
        me=me,
        db=db_session,
    )
    assert db_session.query(AiModelRoute).filter(AiModelRoute.key_id == created.id).count() == 1
    with pytest.raises(HTTPException) as exc:
        platform_routes.update_key(
            created.id,
            platform_routes.AiProviderKeyUpdateIn(provider_key="toapis"),
            me=me,
            db=db_session,
        )
    assert exc.value.status_code == 409


@pytest.mark.anyio
async def test_discovery_adds_catalog_but_does_not_auto_enable_unknown_routes(db_session, monkeypatch):
    key = _key(db_session, "coultra", "coultra")

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return httpx.Response(200, json={"data": [{"id": "gpt-5.4-mini"}, {"id": "flux-image-pro"}]})

    monkeypatch.setattr("app.services.ai_routing.discovery.httpx.AsyncClient", Client)
    result = await discover_models_for_key(db_session, key=key)
    db_session.commit()
    assert result["discovered"] == 2
    assert db_session.query(AiProviderModel).count() == 2
    routes = db_session.query(AiModelRoute).all()
    assert routes
    assert all(not route.is_enabled and not route.is_verified for route in routes)
    text_route = next(route for route in routes if route.provider_model_id == "gpt-5.4-mini" and route.capability == "text")
    multimodal_route = next(route for route in routes if route.provider_model_id == "gpt-5.4-mini" and route.capability == "multimodal")
    assert text_route.priority == 100
    assert multimodal_route.priority == 100


@pytest.mark.anyio
async def test_flow_discovery_maps_live_image_variants_to_base_alias(
    db_session,
    monkeypatch,
):
    key = _key(db_session, "flow-image-discovery", "flow2api")
    key.scopes_json = ["image:nano_banana_pro"]
    key.model_priorities_json = {"nano_banana_pro": 5}
    db_session.commit()

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return httpx.Response(200, json={"data": [
                {"id": "gemini-3.0-pro-image-portrait"},
                {"id": "gemini-3.0-pro-image-square-2k"},
            ]})

    monkeypatch.setattr(
        "app.services.ai_routing.discovery.httpx.AsyncClient",
        Client,
    )
    result = await discover_models_for_key(db_session, key=key)
    db_session.commit()

    assert "gemini-3.0-pro-image" in result["_model_ids"]
    alias = db_session.query(AiProviderModel).filter(
        AiProviderModel.provider_key == "flow2api",
        AiProviderModel.provider_model_id == "gemini-3.0-pro-image",
    ).one()
    assert alias.discovery_source == "UPSTREAM_ALIAS"
    assert alias.capabilities_json == ["image"]
    route = db_session.query(AiModelRoute).filter(
        AiModelRoute.key_id == key.id,
        AiModelRoute.logical_model_id == "nano_banana_pro",
    ).one()
    assert route.adapter_type == "flow2api_openai_images"
    assert route.is_enabled is False
    assert route.is_verified is False


@pytest.mark.anyio
async def test_coultra_discovery_seeds_known_text_models_as_ordered_disabled_backups(db_session, monkeypatch):
    key = _key(db_session, "coultra-known-models", "coultra")

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return httpx.Response(200, json={"data": [
                {"id": "gpt-5.4-mini"},
                {"id": "gpt-5.4-mini-2026-03-17"},
                {"id": "gpt-5.6-luna"},
                {"id": "gpt-5.6-terra"},
            ]})

    monkeypatch.setattr("app.services.ai_routing.discovery.httpx.AsyncClient", Client)
    await discover_models_for_key(db_session, key=key)
    db_session.commit()

    routes = db_session.query(AiModelRoute).filter(
        AiModelRoute.key_id == key.id,
        AiModelRoute.capability == "text",
        AiModelRoute.logical_model_id.in_(("gpt-5.4-mini", "gpt-5.6-luna", "gpt-5.6-terra")),
    ).all()
    route_map = {(route.logical_model_id, route.provider_model_id): route for route in routes}
    assert route_map[("gpt-5.4-mini", "gpt-5.4-mini")].priority == 100
    assert route_map[("gpt-5.4-mini", "gpt-5.4-mini-2026-03-17")].priority == 20
    assert route_map[("gpt-5.6-luna", "gpt-5.6-luna")].priority == 20
    assert route_map[("gpt-5.6-terra", "gpt-5.6-terra")].priority == 20
    assert all(not route.is_enabled and not route.is_verified for route in route_map.values())


@pytest.mark.anyio
async def test_provider_discovery_scans_all_active_keys_and_unions_models(db_session, monkeypatch):
    first = _key(db_session, "coultra-a", "coultra")
    second = _key(db_session, "coultra-b", "coultra")

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **kwargs):
            auth = kwargs["headers"]["Authorization"]
            model = "gpt-coultra-a" if auth.endswith("secret-coultra-a") else "gpt-coultra-b"
            return httpx.Response(200, json={"data": [{"id": model}]})

    monkeypatch.setattr("app.services.ai_routing.discovery.httpx.AsyncClient", Client)
    result = await discover_models_for_provider(db_session, provider_key="coultra")
    db_session.commit()
    assert result == {
        "provider_key": "coultra",
        "discovered": 2,
        "unavailable": 0,
        "keys_scanned": 2,
        "partial": False,
        "errors": [],
        "source": "UPSTREAM",
    }
    assert {row.provider_model_id for row in db_session.query(AiProviderModel).all()} == {
        "gpt-coultra-a", "gpt-coultra-b",
    }
    assert {row.key_id for row in db_session.query(AiModelRoute).all()} == {first.id, second.id}


@pytest.mark.anyio
async def test_failed_probe_never_commits_temporary_enablement(db_session, monkeypatch):
    key = _key(db_session, "coultra-probe", "coultra")
    route = AiModelRoute(
        key_id=key.id,
        provider_key="coultra",
        workload="default",
        logical_model_id="gpt-probe",
        provider_model_id="gpt-probe",
        capability="text",
        adapter_type="openai_chat_completions",
        priority=10,
        is_enabled=False,
        is_verified=False,
        health_status="UNKNOWN",
    )
    db_session.add(route)
    db_session.commit()

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return httpx.Response(401, json={"error": "bad key"})

    monkeypatch.setattr("app.services.ai_routing.router.httpx.AsyncClient", Client)
    with pytest.raises(AiGatewayError):
        await probe_route(db_session, route_id=route.id, enable_on_success=True)
    db_session.refresh(route)
    assert route.is_enabled is False
    assert route.is_verified is False
    assert route.health_status == "CIRCUIT_OPEN"


@pytest.mark.anyio
async def test_flow_image_probe_downloads_and_decodes_before_enabling(
    db_session,
    monkeypatch,
):
    key = _key(db_session, "flow-image-probe", "flow2api")
    key.scopes_json = ["image:nano_banana_pro"]
    key.model_priorities_json = {"nano_banana_pro": 5}
    route = AiModelRoute(
        key_id=key.id,
        provider_key="flow2api",
        workload="content_factory_visual",
        logical_model_id="nano_banana_pro",
        provider_model_id="gemini-3.0-pro-image",
        capability="image",
        adapter_type="flow2api_openai_images",
        priority=5,
        is_enabled=False,
        is_verified=False,
        health_status="UNKNOWN",
    )
    db_session.add(route)
    db_session.commit()

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("POST", "http://flow.test/v1/chat/completions"),
                json={
                    "choices": [{"message": {"content": "generated"}}],
                    "url": "https://flow-content.google/probe.png",
                },
            )

    async def download(_self, _url):
        output = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(output, format="PNG")
        return output.getvalue(), "image/png"

    monkeypatch.setattr("app.services.ai_routing.router.httpx.AsyncClient", Client)
    monkeypatch.setattr(
        "app.services.flow2api.client.Flow2ApiImageClient.download",
        download,
    )
    result = await probe_route(
        db_session,
        route_id=route.id,
        enable_on_success=True,
    )

    db_session.refresh(route)
    assert result["ok"] is True
    assert route.is_enabled is True
    assert route.is_verified is True
    assert route.health_status == "HEALTHY"


def test_expired_circuit_is_reported_as_half_open_and_eligible(db_session):
    key = _key(db_session, "coultra-half-open", "coultra")
    route = _chat_route(db_session, key, "coultra", 10)
    route.health_status = "CIRCUIT_OPEN"
    route.circuit_open_until = datetime.now() - timedelta(seconds=1)
    db_session.commit()
    item = next(row for row in routing_overview(db_session)["routes"] if row["id"] == route.id)
    assert item["health_status"] == "HALF_OPEN"
    assert item["is_eligible"] is True


def test_admin_catalogs_are_paginated_and_compact_overview_keeps_key_health(db_session):
    key = _key(db_session, "coultra-page", "coultra")
    db_session.add_all([
        AiProviderModel(
            provider_key="coultra",
            provider_model_id="gpt-alpha",
            display_name="GPT Alpha",
            capabilities_json=["text"],
            endpoint_modes_json=["chat_completions"],
            lifecycle_status="VERIFIED",
            is_available=True,
        ),
        AiProviderModel(
            provider_key="coultra",
            provider_model_id="gpt-beta",
            display_name="GPT Beta",
            capabilities_json=["text", "multimodal"],
            endpoint_modes_json=["chat_completions"],
            lifecycle_status="DISCOVERED",
            is_available=True,
        ),
        AiProviderModel(
            provider_key="toapis",
            provider_model_id="image-only",
            display_name="Image Only",
            capabilities_json=["image"],
            endpoint_modes_json=["images"],
            lifecycle_status="DISCOVERED",
            is_available=True,
        ),
    ])
    first_route = _chat_route(db_session, key, "coultra", 10)
    first_route.logical_model_id = "writer-alpha"
    second_route = AiModelRoute(
        key_id=key.id,
        provider_key="coultra",
        workload="default",
        logical_model_id="writer-beta",
        provider_model_id="gpt-beta",
        capability="text",
        adapter_type="openai_chat_completions",
        priority=20,
        is_enabled=False,
        is_verified=False,
        health_status="UNKNOWN",
    )
    db_session.add(second_route)
    db_session.commit()

    models = model_catalog_page(db_session, page=1, page_size=1, provider_key="coultra", capability="text")
    assert models["total"] == 2
    assert len(models["items"]) == 1
    assert models["items"][0]["provider_model_id"] == "gpt-alpha"
    routes = route_catalog_page(db_session, page=1, page_size=10, search="writer-beta")
    assert routes["total"] == 1
    assert routes["items"][0]["logical_model_id"] == "writer-beta"
    enabled_routes = route_catalog_page(db_session, page=1, page_size=10, enabled=True)
    disabled_routes = route_catalog_page(db_session, page=1, page_size=10, enabled=False)
    assert enabled_routes["total"] == 1
    assert enabled_routes["items"][0]["logical_model_id"] == "writer-alpha"
    assert disabled_routes["total"] == 1
    assert disabled_routes["items"][0]["logical_model_id"] == "writer-beta"

    compact = routing_overview(db_session, include_details=False)
    assert "models" not in compact
    assert "routes" not in compact
    assert compact["summary"]["discovered_models"] == 3
    assert compact["summary"]["enabled_routes"] == 1
    assert next(item for item in compact["key_health"] if item["key_id"] == key.id)["health_status"] == "HEALTHY"


@pytest.mark.anyio
async def test_gateway_fails_over_and_records_metadata_only_attempts(db_session, monkeypatch):
    first_key = _key(db_session, "toapis", "toapis")
    second_key = _key(db_session, "coultra", "coultra")
    first = _chat_route(db_session, first_key, "toapis", 10)
    second = _chat_route(db_session, second_key, "coultra", 20)
    calls = 0

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, json={"error": "down"})
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}})

    monkeypatch.setattr("app.services.ai_routing.router.httpx.AsyncClient", Client)
    result = await call_chat_with_failover(
        db_session,
        logical_model_id="writer",
        messages=[{"role": "user", "content": "private prompt must not be logged"}],
        metadata={"source": "test", "prompt": "must be dropped"},
    )
    assert result["_gmv_route"]["route_id"] == second.id
    attempts = db_session.query(AiRouteAttempt).order_by(AiRouteAttempt.id.asc()).all()
    assert [row.status for row in attempts] == ["FAILED", "SUCCEEDED"]
    assert attempts[1].switched_from_route_id == first.id
    assert all("prompt" not in str(row.metadata_json).lower() for row in attempts)


def test_local_gateway_adapts_completed_response_to_openai_sse(monkeypatch):
    from app.services.ai_routing import gateway_server

    async def fake_call(*_args, **_kwargs):
        return {
            "id": "chatcmpl-test",
            "created": 123,
            "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "_gmv_route": {"route_id": 1},
        }

    monkeypatch.setenv("GMV_AI_GATEWAY_KEY", "internal-test-key")
    monkeypatch.setattr(gateway_server, "call_chat_with_failover", fake_call)
    monkeypatch.setattr(
        gateway_server,
        "_resolve_route_scope",
        lambda *_args, **_kwargs: ("video_analyst", "multimodal"),
    )
    gateway_server.app.dependency_overrides[gateway_server._session] = lambda: object()
    try:
        with TestClient(gateway_server.app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer internal-test-key"},
                json={
                    "model": "video-analyst-gpt-5.4-mini",
                    "messages": [{"role": "user", "content": "test"}],
                    "stream": True,
                },
            )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"content": "OK"' in response.text
        assert "data: [DONE]" in response.text
    finally:
        gateway_server.app.dependency_overrides.clear()


def test_local_gateway_forwards_tools_and_streams_tool_calls(monkeypatch):
    from app.services.ai_routing import gateway_server

    captured = {}

    async def fake_call(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "id": "chatcmpl-tool",
            "created": 123,
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{\"id\":1}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }

    monkeypatch.setenv("GMV_AI_GATEWAY_KEY", "internal-test-key")
    monkeypatch.setattr(gateway_server, "call_chat_with_failover", fake_call)
    monkeypatch.setattr(
        gateway_server,
        "_resolve_route_scope",
        lambda *_args, **_kwargs: ("general", "text"),
    )
    gateway_server.app.dependency_overrides[gateway_server._session] = lambda: object()
    try:
        with TestClient(gateway_server.app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer internal-test-key"},
                json={
                    "model": "gmv-hermes-general-v1",
                    "messages": [{"role": "user", "content": "look it up"}],
                    "tools": [{
                        "type": "function",
                        "function": {"name": "lookup", "parameters": {"type": "object"}},
                    }],
                    "tool_choice": "auto",
                    "parallel_tool_calls": True,
                    "response_format": {"type": "json_object"},
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            )
        assert response.status_code == 200
        assert captured["workload"] == "general"
        assert captured["payload_overrides"]["tools"][0]["function"]["name"] == "lookup"
        assert captured["payload_overrides"]["tool_choice"] == "auto"
        assert captured["payload_overrides"]["parallel_tool_calls"] is True
        assert captured["payload_overrides"]["response_format"] == {"type": "json_object"}
        assert '"tool_calls"' in response.text
        assert '"name": "lookup"' in response.text
        assert '"usage"' in response.text
        assert "data: [DONE]" in response.text
    finally:
        gateway_server.app.dependency_overrides.clear()


def test_local_gateway_long_request_retry_profile_is_explicitly_scoped(
    monkeypatch,
):
    from app.services.ai_routing import gateway_server

    monkeypatch.setenv(
        "AI_ROUTING_LONG_REQUEST_MODEL_IDS",
        "large-structured-model, another-large-model",
    )
    monkeypatch.setenv(
        "AI_ROUTING_LONG_REQUEST_TOTAL_BUDGET_SECONDS",
        "540",
    )
    monkeypatch.setenv(
        "AI_ROUTING_LONG_REQUEST_ATTEMPT_TIMEOUT_SECONDS",
        "180",
    )
    monkeypatch.setenv(
        "AI_ROUTING_LONG_REQUEST_TIMEOUT_SECONDS",
        "570",
    )
    monkeypatch.setenv(
        "AI_ROUTING_LONG_REQUEST_MAX_ATTEMPTS",
        "6",
    )

    assert gateway_server._long_request_retry_overrides(
        "ordinary-model"
    ) == {}
    assert gateway_server._long_request_retry_overrides(
        "large-structured-model"
    ) == {
        "timeout_seconds": 570.0,
        "max_attempts": 6,
        "total_budget_seconds": 540.0,
        "attempt_timeout_seconds": 180.0,
    }
