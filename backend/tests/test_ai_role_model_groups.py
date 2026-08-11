from app.data.models.ai_routing import AiModelRoute
from app.data.models.kie_api import KieApiKey
from app.services.ai_routing.role_groups import (
    MANAGED_BY,
    managed_role_groups,
    set_role_provider_order,
    sync_role_model_group,
)


def _source(db_session, *, key_id: int, model: str, priority: int, enabled: bool = True):
    route = AiModelRoute(
        key_id=key_id,
        provider_key="toapis",
        workload="default",
        logical_model_id=model,
        provider_model_id=model,
        capability="text",
        adapter_type="openai_chat_completions",
        priority=priority,
        is_enabled=enabled,
        is_verified=True,
        health_status="HEALTHY",
    )
    db_session.add(route)
    db_session.flush()
    return route


def test_role_model_group_is_config_driven_and_preserves_route_health(db_session):
    key = KieApiKey(
        name="role-model-key",
        provider_key="toapis",
        api_key_ciphertext="not-used",
        is_active=True,
        is_default=False,
    )
    db_session.add(key)
    db_session.flush()
    terra = _source(db_session, key_id=key.id, model="terra", priority=20)
    terra.health_status = "CIRCUIT_OPEN"
    luna = _source(db_session, key_id=key.id, model="luna", priority=10)
    policy = {
        "roles": {
            "director": {
                "logical_model_id": "content-director-v1",
                "sources": [
                    {"logical_model_id": "terra", "priority": 1000},
                    {"logical_model_id": "luna", "priority": 2000},
                ],
            }
        }
    }

    result = sync_role_model_group(
        db_session, role="director", policy=policy
    )

    assert result["created"] == 2
    routes = (
        db_session.query(AiModelRoute)
        .filter(AiModelRoute.logical_model_id == "content-director-v1")
        .order_by(AiModelRoute.priority)
        .all()
    )
    assert [(row.provider_model_id, row.priority) for row in routes] == [
        ("terra", 1020),
        ("luna", 2010),
    ]
    assert routes[0].health_status == "CIRCUIT_OPEN"
    assert routes[0].config_json["managed_by"] == MANAGED_BY

    luna.is_enabled = False
    sync_role_model_group(db_session, role="director", policy=policy)
    db_session.refresh(routes[1])
    assert routes[1].is_enabled is False


def test_business_role_uses_own_workload_and_preserves_operator_provider_order(db_session):
    keys = []
    for provider in ("sub2api", "toapis", "coultra"):
        key = KieApiKey(
            name=provider,
            provider_key=provider,
            api_key_ciphertext="not-used",
            is_active=True,
            is_default=False,
        )
        db_session.add(key)
        db_session.flush()
        source = _source(
            db_session,
            key_id=key.id,
            model="gpt-5.6-terra",
            priority={"sub2api": 1, "toapis": 10, "coultra": 20}[provider],
        )
        source.provider_key = provider
        keys.append(key)
    policy = {
        "roles": {
            "ads_review": {
                "display_name": "广告复核",
                "logical_model_id": "gmv-ads-review-v1",
                "workload": "ads_review",
                "sources": [{"logical_model_id": "gpt-5.6-terra", "priority": 1000}],
            }
        }
    }
    sync_role_model_group(db_session, role="ads_review", policy=policy)
    initial = managed_role_groups(db_session)
    role = next(item for item in initial if item["role"] == "ads_review")
    assert role["workload"] == "ads_review"
    assert role["active_route"]["provider_key"] == "sub2api"

    updated = set_role_provider_order(
        db_session,
        role="ads_review",
        provider_order=["coultra", "sub2api", "toapis"],
    )
    assert updated["provider_order"] == ["coultra", "sub2api", "toapis"]
    assert updated["active_route"]["provider_key"] == "coultra"

    sync_role_model_group(db_session, role="ads_review", policy=policy)
    persisted = next(
        item for item in managed_role_groups(db_session) if item["role"] == "ads_review"
    )
    assert persisted["provider_order"] == ["coultra", "sub2api", "toapis"]


def test_effective_group_ignores_disabled_legacy_capability_after_v2_migration(
    db_session,
):
    key = KieApiKey(
        name="multimodal-role-key",
        provider_key="toapis",
        api_key_ciphertext="not-used",
        is_active=True,
        is_default=False,
    )
    db_session.add(key)
    db_session.flush()
    legacy = AiModelRoute(
        key_id=key.id,
        provider_key="toapis",
        workload="default",
        logical_model_id="content-director-v1",
        provider_model_id="terra",
        capability="text",
        adapter_type="openai_chat_completions",
        priority=1001,
        is_enabled=False,
        is_verified=True,
        health_status="HEALTHY",
        # Old sync runs may already have upgraded the manager marker before a
        # later capability migration disabled this row.
        config_json={"managed_by": MANAGED_BY, "role": "director"},
    )
    source = AiModelRoute(
        key_id=key.id,
        provider_key="toapis",
        workload="default",
        logical_model_id="terra-mm",
        provider_model_id="terra-mm",
        capability="multimodal",
        adapter_type="openai_chat_completions",
        priority=1,
        is_enabled=True,
        is_verified=True,
        health_status="HEALTHY",
    )
    db_session.add_all((legacy, source))
    db_session.flush()
    sync_role_model_group(
        db_session,
        role="director",
        policy={
            "roles": {
                "director": {
                    "logical_model_id": "content-director-v1",
                    "capability": "multimodal",
                    "sources": [{"logical_model_id": "terra-mm"}],
                },
            },
        },
    )

    group = next(
        item for item in managed_role_groups(db_session)
        if item["role"] == "director"
    )
    assert group["capability"] == "multimodal"
    assert {route["id"] for route in group["routes"]} == {
        route.id
        for route in db_session.query(AiModelRoute).filter(
            AiModelRoute.logical_model_id == "content-director-v1",
            AiModelRoute.capability == "multimodal",
        )
    }
