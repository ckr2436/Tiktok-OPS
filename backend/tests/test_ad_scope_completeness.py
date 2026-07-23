from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import APIError
from app.data.models.scheduling import Schedule, ScheduleRun
from app.features.tenants.ttb.gmv_max import router_provider
from app.features.tenants.ttb.router import common
from app.services import (
    gmvmax_hermes_advisor,
    gmvmax_hermes_daily_report,
    gmvmax_smart_guard,
)


class _RowsResult:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def mappings(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SqlLimitAwareDb:
    """Make raw SQL LIMIT clauses observable without a production database."""

    def __init__(self, rows) -> None:
        self._rows = list(rows)
        self.statements: list[str] = []
        self.parameters: list[dict] = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append(sql)
        self.parameters.append(dict(parameters or {}))
        match = re.search(r"\blimit\s+(\d+)", sql, flags=re.IGNORECASE)
        rows = self._rows[: int(match.group(1))] if match else self._rows
        return _RowsResult(rows)


class _StrategyConfigQuery:
    def __init__(self, rows) -> None:
        self._rows = list(rows)
        self.limit_value: int | None = None

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, value: int):
        self.limit_value = int(value)
        return self

    def all(self):
        if self.limit_value is None:
            return list(self._rows)
        return list(self._rows[: self.limit_value])


class _StrategyConfigDb:
    def __init__(self, rows) -> None:
        self.query_result = _StrategyConfigQuery(rows)

    def query(self, *_args):
        return self.query_result


def test_hermes_daily_report_enumerates_more_than_100_scopes() -> None:
    rows = [
        {
            "workspace_id": index,
            "auth_id": index,
            "advertiser_id": f"adv-{index}",
            "store_id": f"store-{index}",
        }
        for index in range(125)
    ]
    db = _SqlLimitAwareDb(rows)

    scopes = gmvmax_hermes_daily_report._load_report_scopes(db)

    assert len(scopes) == 125
    assert "limit 100" not in db.statements[0].lower()


def test_hermes_advisor_enumerates_more_than_200_scopes() -> None:
    rows = [
        {
            "strategy_id": index,
            "workspace_id": index,
            "auth_id": index,
            "campaign_id": f"campaign-{index}",
        }
        for index in range(225)
    ]
    db = _SqlLimitAwareDb(rows)

    scopes = gmvmax_hermes_advisor._load_hermes_scopes(db)

    assert len(scopes) == 225
    assert "limit 200" not in db.statements[0].lower()


def test_smart_guard_loads_every_campaign_item_group() -> None:
    item_group_ids = [f"product-{index:03d}" for index in range(75)]
    db = _SqlLimitAwareDb(item_group_ids)
    campaign = gmvmax_smart_guard.CatalogCampaign(
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        campaign_id="campaign-1",
        campaign_name="Campaign",
        promotion_type="PRODUCT",
        operation_status="ENABLE",
        secondary_status="CAMPAIGN_STATUS_ENABLE",
        budget_value=10000,
        roas_bid=None,
    )

    loaded = gmvmax_smart_guard._campaign_item_group_ids(db, campaign)

    assert loaded == item_group_ids
    assert "limit 50" not in db.statements[0].lower()
    assert db.parameters[0]["store_id"] == "store-1"
    assert db.statements[0].lower().count("store_id=:store_id") == 2


def test_hermes_product_price_searches_beyond_80_strategy_configs() -> None:
    rows = [
        (
            {
                "smart_guard": {
                    "product_effective_prices": {f"other-{index}": "9.99"}
                }
            },
        )
        for index in range(80)
    ]
    rows.append(
        (
            {
                "creative_guard": {
                    "product_effective_prices": {"target-product": "19.95"}
                }
            },
        )
    )
    db = _StrategyConfigDb(rows)

    price, source = router_provider._configured_product_price_for_hermes(
        db,
        workspace_id=1,
        auth_id=2,
        item_group_id="target-product",
    )

    assert price == 19.95
    assert source == "strategy.creative_guard.product_effective_prices"
    assert db.query_result.limit_value is None


def test_product_sync_limit_query_matches_scope_before_bounding(db_session) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    target_schedule = Schedule(
        workspace_id=1,
        task_name=common.SYNC_TASKS["products"],
        schedule_type="oneoff",
        params_json={
            "provider": "tiktok-business",
            "auth_id": 2,
            "scope": "products",
            "options": {
                "advertiser_id": "target-advertiser",
                "store_id": "target-store",
                "product_eligibility": "gmv_max",
            },
        },
        timezone="UTC",
        enabled=False,
    )
    db_session.add(target_schedule)
    db_session.flush()
    db_session.add(
        ScheduleRun(
            schedule_id=int(target_schedule.id),
            workspace_id=1,
            scheduled_for=now - timedelta(minutes=5),
            enqueued_at=now - timedelta(minutes=5),
            status="running",
            idempotency_key="target-running-sync",
            created_at=now - timedelta(minutes=5),
        )
    )

    # These newer runs filled the old global LIMIT 50 prefix, even though none
    # belongs to the target advertiser/store scope.
    for index in range(51):
        schedule = Schedule(
            workspace_id=1,
            task_name=common.SYNC_TASKS["products"],
            schedule_type="oneoff",
            params_json={
                "provider": "tiktok-business",
                "auth_id": 2,
                "scope": "products",
                "options": {
                    "advertiser_id": f"decoy-advertiser-{index}",
                    "store_id": f"decoy-store-{index}",
                    "product_eligibility": "gmv_max",
                },
            },
            timezone="UTC",
            enabled=False,
        )
        db_session.add(schedule)
        db_session.flush()
        db_session.add(
            ScheduleRun(
                schedule_id=int(schedule.id),
                workspace_id=1,
                scheduled_for=now - timedelta(minutes=1),
                enqueued_at=now - timedelta(minutes=1),
                status="success",
                idempotency_key=f"decoy-sync-{index}",
                created_at=now - timedelta(minutes=1),
            )
        )
    db_session.commit()

    with pytest.raises(APIError) as exc_info:
        common._enforce_products_limits(
            db_session,
            workspace_id=1,
            auth_id=2,
            advertiser_id="target-advertiser",
            store_id="target-store",
        )

    assert exc_info.value.code == "SYNC_IN_PROGRESS"
