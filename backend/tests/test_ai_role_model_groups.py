from app.data.models.ai_routing import AiModelRoute
from app.data.models.kie_api import KieApiKey
from app.services.ai_routing.role_groups import MANAGED_BY, sync_role_model_group


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
