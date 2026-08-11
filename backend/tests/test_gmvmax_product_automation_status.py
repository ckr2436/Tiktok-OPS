from datetime import date, datetime
from types import SimpleNamespace

from app.features.tenants.ttb.router import meta


class _FakeRows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.statement = ""

    def execute(self, statement, params):
        self.statement = str(statement)
        return _FakeRows(self._rows)


class _TimezoneQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return SimpleNamespace(
            display_timezone="America/New_York",
            timezone="Etc/GMT+5",
        )


class _TimezoneSession:
    def query(self, *_args, **_kwargs):
        return _TimezoneQuery()


def test_advertiser_date_window_uses_local_midnight_across_dst_boundary():
    start_utc, end_utc = meta._advertiser_date_window_utc(
        _TimezoneSession(),
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
    )

    assert start_utc == datetime(2026, 3, 8, 5, 0, 0)
    assert end_utc == datetime(2026, 3, 9, 4, 0, 0)
    assert (end_utc - start_utc).total_seconds() == 23 * 60 * 60


def test_product_automation_status_prefers_catalog_over_stale_realtime(monkeypatch):
    monkeypatch.setattr(
        meta,
        "_advertiser_date_window_utc",
        lambda *args, **kwargs: (None, None),
    )
    db = _FakeSession(
        [
            {
                "product_id": "product-1",
                "latest_campaign_id": "campaign-1",
                "latest_campaign_name": "Campaign",
                "latest_strategy_enabled": 0,
                "latest_catalog_status": "DISABLE",
                "latest_realtime_status": "ENABLE",
                "latest_effective_status": "DISABLE",
                "active_campaign_count": 0,
            }
        ]
    )

    result = meta._load_product_automation_stats(
        db,
        workspace_id=1,
        auth_id=2,
        advertiser_id="advertiser-1",
        store_id="store-1",
        product_ids=["product-1"],
    )

    assert result["product-1"]["campaign_operation_status"] == "DISABLE"
    assert result["product-1"]["active_campaign_count"] == 0
    normalized_statement = " ".join(db.statement.split())
    assert "mc.effective_operation_status='ENABLE'" in normalized_statement
    assert "from gmv_product_metrics_daily" in normalized_statement
    assert "from gmv_product_metrics_hourly" in normalized_statement
    assert "join managed_campaigns mc" in normalized_statement
    assert "(s.id is not null or r.strategy_id is not null)" not in normalized_statement
    assert "when mc.effective_operation_status='ENABLE' then 0" in normalized_statement
    assert "from product_daily d where d.source_observed_at is not null" in normalized_statement
    assert (
        "d.metric_date=h.metric_date and d.source_observed_at is not null"
        in normalized_statement
    )
    assert ":advertiser_today" not in normalized_statement
    assert "from gmvmax_product_creative_metrics_daily" not in normalized_statement
    assert "r.latest_cost_cents" not in normalized_statement
    assert result["product-1"]["metric_scope"] == "ALL_CAMPAIGNS_PRODUCT"
