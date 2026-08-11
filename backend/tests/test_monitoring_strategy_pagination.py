from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.deps import require_platform_admin
from app.data.db import get_db
from app.data.models.gmv_restructured import GmvMonitoringStrategy
from app.features.platform.router_gmvmax_monitoring_strategies import router


def test_monitoring_strategy_list_pages_all_rows_and_preserves_legacy_offset_limit(
    db_session,
):
    db_session.add_all(
        [
            GmvMonitoringStrategy(
                workspace_id=7,
                auth_id=11,
                advertiser_id="advertiser-1",
                store_id="store-1",
                category="GMVMAX",
                task_name="gmvmax.strategy",
                level="CAMPAIGN_DAILY",
                interval_minutes=10,
                enabled=True,
            )
            for _ in range(105)
        ]
    )
    db_session.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_platform_admin] = lambda: object()
    with TestClient(app) as client:
        numbered = client.get(
            "/api/v1/admin/platform/gmvmax/monitoring-strategies",
            params={"page": 2, "page_size": 50},
        )
        last_page = client.get(
            "/api/v1/admin/platform/gmvmax/monitoring-strategies",
            params={"page": 3, "page_size": 50},
        )
        legacy = client.get(
            "/api/v1/admin/platform/gmvmax/monitoring-strategies",
            params={"offset": 50, "limit": 50},
        )

    assert numbered.status_code == 200
    assert numbered.json()["total"] == 105
    assert numbered.json()["page"] == 2
    assert numbered.json()["page_size"] == 50
    assert len(numbered.json()["items"]) == 50
    assert last_page.json()["page"] == 3
    assert len(last_page.json()["items"]) == 5
    assert legacy.json()["page"] == 2
    assert legacy.json()["page_size"] == 50
    assert legacy.json()["total"] == 105
    numbered_ids = {item["id"] for item in numbered.json()["items"]}
    last_page_ids = {item["id"] for item in last_page.json()["items"]}
    assert numbered_ids.isdisjoint(last_page_ids)
    assert len(numbered_ids | last_page_ids) == 55
