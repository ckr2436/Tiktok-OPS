from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any
import types

import sqlalchemy as sa
from sqlalchemy import event

from app.data.models.gmv_restructured import (
    GmvCampaign,
    GmvCampaignMetricsHourly,
    PromotionTypeEnum,
)
from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.workspaces import Workspace
from app.services.ttb_gmvmax import sync_gmvmax_metrics_hourly


class StubReportClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[Any] = []

    async def gmv_max_report_get(self, request: Any) -> Any:
        self.calls.append(request)
        normalized_entries = [
            {"metrics": entry, "dimensions": {}} for entry in self.payload.get("list", [])
        ]
        data = types.SimpleNamespace(
            list=normalized_entries,
            page_info=self.payload.get("page_info"),
            summary=self.payload.get("summary"),
        )
        return types.SimpleNamespace(data=data)


@event.listens_for(GmvCampaignMetricsHourly, "before_insert")
def _assign_metrics_id(mapper, connection, target) -> None:  # pragma: no cover - helper for SQLite
    if target.id is not None:
        return
    result = connection.execute(
        sa.text("SELECT COALESCE(MAX(id), 0) + 1 FROM gmv_campaign_metrics_hourly")
    )
    target.id = result.scalar_one()


def test_sync_metrics_hourly_is_idempotent(db_session):
    workspace = Workspace(id=1, name="Metrics", company_code="0002")
    db_session.add(workspace)
    db_session.flush()

    provider_app = OAuthProviderApp(
        id=1,
        provider="tiktok-business",
        name="Provider",
        client_id="client-id",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.com/callback",
    )
    db_session.add(provider_app)
    db_session.flush()

    account = OAuthAccountTTB(
        id=1,
        workspace_id=workspace.id,
        provider_app_id=provider_app.id,
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    db_session.add(account)
    db_session.flush()

    campaign = GmvCampaign(
        id=1,
        workspace_id=workspace.id,
        auth_id=account.id,
        advertiser_id="adv-2",
        campaign_id="cmp-2",
        name="Metrics Campaign",
        status="PAUSED",
        currency="USD",
        store_id="shop-9",
        promotion_type=PromotionTypeEnum.PRODUCT,
    )
    db_session.add(campaign)
    db_session.flush()

    report_payload = {
        "list": [
            {
                "interval_start": "2024-01-01 00:00:00",
                "interval_end": "2024-01-01 00:59:59",
                "impressions": 120,
                "clicks": 6,
                "cost": "3.21",
                "orders": 2,
                "gross_revenue": "9.87",
                "roi": "3.075",
            }
        ],
        "page_info": {"has_more": False},
    }
    client = StubReportClient(report_payload)

    result_first = asyncio.run(
        sync_gmvmax_metrics_hourly(
            db_session,
            client,
            workspace_id=workspace.id,
            auth_id=account.id,
            advertiser_id=campaign.advertiser_id,
            campaign=campaign,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )
    result_second = asyncio.run(
        sync_gmvmax_metrics_hourly(
            db_session,
            client,
            workspace_id=workspace.id,
            auth_id=account.id,
            advertiser_id=campaign.advertiser_id,
            campaign=campaign,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )
    )

    rows = (
        db_session.query(GmvCampaignMetricsHourly)
        .filter(GmvCampaignMetricsHourly.campaign_id == campaign.campaign_id)
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.impressions == 120
    assert row.clicks == 6
    assert row.cost_cents == 321
    assert row.gross_revenue_cents == 987
    assert Decimal(str(row.roi)) == Decimal("3.0750")
    assert result_first["synced_rows"] == 1
    assert result_second["synced_rows"] == 1
